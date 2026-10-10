"""The teaching desk (#116, #117): a teacher's day is read from the timetable
and that day's approved changes, attendance is taken one class hour at a time
by the teacher delivering it and rolls into the totals the student dashboard
reads, and a submitted marks register is locked. No server, no API cost.

    python tests/teaching_test.py
"""
import os
import re
import sys
import tempfile
import datetime as dt
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_teaching_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import academics as A                                              # noqa: E402
import auth                                                        # noqa: E402
import guard                                                       # noqa: E402
import orchestrator                                                # noqa: E402
import portal                                                      # noqa: E402
import teaching as T                                               # noqa: E402
import tools                                                       # noqa: E402
from agents import TimetableAgent, one, rows                       # noqa: E402
from nlu import TODAY                                              # noqa: E402

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
THU, WED, FRI = "2026-09-03", "2026-09-02", TODAY.isoformat()


def S(user=None):
    return {"pending": None, "history": [], "ctx": {}, "user": user}


def call(name, args, user=None, U=""):
    return tools.execute(con, S(user), U, name, args)


def who(pid):
    return auth.principal(con, pid)


def refused(r):
    d = r.get("data") or {}
    return bool(d.get("error") or d.get("DENIED"))


def snap():
    T.ensure(con)
    return ([tuple(r) for r in con.execute("SELECT * FROM attendance_sessions ORDER BY id")],
            [tuple(r) for r in con.execute("SELECT * FROM attendance_marks ORDER BY session_id, usn")],
            [tuple(r) for r in con.execute("SELECT usn, subject, held, attended FROM attendance ORDER BY id")],
            [tuple(r) for r in con.execute("SELECT usn, attendance FROM students ORDER BY usn")])


def class_usns(dept, sem, section):
    return [r["usn"] for r in rows(con, "SELECT usn FROM students WHERE dept=? AND sem=? AND section=? ORDER BY usn",
                                   (dept, sem, section))]


def total(usn, subject):
    r = one(con, "SELECT held, attended FROM attendance WHERE usn=? AND subject=?", (usn, subject))
    return (r["held"], r["attended"]) if r else (0, 0)


def staged_commit(user, args, word="yes"):
    """A teacher's proposal, then their confirmation in a later turn."""
    s = S(user)
    first = tools.execute(con, s, word, "take_attendance", args)
    second = tools.execute(con, s, word, "take_attendance", (s.get("pending") or {}).get("args") or args)
    return first, second


F002, HOD = who("F002"), who("F001")
STU = who(one(con, "SELECT usn FROM students WHERE dept='CSE' AND sem=3 AND section='A' ORDER BY usn")["usn"])

# ------------------------------------------------------------------ 1 · the day is read from the timetable
print("\n1 · a teacher's day comes from the timetable and that day's approved changes")
print("-" * 78)
r = call("teaching_day", {"date": THU}, F002)
weekly = rows(con, "SELECT period, dept, sem, section, subject FROM timetable WHERE faculty='F002' AND day='Thu'")
got = {(c["period"], c["dept"], c["sem"], c["section"], c["subject"]) for c in r["data"]["classes"]}
check("1a Thursday lists exactly F002's Thursday rows in the timetable",
      got == {(w["period"], w["dept"], w["sem"], w["section"], w["subject"]) for w in weekly}, str(got))
check("1b every class names its semester, department and section as the timetable has them",
      all(c["cls"] == f"{c['dept']}-{c['sem']}{c['section']}" for c in r["data"]["classes"]))
r = call("teaching_day", {}, HOD)
mine = rows(con, "SELECT period FROM timetable WHERE faculty='F001' AND day=?", (TODAY.strftime("%a"),))
check("1c an HOD's day is only the classes they teach, not the department's",
      sorted(c["period"] for c in r["data"]["classes"]) == sorted(m["period"] for m in mine), str(r["data"]["classes"])[:120])
live = [c for c in r["data"]["classes"] if c["status"] not in ("Cancelled", "Covered")]
check("1d today's statuses are read against the demo clock (campus.NOW), the same rule the student dashboard uses",
      [c["status"] for c in live] == portal.class_status([c["period"] for c in live]), str([c["status"] for c in live]))
r = call("teaching_day", {"date": "2026-10-02"}, HOD)
check("1e on a fixed holiday every class is shown as Holiday and none can be taken",
      r["data"]["holiday"] == "Gandhi Jayanti" and all(c["status"] == "Holiday" and not c["attendance"]["can_take"]
                                                         for c in r["data"]["classes"]), str(r["data"]["classes"])[:120])
fri = call("teaching_day", {}, F002)["data"]["classes"]
act = next((c for c in fri if c["kind"] == "A"), None)
check("1f an activity hour (placement training) keeps no register", act is not None and not act["attendance"]["can_take"]
      and "No register" in act["attendance"]["label"], str(act))
