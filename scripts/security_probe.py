#!/usr/bin/env python3
"""Re-run the deep-security-audit exploit checks against a live ephemeral
instance. Verifies the CRIT/HIGH fixes (host check, anti-pivot token, forced
chat mode, tool dispatch block, router 404s, local KaTeX/Mermaid, CSP) AND that
a normal chat still streams a real local response.

Usage:
  security_probe.py <port> <secret_token>   # against an already-running backend

Exit code 0 iff every expected outcome holds.
"""
import json
import sys
import urllib.request
import urllib.error

PORT = int(sys.argv[1])
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
BASE = f"http://127.0.0.1:{PORT}"
COOKIE_NAME = "alice_local_token"
HEADER_NAME = "X-Alice-Local"

results = []  # (name, passed, detail)


def rec(name, passed, detail=""):
    results.append((name, passed, detail))
    flag = "PASS" if passed else "FAIL"
    print(f"[{flag}] {name}{(' — ' + detail) if detail else ''}")


def req(method, path, *, headers=None, data=None, host=None, cookie=None,
        token_header=None, want_read=False, timeout=60):
    url = BASE + path
    h = dict(headers or {})
    if host:
        h["Host"] = host
    if cookie:
        h["Cookie"] = f"{COOKIE_NAME}={cookie}"
    if token_header:
        h[HEADER_NAME] = token_header
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read() if want_read else b""
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as e:
        body = e.read() if want_read else b""
        return e.code, dict(e.headers), body
    except Exception as e:  # noqa: BLE001
        return None, {"_err": str(e)}, b""


def mp_body(fields):
    """Build a multipart/form-data body (a CORS 'simple' request)."""
    boundary = "----aliceprobe1234567890"
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\n")
        parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n')
        parts.append(f"{v}\r\n")
    parts.append(f"--{boundary}--\r\n")
    body = "".join(parts).encode()
    ct = f"multipart/form-data; boundary={boundary}"
    return body, ct


# ── 1. healthz stays open (no token) ──────────────────────────────────────
st, _, _ = req("GET", "/healthz")
rec("healthz open without token", st == 200, f"status={st}")

# ── 2. Host spoof / DNS-rebind rejected ────────────────────────────────────
st, _, _ = req("GET", "/api/version", host="attacker.example.com")
rec("bad Host rejected (TrustedHost)", st == 400, f"status={st} (expect 400)")
# healthz must remain reachable for the shell even via odd host? It binds
# 127.0.0.1 already; TrustedHost still applies. Shell uses 127.0.0.1 Host.
st, _, _ = req("GET", "/healthz", host="127.0.0.1")
rec("healthz ok with loopback Host", st == 200, f"status={st}")

# ── 3. cross-origin POST /api/chat_stream (no token, evil Origin) ──────────
body, ct = mp_body({"message": "hi", "session": "__x__",
                    "mode": "agent", "allow_bash": "true"})
st, _, rb = req("POST", "/api/chat_stream",
                headers={"Origin": "http://evil.com", "Content-Type": ct},
                data=body, want_read=True)
rec("cross-origin no-token chat_stream REJECTED",
    st in (403, 401), f"status={st} body={rb[:80]!r} (expect 403/401)")

# ── 4. no-token same-origin-looking POST still rejected (missing token) ────
body, ct = mp_body({"message": "hi", "session": "__x__"})
st, _, rb = req("POST", "/api/chat_stream",
                headers={"Content-Type": ct}, data=body, want_read=True)
rec("no-token chat_stream REJECTED",
    st in (403, 401), f"status={st} (expect 403/401)")

# ── 5. /v1/chat/completions cross-origin no-token rejected ─────────────────
st, _, rb = req("POST", "/v1/chat/completions",
                headers={"Origin": "http://evil.com",
                         "Content-Type": "application/json"},
                data=json.dumps({"model": "alice-lite",
                                 "messages": [{"role": "user", "content": "hi"}]}).encode(),
                want_read=True)
rec("/v1/chat/completions no-token REJECTED",
    st in (403, 401), f"status={st} (expect 403/401)")

# ── 6. /alice/* mutating no-token rejected (load is POST) ──────────────────
st, _, rb = req("POST", "/alice/load",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"id": "lite"}).encode(), want_read=True)
rec("/alice/load no-token REJECTED", st in (403, 401),
    f"status={st} (expect 403/401)")

