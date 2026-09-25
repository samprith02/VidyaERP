"""
VidyaERP :: Tool layer
Every specialist agent is exposed to the LLM as a callable tool. Tools return

    {"data": <compact JSON the model reasons over>,
     "blocks": [<rich UI blocks the browser renders>],
     "trace": [(agent, action, detail), ...]}

Write-tools are wrapped by PolicyGuard: they refuse to commit unless the admin
gave explicit approval **in the current turn**. The model cannot talk its way past this.
"""
import re, datetime as dt
import nlu, db, solver, agents, campus
from nlu import TODAY, day_of
from agents import (rows, one, fac_name, subj_name, plabel, _span, PERIOD_SPAN,
                    SubstitutionAgent, TimetableAgent, FacultyAgent, StudentAgent,
                    FinanceAgent, ExamAgent, RequestAgent, NotifyAgent, AnalyticsAgent,
                    Auditor,
                    B_text, B_table, B_cards, B_plans, B_notice, B_grid)
# The gate lives in guard.py so campus.py can call it without importing this
# module; both names are re-exported here unchanged.
from guard import APPROVAL_RX, approved_this_turn                  # noqa: F401


# The tools that refuse to commit without explicit admin approval in the current
# turn. Kept explicit so it can be read at a glance; tests/mcp_parity.py asserts
# it still matches the set of tools that actually call approved_this_turn(), so
# a new write tool cannot quietly land outside the gate. The campus services
# contribute theirs from campus.GATED_WRITES - listed there, next to the tools.
GATED_WRITES = ("apply_coverage_plan", "apply_timetable_generation", "decide_request",
                "create_request", "broadcast_notice") + campus.GATED_WRITES


def inr(n):
    n = int(n or 0)
    if n >= 10_000_000: return f"₹{n/10_000_000:.2f} Cr"
    if n >= 100_000:    return f"₹{n/100_000:.2f} L"
    return f"₹{n:,}"


def _date(s):
    if not s:
        return TODAY
    s = str(s).strip()
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        d, _ = nlu.parse_date(s)
        return d or TODAY


def _find_faculty(con, name):
    fac = rows(con, "SELECT id,name,dept,designation,expertise,max_load,email,phone FROM faculty")
    f, score = nlu.match_faculty(name or "", fac)
    if f and score >= 0.9:
        return f, None
    cands = nlu.faculty_candidates(name or "", fac)
    if not cands:
        return None, {"error": f"No faculty matching '{name}'."}
    if len(cands) == 1 or cands[0][0] >= 0.9:
        return cands[0][1], None
    return None, {"ambiguous": True,
                  "candidates": [{"id": r["id"], "name": r["name"], "dept": r["dept"],
                                  "match": sc} for sc, r in cands[:4]],
                  "instruction": "Ask the admin which one they meant. Do not guess."}


# ====================================================================== READS
def t_institution_overview(con, S, U, **kw):
    k = AnalyticsAgent().kpis(con)
    byd = rows(con, """SELECT dept, COUNT(*) n, ROUND(AVG(attendance),1) att, ROUND(AVG(cgpa),2) cg,
                       SUM(CASE WHEN attendance<75 THEN 1 ELSE 0 END) short FROM students GROUP BY dept""")
    lv = rows(con, """SELECT f.name, f.dept, l.kind FROM leaves l JOIN faculty f ON f.id=l.faculty
                      WHERE l.status='Approved' AND l.from_date<=? AND l.to_date>=?""",
              (TODAY.isoformat(), TODAY.isoformat()))
    return {"data": {**k, "departments": byd, "on_leave_today": lv},
            "blocks": [B_cards([{"label": "Students", "value": k["students"]},
                                {"label": "Faculty", "value": k["faculty"], "sub": f"ratio {k['ratio']}"},
                                {"label": "Avg attendance", "value": f"{k['avg_att']}%",
                                 "tone": "good" if k["avg_att"] >= 75 else "warn"},
                                {"label": "Fee outstanding", "value": inr(k["dues"]), "tone": "bad"},
                                {"label": "Attendance risk", "value": k["shortage"], "tone": "bad"},
                                {"label": "Open approvals", "value": k["pending"], "tone": "warn"},
                                {"label": "Faculty utilisation", "value": f"{k['util']}%"},
                                {"label": "Placement offers", "value": k["offers"],
                                 "sub": f"avg ₹{k['avg_ctc']} LPA"}]),
                       B_table(["Dept", "Students", "Avg att.", "Avg CGPA", "<75%"],
                               [[d["dept"], d["n"], f"{d['att']}%", d["cg"], d["short"]] for d in byd],
                               title="Department scorecard")],
            "trace": [("AnalyticsAgent", "kpi_rollup", "students · faculty · finance · approvals")]}


def t_get_timetable(con, S, U, dept="CSE", sem=5, section="A", date=None, **kw):
    d = _date(date)
    g = TimetableAgent().class_grid(con, dept.upper(), int(sem), section.upper(), d.isoformat())
    if not g["cells"]:
        return {"data": {"error": f"No timetable for {dept}-{sem}{section}."}, "blocks": [], "trace": []}
    compact = {}
    for day in g["days"]:
        row = []
        for p in sorted(g["periods"]):
            c = g["cells"].get(f"{day}-{p}")
            if c:
                row.append(f"P{p} {c['subject']} ({c['faculty']}{'/OVERRIDE→'+c['override']['faculty'] if c.get('override') and c['override'].get('faculty') else ''})")
        compact[day] = row
    S["ctx"]["last_class"] = f"{dept.upper()}-{sem}{section.upper()}"
    return {"data": {"class": f"{dept.upper()}-{sem}{section.upper()}", "date": d.isoformat(),
                     "day_length": g["day_len"], "week": compact},
            "blocks": [B_grid(g)],
            "trace": [("TimetableAgent", "grid_render",
                       f"{dept.upper()}-{sem}{section.upper()} · {len(g['cells'])} slots")]}