r = call("take_attendance", {"date": FRI, "period": act["period"], "dept": act["dept"], "sem": act["sem"],
                             "section": act["section"], "absent": "none"})
check("1g ... and taking it is refused as such", refused(r) and "activity" in r["data"]["error"], str(r["data"]))

# a substitution: plan an absence for today and apply it as the Registrar
p1 = one(con, """SELECT t.faculty, t.dept, t.sem, t.section, t.subject FROM timetable t JOIN subjects s ON s.code=t.subject
                 JOIN faculty f ON f.id=t.faculty WHERE t.day=? AND t.period=1 AND s.kind='T' AND f.is_hod=0
                 ORDER BY t.faculty LIMIT 1""", (TODAY.strftime("%a"),))
adm = S()
tools.execute(con, adm, "who covers?", "plan_absence_coverage", {"faculty_name": p1["faculty"], "date": FRI})
tools.execute(con, adm, "yes, apply plan A", "apply_coverage_plan", {"plan_code": "A"})
hour = {"date": FRI, "period": 1, "dept": p1["dept"], "sem": p1["sem"], "section": p1["section"]}
res = T.resolve(con, TODAY, p1["dept"], p1["sem"], p1["section"], 1)
sub = res["faculty"] if res and res.get("source") in ("substitute", "swap") else None
check("1h an applied plan hands P1 to another teacher for the day", sub and sub != p1["faculty"], str(res))
if sub:
    theirs = call("teaching_day", {}, who(sub))["data"]["classes"]
    check("1i ... the substitute's day shows the hour, marked as a substitution",
          any(c["period"] == 1 and c["cls"] == res["cls"] and c["source"] in ("substitute", "swap") for c in theirs))
    absent_day = call("teaching_day", {}, who(p1["faculty"]))["data"]["classes"]
    check("1j ... the absent teacher's day shows it covered, with nothing to take",
          any(c["period"] == 1 and c["status"] == "Covered" and not c["attendance"]["can_take"] for c in absent_day))
    r = call("attendance_sheet", hour, who(p1["faculty"]))
    check("1k ... and the absent teacher is refused its register (DENIED, not a failure)",
          "DENIED" in r["data"] and tools.failure_of(r) is None, str(r["data"])[:120])
    before = snap()
    a, b = staged_commit(who(sub), {**hour, "absent": "none"})
    check("1l ... while the substitute takes it, and the session records who delivered it",
          b["data"].get("recorded") is True and one(con, "SELECT faculty FROM attendance_sessions WHERE date=? AND "
                                                    "period=1 AND dept=? AND sem=? AND section=?",
                                                    (FRI, p1["dept"], p1["sem"], p1["section"]))["faculty"] == sub,
          str(b["data"])[:140])

# ------------------------------------------------------------------ 2 · who may
print("\n2 · only the teacher delivering a class hour may take it")
print("-" * 78)
oth = one(con, "SELECT period, dept, sem, section FROM timetable WHERE dept='CSE' AND day='Thu' AND faculty<>'F002' "
               "AND faculty IS NOT NULL AND subject IN (SELECT code FROM subjects WHERE kind='T') LIMIT 1")
oh = {"date": THU, "period": oth["period"], "dept": "CSE", "sem": oth["sem"], "section": oth["section"], "absent": "none"}
before = snap()
a, b = staged_commit(F002, oh)
check("2a a teacher is refused another teacher's class, and nothing is written",
      "DENIED" in a["data"] and "DENIED" in b["data"] and snap() == before, str(a["data"])[:120])
r = call("teaching_day", {"faculty": "F003"}, F002)
check("2b a teacher cannot open another teacher's day", "DENIED" in r["data"], str(r["data"])[:100])
r = call("take_attendance", {**oh, "faculty": oth and one(con, "SELECT faculty FROM timetable WHERE dept='CSE' AND "
                                                         "day='Thu' AND period=? AND sem=? AND section=?",
                                                         (oth["period"], oth["sem"], oth["section"]))["faculty"]},
         F002, "yes")
check("2c naming the real teacher in the arguments does not help: access.py refuses it", "DENIED" in r["data"])
for name in ("take_attendance", "attendance_sheet", "teaching_day", "cie_sheet", "submit_cie_marks"):
    r = call(name, {**oh}, STU, "yes")
    check(f"2d a student cannot call {name}", "DENIED" in r["data"], str(r["data"])[:100])
hod_thu = rows(con, "SELECT period, dept, sem, section FROM timetable WHERE faculty='F001' AND day='Thu' ORDER BY period")
dept_not_hod = one(con, "SELECT period, sem, section FROM timetable WHERE dept='CSE' AND day='Thu' AND faculty<>'F001' "
                        "AND subject IN (SELECT code FROM subjects WHERE kind='T') ORDER BY period LIMIT 1")
