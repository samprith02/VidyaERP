"""Deployment readiness. No server, no LLM, no API cost.

    python tests/deploy_test.py

Calls the route functions directly rather than over HTTP, so it needs neither a
running server nor an HTTP client library — there is no `httpx` here for the
same reason there is no LLM SDK.

--------------------------------------------------------------------------
What this holds down
--------------------------------------------------------------------------
Everything here is a claim the README or render.yaml makes about the deployed
instance. A deployment claim that nothing checks is the kind that stays true
right up until the demo.

  * the key never appears in any response, including error paths;
  * a real environment variable beats .env, or a platform cannot configure this
    app at all (it did not, until it was measured — see 2a);
  * /health answers from the database, so a broken instance fails its health
    check instead of staying in rotation;
  * the two operator endpoints refuse an unauthenticated caller once a token is
    configured.
"""
import importlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db                                                          # noqa: E402

_REAL = db.DB_PATH
_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_deploy_test.db")
db.seed()
shutil.copy(_REAL, _TMP)
db.DB_PATH = _TMP

import llm                                                         # noqa: E402

FAILED = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTINEL = "gsk_TOTALLY_FAKE_KEY_FOR_THIS_TEST_0123456789"


class FakeRequest:
    """Enough of a Request for the header check the operator endpoints make."""
    def __init__(self, headers=None):
        self.headers = headers or {}


def body_of(resp):
    return resp.body.decode() if hasattr(resp, "body") else json.dumps(resp, default=str)


# ============================================== 1 · the key is never exposed
print("\n1 · the API key never leaves the process")
print("-" * 78)

os.environ["LLM_API_KEY"] = SENTINEL
cfg = llm.CFG.reload()
check("1a the key is actually loaded (so the rest of this section means something)",
      cfg.api_key == SENTINEL,
      # Never print the key, not even a prefix, not even on failure — this
      # suite exists to keep key material out of output.
      f"a different key is in effect (len {len(cfg.api_key)}); is a stale .env shadowing it?")

desc = json.dumps(cfg.describe(), default=str)
check("1b /api/mode reports key_set, never the key", SENTINEL not in desc, desc[:160])
check("1c /api/mode does report WHETHER a key is set", cfg.describe()["key_set"] is True)

# The transport builds an Authorization header; an error must not echo it back.
err = llm.ping(cfg)                       # unreachable host -> the error path
check("1d a failed provider call does not leak the key",
      SENTINEL not in json.dumps(err, default=str), json.dumps(err)[:160])

os.environ.pop("LLM_API_KEY", None)
llm.CFG.reload()

# Nothing tracked in git may carry key material. .env is gitignored; if that
# ever regressed, this is what would notice.
tracked = os.popen(f'git -C "{HERE}" ls-files').read().split()
check("1e .env is not tracked by git", ".env" not in tracked)
check("1f no database file is tracked", not [t for t in tracked if t.endswith(".db")],
      str([t for t in tracked if t.endswith(".db")]))
check("1g .env.example ships an EMPTY key",
      any(l.strip() == "LLM_API_KEY=" for l in
          open(os.path.join(HERE, ".env.example"), encoding="utf-8")))


# ==================================== 2 · a platform can configure this app
print("\n2 · environment variables beat .env")
print("-" * 78)
# DEFECT THIS ENCODES: reload() read `.env` FIRST and the environment second, so
# a value in a stale .env inside an image would silently shadow the platform's
# dashboard, with nothing in the UI to show why. Measured before the fix: with
# LLM_MODEL set in the shell AND in .env, .env won.
os.environ["LLM_MODEL"] = "env-var-must-win"
check("2a a real environment variable overrides .env",
      llm.CFG.reload().model == "env-var-must-win", llm.CFG.model)
os.environ.pop("LLM_MODEL", None)
llm.CFG.reload()

check("2b the database path is overridable for a mounted disk",
      "VIDYAERP_DB" in open(os.path.join(HERE, "db.py"), encoding="utf-8").read())

# DEFECT THIS ENCODES: .env used to be read by llm.py, but db.py computes
# DB_PATH at import time and app.py imports db FIRST — and mcp_server.py never
# imported llm at all. So VIDYAERP_DB in .env was silently ignored by both
# entry points. Had one honoured it and the other not, the console and the MCP
# server would have opened different databases, and an approval code minted in
# one would have been invisible to the other.
check("2i the .env reader is in db.py, below everything that reads a setting",
      "def load_env" in open(os.path.join(HERE, "db.py"), encoding="utf-8").read()
      and "def load_env" not in open(os.path.join(HERE, "llm.py"), encoding="utf-8").read(),
      "load_env must live in db.py or DB_PATH is computed before .env is read")