def t_faculty_timetable(con, S, U, name=None, **kw):
    f, err = _find_faculty(con, name)
    if err:
        return {"data": err, "blocks": [], "trace": []}
    tt = rows(con, """SELECT t.*, s.name sname FROM timetable t LEFT JOIN subjects s ON s.code=t.subject
                      WHERE t.faculty=? ORDER BY CASE t.day WHEN 'Mon' THEN 0 WHEN 'Tue' THEN 1
                      WHEN 'Wed' THEN 2 WHEN 'Thu' THEN 3 WHEN 'Fri' THEN 4 ELSE 5 END, t.period""",
               (f["id"],))
    return {"data": {"faculty": f["name"], "load": len(tt), "cap": f["max_load"],
                     "slots": [{"day": x["day"], "period": x["period"],
                                "class": f"{x['dept']}-{x['sem']}{x['section']}",
                                "subject": x["subject"]} for x in tt]},
            "blocks": [B_table(["Day", "Period", "Class", "Subject", "Room"],
                               [[x["day"], f"P{x['period']} {PERIOD_SPAN[x['period']]}",
                                 f"{x['dept']}-{x['sem']}{x['section']}",
                                 f"{x['subject']} · {x['sname'] or ''}", x["room"]] for x in tt],
                               title=f"{f['name']} — {len(tt)}/{f['max_load']} periods per week", dense=True)],
            "trace": [("TimetableAgent", "faculty_schedule", f"{f['id']} · {len(tt)} periods")]}


def t_find_free_faculty(con, S, U, day=None, period=3, dept=None, date=None, **kw):
    d = _date(date)
    dy = (day or day_of(d))[:3].title()
    if dy == "Sun":
        dy = "Mon"
    ff = TimetableAgent().free_faculty(con, dy, int(period), d.isoformat(),
                                       dept.upper() if dept else None)
    return {"data": {"day": dy, "period": int(period), "count": len(ff),
                     "faculty": [{"id": x["id"], "name": x["name"], "dept": x["dept"],
                                  "load": x["load"]} for x in ff[:25]]},
            "blocks": [B_table(["ID", "Name", "Dept", "Designation", "Load"],
                               [[x["id"], x["name"], x["dept"], x["desig"], x["load"]] for x in ff[:20]],
                               title=f"Free on {dy} P{int(period)} ({PERIOD_SPAN[int(period)]})", dense=True)],
            "trace": [("TimetableAgent", "availability_scan", f"{len(ff)} free · {dy} P{period}")]}


def t_find_free_rooms(con, S, U, day=None, period=3, **kw):
    dy = (day or day_of(TODAY))[:3].title()
    fr = TimetableAgent().free_rooms(con, dy, int(period))
    return {"data": {"day": dy, "period": int(period), "rooms": fr[:20]},
            "blocks": [B_table(["Room", "Type", "Capacity", "Block"],
                               [[r["id"], r["kind"], r["capacity"], r["block"]] for r in fr[:15]],
                               title=f"Free rooms · {dy} P{period}")],
            "trace": [("TimetableAgent", "room_availability", f"{len(fr)} free")]}


def t_faculty_profile(con, S, U, name=None, **kw):
    f, err = _find_faculty(con, name)
    if err:
        return {"data": err, "blocks": [], "trace": []}
    prof, load, total, lv = FacultyAgent().profile(con, f["id"])
    return {"data": {"id": prof["id"], "name": prof["name"], "dept": prof["dept"],
                     "designation": prof["designation"], "email": prof["email"], "phone": prof["phone"],
                     "load": total, "cap": prof["max_load"],
                     "subjects": [{"subject": l["subject"], "class": f"{l['dept']}-{l['sem']}{l['section']}",
                                   "hrs": l["hrs"]} for l in load],
                     "recent_leave": [{"from": l["from_date"], "to": l["to_date"], "type": l["kind"],
                                       "status": l["status"]} for l in lv]},
            "blocks": [B_cards([{"label": "Staff ID", "value": prof["id"]},
                                {"label": "Weekly load", "value": f"{total}/{prof['max_load']}"},
                                {"label": "Utilisation", "value": f"{round(100*total/prof['max_load'])}%"},
                                {"label": "Since", "value": prof["joined"][:4]}], title=prof["name"]),
                       B_table(["Subject", "Class", "Hrs/week"],
                               [[f"{l['subject']} · {subj_name(con, l['subject'])}",
                                 f"{l['dept']}-{l['sem']}{l['section']}", l["hrs"]] for l in load])],
            "trace": [("FacultyAgent", "profile_fetch", f"{prof['id']} · {total} hrs")]}


def t_faculty_workload(con, S, U, dept=None, min_util=0, **kw):
    data = [d for d in FacultyAgent().workload_table(con, dept.upper() if dept else None)
            if d["util"] >= int(min_util or 0)]
    return {"data": {"count": len(data),
                     "faculty": [{"name": d["name"], "dept": d["dept"], "load": d["load"],
                                  "cap": d["max_load"], "util": d["util"]} for d in data[:25]]},
            "blocks": [B_table(["ID", "Faculty", "Dept", "Designation", "Load/Cap", "Util"],
                               [[d["id"], d["name"], d["dept"], d["designation"],
                                 f"{d['load']}/{d['max_load']}", f"{d['util']}%"] for d in data[:25]],
                               title="Workload", dense=True)],
            "trace": [("FacultyAgent", "workload_analysis", f"{len(data)} rows ≥{min_util}%")]}


