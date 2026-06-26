"""
QBS Global — website chat backend.

The site's chat widget (widget.js) POSTs {sessionId, chatInput} here and expects
{"output": "<reply>", "leadCaptured": bool} back. We retrieve the most relevant
rows from knowledge-base.csv, hand them to Gemini as grounding context, and return
a short, on-brand answer. No fabricated prices/clients/stats — answers come from the KB.

Hardening (2026-06): rate limiting, input cap, lead persistence to the same
Supabase `contact_submissions` table the contact form uses, optional instant alert
webhook, and light in-chat lead qualification.

Same-origin in production: the site's nginx proxies /api/chat -> this service,
so the browser never sees this URL or any key (same pattern as /api/contact-submit).
"""
import os
import re
import csv
import time
import logging
from collections import defaultdict, deque
from urllib.parse import urlparse, parse_qs

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("qbs-chat")

# ── Config ──────────────────────────────────────────────────────────────────
KB_PATH = os.path.join(os.path.dirname(__file__), "knowledge-base.csv")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://qbsglobal.ae,https://www.qbsglobal.ae,http://localhost:8137,http://127.0.0.1:8137",
).split(",")

# Lead persistence (same table the contact form writes to). Empty SUPABASE_REST_URL = persistence off.
SUPABASE_REST_URL = os.environ.get("SUPABASE_REST_URL", "")        # e.g. https://outreach-api.qbsglobal.net/rest/v1/contact_submissions
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
ALERT_WEBHOOK = os.environ.get("ALERT_WEBHOOK", "")               # optional Discord/Slack incoming webhook for instant lead alerts

MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "1000"))
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "20"))  # per client IP

SYSTEM = (
    "You are the QBS Global AI assistant on the qbsglobal.ae website. "
    "QBS Global FZCO is a Dubai-based agency (IFZA Free Zone, Trade Licence 52838) "
    "with four service lines: AI & workflow automation / software development, "
    "social media management, project management, and HR consultancy. It also offers "
    "an Employer of Record (EOR) + offshore staff-augmentation service: hiring dedicated "
    "full-time talent abroad with full compliance and a managed office, no foreign entity. "
    "RULES: Answer ONLY using the CONTEXT provided. If the answer is not in the "
    "context, say you are not certain and offer to connect them with the team. "
    "NEVER state specific prices, fees, rates, percentages, discounts, or currency "
    "amounts — even if they appear in the context. For ANY cost, pricing, budget, or "
    "discount question, reply that pricing is tailored to each project's scope and "
    "invite them to book a free call for a quote. "
    "Never invent client names, statistics, or guarantees. "
    "Keep replies short (2-4 sentences), warm and professional, plain English. "
    "QUALIFY & BOOK A CALL: when the visitor shows buying intent or asks to book a call or "
    "talk to someone, run a short booking flow — ask, ONE question at a time and "
    "conversationally (never as a form, never all at once): (1) their name, (2) their work "
    "email, (3) what they need in one line, and (4) a preferred day and time, with their "
    "timezone, for a free 30-minute call. As soon as you have at least their email, confirm "
    "clearly, e.g. 'Thanks [name] — I've passed your request to the team and we'll email you "
    "at [email] to confirm your call' (mention their preferred time if they gave one). Make it "
    "clear the team will confirm the exact slot. They can also message WhatsApp "
    "(+971 56 181 4519) or email sales@qbsglobal.ae directly."
)

# ── Knowledge base ──────────────────────────────────────────────────────────
def load_kb(path):
    rows = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append({k: (v or "").strip() for k, v in r.items()})
    except FileNotFoundError:
        log.error("KB file not found at %s", path)
    return rows


KB = load_kb(KB_PATH)
log.info("Loaded %d knowledge-base rows", len(KB))

_WORD = re.compile(r"[a-z0-9]+")


def tokenize(s):
    return set(_WORD.findall((s or "").lower()))


def retrieve(query, k=6):
    """Keyword-overlap retrieval; keyword-column hits weighted higher."""
    q = tokenize(query)
    if not q:
        return KB[:k]
    scored = []
    for row in KB:
        kw = tokenize(row.get("keywords", "")) | tokenize(row.get("title", ""))
        body = tokenize(row.get("content", ""))
        score = 2 * len(q & kw) + len(q & body)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:k]]


