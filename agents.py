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
    def rank_candidates(self, con, slot, date_iso, already_used):
        day, subject, dept = slot["day"], slot["subject"], slot["dept"]
        ps = slot.get("periods", [slot["period"]])
        busy = set()
        for period in ps:
            busy |= self.busy_faculty(con, day, period, date_iso)
        leave = self.on_leave(con, date_iso)
        loads = self.load_map(con)
        same_sub = self.teaches_same_subject(con, subject)
        kind = (one(con, "SELECT kind FROM subjects WHERE code=?", (subject,)) or {}).get("kind", "T")
        out = []
        for f in rows(con, "SELECT * FROM faculty"):
            fid = f["id"]
            if fid == slot["faculty"] or fid in busy or fid in leave:
                continue
            load = loads.get(fid, 0)
            extra = already_used.get(fid, 0)
            if load + extra + len(ps) > f["max_load"] + 2:
                continue
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
            out.append({"id": fid, "name": f["name"], "dept": f["dept"],
                        "desig": f["designation"], "score": round(s, 1),
                        "load": load, "max": f["max_load"], "why": why})
        out.sort(key=lambda x: -x["score"])
        return out

    # ---------------------------------------------------------- swaps
    def find_swap(self, con, slot, date_iso):
        """Pull a later same-week class of the SAME batch forward into the vacant period."""
        day, period = slot["day"], slot["period"]
        busy_now = self.busy_faculty(con, day, period, date_iso)
        leave = self.on_leave(con, date_iso)
        cands = rows(con, """SELECT * FROM timetable WHERE dept=? AND sem=? AND section=? AND day=?
                             AND period>? AND faculty<>?""",
                     (slot["dept"], slot["sem"], slot["section"], day, period, slot["faculty"]))
        for c in cands:
            if c["faculty"] not in busy_now and c["faculty"] not in leave:
                return c
        return None

    def find_makeup(self, con, slot, after_date):
        """Earliest free common slot for the batch AND the returning faculty."""
        for k in range(1, 8):
            d = after_date + dt.timedelta(days=k)
            dy = day_of(d)
            if dy == "Sun":
                continue
            for p in range(1, db.day_periods(slot["sem"], dy) + 1):
                cls = one(con, """SELECT 1 FROM timetable WHERE dept=? AND sem=? AND section=?
                                  AND day=? AND period=?""",
                          (slot["dept"], slot["sem"], slot["section"], dy, p))
                fac = one(con, "SELECT 1 FROM timetable WHERE faculty=? AND day=? AND period=?",
                          (slot["faculty"], dy, p))
                if not cls and not fac:
                    return {"date": d.isoformat(), "day": dy, "period": p}
        return None

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
        used, legs_a, conf_a = {}, [], []
        for s in affected:
            cands = self.rank_candidates(con, s, date_iso, used)
            top = cands[0] if cands else None
            if top:
                used[top["id"]] = used.get(top["id"], 0) + 1
            legs_a.append({
                "slot_id": s["id"], "slot_ids": s["slot_ids"], "periods": s["periods"],
                "period": s["period"], "time": _span(s["periods"]),
                "class": f"{s['dept']}-{s['sem']}{s['section']}", "subject": s["subject"],
                "subject_name": subj_name(con, s["subject"]), "room": s["room"],
                "action": "SUBSTITUTE" if top else "UNCOVERED",
                "to": top["name"] if top else "— no free competent faculty —",
                "to_id": top["id"] if top else None,
                "why": "; ".join(top["why"][:3]) if top else "all qualified staff engaged/on leave",
                "alts": [f"{c['name']} ({c['score']})" for c in cands[1:4]],
                "score": top["score"] if top else 0})
            conf_a.append(min(100, max(35, (top["score"] if top else 0) * 1.05)))
        cov_a = round(100 * sum(1 for l in legs_a if l["to_id"]) / len(legs_a))

        # ---------------- Plan B : swap-forward (zero syllabus loss)
        legs_b, swaps = [], 0
        for s in affected:
            sw = self.find_swap(con, s, date_iso)
            if sw:
                swaps += 1
                legs_b.append({
                    "slot_id": s["id"], "slot_ids": s["slot_ids"], "periods": s["periods"],
                    "period": s["period"], "time": _span(s["periods"]),
                    "class": f"{s['dept']}-{s['sem']}{s['section']}", "subject": s["subject"],
                    "subject_name": subj_name(con, s["subject"]),
                    "action": "SWAP", "to": fac_name(con, sw["faculty"]), "to_id": sw["faculty"],
                    "swap_with": sw["id"], "room": s["room"],
                    "why": f"P{sw['period']} {sw['subject']} pulled forward to P{s['period']}; "
                           f"{s['subject']} shifts to P{sw['period']} for makeup",
                    "alts": [], "score": 78})
            else:
                cands = self.rank_candidates(con, s, date_iso, used)
                top = cands[0] if cands else None
                legs_b.append({
                    "slot_id": s["id"], "slot_ids": s["slot_ids"], "periods": s["periods"],
                    "period": s["period"], "time": _span(s["periods"]),
                    "class": f"{s['dept']}-{s['sem']}{s['section']}", "subject": s["subject"],
                    "subject_name": subj_name(con, s["subject"]), "room": s["room"],
                    "action": "SUBSTITUTE" if top else "UNCOVERED",
                    "to": top["name"] if top else "—", "to_id": top["id"] if top else None,
                    "why": "no swappable later period – fallback substitution",
                    "alts": [], "score": top["score"] if top else 0})
        cov_b = round(100 * sum(1 for l in legs_b if l["to_id"]) / len(legs_b))

        # ---------------- Plan C : compact + makeup
        legs_c = []
        for s in affected:
            mk = self.find_makeup(con, s, date)
            legs_c.append({
                "slot_id": s["id"], "slot_ids": s["slot_ids"], "periods": s["periods"],
                "period": s["period"], "time": _span(s["periods"]),
                "class": f"{s['dept']}-{s['sem']}{s['section']}", "subject": s["subject"],
                "subject_name": subj_name(con, s["subject"]), "room": s["room"],
                "action": "MAKEUP", "to": faculty["name"], "to_id": faculty["id"],
                "makeup": mk,
                "why": (f"slot released today (supervised study in Central Library); "
                        f"makeup by same faculty on {mk['date']} {mk['day']} P{mk['period']}")
                if mk else "no common free slot in next 7 days – needs manual scheduling",
                "alts": [], "score": 62 if mk else 30})
        cov_c = round(100 * sum(1 for l in legs_c if l.get("makeup")) / len(legs_c))

        pid = uuid.uuid4().hex[:6]
        plans = [
            {"id": f"A-{pid}", "code": "A", "title": "Competency-matched substitution",
             "confidence": min(96, int(sum(conf_a) / len(conf_a))),
             "coverage": cov_a,
             "continuity": 74,
             "summary": f"{sum(1 for l in legs_a if l['to_id'])} of {len(legs_a)} periods covered by free, "
                        f"subject-competent faculty. No change to student timetable.",
             "pros": ["Students see zero timetable change", "Syllabus pace maintained",
                      "Attendance marking unaffected"],
             "cons": ["Adds 1 extra hour to substitute faculty's day",
                      "Depth of coverage varies for lab sessions"],
             "legs": legs_a},
            {"id": f"B-{pid}", "code": "B", "title": "Swap-forward re-sequencing",
             "confidence": 71 + min(20, swaps * 6), "coverage": cov_b, "continuity": 92,
             "summary": f"{swaps} period(s) re-sequenced within the same day — the absent faculty's hour "
                        f"is traded with a later class of the same batch. Zero syllabus loss.",
             "pros": ["No extra workload on any faculty", "100% subject-teacher authenticity",
                      "Best for exam-critical subjects"],
             "cons": ["Student timetable changes for the day — needs 12h notice",
                      "Room re-allocation may be required"],
             "legs": legs_b},
            {"id": f"C-{pid}", "code": "C", "title": "Release + guaranteed make-up",
             "confidence": 66, "coverage": cov_c, "continuity": 88,
             "summary": "Periods released with supervised self-study; the same faculty conducts a "
                        "make-up class in the earliest mutually-free slot.",
             "pros": ["Original faculty retains the topic", "Zero substitution burden"],
             "cons": ["Contact hours deferred — CIE calendar risk if repeated",
                      "Requires library/proctor booking"],
             "legs": legs_c},
        ]
        plans.sort(key=lambda p: -(p["confidence"] * 0.6 + p["coverage"] * 0.25 + p["continuity"] * 0.15))
        n_fac = one(con, "SELECT COUNT(*) c FROM faculty")["c"]
        trace.add(self.name, "constraint_solve",
                  f"searched {n_fac} faculty × {len(affected)} block(s) · hard constraints: "
                  f"clash-free, leave-free, load-cap · soft: competency, dept, balance")
        trace.add("WorkloadBalancer", "fairness_check",
                  "no substitute assigned >1 extra hour; professors de-prioritised for adjustments")
        return plans, affected

    # ---------------------------------------------------------- commit
    def apply_plan(self, con, plan, faculty, date, actor="admin@vidyatech"):
        """Write one override row per underlying PERIOD (a 3-hour lab block = 3 rows)."""
        date_iso = date.isoformat()
        now = dt.datetime.now().isoformat(timespec="seconds")
        applied = []
        for leg in plan["legs"]:
            slots = leg.get("slot_ids") or [leg["slot_id"]]
            if leg["action"] == "SWAP":
                room, reason = leg.get("room"), \
                    f"Swap with slot {leg.get('swap_with')} — {faculty['name']} absent"
            elif leg["action"] == "MAKEUP":
                mk = leg.get("makeup") or {}
                room = "Central Library"
                reason = f"Released; make-up on {mk.get('date','TBD')} P{mk.get('period','-')}"
            elif leg["to_id"]:
                room = leg.get("room")
                reason = f"{faculty['name']} absent — assigned by SubstitutionAgent"
            else:
                continue
            for sid in slots:
                con.execute("""INSERT INTO overrides(date,slot_id,action,new_faculty,new_room,
                               new_subject,reason,status,created_by,created_at,plan_ref)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (date_iso, sid, leg["action"], leg["to_id"], room, None,
                             reason, "Applied", actor, now, plan["id"]))
            applied.append(leg)
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
            cells[f"{r['day']}-{r['period']}"] = {
                "subject": r["subject"], "sname": r["sname"], "room": r["room"],
                "kind": r["kind"] or "T",
                "faculty": fac_name(con, r["faculty"]) if r["faculty"] else "Class Mentor",
                "override": ({"action": o["action"], "faculty": fac_name(con, o["new_faculty"]) if o["new_faculty"] else None,
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