r = call("attendance_sheet", {"date": THU, "period": dept_not_hod["period"], "dept": "CSE", "sem": dept_not_hod["sem"],
                              "section": dept_not_hod["section"]}, HOD)
check("2e the HOD role opens no class of the department the HOD does not teach", "DENIED" in r["data"],
      str(r["data"])[:100])
h = hod_thu[0]
a, b = staged_commit(HOD, {"date": THU, "period": h["period"], "dept": h["dept"], "sem": h["sem"],
                           "section": h["section"], "absent": "none"})
check("2f ... but the HOD takes attendance for a class they teach, as a teacher would",
      "BLOCKED" in a["data"] and b["data"].get("recorded") is True, str(b["data"])[:120])

import app as APP                                                  # noqa: E402


def req(user, **query):
    return SimpleNamespace(state=SimpleNamespace(user=user), query_params=query, headers={}, cookies={})


r = APP.my_read(req(STU, tool="take_attendance"), tool="take_attendance")
check("2g /api/me/read serves only the listed read views", getattr(r, "status_code", 200) == 404)
try:
    APP.my_propose(req(STU), {"tool": "apply_coverage_plan", "args": {}, "session": "t"})
    ok = False
except APP.BadInput:
    ok = True
check("2h /api/me/propose stages only the listed form writes", ok)
r = APP.my_propose(req(STU), {"tool": "take_attendance", "args": oh, "session": "t"})
check("2i a student reaching the form endpoint is still refused by the tool policy",
      "DENIED" in r["data"] and not r["staged"], str(r["data"])[:100])
check("2j both portal routes are open to a signed-in person, so the tool policy is what decides",
      auth.gate("/api/me/read", STU) is None and auth.gate("/api/me/propose", STU) is None
      and auth.gate("/api/me/read", None)[0] == 401)

# ------------------------------------------------------------------ 3 · the rules
print("\n3 · never a future class, not before it begins, not after the window")
print("-" * 78)
wed = rows(con, "SELECT period, dept, sem, section, subject FROM timetable WHERE faculty='F002' AND day='Wed' "
                "AND subject IN (SELECT code FROM subjects WHERE kind='T') ORDER BY period")
w0 = {"dept": wed[0]["dept"], "sem": wed[0]["sem"], "section": wed[0]["section"], "period": wed[0]["period"]}
before = snap()
r = call("take_attendance", {**w0, "date": "2026-09-09", "absent": "none"}, F002, "yes")
check("3a a future date is refused, nothing written", refused(r) and "has not come yet" in str(r["data"])
      and snap() == before, str(r["data"])[:120])
later = next(c for c in call("teaching_day", {}, HOD)["data"]["classes"] if c["status"] in ("Next", "Later"))
r = call("take_attendance", {"date": FRI, "period": later["period"], "dept": later["dept"], "sem": later["sem"],
                             "section": later["section"], "absent": "none"}, HOD, "yes")
check("3b today's class that has not begun yet is refused, with the time it opens",
      refused(r) and "begins at" in r["data"]["error"], str(r["data"])[:120])
r = call("take_attendance", {**w0, "date": "2026-08-26", "absent": "none"}, F002, "yes")
check("3c a class outside the correction window is closed", refused(r) and "closed" in r["data"]["error"],
      str(r["data"])[:120])
usns = class_usns(w0["dept"], w0["sem"], w0["section"])
far = one(con, "SELECT usn FROM students WHERE NOT (dept=? AND sem=? AND section=?) LIMIT 1",
          (w0["dept"], w0["sem"], w0["section"]))["usn"]
r = call("take_attendance", {**w0, "date": WED, "absent": f"{usns[0]}, {far}"}, F002, "yes")
check("3d a USN from another class is refused, by name", refused(r) and far in r["data"]["error"], str(r["data"])[:120])
r = call("take_attendance", {**w0, "date": WED, "absent": f"{usns[0]}, {usns[0]}"}, F002, "yes")
check("3e the same USN twice is refused", refused(r) and "twice" in r["data"]["error"])
r = call("take_attendance", {**w0, "date": WED, "absent": ""}, F002, "yes")
check("3f an empty absentee list is a question, not 'everyone present'", refused(r) and "none" in r["data"]["error"])
r = call("take_attendance", {**w0, "date": WED, "section": "Z", "absent": "none"}, F002, "yes")
check("3g a section that does not run is refused", refused(r) and "No section" in r["data"]["error"])
check("3h nothing was written by any of the refusals", snap() == before)

# ------------------------------------------------------------------ 4 · the write
print("\n4 · proposed, confirmed, written and re-read")
print("-" * 78)
args = {**w0, "date": WED, "absent": f"{usns[1]}, {usns[2]}"}
subj = wed[0]["subject"]
t_before = {u: total(u, subj) for u in usns}
head_before = {r["usn"]: r["attendance"] for r in rows(con, "SELECT usn, attendance FROM students WHERE usn IN (%s)"
                                                       % ",".join("?" * len(usns)), tuple(usns))}
