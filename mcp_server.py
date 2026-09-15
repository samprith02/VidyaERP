"""
VidyaERP :: MCP surface

Puts the existing tools in front of an external MCP client - Claude Desktop,
ChatGPT, anything that speaks the protocol - without giving that client a
second way into the database.

    Claude Desktop ──stdio JSON-RPC──▶ mcp_server.call_as
                                             │
                                             ▼
                                      tools.execute  ◀── llm_agent.py (the app's
                                             │             own planner) arrives
                                             ▼             at exactly this line
                                      PolicyGuard-wrapped tools

`call_as` is, by reading, a thin delegation to `tools.execute`. There is no
authorisation logic in this module and there must never be: the moment this
file makes its own allow/deny decision, the guarded surface an external model
sees stops being the guarded surface the app sees, and the parity claim is
gone. `tests/mcp_parity.py` replays the same adversarial items through both
entry points and fails if the verdicts ever diverge.

No MCP-only tool. No tool hidden from MCP. All 22 are exposed and the guard is
the only thing that decides.

────────────────────────────────────────────────────────────────────────────
Where approval comes from, and why it is not in the arguments
────────────────────────────────────────────────────────────────────────────
The five tools in `tools.GATED_WRITES` commit only when the admin approved in
the current turn - `tools.approved_this_turn(U)`, where `U` is the admin's own
message. On the web path the model cannot forge `U`, because it never writes
the user's message.

Over MCP there is no user message: **the caller is the model**. An "approval"
string in the tool arguments is therefore worth exactly nothing, and this
module never reads one. The only thing that can produce an approving `U` here
is a one-time code the admin minted out of band:

    python mcp_server.py --approve          (on the machine running the ERP)
    POST /api/mcp/approval                  (from the VidyaERP console)

The code is six digits, valid for 120 seconds, single-use, and consumed the
moment it is redeemed. A model can relay a code the admin read out; it cannot
mint one, and it cannot replay one. No code means no write - the same BLOCKED
payload the web path returns when the admin has not said yes.

────────────────────────────────────────────────────────────────────────────
Dependencies: none. MCP's stdio transport is newline-delimited JSON-RPC 2.0,
which is `json` and `sys.stdin`. There is no MCP SDK here for the same reason
there is no LLM SDK in llm.py.

Gotcha, and it is a sharp one: **stdout is the protocol**. Anything printed
there that is not a JSON-RPC message corrupts the stream and the client drops
the connection with no useful error. Every diagnostic in this file goes to
stderr, and that is why.

    Claude Desktop registration (claude_desktop_config.json):

    {"mcpServers": {"vidyaerp": {
        "command": "python",
        "args": ["C:/Users/you/VidyaERP/mcp_server.py"]}}}
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db
import orchestrator
import tools
from agents import Auditor, one

SERVER_NAME = "vidyaerp"
SERVER_VERSION = "1.0.0"

# Revisions of the MCP spec this server will speak. A client that asks for one
# of these gets it echoed back; anything else is answered with our newest, which
# is what the spec says to do when the requested revision is unsupported.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL = PROTOCOL_VERSIONS[0]

MCP_SESSION = "mcp"            # one orchestrator session, so propose -> commit works
MCP_ACTOR = "mcp-client@claude-desktop"


# ============================================================ approval channel
APPROVAL_TTL = 120             # seconds - long enough to read a code out, short
                               # enough that a leaked one is worthless
APPROVAL_PHRASE = ("the admin approved this write in the VidyaERP console")
# ^ redeeming a code hands this to the tool as `U`. It is a real approval in the
#   one sense that matters: a human produced it. APPROVAL_RX matches "approved".

_APPROVALS_DDL = """
CREATE TABLE IF NOT EXISTS mcp_approvals(
  code TEXT PRIMARY KEY, issued_at TEXT, expires_at TEXT,
  issued_to TEXT, used_at TEXT, used_for TEXT)"""


def _ensure_approvals(con):
    """Created on demand rather than in db.SCHEMA, so an existing college.db
    picks the table up without a reseed."""
    con.execute(_APPROVALS_DDL)
    con.commit()


def mint_approval(con, issued_to="admin@vidyatech", ttl=APPROVAL_TTL):
    """The human half of the write gate. Called from the console or the CLI -
    never from anything an MCP client can reach."""
    _ensure_approvals(con)
    now = dt.datetime.now()
    # Codes live 120 seconds; anything a day old is spent or long expired, and
    # keeping it only makes a collision on a six-digit space more likely.
    con.execute("DELETE FROM mcp_approvals WHERE issued_at < ?",
                ((now - dt.timedelta(days=1)).isoformat(timespec="seconds"),))
    code = f"{secrets.randbelow(1000000):06d}"
    con.execute("INSERT OR REPLACE INTO mcp_approvals"
                "(code,issued_at,expires_at,issued_to,used_at,used_for) VALUES(?,?,?,?,NULL,NULL)",
                (code, now.isoformat(timespec="seconds"),
                 (now + dt.timedelta(seconds=ttl)).isoformat(timespec="seconds"), issued_to))
    con.commit()
    Auditor().log(con, issued_to, "MCP", "approval.mint", {"ttl_seconds": ttl}, "issued")
    return {"code": code, "expires_at": (now + dt.timedelta(seconds=ttl)).isoformat(timespec="seconds"),
            "ttl_seconds": ttl,
            "note": "Single use. Read it out to the MCP client for ONE guarded write."}


def redeem_approval(con, code, tool_name):
    """Consume a one-time approval. Returns the approval phrase, or "" - and ""
    is the same `U` a web turn carries when the admin has not said yes, so the
    tool blocks identically.

    The UPDATE is the check: `used_at IS NULL` in the WHERE clause means two
    concurrent redemptions of one code cannot both win.
    """
    _ensure_approvals(con)
    code = re.sub(r"\D", "", str(code or ""))
    if not code:
        return ""
    row = one(con, "SELECT * FROM mcp_approvals WHERE code=?", (code,))
    if not row or row["used_at"] or dt.datetime.now() > dt.datetime.fromisoformat(row["expires_at"]):
        return ""
    cur = con.execute("UPDATE mcp_approvals SET used_at=?, used_for=? WHERE code=? AND used_at IS NULL",
                      (dt.datetime.now().isoformat(timespec="seconds"), tool_name, code))
    con.commit()
    return APPROVAL_PHRASE if cur.rowcount == 1 else ""


def approval_text(con, name, args):
    """Where `U` comes from over MCP, and the only place it can come from.

    `args` is model-authored, so it is searched for exactly one thing: a code
    the model may have been *told* by the admin. Everything else in there -
    an "approval": "yes go ahead", a "confirmed": true - is ignored on purpose.
    """
    if name not in tools.GATED_WRITES:
        return ""                                  # reads do not consult U at all
    return redeem_approval(con, (args or {}).pop("approval_code", None), name)


# ================================================================ the entry point
def call_as(con, S, U, name, args):
    """The single external entry point. Deliberately a thin delegation.

    If this function ever grows a branch, the guard-parity claim is void.
    tests/mcp_parity.py asserts its bytecode still references nothing but
    tools.execute."""
    return tools.execute(con, S, U, name, args)


# ===================================================================== tool specs
_APPROVAL_ARG = {
    "type": ["string", "null"],
    "description": ("One-time six-digit approval code the ADMIN generated in the VidyaERP "
                    "console or with `python mcp_server.py --approve`. You cannot create "
                    "one; ask the admin for it. Without a valid unused code this call is "
                    "refused, which is the expected behaviour, not an error."),
}


def tool_specs():
    """The MCP tool list: every tool in the registry, schemas deep-copied so the
    approval argument cannot leak into the schemas the in-app planner sends."""
    out = []
    for _fn, name, desc, _schema in tools.REGISTRY:
        params = copy.deepcopy(tools.BY_NAME[name]["function"]["parameters"])
        if name in tools.GATED_WRITES:
            params.setdefault("properties", {})["approval_code"] = dict(_APPROVAL_ARG)
            desc = desc + (" GUARDED: commits only with a one-time approval code the admin "
                           "issued out of band. Propose first, show the admin, then ask.")
        out.append({"name": name, "description": desc, "inputSchema": params})
    return out


def _as_content(result):
    """Tool result -> MCP content. `blocks` are for the VidyaERP console's
    renderer and mean nothing to an external client, so they are dropped; `data`
    is already the compact JSON a model reasons over. `trace` rides along
    because the guard's own reasoning is the interesting part over MCP."""
    data = result.get("data", {})
    payload = {"data": data}
    if result.get("trace"):
        payload["trace"] = [{"agent": t[0], "action": t[1],
                             "detail": t[2] if len(t) > 2 else ""} for t in result["trace"]]
    is_error = isinstance(data, dict) and ("error" in data or "BLOCKED" in data)
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str, indent=1)}],
            "isError": is_error}


