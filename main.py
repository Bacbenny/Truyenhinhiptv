import gzip
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import cloudscraper
from flask import Flask, Response, request

app = Flask(__name__)

# ─── Hội Quán TV config ───────────────────────────────────────────────────────
HOIQUAN_FRONTEND_URL  = os.environ.get("HOIQUAN_FRONTEND", "https://sv2.hoiquan4.live")
HOIQUAN_KNOWN_API_BASE= os.environ.get("HOIQUAN_API",      "https://sv.hoiquantv.xyz/api/v1/external")

# ─── Khán Đài A config ───────────────────────────────────────────────────────
KHANDAIA_FRONTEND_URL   = os.environ.get("KHANDAIA_FRONTEND", "https://tructiep.khandaia.link")
KHANDAIA_KNOWN_API_BASE = os.environ.get("KHANDAIA_API",      "https://sv.khandai-a.xyz/api/v1/external")

# ─── EPG — override via env var, otherwise auto-built from /epg.xml endpoint ─
EPG_URL_OVERRIDE = os.environ.get("EPG_URL", "")

# ─── Shared config ────────────────────────────────────────────────────────────
VN_TZ                = timezone(timedelta(hours=7))
SELF_PING_INTERVAL   = 240   # seconds
PREFETCH_INTERVAL    = 300   # seconds — refresh cache every 5 minutes
API_DISCOVERY_TTL    = 3600  # seconds — re-discover API URL every 1 hour

FINISHED_STATUS_STRINGS    = {"finished", "end", "ended", "complete", "completed"}
MATCH_MAX_AGE_SECONDS      = int(os.environ.get("MATCH_MAX_DURATION", 7200))  # 2 h

# ─── Sport logos (Twemoji via jsDelivr) ───────────────────────────────────────
_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
SPORT_LOGOS = {
    "football":   f"{_CDN}/26bd.png",
    "tennis":     f"{_CDN}/1f3be.png",
    "basketball": f"{_CDN}/1f3c0.png",
    "volleyball": f"{_CDN}/1f3d0.png",
    "billiards":  f"{_CDN}/1f3b1.png",
    "badminton":  f"{_CDN}/1f3f8.png",
    "default":    f"{_CDN}/1f3c6.png",
}

# ─── API URL caches ───────────────────────────────────────────────────────────
_hoiquan_api_cache  = {"url": HOIQUAN_KNOWN_API_BASE,  "discovered_at": 0}
_khandaia_api_cache = {"url": KHANDAIA_KNOWN_API_BASE, "discovered_at": 0}

# ─── Playlist content cache ───────────────────────────────────────────────────
def _empty_entry():
    return {"content": None, "gz": None, "etag": None, "built_at": 0,
            "lock": threading.Lock()}

_playlist_cache = {
    "combined": _empty_entry(),
    "hoiquan":  _empty_entry(),
    "khandaia": _empty_entry(),
}

_last_counts = {
    "hoiquan": 0, "khandaia": 0,
    "refreshed_at": 0, "last_error": "",
}

_epg_cache: dict = {"content": None, "gz": None, "etag": None, "built_at": 0}
_epg_lock  = threading.Lock()
EPG_CACHE_TTL = 3600

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_public_url() -> str:
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains: return f"https://{domains.split(',')[0].strip()}"
    render = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render: return render.rstrip("/")
    return f"http://localhost:{os.environ.get('PORT', 5000)}"

def _epg_url() -> str:
    return EPG_URL_OVERRIDE if EPG_URL_OVERRIDE else f"{_get_public_url()}/epg.xml"

def _build_epg_xml() -> str:
    seen_ids: dict[str, tuple[str, str]] = {}
    combined = _playlist_cache.get("combined", {})
    raw = combined.get("content") or b""
    content = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else (raw or "")

    for m in re.finditer(r'#EXTINF[^\n]*?tvg-id="(?P<tid>[^"]*)"[^\n]*?tvg-logo="(?P<tlogo>[^"]*)"[^\n]*?,(?P<label>[^\n]*)', content):
        tid, label, tlogo = m.group("tid").strip(), m.group("label").strip(), m.group("tlogo").strip()
        if tid and tid not in seen_ids: seen_ids[tid] = (label, tlogo)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="IPTV M3U Server">']
    for cid, (name, logo) in seen_ids.items():
        logo_tag = f'\n    <icon src="{logo}" />' if logo else ""
        lines.append(f'  <channel id="{cid}">\n    <display-name>{name}</display-name>{logo_tag}\n  </channel>')
    lines.append("</tv>")
    return "\n".join(lines)

def _get_or_build_epg() -> dict:
    with _epg_lock:
        now = time.time()
        if _epg_cache["content"] is None or (now - _epg_cache["built_at"]) > EPG_CACHE_TTL:
            xml = _build_epg_xml()
            gz  = gzip.compress(xml.encode("utf-8"), compresslevel=6)
            etag = '"' + hashlib.md5(gz).hexdigest() + '"'
            _epg_cache.update({"content": xml, "gz": gz, "etag": etag, "built_at": now})
        return dict(_epg_cache)

def _logo_from_text(text: str) -> str:
    t = text.lower()
    if "tennis" in t: return SPORT_LOGOS["tennis"]
    if any(k in t for k in ["basketball", "bóng rổ", "bong ro", "nba"]): return SPORT_LOGOS["basketball"]
    if any(k in t for k in ["volleyball", "bóng chuyền", "bong chuyen"]): return SPORT_LOGOS["volleyball"]
    if any(k in t for k in ["billiard", "bi-a", "bia"]): return SPORT_LOGOS["billiards"]
    if any(k in t for k in ["badminton", "cầu lông", "cau long"]): return SPORT_LOGOS["badminton"]
    return SPORT_LOGOS["football"]

