/**
 * TieuLam TV IPTV Relay — Cloudflare Worker (ES Modules)
 *
 * Thay thế relay cũ (chỉ trả live matches) bằng phiên bản đầy đủ:
 *   - Fetch toàn bộ lịch thi đấu từ Direct API (giống scraper.py)
 *   - Cache Cloudflare 5 phút (giảm load API)
 *   - Tự động đọc api_base mới nhất từ config/api.json trên repo
 *   - CORS + auth token (RELAY_SECRET env var)
 *
 * Env vars (Cloudflare Worker secrets):
 *   RELAY_SECRET   — token bảo vệ endpoint (phải khớp X-Relay-Token header)
 *   TIEULAM_API    — fallback API base nếu config.json không đọc được
 */

const FRONTEND   = 'https://sv2.tieulamtv.xyz';
const CONFIG_URL =
  'https://raw.githubusercontent.com/Bacbenny/testtieulam/dekki/config/api.json';
const CACHE_TTL  = 300;   // 5 phút
const MAX_AGE    = 10800; // 3 giờ — cửa sổ trận đã bắt đầu
const LIVE_MAX   = 14400; // 4 giờ — tối đa khi is_live bị kẹt
const FUTURE_H   = 72;    // giờ tương lai tối đa
const MAX_PAGES  = 5;

const CORS = {
  'Access-Control-Allow-Origin':  '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, X-Relay-Token',
};

// ── helpers ────────────────────────────────────────────────────────

function vnNow() {
  // Trả về Date đã cộng offset GMT+7 (dùng cho query window)
  return new Date(Date.now() + 7 * 3600 * 1000);
}

function isoLocal(d) {
  return d.toISOString().slice(0, 19); // "2026-06-24T15:00:00"
}

function buildPayload(page) {
  const now  = vnNow();
  const past = new Date(now.getTime() - MAX_AGE * 1000);
  const future = new Date(now.getTime() + FUTURE_H * 3600 * 1000);
  return {
    queries: [
      { field: 'start_date', type: 'gte',       value: isoLocal(past)   },
      { field: 'start_date', type: 'lte',       value: isoLocal(future) },
      { field: 'blv',        type: 'not_equal', value: null             },
      { field: 'blv',        type: 'not_equal', value: ''               },
    ],
    query_and: true,
    limit:     50,
    page,
    order_asc: 'start_date',
  };
}

async function getApiBase(env) {
  try {
    const r = await fetch(CONFIG_URL, { cf: { cacheTtl: 300, cacheEverything: true } });
    if (r.ok) {
      const cfg = await r.json();
      if (cfg.api_base) return cfg.api_base.replace(/\/$/, '');
    }
  } catch (_) {}
  return (env.TIEULAM_API || 'https://api.tlap17062026.com').replace(/\/$/, '');
}

async function fetchAllMatches(apiBase) {
  const all = [];
  const headers = {
    'Content-Type': 'application/json',
    'Referer':      `${FRONTEND}/`,
    'Origin':       FRONTEND,
    'User-Agent':
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36',
  };

  for (let page = 1; page <= MAX_PAGES; page++) {
    let r;
    try {
      r = await fetch(`${apiBase}/matches/graph`, {
        method:  'POST',
        headers,
        body:    JSON.stringify(buildPayload(page)),
        cf:      { cacheTtl: 0 },
      });
    } catch (e) {
      console.error(`[relay] fetch page ${page} error:`, e.message);
      break;
    }

    if (!r.ok) {
      console.error(`[relay] API HTTP ${r.status} page ${page}`);
      break;
    }

    let batch;
    try {
      const json = await r.json();
      batch = json.data || [];
    } catch (_) {
      break;
    }

    all.push(...batch);
    if (batch.length < 50) break; // last page
  }

  return all;
}

function parseStartMs(value) {
  if (!value) return null;
  const s = String(value).replace('Z', '+00:00');
  const t = Date.parse(s);
  if (!Number.isNaN(t)) return t;
  // Naive VN time: append +07:00
  return Date.parse(s.includes('+') || s.includes('Z') ? s : `${s}+07:00`);
}

function filterMatches(matches) {
  const nowMs = Date.now();
  return matches.filter((m) => {
    const startMs = parseStartMs(m.start_date);
    if (!startMs) {
      return (m.is_live || m.live_integrated) && (m.stream_key || m.source_live);
    }
    const elapsed = (nowMs - startMs) / 1000;
    if (elapsed < -172800) return false;
    if (m.is_live || m.live_integrated) return elapsed <= LIVE_MAX;
    if (elapsed > MAX_AGE) return false;
    return true;
  });
}

// ── main handler ───────────────────────────────────────────────────

export default {
  async fetch(request, env, ctx) {
    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: CORS });
    }

    // Auth
    const secret = (env.RELAY_SECRET || '').trim();
    if (secret) {
      const token = (request.headers.get('X-Relay-Token') || '').trim();
      if (token !== secret) {
        return new Response(JSON.stringify({ error: 'Unauthorized', data: [] }), {
          status: 401,
          headers: { ...CORS, 'Content-Type': 'application/json' },
        });
      }
    }

    // Try Cloudflare cache
    const cache    = caches.default;
    const cacheKey = new Request('https://cache.internal/tieulam-relay-v2', { method: 'GET' });
    const cached   = await cache.match(cacheKey);
    if (cached) {
      return new Response(cached.body, {
        status:  cached.status,
        headers: { ...CORS, ...Object.fromEntries(cached.headers), 'X-Cache': 'HIT' },
      });
    }

    // Fetch fresh data
    let matches = [];
    let apiBase  = 'unknown';
    let fetchErr = null;

    try {
      apiBase = await getApiBase(env);
      matches = filterMatches(await fetchAllMatches(apiBase));
      console.log(`[relay] fetched ${matches.length} matches from ${apiBase}`);
    } catch (e) {
      fetchErr = e.message;
      console.error('[relay] fatal:', e.message);
    }

    const body = JSON.stringify({
      data:      matches,
      total:     matches.length,
      api_base:  apiBase,
      cached_at: new Date().toISOString(),
      error:     fetchErr,
    });

    const resp = new Response(body, {
      status:  fetchErr && matches.length === 0 ? 502 : 200,
      headers: {
        ...CORS,
        'Content-Type':     'application/json',
        'Cache-Control':    `public, max-age=${CACHE_TTL}`,
        'X-Cache':          'MISS',
        'X-Total-Matches':  String(matches.length),
      },
    });

    // Store in cache (only on success)
    if (!fetchErr || matches.length > 0) {
      ctx.waitUntil(cache.put(cacheKey, resp.clone()));
    }

    return resp;
  },
};
