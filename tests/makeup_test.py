"""Make-up classes are booked, not just promised (#18). No server, no LLM, no API cost.

    python tests/makeup_test.py

Before #18 a coverage plan RECORDED a make-up slot and nothing held it: a
second absence could be promised the same hour, the same teacher or the same
room, and undoing a plan left nothing to release. Now applying a plan books a
dated session, find_makeup treats bookings as occupied, and undo releases them.

The strongest check here is 3a: after many plans are applied, re-read EVERY
booked period against the weekly timetable and against every other booking,
for the batch, the teacher and the room.
"""
import datetime as dt
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_makeup_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import auth                                                        # noqa: E402
import tools                                                       # noqa: E402
from agents import SubstitutionAgent, TimetableAgent, one, rows, day_of, ensure_makeups  # noqa: E402

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
ensure_makeups(con)
sub = SubstitutionAgent()
TODAY = dt.date(2026, 9, 4)
MON = TODAY + dt.timedelta(days=3)


class T:
    def add(self, *a, **k):
        pass


def plans_for(fid, date):
    f = one(con, "SELECT * FROM faculty WHERE id=?", (fid,))
    return f, sub.build_plans(con, f, date, T())[0]


def plan(plans, code):
    return next((p for p in plans if p["code"] == code or code in p.get("also_commits", [])), None)


def booked(ref=None):
    q = "SELECT * FROM makeup_sessions WHERE status='Scheduled'"
    return rows(con, q + (" AND plan_ref=?" if ref else ""), (ref,) if ref else ())


# ------------------------------------------------ 1 · a plan books what it promises
print("\n1 · applying a plan books its make-ups")
print("-" * 78)
teachers = [r["faculty"] for r in rows(con, """SELECT faculty, COUNT(*) n FROM timetable WHERE day='Mon'
                                               AND faculty IS NOT NULL GROUP BY faculty ORDER BY n DESC, faculty""")]
fac, ps = plans_for(teachers[0], MON)
pc = plan(ps, "C")
promised = sum(len((l.get("makeup") or {}).get("periods") or []) for l in pc["legs"] if l.get("makeup"))
check("1a plan C promises make-ups with a room and a period span",
      promised and all(l["makeup"].get("room") and l["makeup"].get("periods") for l in pc["legs"] if l.get("makeup")),
      json.dumps([l.get("makeup") for l in pc["legs"]])[:200])
sub.apply_plan(con, pc, fac, MON)
got = booked(pc["id"])
check("1b applying it books exactly the promised periods", len(got) == promised, f"{len(got)} vs {promised}")
check("1c each booking is dated after the absence, with the absent teacher and a room",
      all(r["date"] > MON.isoformat() and r["faculty"] == fac["id"] and r["room"] for r in got), str(got[:1]))
o = rows(con, "SELECT reason FROM overrides WHERE plan_ref=?", (pc["id"],))
# A teacher who misses two labs cannot make both up in one Saturday afternoon:
# the second leg honestly says so, rather than being given the same hour.
check("1d each override reason names its booked slot and room — or says plainly it is not scheduled",
      o and any(" in " in x["reason"] for x in o)
      and all(" in " in x["reason"] or "NOT yet scheduled" in x["reason"] for x in o), str(o))
lab = next((l for p in [plan(plans_for(t, MON)[1] or [], "C") for t in teachers[:40]] if p
            for l in p["legs"] if len(l["periods"]) > 1 and l.get("makeup")), None)
check("1e a missed 3-period lab is made up as 3 consecutive periods, not one",
      lab and len(lab["makeup"]["periods"]) == len(lab["periods"])
      and lab["makeup"]["periods"] == list(range(lab["makeup"]["period"], lab["makeup"]["period"] + len(lab["periods"]))),
      json.dumps(lab and lab["makeup"]))

# --------------------------------------------- 2 · a booking is occupied for the next plan
print("\n2 · a booked hour is not promised twice")
print("-" * 78)
first = got[0]
same_class = [t for t in teachers[1:] if one(con, """SELECT 1 FROM timetable WHERE faculty=? AND day='Mon' AND
                                                      dept=? AND sem=? AND section=?""",
                                              (t, first["dept"], first["sem"], first["section"]))]
f2, ps2 = plans_for(same_class[0], MON)
c2 = plan(ps2, "C")
clash = [l for l in c2["legs"] if l.get("makeup") and l["class"] == f"{first['dept']}-{first['sem']}{first['section']}"
         and l["makeup"]["date"] == first["date"] and first["period"] in l["makeup"]["periods"]]
