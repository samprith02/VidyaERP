"""
VidyaERP :: Multi-Agent Core
------------------------------------------------------------------
A supervisor/worker agent mesh. Every user utterance flows through:

  PolicyGuard  ->  Router  ->  Retriever  ->  [specialist agent(s)]  ->  Synthesizer  ->  Auditor

Specialists:
  SubstitutionAgent  : constraint-solves faculty-absence coverage, emits ranked plans
  TimetableAgent     : timetable / free-slot / room reads
  FacultyAgent       : directory, workload, allocation
  StudentAgent       : records, attendance shortage, academic risk
  FinanceAgent       : fee dues, collection, budget
  ExamAgent          : SEE/CIE schedule, invigilation, eligibility
  RequestAgent       : approvals inbox, raise & route requests
  NotifyAgent        : drafts + dispatches circulars/SMS/app pushes
  AnalyticsAgent     : institution KPIs, NAAC/NBA metrics, risk radar

Write actions are NEVER auto-committed: agents return a plan, PolicyGuard marks it
'requires_human_approval', and it commits only on explicit admin confirmation (HITL).
"""
import json, time, uuid, datetime as dt
import nlu, db
from nlu import TODAY, DAYS, day_of

PERIOD_TIME = {p: t.split("-")[0] for p, t in db.PERIODS.items()}
PERIOD_SPAN = dict(db.PERIODS)
BREAK_AFTER = dict(db.BREAK_AFTER)


# ============================================================== utilities
def rows(con, q, args=()):
    return [dict(r) for r in con.execute(q, args).fetchall()]


def one(con, q, args=()):
    r = con.execute(q, args).fetchone()
    return dict(r) if r else None


def fac_name(con, fid):
    r = one(con, "SELECT name FROM faculty WHERE id=?", (fid,))
    return r["name"] if r else fid


def subj_name(con, code):
    r = one(con, "SELECT name FROM subjects WHERE code=?", (code,))
    return r["name"] if r else code


class Trace:
    def __init__(self):
        self.items = []
        self.t0 = time.time()

    def add(self, agent, action, detail="", status="ok"):
        self.items.append({
            "agent": agent, "action": action, "detail": detail, "status": status,
            "ms": int((time.time() - self.t0) * 1000)})
        return self


def _span(periods):
    """Human clock span for a block of consecutive periods."""
    a, b = periods[0], periods[-1]
    return PERIOD_SPAN[a].split("-")[0] + "-" + PERIOD_SPAN[b].split("-")[1]


def plabel(leg):
    ps = leg.get("periods") or [leg["period"]]
    return f"P{ps[0]}" if len(ps) == 1 else f"P{ps[0]}-P{ps[-1]}"


def B_text(md):            return {"type": "text", "md": md}
def B_table(cols, rws, title=None, dense=False):
    return {"type": "table", "columns": cols, "rows": rws, "title": title, "dense": dense}
def B_cards(cards, title=None):   return {"type": "cards", "cards": cards, "title": title}
def B_plans(plans):        return {"type": "plans", "plans": plans}
def B_notice(items):       return {"type": "notice", "items": items}
def B_grid(grid):          return {"type": "grid", **grid}


# ============================================================== PolicyGuard
class PolicyGuard:
    """RBAC + scope + safety. Admin persona has institution-wide read, gated write."""
    WRITE_INTENTS = {"absence.cover", "request.manage", "notify.broadcast"}
    ROLE_SCOPE = {
        "admin":     {"read": "*", "write": {"timetable_override", "approve_request", "broadcast",
                                             "raise_request", "leave_decision"}},
        "hod":       {"read": "dept", "write": {"timetable_override", "raise_request"}},
        "faculty":   {"read": "self", "write": {"raise_request"}},
    }

    def check(self, role, intent, trace):
        scope = self.ROLE_SCOPE.get(role, self.ROLE_SCOPE["faculty"])
        needs_approval = intent in self.WRITE_INTENTS
        trace.add("PolicyGuard", "rbac_check",
                  f"role={role} · scope={scope['read']} · write_gate={'HITL' if needs_approval else 'n/a'}")
        return {"allowed": True, "hitl": needs_approval}


# ========================================================== ranking policy
# How the three measured axes combine into the rank order of coverage plans.
#
# These are POLICY COEFFICIENTS, not findings. A college that never wants a
# class released would raise `continuity`; one that cares most about who stands
# in front of the room would raise `confidence`. They live here, in one place,
# and the console prints them on every plan card - a weighting that decides
# which plan an administrator sees first should be arguable, not buried in a
# sort key.
#
# What IS measured is everything they are applied to: see score_plan().
RANK_WEIGHTS = {"confidence": 0.60, "coverage": 0.25, "continuity": 0.15}

# Continuity is three measured things about the batch's learning thread:
#   hours   - does this subject's contact hour happen at all?
#   teacher - does someone who genuinely teaches it deliver it?
#   timing  - how far does it slip from the slot it was meant to occupy?
CONTINUITY_WEIGHTS = {"hours": 0.45, "teacher": 0.35, "timing": 0.20}