# ================================================================== JSON-RPC 2.0
def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle_request(req, con, S):
    """One JSON-RPC request -> one response dict, or None for a notification."""
    rid = req.get("id")
    method = req.get("method") or ""
    params = req.get("params") or {}

    if method.startswith("notifications/"):
        return None                                # notifications are never answered

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _ok(rid, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else LATEST_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "VidyaERP - college ERP for Vidyatech Institute of Engineering. Reads are open. "
                "The five write tools commit only with a one-time approval code the admin "
                "generates in the ERP console; you cannot generate one. Always call the plan_* "
                "tool first, show the admin what would change, and ask them for a code."),
        })

    if method == "ping":
        return _ok(rid, {})

    if method == "tools/list":
        return _ok(rid, {"tools": tool_specs()})

    if method in ("prompts/list", "resources/list", "resources/templates/list"):
        return _ok(rid, {method.split("/")[0]: []})

    if method == "tools/call":
        name = params.get("name")
        args = dict(params.get("arguments") or {})
        if not name:
            return _err(rid, -32602, "tools/call requires a tool name")
        # U is built here, from the approval channel - never from `args`.
        U = approval_text(con, name, args)
        result = call_as(con, S, U, name, args)
        _audit(con, name, args, result)
        return _ok(rid, _as_content(result))

    return _err(rid, -32601, f"Method not found: {method}")


