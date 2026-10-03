"""Strength and ranking (#94, #95), and a class named the console's way (#98).
Every figure is recounted from the students table, a cohort that does not
exist is refused rather than answered with a wider one, and each role sees
only its own scope. No server, no API cost.

    python tests/cohort_test.py
"""
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_cohort_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import access                                                      # noqa: E402
import auth                                                        # noqa: E402
import nlu                                                         # noqa: E402
import orchestrator as O                                           # noqa: E402
import portal                                                      # noqa: E402
import solver                                                      # noqa: E402
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


def call(name, args=None, user=None):
    S = {"pending": None, "history": [], "ctx": {}}
    if user:
        S["user"] = auth.principal(con, user)
    return tools.execute(con, S, "", name, args or {})


def count(where="1", args=()):
    return one(con, f"SELECT COUNT(*) c FROM students WHERE {where}", args)["c"]


def turn(q, user=None):
    sid = f"cohort-{user or 'admin'}-{abs(hash(q))}"
    O.SESS.pop(sid, None)
    if user:
        O.sess(sid)["user"] = auth.principal(con, user)
    out = O.handle(con, q, sid)
    O.SESS.pop(sid, None)
    return out, json.dumps(out.get("blocks"), ensure_ascii=False)


def err(r):
    return (r.get("data") or {}).get("error") or ""


# =========================================================== 1 · strength (#94)
print("\n1 · how many students a section, a semester and a department have")
print("-" * 78)
REPORTED = "What is the strength of the CSE 7th sem SEC A"
TRUE_7A = count("dept='CSE' AND sem=7 AND section='A'")
out, text = turn(REPORTED)
card = next((b for b in out["blocks"] if b.get("type") == "cards"), {"cards": []})["cards"]
check("1a the reported question gets CSE-7A's own count, recounted from the table",
      card and card[0]["label"] == "Students in CSE-7A" and card[0]["value"] == TRUE_7A, f"{card[:1]} vs {TRUE_7A}")
r = call("class_strength", {"dept": "CSE", "sem": 7, "section": "A"})
check("1b ...with the semester and department totals labelled as what they are",
      r["data"]["students"] == TRUE_7A and r["data"]["semester_total"] == count("dept='CSE' AND sem=7")
      and r["data"]["department_total"] == count("dept='CSE'") and TRUE_7A < r["data"]["semester_total"],
      str(r["data"]))
bad = [f"{d}-{s}{x}" for d, s, x in solver.classes(con)
       if call("class_strength", {"dept": d, "sem": s, "section": x})["data"].get("students")
       != count("dept=? AND sem=? AND section=?", (d, s, x))]
check("1c every running section's figure is its own count", not bad and len(solver.classes(con)) == 21, str(bad))
r = call("class_strength", {"dept": "CSE", "sem": 7})
check("1d a semester is the sum of its sections, each listed",
      r["data"]["students"] == count("dept='CSE' AND sem=7")
      and [s["class"] for s in r["data"]["sections"]] == ["CSE-7A", "CSE-7B"], str(r["data"]))
r = call("class_strength", {"dept": "cse", "sem": "7", "section": "a"})
check("1e a model's lower case and string semester still name CSE-7A", r["data"].get("students") == TRUE_7A,
      str(r["data"])[:120])
r = call("class_strength", {})
check("1f no filter is the whole institution, section by section",
      r["data"]["students"] == count() and len(r["data"]["sections"]) == 21)

# Defect (#94): the department figure was offered when the section was not found.
DEPT_TOTAL = str(count("dept='CSE'"))
for k, q in (("1g", "strength of CSE-9A"), ("1h", "How many students are in CSE sem 9"),
             ("1i", "strength of CSE section E 7th sem")):
    out, text = turn(q)
    check(f"{k} '{q}' is refused and names the sections, never a wider figure",
          "No student on the rolls" in text and "CSE-7A" in text and DEPT_TOTAL not in text, text[:160])
out, text = turn("strength of EEE-3A")
check("1j a department the college does not have is named as such, not read as every 3A",
      "EEE-3A" in text and "Departments: CIVIL, CSE, ECE, ISE, MECH" in text, text[:160])
check("1k a semester that is not a number is an answer, not a crash",
      err(call("class_strength", {"dept": "CSE", "sem": "seventh"})).startswith("Semester must be a number"))

# ============================================================ 2 · ranking (#95)
print("\n2 · students ranked by CGPA")
print("-" * 78)
r = call("top_students", nlu.rank_args("ok give me the names of top 10 student who have cgpa above 8 in cse "
                                       "department", {"dept": "CSE"}))
d = r["data"]
cg = [s["cgpa"] for s in d.get("students", [])]
check("2a the reported question: every CSE student above 8 counted, the ten highest shown in order",
      d.get("matching") == count("dept='CSE' AND cgpa>8") and len(cg) >= 10 and cg == sorted(cg, reverse=True)
      and min(cg) > 8 and all(s["class"].startswith("CSE-") for s in d["students"]), str(d)[:200])