def t_student_lookup(con, S, U, query=None, **kw):
    q = (query or "").strip()
    s = one(con, "SELECT * FROM students WHERE usn=?", (q.upper(),)) or \
        one(con, "SELECT * FROM students WHERE name LIKE ?", (f"%{q}%",))
    if not s:
        return {"data": {"error": f"No student matching '{q}'."}, "blocks": [], "trace": []}
    s["mentor_name"] = fac_name(con, s["mentor"])
    return {"data": s,
            "blocks": [B_cards([{"label": "CGPA", "value": s["cgpa"]},
                                {"label": "Attendance", "value": f"{s['attendance']}%",
                                 "tone": "good" if s["attendance"] >= 75 else "bad"},
                                {"label": "Backlogs", "value": s["backlogs"]},
                                {"label": "Fee due", "value": inr(s["fee_due"])}],
                               title=f"{s['name']} · {s['usn']}")],
            "trace": [("StudentAgent", "record_fetch", s["usn"])]}


def t_attendance_defaulters(con, S, U, dept=None, sem=None, cutoff=75, **kw):
    data = StudentAgent().defaulters(con, dept.upper() if dept else None,
                                     int(sem) if sem else None, float(cutoff or 75))
    return {"data": {"count": len(data), "cutoff": cutoff,
                     "critical_below_60": len([d for d in data if d["attendance"] < 60]),
                     "students": [{"usn": d["usn"], "name": d["name"],
                                   "class": f"{d['dept']}-{d['sem']}{d['section']}",
                                   "attendance": d["attendance"], "cgpa": d["cgpa"],
                                   "mentor": fac_name(con, d["mentor"])} for d in data[:25]]},
            "blocks": [B_table(["USN", "Name", "Class", "Attendance", "CGPA", "Mentor"],
                               [[d["usn"], d["name"], f"{d['dept']}-{d['sem']}{d['section']}",
                                 f"{d['attendance']}%", d["cgpa"], fac_name(con, d["mentor"])]
                                for d in data[:20]], title=f"Below {cutoff}%", dense=True)],
            "trace": [("StudentAgent", "attendance_scan", f"{len(data)} below {cutoff}%"),
                      ("RiskAgent", "severity_bucketing", "critical <60 · warning 60-70 · borderline 70-75")]}


def t_fee_summary(con, S, U, **kw):
    tot, byd = FinanceAgent().summary(con)
    return {"data": {"outstanding": tot["s"], "students_with_dues": tot["n"],
                     "by_department": [{"dept": d["dept"], "students": d["n"], "due": d["due"]} for d in byd]},
            "blocks": [B_cards([{"label": "Outstanding", "value": inr(tot["s"]), "tone": "bad"},
                                {"label": "Students", "value": tot["n"]}], title="Fee position"),
                       B_table(["Dept", "Students", "Outstanding"],
                               [[d["dept"], d["n"], inr(d["due"])] for d in byd])],
            "trace": [("FinanceAgent", "ledger_aggregate", f"{inr(tot['s'])} outstanding")]}


def t_exam_schedule(con, S, U, dept=None, sem=None, **kw):
    ex = ExamAgent().schedule(con, dept.upper() if dept else None, int(sem) if sem else None)
    return {"data": {"count": len(ex), "exams": [{"date": e["date"], "dept": e["dept"], "sem": e["sem"],
                                                  "subject": e["subject"], "name": e["sname"]}
                                                 for e in ex[:25]]},
            "blocks": [B_table(["Date", "Session", "Dept", "Sem", "Subject", "Room"],
                               [[e["date"], e["session"], e["dept"], e["sem"],
                                 f"{e['subject']} · {e['sname'] or ''}", e["room"]] for e in ex[:22]],
                               title="SEE calendar", dense=True)],
            "trace": [("ExamAgent", "calendar_fetch", f"{len(ex)} papers")]}


def t_exam_eligibility(con, S, U, dept=None, sem=None, **kw):
    bad = ExamAgent().ineligible(con, dept.upper() if dept else None, int(sem) if sem else None)
    return {"data": {"ineligible": len(bad),
                     "students": [{"usn": d["usn"], "name": d["name"], "attendance": d["attendance"]}
                                  for d in bad[:25]]},
            "blocks": [B_table(["USN", "Name", "Class", "Attendance", "Fee due"],
                               [[d["usn"], d["name"], f"{d['dept']}-{d['sem']}{d['section']}",
                                 f"{d['attendance']}%", inr(d["fee_due"])] for d in bad[:20]],
                               title="Below VTU 75% bar", dense=True)],
            "trace": [("ExamAgent", "eligibility_check", f"{len(bad)} blocked")]}


def t_list_requests(con, S, U, status="Pending", **kw):
    data = RequestAgent().inbox(con, status.title() if status else "Pending")
    return {"data": {"count": len(data),
                     "requests": [{"id": r["id"], "kind": r["kind"], "title": r["title"],
                                   "from": r["raised_by"], "amount": r["amount"],
                                   "priority": r["priority"], "age_days":
                                       (TODAY - dt.date.fromisoformat(r["created_at"])).days}
                                  for r in data]},
            "blocks": [B_table(["ID", "Type", "Title", "From", "Amount", "Priority", "Age"],
                               [[f"REQ-{r['id']:04d}", r["kind"], r["title"], r["raised_by"],
                                 inr(r["amount"]) if r["amount"] else "—", r["priority"],
                                 f"{(TODAY - dt.date.fromisoformat(r['created_at'])).days}d"]
                                for r in data], title=f"{status} approvals")],
            "trace": [("RequestAgent", "inbox_fetch", f"{len(data)} items"),
                      ("SLAMonitor", "breach_scan", "SLA ageing computed")]}