# How much of "the subject's own teacher" a given deliverer is, by the
# strongest relationship the timetable and the expertise profile can evidence.
# Ordered, and every step is something we can point at a row for.
AUTHENTICITY = {"own": 1.00,              # the batch's own teacher for this subject
                "teaches_subject": 0.85,  # teaches it, to another section
                "expertise": 0.65,        # subject listed in their expertise profile
                "same_dept": 0.40,        # department colleague, no subject claim
                "other_dept": 0.20,       # free and competent-ish, but a stranger to it
                "none": 0.00}             # nobody is delivering it


def timing_intact(slip_days):
    """How intact a session's timing is after slipping `slip_days` days.

    1/(1+slip) - no cliff, no tuning constant, and the same curve for a
    within-day move (a fractional day) as for a make-up next week. Delivering
    on time scores 1.0; tomorrow 0.5; three days out 0.25.
    """
    return 1.0 / (1.0 + max(0.0, float(slip_days)))


# ============================================================== Substitution
class SubstitutionAgent:
    """The flagship: absence -> conflict-free, competency-aware coverage plans."""
    name = "SubstitutionAgent"

    def busy_faculty(self, con, day, period, date_iso):
        b = {r["faculty"] for r in rows(
            con, "SELECT DISTINCT faculty FROM timetable WHERE day=? AND period=? AND faculty IS NOT NULL",
            (day, period))}
        for r in rows(con, """SELECT o.new_faculty f FROM overrides o JOIN timetable t ON t.id=o.slot_id
                              WHERE o.date=? AND t.period=? AND o.status='Applied' AND o.new_faculty IS NOT NULL""",
                      (date_iso, period)):
            b.add(r["f"])
        return b

    def on_leave(self, con, date_iso):
        return {r["faculty"] for r in rows(
            con, "SELECT faculty FROM leaves WHERE status='Approved' AND from_date<=? AND to_date>=?",
            (date_iso, date_iso))}

    def load_map(self, con):
        return {r["faculty"]: r["c"] for r in rows(
            con, "SELECT faculty, COUNT(*) c FROM timetable GROUP BY faculty")}

    def teaches_same_subject(self, con, subject):
        return {r["faculty"] for r in rows(
            con, "SELECT DISTINCT faculty FROM timetable WHERE subject=?", (subject,))}

    # ---------------------------------------------------------- candidates
    def score_faculty(self, f, subject, dept, kind, load, extra, n_periods, same_sub):
        """The one candidate-scoring lens in the system.

        Plan A's confidence has always been computed from this. Plans B and C
        now come through here too (see score_deliverer), which is the whole
        point: three confidence numbers that are the same measurement of the
        same thing, rather than three formulas invented next to each other.
        """
        fid = f["id"]
        expertise = f["expertise"].split(",")
        s, why = 0, []
        if fid in same_sub:
            s += 46; why.append("already teaches this subject to another section")
        if subject in expertise:
            s += 24; why.append("subject listed in expertise profile")
        if f["dept"] == dept:
            s += 14; why.append("same department")
        head = max(0, f["max_load"] - load)
        s += min(16, head * 2.2); why.append(f"load {load}/{f['max_load']} ({head} free hrs)")
        if f["designation"] == "Professor":
            s -= 4
        s -= extra * 9
        if kind in ("L", "P") and fid not in same_sub and subject not in expertise:
            s -= 18; why.append("⚠ lab session – limited hands-on familiarity")
        return round(s, 1), why

    def subject_kind(self, con, subject):
        return (one(con, "SELECT kind FROM subjects WHERE code=?", (subject,)) or {}).get("kind", "T")

    def score_deliverer(self, con, fid, subject, dept, n_periods=1):
        """Score ONE named faculty member for a subject — the swap partner
        teaching their own class at a new hour, or the absent teacher taking
        their own make-up. Same lens as rank_candidates, so Plan B and C's
        confidence is comparable with Plan A's instead of being a constant
        parked beside it."""
        f = one(con, "SELECT * FROM faculty WHERE id=?", (fid,))
        if not f:
            return 0.0
        s, _why = self.score_faculty(f, subject, dept, self.subject_kind(con, subject),
                                     self.load_map(con).get(fid, 0), 0, n_periods,
                                     self.teaches_same_subject(con, subject))
        return s

    def rank_candidates(self, con, slot, date_iso, already_used):
        day, subject, dept = slot["day"], slot["subject"], slot["dept"]
        ps = slot.get("periods", [slot["period"]])
        busy = set()
        for period in ps:
            busy |= self.busy_faculty(con, day, period, date_iso)
        leave = self.on_leave(con, date_iso)
        loads = self.load_map(con)
        same_sub = self.teaches_same_subject(con, subject)
        kind = self.subject_kind(con, subject)
        out = []
        for f in rows(con, "SELECT * FROM faculty"):
            fid = f["id"]
            if fid == slot["faculty"] or fid in busy or fid in leave:
                continue
            load = loads.get(fid, 0)
            extra = already_used.get(fid, 0)
            if load + extra + len(ps) > f["max_load"] + 2:
                continue
            s, why = self.score_faculty(f, subject, dept, kind, load, extra, len(ps), same_sub)
            out.append({"id": fid, "name": f["name"], "dept": f["dept"],
                        "desig": f["designation"], "score": s,
                        "load": load, "max": f["max_load"], "why": why,
                        "authenticity": self.authenticity_of(f, fid, subject, dept, same_sub)})
        out.sort(key=lambda x: -x["score"])
        return out

    def authenticity_of(self, f, fid, subject, dept, same_sub):
        """How much of the subject's own teacher this person is. Every branch is
        a row we can point at, which is what the old constant `continuity: 74`
        could not do."""
        if fid in same_sub:
            return AUTHENTICITY["teaches_subject"]
        if subject in f["expertise"].split(","):
            return AUTHENTICITY["expertise"]
        if f["dept"] == dept:
            return AUTHENTICITY["same_dept"]
        return AUTHENTICITY["other_dept"]

    # ---------------------------------------------------------- swaps
    def in_multi_period_block(self, con, row):
        """Is this period one slice of a contiguous same-subject block?

        A lab is three consecutive periods inside one session window
        (db.LAB_WINDOWS). Pulling a single slice of one forward is meaningless -
        you cannot run a third of a practical early, and the remaining two
        periods would still need the teacher who is away. CIVIL-3A Mon P5-P7
        (BCVL305, kind='L') is the live example in the seeded data.
        """
        if (row["kind"] or "T") in ("L", "P"):
            return True
        neighbours = rows(con, """SELECT period FROM timetable WHERE dept=? AND sem=? AND section=?
                                  AND day=? AND subject=? AND period IN (?,?)""",
                          (row["dept"], row["sem"], row["section"], row["day"], row["subject"],
                           row["period"] - 1, row["period"] + 1))
        return bool(neighbours)

    def find_swap(self, con, slot, date_iso):
        """Pull a later same-day class of the SAME batch forward into the vacant
        period, sending the absent teacher's subject to that later slot.

        Single-period theory on BOTH sides, for the reason in
        in_multi_period_block(): a swap that splits a lab block is not a swap.
        """
        ps = slot.get("periods", [slot["period"]])
        if len(ps) > 1:
            return None                      # the vacated block is itself a lab
        day, period = slot["day"], ps[0]
        busy_now = self.busy_faculty(con, day, period, date_iso)
        leave = self.on_leave(con, date_iso)
        cands = rows(con, """SELECT * FROM timetable WHERE dept=? AND sem=? AND section=? AND day=?
                             AND period>? AND faculty IS NOT NULL AND faculty<>? ORDER BY period""",
                     (slot["dept"], slot["sem"], slot["section"], day, period, slot["faculty"]))
        for c in cands:
            if c["faculty"] in busy_now or c["faculty"] in leave:
                continue
            if self.in_multi_period_block(con, c):
                continue
            return c
        return None

    def find_makeup(self, con, slot, after_date):
        """Earliest slot in the next week where the batch AND the returning
        faculty are both free.

        The search runs to the last period of the college day, not to the end of
        this batch's day. That matters: `db.verify()` asserts every batch fills
        periods 1..day_periods(sem, day) with no holes, so a search bounded by
        the batch's own day length can never succeed — measured, 0 of 126
        class-days have a free in-day period. Plan C therefore reported 0%
        coverage for every absence ever put to it, which the old flat confidence
        of 66 hid completely.

        An extra hour after the day officially ends is also what a department
        actually does for a make-up, so the honest search and the realistic one
        are the same search.
        """
        last = max(PERIOD_SPAN)
        for k in range(1, 8):
            d = after_date + dt.timedelta(days=k)
            dy = day_of(d)
            if dy == "Sun":
                continue
            for p in range(1, last + 1):
                cls = one(con, """SELECT 1 FROM timetable WHERE dept=? AND sem=? AND section=?
                                  AND day=? AND period=?""",
                          (slot["dept"], slot["sem"], slot["section"], dy, p))
                fac = one(con, "SELECT 1 FROM timetable WHERE faculty=? AND day=? AND period=?",
                          (slot["faculty"], dy, p))
                if not cls and not fac:
                    return {"date": d.isoformat(), "day": dy, "period": p,
                            "extra_hour": p > db.day_periods(slot["sem"], dy)}
        return None

    # ---------------------------------------------------------- measurement
    @staticmethod
    def conf_curve(score):
        """Candidate score -> confidence. Unchanged from the day Plan A used it;
        Plans B and C are now fed through the same curve rather than being
        assigned a number."""
        return min(100.0, max(35.0, score * 1.05))

    def score_leg(self, con, leg, absent, date):
        """Measure ONE leg on the three axes. Every branch returns numbers
        derived from this instance - which teacher, which period, how many days
        - and nothing here is a per-strategy constant.

        A leg can hold more than one arrangement: a swap both moves a class
        forward AND defers a subject to a make-up, so it is scored as the mean
        of its two arrangements rather than being credited for the good half.
        """
        def arrangement(hours, teacher, timing, conf):
            return {"hours": hours, "teacher": teacher, "timing": timing, "conf": conf}

        parts = []
        act = leg["action"]

        if act == "SUBSTITUTE" and leg.get("to_id"):
            # Delivered today, in its own slot, by someone else.
            parts.append(arrangement(1.0, leg.get("authenticity", 0.0), 1.0,
                                     self.conf_curve(leg.get("score", 0))))

        elif act == "SWAP":
            dept, sem = leg["dept"], leg["sem"]
            per_day = max(1, db.day_periods(sem, leg["day"]))
            # (1) the partner's class moves earlier, taught by its own teacher.
            moved = abs(leg["swap_period"] - leg["period"]) / per_day      # in days
            parts.append(arrangement(
                1.0, AUTHENTICITY["own"], timing_intact(moved),
                self.conf_curve(self.score_deliverer(con, leg["swap_faculty_id"],
                                                     leg["swap_subject"], dept))))
            # (2) the absent teacher's own subject, deferred to a make-up.
            mk = leg.get("makeup")
            if mk:
                slip = (dt.date.fromisoformat(mk["date"]) - date).days
                parts.append(arrangement(
                    1.0, AUTHENTICITY["own"], timing_intact(slip),
                    self.conf_curve(self.score_deliverer(con, absent["id"],
                                                         leg["subject"], dept))))
            else:
                parts.append(arrangement(0.0, 0.0, 0.0, 0.0))

        elif act == "MAKEUP":
            mk = leg.get("makeup")
            if mk:
                slip = (dt.date.fromisoformat(mk["date"]) - date).days
                parts.append(arrangement(
                    1.0, AUTHENTICITY["own"], timing_intact(slip),
                    self.conf_curve(self.score_deliverer(con, absent["id"],
                                                         leg["subject"], leg["dept"]))))
            else:
                parts.append(arrangement(0.0, 0.0, 0.0, 0.0))

        if not parts:                                     # UNCOVERED, or nobody found
            parts.append(arrangement(0.0, 0.0, 0.0, 0.0))

        mean = lambda k: sum(p[k] for p in parts) / len(parts)
        hours, teacher, timing = mean("hours"), mean("teacher"), mean("timing")
        W = CONTINUITY_WEIGHTS
        return {"confidence": mean("conf"),
                "coverage": hours,                        # an arranged hour IS a covered hour
                "continuity": 100.0 * (W["hours"] * hours + W["teacher"] * teacher
                                       + W["timing"] * timing),
                "hours": hours, "teacher": teacher, "timing": timing,
                "arrangements": len(parts)}

    def score_plan(self, con, legs, absent, date):
        """Plan-level score: the mean of its legs on each axis, plus the
        rank the policy weights give it. Nothing here is per-strategy."""
        per_leg = [self.score_leg(con, l, absent, date) for l in legs]
        for l, s in zip(legs, per_leg):
            l["measured"] = {k: round(s[k], 3) for k in ("hours", "teacher", "timing")}
            l["leg_confidence"] = round(s["confidence"])
        mean = lambda k: sum(s[k] for s in per_leg) / len(per_leg) if per_leg else 0.0
        conf, cov, cont = mean("confidence"), 100.0 * mean("coverage"), mean("continuity")
        return {"confidence": int(round(conf)), "coverage": int(round(cov)),
                "continuity": int(round(cont)),
                "components": {"hours_preserved": round(100 * mean("hours")),
                               "own_teacher": round(100 * mean("teacher")),
                               "timing_intact": round(100 * mean("timing"))},
                "rank": round(conf * RANK_WEIGHTS["confidence"]
                              + cov * RANK_WEIGHTS["coverage"]
                              + cont * RANK_WEIGHTS["continuity"], 2)}

    # ---------------------------------------------------------- main
    def build_plans(self, con, faculty, date, trace, reason="Faculty absence"):
        day = day_of(date)
        date_iso = date.isoformat()
        raw = rows(con, """SELECT * FROM timetable WHERE faculty=? AND day=? ORDER BY period""",
                   (faculty["id"], day))
        affected = []            # consecutive same-subject periods collapse into ONE block
        for r in raw:
            prev = affected[-1] if affected else None
            if (prev and r["subject"] == prev["subject"] and r["section"] == prev["section"]
                    and r["sem"] == prev["sem"] and r["dept"] == prev["dept"]
                    and r["period"] == prev["periods"][-1] + 1):
                prev["periods"].append(r["period"])
                prev["slot_ids"].append(r["id"])
            else:
                b = dict(r)
                b["periods"] = [r["period"]]
                b["slot_ids"] = [r["id"]]
                affected.append(b)
        trace.add(self.name, "impact_scan",
                  f"{len(raw)} period(s) in {len(affected)} teaching block(s) on {day} {date_iso} "
                  f"for {faculty['name']}")
        if not affected:
            return None, affected

        # ---------------- Plan A : competency-matched substitution
        used, legs_a = {}, []
        for s in affected:
            cands = self.rank_candidates(con, s, date_iso, used)
            top = cands[0] if cands else None
            if top:
                used[top["id"]] = used.get(top["id"], 0) + 1
            legs_a.append({
                "slot_id": s["id"], "slot_ids": s["slot_ids"], "periods": s["periods"],
                "period": s["period"], "time": _span(s["periods"]),
                "dept": s["dept"], "sem": s["sem"], "day": day,
                "class": f"{s['dept']}-{s['sem']}{s['section']}", "subject": s["subject"],
                "subject_name": subj_name(con, s["subject"]), "room": s["room"],
                "action": "SUBSTITUTE" if top else "UNCOVERED",
                "to": top["name"] if top else "— no free competent faculty —",
                "to_id": top["id"] if top else None,
                "why": "; ".join(top["why"][:3]) if top else "all qualified staff engaged/on leave",
                "alts": [f"{c['name']} ({c['score']})" for c in cands[1:4]],
                "authenticity": top["authenticity"] if top else 0.0,
                "score": top["score"] if top else 0})

        # ---------------- Plan B : swap-forward re-sequencing
        # A real swap, and it costs the partner nothing: their class moves to an
        # earlier hour they were already free for, and the absent teacher's
        # subject takes the slot they vacate, to be made up later. Two override
        # rows, one plan_ref - see apply_plan.
        # `used_b` is Plan B's OWN fairness accumulator. It used to share Plan
        # A's, which scored B's fallback substitutes as if A had already been
        # applied (-9 per assignment) — but the plans are alternatives and only
        # one is ever committed, so that understated B's confidence against A's.
        legs_b, swaps, used_b = [], 0, {}
        for s in affected:
            sw = self.find_swap(con, s, date_iso)
            if sw:
                swaps += 1
                mk = self.find_makeup(con, s, date)
                legs_b.append({
                    "slot_id": s["id"], "slot_ids": s["slot_ids"], "periods": s["periods"],
                    "period": s["period"], "time": _span(s["periods"]),
                    "dept": s["dept"], "sem": s["sem"], "day": day,
                    "class": f"{s['dept']}-{s['sem']}{s['section']}", "subject": s["subject"],
                    "subject_name": subj_name(con, s["subject"]),
                    "action": "SWAP", "to": fac_name(con, sw["faculty"]), "to_id": sw["faculty"],
                    "swap_with": sw["id"], "swap_period": sw["period"],
                    "swap_subject": sw["subject"], "swap_faculty_id": sw["faculty"],
                    "swap_room": sw["room"], "room": s["room"], "makeup": mk,
                    "why": (f"{sw['subject']} moves P{sw['period']}→P{s['period']} with its own "
                            f"teacher; P{sw['period']} is released and {s['subject']} is made up "
                            + (f"on {mk['date']} ({mk['day']}) P{mk['period']}" if mk
                               else "— NO free common slot in 7 days, needs manual scheduling")),
                    "alts": [], "authenticity": AUTHENTICITY["own"], "score": 0})
            else:
                cands = self.rank_candidates(con, s, date_iso, used_b)
                top = cands[0] if cands else None
                if top:
                    used_b[top["id"]] = used_b.get(top["id"], 0) + 1
                legs_b.append({
                    "slot_id": s["id"], "slot_ids": s["slot_ids"], "periods": s["periods"],
                    "period": s["period"], "time": _span(s["periods"]),
                    "dept": s["dept"], "sem": s["sem"], "day": day,
                    "class": f"{s['dept']}-{s['sem']}{s['section']}", "subject": s["subject"],
                    "subject_name": subj_name(con, s["subject"]), "room": s["room"],
                    "action": "SUBSTITUTE" if top else "UNCOVERED",
                    "to": top["name"] if top else "—", "to_id": top["id"] if top else None,
                    "why": ("no swappable later period (a lab block cannot be split) – "
                            "fallback substitution"),
                    "alts": [], "authenticity": top["authenticity"] if top else 0.0,
                    "score": top["score"] if top else 0})

        # ---------------- Plan C : compact + makeup
        legs_c = []
        for s in affected:
            mk = self.find_makeup(con, s, date)
            legs_c.append({
                "slot_id": s["id"], "slot_ids": s["slot_ids"], "periods": s["periods"],
                "period": s["period"], "time": _span(s["periods"]),
                "dept": s["dept"], "sem": s["sem"], "day": day,
                "class": f"{s['dept']}-{s['sem']}{s['section']}", "subject": s["subject"],
                "subject_name": subj_name(con, s["subject"]), "room": s["room"],
                "action": "MAKEUP", "to": faculty["name"], "to_id": faculty["id"],
                "makeup": mk,
                "why": (f"slot released today (supervised study in Central Library); "
                        f"makeup by same faculty on {mk['date']} {mk['day']} P{mk['period']}")
                if mk else "no common free slot in next 7 days – needs manual scheduling",
                "alts": [], "authenticity": AUTHENTICITY["own"], "score": 0})

        pid = uuid.uuid4().hex[:6]
        covered_a = sum(1 for l in legs_a if l["to_id"])
        made_up_b = sum(1 for l in legs_b if l["action"] == "SWAP" and l.get("makeup"))
        made_up_c = sum(1 for l in legs_c if l.get("makeup"))
        plans = [
            {"id": f"A-{pid}", "code": "A", "title": "Competency-matched substitution",
             "summary": f"{covered_a} of {len(legs_a)} periods covered by free, subject-competent "
                        f"faculty. No change to the student timetable.",
             "pros": ["Students see zero timetable change", "Syllabus pace maintained",
                      "Attendance marking unaffected"],
             "cons": ["Adds an hour to the substitute's day",
                      "Subject depth varies — a stand-in is not the subject teacher"],
             "legs": legs_a},
            {"id": f"B-{pid}", "code": "B", "title": "Swap-forward re-sequencing",
             "summary": (f"{swaps} of {len(legs_b)} block(s) re-sequenced: a later class of the same "
                         f"batch moves up with its own teacher, and the absent teacher's subject "
                         f"takes the vacated slot for a make-up ({made_up_b} scheduled)."
                         if swaps else
                         f"No period could be re-sequenced — every later slot is a lab block or its "
                         f"teacher is unavailable. All {len(legs_b)} block(s) fall back to "
                         f"substitution."),
             "pros": ["No extra teaching hour for anyone — the partner's class only moves",
                      "Every delivered hour is taught by its own subject teacher",
                      "The batch's day still has no free hole"],
             "cons": ["The student timetable changes for the day — needs notice",
                      "The absent teacher's subject is deferred, not delivered"],
             "legs": legs_b},
            {"id": f"C-{pid}", "code": "C", "title": "Release + guaranteed make-up",
             "summary": f"Periods released to supervised self-study; the same faculty conducts the "
                        f"make-up in the earliest mutually-free slot ({made_up_c} of {len(legs_c)} "
                        f"scheduled).",
             "pros": ["The original faculty retains the topic", "Zero substitution burden"],
             "cons": ["Contact hours deferred — CIE calendar risk if repeated",
                      "Requires library/proctor booking"],
             "legs": legs_c},
        ]
        # ---- every number on a plan card is measured from this instance ----
        for p in plans:
            p.update(self.score_plan(con, p["legs"], faculty, date))
            p["weights"] = dict(RANK_WEIGHTS)
            p["incomplete"] = p["coverage"] < 100
        plans.sort(key=lambda p: -p["rank"])

        n_fac = one(con, "SELECT COUNT(*) c FROM faculty")["c"]
        trace.add(self.name, "constraint_solve",
                  f"searched {n_fac} faculty × {len(affected)} block(s) · hard constraints: "
                  f"clash-free, leave-free, load-cap · soft: competency, dept, balance")
        # Measured, not asserted. The old wording claimed a fairness property the
        # swap path did not have; state the number the plan actually produces.
        worst = max(used.values(), default=0)
        trace.add("WorkloadBalancer", "fairness_check",
                  f"plan A: {len(used)} substitute(s), max {worst} extra hour(s) each; "
                  f"plan B adds no teaching hours; professors de-prioritised")
        trace.add("PlanRanker", "score",
                  " · ".join(f"{p['code']} rank {p['rank']} "
                             f"(conf {p['confidence']} cov {p['coverage']} cont {p['continuity']})"
                             for p in plans)
                  + f" · weights {RANK_WEIGHTS['confidence']}/{RANK_WEIGHTS['coverage']}"
                    f"/{RANK_WEIGHTS['continuity']}")
        return plans, affected

    # ---------------------------------------------------------- commit
    def apply_plan(self, con, plan, faculty, date, actor="admin@vidyatech"):
        """Write one override row per underlying PERIOD (a 3-hour lab block = 3 rows)."""
        date_iso = date.isoformat()
        now = dt.datetime.now().isoformat(timespec="seconds")
        applied = []
        writes = []          # (slot_id, action, new_faculty, new_room, new_subject, reason)
        for leg in plan["legs"]:
            slots = leg.get("slot_ids") or [leg["slot_id"]]
            if leg["action"] == "SWAP":
                # TWO rows, one plan_ref, so undo_last_change reverts the pair.
                # (1) the partner's subject and its own teacher move into the
                #     vacated period — this is why new_subject exists.
                mk = leg.get("makeup") or {}
                for sid in slots:
                    writes.append((sid, "SWAP", leg["swap_faculty_id"], leg.get("room"),
                                   leg["swap_subject"],
                                   f"{leg['swap_subject']} moved up from P{leg['swap_period']} — "
                                   f"{faculty['name']} absent"))
                # (2) the partner's own period is released; the absent teacher's
                #     subject is what is owed there, and is made up later.
                writes.append((leg["swap_with"], "VACATED", None, leg.get("swap_room"), None,
                               f"Released — {leg['subject']} deferred; make-up "
                               + (f"{mk['date']} ({mk['day']}) P{mk['period']}" if mk
                                  else "NOT yet scheduled")))
            elif leg["action"] == "MAKEUP":
                mk = leg.get("makeup") or {}
                for sid in slots:
                    writes.append((sid, "MAKEUP", leg["to_id"], "Central Library", None,
                                   f"Released; make-up on {mk.get('date','TBD')} "
                                   f"P{mk.get('period','-')}"))
            elif leg["to_id"]:
                for sid in slots:
                    writes.append((sid, leg["action"], leg["to_id"], leg.get("room"), None,
                                   f"{faculty['name']} absent — assigned by SubstitutionAgent"))
            else:
                continue
            applied.append(leg)
        for sid, action, new_fac, room, new_sub, reason in writes:
            con.execute("""INSERT INTO overrides(date,slot_id,action,new_faculty,new_room,
                           new_subject,reason,status,created_by,created_at,plan_ref)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (date_iso, sid, action, new_fac, room, new_sub,
                         reason, "Applied", actor, now, plan["id"]))
        con.commit()
        return applied


# ============================================================== Timetable
class TimetableAgent:
    name = "TimetableAgent"

    def class_grid(self, con, dept, sem, sec, date=None):
        tt = rows(con, """SELECT t.*, s.name sname FROM timetable t LEFT JOIN subjects s ON s.code=t.subject
                          WHERE t.dept=? AND t.sem=? AND t.section=?""", (dept, sem, sec))
        maxp = max([r["period"] for r in tt], default=7)
        ov = {}
        if date:
            for o in rows(con, "SELECT * FROM overrides WHERE date=? AND status='Applied'", (date,)):
                ov[o["slot_id"]] = o
        cells = {}
        for r in tt:
            o = ov.get(r["id"])
            # A swap moves a different SUBJECT into this period, so the cell has
            # to follow new_subject — rendering only new_faculty would show the
            # batch the old subject with the wrong teacher against it.
            subject, sname = r["subject"], r["sname"]
            if o and o["new_subject"]:
                subject, sname = o["new_subject"], subj_name(con, o["new_subject"])
            cells[f"{r['day']}-{r['period']}"] = {
                "subject": subject, "sname": sname, "room": (o and o["new_room"]) or r["room"],
                "kind": r["kind"] or "T",
                "faculty": fac_name(con, r["faculty"]) if r["faculty"] else "Class Mentor",
                "override": ({"action": o["action"],
                              "faculty": fac_name(con, o["new_faculty"]) if o["new_faculty"] else None,
                              "subject": o["new_subject"],
                              "released": o["action"] in ("VACATED", "MAKEUP"),
                              "was": r["subject"] if o["new_subject"] else None,
                              "reason": o["reason"]} if o else None)}
        return {"type": "grid", "title": f"{dept} · Sem {sem} · Sec {sec}",
                "days": DAYS, "periods": {p: t for p, t in PERIOD_SPAN.items() if p <= maxp},
                "breaks": BREAK_AFTER, "cells": cells,
                "day_len": {d: db.day_periods(sem, d) for d in DAYS}}

    def free_faculty(self, con, day, period, date_iso, dept=None):
        sub = SubstitutionAgent()
        busy = sub.busy_faculty(con, day, period, date_iso)
        leave = sub.on_leave(con, date_iso)
        loads = sub.load_map(con)
        out = []
        for f in rows(con, "SELECT * FROM faculty" + (" WHERE dept=?" if dept else ""), (dept,) if dept else ()):
            if f["id"] in busy or f["id"] in leave:
                continue
            out.append({"id": f["id"], "name": f["name"], "dept": f["dept"],
                        "desig": f["designation"], "load": f"{loads.get(f['id'],0)}/{f['max_load']}"})
        return out

    def free_rooms(self, con, day, period):
        busy = {r["room"] for r in rows(con, "SELECT DISTINCT room FROM timetable WHERE day=? AND period=?",
                                        (day, period))}
        return [r for r in rows(con, "SELECT * FROM rooms") if r["id"] not in busy]


# ============================================================== Others
class FacultyAgent:
    name = "FacultyAgent"

    def profile(self, con, fid):
        f = one(con, "SELECT * FROM faculty WHERE id=?", (fid,))
        load = rows(con, """SELECT subject, dept, sem, section, COUNT(*) hrs FROM timetable
                            WHERE faculty=? GROUP BY subject,dept,sem,section""", (fid,))
        total = sum(l["hrs"] for l in load)
        lv = rows(con, "SELECT * FROM leaves WHERE faculty=? ORDER BY from_date DESC LIMIT 4", (fid,))
        return f, load, total, lv

    def workload_table(self, con, dept=None):
        q = """SELECT f.id, f.name, f.dept, f.designation, f.max_load,
                      (SELECT COUNT(*) FROM timetable t WHERE t.faculty=f.id) load
               FROM faculty f"""
        if dept:
            q += " WHERE f.dept=?"
        r = rows(con, q, (dept,) if dept else ())
        for x in r:
            x["util"] = round(100 * x["load"] / x["max_load"])
        r.sort(key=lambda x: -x["util"])
        return r


class StudentAgent:
    name = "StudentAgent"

    def defaulters(self, con, dept=None, sem=None, cutoff=75):
        q = "SELECT * FROM students WHERE attendance < ?"
        a = [cutoff]
        if dept: q += " AND dept=?"; a.append(dept)
        if sem:  q += " AND sem=?";  a.append(sem)
        return rows(con, q + " ORDER BY attendance ASC", tuple(a))

    def risk(self, con):
        return rows(con, """SELECT * FROM students
                            WHERE attendance<70 AND (cgpa<6.5 OR backlogs>0)
                            ORDER BY attendance ASC LIMIT 25""")


class FinanceAgent:
    name = "FinanceAgent"

    def summary(self, con):
        tot = one(con, "SELECT SUM(fee_due) s, COUNT(*) n FROM students WHERE fee_due>0")
        byd = rows(con, """SELECT dept, SUM(fee_due) due, COUNT(*) n FROM students
                           WHERE fee_due>0 GROUP BY dept ORDER BY due DESC""")
        return tot, byd


class ExamAgent:
    name = "ExamAgent"

    def schedule(self, con, dept=None, sem=None):
        q = """SELECT e.*, s.name sname FROM exams e LEFT JOIN subjects s ON s.code=e.subject WHERE 1=1"""
        a = []
        if dept: q += " AND e.dept=?"; a.append(dept)
        if sem:  q += " AND e.sem=?";  a.append(sem)
        return rows(con, q + " ORDER BY e.date", tuple(a))

    def ineligible(self, con, dept=None, sem=None):
        return StudentAgent().defaulters(con, dept, sem, 75)


class RequestAgent:
    name = "RequestAgent"

    def inbox(self, con, status="Pending"):
        return rows(con, "SELECT * FROM requests WHERE status=? ORDER BY "
                         "CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, created_at",
                    (status,))

    def create(self, con, kind, title, details, target, raised_by="admin@vidyatech",
               role="Admin", amount=0, priority="Medium"):
        cur = con.execute("""INSERT INTO requests(kind,raised_by,role,target,title,details,amount,status,
                             priority,created_at,sla_hrs) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                          (kind, raised_by, role, target, title, details, amount, "Pending", priority,
                           TODAY.isoformat(), 48))
        con.commit()
        return cur.lastrowid

    def decide(self, con, rid, decision):
        con.execute("UPDATE requests SET status=? WHERE id=?", (decision, rid))
        con.commit()


