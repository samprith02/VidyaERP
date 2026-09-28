"""CIE marks (#32): seeded from the student's own record, standing computed
from what is entered, entry validated and gated, scoped by role, and read by
the eligibility check, Student 360 and the Ops radar. No server, no API cost.

    python tests/cie_test.py
"""
import datetime as dt
import hashlib
import json
import os
import statistics
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_cie_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import academics as A                                              # noqa: E402
import auth                                                        # noqa: E402
import nlu                                                         # noqa: E402
import orchestrator                                                # noqa: E402
import portal                                                      # noqa: E402
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
TODAY = nlu.TODAY


def marks_hash():
    rs = [list(r) for r in con.execute("SELECT usn, subject, component, marks, absent, entered_by, entered_at "
                                       "FROM cie_marks ORDER BY usn, subject, component")]
    return hashlib.sha256(json.dumps(rs).encode()).hexdigest()[:16]


def S(user=None):
    s = {"pending": None, "history": [], "ctx": {}}
    if user:
        s["user"] = auth.principal(con, user)
    return s


def mark(usn, subject, comp):
    r = one(con, "SELECT marks, absent FROM cie_marks WHERE usn=? AND subject=? AND component=?", (usn, subject, comp))
    return None if r is None else ("AB" if r["absent"] else r["marks"])


