"""
VidyaERP :: Timetable generation from scratch.

Ported from the Chronos reference solver
(`teacher-erp-with-timetable-simulation/src/lib/solver/engine.ts`, 755 lines of
TypeScript) and adapted to the way this college actually runs. VidyaERP could
re-arrange an existing timetable but could never build one; this closes that gap.

Four phases, same as the reference:

  1. Staffing  - one teacher owns a subject for a class for the whole semester.
  2. Search    - chronological backtracking, MRV variable ordering, cost-ranked
                 value ordering, forward feasibility checks, bounded restarts.
  3. Repair    - min-conflicts ejection for whatever the search could not place.
  4. Polish    - hill-climbing relocation + pairwise exchange to close gaps and
                 pull core subjects earlier in the day.

Then one phase the reference does not have, because its school does not need it:

  5. Fill      - an Indian engineering college day is a SOLID block of periods.
                 Any slot curriculum hours cannot fill gets a real academic
                 activity (placement training, mentoring, library), never a hole.

`solve()` is a generator, exactly as the reference is: the API layer streams every
decision to the browser as it happens, and a caller that just wants a timetable
drains it synchronously. Same code path, so what you watch is what you get.

What this port adds over the reference:

  * Multi-period blocks. A lab is 3 CONSECUTIVE periods inside one session and
    must never straddle lunch. Chronos only ever places single periods.
  * Per-class day length. Chronos has one uniform grid with a global break set;
    here Sem 3 sits 7 periods, Sem 5 sits 6 and Sem 7 sits 5, and Saturday is
    short. A slot past a class's own day end is structurally illegal for it.
  * Pinning. Regenerating one section leaves every other section's bookings
    frozen and still respects them - the reference's `assignments.locked`,
    generalised to a scope.

Honest limits, stated where a reader will see them:

  * Room capacity is a HARD constraint when some room of the required kind can
    hold the class, and RELAXED with a recorded flag when none can. The lab rooms
    seat 36 and a CSE section is ~60, because real colleges split a lab into
    batches and this dataset does not model batches. Relaxing silently would hide
    that; `capacity_relaxed` in the result names every class it happened to.
  * Teacher unavailability is read from `faculty_availability` (#19): standing
    weekly windows, which is what a weekly timetable can honour. Approved leave
    is dated, so it drives the RESCHEDULING path, not generation.
"""
from __future__ import annotations

import itertools
import random
import time

import db
from db import (ACTIVITIES, DAYS, LAB_WINDOWS, PERIODS, SUBJECTS, day_periods)

PMAX = max(PERIODS)                 # 7 - the longest day any semester sits
NDAYS = len(DAYS)                   # 6 - Mon..Sat
NSLOTS = NDAYS * PMAX               # 42 - the full grid, before per-class trimming

# A 3-period block may only START where a lab window starts, so it cannot
# straddle the short break or lunch. Derived from db.LAB_WINDOWS, 1-based.
LAB_STARTS = {d: [a for (a, b) in LAB_WINDOWS[d]] for d in DAYS}

# Weekly period demand per subject kind. Mirrors db.seed()'s `demand` rule so a
# generated timetable is the same shape as a seeded one.
BLOCK = {"T": 1, "L": 3, "P": 3}


def slot_of(day_i: int, period: int) -> int:
    """(day index, 1-based period) -> flat slot index."""
    return day_i * PMAX + (period - 1)


def day_of(slot: int) -> int:
    return slot // PMAX


def period_of(slot: int) -> int:
    return slot % PMAX + 1


def weekly_periods(kind: str, credits: int) -> int:
    """Total periods a subject needs per week."""
    return 6 if kind == "P" else 3 if kind == "L" else credits + 1


# --------------------------------------------------------------------------- input


class Klass:
    __slots__ = ("key", "idx", "dept", "sem", "section", "size", "day_len", "home_room", "label")

    def __init__(self, idx, dept, sem, section, size, home_room):
        self.idx = idx
        self.dept, self.sem, self.section = dept, sem, section
        self.key = (dept, sem, section)
        self.size = size
        self.home_room = home_room
        self.day_len = {d: day_periods(sem, d) for d in DAYS}
        self.label = f"{dept}-{sem}{section}"


class Var:
    """One placeable unit: `length` consecutive periods of one subject for one class."""
    __slots__ = ("k", "c", "s", "t", "n", "length", "room_kind", "max_per_day",
                 "priority", "slots", "rooms", "cap_relaxed")

    def __init__(self, k, c, s, t, n, length, room_kind, max_per_day, priority):
        self.k, self.c, self.s, self.t, self.n = k, c, s, t, n
        self.length = length
        self.room_kind = room_kind
        self.max_per_day = max_per_day
        self.priority = priority
        self.slots = []          # structurally legal start slots, precomputed
        self.rooms = []          # room indices that can host it
        self.cap_relaxed = False