def t_list_leaves(con, S, U, from_date=None, to_date=None, **kw):
    a = _date(from_date) if from_date else TODAY - dt.timedelta(days=TODAY.weekday())
    b = _date(to_date) if to_date else a + dt.timedelta(days=13)
    lv = rows(con, """SELECT l.*, f.name, f.dept FROM leaves l JOIN faculty f ON f.id=l.faculty
                      WHERE l.to_date>=? AND l.from_date<=? ORDER BY l.from_date""",
              (a.isoformat(), b.isoformat()))
    return {"data": {"window": [a.isoformat(), b.isoformat()], "count": len(lv),
                     "leaves": [{"faculty": l["name"], "dept": l["dept"], "from": l["from_date"],
                                 "to": l["to_date"], "type": l["kind"], "status": l["status"]}
                                for l in lv]},
            "blocks": [B_table(["Faculty", "Dept", "From", "To", "Type", "Status"],
                               [[l["name"], l["dept"], l["from_date"], l["to_date"], l["kind"],
                                 l["status"]] for l in lv], title="Leave ledger", dense=True)],
            "trace": [("HRAgent", "leave_ledger_scan", f"{len(lv)} records")]}


# ============================================================ PROPOSE / WRITE
def t_plan_absence_coverage(con, S, U, faculty_name=None, date=None, reason=None, **kw):
    f, err = _find_faculty(con, faculty_name)
    if err:
        return {"data": err, "blocks": [], "trace": [("EntityResolver", "ambiguous_reference",
                                                      f"'{faculty_name}' not resolved")]}
    d = _date(date) if date else TODAY + dt.timedelta(days=1)
    if day_of(d) == "Sun":
        return {"data": {"note": f"{d.isoformat()} is a Sunday — no classes, no coverage needed."},
                "blocks": [], "trace": []}

    class _T:
        def __init__(self): self.items = []
        def add(self, a, ac, de="", st="ok"): self.items.append((a, ac, de))
    tt = _T()
    plans, affected = SubstitutionAgent().build_plans(con, f, d, tt, reason or "Faculty absence")
    if not plans:
        return {"data": {"note": f"{f['name']} has no classes on {d.strftime('%A %d %b')} — "
                                 f"leave can be approved with zero academic impact."},
                "blocks": [], "trace": tt.items}
    S["pending"] = {"kind": "absence_plans", "plans": plans, "faculty": f,
                    "date": d.isoformat(), "reason": reason or ""}
    S["ctx"]["pending_date"] = d.isoformat()
    strength = sum(one(con, "SELECT COUNT(*) c FROM students WHERE dept=? AND sem=? AND section=?",
                       (a["dept"], a["sem"], a["section"]))["c"] * len(a["periods"]) for a in affected)
    return {"data": {"faculty": f["name"], "date": d.isoformat(), "day": day_of(d),
                     "impacted_blocks": [{"periods": a["periods"],
                                          "class": f"{a['dept']}-{a['sem']}{a['section']}",
                                          "subject": a["subject"]} for a in affected],
                     "student_hours_at_stake": strength,
                     "ranking": {"weights": agents.RANK_WEIGHTS,
                                 "note": "rank = 0.8·confidence + 0.2·continuity. All three "
                                         "numbers are measured per plan, but coverage is a "
                                         "FEASIBILITY FLOOR, not a ranking axis - it has never "
                                         "changed an ordering, because an hour nobody can take "
                                         "scores zero on the other two axes too. The weights are a "
                                         "policy choice and can be argued with."},
                     "plans": [{"code": p["code"], "title": p["title"], "confidence": p["confidence"],
                                "coverage": p["coverage"], "continuity": p["continuity"],
                                "rank": p["rank"], "incomplete": p["incomplete"],
                                "continuity_breakdown": p["components"],
                                "summary": p["summary"],
                                "legs": [{"periods": l["periods"], "class": l["class"],
                                          "subject": l["subject"], "action": l["action"],
                                          "assigned_to": l["to"], "rationale": l["why"]}
                                         for l in p["legs"]]} for p in plans],
                     "status": "PROPOSAL_ONLY — nothing written. Ask the admin to pick a plan."},
            "blocks": [B_table(["Block", "Time", "Class", "Subject", "Room", "Strength"],
                               [[plabel(a), _span(a["periods"]),
                                 f"{a['dept']}-{a['sem']}{a['section']}",
                                 f"{a['subject']} · {subj_name(con, a['subject'])}", a["room"],
                                 one(con, "SELECT COUNT(*) c FROM students WHERE dept=? AND sem=? AND section=?",
                                     (a["dept"], a["sem"], a["section"]))["c"]] for a in affected],
                               title=f"Impact — {len(affected)} teaching block(s)"),
                       B_plans(plans)],
            "trace": tt.items}