check("2j every entry point resolves the same database from the same .env",
      __import__("mcp_server").db.DB_PATH == db.DB_PATH == __import__("app").db.DB_PATH)

# Every variable render.yaml sets must be one the code actually reads, and every
# variable the code reads should be documented in .env.example. A blueprint that
# sets a name nothing consumes is a silent no-op.
blueprint = open(os.path.join(HERE, "render.yaml"), encoding="utf-8").read()
example = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
src = "".join(open(os.path.join(HERE, f), encoding="utf-8").read()
              for f in ("app.py", "db.py", "llm.py"))
declared = [l.split("key:")[1].strip() for l in blueprint.splitlines()
            if l.strip().startswith("- key:")]
unread = [k for k in declared if k not in src and not k.startswith("PYTHON")]
check("2c every variable render.yaml sets is one the code reads", not unread, str(unread))
undocumented = [k for k in declared
                if k not in example and not k.startswith("PYTHON")]
check("2d every variable render.yaml sets is documented in .env.example",
      not undocumented, str(undocumented))
# A live API check does not belong in a no-network suite, so this cannot tell
# you a model id is still SERVED — only that the chain is structurally sound.
# That is still worth one guard: llama-3.3-70b-versatile sat at the end of this
# chain after Groq retired it, so under the rate-limit pressure the chain exists
# to absorb, the last hop 404'd and the whole thing exhausted into the rule
# engine. A blank or duplicated entry fails the same way and is checkable here.
def _setting(text, key):
    """Pull a value out of either .env syntax or render.yaml's envVars block."""
    lines = [l.rstrip() for l in text.splitlines()]
    for i, l in enumerate(lines):
        s = l.strip()
        if s.startswith(f"{key}="):
            return s.split("=", 1)[1].strip()
        if s == f"- key: {key}" and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt.startswith("value:"):
                return nxt.split("value:", 1)[1].strip().strip('"').strip("'")
    return None


for _src, _text in (("render.yaml", blueprint), (".env.example", example)):
    primary = _setting(_text, "LLM_MODEL")
    raw = _setting(_text, "LLM_FALLBACK_MODELS") or ""
    chain = [m.strip() for m in raw.split(",")]
    check(f"2k {_src}: the fallback chain is declared", bool(raw), repr(raw))
    check(f"2l {_src}: no fallback id is blank", all(chain) and chain == [m for m in chain if m],
          f"blank entry in {chain!r} — a stray comma silently shortens the chain")
    check(f"2m {_src}: no fallback repeats the primary model",
          primary not in chain,
          f"{primary!r} is already the primary; chat_resilient skips it, so the "
          f"chain is one hop shorter than it reads")
    check(f"2n {_src}: fallback ids are distinct from each other",
          len(chain) == len(set(chain)), f"duplicate in {chain!r}")

check("2o both files declare the SAME fallback chain",
      _setting(blueprint, "LLM_FALLBACK_MODELS") == _setting(example, "LLM_FALLBACK_MODELS"),
      f"render.yaml={_setting(blueprint, 'LLM_FALLBACK_MODELS')!r} "
      f".env.example={_setting(example, 'LLM_FALLBACK_MODELS')!r}")

# Ids known to be unusable HERE. Checked against the declared values only, not
# the file text: both files carry a comment explaining why llama-3.3 was
# removed, and that comment should survive exactly so nobody puts it back.
RETIRED = {"llama-3.3-70b-versatile"}          # retired by Groq, no longer served
NO_TOOL_CALLING = {"groq/compound-mini"}       # HTTP 400 "tool calling is not supported"
for _src, _text in (("render.yaml", blueprint), (".env.example", example)):
    declared_models = {(_setting(_text, "LLM_MODEL") or "").strip(),
                       *[m.strip() for m in (_setting(_text, "LLM_FALLBACK_MODELS") or "").split(",")]}
    check(f"2p {_src}: no retired model id is shipped",
          not declared_models & RETIRED, str(declared_models & RETIRED))
    check(f"2q {_src}: no chat-only model is in the chain",
          not declared_models & NO_TOOL_CALLING,
          f"{declared_models & NO_TOOL_CALLING} cannot serve a tool call, and the "
          f"agent loop is nothing but tool calls")

check("2e render.yaml never hardcodes a key",
      "sync: false" in blueprint and SENTINEL not in blueprint
      and "gsk_" not in blueprint)
check("2f render.yaml points its health check at /health",
      "healthCheckPath: /health" in blueprint)
check("2g render.yaml pins ONE worker (shared SQLite connection + in-process sessions)",
      "--workers 1" in blueprint)
check("2h render.yaml binds the platform port, not a fixed one",
      "--host 0.0.0.0" in blueprint and "$PORT" in blueprint)


