"""Standing faculty availability (#19): seeded consistently, honoured by the
solver, the substitution planner and make-up booking, and changed only through
a gated, re-read write. No server, no API cost.

    python tests/academics_test.py
"""
import datetime as dt
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_academics_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import academics                                                   # noqa: E402
import auth                                                        # noqa: E402
import nlu                                                         # noqa: E402
import orchestrator                                                # noqa: E402
import solver                                                      # noqa: E402
import tools                                                       # noqa: E402
from agents import SubstitutionAgent, one, rows, unavailable_at    # noqa: E402

FAILED, PASSED = [], 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


class T:
    def add(self, *a, **k):
        pass


con = db.connect()
sub = SubstitutionAgent()
MON = dt.date(2026, 9, 7)


def add_window(fid, day, a, b, reason="test"):
    cur = con.execute("""INSERT INTO faculty_availability(faculty,day,p_from,p_to,reason,status,created_by,
                         created_at) VALUES(?,?,?,?,?,'Active','test','')""", (fid, day, a, b, reason))
    con.commit()
    return cur.lastrowid


def drop_window(wid):
    con.execute("DELETE FROM faculty_availability WHERE id=?", (wid,))
    con.commit()


# ------------------------------------------------------------------ 1 · seed
print("\n1 · the seeded windows")
print("-" * 78)
seed = rows(con, "SELECT * FROM faculty_availability")
check("1a six standing windows are seeded, each inside the college day",
      len(seed) == 6 and all(r["status"] == "Active" and 1 <= r["p_from"] <= r["p_to"] <= 7 for r in seed),
      str([(r["faculty"], r["day"], r["p_from"], r["p_to"]) for r in seed]))
check("1b the seeded timetable already honours every one of them",
      not [p for p in solver.verify(con) if p.startswith("UNAVAILABLE")])
academics.ensure(con)
check("1c ensure() is idempotent", one(con, "SELECT COUNT(*) c FROM faculty_availability")["c"] == 6)
check("1d the solver reads them as slots",
      sum(len(v) for v in solver.standing_unavailability(con).values())
      == sum(r["p_to"] - r["p_from"] + 1 for r in seed))

# ------------------------------------------------------------ 2 · the solver
print("\n2 · generation never places a class in a window, and verify says when one sits there")
print("-" * 78)
# Take a teacher's real teaching hour and declare it a standing window: the
# committed timetable now breaks it, and a rebuild must not.
t = one(con, """SELECT * FROM timetable WHERE faculty IS NOT NULL AND kind='T' AND day='Tue' AND period=2
                ORDER BY faculty LIMIT 1""")
wid = add_window(t["faculty"], "Tue", 2, 3, "constructed")
scope = dict(scope="class", dept=t["dept"], sem=t["sem"], section=t["section"])
bad = [p for p in solver.verify(con, **scope) if p.startswith("UNAVAILABLE")]
check("2a verify reports a committed class sitting inside a window", bad and t["faculty"] in bad[0], str(bad))
# 2b must not pass by luck: solve once WITHOUT the window, take an hour the
# teacher really lands in, make exactly that hour a window, and solve again with
# the same seed. A solver that ignored windows would reproduce the same booking.
drop_window(wid)
free = solver.run(solver.build_input(con, **scope, seed=7))
lands = next(b for b in free["bookings"] if b["faculty"] == t["faculty"])
wid2 = add_window(t["faculty"], lands["day"], lands["period"], lands["period"], "constructed")
res = solver.run(solver.build_input(con, **scope, seed=7))
inside = [b for b in res["bookings"] if b["faculty"] == t["faculty"] and b["day"] == lands["day"]
          and b["period"] == lands["period"]]
check("2b the same seed that put them there without the window keeps them out with it", res["ok"] and not inside,
      f"ok={res['ok']} {lands['day']} P{lands['period']} inside={inside}")
drop_window(wid2)
wid = add_window(t["faculty"], "Tue", 2, 3, "constructed")
wid3 = add_window(t["faculty"], lands["day"], lands["period"], lands["period"], "constructed")
res = solver.run(solver.build_input(con, **scope, seed=7))
solver.apply(con, res, **scope)
check("2c ...and once committed, verify is clean for it",
      res["ok"] and not [p for p in solver.verify(con, **scope) if p.startswith("UNAVAILABLE")])
drop_window(wid)
drop_window(wid3)

# --------------------------------------------------------- 3 · the planners
print("\n3 · no substitute class and no make-up inside a window")
print("-" * 78)
fac = rows(con, """SELECT faculty, COUNT(*) n FROM timetable WHERE day='Mon' AND faculty IS NOT NULL
                   GROUP BY faculty ORDER BY n DESC, faculty""")
leg = None
for r in fac:
    f = one(con, "SELECT * FROM faculty WHERE id=?", (r["faculty"],))
    plans, _ = sub.build_plans(con, f, MON, T())
    a = next((p for p in plans if p["code"] == "A"), None)
    leg = next((l for l in (a or {}).get("legs", []) if l.get("to_id") and len(l["periods"]) == 1), None)
    if leg:
        break
wid = add_window(leg["to_id"], "Mon", leg["period"], leg["period"], "constructed")
check("3a busy_faculty counts a teacher inside their window as busy",
      leg["to_id"] in sub.busy_faculty(con, "Mon", leg["period"], MON.isoformat()))
plans2, _ = sub.build_plans(con, f, MON, T())
again = [l for p in plans2 for l in p["legs"]
         if l.get("to_id") == leg["to_id"] and l["period"] == leg["period"] and l["action"] != "MAKEUP"]