def _hq_kda_logo(fixture: dict) -> str:
    icon = fixture.get("sport", {}).get("iconUrl", "")
    return icon if icon else _logo_from_text(" ".join([fixture.get("sport", {}).get("name", ""), fixture.get("sport", {}).get("slug", "")]))

_HQ_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# ─── API Discovery & Fetch (Hội Quán / Khán Đài A) ──────────────────────────

def _discover_api(url, base_url, scraper) -> str:
    try:
        r = scraper.get(url, timeout=10)
        js_files = re.findall(r'src="(/assets/[^"]+\.js)"', r.text)
        if js_files:
            js = scraper.get(url.rstrip("/") + js_files[0], timeout=15).text
            hits = re.findall(r'https://sv\.[a-z0-9\-\.]+/api/v1/external', js)
            if hits: return hits[0]
    except Exception: pass
    return base_url

def _fetch_fixtures(api_cache, frontend_url, api_base_known) -> list:
    scraper = cloudscraper.create_scraper()
    now = time.time()
    if now - api_cache["discovered_at"] > API_DISCOVERY_TTL:
        api_cache["url"] = _discover_api(frontend_url, api_base_known, scraper)
        api_cache["discovered_at"] = now
    
    url = api_cache["url"].rstrip("/") + "/fixtures/unfinished"
    resp = scraper.get(url, headers={**_HQ_HEADERS, "Referer": frontend_url + "/"}, timeout=15)
    data = resp.json()
    return data.get("data", []) if data.get("success") else []

def _fixture_is_active(fixture: dict) -> bool:
    if fixture.get("isFinished") or fixture.get("isEnd"): return False
    return True

def _pick_best_stream(streams: list) -> str:
    for q in ("fhd", "hd", "sd"):
        for s in streams:
            if s.get("name", "").lower() == q and s.get("sourceUrl"): return s["sourceUrl"]
    return streams[0].get("sourceUrl", "") if streams else ""

def _build_fixture_lines(fixtures: list, group_title: str) -> list:
    lines = []
    for fixture in sorted(fixtures, key=lambda f: f.get("startTime") or ""):
        if not _fixture_is_active(fixture): continue
        home, away = fixture.get("homeTeam", {}).get("name", "Home"), fixture.get("awayTeam", {}).get("name", "Away")
        for entry in fixture.get("fixtureCommentators", []):
            stream_url = _pick_best_stream(entry.get("commentator", {}).get("streams", []))
            if stream_url:
                lines.append(f'#EXTINF:-1 tvg-logo="{_hq_kda_logo(fixture)}" group-title="{group_title}",{home} VS {away}')
                lines.append(stream_url)
    return lines

# ─── Background Tasks ────────────────────────────────────────────────────────

def _refresh_all_playlists():
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_fetch_fixtures, _hoiquan_api_cache, HOIQUAN_FRONTEND_URL, HOIQUAN_KNOWN_API_BASE)
        f2 = ex.submit(_fetch_fixtures, _khandaia_api_cache, KHANDAIA_FRONTEND_URL, KHANDAIA_KNOWN_API_BASE)
        hq_lines = _build_fixture_lines(f1.result(), "Hội Quán TV")
        kda_lines = _build_fixture_lines(f2.result(), "Khán Đài A")

    epg_header = f'#EXTM3U url-tvg="{_epg_url()}" x-tvg-url="{_epg_url()}"'
    
    for key, lines in [("hoiquan", hq_lines), ("khandaia", kda_lines)]:
        text = epg_header + "\n" + "\n".join(lines)
        packed = {"content": text.encode("utf-8"), "gz": gzip.compress(text.encode("utf-8")), "etag": hashlib.md5(text.encode("utf-8")).hexdigest(), "built_at": time.time()}
        _playlist_cache[key].update(packed)
        
    combined_text = epg_header + "\n" + "\n".join(hq_lines + kda_lines)
    _playlist_cache["combined"].update({"content": combined_text.encode("utf-8"), "gz": gzip.compress(combined_text.encode("utf-8")), "etag": hashlib.md5(combined_text.encode("utf-8")).hexdigest(), "built_at": time.time()})
    _last_counts.update({"hoiquan": len(hq_lines)//2, "khandaia": len(kda_lines)//2, "refreshed_at": time.time()})

def _prefetch_loop():
    while True:
        try: _refresh_all_playlists()
        except: pass
        time.sleep(PREFETCH_INTERVAL)

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/live.m3u")
def live_m3u():
    entry = _playlist_cache["combined"]
    return Response(entry["gz"], mimetype="application/x-mpegurl", headers={"Content-Encoding": "gzip", "ETag": entry["etag"]})

@app.route("/hoiquan.m3u")
def hoiquan_m3u():
    entry = _playlist_cache["hoiquan"]
    return Response(entry["gz"], mimetype="application/x-mpegurl", headers={"Content-Encoding": "gzip", "ETag": entry["etag"]})

@app.route("/khandaia.m3u")
def khandaia_m3u():
    entry = _playlist_cache["khandaia"]
    return Response(entry["gz"], mimetype="application/x-mpegurl", headers={"Content-Encoding": "gzip", "ETag": entry["etag"]})

@app.route("/epg.xml")
def epg_xml():
    entry = _get_or_build_epg()
    return Response(entry["gz"], mimetype="application/xml", headers={"Content-Encoding": "gzip", "ETag": entry["etag"]})

@app.route("/")
def index():
    return f"<h2>IPTV Server</h2><p>Hội Quán: {_last_counts['hoiquan']} | Khán Đài A: {_last_counts['khandaia']}</p>"

if __name__ == "__main__":
    threading.Thread(target=_prefetch_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
