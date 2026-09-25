"""The fault channel behind the agent mesh. No server, no LLM, no API cost.

    python tests/mesh_test.py

The console's 3D mesh blinks an agent red only where the SERVER said
status="error". That is only honest if the server says it every time a tool
fails, pins it on the agent that owns the tool, never says it for a write the
guard merely held, and never names an agent the graph does not know. This file
holds those four down (#45).
"""
import json
import os
import re
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_mesh_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import mcp_server                                                  # noqa: E402
import orchestrator as orch                                        # noqa: E402
import tools                                                       # noqa: E402

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
MESH = open(os.path.join(HERE, "static", "mesh.js"), encoding="utf-8").read()
CATALOG = set(re.findall(r"^\s*'([A-Za-z ]+)':\s*\{g:", MESH, re.M))
S = lambda: {"pending": None, "history": [], "ctx": {}}


def errors(trace):
    return [t for t in trace if t.get("status") == "error"]


# ============================================================ 1 · the catalog
print("\n1 · the graph knows every agent the server can name")
print("-" * 78)
check("1a the mesh catalog parsed", len(CATALOG) >= 30, str(len(CATALOG)))
owners = set(tools.TOOL_AGENT.values())
check("1b every tool has an owning agent", set(tools.FUNCS) <= set(tools.TOOL_AGENT),
      str(set(tools.FUNCS) - set(tools.TOOL_AGENT)))
check("1c every owning agent is in the mesh catalog", owners <= CATALOG, str(owners - CATALOG))
seen = set()
for q in ["What needs my attention today?", "Brief me on today's institution status", "Library status",
          "Hostel status", "Transport status", "Pending gate passes", "Placement status",
          "Certificate register", "Review pending leave applications", "Everything about 4VP24CS017",
          "Show CSE 5th sem A timetable", "Faculty workload above 90%", "Attendance defaulters in ECE sem 5",
          "Pending approvals in my inbox", "SEE eligibility check", "Fee dues by department",
          "Who is on leave this week?", "Notify all CSE students that lab exams are postponed",
          "Rebuild the timetable for CSE sem 5 A", "hello there"]:
    orch.SESS.pop("mesh", None)
    seen |= {t["agent"] for t in orch.handle(con, q, "mesh")["trace"]}
check("1d every agent a real rule-engine run emits is in the catalog", seen <= CATALOG, str(seen - CATALOG))

# ======================================================= 2 · failures are said
print("\n2 · a failed tool call is reported as an error, on the agent that owns it")
print("-" * 78)
out = orch.handle(con, "No dues status of 4VP99ZZ999", "f1")
e = errors(out["trace"])
check("2a rule engine: an unknown USN is an error step", bool(e), json.dumps(out["trace"])[:200])
check("2b ...pinned on the agent that owns the tool, not on the bus",
      e and e[-1]["agent"] == "DocumentAgent" and e[-1]["action"] == "tool_error", str(e))
out = orch.handle(con, "Student 4VP99ZZ999 details", "f2")
check("2c the classic student lookup reports a missing record as an error",
      any(t["agent"] == "StudentAgent" for t in errors(out["trace"])), json.dumps(out["trace"])[-240:])
r = tools.execute(con, S(), "", "issue_book", {"book": "BK0001", "member": "4VP99ZZ999"})
check("2d tools.failure_of sees a tool's error", tools.failure_of(r) and "4VP99ZZ999" in tools.failure_of(r))
check("2e tools.agent_of names the owner", tools.agent_of("issue_book") == "LibraryAgent"
      and tools.agent_of("plan_absence_coverage") == "SubstitutionAgent"
      and tools.agent_of("no_such_tool") == "ToolBus")

# =================================================== 3 · a hold is not a fault
print("\n3 · a write held by PolicyGuard is the guard working, not a failure")
print("-" * 78)
held = tools.execute(con, S(), "", "library_remind_overdue", {})
check("3a a held write is BLOCKED...", "BLOCKED" in held["data"])
check("3b ...and failure_of says it is not a failure", tools.failure_of(held) is None)
out = orch.handle(con, "Send overdue library reminders", "h1")
check("3c the rule-engine turn carries a write_blocked step and no error step",
      any(t["action"] == "write_blocked" for t in out["trace"]) and not errors(out["trace"]),
      json.dumps(errors(out["trace"])))
out = orch.handle(con, "yes", "h1")
check("3d approving it produces a verify step that is NOT an error (the reminders really went)",
      not errors(out["trace"]) and any(t["action"] == "write_authorisation" for t in out["trace"]))

# ======================================== 4 · every verify step carries a status
print("\n4 · post-write verification reports its own outcome")
print("-" * 78)
src = open(os.path.join(HERE, "campus.py"), encoding="utf-8").read()
verifies = re.findall(r'\("\w+Agent", "verify",[^()]*?(?:\([^()]*\)[^()]*?)*\)', src)
bare = [v for v in verifies if not re.search(r'"(?:ok|error)"\s*(?:if|\))|else "(?:ok|error)"', v)]
check("4a every campus verify step states ok/error explicitly", verifies and not bare,
      f"{len(verifies)} found; without a status: {bare[:2]}")
r = tools.execute(con, S(), "yes", "allocate_hostel_room", {})
v = [t for t in r["trace"] if t[1] == "verify"]
check("4b a clean allotment verifies as ok", v and v[0][3] == "ok", str(v))
bad = tools.execute(con, S(), "yes", "handle_bus_breakdown", {"route": "R03"})      # 2 spares exist
bad2 = tools.execute(con, S(), "yes", "handle_bus_breakdown", {"route": "R06"})
bad3 = tools.execute(con, S(), "yes", "handle_bus_breakdown", {"route": "R01"})    # no spare left
check("4c running out of spare buses is a reported failure, not a silent one",
      tools.failure_of(bad3) and "spare" in tools.failure_of(bad3), str(bad3.get("data")))

# =============================================================== 5 · surfaces
print("\n5 · every surface carries the status")
print("-" * 78)
resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "library_remind_overdue", "arguments": {}}}, con, S())
tr = json.loads(resp["result"]["content"][0]["text"]).get("trace", [])
check("5a MCP trace steps carry a status", tr and all("status" in t for t in tr), str(tr[:1]))
llm_src = open(os.path.join(HERE, "llm_agent.py"), encoding="utf-8").read()
check("5b the LLM loop records a failed tool as an error on its owner",
      'add(tools.agent_of(name), "tool_error"' in llm_src and "tools.failure_of(result)" in llm_src)
check("5c an unreachable model is an error, not a warning",
      'add("LLM Planner", "transport_error", str(e)[:160], "error")' in llm_src)
html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
scripts = re.findall(r'<script\s+src="([^"]+)"', html)
check("5d the console loads only local scripts", scripts == ["/static/mesh.js"], str(scripts))
check("5e mesh.js itself fetches nothing from outside",
      not re.search(r"https?://", MESH), re.findall(r"https?://\S+", MESH)[:2])
check("5f the mesh draws red only from the server's own status",
      "if (step.status === 'error') return 'error';" in MESH and "Math.random() < " not in MESH)

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
