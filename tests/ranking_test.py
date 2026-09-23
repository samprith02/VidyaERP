"""Coverage-plan ranking. No server, no LLM, no API cost.

    python tests/ranking_test.py

Runs against a COPY of college.db in the system temp directory, so it can never
damage the shipped database.

--------------------------------------------------------------------------
What this suite exists to hold down
--------------------------------------------------------------------------
Every number on a plan card used to be one of three things:

    continuity          a per-strategy CONSTANT  (74 / 92 / 88)
    plan B confidence   71 + min(20, swaps*6)
    plan C confidence   a flat 66

None of them looked at the instance, so a plan could be ranked first on a
property nobody had measured. They are now computed from the timetable, the
expertise profiles and the calendar, and the assertions below are what stops
them quietly becoming constants again: a measurement that never varies across
40 different absences is a constant wearing a function's clothes.

Four assertions encode defects found by measurement while this was written, and
their comments say so. Leave them labelled:

  * a swap must not change the partner's teaching hours (the old SWAP wrote ONE
    override and silently handed the partner an extra hour, while the card
    claimed "no extra workload on any faculty");
  * a swap must never split a lab block (labs are 3 contiguous periods;
    CIVIL-3A Mon P5-P7 is the live example);
  * find_makeup must be able to succeed (it searched only within the batch's
    own day, and db.verify() asserts those are full - so Plan C reported 0%
    coverage for every absence ever put to it);
  * the rank shown must be the rank used (the sort key and the weights printed
    on the card have to be the same numbers).
"""
import datetime as dt
import os
import shutil
import sys
import tempfile

# The failure path prints U+2718, and Windows encodes a piped stdout as cp1252
# by default - which turns a FAILING run into a UnicodeEncodeError traceback
# instead of the name of the assertion that failed. Exit codes were always
# right, so CI never noticed; the person debugging locally got the wrong error
# at the worst moment. errors="replace" so an exotic console degrades to '?'
# rather than taking the suite down on its way to reporting a failure.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db                                                          # noqa: E402

_REAL = db.DB_PATH
_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_ranking_test.db")
db.seed()
shutil.copy(_REAL, _TMP)
db.DB_PATH = _TMP

import agents                                                      # noqa: E402
from agents import (AUTHENTICITY, CONTINUITY_WEIGHTS, RANK_WEIGHTS, SubstitutionAgent,
                    TimetableAgent, one, rows, timing_intact)      # noqa: E402
from nlu import TODAY, day_of                                      # noqa: E402

FAILED = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


con = db.connect()
SUB = SubstitutionAgent()


class Tr:
    def __init__(self): self.items = []
    def add(self, *a, **k): self.items.append(a)


DATE = next(TODAY + dt.timedelta(days=i) for i in range(1, 8)
            if day_of(TODAY + dt.timedelta(days=i)) != "Sun")
DAY = day_of(DATE)

# A broad sample, not one lucky absence: every measurement below is asserted to
# vary across these, which is the only way to tell a computation from a constant.
TEACHERS = rows(con, """SELECT f.* FROM faculty f JOIN timetable t ON t.faculty=f.id
                        WHERE t.day=? GROUP BY f.id ORDER BY COUNT(t.id) DESC LIMIT 40""", (DAY,))
SAMPLE = []
for _f in TEACHERS:
    _plans, _aff = SUB.build_plans(con, _f, DATE, Tr())
    if _plans:
        SAMPLE.append((_f, _plans, _aff))
assert len(SAMPLE) >= 20, f"need a real sample, got {len(SAMPLE)}"
print(f"\nsample: {len(SAMPLE)} absences on {DAY} {DATE}")


# =================================================== 1 · nothing is a constant
print("\n1 · the numbers are computed, not asserted")
print("-" * 78)

for code in ("A", "B", "C"):
    conts = {p["continuity"] for _f, ps, _a in SAMPLE for p in ps if p["code"] == code}
    confs = {p["confidence"] for _f, ps, _a in SAMPLE for p in ps if p["code"] == code}
    # The old code produced exactly ONE continuity per strategy (74/92/88) and,
    # for B and C, a confidence driven by nothing but a swap count.
    check(f"1a plan {code} continuity varies across absences", len(conts) > 1,
          f"only ever {conts}")
    check(f"1b plan {code} confidence varies across absences", len(confs) > 1,
          f"only ever {confs}")

