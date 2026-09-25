"""Date resolution. No server, no LLM, no API cost.

    python tests/nlu_test.py

Every reference day is passed in explicitly, so nothing here depends on the
seeded TODAY or on the day you happen to run it.

--------------------------------------------------------------------------
Why this suite exists
--------------------------------------------------------------------------
`parse_date` decides which day an absence lands on, and the absence flow
writes timetable overrides for that day. A week's error here is not a
cosmetic one: it leaves the real absence uncovered on the day it actually
happens, and nothing downstream notices, because every override it wrote is
internally consistent — just for the wrong week.

That is what "next Monday" used to do. `this/coming/on X` resolved to the
next X, and `next X` added a further week, so on Friday 04 Sep "next monday"
was the 14th and the 7th was skipped. It was deliberate, and it was wrong
for three reasons, all encoded below:

  * it collapsed for one weekday in seven anyway — when the target is today's
    own weekday, both readings land +7, so the distinction it drew was never
    honoured consistently (1c, 2c);
  * the failure costs are asymmetric: a week late is silent, a week early is
    caught by the date echoed back before anything commits;
  * the coming Monday is the dominant reading.

All four phrasings now mean the same day. The table below pins every
(reference day x target day) pair so that changing the convention again is a
deliberate act with a failing test attached, not a quiet edit.
"""
import datetime as dt
import os
import sys

# The failure path prints U+2718, which a cp1252 stdout cannot encode; without
# this a FAILING run shows a traceback instead of the assertion that failed.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nlu                                                          # noqa: E402

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


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FULL = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# One real week, so every weekday gets a turn as "today".
WEEK = [dt.date(2026, 9, 7) + dt.timedelta(days=i) for i in range(7)]   # Mon..Sun
assert [d.weekday() for d in WEEK] == list(range(7))

# `on X` deliberately has no Sunday branch — the college does not run on Sunday.
PREFIXES = ("this", "next", "coming", "on")


def expected(ref, target_idx):
    """The next occurrence. Today never counts as 'the next one'."""
    return ref + dt.timedelta(days=(target_idx - ref.weekday()) % 7 or 7)


print("\n1 · every phrasing resolves to the next occurrence")
print("-" * 78)

mismatch = []
for ref in WEEK:
    for i, name in enumerate(FULL):
        want = expected(ref, i)
        for pre in PREFIXES:
            if pre == "on" and name == "sunday":
                continue                      # no Sunday branch, by design
            got, _lbl = nlu.parse_date(f"{pre} {name}", today=ref)
            if got != want:
                mismatch.append(f"{DAYS[ref.weekday()]} + '{pre} {name}' -> {got}, want {want}")
check("1a this / next / coming / on agree for all 7x7 reference-target pairs",
      not mismatch, f"{len(mismatch)} disagree, first: {mismatch[:3]}")

ahead = []
for ref in WEEK:
    for i, name in enumerate(FULL):
        got, _ = nlu.parse_date(f"next {name}", today=ref)
        if not (1 <= (got - ref).days <= 7):
            ahead.append(f"{DAYS[ref.weekday()]} + 'next {name}' -> {(got - ref).days}d")
check("1b the answer is always 1-7 days ahead, never behind and never 0",
      not ahead, str(ahead[:3]))

same_weekday = []
for ref in WEEK:
    name = FULL[ref.weekday()]
    for pre in PREFIXES:
        if pre == "on" and name == "sunday":
            continue
        got, _ = nlu.parse_date(f"{pre} {name}", today=ref)
        if (got - ref).days != 7:
            same_weekday.append(f"{DAYS[ref.weekday()]} + '{pre} {name}' -> {(got - ref).days}d")
# This is the case that used to collapse: "this Friday" and "next Friday" both
# landed +7 on a Friday, so the old this/next distinction held for only 6 of 7
# weekdays. Now they agree everywhere, and they agree here too — deliberately.
check("1c when the target IS today's weekday every phrasing gives +7, not 0",
      not same_weekday, str(same_weekday[:3]))


print("\n2 · the specific regression")
print("-" * 78)
FRI = dt.date(2026, 9, 4)
check("2a Fri 04 Sep + 'next monday' is the 7th, not the 14th",
      nlu.parse_date("next monday", today=FRI)[0] == dt.date(2026, 9, 7),
      f"got {nlu.parse_date('next monday', today=FRI)[0]} — a week late means the "
      f"absence goes uncovered on the day it happens")