check("2a another absence in the same class is not offered the booked hour", not clash, json.dumps(clash)[:200])
slot = {"dept": first["dept"], "sem": first["sem"], "section": first["section"], "faculty": first["faculty"],
        "period": 1, "periods": [1], "room": first["room"]}
check("2b the booking's own plan can still see its hour (re-check at apply time excludes itself)",
      not sub._busy_at(con, slot, first["day"], first["date"], first["period"], first["plan_ref"])
      and sub._busy_at(con, slot, first["day"], first["date"], first["period"], "other-plan"))

# a stale proposal: plan the second absence, let someone else book its slot, then apply
f3, ps3 = plans_for(same_class[1] if len(same_class) > 1 else teachers[5], MON)
c3 = plan(ps3, "C")
leg3 = next(l for l in c3["legs"] if l.get("makeup"))
m = leg3["makeup"]
con.execute("""INSERT INTO makeup_sessions(date,day,period,dept,sem,section,subject,faculty,room,plan_ref,status,
               created_at,created_by,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (m["date"], m["day"], m["period"], leg3["dept"], leg3["sem"], leg3["class"][-1], "X", "F999",
             m["room"], "squatter", "Scheduled", "now", "test", "someone else got there first"))
con.commit()
sub.apply_plan(con, c3, f3, MON)
mine = [r for r in booked(c3["id"]) if r["subject"] == leg3["subject"]]
check("2c a slot taken between proposal and apply is rebooked, not double-booked",
      mine and not any(r["date"] == m["date"] and r["period"] == m["period"] for r in mine), str(mine[:2]))
con.execute("DELETE FROM makeup_sessions WHERE plan_ref='squatter'")
con.commit()

# ------------------------------------------------------ 3 · nothing is double-booked
print("\n3 · across many applied plans, nothing is double-booked")
print("-" * 78)
for t in teachers[6:30]:
    f, pp = plans_for(t, MON)
    if pp:
        sub.apply_plan(con, plan(pp, "C") or pp[0], f, MON)
allb = booked()
problems = []
for r in allb:
    if one(con, "SELECT 1 FROM timetable WHERE dept=? AND sem=? AND section=? AND day=? AND period=?",
           (r["dept"], r["sem"], r["section"], r["day"], r["period"])):
        problems.append(("class has a timetabled period", r["id"]))
    if one(con, "SELECT 1 FROM timetable WHERE faculty=? AND day=? AND period=?", (r["faculty"], r["day"], r["period"])):
        problems.append(("teacher has a timetabled period", r["id"]))
    if one(con, "SELECT 1 FROM timetable WHERE room=? AND day=? AND period=?", (r["room"], r["day"], r["period"])):
        problems.append(("room has a timetabled period", r["id"]))
for col in ("room", "faculty"):
    for d in rows(con, f"""SELECT {col}, date, period, COUNT(*) n FROM makeup_sessions WHERE status='Scheduled'
                           GROUP BY {col}, date, period HAVING n>1"""):
        problems.append((f"{col} booked twice", dict(d)))
for d in rows(con, """SELECT dept, sem, section, date, period, COUNT(*) n FROM makeup_sessions WHERE status='Scheduled'
                      GROUP BY dept, sem, section, date, period HAVING n>1"""):
    problems.append(("class booked twice", dict(d)))
check("3a every booked period is free for its class, teacher and room", len(allb) > 20 and not problems,
      f"{len(allb)} bookings · {problems[:3]}")
leave_clash = [r["id"] for r in allb if one(con, """SELECT 1 FROM leaves WHERE faculty=? AND status='Approved'
                                                   AND from_date<=? AND to_date>=?""", (r["faculty"], r["date"], r["date"]))]
check("3b no make-up is booked on a day its teacher is on approved leave", not leave_clash, str(leave_clash[:3]))

# -------------------------------------------------------------- 4 · undo releases
print("\n4 · undo releases the hours")
print("-" * 78)
before = len(booked(pc["id"]))
n = sub.revert(con, pc["id"])
check("4a revert cancels exactly that plan's bookings", n == before and not booked(pc["id"]), f"{n} vs {before}")
check("4b and reverts its overrides", not one(con, "SELECT 1 FROM overrides WHERE plan_ref=? AND status='Applied'",
                                              (pc["id"],)))
last = one(con, "SELECT plan_ref FROM overrides WHERE status='Applied' ORDER BY id DESC LIMIT 1")["plan_ref"]
k = len(booked(last))
r = tools.execute(con, {"pending": None}, "", "undo_last_change", {})
check("4c the undo tool goes through the same path", r["data"].get("makeups_cancelled") == k and not booked(last),
      str(r["data"]))

# ------------------------------------------------------------- 5 · where people see them
print("\n5 · they show where people look")
print("-" * 78)
b = booked()[0]
g = TimetableAgent().class_grid(con, b["dept"], b["sem"], b["section"], b["date"])
cell = g["cells"].get(f"{b['day']}-{b['period']}")
check("5a the class timetable for that week shows the make-up", cell and cell.get("kind") == "M"
      and cell.get("makeup", {}).get("date") == b["date"], str(cell))
g2 = TimetableAgent().class_grid(con, b["dept"], b["sem"], b["section"],
                                 (dt.date.fromisoformat(b["date"]) + dt.timedelta(days=14)).isoformat())
check("5b ...and not in a different week", g2["cells"].get(f"{b['day']}-{b['period']}", {}).get("kind") != "M")
stu = one(con, "SELECT usn FROM students WHERE dept=? AND sem=? AND section=? ORDER BY usn",
          (b["dept"], b["sem"], b["section"]))["usn"]
other = one(con, "SELECT usn FROM students WHERE NOT (dept=? AND sem=? AND section=?) ORDER BY usn",
            (b["dept"], b["sem"], b["section"]))["usn"]
S_stu = {"pending": None, "user": auth.principal(con, stu)}
r = tools.execute(con, S_stu, "", "makeup_schedule", {})
check("5c a student sees their own class's make-ups", r["data"]["count"] >= 1
      and all(x["class"] == f"{b['dept']}-{b['sem']}{b['section']}" for x in r["data"]["sessions"]), str(r["data"])[:160])
r = tools.execute(con, S_stu, "", "makeup_schedule", {"dept": "MECH" if b["dept"] != "MECH" else "CSE"})
check("5d ...and cannot ask for another department's", "DENIED" in r["data"])
S_fac = {"pending": None, "user": auth.principal(con, b["faculty"])}
r = tools.execute(con, S_fac, "", "makeup_schedule", {})
check("5e a teacher sees the make-ups they teach", r["data"]["count"] >= 1
      and all(x["teacher"] == one(con, "SELECT name FROM faculty WHERE id=?", (b["faculty"],))["name"]
              for x in r["data"]["sessions"]))
home = tools.execute(con, S_stu, "", "my_home", {})
check("5f a student's day lists the booked make-ups", any(x.get("title") == "Make-up classes booked"
                                                          for x in home["blocks"]))

# ------------------------------- 6 · a commit is reported on a re-read, not its own word
print("\n6 · a coverage commit is verified by re-reading it (#51), and make-ups hold their teacher (#54)")
print("-" * 78)
import orchestrator                                                # noqa: E402
TUE = MON + dt.timedelta(days=1)


def steps(tr):
    """Trace entries as (agent, action, status), whatever shape they arrive in."""
    out = []
    for t in tr or []:
        if isinstance(t, dict):
            out.append((t.get("agent"), t.get("action"), t.get("status", "ok")))
        else:
            out.append((t[0], t[1], t[3] if len(t) > 3 else "ok"))
    return out


# The tool path: what the LLM agent and MCP reach.
tue_teachers = [r["faculty"] for r in rows(con, """SELECT faculty, COUNT(*) n FROM timetable WHERE day='Tue'
                                                   AND faculty IS NOT NULL GROUP BY faculty ORDER BY n DESC, faculty""")]
f6, ps6 = plans_for(tue_teachers[0], TUE)
S6 = {"pending": {"kind": "absence_plans", "plans": ps6, "faculty": f6, "date": TUE.isoformat()}, "ctx": {}}
r = tools.execute(con, S6, "yes, apply plan A", "apply_coverage_plan", {"plan_code": "A"})
v6 = [x for x in steps(r.get("trace")) if x[1] == "verify"]
check("6a the tool commit reports verified=True from a re-read, with an Auditor verify step marked ok",
      r["data"].get("verified") is True and v6 == [("Auditor", "verify", "ok")], str(r["data"])[:200] + str(v6))

# The rule engine's own commit path.
f7 = one(con, "SELECT * FROM faculty WHERE id=?", (tue_teachers[1],))
orchestrator.handle(con, f"{f7['name']} is absent on {TUE.isoformat()}, arrange coverage", "mk6", "admin", "registrar")
out = orchestrator.handle(con, "apply plan A", "mk6", "admin", "registrar")
v7 = [x for x in steps(out.get("trace")) if x[1] == "verify"]
check("6b the rule engine's commit carries the same verify step, marked ok, and says it checked",
      v7 == [("Auditor", "verify", "ok")] and any("Checked after writing" in (b.get("md") or "")
                                                   for b in out["blocks"]), str(v7))

# The check has teeth: break the committed state and it must say so.
f8, ps8 = plans_for(tue_teachers[2], TUE)
p8 = plan(ps8, "C") if any(l.get("makeup") for l in plan(ps8, "C")["legs"]) else plan(ps8, "A")
s8 = SubstitutionAgent()
s8.apply_plan(con, p8, f8, TUE)
check("6c a clean commit verifies", s8.verify_plan(con, p8["id"], TUE.isoformat(), s8.last_commit)["ok"])
gone = one(con, "SELECT * FROM overrides WHERE plan_ref=? ORDER BY id", (p8["id"],))
con.execute("DELETE FROM overrides WHERE id=?", (gone["id"],))
v = s8.verify_plan(con, p8["id"], TUE.isoformat(), s8.last_commit)
check("6d a row the plan wrote that is no longer there is reported missing",
      not v["ok"] and any("missing" in x for x in v["problems"]), str(v["problems"]))
con.execute("""INSERT INTO overrides(date,slot_id,action,new_faculty,new_room,new_subject,reason,status,
               created_by,created_at,plan_ref) SELECT date,slot_id,action,new_faculty,new_room,new_subject,
               reason,status,created_by,created_at,plan_ref FROM overrides WHERE plan_ref=? LIMIT 1""", (p8["id"],))
v = s8.verify_plan(con, p8["id"], TUE.isoformat(), s8.last_commit)
check("6e a row the plan never made is reported, not counted as success",
      not v["ok"] and any("never made" in x for x in v["problems"]), str(v["problems"]))
mk = one(con, "SELECT * FROM makeup_sessions WHERE status='Scheduled' ORDER BY id")
con.execute("""INSERT INTO makeup_sessions(date,day,period,dept,sem,section,subject,faculty,room,plan_ref,status,
               created_at,created_by,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mk["date"], mk["day"], mk["period"], "XX", 9, "Z", "X", "F999", mk["room"], "intruder",
             "Scheduled", "", "test", "double-booked room"))
