"""Notification delivery (#26), and an honest status for every notice (#83).

Every notice still lands in the `notifications` table first; that row is the
record. What this module adds is the hand-off to something that can actually
reach a phone or an inbox, and a status that says what happened to it:

    Recorded   no gateway is configured - the row is all there is
    Queued     a gateway is configured and the attempt has not finished
    Accepted   the gateway took it (an HTTP 2xx, or a line in the outbox file)
    Failed     the gateway refused it or could not be reached; `error` says why

"Accepted" and not "Delivered" on purpose: the gateway owns the last mile (SMS,
email, WhatsApp, the recipient lookup), and a 2xx means it took the message,
not that a student read it. Before this the row said "Sent" when nothing was.

It sits OUTSIDE the write gate's decision. A gated write commits, then its
notices are dispatched; a gateway that is down marks them Failed and the write
stays committed and reported as committed. A write the gate blocks never gets
here, so a blocked write sends nothing (tests/notify_test.py section 4).

Configured by environment only, like the LLM key:

    VIDYAERP_NOTIFY_URL     https://gateway.example/hook   POST one JSON batch
                            file:///var/vidyaerp/outbox.jsonl   append JSON lines
    VIDYAERP_NOTIFY_TOKEN   optional; sent as "Authorization: Bearer ..."

Nothing here ever reports the URL or the token: not /health, not the stored
error, not the trace. A token is never sent over plain http to anything but
the loopback interface. No new dependency: `urllib` from the standard library.
"""
import datetime as dt
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 5                       # seconds; one batch per dispatch, not one per message
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
COLUMNS = {"attempts": "INT DEFAULT 0", "delivered_at": "TEXT", "error": "TEXT"}
_CALL = threading.local()         # outcomes of the deliveries made during one tool call


def ensure(con):
    """Add the delivery columns to a notifications table that predates them."""
    have = {r[1] for r in con.execute("PRAGMA table_info(notifications)")}
    if not have:
        return
    for col, decl in COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE notifications ADD COLUMN {col} {decl}")
    con.commit()


def config():
    """What the environment asks for: {"kind": none|webhook|outbox, ...}.

    `error` is set when the configuration cannot be used; the caller then marks
    the notices Failed rather than pretending there is no gateway.
    """
    url = (os.environ.get("VIDYAERP_NOTIFY_URL") or "").strip()
    token = (os.environ.get("VIDYAERP_NOTIFY_TOKEN") or "").strip()
    if not url:
        return {"kind": "none"}
    u = urllib.parse.urlsplit(url)
    if u.scheme == "file":
        path = urllib.request.url2pathname(u.path) if not u.netloc else \
            urllib.request.url2pathname(f"//{u.netloc}{u.path}")
        return {"kind": "outbox", "path": path}
    if u.scheme in ("http", "https") and u.hostname:
        if u.scheme == "http" and token and u.hostname not in LOOPBACK:
            return {"kind": "webhook", "error": "refusing to send the token over plain http"}
        return {"kind": "webhook", "url": url, "token": token}
    return {"kind": "invalid", "error": "VIDYAERP_NOTIFY_URL must be https://, http:// or file://"}


def describe():
    """For /health: whether notices leave the building, never where to."""
    c = config()
    return {"gateway": c["kind"], "delivers": c["kind"] in ("webhook", "outbox") and "error" not in c,
            **({"error": c["error"]} if "error" in c else {})}