check("1c continuity is not one of the old constants for every plan",
      not all(p["continuity"] in (74, 92, 88) for _f, ps, _a in SAMPLE for p in ps))

# Every plan must carry the evidence, not just the verdict.
check("1d every plan reports its measured components",
      all({"hours_preserved", "own_teacher", "timing_intact"} == set(p["components"])
          for _f, ps, _a in SAMPLE for p in ps))
check("1e every plan carries the weights it was ranked by",
      all(p["weights"] == RANK_WEIGHTS for _f, ps, _a in SAMPLE for p in ps))


# ================================================== 2 · the rank is the rank
print("\n2 · the rank shown is the rank used")
print("-" * 78)
# DEFECT THIS ENCODES: the weights live in one dict, are printed on the card,
# and are what the sort key uses. If any of the three drift apart, an admin is
# shown a justification for an order that was produced some other way.
worst = 0.0
for _f, ps, _a in SAMPLE:
    for p in ps:
        expect = (p["confidence"] * RANK_WEIGHTS["confidence"]
                  + p["continuity"] * RANK_WEIGHTS["continuity"])
        worst = max(worst, abs(expect - p["rank"]))
check("2a rank equals the weighted sum of the published numbers", worst < 1.0,
      f"largest disagreement {worst:.3f}")
check("2b plans are returned in rank order",
      all([p["rank"] for p in ps] == sorted((p["rank"] for p in ps), reverse=True)
          for _f, ps, _a in SAMPLE))
check("2c the weights sum to 1", abs(sum(RANK_WEIGHTS.values()) - 1.0) < 1e-9)
check("2d the continuity weights sum to 1", abs(sum(CONTINUITY_WEIGHTS.values()) - 1.0) < 1e-9)


# ============================================ 3 · continuity means something
print("\n3 · continuity tracks the three things it claims to measure")
print("-" * 78)

check("3a timing decays with slip and never goes negative",
      timing_intact(0) == 1.0 and timing_intact(1) == 0.5 and timing_intact(3) == 0.25
      and 0 < timing_intact(30) < 0.05)
check("3b timing is monotonically decreasing",
      all(timing_intact(i) > timing_intact(i + 1) for i in range(0, 10)))

# A deferred hour must never look as timely as one delivered in its own slot.
same_day = [p for _f, ps, _a in SAMPLE for p in ps
            if p["code"] == "A" and p["components"]["hours_preserved"] == 100]
deferred = [p for _f, ps, _a in SAMPLE for p in ps if p["code"] == "C"]
check("3c a make-up plan is never more timely than same-slot substitution",
      max((p["components"]["timing_intact"] for p in deferred), default=0)
      <= min((p["components"]["hours_preserved"] and p["components"]["timing_intact"]
              for p in same_day), default=100),
      f"C timing max {max((p['components']['timing_intact'] for p in deferred), default=0)}")
check("3d a make-up is delivered by the subject's own teacher",
      all(p["components"]["own_teacher"] == 100 for p in deferred
          if p["coverage"] == 100), "plan C should be 100% own-teacher when fully covered")

# Authenticity has to be ordered, or "delivered by its own teacher" is noise.
check("3e authenticity is strictly ordered",
      AUTHENTICITY["own"] > AUTHENTICITY["teaches_subject"] > AUTHENTICITY["expertise"]
      > AUTHENTICITY["same_dept"] > AUTHENTICITY["other_dept"] > AUTHENTICITY["none"])

# Synthetic legs: the only way to pin the ends of the scale deterministically.
_absent = SAMPLE[0][0]
_leg = dict(SAMPLE[0][1][0]["legs"][0])
_leg.update(action="UNCOVERED", to_id=None, authenticity=0.0, score=0, makeup=None)
_unc = SUB.score_leg(con, _leg, _absent, DATE)
check("3f an uncovered leg scores zero on every axis",
      _unc["confidence"] == 0 and _unc["coverage"] == 0 and _unc["continuity"] == 0, str(_unc))

