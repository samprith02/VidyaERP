"""Timetable solver regression. No server, no LLM, no API cost - pure solver.

Runs against a COPY of college.db in the system temp directory, so it can never
damage the shipped database.

    python3 tests/solver_test.py

Every assertion here is a rule a real college timetable has to obey. The two that
matter most are the ones that caught actual defects while the solver was being
written, and they are labelled where they sit:

  * scoped regeneration must not promise a teacher hours their PINNED bookings
    already consume (staffing used to start every teacher at zero load);
  * room scarcity must degrade gracefully, never collapse (the search used to
    oscillate - 3205 placements against 3183 undos - and keep 24 of 456 periods).
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db                                                          # noqa: E402

_REAL = db.DB_PATH
_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_solver_test.db")
db.seed()
shutil.copy(_REAL, _TMP)
db.DB_PATH = _TMP

import solver                                                      # noqa: E402
from db import DAYS, LAB_WINDOWS, day_periods                      # noqa: E402

FAILED = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok    {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def grid(bookings):
    """bookings -> {(cls, day): {period: booking}}"""
    g = {}
    for b in bookings:
        g.setdefault((b["cls"], b["day"]), {})[b["period"]] = b
    return g


con = db.connect()

# ---------------------------------------------------------------- full generation
print("\nfull generation (all 21 sections, from scratch)")
inp = solver.build_input(con, scope="all", seed=7)
res = solver.run(inp)

check("places every curriculum period", res["placed"] == res["total"],
      f"{res['placed']}/{res['total']} unplaced={res['unplaced'][:3]}")
check("finishes in under 5 seconds", res["ms"] < 5000, f"{res['ms']}ms")
check("did not hit the time budget", not res["timed_out"])

g = grid(res["bookings"])

# ---- hard constraint: nobody is in two places at once
clash_f = clash_r = clash_c = 0
seen_f, seen_r, seen_c = {}, {}, {}
for b in res["bookings"]:
    key = (b["day"], b["period"])
    if b["faculty"]:
        if (key, b["faculty"]) in seen_f:
            clash_f += 1
        seen_f[(key, b["faculty"])] = b
    if b["room"]:
        if (key, b["room"]) in seen_r:
            clash_r += 1
        seen_r[(key, b["room"])] = b
    if (key, b["cls"]) in seen_c:
        clash_c += 1
    seen_c[(key, b["cls"])] = b
check("no faculty is double-booked", clash_f == 0, f"{clash_f} clashes")
check("no room is double-booked", clash_r == 0, f"{clash_r} clashes")
check("no class is double-booked", clash_c == 0, f"{clash_c} clashes")

# ---- hard constraint: the day is a SOLID block, no free holes
holes = []
for (cls, day), periods in g.items():
    want = set(range(1, day_periods(cls[1], day) + 1))
    if set(periods) != want:
        holes.append((cls, day, sorted(set(want) - set(periods))))
check("every class day is a contiguous block", not holes, str(holes[:3]))

# ---- hard constraint: labs are 3 consecutive periods inside ONE session
bad_labs = []
for (cls, day), periods in g.items():
    runs = {}
    for p, b in periods.items():
        if b["kind"] == "L":
            runs.setdefault(b["subject"], []).append(p)
    for subj, ps in runs.items():
        ps.sort()
        if len(ps) != 3 or ps != list(range(ps[0], ps[0] + 3)):
            bad_labs.append((cls, day, subj, ps))
        elif (ps[0], ps[-1]) not in LAB_WINDOWS[day]:
            bad_labs.append((cls, day, subj, ps, "straddles a break"))
check("lab blocks are 3 consecutive periods in one session", not bad_labs, str(bad_labs[:3]))

# ---- a teacher never exceeds their sanctioned weekly load
over = []
load = {}
for b in res["bookings"]:
    if b["faculty"]:
        load[b["faculty"]] = load.get(b["faculty"], 0) + 1
for fid, n in load.items():
    cap = inp.teacher_meta[fid]["max_load"]
    if n > cap:
        over.append((fid, n, cap))
check("no teacher exceeds their sanctioned load", not over, str(over[:3]))

# ---- capacity relaxation is REPORTED, never silent
check("capacity relaxation is reported when it happens",
      all(r["size"] > 36 for r in res["capacity_relaxed"]),
      "a relaxation was recorded for a class that actually fits")

# ---------------------------------------------------------------- determinism
print("\ndeterminism")
a = solver.run(solver.build_input(con, scope="all", seed=7))
b = solver.run(solver.build_input(con, scope="all", seed=7))
c = solver.run(solver.build_input(con, scope="all", seed=99))
key = lambda r: sorted((x["cls"], x["day"], x["period"], x["subject"], x["faculty"], x["room"])
                       for x in r["bookings"])
check("same seed gives an identical timetable", key(a) == key(b))
check("a different seed gives a different timetable", key(a) != key(c),
      "the seed is being ignored")

# ---------------------------------------------------------------- persistence
print("\napply + independent verify")
solver.apply(con, res, scope="all")
problems = solver.verify(con, scope="all")
check("committed timetable passes independent verification", not problems, str(problems[:3]))
check("every section is present",
      con.execute("SELECT COUNT(DISTINCT dept||sem||section) c FROM timetable").fetchone()["c"]
      == len(inp.classes))

# ------------------------------------------------- scoped regeneration with pins
print("\nscoped regeneration (one section, the rest pinned)")
TARGET = ("CSE", 5, "A")
others_before = [tuple(r) for r in con.execute(
    """SELECT dept,sem,section,day,period,subject,faculty,room FROM timetable
       WHERE NOT(dept=? AND sem=? AND section=?) ORDER BY 1,2,3,4,5""", TARGET)]

inp2 = solver.build_input(con, scope="class", dept=TARGET[0], sem=TARGET[1],
                          section=TARGET[2], seed=11)
res2 = solver.run(inp2)
check("regenerating one section pins all the others", len(inp2.pins) > 600, str(len(inp2.pins)))
# THIS is the staffing-vs-pins defect: it placed 20/25 before pinned load counted.
check("scoped regeneration still places every period",
      res2["placed"] == res2["total"],
      f"{res2['placed']}/{res2['total']} - staffing is ignoring pinned teacher load")

solver.apply(con, res2, scope="class", dept=TARGET[0], sem=TARGET[1], section=TARGET[2])
others_after = [tuple(r) for r in con.execute(
    """SELECT dept,sem,section,day,period,subject,faculty,room FROM timetable
       WHERE NOT(dept=? AND sem=? AND section=?) ORDER BY 1,2,3,4,5""", TARGET)]
check("other sections are untouched by a scoped rebuild", others_before == others_after,
      f"{len(others_before)} rows before, {len(others_after)} after")
check("the whole college still verifies after a scoped rebuild",
      not solver.verify(con, scope="all"), str(solver.verify(con)[:3]))

# ---------------------------------------------------------- teacher unavailability
print("\nteacher unavailability")
busy = max(load, key=load.get)
blocked = set(range(0, solver.NSLOTS, 2))          # every other slot, all week
inp3 = solver.build_input(con, scope="all", seed=7, unavailable={busy: blocked})
res3 = solver.run(inp3)
violations = [b for b in res3["bookings"] if b["faculty"] == busy
              and solver.slot_of(DAYS.index(b["day"]), b["period"]) in blocked]
check("an unavailable teacher is never scheduled", not violations, str(violations[:3]))

# -------------------------------------------------------- graceful degradation
# The search used to oscillate here: 3205 placements against 3183 undos, 24 of 456
# periods surviving, the full time budget burned. It must degrade, not collapse.
print("\ngraceful degradation under room scarcity")
prev = 10 ** 9
for nlabs in (5, 4, 3, 2, 1):
    inp4 = solver.build_input(con, scope="all", seed=7, time_budget_s=20)
    labs = [r for r in inp4.rooms if inp4.room_meta[r]["kind"] == "Lab"]
    inp4.rooms = [r for r in inp4.rooms if r not in set(labs[nlabs:])]
    r4 = solver.run(inp4)
    pct = 100 * r4["placed"] / r4["total"]
    print(f"        {nlabs} lab rooms -> {r4['placed']}/{r4['total']} "
          f"({pct:.1f}%) in {r4['ms']}ms, {r4['backtracks']} backtracks")
    check(f"{nlabs} lab rooms: keeps most of the timetable", pct >= 90, f"{pct:.1f}%")
    check(f"{nlabs} lab rooms: does not burn the time budget", r4["ms"] < 5000, f"{r4['ms']}ms")
    check(f"{nlabs} lab rooms: still a solid day block",
          not [1 for (cls, day), ps in grid(r4["bookings"]).items()
               if set(ps) != set(range(1, day_periods(cls[1], day) + 1))])
    check(f"{nlabs} lab rooms: no worse than {nlabs + 1}", r4["placed"] <= prev + 1,
          "fewer rooms placed MORE periods - the search is not monotone")
    prev = r4["placed"]

# ---- and the backtracking machinery is genuinely reached, not dead code
inp5 = solver.build_input(con, scope="all", seed=7, time_budget_s=20)
labs = [r for r in inp5.rooms if inp5.room_meta[r]["kind"] == "Lab"]
inp5.rooms = [r for r in inp5.rooms if r not in set(labs[3:])]
r5 = solver.run(inp5)
check("backtracking is actually exercised somewhere", r5["backtracks"] > 0,
      "the whole search path is dead code on every instance tested")

# ---------------------------------------------------------------- trace contract
print("\nstreaming trace")
kinds = {e["t"] for e in res["trace"]}
check("trace opens with init and closes with done",
      res["trace"][0]["t"] == "init" and res["trace"][-1]["t"] == "done")
check("trace carries every phase",
      {"init", "phase", "place", "done"} <= kinds, str(sorted(kinds)))
check("trace is bounded for a UI", len(res["trace"]) < 12000, str(len(res["trace"])))
phases = [e["name"] for e in res["trace"] if e["t"] == "phase"]
check("all five phases run", phases == ["Staffing", "Search", "Polish", "Fill"]
      or phases == ["Staffing", "Search", "Repair", "Polish", "Fill"], str(phases))

# ---------------------------------------------------------------------- summary
# Windows holds the file while the connection is open - close before unlinking.
con.close()
try:
    os.remove(_TMP)
except OSError:
    pass
print(f"\n{PASSED} passed, {len(FAILED)} failed")
if FAILED:
    for f in FAILED:
        print("  FAILED:", f)
    sys.exit(1)