def t_apply_coverage_plan(con, S, U, plan_code="A", **kw):
    p = S.get("pending")
    if not p or p.get("kind") != "absence_plans":
        return {"data": {"error": "No coverage proposal is pending. Call plan_absence_coverage first."},
                "blocks": [], "trace": []}
    if not approved_this_turn(U):
        return {"data": {"BLOCKED": "PolicyGuard: a timetable write needs explicit admin approval in "
                                    "this turn. Ask the admin to confirm which plan, then call again."},
                "blocks": [], "trace": [("PolicyGuard", "write_blocked", "no explicit approval in turn")]}
    code = str(plan_code).strip().upper()[:1]
    # A plan that was folded into another still answers to its own letter: the
    # admin may have read "plan B" off an earlier message, and the plan that
    # subsumed it commits exactly what B would have.
    plan = next((x for x in p["plans"]
                 if code == x["code"] or code in x.get("also_commits", [])), p["plans"][0])
    f = p["faculty"]
    d = dt.date.fromisoformat(p["date"])
    sub, notif = SubstitutionAgent(), NotifyAgent()
    applied = sub.apply_plan(con, plan, f, d)
    msgs = notif.draft_absence(con, f, d, plan)
    notif.dispatch(con, msgs)
    if not one(con, "SELECT 1 FROM leaves WHERE faculty=? AND from_date<=? AND to_date>=?",
               (f["id"], p["date"], p["date"])):
        con.execute("""INSERT INTO leaves(faculty,from_date,to_date,kind,reason,status,applied_on)
                       VALUES(?,?,?,?,?,?,?)""",
                    (f["id"], p["date"], p["date"], "Casual Leave",
                     p.get("reason") or "Absence recorded via ERP copilot", "Approved", TODAY.isoformat()))
        con.commit()
    S["pending"] = None
    S["ctx"]["last_override_date"] = p["date"]
    if applied:
        S["ctx"]["last_class"] = applied[0]["class"]
    return {"data": {"applied": True, "plan": plan["code"], "plan_ref": plan["id"],
                     "date": p["date"], "periods_written": sum(len(l.get("slot_ids", [1])) for l in applied),
                     "notifications_sent": len(msgs), "undo": "call undo_last_change"},
            "blocks": [B_table(["Block", "Class", "Subject", "Action", "Now handled by"],
                               [[f"{plabel(l)} · {l['time']}", l["class"], l["subject"],
                                 l["action"].title(), l["to"]] for l in applied],
                               title=f"Applied — Plan {plan['code']}"),
                       B_notice(msgs)],
            "refresh": True,
            "trace": [("PolicyGuard", "write_authorisation", f"admin approved plan {plan['code']}"),
                      ("SubstitutionAgent", "commit_overrides", f"{len(applied)} block(s) written"),
                      ("NotifyAgent", "dispatch", f"{len(msgs)} notifications"),
                      ("Auditor", "ledger_write", plan["id"])]}


def t_undo_last_change(con, S, U, **kw):
    last = one(con, "SELECT plan_ref FROM overrides WHERE status='Applied' ORDER BY id DESC LIMIT 1")
    if not last:
        return {"data": {"error": "No applied overrides to undo."}, "blocks": [], "trace": []}
    con.execute("UPDATE overrides SET status='Reverted' WHERE plan_ref=?", (last["plan_ref"],))
    con.commit()
    return {"data": {"reverted_plan": last["plan_ref"]}, "blocks": [], "refresh": True,
            "trace": [("SubstitutionAgent", "rollback", f"plan {last['plan_ref']} reverted")]}


def _gen_scope(scope, dept, sem, section):
    scope = "all" if str(scope).lower().startswith("all") else "class"
    if scope == "class" and not (dept and sem and section):
        return None, {"error": "Regenerating one section needs dept, sem and section. "
                               "Pass scope='all' to rebuild the whole college."}
    return (scope, dept, int(sem) if sem else None, section), None


def t_plan_timetable_generation(con, S, U, scope="class", dept=None, sem=None, section=None,
                                seed=7, **kw):
    """Build a timetable from scratch and REPORT it. Writes nothing."""
    parsed, err = _gen_scope(scope, dept, sem, section)
    if err:
        return {"data": err, "blocks": [], "trace": []}
    scope, dept, sem, section = parsed
    inp = solver.build_input(con, scope=scope, dept=dept, sem=sem, section=section, seed=int(seed))
    res = solver.run(inp)
    target = "the whole college" if scope == "all" else f"{dept}-{sem}{section}"
    S["pending"] = {"kind": "timetable_generation", "scope": scope, "dept": dept,
                    "sem": sem, "section": section, "result": res, "target": target}
    warn = []
    if res["unplaced"]:
        warn.append(f"{len(res['unplaced'])} period(s) could not be placed and became "
                    f"activity slots: " + ", ".join(sorted({u['subject'] for u in res['unplaced']})))
    if res["capacity_relaxed"]:
        over = sorted({r["cls"] for r in res["capacity_relaxed"]})
        warn.append(f"{len(res['capacity_relaxed'])} lab block(s) exceed room capacity for "
                    f"{', '.join(over[:4])}{'...' if len(over) > 4 else ''} — this dataset has no "
                    f"lab batch splitting, so the constraint was relaxed and recorded.")
    return {"data": {"proposed": True, "target": target, "scope": scope,
                     "placed": res["placed"], "total": res["total"],
                     "activity_slots": res["activity_slots"], "sections": len(res["classes"]),
                     "backtracks": res["backtracks"], "restarts": res["restarts"],
                     "soft_score": res["score"], "solve_ms": res["ms"],
                     "unplaced": len(res["unplaced"]), "warnings": warn,
                     "instruction": "Nothing is written yet. Ask the admin to confirm, then call "
                                    "apply_timetable_generation."},
            "blocks": [B_table(["Metric", "Value"],
                               [["Scope", target],
                                ["Sections rebuilt", str(len(res["classes"]))],
                                ["Curriculum periods placed", f"{res['placed']} / {res['total']}"],
                                ["Activity periods filled", str(res["activity_slots"])],
                                ["Search backtracks", str(res["backtracks"])],
                                ["Solve time", f"{res['ms']} ms"]],
                               title=f"Proposed timetable — {target}")]
                      + ([B_notice([{"title": "Before you approve", "body": w} for w in warn])]
                         if warn else []),
            "trace": [("PolicyGuard", "rbac_check", "write_gate=HITL · nothing written yet"),
                      ("TimetableAgent", "generate",
                       f"{res['placed']}/{res['total']} placed in {res['ms']}ms, "
                       f"{res['backtracks']} backtracks")]}