_score = SUB.score_plan(con, [_leg], _absent, DATE)
check("3g a wholly uncovered plan is flagged, not quietly ranked",
      _score["coverage"] == 0 and _score["continuity"] == 0, str(_score))
check("3h incomplete plans are marked incomplete",
      all(p["incomplete"] == (p["coverage"] < 100) for _f, ps, _a in SAMPLE for p in ps))


# ==================================================== 4 · the swap is a swap
print("\n4 · plan B re-sequences, and costs the partner nothing")
print("-" * 78)

SWAPPED = [(f, ps, l) for f, ps, _a in SAMPLE for p in ps if p["code"] == "B"
           for l in p["legs"] if l["action"] == "SWAP"]
check("4a the sample actually contains swaps", len(SWAPPED) > 0, f"{len(SWAPPED)} found")

# DEFECT THIS ENCODES: a lab is three contiguous periods. The old find_swap
# would happily pull one slice of CIVIL-3A's Mon P5-P7 BCVL305 block forward,
# which is not a thing a college can do.
bad_lab = []
for _f, _ps, l in SWAPPED:
    partner = one(con, "SELECT * FROM timetable WHERE id=?", (l["swap_with"],))
    if (partner["kind"] or "T") in ("L", "P") or SUB.in_multi_period_block(con, partner):
        bad_lab.append(f"{partner['dept']}-{partner['sem']}{partner['section']} "
                       f"{partner['day']}P{partner['period']} {partner['subject']}")
check("4b no swap ever splits a lab or multi-period block", not bad_lab, str(bad_lab[:3]))

civil = one(con, """SELECT * FROM timetable WHERE dept='CIVIL' AND sem=3 AND section='A'
                    AND day='Mon' AND period=5""")
check("4c the CIVIL-3A Mon P5-P7 lab block is recognised as unswappable",
      civil and SUB.in_multi_period_block(con, civil), str(civil))
check("4d a multi-period source block is never swapped",
      SUB.find_swap(con, {**civil, "periods": [5, 6, 7]}, DATE.isoformat()) is None)


def effective_day_load(fid, day, date_iso):
    """Periods this faculty ACTUALLY teaches on `day`, after overrides — which
    is the only number that can answer "did the swap cost them an hour?"."""
    n = 0
    for r in rows(con, "SELECT * FROM timetable WHERE day=?", (day,)):
        o = one(con, """SELECT * FROM overrides WHERE slot_id=? AND date=? AND status='Applied'
                        ORDER BY id DESC LIMIT 1""", (r["id"], date_iso))
        who = r["faculty"] if not o else o["new_faculty"]
        if o and o["action"] in ("VACATED", "MAKEUP"):
            who = None
        if who == fid:
            n += 1
    return n


# THE assertion. DEFECT THIS ENCODES: the old SWAP wrote a single override on
# the vacated slot and left the partner's own period untouched, so the partner
# taught BOTH — an extra hour — while the plan card claimed "No extra workload
# on any faculty". Nothing in the suite noticed, because nothing measured it.
absent, plans, _aff = next((f, ps, a) for f, ps, a in SAMPLE
                           if any(l["action"] == "SWAP"
                                  for p in ps if p["code"] == "B" for l in p["legs"]))
planB = next(p for p in plans if p["code"] == "B")
swap_legs = [l for l in planB["legs"] if l["action"] == "SWAP"]
partners = {l["swap_faculty_id"] for l in swap_legs}
before = {fid: effective_day_load(fid, DAY, DATE.isoformat()) for fid in partners}
before_verify = db.verify()

SUB.apply_plan(con, planB, absent, DATE)

after = {fid: effective_day_load(fid, DAY, DATE.isoformat()) for fid in partners}
check("4e the swap partner's teaching hours are UNCHANGED", before == after,
      f"{before} -> {after}")
check("4f the absent teacher is left with no hours that day",
      effective_day_load(absent["id"], DAY, DATE.isoformat())
      < len(rows(con, "SELECT 1 FROM timetable WHERE faculty=? AND day=?", (absent["id"], DAY))),
      "some period is still assigned to the absent teacher")

