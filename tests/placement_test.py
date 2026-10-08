"""Placement drives end to end (#115): each drive's own criteria, one
eligibility engine that says criterion by criterion why, applications that
re-check the records, and the drive on the academic calendar of the classes it
is open to. No server, no API cost.

    python tests/placement_test.py
"""
import os
import re
import sys
import json
import tempfile
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_placement_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import academic_calendar as cal                                    # noqa: E402
import auth                                                        # noqa: E402
import campus                                                      # noqa: E402
import orchestrator                                                # noqa: E402
import placement as PL                                             # noqa: E402
import portal                                                      # noqa: E402
import tools                                                       # noqa: E402
from agents import one, rows                                       # noqa: E402
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


def S(user=None):
    return {"pending": None, "history": [], "ctx": {}, "user": user}


def call(name, args, user=None, U=""):
    return tools.execute(con, S(user), U, name, args)


def admin(name, args):
    """The Registrar: propose, then approve in their own words."""
    s = S()
    first = tools.execute(con, s, "prepare it", name, args)
    second = tools.execute(con, s, "yes, go ahead", name, (s.get("pending") or {}).get("args") or args)
    return first, second


def as_student(usn):
    return auth.principal(con, usn)


def refused(r):
    d = r.get("data") or {}
    return bool(d.get("error") or d.get("DENIED"))


def snap():
    return ([tuple(r) for r in con.execute("SELECT * FROM placement_drives ORDER BY id")],
            [tuple(r) for r in con.execute("SELECT * FROM drive_registrations ORDER BY drive_id, usn")],
            [tuple(r) for r in con.execute("SELECT * FROM calendar_events ORDER BY id")],
            [tuple(r) for r in con.execute("SELECT * FROM student_offers ORDER BY id")])


def student(where, *a):
    return one(con, f"""SELECT s.*, m.entry, m.tenth, m.twelfth, m.diploma FROM students s
                        JOIN student_school_marks m ON m.usn=s.usn
                        WHERE {where} AND s.usn NOT IN (SELECT usn FROM student_offers) ORDER BY s.usn LIMIT 1""", a)


def drive_id(company):
    return one(con, "SELECT id FROM placement_drives WHERE company=? ORDER BY id DESC", (company,))["id"]


# ------------------------------------------------------------------ 1 · data
print("\n1 · the data the engine reads")
print("-" * 78)
sch = rows(con, "SELECT m.*, s.cgpa FROM student_school_marks m JOIN students s ON s.usn=m.usn")
n_st = one(con, "SELECT COUNT(*) n FROM students")["n"]
lat = [x for x in sch if x["entry"] == "Lateral"]
check("1a every student has 10th marks; a lateral entrant a diploma and no 12th, everyone else a 12th",
      len(sch) == n_st and all(x["tenth"] is not None for x in sch)
      and all(x["twelfth"] is None and x["diploma"] is not None for x in lat)
      and all(x["twelfth"] is not None and x["diploma"] is None for x in sch if x["entry"] == "Regular"))
check("1b about one in twelve is a lateral entrant", 0.05 < len(lat) / n_st < 0.13, f"{len(lat)}/{n_st}")