# ── Gemini ──────────────────────────────────────────────────────────────────
async def ask_gemini(context, history, user_text):
    convo = "\n".join(f"{role}: {t}" for role, t in history)
    prompt = (
        f"CONTEXT (knowledge base — answer only from this):\n{context}\n\n"
        f"Conversation so far:\n{convo or '(none)'}\n\n"
        f"Visitor: {user_text}\nAssistant:"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 320},
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{GEMINI_URL}?key={GEMINI_API_KEY}", json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"gemini http {r.status_code}")  # never log the key-bearing URL
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


# ── Lead persistence + alerts ────────────────────────────────────────────────
NAME_RE = re.compile(r"(?:my name is|i am|i'm|this is|it's)\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)")


def guess_name(history_text):
    m = NAME_RE.search(history_text or "")
    return m.group(1).strip() if m else ""


def _utm_from_url(url):
    """Pull utm_source/medium/campaign + gclid from a URL's query string (lead attribution)."""
    out = {}
    try:
        q = parse_qs(urlparse(url or "").query)
        for k in ("utm_source", "utm_medium", "utm_campaign", "gclid"):
            if q.get(k):
                out[k] = q[k][0][:200]
    except Exception:
        pass
    return out


async def persist_lead(email, name, convo_text, source_url, user_agent):
    """Insert a chat lead into the same Supabase contact_submissions table the form uses."""
    if not (SUPABASE_REST_URL and SUPABASE_ANON_KEY):
        log.info("LEAD (not persisted — Supabase env not set): %s", email)
        return
    first = (name or "Chat visitor").split(" ")[0]
    last = " ".join((name or "").split(" ")[1:]) or None
    payload = {
        "first_name": first,
        "last_name": last,
        "email": email,
        "interest": "chat",
        "lead_type": "chatbot",
        "message": ("[Captured by the website chatbot]\n" + convo_text)[:4000],
        "source_url": source_url,
        "user_agent": user_agent,
    }
    payload.update(_utm_from_url(source_url))
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(SUPABASE_REST_URL, json=payload, headers=headers)
        if r.status_code in (200, 201, 204):
            log.info("LEAD persisted: %s", email)
        else:
            log.warning("LEAD persist failed http %s", r.status_code)
    except Exception as e:
        log.warning("LEAD persist error: %s", type(e).__name__)


async def _post_webhook(msg):
    """Fire-and-forget POST of a plain message to the Discord/Slack incoming webhook."""
    if not ALERT_WEBHOOK:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(ALERT_WEBHOOK, json={"content": msg, "text": msg})
    except Exception as e:
        log.warning("Alert error: %s", type(e).__name__)


async def send_alert(email, name, last_msg):
    await _post_webhook(
        f"🟢 New website chat lead: {name or 'Chat visitor'} <{email}> — \"{last_msg[:160]}\""
    )


# ── Rate limiting (in-memory per-IP sliding window) ───────────────────────────
_HITS = defaultdict(lambda: deque())


def rate_limited(ip):
    now = time.time()
    q = _HITS[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        return True
    q.append(now)
    return False


# ── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="QBS Global Chat")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)

SESSIONS = defaultdict(lambda: deque(maxlen=8))  # sessionId -> recent (role, text)
LEAD_SENT = set()  # sessionIds we've already persisted a lead for (de-dupe)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def client_ip(request):
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "anon")


@app.get("/health")
def health():
    return {
        "ok": True, "kb_rows": len(KB), "model": MODEL, "key_set": bool(GEMINI_API_KEY),
        "lead_persist": bool(SUPABASE_REST_URL and SUPABASE_ANON_KEY), "alerts": bool(ALERT_WEBHOOK),
    }