# db.verify() reads the BASE timetable, which apply_plan must never touch. The
# assertion is cheap and it is the one that would have caught a write landing
# in `timetable` instead of `overrides`.
check("4g db.verify() still passes after applying a plan",
      db.verify() == before_verify, str(db.verify()[:3]))

# Two rows, one plan_ref — and undo has to take back both halves of a swap.
wrote = rows(con, "SELECT * FROM overrides WHERE plan_ref=?", (planB["id"],))
check("4h a swap writes two override rows, one for each end",
      sum(1 for w in wrote if w["action"] == "SWAP") >= len(swap_legs)
      and sum(1 for w in wrote if w["action"] == "VACATED") == len(swap_legs),
      f"{[(w['action']) for w in wrote]}")
check("4i the moved subject is recorded in new_subject",
      all(w["new_subject"] for w in wrote if w["action"] == "SWAP"),
      "new_subject is what tells the grid a different subject now occupies the slot")
check("4j both halves share one plan_ref, so undo reverts the pair",
      len({w["plan_ref"] for w in wrote}) == 1)

# The released period must not be left showing the teacher who is away.
sw = swap_legs[0]
partner_row = one(con, "SELECT * FROM timetable WHERE id=?", (sw["swap_with"],))
grid = TimetableAgent().class_grid(con, partner_row["dept"], partner_row["sem"],
                                   partner_row["section"], DATE.isoformat())
vac = grid["cells"][f"{DAY}-{partner_row['period']}"]
moved = grid["cells"][f"{DAY}-{sw['period']}"]
check("4k the vacated period renders as released", vac["override"]["released"] is True, str(vac))
check("4l the moved subject shows in its new period",
      moved["subject"] == sw["swap_subject"], f"{moved['subject']} != {sw['swap_subject']}")
check("4m the moved period records what it displaced",
      moved["override"]["was"] == sw["subject"], str(moved["override"]))

con.execute("UPDATE overrides SET status='Reverted' WHERE plan_ref=?", (planB["id"],))
con.commit()
check("4n undo by plan_ref clears both halves",
      not rows(con, "SELECT 1 FROM overrides WHERE plan_ref=? AND status='Applied'",
               (planB["id"],)))


# ================================================= 5 · the make-up can happen
print("\n5 · plan C can actually schedule something")
print("-" * 78)
# DEFECT THIS ENCODES: find_makeup searched only periods 1..day_periods(sem,day),
# and db.verify() asserts every batch fills exactly those with no holes — so the
# search could never succeed. Measured: 0 of 126 class-days have a free in-day
# period. Plan C reported 0% coverage for every absence ever put to it, and the
# flat confidence of 66 hid it.
in_day_free = sum(1 for r in rows(con, """SELECT dept,sem,section,day,COUNT(*) n FROM timetable
                                          GROUP BY dept,sem,section,day""")
                  if r["n"] != db.day_periods(r["sem"], r["day"]))
check("5a the seeded college genuinely has no free in-day period", in_day_free == 0,
      f"{in_day_free} class-days have one — the original search would have worked")

made = [p for _f, ps, _a in SAMPLE for p in ps if p["code"] == "C" and p["coverage"] > 0]
check("5b plan C schedules make-ups for most absences",
      len(made) >= 0.8 * len(SAMPLE), f"{len(made)} of {len(SAMPLE)}")
check("5c a make-up lands after the absence, never before",
      all(dt.date.fromisoformat(l["makeup"]["date"]) > DATE
          for _f, ps, _a in SAMPLE for p in ps if p["code"] == "C"
          for l in p["legs"] if l.get("makeup")))
check("5d a make-up never double-books the returning teacher",
      not [1 for _f, ps, _a in SAMPLE for p in ps if p["code"] == "C"
           for l in p["legs"] if l.get("makeup")
           and one(con, "SELECT 1 FROM timetable WHERE faculty=? AND day=? AND period=?",
                   (_f["id"], l["makeup"]["day"], l["makeup"]["period"]))])