v = SubstitutionAgent().verify_plan(con, mk["plan_ref"], "1900-01-01", {"overrides": [], "makeups": [
    (r2["date"], r2["period"], r2["faculty"], r2["room"]) for r2 in booked(mk["plan_ref"])]})
check("6f a make-up whose room is booked twice at the same hour is reported",
      any("shares its room" in x for x in v["problems"]), str(v["problems"]))
con.execute("DELETE FROM makeup_sessions WHERE plan_ref='intruder'")
con.commit()

# #54, constructed: a teacher holding a make-up at an hour is not free then.
# Saturday P6 is outside every batch's day, so only the booking can make them busy.
sat = MON + dt.timedelta(days=5)
t54 = tue_teachers[5]
check("6g (#54) with nothing booked, the teacher is free at Sat P6",
      t54 not in sub.busy_faculty(con, "Sat", 6, sat.isoformat()))
con.execute("""INSERT INTO makeup_sessions(date,day,period,dept,sem,section,subject,faculty,room,plan_ref,status,
               created_at,created_by,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sat.isoformat(), "Sat", 6, "CSE", 5, "A", "X", t54, "SB-102", "t54", "Scheduled", "", "test", "t"))
check("6h (#54) once they hold a make-up then, busy_faculty counts them busy - so no plan can hand them a class",
      t54 in sub.busy_faculty(con, "Sat", 6, sat.isoformat()))
con.execute("DELETE FROM makeup_sessions WHERE plan_ref='t54'")
con.commit()

# ------------------------------ 7 · two different days in one instruction are asked about (#17)
print("\n7 · the absence flow asks when a weekday and a date disagree (#17)")
print("-" * 78)
f9 = one(con, "SELECT * FROM faculty WHERE id=?", (tue_teachers[3],))
before = one(con, "SELECT COUNT(*) c FROM overrides")["c"]
out = orchestrator.handle(con, f"{f9['name']} is absent on monday 15 sep, arrange coverage", "mk7", "admin",
                          "registrar")
text = " ".join(b.get("md") or "" for b in out["blocks"])
check("7a it asks which day, and plans nothing", "Which day" in text and not any(b.get("type") == "plans"
                                                                                   for b in out["blocks"]), text[:160])
check("7b both readings are one tap away", any("2026-09-15" in c for c in out.get("chips", []))
      and any("2026-09-07" in c for c in out.get("chips", [])), str(out.get("chips")))
out2 = orchestrator.handle(con, "apply plan A", "mk7", "admin", "registrar")
check("7c a 'yes' after the question commits nothing",
      one(con, "SELECT COUNT(*) c FROM overrides")["c"] == before, str([b.get("md") for b in out2["blocks"]])[:160])

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