def pearson(xs, ys):
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    return sxy / (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5


r10 = pearson([x["cgpa"] for x in sch], [x["tenth"] for x in sch])
check("1c school marks follow the CGPA (r > 0.3), so a cut-off is not a coin toss", r10 > 0.3, f"r={r10:.2f}")
seeded = rows(con, "SELECT * FROM placement_drives ORDER BY id")
check("1d the seeded drives are published, each with its criteria as one record",
      all(d["status"] == "Published" and PL.criteria(d)["sems"] == [7] for d in seeded))
ok_cal = True
for d in seeded:
    ids = PL._cal_ids(d)
    evs = [one(con, "SELECT * FROM calendar_events WHERE id=?", (i,)) for i in ids]
    ok_cal &= len(ids) == len(PL.criteria(d)["depts"]) and all(
        e and e["status"] == "Active" and e["date"] == d["drive_date"] and e["sem"] == 7 for e in evs)
check("1e each published drive is on the calendar once per department it is open to, on its date", ok_cal)

# the old rule, restated independently: department, semester 7, CGPA, backlogs, one offer
best = {r["usn"]: r["c"] for r in rows(con, "SELECT usn, MAX(ctc) c FROM student_offers GROUP BY usn")}
same = True
for d in seeded:
    old_ok = [s["usn"] for s in rows(con, "SELECT * FROM students WHERE sem=7 ORDER BY cgpa DESC, usn")
              if s["dept"] in d["depts"].split(",") and s["cgpa"] >= d["min_cgpa"] and s["backlogs"] <= d["max_backlogs"]
              and not (best.get(s["usn"]) and not (d["dream"] and d["ctc"] >= 1.5 * best[s["usn"]]))]
    new_ok, _w = campus.PlacementAgent().eligibility(con, d)
    same &= old_ok == [s["usn"] for s in new_ok]
check("1f the existing drives' eligible lists are unchanged by the new engine (one-offer rule preserved)", same)

# ------------------------------------------------------------------ 2 · creating drives
print("\n2 · creating a drive: validated, previewed, saved as a draft")
print("-" * 78)
ABC = {"company": "Aravalli Byte Labs", "role": "Software Developer", "ctc": 6, "drive_date": "2026-10-20",
       "reg_end": "2026-10-15", "drive_time": "10:00", "location": "Bengaluru", "industry": "Software",
       "description": "Product engineering for logistics software.", "website": "https://aravalli-byte.example",
       "job_description": "Backend services in Python.", "job_location": "Bengaluru", "work_mode": "Hybrid",
       "selection_process": "Aptitude · technical · HR", "openings": 10,
       "min_cgpa": 7.0, "max_backlogs": 0, "min_tenth": 60, "min_twelfth": 60, "depts": "CSE, ISE", "sems": "7"}
XYZ = {**ABC, "company": "Xylem Yard Systems", "role": "Graduate Trainee", "ctc": 4.8, "drive_date": "2026-10-22",
       "reg_end": "2026-10-16", "website": "https://xylem-yard.example",
       "min_cgpa": 6.5, "max_backlogs": 1, "min_tenth": 55, "min_twelfth": 55, "depts": "CSE, ECE, MECH"}
before = snap()
r = call("save_placement_drive", ABC, None, "")
check("2a without approval it proposes and writes nothing", "BLOCKED" in r["data"] and snap() == before)
r = call("save_placement_drive", {k: v for k, v in ABC.items() if k not in ("company", "reg_end")}, None, "yes")
check("2b missing required fields are named, nothing proposed", refused(r) and "company" in r["data"]["error"]
      and "reg end" in r["data"]["error"], str(r["data"])[:120])
for bad, why in (({"reg_end": "2026-10-25"}, "close"), ({"drive_date": "2026-08-01", "reg_end": "2026-07-30"}, "passed"),
                 ({"depts": "CSE, AIML"}, "AIML"), ({"sems": "8"}, "semester 8"), ({"min_cgpa": 11}, "between"),
                 ({"website": "aravalli"}, "http"), ({"batches": "2031"}, "2031")):
    r = call("save_placement_drive", {**ABC, **bad}, None, "yes")
    check(f"2c refused: {list(bad)} ({why})", refused(r) and why in r["data"]["error"] and snap() == before,
          str(r["data"])[:120])
f1, f2 = admin("save_placement_drive", ABC)
abc = one(con, "SELECT * FROM placement_drives WHERE company=?", (ABC["company"],))
check("2d approved, it is saved as a Draft with its criteria as one record, and re-read",
      f2["data"].get("saved") is True and abc["status"] == "Draft" and PL.criteria(abc)["min_tenth"] == 60
      and PL.criteria(abc)["depts"] == ["CSE", "ISE"] and PL.criteria(abc)["sems"] == [7], str(f2["data"]))
indep = [s["usn"] for s in rows(con, """SELECT s.* FROM students s JOIN student_school_marks m ON m.usn=s.usn
          WHERE s.sem=7 AND s.dept IN ('CSE','ISE') AND s.cgpa>=7 AND s.backlogs=0 AND m.tenth>=60
          AND ((m.entry='Regular' AND m.twelfth>=60) OR (m.entry='Lateral' AND m.diploma>=60))""")
         if s["usn"] not in best]
check("2e the eligible count it previewed is every criterion, recounted independently",
      f1["data"].get("BLOCKED") and any(c.get("value") == len(indep) for b in f1["blocks"] if b.get("type") == "cards"
                                        for c in b["cards"] if c["label"].startswith("Eligible")), str(len(indep)))
admin("save_placement_drive", XYZ)
xyz = one(con, "SELECT * FROM placement_drives WHERE company=?", (XYZ["company"],))
u_any = one(con, "SELECT usn FROM students WHERE sem=7 AND dept='CSE' ORDER BY usn")["usn"]
r = call("placement_drives", {}, as_student(u_any))
check("2f a draft is invisible to students: not in their list, not found by name",
      ABC["company"] not in json.dumps(r["data"]) and refused(call("drive_details", {"drive": ABC["company"]},
                                                                   as_student(u_any))))

# ------------------------------------------------------------------ 3 · publishing
print("\n3 · publishing: the drive reaches the calendar of the classes it is open to")
print("-" * 78)
nb = one(con, "SELECT COUNT(*) n FROM notifications")["n"]
p1, p2 = admin("set_drive_status", {"drive": abc["id"], "action": "publish"})
abc = one(con, "SELECT * FROM placement_drives WHERE id=?", (abc["id"],))
ids = PL._cal_ids(abc)
check("3a published: status, and one calendar entry per department and semester, verified",
      abc["status"] == "Published" and len(ids) == 2 and p2["data"].get("done") is True
      and any(t[1] == "verify" and t[3] == "ok" for t in p2["trace"] if len(t) > 3), str(p2["data"]))
admin("set_drive_status", {"drive": xyz["id"], "action": "publish"})
cse7 = one(con, "SELECT usn FROM students WHERE dept='CSE' AND sem=7 ORDER BY usn")["usn"]
ece7 = one(con, "SELECT usn FROM students WHERE dept='ECE' AND sem=7 ORDER BY usn")["usn"]
cse5 = one(con, "SELECT usn FROM students WHERE dept='CSE' AND sem=5 ORDER BY usn")["usn"]


def on_cal(usn, title):
    r = call("calendar_events", {"from_date": "2026-10-01", "to_date": "2026-10-31"}, as_student(usn))
    return any(e["title"].startswith(f"Placement drive: {title}") for e in r["data"].get("events", []))


check("3b a CSE-7 student sees it on their calendar; an ECE-7 and a CSE-5 student do not",
      on_cal(cse7, ABC["company"]) and not on_cal(ece7, ABC["company"]) and not on_cal(cse5, ABC["company"]))
r = call("calendar_events", {"from_date": "2026-10-01", "to_date": "2026-10-31"})
check("3c the Registrar's calendar shows it", any(e["title"].startswith(f"Placement drive: {ABC['company']}")
                                                  for e in r["data"]["events"]))
note = rows(con, "SELECT * FROM notifications WHERE id>? ORDER BY id", (nb,))
check("3d the eligible students are told, with the count", any(ABC["company"] in n["title"] and
                                                               f"({len(indep)})" in n["audience"] for n in note),
      str([n["audience"] for n in note]))
r = call("save_placement_drive", {"drive": abc["id"], "drive_date": "2026-10-21", "reg_end": "2026-10-15"}, None, "")
check("3e an edit of a published drive shows the date change before anything moves", "BLOCKED" in r["data"])
admin("save_placement_drive", {"drive": abc["id"], "drive_date": "2026-10-21"})
abc2 = one(con, "SELECT * FROM placement_drives WHERE id=?", (abc["id"],))
moved = [one(con, "SELECT * FROM calendar_events WHERE id=?", (i,)) for i in PL._cal_ids(abc2)]
check("3f a new date moves the same calendar entries: same ids, new date, no duplicates",
      PL._cal_ids(abc2) == ids and all(e["date"] == "2026-10-21" for e in moved)
      and one(con, "SELECT COUNT(*) n FROM calendar_events WHERE title LIKE ? AND status='Active'",
              (f"Placement drive: {ABC['company']}%",))["n"] == 2, str([(e["id"], e["date"]) for e in moved]))

# ------------------------------------------------------------------ 4 · the engine
print("\n4 · eligibility: every criterion, and which one failed")
print("-" * 78)
abc = abc2
good = student("s.sem=7 AND s.dept='CSE' AND s.cgpa>=7.5 AND s.backlogs=0 AND m.entry='Regular' "
               "AND m.tenth>=62 AND m.twelfth>=62")
ev = PL.evaluate(con, abc, good)
check("4a fully eligible: every criterion met, and the action is Apply",
      ev["eligible"] and all(r["ok"] for r in ev["criteria"]) and ev["action"] == "Apply"
      and {r["key"] for r in ev["criteria"]} >= {"dept", "sem", "cgpa", "backlogs", "tenth", "twelfth", "policy"})
low = student("s.sem=7 AND s.dept='CSE' AND s.cgpa<7 AND s.backlogs=0 AND m.entry='Regular' AND m.tenth>=60 "
              "AND m.twelfth>=60")
ev = PL.evaluate(con, abc, low)
check("4b CGPA below the cut-off: not eligible, and the reason is the CGPA with both figures",
      not ev["eligible"] and [r["key"] for r in ev["failed"]] == ["cgpa"]
      and f"{low['cgpa']:.2f}" in PL.reasons(ev)[0] and "7.00" in PL.reasons(ev)[0] and ev["action"] == "Not eligible",
      str(PL.reasons(ev)))
mech = student("s.sem=7 AND s.dept='MECH' AND s.cgpa>=7.5 AND s.backlogs=0 AND m.tenth>=62 AND m.entry='Regular' "
               "AND m.twelfth>=62")
ev = PL.evaluate(con, abc, mech)
check("4c a department the drive is not open to: that, and only that", [r["key"] for r in ev["failed"]] == ["dept"])
sem5 = student("s.sem=5 AND s.dept='CSE' AND s.cgpa>=7.5 AND s.backlogs=0 AND m.tenth>=62 AND m.entry='Regular' "
               "AND m.twelfth>=62")
ev = PL.evaluate(con, abc, sem5)
check("4d a 5th-semester student for a 7th-semester drive: the semester", [r["key"] for r in ev["failed"]] == ["sem"]
      and "5th" in PL.reasons(ev)[0])
bl = student("s.sem=7 AND s.dept='CSE' AND s.cgpa>=7 AND s.backlogs=1 AND m.tenth>=60 AND m.entry='Regular' "
             "AND m.twelfth>=60")
check("4e one backlog: refused by ABC (0 allowed), allowed by XYZ (1 allowed)",
      [r["key"] for r in PL.evaluate(con, abc, bl)["failed"]] == ["backlogs"]
      and PL.evaluate(con, one(con, "SELECT * FROM placement_drives WHERE id=?", (xyz["id"],)), bl)["eligible"],
      str(PL.reasons(PL.evaluate(con, abc, bl))))
many = student("s.sem=7 AND s.dept='CSE' AND s.cgpa<7 AND s.backlogs>0 AND m.entry='Regular' AND m.twelfth<60")
ev = PL.evaluate(con, abc, many)
check("4f several failures are all reported", {"cgpa", "backlogs", "twelfth"} <= {r["key"] for r in ev["failed"]}
      and len(PL.reasons(ev)) >= 3, str(PL.reasons(ev)) if many else "no such student")
latd = student("m.entry='Lateral'")             # any lateral entrant: which rows apply, not whether they pass
ev = PL.evaluate(con, abc, latd) if latd else None
check("4g a lateral entrant is held to the 12th cut-off on their diploma, never as 'no 12th'",
      ev and any(r["key"] == "diploma" for r in ev["criteria"]) and not any(r["key"] == "twelfth" for r in ev["criteria"]),
      str(ev and ev["criteria"]))
gone = good["usn"]
con.execute("DELETE FROM student_school_marks WHERE usn=?", (gone,))
ev = PL.evaluate(con, abc, good)
check("4h a criterion with nothing on record is not met, and says so", not ev["eligible"]
      and any(r["actual"] == "not on record" for r in ev["failed"]), str(PL.reasons(ev)))
con.execute("INSERT INTO student_school_marks VALUES(?,?,?,?,?)", (good["usn"], good["entry"], good["tenth"],
                                                                    good["twelfth"], good["diploma"]))
placed = one(con, """SELECT s.* FROM students s JOIN student_school_marks m ON m.usn=s.usn
                     WHERE s.sem=7 AND s.dept='CSE' AND s.cgpa>=7.5 AND s.backlogs=0 AND m.entry='Regular'
                     AND m.tenth>=62 AND m.twelfth>=62 AND s.usn IN (SELECT usn FROM student_offers) LIMIT 1""")
ev = PL.evaluate(con, abc, placed)
check("4i the one-offer rule is its own layer: academic met, placement policy not, final not eligible",
      ev["academic_ok"] and ev["cohort_ok"] and not ev["policy_ok"] and not ev["eligible"]
      and [r["key"] for r in ev["failed"]] == ["policy"], str(ev["failed"]))
# The seeded 10th marks clear 60% for everyone near ABC's other cut-offs, so a
# drive is built with its 10th bar just above a real student's own 10th: that
# criterion, and only it, must fail, and the eligible count must drop by
# exactly the students under the bar (found by mutation: a 10th check that
# never failed went unnoticed).
bar = good["tenth"] + 0.5
probe = {**abc, "criteria": json.dumps({**PL.criteria(abc), "min_tenth": bar})}
ev = PL.evaluate(con, probe, good)
under = [s for s in rows(con, """SELECT s.usn FROM students s JOIN student_school_marks m ON m.usn=s.usn
                                 WHERE s.sem=7 AND s.dept IN ('CSE','ISE') AND s.cgpa>=7 AND s.backlogs=0
                                 AND ((m.entry='Regular' AND m.twelfth>=60) OR (m.entry='Lateral' AND m.diploma>=60))
                                 AND m.tenth>=?""", (bar,)) if s["usn"] not in best]
check("4k a 10th cut-off above a student's 10th fails that criterion alone, and the count follows it",
      [r["key"] for r in ev["failed"]] == ["tenth"] and "10th" in PL.reasons(ev)[0]
      and len(PL.cohort_eligibility(con, probe)[0]) == len(under), f"{PL.reasons(ev)} {len(under)}")
r = call("drive_details", {"drive": ABC["company"]}, as_student(low["usn"]))
check("4j the student's page names the failed criterion, not a bare no",
      r["data"]["you"]["eligible"] is False and "CGPA" in r["blocks"][1]["md"] and "not eligible" in r["blocks"][0]["title"])

# ------------------------------------------------------------------ 5 · applying
print("\n5 · applying: checked again from the records, confirmed, re-read")
print("-" * 78)
GS = as_student(good["usn"])
s1 = S(GS)
before = snap()
a1 = tools.execute(con, s1, "apply", "apply_to_drive", {"drive": ABC["company"]})
check("5a the first message only proposes, even though 'apply' is an approval word",
      "BLOCKED" in a1["data"] and snap() == before, str(a1["data"])[:100])
a2 = tools.execute(con, s1, "yes", "apply_to_drive", s1["pending"]["args"])
reg = one(con, "SELECT * FROM drive_registrations WHERE drive_id=? AND usn=?", (abc["id"], good["usn"]))
check("5b the confirmation files it: Applied, a reference, the snapshot of what was checked, verified",
      a2["data"].get("applied") is True and reg and reg["status"] == "Applied"
      and json.loads(reg["submitted"])["cgpa"] == f"{good['cgpa']:.2f}" and a2["data"]["reference"].startswith("APP-")
      and any(t[1] == "verify" and t[3] == "ok" for t in a2["trace"] if len(t) > 3), str(a2["data"]))
aud = one(con, "SELECT * FROM audit WHERE action='placement.apply' ORDER BY id DESC LIMIT 1")
check("5c the ledger records the student, not anyone they named", aud and aud["actor"] == good["usn"])
r = call("apply_to_drive", {"drive": ABC["company"]}, GS, "yes")
check("5d a second application is refused", refused(r) and "already applied" in r["data"]["error"])
r = call("placement_drives", {}, GS)
check("5e the student sees it under their applications, with its status",
      any(x["company"] == ABC["company"] and x["action"] == "Applied" for x in r["data"]["applied"]))
r = call("drive_applications", {"drive": ABC["company"]})
check("5f ... and it reaches the Registrar's list of applicants",
      any(x["usn"] == good["usn"] and x["status"] == "Applied" for x in r["data"]["applicants"]))
LS = as_student(low["usn"])
s2 = S(LS)
before = snap()
tools.execute(con, s2, "apply", "apply_to_drive", {"drive": ABC["company"], "cgpa": 9.9})
r = tools.execute(con, s2, "yes", "apply_to_drive", {"drive": ABC["company"], "cgpa": 9.9})
check("5g an ineligible student is refused, even sending a better CGPA in the arguments",
      refused(r) and "CGPA" in r["data"]["error"] and snap() == before and not s2["pending"], str(r["data"])[:120])
other = student("s.sem=7 AND s.dept='CSE' AND s.cgpa>=7.5 AND s.backlogs=0 AND m.entry='Regular' AND m.tenth>=62 "
                "AND m.twelfth>=62 AND s.usn<>?", good["usn"])
s3 = S(LS)
tools.execute(con, s3, "apply", "apply_to_drive", {"drive": ABC["company"], "usn": other["usn"]})
check("5h nobody can apply as another student: a USN argument is not the applicant",
      not one(con, "SELECT 1 FROM drive_registrations WHERE drive_id=? AND usn=?", (abc["id"], other["usn"])))
r = call("apply_to_drive", {"drive": ABC["company"]}, None, "yes")
check("5i with no signed-in student (MCP, an operator) it refuses: there is nobody to act for",
      refused(r) and "student" in r["data"]["error"])
for name, args in (("save_placement_drive", ABC), ("set_drive_status", {"drive": abc["id"], "action": "cancel"}),
                   ("update_applications", {"drive": abc["id"], "usns": good["usn"], "status": "Selected"}),
                   ("drive_applications", {"drive": abc["id"]})):
    r = call(name, args, GS, "yes")
    check(f"5j a student cannot call {name}", "DENIED" in r["data"])
admin("set_drive_status", {"drive": abc["id"], "action": "close_registration"})
OS = as_student(other["usn"])
s4 = S(OS)
tools.execute(con, s4, "apply", "apply_to_drive", {"drive": ABC["company"]})
r = tools.execute(con, s4, "yes", "apply_to_drive", {"drive": ABC["company"]})
check("5k after registration closes, an eligible student is refused",
      refused(r) and "closed" in r["data"]["error"].lower(), str(r["data"])[:120])
check("5l ... and the page says Registration closed", PL.evaluate(con, one(con, "SELECT * FROM placement_drives WHERE id=?",
                                                                           (abc["id"],)), other)["action"]
      == "Registration closed")

# ------------------------------------------------------------------ 6 · decisions
print("\n6 · the Registrar decides applications")
print("-" * 78)
xyz = one(con, "SELECT * FROM placement_drives WHERE id=?", (xyz["id"],))
appl = [s["usn"] for s in PL.cohort_eligibility(con, xyz)[0] if s["dept"] == "CSE"][:3]
for u in appl:
    ss = S(as_student(u))
    tools.execute(con, ss, "apply", "apply_to_drive", {"drive": xyz["id"]})
    tools.execute(con, ss, "yes", "apply_to_drive", {"drive": xyz["id"]})
d1, d2 = admin("update_applications", {"drive": xyz["id"], "usns": ", ".join(appl[:2]), "status": "Shortlisted"})
check("6a shortlisting is proposed, then written for exactly those applicants",
      "BLOCKED" in d1["data"] and d2["data"].get("updated") == 2
      and [r["status"] for r in rows(con, "SELECT status FROM drive_registrations WHERE drive_id=? AND usn IN (?,?) "
                                          "ORDER BY usn", (xyz["id"], *sorted(appl[:2])))] == ["Shortlisted"] * 2)
r = call("drive_applications", {"drive": xyz["id"], "status": "shortlisted"})
check("6b the applicant list filters by status", sorted(x["usn"] for x in r["data"]["applicants"]) == sorted(appl[:2]))
r = call("drive_applications", {"drive": xyz["id"], "min_cgpa": 9.9})
check("6c ... and by CGPA", all(x["cgpa"] >= 9.9 for x in r["data"]["applicants"]))
admin("update_applications", {"drive": xyz["id"], "usns": appl[0], "status": "Selected"})
off = one(con, "SELECT * FROM student_offers WHERE usn=? AND company=?", (appl[0], XYZ["company"]))
nd = next(d for d in seeded if not d["dream"] and "CSE" in d["depts"])
check("6d a selection records the offer, and the one-offer rule then holds the student out of a non-dream drive",
      off and off["ctc"] == XYZ["ctc"] and not PL.evaluate(con, one(con, "SELECT * FROM placement_drives WHERE id=?",
                                                                      (nd["id"],)),
                                                          one(con, "SELECT * FROM students WHERE usn=?",
                                                              (appl[0],)))["policy_ok"])
r = call("update_applications", {"drive": xyz["id"], "usns": appl[0], "status": "Rejected"}, None, "yes")
check("6e a decided application is not decided again", refused(r) and "already selected" in r["data"]["error"])
r = call("update_applications", {"drive": xyz["id"], "usns": cse5, "status": "Shortlisted"}, None, "yes")
check("6f nobody who did not apply can be shortlisted", refused(r) and "did not apply" in r["data"]["error"])
ap = [r["usn"] for r in rows(con, "SELECT usn FROM drive_registrations WHERE drive_id=? AND status IN "
                                  "('Applied','Shortlisted')", (xyz["id"],))]
nb = one(con, "SELECT COUNT(*) n FROM notifications")["n"]
admin("set_drive_status", {"drive": xyz["id"], "action": "cancel"})
xyz = one(con, "SELECT * FROM placement_drives WHERE id=?", (xyz["id"],))
check("6g a cancelled drive leaves every calendar, and its open applicants are told",
      xyz["status"] == "Cancelled" and not on_cal(cse7, XYZ["company"])
      and any(f"({len(ap)})" in n["audience"] and "cancelled" in n["title"]
              for n in rows(con, "SELECT * FROM notifications WHERE id>?", (nb,))))
ss = S(as_student(other["usn"]))
r = tools.execute(con, ss, "yes", "apply_to_drive", {"drive": xyz["id"]})
check("6h a cancelled drive takes no application", refused(r))

# ------------------------------------------------------------------ 7 · in words
print("\n7 · the assistant: the same engine, asked in words")
print("-" * 78)
co = seeded[3]["company"]
ent = {"usn": None, "dept": None, "sem": None, "section": None}
for text, want in (("What placement drives are available?", "placement_drives"),
                   ("Which companies am I eligible for?", "placement_drives"),
                   (f"Am I eligible for {co}?", "drive_details"), (f"Why am I not eligible for {co}?", "drive_details"),
                   (f"When is the {co} placement drive?", "drive_details"), (f"Have I applied for {co}?", "drive_details"),
                   ("What is my application status?", "placement_drives"),
                   ("Show my placement applications", "placement_drives"),
                   (f"Apply for the {co} drive", "apply_to_drive")):
    tool, a = portal.route(con, text, GS, ent)
    check(f"7a '{text}' -> {want}", tool == want, f"{tool} {a}")
orchestrator.SESS.clear()
sid = "plc-e2e"
st = orchestrator.sess(sid)
fresh = student("s.sem=7 AND s.dept='CSE' AND s.cgpa>=7.5 AND s.backlogs=0 AND m.entry='Regular' AND m.tenth>=62 "
                "AND m.twelfth>=62 AND s.usn NOT IN (?, ?)", good["usn"], other["usn"])
FS = as_student(fresh["usn"])
st["user"] = FS
cd = seeded[3]
out = orchestrator.handle(con, f"Am I eligible for {cd['company']}?", sid, "student", actor=FS["id"])
tbl = next((b for b in out["blocks"] if b.get("type") == "table" and b.get("columns", [""])[0] == "Criterion"), None)
check("7b 'am I eligible?' answers criterion by criterion from the records",
      tbl and any(r[0] == "CGPA" and r[2] == f"{fresh['cgpa']:.2f}" for r in tbl["rows"]), str(out["blocks"])[:160])
orchestrator.handle(con, f"Apply for the {cd['company']} drive", sid, "student", actor=FS["id"])
staged = (st.get("pending") or {}).get("tool")
orchestrator.handle(con, "yes", sid, "student", actor=FS["id"])
check("7c end to end on the rule engine: proposed, then 'yes' files it",
      staged == "apply_to_drive" and one(con, "SELECT status FROM drive_registrations WHERE drive_id=? AND usn=?",
                                         (cd["id"], fresh["usn"])) is not None)
for text, want in ((f"Applications for the {co} drive", "drive_applications"),
                   (f"Close registration for the {co} drive", "set_drive_status"),
                   (f"Shortlist {fresh['usn']} for the {co} drive", "update_applications")):
    tool, a = campus.rule_route(con, "placement", text, {"usn": None})
    check(f"7d Registrar: '{text}' -> {want}", tool == want, f"{tool} {a}")
tool, a = campus.rule_route(con, "placement", f"Prepare the shortlist for the {co} drive", {"usn": None})
check("7e the existing shortlist sentence still reaches the shortlist", tool == "publish_drive_shortlist")

# ------------------------------------------------------------------ 8 · the pages and routes
print("\n8 · the pages and the routes")
print("-" * 78)
import app as APP                                                  # noqa: E402

portal_page = open(os.path.join(HERE, "static", "portal.html"), encoding="utf-8").read()
console = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
check("8a the student's Apply only stages, through the portal's one propose() call",
      "propose('apply_to_drive'" in portal_page and portal_page.count("apply_to_drive") == 1)
check("8b the console's placement forms post only to /api/placement/propose, which is the Registrar's alone",
      len(re.findall(r"api\('/api/placement/propose'", console)) == 1
      and auth.gate("/api/placement/propose", GS)[0] == 403 and auth.gate("/api/placement/propose", None)[0] == 401)
try:
    APP.placement_propose(SimpleNamespace(state=SimpleNamespace(user=auth.principal(con, "registrar")), headers={},
                                          cookies={}), {"tool": "apply_to_drive", "args": {}, "session": "t"})
    ok = False
except APP.BadInput:
    ok = True
check("8c the console route stages only the Registrar's three drive writes", ok and APP.PLACEMENT_PROPOSALS ==
      {"save_placement_drive", "set_drive_status", "update_applications"})
check("8d every placement read is a panel, and no placement write is", {"placement_drives", "drive_details",
                                                                       "drive_applications"} <= APP.PANELS
      and not set(PL.GATED_WRITES) & APP.PANELS)

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