def pearson(xs, ys):
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / ((sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5)


# ------------------------------------------------------------------ 1 · seed
print("\n1 · the seeded marks register")
print("-" * 78)
courses = A._courses(con)
theory = [c for c in courses if c["kind"] == "T"]
labs = [c for c in courses if c["kind"] == "L"]
book = A._book(con, courses)


def complete(c, comp):
    return all(comp in book.get((c["subject"], s["usn"]), {}) for s in A._class_students(con, c))


check("1a every examined course has a teacher of record and a class", len(courses) == 120
      and all(c["teacher"] and c["size"] > 0 for c in courses), f"{len(courses)} courses")
check("1b IA1 is in for every theory course and the record for every lab",
      all(complete(c, "IA1") for c in theory) and all(complete(c, "RECORD") for c in labs))
ia2 = [c for c in theory if complete(c, "IA2")]
ia2_none = [c for c in theory if not any("IA2" in book.get((c["subject"], s["usn"]), {})
                                         for s in A._class_students(con, c))]
check("1c IA2 is either fully in or not started, for most courses in (seed share 0.8)",
      len(ia2) + len(ia2_none) == len(theory) and 0.65 <= len(ia2) / len(theory) <= 0.95,
      f"{len(ia2)}/{len(theory)} in")
future = [(c, x) for c, x, *_r in [(r["subject"], r["component"]) for r in rows(con, "SELECT subject, component "
                                                                                  "FROM cie_marks")]
          if x in ("AAT", "LABTEST")]
check("1d nothing is entered for an assessment that has not happened (AAT 11 Sep, lab test 14 Sep)", not future,
      str(future[:3]))
limits = {k: mx for kind in A.CIE_SCHEME.values() for k, mx, _w, _d in kind}
bad = rows(con, "SELECT * FROM cie_marks WHERE marks < 0")
bad += [dict(r) for r in rows(con, "SELECT * FROM cie_marks") if r["marks"] > limits[r["component"]]]
check("1e every mark lies within its component's range", not bad, str(bad[:2]))
check("1f an absentee scores 0 and is marked AB; labs have no absentees",
      not rows(con, "SELECT 1 FROM cie_marks WHERE absent=1 AND marks<>0")
      and not rows(con, "SELECT 1 FROM cie_marks WHERE absent=1 AND component='RECORD'"))
pts = [(s["cgpa"], r["marks"]) for r in rows(con, "SELECT usn, marks FROM cie_marks WHERE component='IA1' AND absent=0")
       for s in [one(con, "SELECT cgpa FROM students WHERE usn=?", (r["usn"],))]]
rho = pearson([p[0] for p in pts], [p[1] for p in pts])
# The point of seeding from the record: a CIE risk means something about the
# student. Independent draws (the #21 weakness) would give r near 0.
check("1g IA marks follow CGPA (Pearson r > 0.3)", rho > 0.3, f"r = {rho:.3f}")
first = marks_hash()
con.close()
db.seed(force=True)
con = db.connect()
check("1h a reseed rebuilds the identical register", marks_hash() == first)

# ------------------------------------------------------------------ 2 · standing
print("\n2 · standing is computed from what is entered")
print("-" * 78)


def m(**kw):
    return {k: {"marks": 0 if v == "AB" else v, "absent": int(v == "AB")} for k, v in kw.items()}


st = A.standing("T", m(IA1=25, IA2=21))
check("2a IA 25 and 21 bank 23 of 50 before the assignment: minimum already secured",
      st["secured"] == 23 and st["status"] == "Secured" and st["needs"] == 0, str(st))
st = A.standing("T", m(IA1=8, IA2=9))
check("2b IA 8 and 9 project 17/50: at risk, needs 11.5 more", st["projected"] == 17 and st["status"] == "At risk"
      and st["needs"] == 11.5 and st["ceiling"] == 33.5, str(st))
st = A.standing("T", m(IA1=2, IA2=2, AAT=5))
check("2c with everything in and 7/50, it cannot be reached", st["status"] == "Cannot reach" and st["ceiling"] == 7,
      str(st))
st = A.standing("T", m(IA1=10, IA2=10))
check("2d exactly 40% projects exactly 20: on track, not at risk", st["projected"] == 20
      and st["status"] == "On track", str(st))
st = A.standing("T", m(IA1=15, IA2=15, AAT=5))
check("2e exactly 20 secured is secured", st["secured"] == 20 and st["status"] == "Secured", str(st))
st = A.standing("L", m(RECORD=6))
check("2f a lab counts the record out of 30 with the test still to come", st["projected"] == 10
      and st["ceiling"] == 26 and st["status"] == "At risk", str(st))
check("2g nothing entered is 'Not assessed', not a zero", A.standing("T", {})["status"] == "Not assessed")
st = A.standing("T", m(IA1="AB", IA2=20))
check("2h an absence counts as zero", st["secured"] == 10 and st["projected"] == 20, str(st))

# ------------------------------------------------------------------ 3 · entry status
print("\n3 · who has not entered what")
print("-" * 78)
late = A.overdue_entries(con)
check("3a every theory course without IA2 is overdue - and nothing else is",
      sorted((x["subject"], x["cls"]) for x in late) == sorted((c["subject"], c["cls"]) for c in ia2_none)
      and all(x["component"] == "IA2" for x in late), f"{len(late)} vs {len(ia2_none)}")
check("3b IA2 was held 26 Aug and due 02 Sep, so it is 2 days late on Fri 04 Sep",
      all(x["overdue_days"] == 2 for x in late), str({x["overdue_days"] for x in late}))
ov = tools.execute(con, S(), "", "cie_marks", {})
check("3c the overview names the same overdue entries and the same students at risk",
      sum(d["entries_overdue"] for d in ov["data"]["departments"]) == len(late)
      and ov["data"]["students_at_risk"] == len({r["usn"] for r in A.cie_risk(con)}), json.dumps(ov["data"])[:200])

# ------------------------------------------------------------------ 4 · reads
print("\n4 · the views")
print("-" * 78)
r = tools.execute(con, S(), "", "cie_marks", {"usn": "4VP24CS009"})
check("4a a student's sheet covers every course of their class", r["data"]["class"] == "CSE-5A"
      and len(r["data"]["subjects"]) == len(A._courses(con, "CSE", 5, "A")), str(r["data"])[:160])
c0 = late[0]
r = tools.execute(con, S(), "", "cie_marks", {"subject": c0["subject"], "section": c0["section"]})
check("4b a course view reports its overdue component", any(e["component"] == "IA2" and e["state"].startswith("Overdue")
                                                             for e in r["data"]["entry"]), str(r["data"]["entry"]))
check("4c ... and its at-risk list matches cie_risk for that course",
      sorted(x["usn"] for x in r["data"]["at_risk"])
      == sorted(x["usn"] for x in A.cie_risk(con) if x["subject"] == c0["subject"] and x["cls"] == c0["cls"]))
r = tools.execute(con, S(), "", "cie_marks", {"teacher": c0["teacher"]})
mine = A._courses(con, teacher=c0["teacher"])
check("4d a teacher filter shows exactly the courses they teach",
      (len(mine) == 1 and r["data"]["course"]["subject"] == mine[0]["subject"])
      or sorted(x["subject"] + x["class"] for x in r["data"].get("courses", []))
      == sorted(c["subject"] + c["cls"] for c in mine), str(r["data"])[:160])
check("4e every view states the scheme it computed with", "20/50" in r["data"]["scheme"])

# ------------------------------------------------------------------ 5 · entry is validated
print("\n5 · entry: validated before anything is proposed")
print("-" * 78)
cls = A._class_students(con, c0)
u = [s["usn"] for s in cls]
other = one(con, "SELECT usn FROM students WHERE NOT (dept=? AND sem=? AND section=?) LIMIT 1",
            (c0["dept"], c0["sem"], c0["section"]))["usn"]
base = {"subject": c0["subject"], "component": "IA2", "section": c0["section"]}


def refused(args, U="yes"):
    before = marks_hash()
    s = S()
    r = tools.execute(con, s, U, "record_cie_marks", {**base, **args})
    return (r["data"].get("error") or r["data"].get("DENIED")) and marks_hash() == before and not s["pending"], r


ok, r = refused({"marks": f"{u[0]} 26"})
check("5a above the maximum (26/25) is refused, nothing staged", ok, str(r["data"])[:120])
ok, r = refused({"marks": f"{u[0]} 18, {other} 20"})
check("5b a USN from another class is refused", ok and other in r["data"]["error"], str(r["data"])[:120])
ok, r = refused({"marks": f"{u[0]} 18, {u[0]} 19"})
check("5c the same USN twice is refused", ok and "twice" in r["data"]["error"], str(r["data"])[:120])
ok, r = refused({"component": "AAT", "marks": f"{u[0]} 18"})
check("5d an assessment not yet held (AAT, 11 Sep) cannot be entered", ok and "11 Sep" in r["data"]["error"])
ok, r = refused({"component": "LABTEST", "marks": f"{u[0]} 18"})
check("5e a lab component on a theory course is refused", ok)
ok, r = refused({"marks": "no marks here"})
check("5f no USN-mark pairs, nothing proposed", ok)
done = next(c for c in theory if complete(c, "IA2") and c["section"] == "A")
s1 = A._class_students(con, done)[0]["usn"]
ok, r = refused({"subject": done["subject"], "section": "A", "marks": f"{s1} {mark(s1, done['subject'], 'IA2')}"})
check("5g re-entering the same mark changes nothing and is said so", ok and "already recorded" in r["data"]["error"])
two = [c for c in theory if c["subject"] == theory[0]["subject"]]
multi = next((c for c in theory if len(A._courses(con, c["dept"], c["sem"], subject=c["subject"])) > 1), None)
if multi:
    ss = {x["section"]: A._class_students(con, x)[0]["usn"] for x in A._courses(con, multi["dept"], multi["sem"],
                                                                                   subject=multi["subject"])}
    ok, r = refused({"subject": multi["subject"], "section": None, "component": "IA1",
                     "marks": ", ".join(f"{v} 10" for v in ss.values())})
    check("5h USNs from two sections at once are refused", ok and "one section" in r["data"]["error"],
          str(r["data"])[:120])

# ------------------------------------------------------------------ 6 · the write
print("\n6 · entry: proposed, committed only with approval, re-read")
print("-" * 78)
s = S()
before = marks_hash()
args = {**base, "marks": f"{u[0]} 18, {u[1]} AB, {u[2]} 22.5"}
r = tools.execute(con, s, "enter these", "record_cie_marks", args)
check("6a without approval it proposes and writes nothing", "BLOCKED" in r["data"] and marks_hash() == before
      and s["pending"]["tool"] == "record_cie_marks", str(r["data"])[:120])
r = tools.execute(con, s, "yes", "record_cie_marks", args)
check("6b with approval it commits and says it re-read them",
      r["data"].get("recorded") == 3 and any(t[1] == "verify" and t[3] == "ok" for t in r["trace"] if len(t) > 3)
      and (mark(u[0], c0["subject"], "IA2"), mark(u[1], c0["subject"], "IA2"), mark(u[2], c0["subject"], "IA2"))
      == (18, "AB", 22.5), str(r["data"]))
r = tools.execute(con, S(), "yes", "record_cie_marks", {**base, "marks": f"{u[0]} 19"})
aud = one(con, "SELECT payload FROM audit WHERE action='cie.record' ORDER BY id DESC LIMIT 1")
check("6c a correction is labelled one, and the audit ledger keeps the old mark",
      mark(u[0], c0["subject"], "IA2") == 19 and json.loads(aud["payload"])["changes"] == [[u[0], 18.0, 19.0]],
      aud["payload"] if aud else "no audit row")
rest = ", ".join(f"{x} 15" for x in u[3:])
tools.execute(con, S(), "yes", "record_cie_marks", {**base, "marks": rest})
note = one(con, "SELECT * FROM notifications ORDER BY id DESC LIMIT 1")
check("6d completing a component publishes it to the class", note and c0["cls"] in note["audience"]
      and "IA2" in note["title"], str(dict(note)) if note else "none")
check("6e ... and the course is no longer overdue", (c0["subject"], c0["cls"]) not in
      {(x["subject"], x["cls"]) for x in A.overdue_entries(con)})

# ------------------------------------------------------------------ 7 · who may
print("\n7 · who may read and enter which marks")
print("-" * 78)
c1 = late[1]
t1 = c1["teacher"]
u1 = [x["usn"] for x in A._class_students(con, c1)]
st1 = S(t1)
r = tools.execute(con, st1, "yes", "record_cie_marks",
                  {"subject": c1["subject"], "component": "IA2", "marks": f"{u1[0]} 17"})
# People confirm by saying "apply"/"ok" - so a teacher's first message can never
# commit, even with an approval word in it (portal._staged, #27).
check("7a a teacher's first message only proposes, even if it says yes", "BLOCKED" in r["data"]
      and mark(u1[0], c1["subject"], "IA2") is None, str(r["data"])[:120])
r = tools.execute(con, st1, "yes", "record_cie_marks",
                  {"subject": c1["subject"], "component": "IA2", "marks": f"{u1[0]} 17"})
check("7b ... and the confirmation commits it, entered in their name",
      mark(u1[0], c1["subject"], "IA2") == 17 and one(con, "SELECT entered_by FROM cie_marks WHERE usn=? AND "
                                                           "subject=? AND component='IA2'",
                                                      (u1[0], c1["subject"]))["entered_by"] == t1, str(r["data"])[:120])
wrong = next(f["id"] for f in rows(con, "SELECT id FROM faculty WHERE dept=? AND is_hod=0 ORDER BY id", (c1["dept"],))
             if f["id"] != t1 and not A._courses(con, teacher=f["id"], subject=c1["subject"], section=c1["section"]))
r = tools.execute(con, S(wrong), "yes", "record_cie_marks",
                  {"subject": c1["subject"], "component": "IA2", "marks": f"{u1[1]} 17"})
check("7c another teacher is refused - DENIED, not a failure the mesh would blink red",
      "DENIED" in r["data"] and tools.failure_of(r) is None and mark(u1[1], c1["subject"], "IA2") is None,
      str(r["data"])[:140])
r = tools.execute(con, S(wrong), "", "cie_marks", {"teacher": t1})
check("7d a teacher cannot read another teacher's courses", "DENIED" in r["data"], str(r["data"])[:120])
stu = u1[2]
r = tools.execute(con, S(stu), "", "cie_marks", {})
check("7e a student sees their own sheet", r["data"].get("usn") == stu)
r = tools.execute(con, S(stu), "", "cie_marks", {"usn": u1[3]})
check("7f ... and is refused anyone else's", "DENIED" in r["data"], str(r["data"])[:120])
r = tools.execute(con, S(stu), "yes", "record_cie_marks", {"subject": c1["subject"], "component": "IA2",
                                                           "marks": f"{stu} 25"})
check("7g a student cannot enter marks at all", "DENIED" in r["data"] and mark(stu, c1["subject"], "IA2") is None)
hod = one(con, "SELECT id FROM faculty WHERE dept=? AND is_hod=1", (c1["dept"],))["id"]
far = one(con, "SELECT usn FROM students WHERE dept<>? LIMIT 1", (c1["dept"],))["usn"]
r = tools.execute(con, S(hod), "", "cie_marks", {"usn": far})
check("7h an HOD is refused a student outside the department", "DENIED" in r["data"], str(r["data"])[:120])
sh = S(hod)
a = {"subject": c1["subject"], "component": "IA2", "marks": f"{u1[0]} 16"}
tools.execute(con, sh, "correct it", "record_cie_marks", a)
tools.execute(con, sh, "yes", "record_cie_marks", a)
check("7i an HOD can correct a mark in their department", mark(u1[0], c1["subject"], "IA2") == 16)

# ------------------------------------------------------------------ 8 · routing
print("\n8 · the rule engine and the portal")
print("-" * 78)
ent = {"usn": None, "dept": None, "sem": None, "section": None, "faculty": None}
tool, a = A.cie_route(con, "Enter IA2 marks for BCS501 CSE 5A: 4VP24CS001 18, 4VP24CS002 AB", ent)
check("8a an entry sentence becomes record_cie_marks with every pair",
      tool == "record_cie_marks" and a == {"subject": "BCS501", "component": "IA2", "section": "A",
                                           "marks": "4VP24CS001 18, 4VP24CS002 AB"}, f"{tool} {a}")
tool, a = A.cie_route(con, "internal marks of 4VP24CS009 5th sem", {**ent, "usn": "4VP24CS009"})
check("8b a USN followed by '5th sem' is a read, not a mark of 5", tool == "cie_marks" and a == {"usn": "4VP24CS009"},
      f"{tool} {a}")
tool, a = A.cie_route(con, "Correct 4VP24CS009's IA1 in BCS501 to 19", {**ent, "usn": "4VP24CS009"})
check("8c 'correct X's IA1 ... to 19' is a one-mark correction", tool == "record_cie_marks"
      and a["marks"] == "4VP24CS009 19" and a["component"] == "IA1", f"{tool} {a}")
top = nlu.classify("Which internal marks are still to be entered?")[0][0]
check("8d the radar's sentence reaches the CIE intent", top == "cie", top)
check("8e 'Computer Science' is not a CIE question (no substring 'cie')",
      all(i != "cie" for i, *_r in nlu.classify("Computer Science department overview")))
P = auth.principal(con, "4VP24CS009")
check("8f a student's 'my internal marks' is their own sheet",
      portal.route(con, "my internal marks", P, {"usn": None}) == ("cie_marks", {}))
check("8g ... and naming another USN passes it on, so access.py refuses it",
      portal.route(con, "IA2 marks of 4VP24CS010", P, {"usn": "4VP24CS010"}) == ("cie_marks", {"usn": "4VP24CS010"}))
orchestrator.sess("t8")["user"] = auth.principal(con, c1["teacher"])
out = orchestrator.handle(con, f"Enter IA2 marks for {c1['subject']}: {u1[5]} 14", "t8")
out = orchestrator.handle(con, "yes", "t8")
check("8h a teacher enters marks through the portal: propose, then yes", mark(u1[5], c1["subject"], "IA2") == 14,
      str([b.get("md") for b in out["blocks"]])[:160])

# ------------------------------------------------------------------ 9 · read elsewhere
print("\n9 · the SEE eligibility check, Student 360 and the radar read it")
print("-" * 78)
risk = A.cie_risk(con)
r = tools.execute(con, S(), "", "exam_eligibility", {})
check("9a the eligibility check counts every course below the CIE minimum", r["data"]["cie_at_risk"] == len(risk),
      f"{r['data'].get('cie_at_risk')} vs {len(risk)}")
who = risk[0]["usn"]
r = tools.execute(con, S(), "", "student_360", {"usn": who})
check("9b Student 360 shows the courses at risk", risk[0]["subject"] in r["data"]["cie_at_risk"],
      str(r["data"].get("cie_at_risk")))
rad = tools.execute(con, S(), "", "ops_radar", {})
titles = [x["title"] for x in rad["data"]["actions"]]
check("9c the radar raises overdue entries and students at risk",
      any("CIE marks entries overdue" in t for t in titles) and any("CIE minimum" in t for t in titles), str(titles))
cmds = [x["command"] for x in rad["data"]["actions"] if x["domain"] == "Examinations"]
check("9d none of its commands can approve anything", not any(tools.APPROVAL_RX.search(c) for c in cmds), str(cmds))
con.execute("INSERT OR REPLACE INTO cie_marks VALUES('4VP24CS001','BCS501','IA1',1,0,'x','')")
con.commit()
con.close()
db.seed(force=True)
con = db.connect()
check("9e a reset restores the seeded register (the #64 lesson)", marks_hash() == first)

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