s = S(F002)
before = snap()
r = tools.execute(con, s, "yes", "take_attendance", args)
check("4a a teacher's first message only proposes, even when it says yes", "BLOCKED" in r["data"] and snap() == before
      and s["pending"]["tool"] == "take_attendance", str(r["data"])[:120])
r = tools.execute(con, s, "yes", "take_attendance", s["pending"]["args"])
sess = one(con, "SELECT * FROM attendance_sessions WHERE date=? AND period=? AND dept=? AND sem=? AND section=?",
           (WED, w0["period"], w0["dept"], w0["sem"], w0["section"]))
check("4b the confirmation records the hour: one session, a mark for every student",
      r["data"].get("recorded") is True and sess and sess["present"] == len(usns) - 2 and sess["absent"] == 2
      and one(con, "SELECT COUNT(*) n FROM attendance_marks WHERE session_id=?", (sess["id"],))["n"] == len(usns),
      str(r["data"]))
check("4c ... under the teacher's id, with a verify step that says ok",
      sess["taken_by"] == "F002" and any(t[1] == "verify" and t[3] == "ok" for t in r["trace"] if len(t) > 3))
after = {u: total(u, subj) for u in usns}
check("4d every student's hours held went up by one; hours attended only for those present",
      all(after[u] == (t_before[u][0] + 1, t_before[u][1] + (u not in (usns[1], usns[2]))) for u in usns),
      str([(u, t_before[u], after[u]) for u in usns[:3]]))
hd = one(con, "SELECT SUM(held) h, SUM(attended) a FROM attendance WHERE usn=?", (usns[1],))
check("4e the headline attendance is recomputed from the per-subject totals",
      one(con, "SELECT attendance FROM students WHERE usn=?", (usns[1],))["attendance"] == round(100 * hd["a"] / hd["h"], 1)
      and one(con, "SELECT attendance FROM students WHERE usn=?", (usns[1],))["attendance"] != head_before[usns[1]])
perf = next(p for p in portal.subject_performance(con, portal._stu(con, usns[1])) if p["code"] == subj)
check("4f the student's dashboard reads the new hour", (perf["attendance"]["held"], perf["attendance"]["attended"])
      == after[usns[1]], str(perf["attendance"]))
aud = one(con, "SELECT * FROM audit WHERE action='attendance.take' ORDER BY id DESC LIMIT 1")
check("4g the audit ledger has it, under the teacher", aud and aud["actor"] == "F002" and aud["outcome"] == "Applied")
before = snap()
r = call("take_attendance", args, F002, "yes")
check("4h the same register again changes nothing and says so", refused(r) and "already recorded" in r["data"]["error"]
      and snap() == before)
a, b = staged_commit(F002, {**args, "absent": usns[1]})
check("4i taking it again is a correction: only the changed student's hours attended move",
      "BLOCKED" in a["data"] and b["data"].get("corrected") == 1 and total(usns[2], subj) == (after[usns[2]][0],
                                                                                           after[usns[2]][1] + 1)
      and total(usns[1], subj) == after[usns[1]], str(b["data"]))
aud = one(con, "SELECT payload FROM audit WHERE action='attendance.correct' ORDER BY id DESC LIMIT 1")
check("4j ... and the ledger keeps what it was", aud and f'["{usns[2]}", 0, 1]' in aud["payload"],
      aud["payload"] if aud else "none")
nxt = next((w for w in wed[1:] if w["subject"] == subj and (w["dept"], w["sem"], w["section"]) ==
            (w0["dept"], w0["sem"], w0["section"])), None)
sheet = call("attendance_sheet", {"date": THU, "period": 3, "dept": "CSE", "sem": 3, "section": "A"}, F002)["data"]
check("4k the next hour of the same subject offers the last class's absentees to copy",
      sheet.get("subject") != subj or sheet.get("last_absent") == [usns[1]], str(sheet.get("last_absent")))
thu = rows(con, "SELECT period, dept, sem, section FROM timetable WHERE faculty='F002' AND day='Thu' AND subject IN "
                "(SELECT code FROM subjects WHERE kind='T') ORDER BY period")[-1]
everyone = class_usns(thu["dept"], thu["sem"], thu["section"])
a, b = staged_commit(F002, {"date": THU, **{k: thu[k] for k in ("period", "dept", "sem", "section")},
                            "absent": ", ".join(everyone)})
check("4l marking everyone absent is a register too (present 0)", b["data"].get("present") == 0
      and b["data"].get("of") == len(everyone), str(b["data"])[:120])
