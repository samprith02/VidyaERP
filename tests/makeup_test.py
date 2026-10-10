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

# A stale proposal: plan an absence, let someone else book its make-up slot, then
# apply. Two honest outcomes, and the seed decides which a given leg meets: another
# free hour that week is booked instead, or - a semester-3 class whose only make-up
# hours are Saturday afternoon, already taken - the leg says it is NOT scheduled.
# Each is found in the data rather than assumed of an arbitrary teacher (#20 moved
# the seed, and the first teacher this used to pick now has no hour left).
cases = {}
for t in same_class[1:] + teachers[5:]:
    f3, ps3 = plans_for(t, MON)
    c3 = plan(ps3, "C")
    leg3 = next((l for l in c3["legs"] if l.get("makeup")), None) if c3 else None
    if not leg3:
        continue
    m = leg3["makeup"]
    con.execute("""INSERT INTO makeup_sessions(date,day,period,dept,sem,section,subject,faculty,room,plan_ref,status,
                   created_at,created_by,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (m["date"], m["day"], m["period"], leg3["dept"], leg3["sem"], leg3["class"][-1], "X", "F999",
                 m["room"], "squatter", "Scheduled", "now", "test", "someone else got there first"))
    alt = sub.find_makeup(con, {"dept": leg3["dept"], "sem": leg3["sem"], "section": leg3["class"][-1],
                                "faculty": f3["id"], "period": leg3["period"], "periods": leg3["periods"],
                                "room": leg3.get("room"), "kind": "L" if len(leg3["periods"]) > 1 else "T"},
                          dt.date.fromisoformat(m["date"]) - dt.timedelta(days=1))
    key = "rebooked" if alt else "none left"
    if key not in cases:
        sub.apply_plan(con, c3, f3, MON)
        cases[key] = (m, leg3, [r for r in booked(c3["id"]) if r["subject"] == leg3["subject"]],
                      [x["reason"] for x in rows(con, "SELECT reason FROM overrides WHERE plan_ref=? AND slot_id=?",
                                                 (c3["id"], leg3["slot_id"]))])
    con.execute("DELETE FROM makeup_sessions WHERE plan_ref='squatter'")
    con.commit()
    if len(cases) == 2:
        break
m, leg3, mine, _why = cases.get("rebooked", ({}, {}, [], []))
check("2c a slot taken between proposal and apply is rebooked, not double-booked",
      mine and not any(r["date"] == m["date"] and r["period"] == m["period"] for r in mine), str(mine[:2]))
m, leg3, mine, why = cases.get("none left", ({}, {}, [], []))
check("2d with no other hour left that week, it is not booked on the taken hour and says it is not scheduled",
      m and not mine and why and all("NOT yet scheduled" in x for x in why), str((mine[:1], why)))

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
# a teacher with nothing at all at that hour, read straight from the tables; the
# make-ups the sections above booked have taken some Saturday afternoons (#20)
t54 = next(t for t in tue_teachers[5:]
           if not one(con, "SELECT 1 FROM makeup_sessions WHERE status='Scheduled' AND faculty=? AND date=? AND period=6",
                      (t, sat.isoformat()))
           and not one(con, "SELECT 1 FROM faculty_availability WHERE status='Active' AND faculty=? AND day='Sat' "
                            "AND 6 BETWEEN p_from AND p_to", (t,)))
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

# --------------------------------------------- 8 · a reset is a clean slate (#64)
print("\n8 · a reset rebuilds bookings and standing windows from the seed (#64)")
print("-" * 78)
import academics                                                   # noqa: E402

con.execute("""INSERT INTO makeup_sessions(date,day,period,dept,sem,section,subject,faculty,room,plan_ref,status,
               created_at,created_by,reason) VALUES('2026-09-12','Sat',5,'CSE',5,'A','X','F001','SB-102',
               'orphan','Scheduled','','test','t')""")
con.execute("INSERT INTO faculty_availability(faculty,day,p_from,p_to,reason,status) "
            "VALUES('F002','Mon',7,7,'reset probe','Active')")
seeded = [tuple(r) for r in con.execute("SELECT * FROM faculty_availability WHERE created_by='seed' ORDER BY id")]
con.execute("UPDATE faculty_availability SET status='Removed' WHERE created_by='seed'")
con.commit()
con.close()
db.seed(force=True)
con = db.connect()
ensure_makeups(con)
# Defect: SCHEMA dropped every table but these two, so a booking whose plan the
# reset had deleted still held its teacher - and could never be undone.
check("8a no make-up booking survives a reset", one(con, "SELECT COUNT(*) c FROM makeup_sessions")["c"] == 0,
      str(rows(con, "SELECT plan_ref, faculty, date FROM makeup_sessions")))
check("8b so the teacher it named is free again", "F001" not in sub.busy_faculty(con, "Sat", 5, "2026-09-12"))
check("8c standing windows are exactly the seeded ones again",
      [tuple(r) for r in con.execute("SELECT * FROM faculty_availability ORDER BY id")] == seeded,
      str([tuple(r) for r in con.execute("SELECT faculty, day, reason, status FROM faculty_availability")]))

# ------------------------------- 9 · activity hours: a substitute, or released (#130)
print("\n9 · an absent teacher's activity hours are supervised or released, never made up (#130)")
print("-" * 78)
# Defect: plan C booked "Make-up for ACT-PT missed on Mon P6", holding a teacher,
# a room and the class for a Saturday hour. The owner's decision: substitute only.
ACT = {r["subject"] for r in rows(con, "SELECT DISTINCT subject FROM timetable WHERE kind='A'")}
sweep = []
for d in (MON + dt.timedelta(days=i) for i in range(6)):
    for r in rows(con, "SELECT DISTINCT faculty FROM timetable WHERE day=? AND faculty IS NOT NULL "
                       "ORDER BY faculty", (day_of(d),)):
        f, ps = plans_for(r["faculty"], d)
        if ps:
            sweep.append((f, d, ps))
act_legs = [l for _f, _d, ps in sweep for p in ps for l in p["legs"] if l["subject"] in ACT]
check("9a no activity hour is ever swapped or made up, in any plan of any absence",
      len(act_legs) > 100 and all(l["action"] in ("SUBSTITUTE", "RELEASED") and not l.get("makeup")
                                  for l in act_legs),
      f"{len(act_legs)} activity legs; " + str(sorted({l['action'] for l in act_legs})))
sig = lambda p: [(l["slot_id"], l["action"], l.get("to_id")) for l in p["legs"] if l["subject"] in ACT]
check("9b an absence's activity hours are identical in every plan, so they cannot pick the winner",
      all(sig(p) == sig(ps[0]) for _f, _d, ps in sweep for p in ps))
only = [(f, d, ps) for f, d, ps in sweep if all(l["subject"] in ACT for l in ps[0]["legs"])]
check("9c an absence of only activity hours is one card, and it says B and C would commit the same",
      len(only) >= 5 and all(len(ps) == 1 and ps[0]["also_commits"] == ["B", "C"] for _f, _d, ps in only),
      f"{len(only)} such absences; " + str([(f['id'], [p['code'] for p in ps]) for f, _d, ps in only
                                            if len(ps) != 1][:3]))
check("9d every card with an activity hour says how activities are handled",
      all("never swapped or made up" in p["summary"] for _f, _d, ps in sweep for p in ps
          if any(l["subject"] in ACT for l in p["legs"])))
check("9e no card claims 'zero substitution' or 'no extra hour for anyone' while it assigns a substitute",
      not [p["code"] for _f, _d, ps in sweep for p in ps
           if any(l["action"] == "SUBSTITUTE" for l in p["legs"])
           and {"Zero substitution burden", "No extra teaching hour for anyone — the partner's class only moves"}
           & set(p["pros"])])


class NobodyFree(SubstitutionAgent):
    """Constructed: the seed always has someone free to supervise an activity,
    so the released path is exercised by taking every candidate away."""
    def rank_candidates(self, con, slot, date_iso, already_used):
        return [] if slot["subject"] in ACT else super().rank_candidates(con, slot, date_iso, already_used)


f, d, _ps = next((f, d, ps) for f, d, ps in sweep
                 if any(l["subject"] in ACT for l in ps[0]["legs"])
                 and any(l["subject"] not in ACT for l in ps[0]["legs"]))
nf = NobodyFree()
pc = next(p for p in nf.build_plans(con, f, d, T())[0] if p["code"] == "C")
rel = [l for l in pc["legs"] if l["subject"] in ACT]
check("9f with nobody free, an activity hour is RELEASED, never a make-up",
      rel and all(l["action"] == "RELEASED" and not l.get("to_id") and not l.get("makeup") for l in rel),
      str([(l["subject"], l["action"]) for l in rel]))
nf.apply_plan(con, pc, f, d)
v = nf.verify_plan(con, pc["id"], d.isoformat(), nf.last_commit)
ids = [sid for l in rel for sid in l["slot_ids"]]
ov = rows(con, f"SELECT * FROM overrides WHERE plan_ref=? AND slot_id IN ({','.join('?' * len(ids))})",
          (pc["id"], *ids))
check("9g ...and is recorded as released: a VACATED row per period, nobody assigned",
      len(ov) == len(ids) and all(o["action"] == "VACATED" and o["new_faculty"] is None for o in ov),
      str([(o["action"], o["new_faculty"]) for o in ov]))
check("9h ...with no make-up booked for it, while the class hours keep theirs",
      not [m for m in booked(pc["id"]) if m["subject"] in ACT]
      and len(booked(pc["id"])) == sum(len(l["makeup"]["periods"]) for l in pc["legs"] if l.get("makeup")),
      str([(m["subject"], m["period"]) for m in booked(pc["id"])]))
check("9i the re-read finds the commit sound", v["ok"], str(v["problems"]))
r0 = one(con, "SELECT * FROM timetable WHERE id=?", (ids[0],))
cell = TimetableAgent().class_grid(con, r0["dept"], r0["sem"], r0["section"], d.isoformat())["cells"][
    f"{r0['day']}-{r0['period']}"]
check("9j the day's grid shows the activity hour released",
      ((cell.get("override") or {}).get("released")) is True, str(cell.get("override")))
from agents import NotifyAgent                                         # noqa: E402
body = " ".join(m["body"] for m in NotifyAgent().draft_absence(con, f, d, pc))
check("9k the class is told it is released and not made up",
      all(f"P{l['period']} {l['subject']} released (not made up)" in body for l in rel), body[:200])
sub.revert(con, pc["id"])
check("9l undo clears it", not rows(con, "SELECT 1 FROM overrides WHERE plan_ref=? AND status='Applied'",
                                     (pc["id"],)) and not booked(pc["id"]))

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