def initial_status():
    return "Recorded" if config()["kind"] == "none" else "Queued"


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _post(cfg, payload):
    req = urllib.request.Request(cfg["url"], data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "vidyaerp-notify/1"})
    if cfg.get("token"):
        req.add_header("Authorization", f"Bearer {cfg['token']}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:     # raises HTTPError on >= 400
        return r.status


def _append(cfg, payload):
    d = os.path.dirname(os.path.abspath(cfg["path"]))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(cfg["path"], "a", encoding="utf-8") as f:
        for m in payload["messages"]:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")


def _why(e):
    """A failure in words that name the class of problem and nothing else.

    Deliberately not str(e): a URLError can carry the host, and an HTTPError's
    text carries the URL, which may have a token in its query string.
    """
    if isinstance(e, urllib.error.HTTPError):
        return f"gateway answered HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        return f"gateway unreachable ({type(r).__name__})" if not isinstance(r, str) else "gateway unreachable"
    if isinstance(e, TimeoutError):
        return f"gateway timed out after {TIMEOUT}s"
    if isinstance(e, OSError):
        return f"outbox not writable ({type(e).__name__})"
    return f"delivery failed ({type(e).__name__})"


def deliver(con, ids):
    """Hand the given notification rows to the gateway; record the outcome on each.

    Returns {"gateway", "accepted", "failed", "recorded", "error"?}. Never raises:
    the caller has already committed its write and must not be told otherwise.
    """
    ids = [i for i in ids if i is not None]
    cfg = config()
    out = {"gateway": cfg["kind"], "accepted": 0, "failed": 0, "recorded": 0}
    if not ids:
        return out
    if cfg["kind"] == "none":
        out["recorded"] = len(ids)
        return out
    q = ",".join("?" * len(ids))
    msgs = [dict(r) for r in con.execute(
        f"SELECT id,audience,channel,title,body,created_at FROM notifications WHERE id IN ({q}) ORDER BY id",
        ids)]
    payload = {"source": "vidyaerp", "messages": msgs}
    err = cfg.get("error")
    if not err:
        try:
            if cfg["kind"] == "outbox":
                _append(cfg, payload)
            else:
                _post(cfg, payload)
        except Exception as e:                    # noqa: BLE001 - every failure becomes a status
            err = _why(e)
    status = "Failed" if err else "Accepted"
    con.execute(f"""UPDATE notifications SET status=?, attempts=COALESCE(attempts,0)+1,
                    delivered_at=?, error=? WHERE id IN ({q})""",
                (status, None if err else _now(), err, *ids))
    con.commit()
    out["failed" if err else "accepted"] = len(msgs)
    if err:
        out["error"] = err
    stack = getattr(_CALL, "stack", None)
    if stack:
        stack[-1].append(out)
    return out


# ------------------------------------------------- reporting it in the trace
def begin():
    """Start collecting this thread's delivery outcomes (tools.execute, per call).
    A stack, so a call nested inside another reports only its own."""
    if not hasattr(_CALL, "stack"):
        _CALL.stack = []
    _CALL.stack.append([])


def end():
    """The trace steps for what was delivered since the matching begin()."""
    stack = getattr(_CALL, "stack", None)
    outs = stack.pop() if stack else []
    return [s for s in (step(o) for o in outs) if s]


def step(out):
    """A trace step (agent, action, detail, status) for one delivery, or None.

    None when no gateway is configured: the notice was recorded, which is the
    documented behaviour, and the dispatch step already says so. A gateway that
    refused or could not be reached is a real NotifyAgent failure and is drawn
    as one; the write that produced the notice is not, and stays committed.
    """
    if not out or out["gateway"] == "none":
        return None
    if out["failed"]:
        return ("NotifyAgent", "deliver", f"{out['failed']} notice(s) not delivered: {out['error']}", "error")
    return ("NotifyAgent", "deliver", f"{out['accepted']} notice(s) accepted by the {out['gateway']}", "ok")


def sentence(out, audience):
    """What to tell the admin about one broadcast, in words that are true."""
    if out["gateway"] == "none":
        return (f"Recorded for **{audience}**. No delivery gateway is configured, so it has not left "
                f"this system; it is listed under Notifications.")
    if out["failed"]:
        return (f"Recorded for **{audience}**, but the gateway did not take it: {out['error']}. "
                f"Retry it from Notifications.")
    return f"Handed to the delivery gateway for **{audience}**; it accepted the notice."


def retry(con, limit=200):
    """Try every Queued or Failed notice again. Recorded ones stay as they are:
    they were written while no gateway existed, and replaying a week of old
    notices the moment one is configured would be its own surprise."""
    ids = [r[0] for r in con.execute(
        "SELECT id FROM notifications WHERE status IN ('Queued','Failed') ORDER BY id LIMIT ?", (limit,))]
    return {"retried": len(ids), **deliver(con, ids)}