@app.post("/chat")
async def chat(request: Request):
    if rate_limited(client_ip(request)):
        return JSONResponse(
            {"output": "You're sending messages a little fast — give me a moment, then try again. For anything urgent, email sales@qbsglobal.ae."},
            status_code=429,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}
    text = (body.get("chatInput") or body.get("message") or "").strip()[:MAX_INPUT_CHARS]
    sid = str(body.get("sessionId") or "anon")[:64]
    source_url = str(body.get("source_url") or request.headers.get("referer") or "")[:300]
    user_agent = request.headers.get("user-agent", "")[:300]

    if not text:
        return JSONResponse({"output": "Ask me anything about QBS Global — our services, how we work, or getting started."})

    rows = retrieve(text)
    context = "\n\n".join(f"[{r.get('category')} — {r.get('title')}]\n{r.get('content')}" for r in rows) or "(no specific match found)"

    try:
        reply = await ask_gemini(context, list(SESSIONS[sid]), text)
        if not reply:
            raise ValueError("empty reply")
    except Exception as e:
        log.warning("Gemini error: %s", e)
        return JSONResponse(
            {"output": "I'm having a brief connection issue. Please try again, or reach us directly at sales@qbsglobal.ae or on WhatsApp +971 56 181 4519."}
        )

    SESSIONS[sid].append(("Visitor", text))
    SESSIONS[sid].append(("Assistant", reply))

    # Lead capture: first email shared in the conversation → persist once, with the transcript for context.
    lead_captured = False
    emails = EMAIL_RE.findall(text)
    if emails and sid not in LEAD_SENT:
        LEAD_SENT.add(sid)
        lead_captured = True
        convo_text = "\n".join(f"{role}: {t}" for role, t in SESSIONS[sid])
        name = guess_name(convo_text)
        await persist_lead(emails[0], name, convo_text, source_url, user_agent)
        await send_alert(emails[0], name, text)

    return JSONResponse({"output": reply, "leadCaptured": lead_captured})


# ── Contact form ingest + notify ──────────────────────────────────────────────
# The website's nginx proxies /api/contact-submit -> here (was proxying straight
# to PostgREST, which saved the lead but notified NOBODY and left a failed save
# with no trace — a real lead was lost that way on 2026-06-25). This thin handler:
#   (a) inserts the lead into the same Supabase contact_submissions table,
#   (b) pings the Discord webhook on success,
#   (c) dead-letters (logs + alerts) on failure so a lost save is never silent.
# It preserves the browser contract: 2xx => the form shows success; non-2xx =>
# the form's retry/error path engages.
_ALLOWED_LEAD_FIELDS = (
    "first_name", "last_name", "email", "interest", "message", "lead_type",
    "source_url", "user_agent", "utm_source", "utm_medium", "utm_campaign", "gclid",
)


async def _insert_contact(payload):
    """Insert one row into Supabase contact_submissions. Returns (ok, status)."""
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(SUPABASE_REST_URL, json=payload, headers=headers)
    return (r.status_code in (200, 201, 204), r.status_code)


@app.post("/contact-submit")
async def contact_submit(request: Request):
    if not (SUPABASE_REST_URL and SUPABASE_ANON_KEY):
        log.error("CONTACT-SUBMIT misconfigured: Supabase env not set")
        return JSONResponse({"error": "lead store not configured"}, status_code=500)

    if rate_limited(client_ip(request)):
        return JSONResponse({"error": "rate_limited"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid payload"}, status_code=400)

    # Whitelist + cap fields (never trust the client to set arbitrary columns).
    payload = {}
    for k in _ALLOWED_LEAD_FIELDS:
        v = body.get(k)
        if v is None or v == "":
            continue
        payload[k] = str(v)[:4000]

    email = payload.get("email", "")
    if not EMAIL_RE.fullmatch(email or ""):
        return JSONResponse({"error": "valid email required"}, status_code=400)

    name = (payload.get("first_name", "") + " " + payload.get("last_name", "")).strip()
    last_msg = payload.get("message", "")

    try:
        ok, status = await _insert_contact(payload)
    except Exception as e:
        # Transport failure → dead-letter so the lead is never silently lost.
        log.error("CONTACT-SUBMIT insert error: %s — lead=%s", type(e).__name__, email)
        await _post_webhook(
            f"🔴 DEAD-LETTER website lead (save failed: {type(e).__name__}) — "
            f"{name or 'Unknown'} <{email}> — \"{last_msg[:160]}\" — capture manually."
        )
        return JSONResponse({"error": "save failed"}, status_code=502)

    if not ok:
        # PostgREST refused (RLS / schema / 4xx-5xx) → dead-letter with the body for manual capture.
        log.error("CONTACT-SUBMIT insert http %s — lead=%s", status, email)
        await _post_webhook(
            f"🔴 DEAD-LETTER website lead (save http {status}) — "
            f"{name or 'Unknown'} <{email}> — \"{last_msg[:160]}\" — capture manually."
        )
        return JSONResponse({"error": "save failed"}, status_code=502)

    # Saved — notify so a lead never pings nobody again.
    src = payload.get("lead_type", "form")
    await _post_webhook(
        f"🟢 New website lead ({src}): {name or 'Unknown'} <{email}> — \"{last_msg[:160]}\""
    )
    log.info("CONTACT-SUBMIT saved + alerted: %s", email)
    # 204 mirrors the old PostgREST return=minimal response the browser expected.
    return Response(status_code=204)