hist = call("attendance_history", {}, F002)["data"]["sessions"]
check("4m the teacher's history lists the hours they recorded, and no one else's",
      len(hist) == one(con, "SELECT COUNT(*) n FROM attendance_sessions WHERE faculty='F002'")["n"] >= 2
      and all(one(con, "SELECT faculty FROM attendance_sessions WHERE date=? AND period=? AND dept||'-'||sem||section=?",
                  (h["date"], h["period"], h["class"]))["faculty"] == "F002" for h in hist), str(hist)[:120])

# ------------------------------------------------------------------ 5 · marks
print("\n5 · the marks desk: the scheme's maxima, submission, the lock")
print("-" * 78)
lab = next(c for c in A._courses(con, teacher=None) if c["kind"] == "L" and c["dept"] == "CSE")
teacher = who(lab["teacher"])
r = call("cie_sheet", {}, teacher)
check("5a the desk lists exactly the teacher's courses of record",
      sorted((c["subject"], c["cls"]) for c in r["data"]["courses"])
      == sorted((c["subject"], c["cls"]) for c in A._courses(con, teacher=lab["teacher"])))
foreign = next(c for c in A._courses(con) if c["teacher"] != lab["teacher"] and c["dept"] == lab["dept"])
r = call("cie_sheet", {"subject": foreign["subject"], "section": foreign["section"]}, teacher)
check("5b another teacher's course is refused (DENIED)", "DENIED" in r["data"], str(r["data"])[:100])
r = call("cie_sheet", {"subject": lab["subject"], "section": lab["section"], "component": "RECORD"}, teacher)
check("5c the maximum comes from the configured scheme (lab record /30)", r["data"]["max"] == 30
      and dict((x["component"], x["max"]) for x in r["data"]["components"]) == {"RECORD": 30, "LABTEST": 20})
check("5d seeded: the lab record is complete and waiting to be submitted (due today)",
      next(x for x in r["data"]["components"] if x["component"] == "RECORD")["action"] == "submit")
th = next(c for c in A._courses(con) if c["kind"] == "T")
check("5e seeded: IA1 is submitted for every theory course",
      all(A.locked(con, c, "IA1") for c in A._courses(con) if c["kind"] == "T"))
late = A.overdue_entries(con)[0]
r = call("submit_cie_marks", {"subject": late["subject"], "section": late["section"], "component": late["component"]},
         who(late["teacher"]), "yes")
check("5f a register with marks missing cannot be submitted, and says how many",
      refused(r) and "have no" in r["data"]["error"] and r["data"].get("missing"), str(r["data"])[:120])
sa = {"subject": lab["subject"], "section": lab["section"], "component": "RECORD"}
s = S(teacher)
r1 = tools.execute(con, s, "yes", "submit_cie_marks", sa)
r2 = tools.execute(con, s, "yes", "submit_cie_marks", s["pending"]["args"])
lock = A.locked(con, lab, "RECORD")
check("5g a complete register is proposed, then submitted and locked under its teacher",
      "BLOCKED" in r1["data"] and r2["data"].get("submitted") is True and lock and lock["submitted_by"] == lab["teacher"],
      str(r2["data"])[:120])
note = one(con, "SELECT * FROM notifications ORDER BY id DESC LIMIT 1")
check("5h ... and the HOD is told", note and note["audience"] == f"HOD · {lab['dept']}" and "submitted" in note["title"])
u0 = A._class_students(con, lab)[0]["usn"]
old = one(con, "SELECT marks FROM cie_marks WHERE usn=? AND subject=? AND component='RECORD'", (u0, lab["subject"]))
newmark = 1 if old["marks"] != 1 else 2
s = S(teacher)
ma = {**sa, "marks": f"{u0} {newmark}"}
r1 = tools.execute(con, s, "fix it", "record_cie_marks", ma)
r2 = tools.execute(con, s, "yes", "record_cie_marks", ma)
check("5i after submission the teacher's correction is refused - locked, DENIED - and nothing changes",
      "DENIED" in r1["data"] and "locked" in r1["data"]["DENIED"] and "DENIED" in r2["data"]
      and one(con, "SELECT marks FROM cie_marks WHERE usn=? AND subject=? AND component='RECORD'",
              (u0, lab["subject"]))["marks"] == old["marks"], str(r1["data"])[:120])
hod = who(one(con, "SELECT id FROM faculty WHERE dept=? AND is_hod=1", (lab["dept"],))["id"])
s = S(hod)
r1 = tools.execute(con, s, "correct it", "record_cie_marks", ma)
r2 = tools.execute(con, s, "yes", "record_cie_marks", ma)
check("5j the HOD can still correct a locked mark, told it is a correction to a locked register",
      "BLOCKED" in r1["data"] and any("locked" in (b.get("md") or "") for b in r1["blocks"])
      and one(con, "SELECT marks FROM cie_marks WHERE usn=? AND subject=? AND component='RECORD'",
              (u0, lab["subject"]))["marks"] == newmark, str(r2["data"])[:120])