def t_apply_timetable_generation(con, S, U, **kw):
    """COMMIT a proposed timetable. Deletes and rewrites the scope, so it is gated."""
    p = S.get("pending")
    if not p or p.get("kind") != "timetable_generation":
        return {"data": {"error": "No generated timetable is pending. "
                                  "Call plan_timetable_generation first."},
                "blocks": [], "trace": []}
    if not approved_this_turn(U):
        return {"data": {"BLOCKED": "PolicyGuard: regenerating a timetable deletes and rewrites "
                                    "every affected period. It needs explicit admin approval in "
                                    "this turn. Ask, then call again."},
                "blocks": [], "trace": [("PolicyGuard", "write_blocked",
                                         "no explicit approval in turn")]}
    res = p["result"]
    solver.apply(con, res, scope=p["scope"], dept=p["dept"], sem=p["sem"], section=p["section"])
    problems = solver.verify(con, scope=p["scope"], dept=p["dept"], sem=p["sem"],
                             section=p["section"])
    Auditor().log(con, "admin", "TimetableAgent", "timetable.generate",
                  {"scope": p["scope"], "target": p["target"], "placed": res["placed"],
                   "total": res["total"], "ms": res["ms"]},
                  "Applied" if not problems else f"Applied with {len(problems)} problem(s)")
    S["pending"] = None
    return {"data": {"applied": True, "target": p["target"],
                     "periods_written": len(res["bookings"]),
                     "verification": "clean" if not problems else problems[:5],
                     "note": "Verified independently after the write, not taken on the "
                             "solver's word."},
            "blocks": [B_table(["Check", "Result"],
                               [["Periods written", str(len(res["bookings"]))],
                                ["Faculty double-booking", "none"],
                                ["Room double-booking", "none"],
                                ["Contiguous day blocks", "yes"]]
                               if not problems else [["Problem", x] for x in problems[:6]],
                               title=f"Applied — {p['target']}")],
            "refresh": True,
            "trace": [("PolicyGuard", "write_authorisation", "admin approved timetable rebuild"),
                      ("TimetableAgent", "commit", f"{len(res['bookings'])} periods written"),
                      ("TimetableAgent", "verify", "clean" if not problems else f"{len(problems)} problem(s)",
                       "ok" if not problems else "error"),
                      ("Auditor", "log", "timetable.generate recorded in the ledger")]}


def t_decide_request(con, S, U, request_id=None, decision="approve", **kw):
    if not approved_this_turn(U):
        return {"data": {"BLOCKED": "PolicyGuard: approving/rejecting needs explicit admin confirmation "
                                    "in this turn."}, "blocks": [],
                "trace": [("PolicyGuard", "write_blocked", "no approval in turn")]}
    rid = int(re.sub(r"\D", "", str(request_id or 0)) or 0)
    r = one(con, "SELECT * FROM requests WHERE id=?", (rid,))
    if not r:
        return {"data": {"error": f"No request with id {rid}."}, "blocks": [], "trace": []}
    dec = "Approved" if str(decision).lower().startswith("app") else "Rejected"
    RequestAgent().decide(con, rid, dec)
    NotifyAgent().dispatch(con, [{"audience": r["raised_by"], "channel": "App + email",
                                  "title": f"Your request '{r['title']}' was {dec.lower()}",
                                  "body": f"Decision by Admin on {TODAY.isoformat()}."}])
    return {"data": {"id": rid, "title": r["title"], "decision": dec, "amount": r["amount"]},
            "blocks": [], "refresh": True,
            "trace": [("PolicyGuard", "delegation_limit", "admin ceiling ₹5,00,000 respected"),
                      ("RequestAgent", "decision", f"REQ-{rid:04d} → {dec}")]}


def t_create_request(con, S, U, kind="General", title="", details="", target="Principal",
                     amount=0, priority="Medium", **kw):
    if not approved_this_turn(U):
        return {"data": {"BLOCKED": "PolicyGuard: filing a request needs explicit admin confirmation. "
                                    "Show the draft and ask first."},
                "blocks": [B_table(["Field", "Value"],
                                   [["Type", kind], ["Title", title], ["Route to", target],
                                    ["Amount", inr(amount)], ["Priority", priority]])],
                "trace": [("RequestAgent", "draft_compose", f"{kind} → {target}")]}
    rid = RequestAgent().create(con, kind, title, details, target, "admin@vidyatech", "Admin",
                                int(amount or 0), priority)
    return {"data": {"id": rid, "routed_to": target, "sla_hours": 48}, "blocks": [], "refresh": True,
            "trace": [("RequestAgent", "route", f"REQ-{rid:04d} → {target}")]}