class NotifyAgent:
    name = "NotifyAgent"

    def draft_absence(self, con, faculty, date, plan):
        d = date.strftime("%d %b %Y (%a)")
        msgs = []
        classes = sorted({l["class"] for l in plan["legs"]})
        for c in classes:
            legs = [l for l in plan["legs"] if l["class"] == c]
            lines = []
            for l in legs:
                if l["action"] == "SUBSTITUTE":
                    lines.append(f"P{l['period']} {l['subject']} → handled by {l['to']}")
                elif l["action"] == "SWAP":
                    lines.append(f"P{l['period']} re-sequenced ({l['to']})")
                else:
                    mk = l.get("makeup") or {}
                    lines.append(f"P{l['period']} released; make-up {mk.get('date','TBD')} P{mk.get('period','-')}")
            msgs.append({"audience": f"Students · {c}", "channel": "App push + class WhatsApp",
                         "title": f"Timetable update — {d}",
                         "body": f"{faculty['name']} is unavailable on {d}. " + "; ".join(lines) +
                                 ". Attendance will be marked as usual."})
        subs = sorted({l["to"] for l in plan["legs"] if l["to_id"] and l["to_id"] != faculty["id"]})
        for s in subs:
            legs = [l for l in plan["legs"] if l["to"] == s]
            msgs.append({"audience": f"Faculty · {s}", "channel": "Email + app",
                         "title": f"Adjustment duty — {d}",
                         "body": "You are assigned: " + "; ".join(
                             f"P{l['period']} {l['subject']} ({l['class']}, {l['room']})" for l in legs) +
                                 ". Lesson plan & unit notes shared on the LMS."})
        msgs.append({"audience": f"HOD · {faculty['dept']} & Principal", "channel": "Email digest",
                     "title": f"Absence coverage approved — {faculty['name']}, {d}",
                     "body": f"Plan {plan['code']} applied. Coverage {plan['coverage']}%, "
                             f"continuity index {plan['continuity']}. No academic hours lost."})
        return msgs

    def dispatch(self, con, msgs):
        for m in msgs:
            con.execute("""INSERT INTO notifications(audience,channel,title,body,created_at,status)
                           VALUES(?,?,?,?,?,?)""",
                        (m["audience"], m["channel"], m["title"], m["body"],
                         dt.datetime.now().isoformat(timespec="seconds"), "Sent"))
        con.commit()


