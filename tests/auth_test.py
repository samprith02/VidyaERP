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
import re
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


# --------------------------- 6 · request decisions: one path, the ceiling enforced (#70, #71)
print("\n6 · every decision path applies the Registrar's delegation and refuses a decided request")
print("-" * 78)
from agents import PolicyGuard                                     # noqa: E402

CAP = PolicyGuard.APPROVAL_CEILING


def status(rid):
    return one(con, "SELECT status FROM requests WHERE id=?", (rid,))["status"]


def new_request(amount, title):
    return con.execute("""INSERT INTO requests(kind,raised_by,role,target,title,details,amount,status,priority,
                          created_at,sla_hrs) VALUES('Infrastructure','HOD-CSE','HOD','Principal',?,'',?,'Pending',
                          'Medium','2026-09-03',48)""", (title, amount)).lastrowid


big = new_request(CAP + 700_000, "AV system above the delegation")
con.commit()
before = {r["id"]: r["status"] for r in rows(con, "SELECT id, status FROM requests")}
out = orch.handle(con, "approve all under 1 lakh", "dec1", "admin", "registrar")
after = {r["id"]: r["status"] for r in rows(con, "SELECT id, status FROM requests")}
changed = {k: after[k] for k in after if after[k] != before[k]}
# Defect (#70): the "1" of "1 lakh" was read as a request id, and REQ-0001 - a
# medical leave - was approved on its own.
check("6a 'approve all under 1 lakh' is a bulk approval, not REQ-0001",
      1 not in changed or one(con, "SELECT amount FROM requests WHERE id=1")["amount"] <= 100_000
      and len(changed) > 1 and all(v == "Approved" for v in changed.values()), str(changed))
check("6b ...and approves only requests at or under ₹1,00,000",
      all(one(con, "SELECT amount FROM requests WHERE id=?", (k,))["amount"] <= 100_000 for k in changed), str(changed))
out = orch.handle(con, "approve all under 5000", "dec0", "admin", "registrar")
check("6b2 a bare rupee figure is a cap too: 'approve all under 5000' is bulk, not REQ-5000",
      "Bulk-approved" in json.dumps(out["blocks"]), json.dumps(out["blocks"])[:140])
out = orch.handle(con, "approve all under 50 crore", "dec2", "admin", "registrar")
# Defect (#71): the ceiling was printed in the trace and never compared.
check("6c a bulk cap above the delegation is clamped to it: the ₹12 L request stays pending", status(big) == "Pending")
check("6c2 ...and the answer says so, rather than calling the asked-for cap respected",
      "stays for the Principal" in json.dumps(out["blocks"])
      and any(t["agent"] == "PolicyGuard" and t["status"] == "warn" for t in out["trace"]), json.dumps(out["blocks"])[:200])
bd = next((t["detail"] for t in out["trace"] if t["action"] == "bulk_decision"), "")
nums = [int(x) for x in re.findall(r"\d+", bd)[:2]]
check("6c3 ...because the clamped cap never selects a request the delegation would refuse",
      len(nums) == 2 and nums[0] == nums[1], bd)
out = orch.handle(con, f"approve {big}", "dec3", "admin", "registrar")
check("6d the rule engine refuses to approve it - a guard warning, not a failure",
      status(big) == "Pending" and any(t["agent"] == "PolicyGuard" and t["status"] == "warn" for t in out["trace"])
      and not any(t["status"] == "error" for t in out["trace"]), json.dumps(out["trace"])[-240:])
r = call("admin", "decide_request", {"request_id": big, "decision": "approve"}, U="yes, approve it")
check("6e so does the tool the LLM and MCP use - DENIED, and failure_of sees no failure",
      status(big) == "Pending" and "DENIED" in r["data"] and tools.failure_of(r) is None, str(r["data"])[:140])
bad = A.decide(big, "approve", Req(P["admin"]))
check("6f and so do the inbox buttons", status(big) == "Pending" and getattr(bad, "status_code", 200) == 403)
check("6g a rejection needs no delegation", A.decide(big, "reject", Req(P["admin"])) == {"ok": True, "status": "Rejected"})
again = A.decide(big, "approve", Req(P["admin"]))
check("6h a decided request is not decided again", status(big) == "Rejected" and getattr(again, "status_code", 200) == 409)
small = new_request(1000, "probe")
con.commit()
check("6i an unknown decision word is refused, not taken as reject",
      getattr(A.decide(small, "maybe", Req(P["admin"])), "status_code", 200) == 400 and status(small) == "Pending")
check("6j a request that does not exist is not 'ok'", getattr(A.decide(99999, "reject", Req(P["admin"])),
                                                              "status_code", 200) == 409)
led = one(con, "SELECT * FROM audit WHERE action='request.decide' ORDER BY id DESC LIMIT 1")
check("6k every decision is audited old -> new under the signed-in account",
      led and led["actor"] == "registrar" and json.loads(led["payload"])["now"] == "Rejected"
      and json.loads(led["payload"])["id"] == big, str(led and dict(led)))


