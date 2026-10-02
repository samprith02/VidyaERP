"""Notification delivery (#26) and an honest delivery status (#83).
No server of ours, no LLM, no API cost; the "gateway" is a stub HTTP server on
the loopback interface, started and stopped by this file.

    python tests/notify_test.py

What it pins:
  1  with no gateway a notice is "Recorded", never "Sent"
  2  a webhook gets one JSON batch per dispatch, with the bearer token
  3  a gateway that fails marks the notices Failed - and the write that
     produced them stays committed and is reported as committed
  4  a write the gate blocks sends nothing to the gateway
  5  retry re-sends Queued/Failed notices, leaves Recorded ones alone
  6  the outbox file form, and the configuration checks
  7  nothing reports the gateway's URL or token
  8  an older database gains the delivery columns
"""
import http.server
import json
import os
import sqlite3
import sys
import tempfile
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

for k in ("VIDYAERP_NOTIFY_URL", "VIDYAERP_NOTIFY_TOKEN"):
    os.environ.pop(k, None)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_notify_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import notify_gateway as ng                                        # noqa: E402
import orchestrator                                                # noqa: E402
import tools                                                       # noqa: E402
from agents import NotifyAgent, one, rows                          # noqa: E402

FAILED, PASSED = [], 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


con = db.connect()
TOKEN = "tok-" + "9" * 24                      # a fake; the point is that it never leaks


# ------------------------------------------------------------ the stub gateway
class Gateway(http.server.BaseHTTPRequestHandler):
    got, answer = [], 200

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        Gateway.got.append({"auth": self.headers.get("Authorization"),
                            "type": self.headers.get("Content-Type"),
                            "body": json.loads(self.rfile.read(n) or b"{}")})
        self.send_response(Gateway.answer)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
threading.Thread(target=srv.serve_forever, daemon=True).start()
HOOK = f"http://127.0.0.1:{srv.server_address[1]}/hook?key=secretquery"


def use(url=None, token=None):
    for k, v in (("VIDYAERP_NOTIFY_URL", url), ("VIDYAERP_NOTIFY_TOKEN", token)):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def latest(n=1):
    return rows(con, f"SELECT * FROM notifications ORDER BY id DESC LIMIT {int(n)}")


def msg(title="t", body="b"):
    return {"audience": "Students · CSE-3A", "channel": "App", "title": title, "body": body}


def S():
    return {"pending": None, "history": [], "ctx": {}}


# ------------------------------------------------------------------ 1 · default
print("\n1 · no gateway: recorded, not sent")
print("-" * 78)
use()
out = NotifyAgent().dispatch(con, [msg("one"), msg("two")])
two = latest(2)
check("1a every notice is 'Recorded'", [r["status"] for r in two] == ["Recorded", "Recorded"], str(two))
check("1b nothing claims 'Sent' anywhere in the table",
      not one(con, "SELECT 1 FROM notifications WHERE status='Sent'"))
check("1c the outcome says no gateway, two recorded",
      out == {"gateway": "none", "accepted": 0, "failed": 0, "recorded": 2}, str(out))
check("1d no attempt is counted when nothing was attempted",
      all((r["attempts"] or 0) == 0 and r["delivered_at"] is None for r in two))
check("1e no gateway adds no trace step (the default is not a failure)", ng.step(out) is None)
check("1f describe() says notices do not leave the system",
      ng.describe() == {"gateway": "none", "delivers": False}, str(ng.describe()))

# ------------------------------------------------------------------ 2 · webhook
print("\n2 · a webhook gets one batch, with the token")
print("-" * 78)
use(HOOK, TOKEN)
Gateway.got.clear()
out = NotifyAgent().dispatch(con, [msg("w1", "first"), msg("w2", "second")])
check("2a one POST for the whole dispatch, not one per message", len(Gateway.got) == 1, str(len(Gateway.got)))
g = Gateway.got[0] if Gateway.got else {"body": {}}
ids = [r["id"] for r in latest(2)][::-1]
check("2b the batch carries both messages, by id, with their text",
      [m["id"] for m in g["body"].get("messages", [])] == ids
      and [m["body"] for m in g["body"]["messages"]] == ["first", "second"], str(g["body"])[:200])
check("2c the token goes as a bearer header", g.get("auth") == f"Bearer {TOKEN}", str(g.get("auth")))
check("2d JSON content type", g.get("type") == "application/json")
check("2e both rows 'Accepted', one attempt, a delivery time",
      all(r["status"] == "Accepted" and r["attempts"] == 1 and r["delivered_at"] for r in latest(2)),
      str(latest(2)))
check("2f the outcome counts them", out["accepted"] == 2 and out["failed"] == 0, str(out))
check("2g describe() says a webhook delivers", ng.describe() == {"gateway": "webhook", "delivers": True})

# A gated write, approved, through the one dispatch point.
Gateway.got.clear()
r = tools.execute(con, S(), "yes, send it", "broadcast_notice",
                  {"audience": "All students", "title": "Holiday", "body": "College closed on Monday.", "send": True})
