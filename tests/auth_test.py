"""Logins, route gating and per-role tool policy (#27). No server, no LLM, no API cost.

    python tests/auth_test.py

What this holds down:
  1. identity   - passwords, demo vs strict mode, hashed session tokens, lockout;
  2. routes     - auth.gate decides every route the app actually registers;
  3. tools      - default deny per role, scoping that only ever NARROWS, and no
                  admin write reachable by a student, teacher or HOD, even with "yes";
  4. self-service writes act for the signed-in person only, propose first, and
     re-read what they wrote;
  5. sessions   - a pending write belongs to the person who staged it; the role
                  comes from the session, never from the request body.
"""
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_auth_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import access                                                      # noqa: E402
import auth                                                        # noqa: E402
import orchestrator as orch                                        # noqa: E402
import tools                                                       # noqa: E402
from agents import one, rows                                       # noqa: E402

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
auth.ensure(con)
HOD = one(con, "SELECT * FROM faculty WHERE is_hod=1 AND dept='CSE'")
FAC = one(con, "SELECT * FROM faculty WHERE is_hod=0 AND dept='CSE' AND id IN (SELECT mentor FROM students) ORDER BY id")
FAC2 = one(con, "SELECT * FROM faculty WHERE is_hod=0 AND dept='ECE' ORDER BY id")
STU = one(con, "SELECT * FROM students WHERE dept='CSE' AND sem=5 AND hostel=1 AND fee_due=0 ORDER BY usn")
STU2 = one(con, "SELECT * FROM students WHERE dept='ECE' ORDER BY usn")
P = {r: auth.principal(con, i) for r, i in (("admin", "registrar"), ("hod", HOD["id"]), ("faculty", FAC["id"]),
                                          ("student", STU["usn"]))}


def S(role=None):
    return {"pending": None, "history": [], "ctx": {}, "user": P.get(role) if role and role != "admin" else None}


def call(role, name, args=None, U=""):
    return tools.execute(con, S(role), U, name, args or {})


def denied(r):
    return "DENIED" in (r.get("data") or {})


# ================================================================ 1 · identity
print("\n1 · identity")
print("-" * 78)
check("1a roles come from the data", [P[r]["role"] for r in ("admin", "hod", "faculty", "student")]
      == ["admin", "hod", "faculty", "student"], str({r: P[r]["role"] for r in P}))
check("1b an unknown account resolves to nobody", auth.principal(con, "4VP99ZZ999") is None
      and auth.principal(con, "F999") is None)