check("3b the substitute chosen before is not offered that hour once it is a window", not again,
      f"{leg['to_id']} P{leg['period']}: {again[:1]}")
slot = {"dept": "ZZ", "sem": 9, "section": "Z", "faculty": leg["to_id"], "period": leg["period"],
        "periods": [leg["period"]], "room": None}
check("3c no make-up is booked for them inside it",
      sub._busy_at(con, slot, "Mon", MON.isoformat(), leg["period"], "\x00"))
drop_window(wid)
check("3d with the window gone they are free again", leg["to_id"] not in unavailable_at(con, "Mon", leg["period"]))

# ----------------------------------------------------------- 4 · the write
print("\n4 · recording a window: proposed, approved, re-read")
print("-" * 78)
busy = one(con, """SELECT faculty, COUNT(*) n FROM timetable WHERE day='Thu' AND period BETWEEN 5 AND 7
                   AND faculty IS NOT NULL GROUP BY faculty ORDER BY n DESC, faculty LIMIT 1""")
S = {"pending": None, "ctx": {}}
before = one(con, "SELECT COUNT(*) c FROM faculty_availability")["c"]
args = {"faculty": busy["faculty"], "day": "Thursday", "periods": "afternoon", "reason": "BoS"}
r = tools.execute(con, S, "record it", "set_faculty_availability", dict(args))
check("4a without approval it proposes, writes nothing, and names the classes the window would cover",
      "BLOCKED" in r["data"] and one(con, "SELECT COUNT(*) c FROM faculty_availability")["c"] == before
      and any("fall inside it" in (b.get("title") or "") for b in r["blocks"]), str(r["data"])[:160])
r = tools.execute(con, S, "yes", "set_faculty_availability", dict(args))
v = [x for x in r.get("trace", []) if (x[1] if isinstance(x, (list, tuple)) else x.get("action")) == "verify"]
check("4b approved in the admin's own turn, it commits, re-reads, and says the classes still inside remain",
      r["data"].get("recorded") is True and r["data"]["classes_inside"] == busy["n"]
      and v and (v[0][3] if isinstance(v[0], (list, tuple)) else v[0]["status"]) == "ok", str(r["data"]))
check("4c the audit ledger has it", one(con, "SELECT 1 FROM audit WHERE action='availability.add'") is not None)
r = tools.execute(con, S, "yes", "set_faculty_availability",
                  {"faculty": busy["faculty"], "day": "Thu", "periods": "P5-P7", "remove": True})
check("4d an approved removal lifts the window, re-read as no longer active",
      r["data"].get("removed") == 1
      and not one(con, "SELECT 1 FROM faculty_availability WHERE faculty=? AND day='Thu' AND status='Active'",
                  (busy["faculty"],)))
check("4e an unknown teacher or a missing weekday is an error, not a guess",
      "error" in tools.execute(con, {"pending": None}, "", "set_faculty_availability",
                               {"faculty": "Dr. Nobody Atall", "day": "Mon", "periods": "P1"})["data"]
      and "error" in tools.execute(con, {"pending": None}, "", "set_faculty_availability",
                                   {"faculty": busy["faculty"], "day": "", "periods": "P1"})["data"])

# ------------------------------------------------- 5 · routing and who may see it
print("\n5 · a standing window is not an absence; and each role sees only its own")
print("-" * 78)
cls = lambda q: (nlu.classify(q) or [("", 0)])[0][0]
check("5a 'not available every Friday afternoon' is a standing window",
      cls("Prof. Vivek Rao is not available every Friday afternoon") == "availability")
check("5b 'absent on Friday' is still an absence", cls("Prof. Vivek Rao is absent on Friday") == "absence.cover")
check("5c 'unavailable on Fridays' (plural) is standing", cls("Dr. Sneha Upadhyaya is unavailable on Fridays")
      == "availability")
out = orchestrator.handle(con, "Prof. Vivek Rao is not available every Friday afternoon because of BoS meetings",
                          "a5", "admin", "registrar")
check("5d the rule engine proposes it through the same tool", (orchestrator.SESS.get("a5") or {}).get("pending", {})
      .get("tool") == "set_faculty_availability", str(orchestrator.SESS.get("a5"))[:160])
w_fac = seed[0]["faculty"]
P = auth.principal(con, w_fac)
r = tools.execute(con, {"pending": None, "user": P}, "", "faculty_availability", {})
check("5e a teacher sees only their own windows", r["data"]["count"] >= 1
      and all(w["faculty_id"] == w_fac for w in r["data"]["windows"]), str(r["data"])[:160])
hod = one(con, "SELECT id, dept FROM faculty WHERE is_hod=1 ORDER BY id")
r = tools.execute(con, {"pending": None, "user": auth.principal(con, hod["id"])}, "", "faculty_availability", {})
check("5f a HOD sees their department's", all(w["dept"] == hod["dept"] for w in r["data"]["windows"]))
stu = one(con, "SELECT usn FROM students ORDER BY usn")["usn"]
check("5g a student is refused", "DENIED" in tools.execute(con, {"pending": None, "user": auth.principal(con, stu)},
                                                           "", "faculty_availability", {})["data"])
check("5h nobody but the Registrar may record one",
      "DENIED" in tools.execute(con, {"pending": None, "user": auth.principal(con, hod["id"])}, "yes",
                                "set_faculty_availability", {"faculty": hod["id"], "day": "Mon",
                                                             "periods": "P1"})["data"])

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f_ in FAILED:
    print("  ✘", f_)
con.close()
sys.exit(1 if FAILED else 0)