check("5e a make-up never double-books the batch",
      not [1 for _f, ps, _a in SAMPLE for p in ps if p["code"] == "C"
           for l in p["legs"] if l.get("makeup")
           and one(con, """SELECT 1 FROM timetable WHERE dept=? AND sem=? AND section=?
                           AND day=? AND period=?""",
                   (l["dept"], l["sem"], l["class"].split("-")[1][1:],
                    l["makeup"]["day"], l["makeup"]["period"]))])


# ============================================== 6 · plan A did not regress
print("\n6 · plan A's candidate search still obeys its hard constraints")
print("-" * 78)
# score_faculty() was extracted out of rank_candidates so B and C could use the
# same lens. Extraction must not have changed what A rejects.
viol = []
for f, ps, _a in SAMPLE[:12]:
    planA = next(p for p in ps if p["code"] == "A")
    for l in planA["legs"]:
        if not l["to_id"]:
            continue
        if l["to_id"] == f["id"]:
            viol.append("assigned the absent teacher")
        if SUB.on_leave(con, DATE.isoformat()) & {l["to_id"]}:
            viol.append(f"{l['to_id']} is on leave")
        for per in l["periods"]:
            if l["to_id"] in SUB.busy_faculty(con, DAY, per, DATE.isoformat()):
                viol.append(f"{l['to_id']} already teaching {DAY}P{per}")
check("6a no substitute is the absent teacher, on leave, or already busy",
      not viol, str(viol[:3]))
check("6b candidates come back in descending score order",
      all([c["score"] for c in SUB.rank_candidates(
          con, {**SAMPLE[0][2][0], "periods": SAMPLE[0][2][0]["periods"]},
          DATE.isoformat(), {})] ==
          sorted((c["score"] for c in SUB.rank_candidates(
              con, {**SAMPLE[0][2][0], "periods": SAMPLE[0][2][0]["periods"]},
              DATE.isoformat(), {})), reverse=True) for _ in (0,)))
check("6c plan B keeps its own fairness accumulator",
      all(max([[l["to_id"] for l in p["legs"] if l["action"] == "SUBSTITUTE"].count(x)
               for x in {l["to_id"] for l in p["legs"] if l["action"] == "SUBSTITUTE"}] or [0]) <= 2
          for _f, ps, _a in SAMPLE for p in ps if p["code"] == "B"),
      "one stand-in was handed more than two fallback legs in a single plan")


# ================================== 7 · coverage is a floor, not a rank axis
print()
print("7 · coverage is a feasibility floor — it cannot change an ordering")
print("-" * 78)
# DEFECT THIS ENCODES: coverage was worth 0.25 of the rank and had never moved
# an ordering. Two measurements put it out of the formula:
#   * normal load - all 813 plans across every faculty x working day score 100%,
#     so the term added a constant 25 to every rank;
#   * scarcity - with every other teacher on leave, plan A goes to confidence 0
#     AND continuity 0 as well as coverage 0, because score_leg() returns zero
#     on every axis for an arrangement that does not exist.
# So it is collinear BY CONSTRUCTION. A case where it discriminates cannot be
# built, which is why this section asserts that rather than pretending one.
check("7a coverage is not a ranking weight", "coverage" not in RANK_WEIGHTS,
      f"RANK_WEIGHTS={RANK_WEIGHTS} — a term that never moves an ordering must not "
      f"be advertised as a quarter of the ranking")
check("7b every plan still reports coverage, labelled as a floor",
      all(p["coverage_is_floor"] and "coverage" in p for _f, ps, _a in SAMPLE for p in ps))
check("7c coverage does not discriminate on this dataset",
      {p["coverage"] for _f, ps, _a in SAMPLE for p in ps} == {100})


def _order(ps, with_coverage):
    """Rank the plans with and without a coverage term at the SAME coefficients.
    Renormalising instead would change the confidence:continuity ratio and
    reorder plans for that reason, which would look like coverage mattering."""
    def key(p):
        base = p["confidence"] * 0.6 + p["continuity"] * 0.15
        return -(base + p["coverage"] * 0.25) if with_coverage else -base
    return [p["code"] for p in sorted(ps, key=key)]