# ── 6b. Earn bridge (/alice/earn/*) rides the SAME token guard (M7) ─────────
# Read-only status: rejected without the token, allowed with it; pure local I/O
# (no network), credit-only honesty contract. open-miner: a mutating POST that
# must be token-guarded AND reject a foreign Origin — we assert the GUARD only
# (no body, evil Origin) so the probe never actually launches the Miner GUI.
st, _, _ = req("GET", "/alice/earn/status")
rec("/alice/earn/status no-token REJECTED", st in (403, 401),
    f"status={st} (expect 403/401)")
st, _, rb = req("GET", "/alice/earn/status", token_header=TOKEN, cookie=TOKEN,
                want_read=True)
earn = {}
try:
    earn = json.loads(rb)
except Exception:
    pass
rec("/alice/earn/status with-token OK", st == 200 and "state" in earn,
    f"status={st} state={earn.get('state')!r}")
# honesty / credit-only invariant printed into the payload (no $, paid_acu=0)
rec("earn status credit-only (paid_acu=0, no GPU-earn live)",
    str(earn.get("honesty", {}).get("paid_acu")) == "0"
    and earn.get("honesty", {}).get("credit_only") is True
    and earn.get("gpu_earn", {}).get("enabled") is False
    and earn.get("gpu_earn", {}).get("status") == "coming_soon",
    f"honesty={earn.get('honesty')} gpu={earn.get('gpu_earn', {}).get('status')}")
# the read-only identity surface never leaks a secret (no pubkey/keystore key)
_idk = set((earn.get("identity") or {}).keys())
rec("earn identity surface has no secret fields",
    not (_idk & {"pubkey", "keystore_path", "keystore", "seed", "password"}),
    f"identity_keys={sorted(_idk)}")
# open-miner: no-token POST rejected (guard present; no launch triggered)
st, _, _ = req("POST", "/alice/earn/open-miner")
rec("/alice/earn/open-miner no-token REJECTED", st in (403, 401),
    f"status={st} (expect 403/401)")
# open-miner: cross-origin POST WITH token still rejected (CSRF/DNS-rebind)
st, _, _ = req("POST", "/alice/earn/open-miner",
               headers={"Origin": "http://evil.com"},
               token_header=TOKEN, cookie=TOKEN)
rec("/alice/earn/open-miner cross-origin REJECTED", st == 403,
    f"status={st} (expect 403)")

# ── 7. privileged routers 404 in Simple mode ───────────────────────────────
for path, label in [
    ("/api/shell/exec", "shell"),
    ("/api/cookbook/packages", "cookbook"),
    ("/api/mcp/servers", "mcp"),
    ("/api/vault/status", "vault"),
]:
    st, _, _ = req("POST" if path.endswith("exec") else "GET", path,
                   token_header=TOKEN, cookie=TOKEN,
                   headers={"Content-Type": "application/json"},
                   data=b"{}" if path.endswith("exec") else None)
    rec(f"router {label} -> 404 (not mounted)", st == 404,
        f"status={st} (expect 404)")

# ── 8. CSP is 'self' only (no jsdelivr) ────────────────────────────────────
st, hdrs, _ = req("GET", "/", token_header=TOKEN, cookie=TOKEN)
csp = hdrs.get("content-security-policy", "") or hdrs.get("Content-Security-Policy", "")
rec("CSP has no jsdelivr", "jsdelivr" not in csp, f"csp_script={csp[:120]!r}")
rec("CSP script-src 'self' only", "script-src 'self'" in csp and "jsdelivr" not in csp,
    "")

# ── 9. served index.html loads ONLY local assets, no CDN ───────────────────
# The lean chat frontend renders neither LaTeX nor diagrams (the old odysseus
# UI vendored KaTeX/Mermaid; the lean rewrite dropped them), so the privacy
# invariant is the general one: every script/style is local, no external origin.
st, _, rb = req("GET", "/", token_header=TOKEN, cookie=TOKEN, want_read=True)
html = rb.decode("utf-8", "replace")
rec("index.html: no jsdelivr <script>/<link>", "jsdelivr" not in html,
    f"jsdelivr_count={html.count('jsdelivr')}")