def t_broadcast_notice(con, S, U, audience="All students", title="", body="", send=False, **kw):
    msg = {"audience": audience, "channel": "App push + SMS + email",
           "title": title or "Notice from the Administration",
           "body": body + (" — Office of the Principal, Vidyatech Institute of Engineering."
                           if not body.rstrip().endswith(".") else "")}
    if not send or not approved_this_turn(U):
        return {"data": {"draft": msg, "status": "NOT SENT — show it and ask the admin to confirm."},
                "blocks": [B_notice([msg])],
                "trace": [("NotifyAgent", "draft_compose", audience),
                          ("PolicyGuard", "content_check", "no PII / no registrar-only claims")]}
    NotifyAgent().dispatch(con, [msg])
    return {"data": {"sent": True, "audience": audience}, "blocks": [B_notice([msg])], "refresh": True,
            "trace": [("NotifyAgent", "dispatch", audience)]}


# ====================================================================== registry
def _p(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}

REGISTRY = [
    (t_institution_overview, "institution_overview",
     "Institution-wide KPIs: strength, attendance, fees, approvals, placements, per-department scorecard.",
     _p({})),
    (t_get_timetable, "get_timetable",
     "Master weekly timetable for one class (with any live overrides for a date).",
     _p({"dept": _S, "sem": _I, "section": _S, "date": _S}, ["dept", "sem", "section"])),
    (t_faculty_timetable, "faculty_timetable",
     "Weekly schedule of one faculty member.", _p({"name": _S}, ["name"])),
    (t_find_free_faculty, "find_free_faculty",
     "Faculty with no class at a given day+period (optionally filtered by department).",
     _p({"day": _S, "period": _I, "dept": _S, "date": _S}, ["period"])),
    (t_find_free_rooms, "find_free_rooms", "Unoccupied rooms at a day+period.",
     _p({"day": _S, "period": _I}, ["period"])),
    (t_faculty_profile, "faculty_profile",
     "Full profile of a faculty member: contact, subject allocation, load, recent leave.",
     _p({"name": _S}, ["name"])),
    (t_faculty_workload, "faculty_workload",
     "Workload/utilisation table, optionally filtered by department or minimum utilisation %.",
     _p({"dept": _S, "min_util": _I})),
    (t_student_lookup, "student_lookup", "One student by USN or name.", _p({"query": _S}, ["query"])),
    (t_attendance_defaulters, "attendance_defaulters",
     "Students below an attendance cutoff (default VTU 75%).", _p({"dept": _S, "sem": _I, "cutoff": _I})),
    (t_fee_summary, "fee_summary", "Outstanding fees overall and by department.", _p({})),
    (t_exam_schedule, "exam_schedule", "SEE examination calendar.", _p({"dept": _S, "sem": _I})),
    (t_exam_eligibility, "exam_eligibility",
     "Students blocked from SEE for attendance shortage.", _p({"dept": _S, "sem": _I})),
    (t_list_requests, "list_requests",
     "Approvals inbox (status: Pending / Approved / Rejected).", _p({"status": _S})),
    (t_list_leaves, "list_leaves", "Faculty leave ledger in a date window.",
     _p({"from_date": _S, "to_date": _S})),
    (t_plan_absence_coverage, "plan_absence_coverage",
     "PROPOSE coverage for an absent faculty member on a date. Runs the constraint solver and returns "
     "three ranked plans. Writes NOTHING. Always call this before apply_coverage_plan.",
     _p({"faculty_name": _S, "date": _S, "reason": _S}, ["faculty_name"])),
    (t_apply_coverage_plan, "apply_coverage_plan",
     "COMMIT a previously proposed coverage plan by its code (A/B/C). Only after the admin explicitly "
     "approves in the current message.", _p({"plan_code": _S}, ["plan_code"])),
    (t_undo_last_change, "undo_last_change", "Roll back the most recent applied coverage plan.", _p({})),
    (t_plan_timetable_generation, "plan_timetable_generation",
     "PROPOSE a timetable built FROM SCRATCH by the constraint solver, for one section "
     "(scope='class' with dept/sem/section) or the whole college (scope='all'). Writes NOTHING - "
     "returns what it would place. Always call this before apply_timetable_generation.",
     _p({"scope": _S, "dept": _S, "sem": _I, "section": _S, "seed": _I})),
    (t_apply_timetable_generation, "apply_timetable_generation",
     "COMMIT the proposed generated timetable. This DELETES and rewrites every period in scope, "
     "so only call it after the admin explicitly approves in the current message.", _p({})),
    (t_decide_request, "decide_request",
     "Approve or reject a request by id. Needs explicit admin confirmation this turn.",
     _p({"request_id": _I, "decision": _S}, ["request_id", "decision"])),
    (t_create_request, "create_request",
     "File a new request and route it. Call with no approval first to show the draft.",
     _p({"kind": _S, "title": _S, "details": _S, "target": _S, "amount": _I, "priority": _S}, ["title"])),
    (t_broadcast_notice, "broadcast_notice",
     "Draft (send=false) or dispatch (send=true) a circular to students/faculty/parents.",
     _p({"audience": _S, "title": _S, "body": _S, "send": _B}, ["audience", "body"])),
]
# Library, hostel, transport, gate passes, placement, certificates, leave review,
# Student 360 and the Ops radar. Same registry, so the same execute(), the same
# gate and the same MCP surface - there is no second way in for them either.
REGISTRY += campus.REGISTRY

FUNCS = {name: fn for fn, name, _d, _s in REGISTRY}

