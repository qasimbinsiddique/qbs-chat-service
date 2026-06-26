# QBS Global — Website Chat Backend

Small FastAPI service that powers the chat widget on **qbsglobal.ae**. It answers
visitor questions from `knowledge-base.csv` using **Gemini** (free tier) as the brain,
and nudges high-intent visitors to *Book a Meeting* / WhatsApp / email.

Replaces the dead n8n webhook the widget used to POST to.

## Contract (matches `widget.js`)
- **POST** `/chat`  →  body `{ "sessionId": "...", "chatInput": "user message" }`
- **200**  →  `{ "output": "<reply>" }`
- **GET** `/health`  →  `{ "ok": true, "kb_rows": 72, "model": "...", "key_set": true }`

## Contact form ingest (`POST /contact-submit`)
The website's nginx proxies `/api/contact-submit` here (previously it proxied
straight to PostgREST, which saved the lead but **notified nobody** and left a
failed save with **no trace** — a real lead was lost that way on 2026-06-25).
This handler inserts the lead into the same `contact_submissions` table, pings
the Discord webhook on success, and **dead-letters** (logs + alerts) on failure.

- **POST** `/contact-submit`  →  same JSON body the lead form sends
  (`first_name`, `last_name`, `email`, `interest`, `message`, `lead_type`,
  `source_url`, `user_agent`, `utm_*`, `gclid` — fields are whitelisted/capped).
- **204** on a confirmed save (mirrors the old PostgREST `return=minimal`, so the
  browser's `r.ok` success path is unchanged).
- **400** on bad JSON / missing-or-invalid email · **429** rate-limited ·
  **500** if `SUPABASE_*` env is unset · **502** on a save failure (PostgREST
  error or transport failure) → the browser's retry/error path engages and a
  `🔴 DEAD-LETTER` alert fires for manual capture.
- Requires `SUPABASE_REST_URL` + `SUPABASE_ANON_KEY` (same as lead persistence)
  and `ALERT_WEBHOOK` for the notifications.

Run the tests: `./.venv/bin/python test_contact_submit.py` (patches the Supabase
insert + webhook; asserts every branch above).

## Run locally
```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
export GEMINI_API_KEY=...        # from .mcp.json (gemini server env)
./.venv/bin/uvicorn app:app --port 8000
curl -s localhost:8000/health
curl -s -X POST localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"sessionId":"t","chatInput":"What services do you offer?"}'
```

## Deploy (Coolify on the VPS) — NOT done yet, awaiting go
1. New Coolify app (Dockerfile build pack) from this folder/repo on the QBS server.
2. Env vars: `GEMINI_API_KEY` (required), `GEMINI_MODEL=gemini-2.5-flash`,
   `ALLOWED_ORIGINS=https://qbsglobal.ae,https://www.qbsglobal.ae`.
   For lead persistence: `SUPABASE_REST_URL=https://outreach-api.qbsglobal.net/rest/v1/contact_submissions`
   + `SUPABASE_ANON_KEY=<outreach anon key>` (same key nginx uses for /api/contact-submit).
   Optional: `ALERT_WEBHOOK=<Discord/Slack webhook>` for instant lead pings;
   `MAX_INPUT_CHARS=1000`, `RATE_LIMIT_PER_MIN=20` (guards).
3. Give it an internal/Coolify domain (e.g. `chat.qbsglobal.net`), port 8000.
4. Health check: `GET /health` (also reports `lead_persist` / `alerts` flags).
5. Needs the `contact_submissions` table (see `../QbsGlobalWebsite/supabase/contact_submissions.sql`).

## Wire the website to it (same-origin, key stays server-side)
In **QbsGlobalWebsite/nginx.conf** add (mirrors `/api/contact-submit`):
```nginx
location = /api/chat {
    limit_except POST OPTIONS { deny all; }
    resolver 127.0.0.11 1.1.1.1 valid=300s ipv6=off;
    set $chat_host "chat.qbsglobal.net";          # the deployed chat domain
    proxy_pass https://$chat_host/chat;
    proxy_ssl_server_name on;
    proxy_set_header Host $chat_host;
    proxy_set_header Content-Type "application/json";
}
```
`widget.js` already points at `/api/chat` (same-origin) — no key on the page, no CORS.

## Hardening (built 2026-06)
- **Lead persistence:** when a visitor shares an email, the lead (with the conversation
  transcript) is inserted into the `contact_submissions` table once per session — same
  place the contact form lands. Falls back to a stdout log if `SUPABASE_REST_URL` is unset.
- **Lead qualification:** on buying intent the bot asks (conversationally) for name +
  work email + need, then confirms.
- **Instant alert:** if `ALERT_WEBHOOK` is set, each captured lead pings it (Discord/Slack).
- **Rate limiting:** in-memory per-IP, `RATE_LIMIT_PER_MIN` (default 20) → 429 over the cap.
- **Input cap:** incoming message truncated to `MAX_INPUT_CHARS` (default 1000).
- **Booking attribution:** response includes `leadCaptured`; widget.js fires GA4
  `chat_lead_captured` so you can see whether the bot drives leads.
- Widget UX: `aria-live` on messages (screen readers), in-chat consent/Privacy line,
  44px send button + larger chips.

## Notes / future
- KB is `knowledge-base.csv` (copied from the site repo). Edit + redeploy to update answers.
- Retrieval is keyword-overlap (good enough for ~67 rows, $0). Swap for embeddings if it grows.