r = call("submit_cie_marks", sa, teacher, "yes")
check("5k a second submission is refused", refused(r) and "already submitted" in r["data"]["error"])
owed = A.marks_pending(con, late["teacher"])
check("5l what a teacher owes lists the overdue entry", any(x["subject"] == late["subject"] and x["cls"] == late["cls"]
                                                            and x["action"] == "enter" for x in owed), str(owed)[:120])

# ------------------------------------------------------------------ 6 · routing
print("\n6 · the assistant: the same desk, in words")
print("-" * 78)
P1T = who(p1["faculty"])            # absent today - so use a teacher with a P1 class still theirs
p1b = one(con, """SELECT t.faculty, t.dept, t.sem, t.section, t.subject FROM timetable t JOIN subjects s ON s.code=t.subject
                  JOIN faculty f ON f.id=t.faculty WHERE t.day=? AND t.period=1 AND s.kind='T' AND f.is_hod=0
                  AND t.faculty<>? AND t.faculty<>? ORDER BY t.faculty LIMIT 1""",
          (TODAY.strftime("%a"), p1["faculty"], sub or ""))
NOWT = who(p1b["faculty"])
ent = {"usn": None, "dept": None, "sem": None, "section": None, "periods": [], "date": None, "faculty": None}
tool, a = portal.route(con, "Take attendance for my class", NOWT, ent)
check("6a 'take attendance' in class opens the register of the hour that is on now",
      tool == "attendance_sheet" and a["period"] == 1 and a["dept"] == p1b["dept"], f"{tool} {a}")
u = class_usns(p1b["dept"], p1b["sem"], p1b["section"])
tool, a = portal.route(con, f"Take attendance, {u[0]} and {u[3]} were absent", NOWT, ent)
check("6b with absentees named it is the write, for that hour", tool == "take_attendance"
      and a["absent"] == f"{u[0]}, {u[3]}" and a["period"] == 1, f"{tool} {a}")
for text, want in (("My pending attendance", "teaching_day"), ("Which marks submissions are pending?", "cie_sheet"),
                   ("my classes today", "teaching_day"), ("my timetable", "faculty_timetable"),
                   ("Attendance history", "attendance_history")):
    tool, a = portal.route(con, text, F002, ent)
    check(f"6c '{text}' -> {want}", tool == want, f"{tool} {a}")
c0 = A._courses(con, teacher="F002")[0]
tool, a = portal.route(con, f"Enter IA2 marks for {c0['sname']} section {c0['section']}", F002,
                       {**ent, "section": c0["section"]})
check("6d 'enter IA2 marks for <subject name>' opens that course's sheet", tool == "cie_sheet"
      and a.get("subject") == c0["subject"], f"{tool} {a}")
thu3 = one(con, "SELECT dept, sem, section FROM timetable WHERE faculty='F002' AND day='Thu' AND period=3")
tool, a = portal.route(con, f"Take attendance for my {thu3['dept']}-{thu3['sem']}{thu3['section']} class yesterday at P3",
                       F002, {**ent, "date": TODAY - dt.timedelta(days=1), "dept": thu3["dept"], "sem": thu3["sem"],
                              "section": thu3["section"]})
check("6g 'yesterday at P3' names the hour the way a teacher does", tool == "attendance_sheet" and a["period"] == 3
      and a["date"] == THU, f"{tool} {a}")
u3 = class_usns(thu3["dept"], thu3["sem"], thu3["section"])
tool, a = portal.route(con, f"{u3[1]} and {u3[6]} were absent in P3 yesterday", F002,
                       {**ent, "date": TODAY - dt.timedelta(days=1)})
check("6h 'X and Y were absent in P3 yesterday' is the write for that hour",
      tool == "take_attendance" and a["absent"] == f"{u3[1]}, {u3[6]}" and a["period"] == 3, f"{tool} {a}")
tool, a = A.cie_route(con, "Submit RECORD marks for BCSL307 section A", ent, admin=False)
check("6e 'submit ... marks for <code>' is the submission", tool == "submit_cie_marks" and a["subject"] == "BCSL307")
orchestrator.SESS.clear()
sid = "teaching-e2e"
st = orchestrator.sess(sid)
st["user"] = NOWT
out1 = orchestrator.handle(con, f"Take attendance, {u[5]} was absent", sid, NOWT["role"], actor=NOWT["id"])
staged = (st.get("pending") or {}).get("tool")
out2 = orchestrator.handle(con, "yes", sid, NOWT["role"], actor=NOWT["id"])
row = one(con, "SELECT * FROM attendance_sessions WHERE date=? AND period=1 AND dept=? AND sem=? AND section=?",
          (FRI, p1b["dept"], p1b["sem"], p1b["section"]))