moved = [f["name"] for f, ps, _a in SAMPLE if _order(ps, True) != _order(ps, False)]
check("7d adding a coverage term back would reorder nothing", not moved,
      f"{len(moved)} absences reorder — if this ever fires, coverage has started "
      f"carrying information and belongs back in RANK_WEIGHTS")

# And the same under the scarcity the seeded data never produces.
_absent = next(f for f, ps, _a in SAMPLE)
_others = [r["id"] for r in rows(con, "SELECT id FROM faculty WHERE id<>?", (_absent["id"],))]
con.executemany("""INSERT INTO leaves(faculty,from_date,to_date,kind,reason,status,applied_on)
                   VALUES(?,?,?,?,?,?,?)""",
                [(fid, DATE.isoformat(), DATE.isoformat(), "Casual Leave",
                  "ranking_test scarcity probe", "Approved", DATE.isoformat())
                 for fid in _others])
con.commit()
try:
    scarce, _a2 = SUB.build_plans(con, _absent, DATE, Tr())
    sa = next((p for p in scarce if p["code"] == "A"), None)
    check("7e under scarcity a plan really can cover nothing",
          sa is not None and sa["coverage"] == 0, str(sa and sa["coverage"]))
    check("7f an uncoverable plan is flagged incomplete", sa and sa["incomplete"] is True)
    check("7g an uncoverable plan scores zero on the OTHER axes too — which is why "
          "the coverage term is redundant",
          sa and sa["confidence"] == 0 and sa["continuity"] == 0,
          f"conf {sa and sa['confidence']}, cont {sa and sa['continuity']}")
    check("7h so it ranks last with or without a coverage term",
          _order(scarce, True) == _order(scarce, False) and _order(scarce, True)[-1] == "A",
          f"with={_order(scarce, True)} without={_order(scarce, False)}")
finally:
    con.execute("DELETE FROM leaves WHERE reason='ranking_test scarcity probe'")
    con.commit()


# ============================== 8 · no plan is offered twice under two names
print()
print("8 · two plans that commit the same rows are one option")
print("-" * 78)
# DEFECT THIS ENCODES: when find_swap finds nothing, plan B falls back to
# substitution and picks the SAME candidates plan A did — measured, 90 of 271
# absences, a third of them. Every one produced an exact rank tie between two
# byte-identical cards, which made "recommended" arbitrary. A tie-break would
# have hidden the duplication instead of removing it.
dupes = [(f["name"], p) for f, ps, _a in SAMPLE for p in ps if p["also_commits"]]
check("8a the sample contains folded duplicates", len(dupes) > 0,
      f"{len(dupes)} — if zero, the fallback stopped colliding and this is stale")
check("8b no two offered plans would commit the same rows",
      all(len({SUB.commit_signature(p["legs"]) for p in ps}) == len(ps)
          for _f, ps, _a in SAMPLE),
      "a duplicate survived the fold")
check("8c a folded plan is named on the card that subsumed it",
      all(any(c in p["summary"] for c in p["also_commits"]) for _n, p in dupes))
check("8d folding removed the exact rank ties it was causing",
      not [1 for _f, ps, _a in SAMPLE
           for i in range(len(ps) - 1)
           if abs(ps[i]["rank"] - ps[i + 1]["rank"]) < 1e-9
           and SUB.commit_signature(ps[i]["legs"]) == SUB.commit_signature(ps[i + 1]["legs"])])
check("8e ordering is deterministic even when ranks genuinely coincide",
      all([(round(-p["rank"], 6), -p["continuity"], -p["coverage"], p["code"]) for p in ps]
          == sorted((round(-p["rank"], 6), -p["continuity"], -p["coverage"], p["code"])
                    for p in ps)
          for _f, ps, _a in SAMPLE),
      "the documented tie-break is rank, then continuity, then coverage, then code")
# The admin may still say "apply plan B" from an earlier message.
_n, _folded = dupes[0]
check("8f a folded letter still resolves to the plan that absorbed it",
      _folded["also_commits"] and _folded["code"] not in _folded["also_commits"])


print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