check("2b ...with rank, USN, name and CGPA on every row",
      all({"rank", "usn", "name", "cgpa"} <= set(s) for s in d["students"]))
top_sql = [x["usn"] for x in rows(con, "SELECT usn FROM students WHERE dept='CSE' AND cgpa>8 "
                                       "ORDER BY cgpa DESC, usn LIMIT 10")]
check("2c ...and they are exactly the ten the table says", [s["usn"] for s in d["students"]][:10] == top_sql)
EXACT = count("dept='CSE' AND cgpa=8")
a = call("top_students", {"dept": "CSE", "cgpa_at_least": 8})["data"]["matching"]
b = call("top_students", {"dept": "CSE", "cgpa_above": 8})["data"]["matching"]
check("2d 'above' is strict and 'at least' is not (a CSE student has exactly 8.00)",
      EXACT >= 1 and a - b == EXACT, f"{a} {b} {EXACT}")
r = call("top_students", {"dept": "CSE", "limit": 1})["data"]
best = one(con, "SELECT MAX(cgpa) m FROM students WHERE dept='CSE'")["m"]
tied = count("dept='CSE' AND cgpa=?", (best,))
# Defect a plain LIMIT has: the topper is whichever tied student sorts first.
check("2e a tie at the cut-off is shown, not broken silently",
      tied >= 2 and r["shown"] == tied and all(s["rank"] == 1 for s in r["students"]), str(r)[:200])
r = call("top_students", {"dept": "CSE", "limit": 4})["data"]["students"]
check("2f a shared CGPA shares a rank and the next rank skips (1, 1, 3)",
      [s["rank"] for s in r][:3] == [1, 1, 3], str([s["rank"] for s in r]))
r = call("top_students", {"dept": "MECH", "order": "bottom", "limit": 5})["data"]
check("2g 'bottom' is the lowest first",
      r["students"][0]["cgpa"] == one(con, "SELECT MIN(cgpa) m FROM students WHERE dept='MECH'")["m"]
      and [s["cgpa"] for s in r["students"]] == sorted(s["cgpa"] for s in r["students"]))
r = call("top_students", {"dept": "CSE", "cgpa_at_least": 7, "cgpa_at_most": 7.5})["data"]
check("2h a band is both its ends, counted in full",
      r["matching"] == count("dept='CSE' AND cgpa>=7 AND cgpa<=7.5"), str(r)[:120])
r = call("top_students", {"dept": "CSE", "cgpa_above": 9.95})
check("2i an empty band is an answer (nobody), not an error", not err(r) and r["data"]["matching"] == 0)
check("2j a limit outside 1-100 is refused, and so is one that is not a number",
      all(err(call("top_students", {"limit": n})) for n in (0, 101, "ten")))
r = call("top_students", {"dept": "CSE", "sem": 9})
check("2k a cohort that does not exist is refused here too", "No student on the rolls" in err(r))
PARSE = {
    "top 10 students with cgpa above 8": {"limit": 10, "cgpa_above": 8.0},
    "students with cgpa 8 and above": {"cgpa_at_least": 8.0},
    "students with cgpa at least 8.5": {"cgpa_at_least": 8.5},
    "students with cgpa below 6": {"cgpa_below": 6.0},
    "cgpa between 8 and 7 in cse": {"dept": "CSE", "cgpa_at_least": 7.0, "cgpa_at_most": 8.0},
    "least scoring 3 students": {"limit": 3, "order": "bottom"},
    "bottom 5 students by cgpa": {"limit": 5, "order": "bottom"},
    "who is the topper of CSE-7A": {"limit": 1, "dept": "CSE", "sem": 7, "section": "A"},
    "top 20 students in CSE 5B": {"limit": 20, "dept": "CSE", "sem": 5, "section": "B"},
}
wrong = {}
for q, want in PARSE.items():
    ent = {"dept": nlu.extract_dept(q), "sem": nlu.extract_sem(q), "section": nlu.extract_section(q)}
    got = nlu.rank_args(q, ent)
    if got != want:
        wrong[q] = got
check("2l the sentence is read into the right band, size and order", not wrong, str(wrong))

# ========================================================== 3 · which questions
print("\n3 · which questions are strength or ranking questions")
print("-" * 78)
check("3a the reported #94 sentence is a student question (it was smalltalk)",
      (nlu.classify(REPORTED) or [("none",)])[0][0] == "student.query")
names = tools.select_tools(REPORTED)[1]
names95 = tools.select_tools("give me the names of top 10 student who have cgpa above 8 in cse department")[1]
check("3b the language model is offered the tool for each", "class_strength" in names and "top_students" in names95,
      f"{names} {names95}")