# ================================================= 3 · /health tells the truth
print("\n3 · /health answers from the database")
print("-" * 78)

import app as A                                                    # noqa: E402

h = A.health()
payload = json.loads(body_of(h))
check("3a a healthy instance returns 200", h.status_code == 200, str(h.status_code))
check("3b it reports real row counts, not a hardcoded ok",
      payload["db"]["students"] > 0 and payload["db"]["faculty"] > 0
      and payload["db"]["timetable"] > 0, str(payload["db"]))
check("3c it names the engine actually in use",
      payload["engine"] in ("llm", "rule-engine"), payload["engine"])
check("3d it says whether storage survives a restart", "persistent" in payload["db"])
check("3e it says whether the operator endpoints are protected",
      payload["operator_endpoints"] in ("open", "token-protected"))
check("3f no key material in /health", "gsk_" not in body_of(h) and SENTINEL not in body_of(h))

# The point of checking the DB: a broken data layer must FAIL the health check,
# not pass it and keep a dead instance serving traffic.
_real_con = A.con


class BrokenCon:
    def execute(self, *a, **k):
        raise RuntimeError("database is locked")


A.con = BrokenCon()
broken = A.health()
A.con = _real_con
check("3g an unreachable database returns 503, not 200",
      broken.status_code == 503, str(broken.status_code))
check("3h the 503 says what failed",
      "database is locked" in body_of(broken), body_of(broken)[:160])
check("3i /health recovers once the database does", A.health().status_code == 200)


# =========================================== 4 · operator endpoints are gated
print("\n4 · the two operator endpoints refuse strangers when a token is set")
print("-" * 78)

check("4a with no token configured they stay open (laptop default)",
      A.ADMIN_TOKEN == "" and A._denied(FakeRequest()) is None)

A.ADMIN_TOKEN = "s3cret-operator-token"
try:
    check("4b an unauthenticated caller is refused",
          A._denied(FakeRequest()).status_code == 401)
    check("4c a wrong token is refused",
          A._denied(FakeRequest({"X-Admin-Token": "guess"})).status_code == 401)
    check("4d the right token is admitted",
          A._denied(FakeRequest({"X-Admin-Token": "s3cret-operator-token"})) is None)
    check("4e the refusal does not echo the token back",
          "s3cret-operator-token" not in body_of(A._denied(FakeRequest())))

    # The one that matters: minting a write approval is the human half of the
    # MCP gate. An open URL would hand that to whoever found it.
    minted = A.mcp_approval(FakeRequest(), {})
    check("4f /api/mcp/approval refuses an unauthenticated caller",
          getattr(minted, "status_code", None) == 401, str(minted)[:120])
    check("4g no approval code is issued to a refused caller",
          "code" not in body_of(minted), body_of(minted)[:120])

    reset = A.reseed(FakeRequest())
    check("4h /api/seed/reset refuses an unauthenticated caller",
          getattr(reset, "status_code", None) == 401, str(reset)[:120])

    ok = A.mcp_approval(FakeRequest({"X-Admin-Token": "s3cret-operator-token"}), {})
    check("4i an authorised operator still gets a code",
          isinstance(ok, dict) and len(ok.get("code", "")) == 6, str(ok)[:120])
finally:
    A.ADMIN_TOKEN = ""

check("4j /api/chat is NOT gated — the console has no login, and the docs say so",
      "no authentication" in open(os.path.join(HERE, "app.py"), encoding="utf-8").read().lower()
      or "NO LOGIN" in open(os.path.join(HERE, "app.py"), encoding="utf-8").read())


# ================================================== 5 · it boots from nothing
print("\n5 · a cold start needs no configuration")
print("-" * 78)

cold = os.path.join(tempfile.gettempdir(), "vidyaerp_coldstart.db")
if os.path.exists(cold):
    os.remove(cold)
_keep = db.DB_PATH
db.DB_PATH = cold
import time                                                        # noqa: E402
t0 = time.time()
db.seed()
elapsed = time.time() - t0
problems = db.verify()                      # while DB_PATH still points at the cold copy
db.DB_PATH = _keep
check("5a an empty instance seeds itself", os.path.exists(cold))
check("5b seeding is fast enough not to trip a health check", elapsed < 10,
      f"{elapsed:.2f}s")
check("5c a freshly seeded instance passes db.verify()", not problems, str(problems[:3]))
check("5d requirements.txt is still two packages",
      len([l for l in open(os.path.join(HERE, "requirements.txt"), encoding="utf-8")
           if l.strip() and not l.startswith("#")]) == 2,
      "a third dependency landed — CLAUDE.md says think hard first")
os.remove(cold)


print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
sys.exit(1 if FAILED else 0)
