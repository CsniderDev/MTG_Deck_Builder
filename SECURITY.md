# Security & cost-abuse protections

This document covers what's already built into the app versus what you still need to
configure manually before/after deploying publicly.

## What's implemented in code

### Rate limiting (`backend/app/main.py`)

- Every route gets a default limit of **60 requests/minute per IP** (`slowapi`
  `default_limits`).
- `/api/decks/build` and `/api/decks/revamp` (the only routes that can trigger an LLM
  call) additionally get **5 requests/minute** and **`DAILY_IP_LIMIT`/day** (default
  **50/day**) per IP.
- Exceeding a limit returns `429` with `{"detail": "Rate limit exceeded (...)"}`, matching
  the rest of the API's error shape.
- The rate-limit identity is the caller's IP (`request.client.host`) unless
  `TRUST_X_FORWARDED_FOR=true`, in which case the first `X-Forwarded-For` entry is used
  instead. **Only enable that when you're actually deployed behind a proxy/CDN that
  overwrites the header itself** (see the Cloudflare/nginx step below) — otherwise any
  client can spoof the header and dodge the per-IP limits entirely.

### Daily LLM call budget (`backend/app/services/llm.py`)

- `LLM_DAILY_CALL_LIMIT` (default **300**) caps real LLM API calls (Gemini or OpenAI,
  whichever `LLM_PROVIDER` selects) per UTC day,
  shared across all users, independent of per-IP rate limiting. Once hit, `LLMService`
  returns `None` and every build/revamp request falls back to the heuristic path instead
  of failing outright.
- Current usage is exposed on `GET /api/health` as `llm_calls_today` /
  `llm_daily_call_limit` so you can check it without digging through logs.
- If `ALERT_WEBHOOK_URL` is set (any endpoint that accepts `{"text": "..."}`, e.g. a Slack
  incoming webhook), a notification fires the first time usage crosses 80% and 100% of
  the daily budget.

### Circuit breaker (`backend/app/services/llm.py`)

- After `LLM_CIRCUIT_FAILURE_THRESHOLD` (default **3**) consecutive LLM failures,
  calls are skipped entirely (falling back to heuristic builds) for
  `LLM_CIRCUIT_COOLDOWN_SECONDS` (default **60s**) instead of continuing to hit a
  failing/unavailable LLM endpoint.

### Response caching (`backend/app/services/deck_builder.py`)

- Identical build/revamp requests (same commander, bracket, prompt, decklist, etc.) are
  served from an in-memory cache for `RESPONSE_CACHE_TTL_SECONDS` (default **120s**)
  instead of re-hitting the LLM/Scryfall/EDHREC. This mainly protects against accidental
  duplicate submissions (double-clicks, retries) and naive repeat-request scripts.

### Access code (optional, recommended for a friends-only deployment)

- Set `ACCESS_CODE` on the backend and share it with the people allowed to use the
  site. Build/revamp requests without it (in the `X-Access-Code` header) get a 401; the
  comparison is constant-time. The site shows an "Access code" field when the backend
  requires one and remembers it in the browser. Leave it empty to disable.
- This matters behind Render's proxy, where per-IP rate limits see every visitor as the
  same client - the code is what stops strangers from spending your LLM quota.

### Pinned dependencies

- `backend/requirements-lock.txt` pins every backend package to an exact, hash-checked
  version for Python 3.12 on Linux, and Render installs from it with `--require-hashes`.
  `requirements.txt` keeps the human-edited version ranges. Regenerate the lock after
  changing `requirements.txt` with the command at the top of the lock file.
- `.github/dependabot.yml` opens weekly update PRs (grouped minor/patch for pip and npm,
  separate PRs for major versions) so upgrades are reviewed instead of arriving silently.

### Bot verification (Cloudflare Turnstile, optional)

- Backend: set `TURNSTILE_SECRET_KEY` to require a valid Turnstile token on every
  build/revamp request; leave it empty to disable (default, safe for local dev).
- Frontend: set `VITE_TURNSTILE_SITE_KEY` to render the widget on the build form and the
  revamp box; leave it empty and the widget doesn't render at all.
- Get a site/secret key pair at <https://dash.cloudflare.com/> → Turnstile (free). The two
  keys must be set together — the backend won't accept tokens from a mismatched key.

### Request size limits (`backend/app/models/schemas.py`)

- `prompt`/`change_request` were already capped at 4000 chars. Added: `gamechangers` /
  `banned_list` capped at 500 entries each, and `previous_decklist` on revamp capped at
  300 entries — previously unbounded, which meant a client could pad a revamp request to
  inflate the prompt sent to the LLM.

### Security headers (`backend/app/main.py`)

- Every response gets `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: same-origin`, a restrictive `Content-Security-Policy` (this is a pure
  JSON API, so `default-src 'none'`), and `Strict-Transport-Security` (only meaningful
  once you're actually served over HTTPS — see below).

### CORS

- `FRONTEND_ORIGIN` now accepts a comma-separated list, so you can allow your production
  domain and (optionally) a staging domain without wildcarding. Set it to your real
  production origin(s) only — don't leave it pointed at `localhost` in production.

## What you still need to do manually

None of this is code the app can enforce on its own — it's account/infrastructure setup.

1. **Rotate your LLM API key (Gemini or OpenAI)** before going live. It's been sitting in a local
   `backend/.env` during development (never committed — `.gitignore` already excludes
   `.env`); rotating it means a leaked dev copy can't be used against your production
   deployment.
2. **Set a budget alert (and hard quota, if your plan supports it) on the LLM API key**
   in Google AI Studio / Google Cloud Console (or, for OpenAI, a monthly budget limit
   under platform.openai.com → Settings → Limits), so a bug or a bypass of the app-level
   protections above still can't produce a surprise bill. If it's a Google Cloud API key
   rather than an AI Studio key, also restrict it to the Generative Language API and, if
   possible, your server's IP.
3. **Put a CDN/WAF in front of the deployment** — Cloudflare's free tier is enough. This
   gets you edge-level DDoS protection, bot scoring, and rate limiting before traffic even
   reaches uvicorn, and it's also what makes `TRUST_X_FORWARDED_FOR=true` safe to enable
   (Cloudflare overwrites `X-Forwarded-For` on every request it proxies, so the header
   can no longer be spoofed by the client once Cloudflare sits in front of you).
4. **Enforce HTTPS end-to-end** and run uvicorn behind a real reverse proxy (nginx, Caddy,
   or your platform's built-in one) rather than exposing it directly on the internet.
5. **Get and configure Turnstile keys** if you want the bot-verification gate active (see
   above) — without them the app runs exactly as it does today.
6. **Set up basic alerting** — at minimum, watch the `llm_calls_today` field on
   `/api/health` or wire up `ALERT_WEBHOOK_URL` so you find out about a usage spike before
   your monthly bill does.

See `backend/.env.example` and `frontend/.env.example` for every environment variable
mentioned above with its default value.