NOT_STRENGTH = ["What is the faculty strength of CSE", "How many students have fee dues",
                "how many students are below 75% attendance", "how many students are in the hostel",
                "How many students have library books overdue"]
check("3c a count that belongs to another tool is not a strength question",
      not [q for q in NOT_STRENGTH if nlu.STRENGTH_RX.search(q)],
      str([q for q in NOT_STRENGTH if nlu.STRENGTH_RX.search(q)]))
NOT_RANK = ["top 5 overdue books", "top students by internal marks",
            "students with cgpa above 7 eligible for the Nethra Robotics drive",
            "lowest attendance students in CSE"]
check("3d nor is a ranking of books, marks, a drive's shortlist or attendance",
      not [q for q in NOT_RANK if nlu.RANK_RX.search(q)], str([q for q in NOT_RANK if nlu.RANK_RX.search(q)]))
out, _t = turn("who is the topper of CSE 7A")
check("3e both engines reach the same tool: the rule engine ranks through top_students",
      "cgpa_rank" in json.dumps(out["trace"]), json.dumps(out["trace"])[-200:])

# ================================================================ 4 · by role
print("\n4 · who may count and who may rank")
print("-" * 78)
STU = one(con, "SELECT * FROM students WHERE dept='CSE' AND sem=5 AND section='A' ORDER BY usn")
HOD = one(con, "SELECT * FROM faculty WHERE is_hod=1 AND dept='CSE'")
FAC = one(con, "SELECT * FROM faculty WHERE is_hod=0 AND dept='CSE' ORDER BY id")
r = call("class_strength", {}, STU["usn"])
check("4a a student's strength question is their own class",
      r["data"].get("scope") == "CSE-5A" and r["data"]["students"] == count("dept='CSE' AND sem=5 AND section='A'"),
      str(r["data"])[:120])
r = call("class_strength", {"dept": "CSE", "sem": 7, "section": "A"}, STU["usn"])
check("4b ...and another class's is refused", "DENIED" in r["data"])
check("4c a student may not rank other students' CGPAs",
      "DENIED" in call("top_students", {}, STU["usn"])["data"]
      and "top_students" not in access.allowed_tools(auth.principal(con, STU["usn"])))
check("4d a teacher counts any class in their department and none outside it",
      call("class_strength", {"sem": 7, "section": "B"}, FAC["id"])["data"].get("scope") == "CSE-7B"
      and "DENIED" in call("class_strength", {"dept": "ECE"}, FAC["id"])["data"])
check("4e ...and may not rank students either", "DENIED" in call("top_students", {}, FAC["id"])["data"])
r = call("top_students", {"cgpa_above": 8}, HOD["id"])
check("4f a HOD's ranking is their department's",
      r["data"].get("matching") == count("dept='CSE' AND cgpa>8") and r["data"]["scope"].startswith("CSE"),
      str(r["data"])[:120])
check("4g ...and never another department's", "DENIED" in call("top_students", {"dept": "ECE"}, HOD["id"])["data"])
out, text = turn("ok give me the names of top 10 student who have cgpa above 8 in cse department", HOD["id"])
# Defect found writing 4h: "department" sent the HOD to the department overview.
check("4h the reported #95 sentence, asked by the CSE HOD, is the ranking, not the overview",
      out.get("intent") == "top_students" and top_sql[0] in text, str(out.get("intent")))
out, text = turn("How many students are in my class?", STU["usn"])
check("4i a student's 'how many in my class' is answered for their class",
      out.get("intent") == "class_strength" and "CSE-5A" in text, str(out.get("intent")))

# ================================================== 5 · CSE-7A is semester 7 (#98)
print("\n5 · a class written the way the console writes it (#98)")
print("-" * 78)
SEMS = {"CSE-7A": 7, "CSE 7A": 7, "cse7a": 7, "ECE 3B timetable": 3, "show CSE-5 timetable": 5,
        "MECH-3A": 3, "CSE 7 faculty are absent": None, "4VP23CS014 details": None, "top 5 students": None}
wrong = {q: nlu.extract_sem(q) for q, s in SEMS.items() if nlu.extract_sem(q) != s}
check("5a CSE-7A, CSE 7A and cse7a are semester 7; a bare 'CSE 7' and a USN are not a class", not wrong, str(wrong))
out, text = turn("Rebuild the timetable for CSE-7A")
check("5b 'Rebuild the timetable for CSE-7A' proposes CSE-7A (it proposed CSE-5A)",
      "Proposed timetable — CSE-7A" in text and "CSE-5A" not in text, text[:160])
out, text = turn("Show CSE 7A timetable")
check("5c ...and 'Show CSE 7A timetable' shows semester 7", "Semester 7" in text and "Semester 5" not in text,
      text[:120])

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