check("2h an approved broadcast reaches the gateway", len(Gateway.got) == 1
      and Gateway.got[0]["body"]["messages"][0]["title"] == "Holiday", str(Gateway.got)[:200])
check("2i ...and its trace says the gateway accepted it",
      any(t[0] == "NotifyAgent" and t[1] == "deliver" and (len(t) < 4 or t[3] == "ok")
          for t in r.get("trace", [])), str(r.get("trace")))

# ------------------------------------------------------------------ 3 · failure
print("\n3 · a failing gateway fails the delivery, not the write")
print("-" * 78)
Gateway.answer = 503
Gateway.got.clear()
r = tools.execute(con, S(), "yes, send it", "broadcast_notice",
                  {"audience": "All students", "title": "Exam notice", "body": "Hall tickets ready.", "send": True})
row = latest()[0]
check("3a the gateway was asked", len(Gateway.got) == 1)
check("3b the notice is recorded and marked 'Failed'", row["title"] == "Exam notice" and row["status"] == "Failed",
      str(row))
check("3c the error names the HTTP status", row["error"] == "gateway answered HTTP 503", str(row["error"]))
check("3d the write is still reported as done", r["data"].get("sent") is True and not tools.failure_of(r),
      str(r["data"]))
fail_steps = [t for t in r.get("trace", []) if t[0] == "NotifyAgent" and t[1] == "deliver"]
check("3e the trace pins the failure on NotifyAgent with status 'error'",
      len(fail_steps) == 1 and fail_steps[0][3] == "error" and "HTTP 503" in fail_steps[0][2], str(fail_steps))
check("3f no delivery time on a failure", row["delivered_at"] is None)

Gateway.answer = 200
use("http://127.0.0.1:9/never", None)          # discard port: nothing listens
NotifyAgent().dispatch(con, [msg("unreachable")])
row = latest()[0]
check("3g an unreachable gateway is 'Failed', said in words",
      row["status"] == "Failed" and row["error"].startswith("gateway unreachable"), str(row))

# The rule engine's own path: a broadcast confirmed with "yes".
use(HOOK, TOKEN)
Gateway.answer = 500
sid = "notify-rule"
orchestrator.SESS.pop(sid, None)
orchestrator.handle(con, "Notify all CSE students that lab exams are postponed to 15 Sep", sid)
res = orchestrator.handle(con, "yes", sid)
words = " ".join(b.get("md", "") for b in res.get("blocks", []) if isinstance(b, dict))
check("3h the rule engine's reply says the gateway did not take it",
      "did not take it" in words and "HTTP 500" in words, words[:200])
check("3i ...and its trace has the NotifyAgent error step",
      any(s["agent"] == "NotifyAgent" and s["action"] == "deliver" and s["status"] == "error"
          for s in res.get("trace", [])), str(res.get("trace"))[:300])
Gateway.answer = 200

# ------------------------------------------------------------------ 4 · the gate
print("\n4 · a blocked write sends nothing")
print("-" * 78)
Gateway.got.clear()
n0 = one(con, "SELECT COUNT(*) n FROM notifications")["n"]
r = tools.execute(con, S(), "draft a notice about the holiday", "broadcast_notice",
                  {"audience": "All students", "title": "Holiday", "body": "Closed Monday.", "send": True})
check("4a without approval: no row and no request to the gateway",
      one(con, "SELECT COUNT(*) n FROM notifications")["n"] == n0 and not Gateway.got,
      f"{len(Gateway.got)} request(s)")
check("4b ...and no delivery step in its trace",
      not any(t[0] == "NotifyAgent" and t[1] == "deliver" for t in r.get("trace", [])))
r = tools.execute(con, S(), "yes", "broadcast_notice",
                  {"audience": "All students", "title": "Holiday", "body": "Closed Monday.", "send": False})
check("4c send=False with a 'yes' is still a draft: nothing leaves",
      not Gateway.got and one(con, "SELECT COUNT(*) n FROM notifications")["n"] == n0)

# ------------------------------------------------------------------ 5 · retry
print("\n5 · retry")
print("-" * 78)
use(HOOK, TOKEN)
Gateway.got.clear()
pending = [r["id"] for r in rows(con, "SELECT id FROM notifications WHERE status IN ('Queued','Failed')")]
recorded = [r["id"] for r in rows(con, "SELECT id FROM notifications WHERE status='Recorded'")]
out = ng.retry(con)
sent_ids = sorted(m["id"] for g in Gateway.got for m in g["body"]["messages"])
check("5a every Failed notice is re-sent", sent_ids == sorted(pending) and len(pending) >= 3,
      f"{sent_ids} vs {pending}")
check("5b Recorded notices are left alone", not set(sent_ids) & set(recorded) and recorded)
check("5c they are 'Accepted' now, on their second attempt",
      all(one(con, "SELECT status s, attempts a FROM notifications WHERE id=?", (i,))["s"] == "Accepted"
          and one(con, "SELECT attempts a FROM notifications WHERE id=?", (i,))["a"] >= 2 for i in pending))