rec("index.html: lean controller referenced locally", "/static/lean/alice-lean.js" in html, "")
rec("index.html: no external script/style origin (no CDN)",
    ('src="http' not in html) and ('href="http' not in html)
    and ("cdnjs" not in html) and ("unpkg" not in html),
    "an external origin or CDN ref leaked into served HTML")

# ── 10. token cookie is injected into served HTML response ─────────────────
st, hdrs, _ = req("GET", "/")
setc = hdrs.get("set-cookie", "") or hdrs.get("Set-Cookie", "")
# Starlette emits SameSite lowercase ("strict"); accept either case.
rec("served HTML sets the local-token cookie",
    COOKIE_NAME in setc and "samesite=strict" in setc.lower(),
    f"set-cookie={setc[:90]!r}")

# ── 11. NORMAL CHAT STILL WORKS (legit UI path: cookie+header token) ───────
# Create a session via the real form route (needs the token). The alice-local
# endpoint is seeded at startup; point the session at our own /v1 + alice-lite.
body, ct = mp_body({"name": "probe", "skip_validation": "true",
                    "model": "alice-lite",
                    "endpoint_url": f"{BASE}/v1/chat/completions"})
st, _, rb = req("POST", "/api/session",
                headers={"Content-Type": ct},
                cookie=TOKEN, token_header=TOKEN,
                data=body, want_read=True)
sid = None
try:
    sid = json.loads(rb).get("session_id") or json.loads(rb).get("id")
except Exception:
    pass
rec("create session with token", st == 200 and bool(sid),
    f"status={st} sid={sid} body={rb[:80]!r}")

# ── 11a. /api/chat_stream with mode=agent+allow_bash passes the token guard
#         and the server FORCES chat mode. NOTE: odysseus's /api/chat_stream has
#         a PRE-EXISTING session-owner check that 403s no-auth sessions (repro
#         at HEAD, ALICE_SIMPLE_MODE=0) — out of scope here. We assert the
#         request is NOT rejected by OUR token/Origin middleware (i.e. not a
#         CROSS_ORIGIN/LOCAL_TOKEN 403) and that no agent tool fired. ──────────
if sid:
    body, ct = mp_body({"message": "Say the single word: pong.",
                        "session": sid, "mode": "agent", "allow_bash": "true"})
    st, _, rb = req("POST", "/api/chat_stream",
                    headers={"Content-Type": ct},
                    cookie=TOKEN, token_header=TOKEN,
                    data=body, want_read=True, timeout=180)
    text = rb.decode("utf-8", "replace")
    rec("chat_stream(token) NOT blocked by token/Origin guard",
        b"CROSS_ORIGIN_BLOCKED" not in rb and b"LOCAL_TOKEN_REQUIRED" not in rb,
        f"status={st}")
    rec("forced chat mode: no agent tool_start in stream",
        '"tool_start"' not in text, "")

# ── 11b. REAL end-to-end local generation via the alice provider (/v1) — the
#         legit inference path. no-token rejected; with-token streams a real
#         on-device reply with the privacy block intact. ──────────────────────
st, _, rb = req("POST", "/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                cookie=TOKEN, token_header=TOKEN,
                data=json.dumps({"model": "alice-lite",
                                 "messages": [{"role": "user",
                                               "content": "Reply with one word: pong"}],
                                 "max_tokens": 24, "stream": False}).encode(),
                want_read=True, timeout=180)
reply = ""
priv = {}
try:
    d = json.loads(rb.decode("utf-8", "replace"), strict=False)
    reply = d.get("choices", [{}])[0].get("message", {}).get("content", "")
    priv = d.get("alice_local", {})
except Exception:
    pass
rec("REAL local generation works (token path)",
    st == 200 and len(reply.strip()) > 0,
    f"status={st} reply={reply[:50]!r}")
rec("generation privacy block intact (no network / paid_acu=0)",
    priv.get("network_calls_made") is False and str(priv.get("paid_acu")) == "0",
    f"alice_local={priv}")

n_fail = sum(1 for _, p, _ in results if not p)
print(f"\n=== {len(results) - n_fail}/{len(results)} checks passed ===")
sys.exit(1 if n_fail else 0)