check("6f end to end on the rule engine: proposed, then 'yes' records it",
      staged == "take_attendance" and row and row["absent"] == 1 and row["taken_by"] == p1b["faculty"],
      str(out2.get("blocks"))[:160])

# ------------------------------------------------------------------ 8 · lab batches (#20)
print("\n8 · a split lab hour is one class hour per batch (#20)")
print("-" * 78)
away = {r["faculty"] for r in rows(con, "SELECT faculty FROM leaves WHERE status='Approved' AND from_date<=? AND to_date>=?",
                                   (THU, THU))}
lab = next(r for r in rows(con, """SELECT dept, sem, section, period, faculty FROM timetable WHERE batch='B1' AND day='Thu'
                                   ORDER BY dept, sem, section, period""")
           if not {x["faculty"] for x in rows(con, "SELECT faculty FROM timetable WHERE dept=? AND sem=? AND section=? "
                                                   "AND day='Thu' AND period=?",
                                              (r["dept"], r["sem"], r["section"], r["period"]))} & away)
hour = {"date": THU, "period": lab["period"], "dept": lab["dept"], "sem": lab["sem"], "section": lab["section"]}
legs = {r["batch"]: r for r in rows(con, "SELECT * FROM timetable WHERE dept=? AND sem=? AND section=? AND day='Thu' "
                                         "AND period=?", (lab["dept"], lab["sem"], lab["section"], lab["period"]))}
roll = {b: [r["usn"] for r in rows(con, "SELECT usn FROM students WHERE dept=? AND sem=? AND section=? AND batch=? "
                                        "ORDER BY usn", (lab["dept"], lab["sem"], lab["section"], b))] for b in legs}
t1, t2 = who(legs["B1"]["faculty"]), who(legs["B2"]["faculty"])
sh1, sh2 = call("attendance_sheet", hour, t1), call("attendance_sheet", hour, t2)
check("8a each lab teacher opens their own batch's register, with only that batch's students on it",
      sorted(legs) == ["B1", "B2"] and all(roll.values()) and set(roll["B1"]).isdisjoint(roll["B2"])
      and sh1["data"].get("batch") == "B1" and sh1["data"]["subject"] == legs["B1"]["subject"]
      and [x["usn"] for x in sh1["data"]["students"]] == roll["B1"]
      and sh2["data"].get("batch") == "B2" and sh2["data"]["subject"] == legs["B2"]["subject"]
      and [x["usn"] for x in sh2["data"]["students"]] == roll["B2"], f"{sh1['data']}"[:200])
other = next(who(f["id"]) for f in rows(con, "SELECT id FROM faculty WHERE dept=? AND is_hod=0 ORDER BY id", (lab["dept"],))
             if f["id"] not in (t1["fid"], t2["fid"]))
r = call("attendance_sheet", hour, other)
check("8b a teacher delivering neither batch is refused, and told who is",
      r["data"].get("DENIED") and "batch B1" in r["data"]["DENIED"] and "batch B2" in r["data"]["DENIED"],
      str(r["data"])[:200])
asked, named = call("attendance_sheet", hour), call("attendance_sheet", {**hour, "batch": "b2"})
check("8c the Registrar is asked which batch, and naming one opens that batch's register",
      refused(asked) and "Name the batch" in asked["data"].get("error", "")
      and [x["usn"] for x in named["data"].get("students", [])] == roll["B2"], str(asked["data"])[:160])
subj = {b: legs[b]["subject"] for b in legs}
before = {u: (total(u, subj["B1"]), total(u, subj["B2"])) for b in roll for u in roll[b]}
p1, c1 = staged_commit(t1, {**hour, "absent": roll["B1"][0]})
p2, c2 = staged_commit(t2, {**hour, "absent": "none"})
moved = {u for u in before if (total(u, subj["B1"]), total(u, subj["B2"])) != before[u]}
s1 = one(con, "SELECT * FROM attendance_sessions WHERE date=? AND dept=? AND sem=? AND section=? AND period=? "
              "AND batch='B1'", (THU, lab["dept"], lab["sem"], lab["section"], lab["period"]))
s2 = one(con, "SELECT * FROM attendance_sessions WHERE date=? AND dept=? AND sem=? AND section=? AND period=? "
              "AND batch='B2'", (THU, lab["dept"], lab["sem"], lab["section"], lab["period"]))