os.environ.pop("VIDYAERP_DEMO_LOGINS", None)
u, tok = auth.login(con, STU["usn"], STU["usn"].lower())
check("1c demo mode: the published password (the ID) signs in", u and u["id"] == STU["usn"], str(tok)[:80])
bad_pw = auth.login(con, STU["usn"], "nope", key="t-1c")[1]
bad_user = auth.login(con, "4VP99ZZ999", "nope", key="t-1c2")[1]
check("1d a wrong password and an unknown user get the SAME message", bad_pw == bad_user, f"{bad_pw} / {bad_user}")
check("1e only the token's SHA-256 is stored, never the token",
      not one(con, "SELECT 1 FROM sessions WHERE token_hash=?", (tok,))
      and one(con, "SELECT 1 FROM sessions WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),)))
check("1f the session resolves to its owner", auth.session_user(con, tok)["id"] == STU["usn"])
auth.logout(con, tok)
check("1g signing out ends the session", auth.session_user(con, tok) is None)
u, tok = auth.login(con, FAC["id"], FAC["id"].lower())
con.execute("UPDATE sessions SET expires='2000-01-01T00:00:00'")
con.commit()
check("1h an expired session is refused", auth.session_user(con, tok) is None)
auth.set_password(con, FAC["id"], "correct horse battery")
check("1i a set password replaces the published one",
      auth.login(con, FAC["id"], FAC["id"].lower(), key="t-1i")[0] is None
      and auth.login(con, FAC["id"], "correct horse battery", key="t-1i2")[0] is not None)
row = one(con, "SELECT * FROM credentials WHERE username=?", (FAC["id"],))
check("1j stored as salted PBKDF2, not the password", row and "correct horse" not in json.dumps(row)
      and row["rounds"] >= 100_000 and len(row["salt"]) == 32)
os.environ["VIDYAERP_DEMO_LOGINS"] = "0"
check("1k strict mode: the published password stops working", auth.login(con, STU2["usn"], STU2["usn"].lower(),
                                                                         key="t-1k")[0] is None)
check("1l strict mode: a set password still works", auth.login(con, FAC["id"], "correct horse battery", key="t-1l")[0])
os.environ["VIDYAERP_ADMIN_PASSWORD"] = "registrar-strict-9"
check("1m strict mode: the Registrar signs in only with VIDYAERP_ADMIN_PASSWORD",
      auth.login(con, "registrar", "registrar", key="t-1m")[0] is None
      and auth.login(con, "registrar", "registrar-strict-9", key="t-1m2")[0] is not None)
os.environ.pop("VIDYAERP_ADMIN_PASSWORD"); os.environ.pop("VIDYAERP_DEMO_LOGINS")
for i in range(auth.LOCK_AFTER):
    auth.login(con, STU2["usn"], f"guess{i}", key="lock-me")
check("1n repeated failures lock the account...", auth.login(con, STU2["usn"], STU2["usn"].lower(), key="lock-me")[0]
      is None)
check("1o ...and say so", "Too many attempts" in auth.login(con, STU2["usn"], "x", key="lock-me")[1])
u, tok = auth.login(con, STU["usn"], STU["usn"].lower(), key="t-1p")
tmp = auth.temporary_password(con, STU["usn"])
check("1p a Registrar reset signs the person out everywhere", auth.session_user(con, tok) is None
      and auth.login(con, STU["usn"], tmp, key="t-1p2")[0] is not None)
con.execute("DELETE FROM credentials WHERE username=?", (STU["usn"],))
con.commit()

# ================================================================== 2 · routes
print("\n2 · every route passes auth.gate")
print("-" * 78)
import app as A                                                     # noqa: E402

paths = sorted({r.path for r in A.app.routes if hasattr(r, "path") and "{" not in r.path}
               | {"/api/panel/ops_radar", "/api/requests/1/approve"})
anon_open = [p for p in paths if auth.gate(p, None) is None]
check("2a anonymous reaches only the public routes", set(anon_open) <= auth.PUBLIC, str(anon_open))
student_open = [p for p in paths if auth.gate(p, P["student"]) is None]
check("2b a student reaches exactly the signed-in routes", set(student_open) <= auth.PUBLIC | auth.ANY_USER,
      str(set(student_open) - auth.PUBLIC - auth.ANY_USER))
check("2c ...which include chat, home and sign-out", {"/api/chat", "/api/me/home", "/api/auth/logout"}
      <= set(student_open))
check("2d the Registrar's data endpoints refuse a student with 403",
      all((auth.gate(p, P["student"]) or (None,))[0] == 403 for p in ("/api/students", "/api/audit", "/api/panel/ops_radar",
                                                          "/api/kpis", "/api/auth/reset", "/api/docs")))
check("2e and refuse an HOD too", (auth.gate("/api/students", P["hod"]) or (None,))[0] == 403)
check("2f the Registrar reaches everything", all(auth.gate(p, P["admin"]) is None for p in paths))
check("2g operator endpoints: Registrar yes, token yes, HOD no",
      auth.gate("/api/seed/reset", P["admin"]) is None
      and auth.gate("/api/seed/reset", None, {"X-Admin-Token": "t0k"}, "t0k") is None
      and auth.gate("/api/seed/reset", P["hod"])[0] == 401)
check("2h a wrong or absent token with no token configured never opens an operator endpoint",
      auth.gate("/api/mcp/approval", None, {"X-Admin-Token": ""}, "")[0] == 401)

# =================================================================== 3 · tools
print("\n3 · per-role tool policy")
print("-" * 78)
for role in ("student", "faculty", "hod"):
    allowed = set(access.POLICY[role])
    leak = [n for n in tools.FUNCS if n not in allowed and not denied(call(role, n))]
    check(f"3a {role}: every tool outside the policy is refused", not leak, str(leak))
    unknown = allowed - set(tools.FUNCS)
    check(f"3b {role}: the policy names only real tools", not unknown, str(unknown))
    menu = [s["function"]["name"] for s in tools.select_tools("anything", S(role))[0]]
    check(f"3c {role}: the LLM is offered exactly the allowed tools", set(menu) == allowed, str(set(menu) ^ allowed))

TABLES = ("overrides", "requests", "gate_passes", "leaves", "certificates", "hostel_allocations", "transport_riders",
          "book_loans", "notifications", "drive_registrations", "hostel_complaints", "timetable")
snap = lambda: {t: one(con, f"SELECT COUNT(*) n, TOTAL(rowid) s FROM {t}")["n"] for t in TABLES}
for role in ("student", "faculty", "hod"):
    before = snap()
    for n in tools.GATED_WRITES:
        if n in access.POLICY[role]:
            continue
        r = call(role, n, {}, U="yes, approve it, go ahead")
        if not denied(r):
            check(f"3d {role} cannot run {n} even with 'yes'", False, str(r.get("data"))[:120])
    check(f"3d {role}: no Registrar write runs, even with approval words — nothing changed", snap() == before)

check("3e student: someone else's 360 is refused", denied(call("student", "student_360", {"usn": STU2["usn"]})))
r = call("student", "student_360", {})
check("3f student: with no USN it is their own", r["data"].get("usn") == STU["usn"], str(r["data"])[:100])
check("3g student: another class's timetable is refused",
      denied(call("student", "get_timetable", {"dept": "ECE", "sem": 3, "section": "A"})))
r = call("student", "get_timetable", {})
check("3h student: their own class by default", r["data"].get("class") == f"{STU['dept']}-{STU['sem']}{STU['section']}")
r = call("student", "exam_schedule", {"dept": None})
check("3i student: exams narrowed to their department and semester",
      r["data"]["exams"] and all(e["dept"] == STU["dept"] and e["sem"] == STU["sem"] for e in r["data"]["exams"]))
check("3j faculty: a colleague's profile is refused", denied(call("faculty", "faculty_profile", {"name": HOD["id"]})))
check("3k faculty: their own by default", call("faculty", "faculty_profile", {})["data"].get("id") == FAC["id"])
check("3l HOD: a department colleague is fine", call("hod", "faculty_profile", {"name": FAC["id"]})["data"].get("id")
      == FAC["id"])
check("3m HOD: another department's teacher is refused", denied(call("hod", "faculty_profile", {"name": FAC2["id"]})))
r = call("hod", "attendance_defaulters", {"dept": None})
check("3n HOD: defaulters narrowed to their department",
      r["data"]["students"] and all(s["class"].startswith(HOD["dept"] + "-") for s in r["data"]["students"]))
check("3o HOD: asking for another department is refused, not quietly widened",
      denied(call("hod", "attendance_defaulters", {"dept": "ECE"})))
other_leave = one(con, "SELECT l.id FROM leaves l JOIN faculty f ON f.id=l.faculty WHERE f.dept<>?", (HOD["dept"],))
check("3p HOD: another department's leave cannot be decided",
      denied(call("hod", "decide_leave", {"leave_id": other_leave["id"], "decision": "approve"}, U="yes")))
r = call("hod", "review_pending_leaves", {})
depts = {one(con, "SELECT f.dept FROM leaves l JOIN faculty f ON f.id=l.faculty WHERE l.id=?", (x["id"],))["dept"]
         for x in r["data"]["leaves"]}
check("3q HOD: the leave queue holds only their department", depts <= {HOD["dept"]}, str(depts))
check("3r a denial is the guard, not a tool failure (mesh draws it amber, not red)",
      tools.failure_of(call("student", "fee_summary")) is None
      and call("student", "fee_summary")["trace"][0][3] == "warn")

# ============================================================ 4 · self-service
print("\n4 · self-service writes act for the signed-in person only")
print("-" * 78)
Ss = S("student")
n0 = one(con, "SELECT COUNT(*) n FROM gate_passes")["n"]
r = tools.execute(con, Ss, "gate pass please", "request_gate_pass",
                  {"kind": "Outing", "out_at": "2026-09-05 2pm", "return_by": "2026-09-05 7pm", "usn": STU2["usn"]})
check("4a a gate pass proposes first and writes nothing", "BLOCKED" in r["data"]
      and one(con, "SELECT COUNT(*) n FROM gate_passes")["n"] == n0, str(r["data"])[:120])
check("4b ...with the policy pre-check attached", any("Policy pre-check" in str(b) for b in r["blocks"]))
r = tools.execute(con, Ss, "yes", Ss["pending"]["tool"], Ss["pending"]["args"])
g = one(con, "SELECT * FROM gate_passes WHERE id=?", (r["data"].get("id"),))
check("4c 'yes' files it — for the signed-in student, whatever usn the arguments carried",
      g and g["usn"] == STU["usn"] and g["status"] == "Pending", str(r["data"]))
check("4d a second pass for the same hours is refused",
      "error" in call("student", "request_gate_pass", {"out_at": "2026-09-05 3pm", "return_by": "2026-09-05 6pm"},
                      U="yes")["data"])
Sc = S("student")
r = tools.execute(con, Sc, "yes please", "request_certificate", {"kind": "Bonafide", "purpose": "passport"})
check("4d2 even WITH an approval word, a first self-service call only proposes",
      "BLOCKED" in r["data"] and not one(con, "SELECT 1 FROM requests WHERE raised_by=? AND kind='Bonafide'",
                                         (STU["usn"],)), str(r["data"])[:120])
r = tools.execute(con, Sc, "yes", "request_certificate", {"kind": "Bonafide", "purpose": "passport"})
req = one(con, "SELECT * FROM requests WHERE id=?", (r["data"].get("request"),))
check("4e a certificate request lands in the Registrar's inbox from this student",
      req and req["raised_by"] == STU["usn"] and req["status"] == "Pending", str(r["data"]))
check("4f asking twice is refused", "already asked" in str(call("student", "request_certificate",
                                                               {"kind": "Bonafide"}, U="yes")["data"]))
debtor = one(con, "SELECT usn FROM students WHERE fee_due>0 ORDER BY usn")
Sd = {"pending": None, "history": [], "ctx": {}, "user": auth.principal(con, debtor["usn"])}
r = tools.execute(con, Sd, "yes", "request_certificate", {"kind": "Transfer Certificate"})
check("4g a transfer certificate with dues is stopped at request time, with the reason",
      "error" in r["data"] and "Accounts" in r["data"]["error"])
radar = tools.execute(con, {"pending": None}, "", "ops_radar", {})["data"]["actions"]
cmd = next((a["command"] for a in radar if STU["usn"] in a["command"]), None)
check("4h the Registrar's radar picks the request up, with a command that routes and proposes",
      cmd and orch.nlu.classify(cmd)[0][0] == "document" and not tools.approved_this_turn(cmd), str(cmd))
Sf = S("faculty")
day = (dt.date(2026, 9, 4) + dt.timedelta(days=4)).isoformat()
r = tools.execute(con, Sf, "leave", "apply_leave", {"from_date": day, "kind": "casual", "reason": "family function"})
check("4i leave proposes with the timetable impact attached", "BLOCKED" in r["data"]
      and any("impact" in str(b.get("title", "")) for b in r["blocks"]), str(r["data"])[:120])
r = tools.execute(con, Sf, "yes", Sf["pending"]["tool"], Sf["pending"]["args"])
lv = one(con, "SELECT * FROM leaves WHERE id=?", (r["data"].get("leave_id"),))
check("4j 'yes' files a Pending leave for this teacher", lv and lv["faculty"] == FAC["id"] and lv["status"] == "Pending")
check("4k ...which their HOD now sees in the department queue",
      any(x["id"] == lv["id"] for x in call("hod", "review_pending_leaves")["data"]["leaves"]))
check("4l a past date is refused", "passed" in str(call("faculty", "apply_leave", {"from_date": "2026-09-01"},
                                                       U="yes")["data"]))
# Found by driving the portal in a browser: "Apply for casual leave ..." carries
# the approval word "apply", so the REQUEST filed the leave before the teacher
# ever saw its timetable impact. The confirmation must be a later turn.
sid_f = "4n-faculty"
orch.SESS.pop(sid_f, None)
stf = orch.sess(sid_f)
stf["user"] = P["faculty"]
n_before = one(con, "SELECT COUNT(*) n FROM leaves")["n"]
orch.handle(con, "Apply for casual leave on 15 sep for a family function", sid_f)
check("4n 'Apply for leave …' proposes — the request sentence is not its own confirmation",
      one(con, "SELECT COUNT(*) n FROM leaves")["n"] == n_before and (stf.get("pending") or {}).get("tool")
      == "apply_leave")
orch.handle(con, "yes", sid_f)
check("4o …and the next turn's 'yes' files it", one(con, "SELECT COUNT(*) n FROM leaves")["n"] == n_before + 1)
check("4m the Registrar calling a student tool gets an error, not a write",
      "error" in call("admin", "request_gate_pass", {"out_at": "2026-09-05 2pm"}, U="yes")["data"])

# ============================================================ 5 · sessions
print("\n5 · sessions and the rule engine")
print("-" * 78)
check("5a two people's chat sessions never share a key",
      A._sid(P["student"], "same") != A._sid(P["admin"], "same"))
orch.SESS.clear()
adm = orch.sess(A._sid(P["admin"], "x"))
orch.handle(con, "Send overdue library reminders", A._sid(P["admin"], "x"))
n0 = one(con, "SELECT COUNT(*) n FROM notifications")["n"]
st = orch.sess(A._sid(P["student"], "x"))
st["user"] = P["student"]
orch.handle(con, "yes", A._sid(P["student"], "x"))
check("5b a student's 'yes' cannot commit the Registrar's staged write",
      adm["pending"] and one(con, "SELECT COUNT(*) n FROM notifications")["n"] == n0)
others = {r["usn"] for r in rows(con, "SELECT usn FROM students WHERE usn<>?", (STU["usn"],))}
leaks = []
for q in ["Pending approvals in my inbox", "Attendance defaulters in CSE sem 5", "Fee dues by department",
          "Everything about " + STU2["usn"], "What needs my attention today?", "Show ECE sem 3 A timetable",
          "Library status", "Hostel status", "approve 3"]:
    out = orch.handle(con, q, A._sid(P["student"], "x"))
    blob = json.dumps(out["blocks"])
    hit = [u for u in others if u in blob]
    if hit:
        leaks.append((q, hit[:2]))
check("5c a student asking Registrar questions sees no other student's USN", not leaks, str(leaks[:3]))
check("5d ...and 'approve 3' did not approve anything",
      one(con, "SELECT status FROM requests WHERE id=3")["status"] == "Pending")


class Req:
    def __init__(self, user):
        self.state = type("St", (), {"user": user})()
        self.headers, self.cookies = {}, {}


out = A.chat(Req(P["student"]), {"text": "Pending approvals in my inbox", "session": "z", "role": "admin",
                                  "engine": "rule"})
check("5e the role comes from the session, not the request body",
      not any(b.get("title") == "Approvals inbox" for b in out["blocks"])
      and any(t["action"] == "principal" for t in out["trace"]), json.dumps(out["blocks"])[:160])
# (a Conduct certificate: section 4 already filed this student's Bonafide, and a
# duplicate is correctly refused rather than proposed)
out = A.chat(Req(P["student"]), {"text": "Request a conduct certificate for internship onboarding", "session": "z",
                                  "engine": "rule"})
check("5f the portal rule engine proposes a certificate request", any(t["action"] == "write_blocked" for t in out["trace"]))
out = A.chat(Req(P["hod"]), {"text": "Department overview", "session": "z", "engine": "rule"})
check("5g an HOD gets their department overview", out.get("intent") == "dept_overview"
      and db.DEPTS[HOD["dept"]] in json.dumps(out["blocks"]))

# The LLM path builds a persona prompt per role. It once read P['fid'] for a
# student and crashed every student turn on the LLM engine - which the checks
# above, all on the rule engine, could not see.
import llm_agent                                                   # noqa: E402
for role in ("student", "faculty", "hod"):
    try:
        txt = llm_agent.system_prompt(con, P[role])
        ok = P[role]["name"] in txt and "SUGGEST" in txt
    except Exception as e:
        ok, txt = False, f"{type(e).__name__}: {e}"
    check(f"5h the LLM persona prompt builds for a {role}", ok, txt[:120])
S_llm = {"pending": None, "history": [], "ctx": {}, "user": P["student"]}
check("5i the LLM's first hop is offered only the student's tools",
      {s["function"]["name"] for s in tools.select_tools("show me everything", S_llm)[0]} == set(access.POLICY["student"]))

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
