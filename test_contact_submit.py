import os, sys
# Configure env BEFORE importing app so SUPABASE_* are set (handler checks them).
os.environ["SUPABASE_REST_URL"] = "https://example.invalid/rest/v1/contact_submissions"
os.environ["SUPABASE_ANON_KEY"] = "test-anon-key"
os.environ["ALERT_WEBHOOK"] = "https://discord.invalid/webhook"
os.environ["GEMINI_API_KEY"] = "x"
os.environ["RATE_LIMIT_PER_MIN"] = "1000"  # don't trip rate limit during tests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as appmod
from fastapi.testclient import TestClient

client = TestClient(appmod.app)

# Capture alerts + control insert outcome via monkeypatch
alerts = []
async def fake_post_webhook(msg):
    alerts.append(msg)
appmod._post_webhook = fake_post_webhook

INSERT_MODE = {"v": ("ok", 201)}  # ("ok",201) | ("fail",403) | ("raise",None)
async def fake_insert(payload):
    mode = INSERT_MODE["v"]
    if mode[0] == "raise":
        raise RuntimeError("connect timeout")
    if mode[0] == "ok":
        return (True, mode[1])
    return (False, mode[1])  # fail
appmod._insert_contact = fake_insert

GOOD = {"first_name":"Jane","last_name":"Doe","email":"jane@acme.com",
        "message":"Phone/WhatsApp: +971501234567","lead_type":"form",
        "source_url":"https://qbsglobal.ae/get-started.html","utm_source":"google","gclid":"abc"}

def run(label, mode, payload, expect_status, expect_alert_contains):
    alerts.clear()
    INSERT_MODE["v"] = mode
    r = client.post("/contact-submit", json=payload)
    a = alerts[0] if alerts else ""
    ok = (r.status_code == expect_status) and (expect_alert_contains in a if expect_alert_contains is not None else True)
    print(f"[{label}] status={r.status_code} (want {expect_status}) | alert={a[:70]!r}")
    assert r.status_code == expect_status, f"{label}: status {r.status_code} != {expect_status}"
    if expect_alert_contains is not None:
        assert expect_alert_contains in a, f"{label}: alert missing {expect_alert_contains!r}"
    if expect_alert_contains is None:
        assert not alerts, f"{label}: unexpected alert {a!r}"

# 1) happy path → 204 + green alert
run("save OK", ("ok",201), GOOD, 204, "🟢 New website lead (form): Jane Doe <jane@acme.com>")
# 2) postgrest refuses (403) → 502 + dead-letter
run("save http 403 → dead-letter", ("fail",403), GOOD, 502, "🔴 DEAD-LETTER")
# 3) transport raise → 502 + dead-letter
run("transport error → dead-letter", ("raise",None), GOOD, 502, "🔴 DEAD-LETTER")
# 4) bad email → 400, no insert attempt, no alert
run("bad email → 400", ("ok",201), {**GOOD,"email":"nope"}, 400, None)
# 5) missing email → 400
run("missing email → 400", ("ok",201), {k:v for k,v in GOOD.items() if k!="email"}, 400, None)

# 6) bad json body → 400
alerts.clear(); INSERT_MODE["v"]=("ok",201)
r = client.post("/contact-submit", data="not-json", headers={"Content-Type":"application/json"})
print(f"[bad json] status={r.status_code} (want 400)")
assert r.status_code == 400

# 7) field whitelist: arbitrary column rejected from payload
captured={}
async def capture_insert(payload):
    captured.update(payload); return (True,201)
appmod._insert_contact = capture_insert
INSERT_MODE["v"]=("ok",201)
client.post("/contact-submit", json={**GOOD, "is_admin":"true", "id":"999"})
print(f"[whitelist] inserted keys = {sorted(captured.keys())}")
assert "is_admin" not in captured and "id" not in captured, "arbitrary columns must be stripped"
assert "email" in captured and "utm_source" in captured and captured["lead_type"]=="form"

# 8) misconfigured env → 500
appmod.SUPABASE_REST_URL = ""
r = client.post("/contact-submit", json=GOOD)
print(f"[misconfigured env] status={r.status_code} (want 500)")
assert r.status_code == 500
appmod.SUPABASE_REST_URL = "https://example.invalid/rest/v1/contact_submissions"

print("\nALL CONTACT-SUBMIT TESTS PASSED")