check("8d the two batches of one hour are two registers: each adds its lab's hour to its own students only",
      c1["data"].get("recorded") and c2["data"].get("recorded") and s1 and s2
      and (s1["present"], s1["absent"]) == (len(roll["B1"]) - 1, 1) and (s2["present"], s2["absent"]) == (len(roll["B2"]), 0)
      and moved == set(before)
      and all(total(u, subj["B1"]) == (before[u][0][0] + 1, before[u][0][1] + (u != roll["B1"][0]))
              and total(u, subj["B2"]) == before[u][1] for u in roll["B1"])
      and all(total(u, subj["B2"]) == (before[u][1][0] + 1, before[u][1][1] + 1)
              and total(u, subj["B1"]) == before[u][0] for u in roll["B2"]),
      f"{c1['data']} {c2['data']}"[:200])
day = T.day_classes(con, t1["fid"], dt.date.fromisoformat(THU))
mine = [c for c in day if c["period"] == lab["period"] and c["dept"] == lab["dept"]]
check("8e the teacher's day lists the hour once, as their batch, sized to it",
      len(mine) == 1 and mine[0]["batch"] == "B1" and mine[0]["size"] == len(roll["B1"]), str(mine)[:200])
fri = next(r for r in rows(con, "SELECT dept, sem, section, period FROM timetable WHERE batch='B2' AND day=? "
                                "ORDER BY dept, sem, section", (TODAY.strftime("%a"),)))
kid = dict(one(con, "SELECT * FROM students WHERE dept=? AND sem=? AND section=? AND batch='B2' ORDER BY usn",
               (fri["dept"], fri["sem"], fri["section"])))
own = one(con, "SELECT subject FROM timetable WHERE dept=? AND sem=? AND section=? AND day=? AND period=? AND batch='B2'",
          (fri["dept"], fri["sem"], fri["section"], TODAY.strftime("%a"), fri["period"]))["subject"]
today = next(b for b in portal._student_home(con, kid)["blocks"] if b.get("type") == "table"
             and str(b.get("title", "")).startswith("Today"))
at = [row for row in today["rows"] if row[0] == f"P{fri['period']}"]
check("8f a student's day shows their own batch's lab, once per hour",
      len(at) == 1 and at[0][2].startswith(own) and len({row[0] for row in today["rows"]}) == len(today["rows"]),
      str(at))
g = TimetableAgent().class_grid(con, lab["dept"], lab["sem"], lab["section"], THU)
cell = g["cells"].get(f"Thu-{lab['period']}") or {}
check("8g the class grid draws the hour as one cell holding both batches",
      [x["batch"] for x in cell.get("batches", [])] == ["B1", "B2"]
      and {x["subject"] for x in cell["batches"]} == set(subj.values()), str(cell)[:200])

# ------------------------------------------------------------------ 7 · the page
print("\n7 · the portal page")
print("-" * 78)
page = open(os.path.join(HERE, "static", "portal.html"), encoding="utf-8").read()
check("7a the dashboard's question buttons carry no approval word (a click is the teacher's own turn)",
      not any(guard.APPROVAL_RX.search(q) for q in portal.STAFF_QUICK)
      and not any(guard.APPROVAL_RX.search(q) for q, _v in portal.STAFF_VIEWS))
home = tools.execute(con, S(F002), "", "my_home", {})
quick = next(b for b in home["blocks"] if b.get("type") == "quick")
check("7b ... and its view buttons open views, asking nothing", all(("view" in i) != ("ask" in i) for i in quick["items"])
      and {i["view"] for i in quick["items"] if "view" in i} == {"tt", "att", "marks"})
hh = tools.execute(con, S(HOD), "", "my_home", {})
# an HOD's home carries the leave count as well; the two once shared a variable and the page came back empty
check("7f an HOD's home renders, with today's classes, what is owed and the department's leave",
      not (hh["data"] or {}).get("error") and "attendance_to_take" in hh["data"]
      and any(c.get("label", "").endswith("leave to decide") for b in hh["blocks"] if b.get("type") == "cards"
              for c in b["cards"]), str(hh["data"])[:140])
sends = re.findall(r"api\('/api/chat',\{method:'POST'.*?body:JSON\.stringify\(\{(.*?)\}\)", page, re.S)
settle_src = page.split("async function settle")[1].split("\n}\n")[0]
check("7c the page talks to the assistant twice: the person's own words, and settle()'s confirmation - "
      "which is sent only from a staged proposal's button, on the rule engine",
      len(sends) == 2 and sends[0] == "text,session:SID" and "Yes, go ahead" in sends[1] and "engine:'rule'" in sends[1]
      and "Yes, go ahead" in settle_src and "if(!s)return" in settle_src, str(sends))
props = re.findall(r"fetch\('/api/me/propose',\{.*?body:JSON\.stringify\(\{(.*?)\}\)", page, re.S)
check("7d the forms only ever stage: one call to /api/me/propose, carrying the tool and its arguments, no words",
      props == ["tool,args,session:SID"], str(props))
check("7e Confirm is the one violet button in the desk", page.count('class="btn ok"') == 1, str(page.count('class="btn ok"')))

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