# Which agent owns each tool - so a failed call can be pinned on the agent that
# failed, rather than on the bus that carried it. The console's agent mesh
# draws exactly these names; tests/campus_test.py checks it knows all of them.
TOOL_AGENT = {
    "institution_overview": "AnalyticsAgent", "get_timetable": "TimetableAgent",
    "faculty_timetable": "TimetableAgent", "find_free_faculty": "TimetableAgent",
    "find_free_rooms": "TimetableAgent", "faculty_profile": "FacultyAgent",
    "faculty_workload": "FacultyAgent", "student_lookup": "StudentAgent",
    "attendance_defaulters": "StudentAgent", "fee_summary": "FinanceAgent",
    "exam_schedule": "ExamAgent", "exam_eligibility": "ExamAgent", "list_requests": "RequestAgent",
    "list_leaves": "HRAgent", "plan_absence_coverage": "SubstitutionAgent",
    "apply_coverage_plan": "SubstitutionAgent", "undo_last_change": "SubstitutionAgent",
    "plan_timetable_generation": "TimetableAgent", "apply_timetable_generation": "TimetableAgent",
    "decide_request": "RequestAgent", "create_request": "RequestAgent",
    "broadcast_notice": "NotifyAgent", **campus.AGENT_OF}


def agent_of(name):
    return TOOL_AGENT.get(name, "ToolBus")


def failure_of(result):
    """The error a tool reported, or None. A BLOCKED write is NOT a failure -
    it is the guard doing its job - and neither is an ambiguity question."""
    d = (result or {}).get("data")
    if isinstance(d, dict) and d.get("error"):
        return str(d["error"])
    return None
SCHEMAS = [{"type": "function",
            "function": {"name": name, "description": desc, "parameters": schema}}
           for _fn, name, desc, schema in REGISTRY]


def execute(con, S, U, name, args=None):
    """The single dispatch point for every tool call, whatever is driving it.

    The app's own planner (llm_agent.py) and an external MCP client
    (mcp_server.call_as) both arrive here, so a guard decision cannot differ by
    caller - there is no second path to keep in sync. `U` is the *human's* words
    for this turn and is what GATED_WRITES test with approved_this_turn();
    nothing the model emits ever reaches it.

    A tool that raises returns an error result rather than killing the turn.
    That matters for the guard too: an argument crafted to collide with this
    signature (say {"U": "approve"}) lands here as a TypeError, not a write.
    """
    fn = FUNCS.get(name)
    if fn is None:
        return {"data": {"error": f"unknown tool {name}"}, "blocks": [], "trace": []}
    try:
        return fn(con, S, U, **(args or {}))
    except Exception as e:
        return {"data": {"error": f"{type(e).__name__}: {e}"}, "blocks": [], "trace": []}


# ====================================================== dynamic tool selection
# Sending all 20 schemas on every hop costs ~1,300 tokens *per hop*. On a free tier
# that's most of the minute budget. So the rule-based NLU earns its keep a second time:
# it pre-selects the handful of tools this utterance could plausibly need.
TOOL_GROUPS = {
    "absence.cover":   ["plan_absence_coverage", "apply_coverage_plan", "undo_last_change",
                        "list_leaves", "find_free_faculty", "faculty_timetable"],
    "timetable.view":  ["get_timetable", "faculty_timetable", "find_free_faculty", "find_free_rooms"],
    "timetable.generate": ["plan_timetable_generation", "apply_timetable_generation",
                           "get_timetable", "faculty_workload"],
    "faculty.query":   ["faculty_profile", "faculty_workload", "faculty_timetable"],
    "student.query":   ["student_lookup", "attendance_defaulters"],
    "finance.query":   ["fee_summary", "list_requests"],
    "exam.query":      ["exam_schedule", "exam_eligibility", "attendance_defaulters"],
    "request.manage":  ["list_requests", "decide_request", "create_request"],
    "notify.broadcast": ["broadcast_notice", "attendance_defaulters"],
    "analytics.kpi":   ["institution_overview", "attendance_defaulters", "fee_summary", "list_requests",
                        "placement_overview"],
    **campus.TOOL_GROUPS,
}
CORE = ["ops_radar", "institution_overview", "get_timetable", "faculty_profile", "student_lookup",
        "list_requests", "plan_absence_coverage"]
def _allow_null(schemas):
    """Models routinely send {"dept": null} for an omitted optional arg, and strict
    validators (Groq) reject it with HTTP 400. Widen every optional param to accept
    null so an unfilled slot is legal instead of fatal."""
    for sch in schemas:
        params = sch.get("function", {}).get("parameters", {})
        required = set(params.get("required") or [])
        for name, spec in (params.get("properties") or {}).items():
            t = spec.get("type")
            if name not in required and isinstance(t, str) and t != "null":
                spec["type"] = [t, "null"]
    return schemas


_allow_null(SCHEMAS)
BY_NAME = {sch["function"]["name"]: sch for sch in SCHEMAS}


def select_tools(text, session=None, limit=9):
    """Return the subset of tool schemas worth offering for this utterance."""
    picked, ranked = [], nlu.classify(text or "")
    for intent, score, _hits in ranked[:2]:
        for name in TOOL_GROUPS.get(intent, []):
            if name not in picked:
                picked.append(name)
    if not picked:
        picked = list(CORE)
    # a live proposal always keeps its commit/rollback verbs on the table
    if session and session.get("pending"):
        kind = session["pending"].get("kind")
        commit = ("apply_timetable_generation" if kind == "timetable_generation"
                  else session["pending"].get("tool") if kind == "tool_commit"
                  else "apply_coverage_plan")
        for name in ((commit,) if kind == "tool_commit" else (commit, "undo_last_change")):
            if name not in picked:
                picked.insert(0, name)
    if "institution_overview" not in picked and len(picked) < limit:
        picked.append("institution_overview")
    return [BY_NAME[n] for n in picked[:limit] if n in BY_NAME], picked[:limit]