def _audit(con, name, args, result):
    """Who asked, which tool acted, what the guard said. The ledger must show an
    MCP-driven write exactly as clearly as a console-driven one."""
    data = result.get("data", {})
    outcome = ("blocked" if isinstance(data, dict) and "BLOCKED" in data else
               "error" if isinstance(data, dict) and "error" in data else "ok")
    try:
        Auditor().log(con, MCP_ACTOR, "MCP", f"tools/call {name}",
                      {k: v for k, v in args.items() if k != "approval_code"}, outcome)
    except Exception as e:                          # the ledger must never break the call
        _log(f"audit write failed: {type(e).__name__}: {e}")


# ======================================================================== stdio
def _log(msg):
    """stderr, always. stdout is the protocol."""
    print(f"[vidyaerp-mcp] {msg}", file=sys.stderr, flush=True)


def serve(stdin=None, stdout=None):
    """Read newline-delimited JSON-RPC from stdin, answer on stdout, until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for stream in (stdin, stdout):
        try:
            stream.reconfigure(encoding="utf-8")   # Windows defaults to cp1252
        except (AttributeError, ValueError):
            pass

    db.seed()
    con = db.connect()
    S = orchestrator.sess(MCP_SESSION)
    _log(f"ready · {len(tools.FUNCS)} tools · {len(tools.GATED_WRITES)} of them guarded")

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _write(stdout, _err(None, -32700, f"Parse error: {e}"))
            continue
        for item in (req if isinstance(req, list) else [req]):
            try:
                resp = handle_request(item, con, S)
            except Exception as e:                  # never take the transport down
                _log(f"unhandled {type(e).__name__}: {e}")
                resp = _err(item.get("id"), -32603, f"Internal error: {type(e).__name__}")
            if resp is not None:
                _write(stdout, resp)
    _log("stdin closed · shutting down")


def _write(stdout, obj):
    stdout.write(json.dumps(obj, default=str) + "\n")
    stdout.flush()


# ========================================================================== CLI
def _main(argv):
    if "--approve" in argv:
        db.seed()
        con = db.connect()
        a = mint_approval(con)
        print(f"approval code  {a['code']}\nvalid until    {a['expires_at']}  "
              f"({a['ttl_seconds']}s)\nsingle use     read it out to the MCP client for ONE write")
        return 0
    if "--tools" in argv:
        db.seed()
        for t in tool_specs():
            print(f"{'*' if t['name'] in tools.GATED_WRITES else ' '} {t['name']}")
        print(f"\n{len(tool_specs())} tools · * = guarded, needs a one-time admin approval code")
        return 0
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