# ------------------------------------------------ 7 · library loans, by role (#91)
print("\n7 · whose library loans each role may read")
print("-" * 78)
CSE_STU = one(con, """SELECT usn FROM students WHERE dept='CSE' AND usn<>? AND usn IN
                      (SELECT member FROM book_loans WHERE returned_on IS NULL) ORDER BY usn""", (STU["usn"],))["usn"]
r = call("student", "library_loans", {})
check("7a a student reads their own loans by default", r["data"].get("member") == STU["usn"], str(r["data"])[:120])
check("7b ...'me' is them too", call("student", "library_loans", {"member": "me"})["data"].get("member") == STU["usn"])
check("7c ...and another student's loans are refused", denied(call("student", "library_loans", {"member": CSE_STU})))
check("7d a teacher reads their own", call("faculty", "library_loans", {})["data"].get("member") == FAC["id"])
check("7e ...but not a student's", denied(call("faculty", "library_loans", {"member": CSE_STU})))
check("7f a HOD reads a student in the department", call("hod", "library_loans", {"member": CSE_STU})["data"]
      .get("member") == CSE_STU)
check("7g ...and a colleague in it", call("hod", "library_loans", {"member": FAC["id"]})["data"].get("member") == FAC["id"])
check("7h ...but not another department's student or teacher",
      denied(call("hod", "library_loans", {"member": STU2["usn"]}))
      and denied(call("hod", "library_loans", {"member": FAC2["id"]})))
sid = A._sid(P["hod"], "loans")
orch.SESS.pop(sid, None)
orch.sess(sid)["user"] = P["hod"]
out = orch.handle(con, f"{CSE_STU} can you tell me which book he has borrowed", sid)
text = json.dumps(out["blocks"]).lower()
check("7i the reported case: the HOD gets the loans, not the 360",
      out.get("intent") == "library_loans" and "on loan" in text and "cgpa" not in text and "attendance" not in text,
      f"{out.get('intent')} {text[:120]}")
out = orch.handle(con, f"show me the details of {CSE_STU}", sid)
check("7j ...and 'details of' still gives the HOD the full profile", out.get("intent") == "student_360")
out = orch.handle(con, "which book has she borrowed?", sid)
check("7k ...and a follow-up's pronoun is that student",
      out.get("intent") == "library_loans" and CSE_STU in json.dumps(out["blocks"]))
orch.SESS.pop(sid, None)
sid = A._sid(P["student"], "loans")
orch.SESS.pop(sid, None)
orch.sess(sid)["user"] = P["student"]
out = orch.handle(con, "which books have I borrowed?", sid)
check("7l a student asking about their own books gets their loans",
      out.get("intent") == "library_loans" and STU["usn"] in json.dumps(out["blocks"]), str(out.get("intent")))
orch.SESS.pop(sid, None)
check("7m the LLM menu for every role includes it",
      all("library_loans" in access.POLICY[r] for r in ("student", "faculty", "hod")))


# ------------------------------------------- 8 · what a user writes is shown as text (#85)
print("\n8 · the pages escape what users can write")
print("-" * 78)
# Defect found 2026-10-02: a student's certificate purpose became a request
# title, and the Registrar's inbox put it into innerHTML raw. Through the LLM
# engine a student could run script in the Registrar's session, which is
# enough to "approve" gated writes as them. Escaping is checked by reading the
# pages: any record field that can carry a person's words, interpolated bare
# as ${x.title} or ${x.title||''}, fails here.
import re                                                          # noqa: E402

USER_TEXT = ("title", "details", "reason", "body", "audience", "channel", "purpose", "note", "payload",
             "actor", "action", "created_by", "raised_by", "target", "kind", "name", "email", "status",
             "outcome", "orig_name", "new_name", "mentor_name", "designation", "plan_ref", "error")
BARE = re.compile(r"\$\{\s*[A-Za-z_]\w{0,3}\.(" + "|".join(USER_TEXT) + r")\s*(?:\|\|\s*'[^']*'\s*)?\}")


def bare_fields(page):
    with open(os.path.join(HERE, "static", page), encoding="utf-8") as f:
        return [(i + 1, m.group(0)) for i, line in enumerate(f) for m in BARE.finditer(line)]


check("8a the console interpolates no user-writable field without esc()", not bare_fields("index.html"),
      str(bare_fields("index.html")[:6]))
check("8b ...and neither does the portal", not bare_fields("portal.html"), str(bare_fields("portal.html")[:6]))
# the calendar's shared renderer (#104) draws titles, venues and descriptions the Registrar typed
check("8e ...nor the calendar renderer the two pages share", not bare_fields("calendar.js"),
      str(bare_fields("calendar.js")[:6]))
inbox = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
check("8c the inbox escapes the request title and details, the fields #85 used",
      "<b>${esc(x.title)}</b>" in inbox and "${esc(x.details)}" in inbox)
check("8d the scan itself finds a bare field (it is not vacuous)",
      len(BARE.findall("<b>${x.title}</b> ${l.reason||''} ${esc(x.body)}")) == 2)


print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