check("5d the error is cleared once delivered",
      not one(con, f"SELECT 1 FROM notifications WHERE id IN ({','.join('?' * len(pending))}) "
                   f"AND error IS NOT NULL", pending))
check("5e a second retry has nothing to do", ng.retry(con)["retried"] == 0)

# ------------------------------------------------------------------ 6 · outbox + config
print("\n6 · the outbox file, and the configuration checks")
print("-" * 78)
box = os.path.join(tempfile.gettempdir(), "vidyaerp_notify_outbox", "out.jsonl")
if os.path.exists(box):
    os.remove(box)
use("file:" + box.replace(os.sep, "/") if box[1:2] != ":" else "file:///" + box.replace(os.sep, "/"))
check("6a a file: URL is the outbox form", ng.config()["kind"] == "outbox", str(ng.config()))
NotifyAgent().dispatch(con, [msg("o1", "line one"), msg("o2", "ಕನ್ನಡ ಸೂಚನೆ")])
with open(box, encoding="utf-8") as f:
    lines = [json.loads(x) for x in f]
check("6b one JSON line per notice, Unicode intact",
      [x["body"] for x in lines] == ["line one", "ಕನ್ನಡ ಸೂಚನೆ"], str(lines)[:200])
check("6c both 'Accepted'", [r["status"] for r in latest(2)] == ["Accepted", "Accepted"])
use("ftp://example.org/x")
check("6d an unsupported scheme is a configuration error, not 'no gateway'",
      ng.config()["kind"] == "invalid" and ng.describe()["delivers"] is False)
NotifyAgent().dispatch(con, [msg("bad scheme")])
check("6e ...and its notices are Failed with the reason, not Recorded",
      latest()[0]["status"] == "Failed" and "must be" in latest()[0]["error"], str(latest()[0]))
Gateway.got.clear()
use(f"http://example.org:{srv.server_address[1]}/hook", TOKEN)
check("6f a token is never sent over plain http to a non-loopback host",
      "error" in ng.config() and "plain http" in ng.config()["error"])
NotifyAgent().dispatch(con, [msg("plain http")])
check("6g ...so that dispatch fails before any connection is made",
      latest()[0]["status"] == "Failed" and not Gateway.got)
use("https://gateway.example/hook", TOKEN)
check("6h https with a token is fine", ng.config().get("error") is None and ng.config()["kind"] == "webhook")
use(f"http://localhost:{srv.server_address[1]}/hook", TOKEN)
check("6i plain http with a token to loopback is fine (an on-box relay)", "error" not in ng.config())

# ------------------------------------------------------------------ 7 · no leaks
print("\n7 · nothing reports the URL or the token")
print("-" * 78)
use(HOOK, TOKEN)
Gateway.answer = 401
NotifyAgent().dispatch(con, [msg("leak check")])
Gateway.answer = 200
use("http://127.0.0.1:9/x?key=secretquery", TOKEN)
NotifyAgent().dispatch(con, [msg("leak check 2")])
blob = json.dumps(rows(con, "SELECT * FROM notifications"), default=str)
check("7a no stored error carries the token", TOKEN not in blob)
check("7b ...or the URL's query string", "secretquery" not in blob)
check("7c ...or the host and port", f":{srv.server_address[1]}" not in blob)
use(HOOK, TOKEN)
d = json.dumps(ng.describe())
check("7d describe() names the kind and nothing else", TOKEN not in d and "127.0.0.1" not in d
      and set(ng.describe()) == {"gateway", "delivers"}, d)

import app as webapp                                               # noqa: E402

h = json.loads(webapp.health().body)
check("7e /health reports the gateway without its URL or token",
      h.get("notifications") == {"gateway": "webhook", "delivers": True}
      and TOKEN not in json.dumps(h) and "secretquery" not in json.dumps(h), str(h.get("notifications")))
check("7f the retry endpoint is Registrar-only",
      webapp.auth.gate("/api/notifications/retry", None, {}, None) is not None
      and webapp.auth.gate("/api/notifications/retry", {"id": "1VT23CS001", "role": "student"}, {}, None)
      is not None)

# ------------------------------------------------------------------ 8 · old DB
print("\n8 · an older database gains the columns")
print("-" * 78)
old = sqlite3.connect(":memory:")
old.execute("""CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, audience TEXT, channel TEXT,
               title TEXT, body TEXT, created_at TEXT, status TEXT)""")
old.execute("INSERT INTO notifications(audience,channel,title,body,created_at,status) VALUES('a','b','c','d','e','Sent')")
ng.ensure(old)
cols = {r[1] for r in old.execute("PRAGMA table_info(notifications)")}
check("8a attempts, delivered_at and error are added", {"attempts", "delivered_at", "error"} <= cols, str(cols))
ng.ensure(old)
check("8b ensure() is idempotent", len([r for r in old.execute("PRAGMA table_info(notifications)")]) == 10)
check("8c existing rows survive", old.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1)

use()
srv.shutdown()
print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
sys.exit(1 if FAILED else 0)