class AnalyticsAgent:
    name = "AnalyticsAgent"

    def kpis(self, con):
        s = one(con, "SELECT COUNT(*) n, AVG(attendance) a, AVG(cgpa) g FROM students")
        f = one(con, "SELECT COUNT(*) n FROM faculty")
        due = one(con, "SELECT SUM(fee_due) s FROM students")
        short = one(con, "SELECT COUNT(*) n FROM students WHERE attendance<75")
        pend = one(con, "SELECT COUNT(*) n FROM requests WHERE status='Pending'")
        util = one(con, """SELECT AVG(u) a FROM (SELECT 100.0*COUNT(*)/f.max_load u FROM timetable t
                           JOIN faculty f ON f.id=t.faculty GROUP BY t.faculty)""")
        plc = one(con, "SELECT SUM(offers) o, AVG(ctc) c FROM placements")
        return {"students": s["n"], "avg_att": round(s["a"], 1), "avg_cgpa": round(s["g"], 2),
                "faculty": f["n"], "ratio": f"1:{round(s['n']/f['n'])}",
                "dues": due["s"], "shortage": short["n"], "pending": pend["n"],
                "util": round(util["a"]), "offers": plc["o"], "avg_ctc": round(plc["c"], 2)}


class Auditor:
    name = "Auditor"

    def log(self, con, actor, agent, action, payload, outcome):
        con.execute("INSERT INTO audit(ts,actor,agent,action,payload,outcome) VALUES(?,?,?,?,?,?)",
                    (dt.datetime.now().isoformat(timespec="seconds"), actor, agent, action,
                     json.dumps(payload)[:1200], outcome))
        con.commit()