class Input:
    """Everything the solver needs, already resolved to indices."""

    def __init__(self):
        self.classes: list[Klass] = []
        self.subjects: list[str] = []
        self.subject_meta: dict = {}
        self.teachers: list[str] = []
        self.teacher_meta: dict = {}
        self.rooms: list[str] = []
        self.room_meta: dict = {}
        self.lessons: list[dict] = []
        self.unavailable: dict[str, set] = {}     # teacher id -> {slot}
        self.pins: list[dict] = []                # frozen bookings from other scopes
        self.seed = 7
        self.max_steps = 40000
        self.optimize_passes = 1400
        self.time_budget_s = 25.0


def standing_unavailability(con) -> dict:
    """teacher id -> {slot} from the active standing windows (#19)."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='faculty_availability'").fetchone():
        return {}
    out: dict = {}
    for r in con.execute("SELECT faculty, day, p_from, p_to FROM faculty_availability WHERE status='Active'"):
        if r["day"] not in DAYS:
            continue
        for p in range(max(1, r["p_from"]), min(PMAX, r["p_to"]) + 1):
            out.setdefault(r["faculty"], set()).add(slot_of(DAYS.index(r["day"]), p))
    return out


def build_input(con, scope="all", dept=None, sem=None, section=None, seed=7,
                unavailable=None, time_budget_s=25.0) -> Input:
    """Read the institution out of the database and shape it for the solver.

    scope="all"    - rebuild every section; nothing is pinned.
    scope="class"  - rebuild one section; every OTHER section's current bookings
                     are pinned, so the regenerated section has to fit around the
                     teachers and rooms they already occupy.
    """
    inp = Input()
    inp.seed = seed
    inp.time_budget_s = time_budget_s

    rows = lambda q, a=(): [dict(r) for r in con.execute(q, a)]

    # ---- rooms
    for r in rows("SELECT id, kind, capacity, block FROM rooms ORDER BY id"):
        inp.room_meta[r["id"]] = r
        inp.rooms.append(r["id"])

    # ---- teachers
    for f in rows("SELECT id, name, dept, max_load, expertise FROM faculty ORDER BY id"):
        f["expertise"] = set((f["expertise"] or "").split(","))
        # Chronos budgets a teacher per day as well as per week; this dataset only
        # carries a weekly cap, so derive a daily one that leaves the week reachable.
        f["max_per_day"] = max(3, -(-f["max_load"] // 4))
        inp.teacher_meta[f["id"]] = f
        inp.teachers.append(f["id"])
    if unavailable is None:
        unavailable = standing_unavailability(con)
    inp.unavailable = {t: set(unavailable.get(t, ())) for t in inp.teachers}

    # ---- subjects (curriculum + the activity codes used by the fill phase)
    for s in rows("SELECT code, name, dept, sem, credits, kind FROM subjects ORDER BY code"):
        inp.subject_meta[s["code"]] = s
        inp.subjects.append(s["code"])

    # ---- classes
    want = []
    for d in sorted(SUBJECTS):
        for sm in sorted(SUBJECTS[d]):
            for sec in sorted({r["section"] for r in rows(
                    "SELECT DISTINCT section FROM students WHERE dept=? AND sem=?", (d, sm))} or {"A"}):
                want.append((d, sm, sec))

    targets = set(want)
    if scope == "class":
        targets = {(dept, int(sem), section)}

    class_rooms = [r for r in inp.rooms if inp.room_meta[r]["kind"] == "Classroom"]
    for i, (d, sm, sec) in enumerate(want):
        if (d, sm, sec) not in targets:
            continue
        size = con.execute("SELECT COUNT(*) c FROM students WHERE dept=? AND sem=? AND section=?",
                           (d, sm, sec)).fetchone()["c"] or 60
        home = class_rooms[i % len(class_rooms)] if class_rooms else None
        inp.classes.append(Klass(len(inp.classes), d, sm, sec, size, home))

    # ---- lessons: one per (class, subject), carrying its weekly period demand
    for k in inp.classes:
        for code, name, credits, kind in SUBJECTS[k.dept][k.sem]:
            inp.lessons.append({
                "cls": k.idx, "subject": code, "kind": kind,
                "periods": weekly_periods(kind, credits),
                "block": BLOCK[kind],
                "priority": 3.0 if kind == "P" else 2.2 if kind == "L" else 1.0 + credits * 0.15,
            })

    # ---- pins: bookings that belong to classes we are NOT regenerating
    if scope == "class":
        for row in rows("""SELECT dept, sem, section, day, period, subject, faculty, room, kind
                           FROM timetable"""):
            if (row["dept"], row["sem"], row["section"]) in targets:
                continue
            inp.pins.append(row)

    return inp


# --------------------------------------------------------------------------- solver

HARD_EMIT_BUDGET = 5200     # a trace this long already exceeds what a UI can render


def solve(inp: Input):
    """Generator of trace events; returns the SolverResult dict via StopIteration.value."""
    started = time.time()
    rnd = random.Random(inp.seed)

    classes, rooms = inp.classes, inp.rooms
    C, R, T = len(classes), len(rooms), len(inp.teachers)
    tix = {t: i for i, t in enumerate(inp.teachers)}
    six = {s: i for i, s in enumerate(inp.subjects)}
    rix = {r: i for i, r in enumerate(rooms)}
    SUB = len(inp.subjects)

    emitted = 0

    def keep(kind):
        """Chronos's emit budget: drop the noisy per-slot events first."""
        if emitted < HARD_EMIT_BUDGET:
            return True
        if kind in ("try", "fail", "pick"):
            return False
        return emitted < HARD_EMIT_BUDGET * 2

    # ---------------------------------------------------------------- staffing
    # Expertise first, then load balance - the same rule db.seed() applies, but
    # reachable from the solver so it can staff an institution that has none.
    #
    # Load starts at whatever the PINS already commit a teacher to. Regenerating
    # one section must not hand a subject to someone whose week is already full
    # teaching the sections we are not touching; without this, staffing promises
    # hours the search then cannot place, and the shortfall silently becomes
    # activity slots.
    planned = {t: 0 for t in inp.teachers}
    for p in inp.pins:
        if p["faculty"] in planned:
            planned[p["faculty"]] += 1
    lesson_teacher = {}
    staff_events = []
    for li, lesson in sorted(enumerate(inp.lessons), key=lambda x: -x[1]["priority"]):
        k = classes[lesson["cls"]]
        pool = [t for t in inp.teachers if inp.teacher_meta[t]["dept"] == k.dept]
        if not pool:
            pool = inp.teachers
        best, bscore = None, -1e9
        for t in pool:
            m = inp.teacher_meta[t]
            head = m["max_load"] - 3 - planned[t]              # keep 3 hrs for activity duty
            if head < lesson["periods"]:
                continue
            score = (5 if lesson["subject"] in m["expertise"] else 0) \
                - planned[t] * 0.35 + rnd.random() * 0.5
            if score > bscore:
                best, bscore = t, score
        if best is None:
            best = min(pool, key=lambda t: planned[t])
        planned[best] += lesson["periods"]
        lesson_teacher[li] = best
        staff_events.append({"t": "staff", "l": li, "subject": lesson["subject"],
                             "cls": k.label, "teacher": best,
                             "name": inp.teacher_meta[best]["name"], "load": planned[best]})

    # ------------------------------------------------------------- variables
    vars_: list[Var] = []
    for li, lesson in enumerate(inp.lessons):
        k = classes[lesson["cls"]]
        t = lesson_teacher.get(li)
        if t is None:
            continue
        length = lesson["block"]
        occurrences = max(1, lesson["periods"] // length)
        kind = "Lab" if lesson["kind"] in ("L", "P") else "Classroom"
        for n in range(occurrences):
            vars_.append(Var(len(vars_), k.idx, six[lesson["subject"]], tix[t], n,
                             length, kind, 1 if length > 1 else 2, lesson["priority"]))
    V = len(vars_)

    # ------------------------------------- structurally legal slots + room pools
    capacity_relaxed = []
    for v in vars_:
        k = classes[v.c]
        legal = []
        for di, dname in enumerate(DAYS):
            dl = k.day_len[dname]
            if v.length == 1:
                legal += [slot_of(di, p) for p in range(1, dl + 1)]
            else:
                for start in LAB_STARTS[dname]:
                    if start + v.length - 1 <= dl:
                        legal.append(slot_of(di, start))
        v.slots = legal
        fit = [rix[r] for r in rooms
               if inp.room_meta[r]["kind"] == v.room_kind
               and inp.room_meta[r]["capacity"] >= k.size]
        if not fit:
            # No room of the required kind can seat this class. Real colleges split
            # the batch; this dataset has no batches, so relax and SAY SO.
            fit = [rix[r] for r in rooms if inp.room_meta[r]["kind"] == v.room_kind]
            v.cap_relaxed = True
            capacity_relaxed.append({"cls": k.label, "subject": inp.subjects[v.s],
                                     "size": k.size, "kind": v.room_kind})
        v.rooms = fit

    yield {"t": "init",
           "days": DAYS, "periods": PMAX, "period_labels": [PERIODS[p] for p in sorted(PERIODS)],
           "classes": [{"i": k.idx, "label": k.label, "dept": k.dept, "sem": k.sem,
                        "section": k.section, "size": k.size,
                        "day_len": [k.day_len[d] for d in DAYS]} for k in classes],
           "rooms": [{"id": r, "kind": inp.room_meta[r]["kind"],
                      "cap": inp.room_meta[r]["capacity"]} for r in rooms],
           "vars": [{"k": v.k, "c": v.c, "s": inp.subjects[v.s], "n": v.n,
                     "len": v.length, "t": inp.teachers[v.t]} for v in vars_],
           "total": V, "pins": len(inp.pins)}
    emitted += 1

    yield {"t": "phase", "name": "Staffing",
           "note": f"Matching {len(inp.lessons)} lesson blocks to qualified faculty"}
    emitted += 1
    for e in staff_events[:40]:
        yield e
        emitted += 1

    # ----------------------------------------------------------------- state
    class_busy = [-1] * (C * NSLOTS)
    teacher_busy = [-1] * (T * NSLOTS)
    room_busy = [-1] * (R * NSLOTS)
    class_subj_day = {}
    teacher_day = [0] * (T * NDAYS)
    teacher_week = [0] * T
    assign_slot = [-1] * V
    assign_room = [-1] * V
    placed_count = 0

    unavail = [inp.unavailable.get(t, set()) for t in inp.teachers]
    max_day = [inp.teacher_meta[t]["max_per_day"] for t in inp.teachers]
    max_week = [inp.teacher_meta[t]["max_load"] for t in inp.teachers]

    # ---- pins occupy teachers and rooms before the search starts
    for p in inp.pins:
        s = slot_of(DAYS.index(p["day"]), p["period"])
        if p["faculty"] and p["faculty"] in tix:
            ti = tix[p["faculty"]]
            teacher_busy[ti * NSLOTS + s] = -2
            teacher_day[ti * NDAYS + day_of(s)] += 1
            teacher_week[ti] += 1
        if p["room"] and p["room"] in rix:
            room_busy[rix[p["room"]] * NSLOTS + s] = -2

    steps = backtracks = conflicts = restarts = 0

    def find_room(v: Var, slot: int) -> int:
        pool = v.rooms
        if not pool:
            return -1
        off = slot % len(pool)
        for i in range(len(pool)):
            r = pool[(i + off) % len(pool)]
            base = r * NSLOTS
            if all(room_busy[base + slot + j] == -1 for j in range(v.length)):
                return r
        return -1

    def violation(vk: int, slot: int):
        """None when feasible, else (reason, entity). Every check is a hard rule."""
        v = vars_[vk]
        d = day_of(slot)
        cb, tb = v.c * NSLOTS, v.t * NSLOTS
        for j in range(v.length):
            s = slot + j
            if class_busy[cb + s] != -1:
                return ("class", classes[v.c].label)
            if s in unavail[v.t]:
                return ("unavail", inp.teachers[v.t])
            if teacher_busy[tb + s] != -1:
                return ("teacher", inp.teachers[v.t])
        if teacher_day[v.t * NDAYS + d] + v.length > max_day[v.t]:
            return ("dayload", inp.teachers[v.t])
        if teacher_week[v.t] + v.length > max_week[v.t]:
            return ("weekload", inp.teachers[v.t])
        if class_subj_day.get((v.c, v.s, d), 0) >= v.max_per_day:
            return ("spread", inp.subjects[v.s])
        if find_room(v, slot) == -1:
            return ("room", v.room_kind)
        return None

    def soft_of(vk: int, slot: int) -> float:
        """Soft preference. Lower is better; nothing here can make a slot illegal."""
        v = vars_[vk]
        d, p0 = day_of(slot), slot % PMAX
        # Core subjects belong early in the day; a tired 4th-year 15:35 slot is worse.
        cost = v.priority * p0 * 0.8
        # Spread a teacher's day rather than stacking it.
        cost += teacher_day[v.t * NDAYS + d] * 0.35
        # The dominant term: never give a class the same subject twice in one day
        # if any other day will take it. (Chronos also carried a small term pulling
        # a subject BACK onto days it already used, which fought its own comment
        # about weekly spread - dropped here rather than transliterated.)
        cost += class_subj_day.get((v.c, v.s, d), 0) * 6.0
        return cost

    def place(vk: int, slot: int, room: int):
        nonlocal placed_count
        v = vars_[vk]
        d = day_of(slot)
        for j in range(v.length):
            s = slot + j
            class_busy[v.c * NSLOTS + s] = vk
            teacher_busy[v.t * NSLOTS + s] = vk
            room_busy[room * NSLOTS + s] = vk
        class_subj_day[(v.c, v.s, d)] = class_subj_day.get((v.c, v.s, d), 0) + 1
        teacher_day[v.t * NDAYS + d] += v.length
        teacher_week[v.t] += v.length
        assign_slot[vk], assign_room[vk] = slot, room
        placed_count += 1

    def unplace(vk: int):
        nonlocal placed_count
        slot = assign_slot[vk]
        if slot < 0:
            return
        v = vars_[vk]
        d = day_of(slot)
        for j in range(v.length):
            s = slot + j
            class_busy[v.c * NSLOTS + s] = -1
            teacher_busy[v.t * NSLOTS + s] = -1
            room_busy[assign_room[vk] * NSLOTS + s] = -1
        class_subj_day[(v.c, v.s, d)] -= 1
        teacher_day[v.t * NDAYS + d] -= v.length
        teacher_week[v.t] -= v.length
        assign_slot[vk] = assign_room[vk] = -1
        placed_count -= 1

    def build_cands(vk: int):
        scored = [(soft_of(vk, s) + rnd.random() * 2.2, s)
                  for s in vars_[vk].slots if violation(vk, s) is None]
        scored.sort()
        return [s for _, s in scored]

    def count_cands(vk: int, cutoff: int = 12) -> int:
        n = 0
        for s in vars_[vk].slots:
            if violation(vk, s) is None:
                n += 1
                if n >= cutoff:
                    return n
        return n

    # MRV is an ORDERING heuristic, so a stale count costs search quality, never
    # correctness - build_cands() re-derives the truth for the variable we pick.
    # That lets us refresh only the variables a placement can actually have
    # affected instead of all V of them on every step.
    by_class = {}
    by_teacher = {}
    lab_vars = []
    for v in vars_:
        by_class.setdefault(v.c, []).append(v.k)
        by_teacher.setdefault(v.t, []).append(v.k)
        if v.room_kind == "Lab":
            lab_vars.append(v.k)

    dom = [0] * V
    dirty = set(range(V))

    def touch(vk: int):
        v = vars_[vk]
        dirty.update(by_class.get(v.c, ()))
        dirty.update(by_teacher.get(v.t, ()))
        if v.room_kind == "Lab":
            dirty.update(lab_vars)      # small set, and lab rooms are the scarce ones

    assigned = bytearray(V)
    impossible = bytearray(V)
    stuck_count = [0] * V
    skipped = bytearray(V)
    stack = []

    # A variable with no feasible slot on an EMPTY timetable can never be placed
    # (no room of its kind exists, its class has no legal window). Park it rather
    # than let it collapse the whole search.
    for vk in range(V):
        if count_cands(vk, 1) == 0:
            impossible[vk] = 1
            skipped[vk] = 1

    def refresh():
        for vk in tuple(dirty):
            if not assigned[vk] and not skipped[vk]:
                dom[vk] = count_cands(vk)
        dirty.clear()

    def choose_var() -> int:
        refresh()
        best, best_score = -1, 1e18
        for vk in range(V):
            if assigned[vk] or skipped[vk]:
                continue
            if dom[vk] == 0:
                return vk                      # fail fast on a wipe-out
            score = dom[vk] * 100 - vars_[vk].priority * 3 + rnd.random()
            if score < best_score:
                best, best_score = vk, score
        return best

    best_placed, best_slots, best_rooms = -1, [-1] * V, [-1] * V

    def snapshot():
        nonlocal best_placed, best_slots, best_rooms
        if placed_count > best_placed:
            best_placed = placed_count
            best_slots = list(assign_slot)
            best_rooms = list(assign_room)

    def reset_all():
        for vk in range(V):
            if assigned[vk]:
                unplace(vk)
        assigned[:] = bytearray(V)
        skipped[:] = bytes(impossible)
        stack.clear()
        dirty.update(range(V))

    yield {"t": "phase", "name": "Search",
           "note": f"Placing {V} lesson blocks across {NSLOTS} time slots"}
    emitted += 1

    MAX_RESTARTS = 2
    MAX_UNWIND = 30         # relevant frames undone per stuck variable
    MAX_PASSED = 400        # irrelevant frames frozen before we give up looking
    STUCK_LIMIT = 2         # times a variable may wipe out before it is parked

    def related(a: int, b: int) -> bool:
        """Could undoing `a` ever free a slot for `b`? Anything else is noise."""
        va, vb = vars_[a], vars_[b]
        return va.c == vb.c or va.t == vb.t or va.room_kind == vb.room_kind

    out_of_time = False

    while True:
        if placed_count >= V:
            break
        if time.time() - started > inp.time_budget_s:
            out_of_time = True
            break
        steps += 1
        if steps > inp.max_steps:
            snapshot()
            if restarts >= MAX_RESTARTS:
                break
            restarts += 1
            reset_all()
            rnd.seed(inp.seed * 7919 + restarts * 104729)
            steps = 0
            yield {"t": "restart", "n": restarts, "placed": placed_count}
            emitted += 1
            continue

        vk = choose_var()
        if vk < 0:
            break
        cands = build_cands(vk)
        if keep("pick"):
            yield {"t": "pick", "v": vk, "d": len(cands),
                   "reason": "most-constrained (forced)" if len(cands) <= 1
                             else "minimum remaining values"}
            emitted += 1

        if not cands:
            conflicts += 1
            if keep("stuck"):
                yield {"t": "stuck", "v": vk}
                emitted += 1
            snapshot()
            # A variable that keeps coming back stuck is not stuck because of the
            # LAST decision - it is stuck structurally, and unwinding cannot help.
            # The reference has no such guard: its `recovered` flag goes true when
            # any OTHER frame's retry succeeds, so the culprit is never parked and
            # the search oscillates. Measured at 4 lab rooms: 11 lab blocks stuck
            # 1106 times between them, 3205 placements against 3183 undos, 24 of
            # 456 periods surviving. Park it here and let Repair, which ejects
            # exactly one conflicting booking, do the surgical version instead.
            stuck_count[vk] += 1
            if stuck_count[vk] >= STUCK_LIMIT:
                skipped[vk] = 1
                continue
            recovered = False
            unwound = passed = 0
            # CONFLICT-DIRECTED backtracking, not the reference's chronological
            # unwind. Chronos pops frames until the stack is empty, which on a tight
            # instance dismantles the whole timetable chasing one unplaceable lab:
            # measured at 4 lab rooms, 20k backtracks and 24 of 456 periods placed
            # with the budget exhausted. Undoing another department's theory period
            # cannot free a lab room, so those frames are passed over - their
            # placement is KEPT, they simply stop being retryable - and only frames
            # that share this variable's class, teacher or room kind are undone.
            while stack and unwound < MAX_UNWIND and passed < MAX_PASSED:
                f = stack[-1]
                if not related(f["v"], vk):
                    stack.pop()             # freeze: stays placed, no longer retryable
                    passed += 1
                    continue
                unwound += 1
                steps += 1          # backtracking is work; it must count toward the
                                    # step budget or a restart can never fire.
                undone = assign_slot[f["v"]]
                unplace(f["v"])
                assigned[f["v"]] = 0
                touch(f["v"])
                backtracks += 1
                if keep("undo"):
                    yield {"t": "undo", "v": f["v"], "s": undone}
                    emitted += 1
                f["pos"] += 1
                applied = False
                while f["pos"] < len(f["cands"]):
                    slot = f["cands"][f["pos"]]
                    if violation(f["v"], slot) is None:
                        room = find_room(vars_[f["v"]], slot)
                        place(f["v"], slot, room)
                        assigned[f["v"]] = 1
                        touch(f["v"])
                        if keep("place"):
                            yield {"t": "place", "v": f["v"], "s": slot, "r": rooms[room],
                                   "tch": inp.teachers[vars_[f["v"]].t]}
                            emitted += 1
                        applied = True
                        break
                    f["pos"] += 1
                if applied:
                    recovered = True
                    break
                stack.pop()
            if not recovered:
                # This subtree is exhausted. Park the variable so the rest of the
                # college still gets a usable timetable, and report it at the end.
                skipped[vk] = 1
            continue

        # Show a few rejected slots so the trace explains WHY, not just what.
        conflicts += len(vars_[vk].slots) - len(cands)
        shown = 0
        for slot in vars_[vk].slots:
            if shown >= 3:
                break
            bad = violation(vk, slot)
            if bad:
                if keep("fail"):
                    yield {"t": "fail", "v": vk, "s": slot, "c": bad[0], "e": bad[1]}
                    emitted += 1
                shown += 1

        slot = cands[0]
        room = find_room(vars_[vk], slot)
        place(vk, slot, room)
        assigned[vk] = 1
        touch(vk)
        stack.append({"v": vk, "cands": cands, "pos": 0})
        if keep("place"):
            yield {"t": "place", "v": vk, "s": slot, "r": rooms[room],
                   "tch": inp.teachers[vars_[vk].t]}
            emitted += 1

    snapshot()
    if placed_count < best_placed:
        reset_all()
        for vk in range(V):
            if best_slots[vk] >= 0 and violation(vk, best_slots[vk]) is None:
                place(vk, best_slots[vk], find_room(vars_[vk], best_slots[vk]))
                assigned[vk] = 1

    # ------------------------------------------------------------------ repair
    if placed_count < V and not out_of_time:
        yield {"t": "phase", "name": "Repair",
               "note": "Min-conflicts ejection for periods the search could not place"}
        emitted += 1
        queue = [vk for vk in range(V) if not assigned[vk] and not impossible[vk]]
        guard = 0
        while queue and guard < 600 and time.time() - started <= inp.time_budget_s * 1.4:
            guard += 1
            target = queue.pop(0)
            if assigned[target]:
                continue
            v = vars_[target]
            best_slot, best_eject, best_cost = -1, -1, 1e18
            for slot in v.slots:
                if any(s in unavail[v.t] for s in range(slot, slot + v.length)):
                    continue
                occ = {class_busy[v.c * NSLOTS + s] for s in range(slot, slot + v.length)}
                tocc = {teacher_busy[v.t * NSLOTS + s] for s in range(slot, slot + v.length)}
                occ.discard(-1)
                tocc.discard(-1)
                if -2 in occ or -2 in tocc:
                    continue                      # a pinned booking is not ejectable
                victims = occ | tocc
                if not victims:
                    if violation(target, slot) is None:
                        best_slot, best_eject = slot, -1
                        break
                    continue
                if len(victims) != 1:
                    continue                      # eject one thing, not a cascade
                cost = soft_of(target, slot) + 10
                if cost < best_cost:
                    best_cost, best_slot, best_eject = cost, slot, next(iter(victims))
            if best_slot < 0:
                continue
            if best_eject >= 0:
                s0 = assign_slot[best_eject]
                unplace(best_eject)
                assigned[best_eject] = 0
                touch(best_eject)
                queue.append(best_eject)
                if keep("undo"):
                    yield {"t": "undo", "v": best_eject, "s": s0}
                    emitted += 1
            if violation(target, best_slot) is not None:
                continue
            room = find_room(v, best_slot)
            place(target, best_slot, room)
            assigned[target] = 1
            touch(target)
            if keep("place"):
                yield {"t": "place", "v": target, "s": best_slot, "r": rooms[room],
                       "tch": inp.teachers[v.t]}
                emitted += 1

    # ------------------------------------------------------------------ polish
    def gap_cost(busy: list, base: int, day_len=None) -> float:
        """Periods a class or teacher sits idle BETWEEN two busy periods."""
        cost = 0.0
        for d in range(NDAYS):
            first = last = -1
            count = 0
            for p in range(PMAX):
                if busy[base + d * PMAX + p] != -1:
                    if first < 0:
                        first = p
                    last = p
                    count += 1
            if first >= 0:
                cost += (last - first + 1 - count) * 6
        return cost

    def entity_cost(vk: int) -> float:
        v = vars_[vk]
        return gap_cost(teacher_busy, v.t * NSLOTS) + gap_cost(class_busy, v.c * NSLOTS)

    def place_cost(vk: int) -> float:
        slot = assign_slot[vk]
        return 0.0 if slot < 0 else vars_[vk].priority * (slot % PMAX) * 0.35

    def total_soft() -> int:
        c = sum(gap_cost(teacher_busy, t * NSLOTS) for t in range(T))
        c += sum(gap_cost(class_busy, k * NSLOTS) for k in range(C))
        c += sum(place_cost(vk) for vk in range(V))
        return round(c)

    yield {"t": "phase", "name": "Polish",
           "note": "Hill-climbing to close gaps and pull core subjects earlier"}
    emitted += 1

    polish_deadline = started + inp.time_budget_s * 1.6
    for i in range(max(0, inp.optimize_passes) if V else 0):
        if i % 64 == 0 and time.time() > polish_deadline:
            break
        if i % 2 == 1:
            # Pairwise exchange inside one class - a different neighbourhood that
            # escapes local optima a pure relocation cannot.
            a, b = rnd.randrange(V), rnd.randrange(V)
            if a == b or not assigned[a] or not assigned[b]:
                continue
            if vars_[a].c != vars_[b].c or vars_[a].length != vars_[b].length:
                continue
            sa, sb, ra, rb = assign_slot[a], assign_slot[b], assign_room[a], assign_room[b]
            if sa == sb:
                continue
            before = entity_cost(a) + entity_cost(b) + place_cost(a) + place_cost(b)
            unplace(a)
            unplace(b)
            if violation(a, sb) is not None:
                place(a, sa, ra)
                place(b, sb, rb)
                continue
            place(a, sb, find_room(vars_[a], sb))
            if violation(b, sa) is not None:
                unplace(a)
                place(a, sa, ra)
                place(b, sb, rb)
                continue
            place(b, sa, find_room(vars_[b], sa))
            after = entity_cost(a) + entity_cost(b) + place_cost(a) + place_cost(b)
            if after < before - 0.01:
                touch(a)
                touch(b)
                if keep("swap"):
                    yield {"t": "swap", "v": a, "from": sa, "to": sb,
                           "r": rooms[assign_room[a]], "gain": round(before - after, 1)}
                    emitted += 1
            else:
                unplace(a)
                unplace(b)
                place(a, sa, ra)
                place(b, sb, rb)
            continue

        vk = rnd.randrange(V)
        if not assigned[vk]:
            continue
        frm, frm_room = assign_slot[vk], assign_room[vk]
        before = entity_cost(vk) + place_cost(vk)
        unplace(vk)
        best_slot = best_room = -1
        best_gain = 0.01
        for slot in vars_[vk].slots:
            if slot == frm or violation(vk, slot) is not None:
                continue
            room = find_room(vars_[vk], slot)
            place(vk, slot, room)
            gain = before - (entity_cost(vk) + place_cost(vk))
            unplace(vk)
            if gain > best_gain:
                best_gain, best_slot, best_room = gain, slot, room
        if best_slot >= 0:
            place(vk, best_slot, best_room)
            assigned[vk] = 1
            touch(vk)
            if keep("swap"):
                yield {"t": "swap", "v": vk, "from": frm, "to": best_slot,
                       "r": rooms[best_room], "gain": round(best_gain, 1)}
                emitted += 1
        else:
            place(vk, frm, frm_room)
            assigned[vk] = 1

    # -------------------------------------------------------------------- fill
    # An Indian engineering college day is a solid block. Anything the curriculum
    # could not fill becomes a real academic activity, never an empty hole.
    yield {"t": "phase", "name": "Fill",
           "note": "Filling every remaining period with a timetabled academic activity"}
    emitted += 1

    act_cycle = itertools.cycle([a for a in ACTIVITIES for _ in range(a[3])])
    act_load = {t: 0 for t in inp.teachers}
    filler = []
    for k in classes:
        for di, dname in enumerate(DAYS):
            for p in range(1, k.day_len[dname] + 1):
                s = slot_of(di, p)
                if class_busy[k.idx * NSLOTS + s] != -1:
                    continue
                code, name, kind, _w = next(act_cycle)
                # Mentored activities get a real teacher; library/sports do not.
                fac = None
                if code in ("ACT-PT", "ACT-MP", "ACT-RM", "ACT-MC"):
                    for t in inp.teachers:
                        m = inp.teacher_meta[t]
                        if m["dept"] != k.dept:
                            continue
                        ti = tix[t]
                        if teacher_busy[ti * NSLOTS + s] != -1 or s in unavail[ti]:
                            continue
                        if teacher_week[ti] + act_load[t] >= max_week[ti]:
                            continue
                        fac, act_load[t] = t, act_load[t] + 1
                        teacher_busy[ti * NSLOTS + s] = -3
                        break
                room = None
                if k.home_room and room_busy[rix[k.home_room] * NSLOTS + s] == -1:
                    room = k.home_room
                else:
                    for r in rooms:
                        if room_busy[rix[r] * NSLOTS + s] == -1:
                            room = r
                            break
                if room:
                    room_busy[rix[room] * NSLOTS + s] = -3
                class_busy[k.idx * NSLOTS + s] = -3
                filler.append({"cls": k.key, "day": dname, "period": p, "subject": code,
                               "faculty": fac, "room": room or k.home_room, "kind": "A"})

    # ------------------------------------------------------------------ result
    out, unplaced = [], []
    for vk in range(V):
        v = vars_[vk]
        k = classes[v.c]
        if assign_slot[vk] < 0:
            unplaced.append({"cls": k.label, "subject": inp.subjects[v.s],
                             "kind": v.room_kind, "length": v.length})
            continue
        slot = assign_slot[vk]
        for j in range(v.length):
            out.append({"cls": k.key, "day": DAYS[day_of(slot + j)],
                        "period": period_of(slot + j),
                        "subject": inp.subjects[v.s], "faculty": inp.teachers[v.t],
                        "room": rooms[assign_room[vk]],
                        "kind": "L" if v.length > 1 else "T",
                        "var": vk, "slot": slot + j})

    score = total_soft()
    result = {
        "ok": not unplaced,
        "bookings": out + filler,
        "placed": V - len(unplaced), "total": V,
        "steps": steps, "backtracks": backtracks, "conflicts": conflicts,
        "restarts": restarts, "score": score,
        "ms": int((time.time() - started) * 1000),
        "unplaced": unplaced,
        "capacity_relaxed": capacity_relaxed,
        "activity_slots": len(filler),
        "timed_out": out_of_time,
        "classes": [k.label for k in classes],
    }
    yield {"t": "done", **{x: result[x] for x in
                           ("ok", "placed", "total", "steps", "backtracks", "conflicts",
                            "restarts", "score", "ms", "activity_slots", "timed_out")},
           "unplaced": len(unplaced), "capacity_relaxed": len(capacity_relaxed)}
    return result


def run(inp: Input):
    """Drain the generator synchronously. Same code path the trace streams."""
    gen = solve(inp)
    trace = []
    try:
        while True:
            trace.append(next(gen))
    except StopIteration as stop:
        result = stop.value
    result["trace"] = trace
    return result


# --------------------------------------------------------------------------- apply


def apply(con, result, scope="all", dept=None, sem=None, section=None, actor="admin"):
    """Commit a solved timetable. Only the regenerated scope is deleted and rewritten."""
    cur = con.cursor()
    if scope == "class":
        cur.execute("DELETE FROM timetable WHERE dept=? AND sem=? AND section=?",
                    (dept, int(sem), section))
    else:
        cur.execute("DELETE FROM timetable")
    for b in result["bookings"]:
        d, sm, sec = b["cls"]
        cur.execute("""INSERT INTO timetable(dept,sem,section,day,period,subject,faculty,room,kind)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (d, sm, sec, b["day"], b["period"], b["subject"],
                     b["faculty"], b["room"], b["kind"]))
    con.commit()
    return cur.rowcount


def verify(con, scope="all", dept=None, sem=None, section=None):
    """Independent check of the committed timetable. Never trust the solver's own word."""
    where, args = "", ()
    if scope == "class":
        where, args = " WHERE dept=? AND sem=? AND section=?", (dept, int(sem), section)
    problems = []
    classes = [(r["dept"], r["sem"], r["section"]) for r in
               con.execute("SELECT DISTINCT dept,sem,section FROM timetable" + where, args)]
    for cls in classes:
        for day in DAYS:
            got = sorted(r["period"] for r in con.execute(
                "SELECT period FROM timetable WHERE dept=? AND sem=? AND section=? AND day=?",
                (*cls, day)))
            want = list(range(1, day_periods(cls[1], day) + 1))
            if got != want:
                problems.append(f"DAY BLOCK {cls} {day}: {got} != {want}")
    for r in con.execute("""SELECT day,period,faculty,COUNT(*) n FROM timetable
                            WHERE faculty IS NOT NULL
                            GROUP BY day,period,faculty HAVING n>1"""):
        problems.append(f"FACULTY CLASH {r['faculty']} {r['day']}P{r['period']} x{r['n']}")
    for r in con.execute("""SELECT day,period,room,COUNT(*) n FROM timetable
                            WHERE room IS NOT NULL
                            GROUP BY day,period,room HAVING n>1"""):
        problems.append(f"ROOM CLASH {r['room']} {r['day']}P{r['period']} x{r['n']}")
    # A standing window the committed rows teach through (#19). Only for the
    # classes in scope: a window added after the rest of the college was built
    # is the admin's known debt there, not this regeneration's failure.
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='faculty_availability'").fetchone():
        for r in con.execute("""SELECT t.faculty, t.day, t.period, a.reason FROM timetable t
                                JOIN faculty_availability a ON a.faculty=t.faculty AND a.day=t.day
                                 AND t.period BETWEEN a.p_from AND a.p_to AND a.status='Active'"""
                             + where.replace("WHERE ", "WHERE t.").replace(" AND ", " AND t."), args):
            problems.append(f"UNAVAILABLE {r['faculty']} {r['day']}P{r['period']} ({r['reason']})")
    return problems