check("2b 'this monday' agrees with it",
      nlu.parse_date("this monday", today=FRI)[0] == dt.date(2026, 9, 7))
check("2c 'this friday' and 'next friday' agree on a Friday",
      nlu.parse_date("this friday", today=FRI)[0]
      == nlu.parse_date("next friday", today=FRI)[0] == dt.date(2026, 9, 11),
      "the old code happened to agree here too, which is why the distinction it "
      "drew was never consistent")
check("2d a full week of Mondays from every reference day",
      [(nlu.parse_date("next monday", today=r)[0] - r).days for r in WEEK]
      == [7, 6, 5, 4, 3, 2, 1],
      str([(nlu.parse_date("next monday", today=r)[0] - r).days for r in WEEK]))


print("\n3 · the other phrasings still work")
print("-" * 78)
check("3a today", nlu.parse_date("today", today=FRI)[0] == FRI)
check("3b tomorrow", nlu.parse_date("tomorrow", today=FRI)[0] == dt.date(2026, 9, 5))
check("3c day after tomorrow",
      nlu.parse_date("day after tomorrow", today=FRI)[0] == dt.date(2026, 9, 6))
check("3d yesterday", nlu.parse_date("yesterday", today=FRI)[0] == dt.date(2026, 9, 3))
check("3e an explicit '8 sep'",
      nlu.parse_date("on 8 sep", today=FRI)[0] == dt.date(2026, 9, 8))
# This assertion used to pin the OPPOSITE of its own name: the weekday phrase
# won and returned the 7th (#17). An explicit calendar date now wins, and the
# disagreement is surfaced by date_conflict() instead of being settled silently.
check("3f an explicit date beats a weekday word in the same sentence",
      nlu.parse_date("absent next monday, i mean 9 sep", today=FRI)[0]
      == dt.date(2026, 9, 9))
check("3g nothing date-like returns None", nlu.parse_date("arrange coverage")[0] is None)
check("3h a label comes back with the date",
      nlu.parse_date("next monday", today=FRI)[1] and
      "Mon" in nlu.parse_date("next monday", today=FRI)[1],
      "the console echoes this back, which is what catches a wrong week early")


# --------------------------------------------------------- 4 · ISO dates (#55)
print("\n4 · an ISO date is the date it says")
print("-" * 78)
FRI = dt.date(2026, 9, 4)
iso = nlu.parse_date("F012 is absent on 2026-09-08, arrange coverage", FRI)[0]
# Defect found 2026-09-25: the dd-mm pattern matched the "09-08" inside the ISO
# date and returned 9 August - a Sunday - so the reply said no cover was needed.
check("4a '2026-09-08' is Tue 8 September, not 9 August (a Sunday)", iso == dt.date(2026, 9, 8), str(iso))
check("4b an unpadded ISO date works too", nlu.parse_date("2026-9-8", FRI)[0] == dt.date(2026, 9, 8))
check("4c the Indian dd-mm and dd-mm-yyyy forms still read day first",
      nlu.parse_date("8/9", FRI)[0] == dt.date(2026, 9, 8)
      and nlu.parse_date("08-09-2026", FRI)[0] == dt.date(2026, 9, 8))
check("4d an impossible ISO date gives no date, not a fragment of it",
      nlu.parse_date("on 2026-02-30", FRI) == (None, None), str(nlu.parse_date("on 2026-02-30", FRI)))

# ------------------------------------------ 5 · a weekday and a date in one sentence (#17)
print("\n5 · a written date wins; a weekday that disagrees with it is a conflict")
print("-" * 78)
d, _ = nlu.parse_date("absent on Monday 14 Sep", FRI)
# Defect: "on Monday" was matched first and returned Mon 7 Sep - a week early.
check("5a 'absent on Monday 14 Sep' is the 14th, not the 7th", d == dt.date(2026, 9, 14), str(d))
check("5b a weekday that agrees with the date is not a conflict", nlu.date_conflict("absent on Monday 14 Sep", d) is None)
d2, _ = nlu.parse_date("absent on monday 15 sep", FRI)
check("5c a weekday that disagrees is reported, so the caller can ask",
      d2 == dt.date(2026, 9, 15) and nlu.date_conflict("absent on monday 15 sep", d2) == "Mon")
check("5d a range like '2-3 days' is still not read as a date",
      nlu.parse_date("next monday for 2-3 days", FRI)[0] == dt.date(2026, 9, 7))
check("5e no weekday word, no conflict", nlu.date_conflict("absent on 15 sep", d2) is None)

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
sys.exit(1 if FAILED else 0)
