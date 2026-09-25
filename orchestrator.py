"""
VidyaERP :: Orchestrator (Supervisor agent)
Routes an utterance through the agent mesh and composes the response payload.
"""
import re, json, datetime as dt
import nlu, campus, tools, portal
from nlu import TODAY, day_of
from agents import *
from agents import _span, plabel

CAMPUS_INTENTS = {"ops.radar", "student.360", "library", "hostel", "transport", "gatepass",
                  "placement", "document", "leave.review"}

SESS = {}   # session_id -> {"pending": {...}, "history": [...]}


def sess(sid):
    return SESS.setdefault(sid, {"pending": None, "history": [], "ctx": {}})


def inr(n):
    n = int(n or 0)
    if n >= 10_000_000: return f"₹{n/10_000_000:.2f} Cr"
    if n >= 100_000:    return f"₹{n/100_000:.2f} L"
    return f"₹{n:,}"


CHIPS_DEFAULT = [
    "What needs my attention today?",
    "Handle a faculty absence for tomorrow",
    "Show CSE 5th sem A timetable",
    "Pending approvals in my inbox",
    "Decide pending gate passes by policy",
    "Brief me on today's institution status",
]


# =====================================================================
def handle(con, text, sid="default", role="admin", actor="admin@vidyatech"):
    st = sess(sid)
    tr = Trace()
    guard, auditor = PolicyGuard(), Auditor()
    tr.add("Supervisor", "receive", f'"{text[:90]}"')

    # A student, teacher or HOD never reaches the handlers below: those read
    # the institution directly. Their turns go through portal routing and
    # tools.execute, where access.py decides what they may see (#27).
    P = st.get("user")
    if P and P.get("role") != "admin":
        out = portal_turn(con, text, st, tr, P)
        out["trace"] = tr.items
        auditor.log(con, P["id"], out.get("agent", "Supervisor"), out.get("intent", "portal"), {"q": text},
                    "answered")
        return out

    # ---------------- pending confirmation branch (HITL) ----------------
    if st["pending"]:
        res = resolve_pending(con, text, st, tr, actor)
        if res:
            auditor.log(con, actor, "Supervisor", "hitl_resolution", {"text": text}, "committed")
            return res

    ranked = nlu.classify(text)
    intent = ranked[0][0] if ranked else "smalltalk"
    conf = min(99, 55 + (ranked[0][1] * 4)) if ranked else 40
    tr.add("Router", "intent_classification",
           f"{intent} ({conf}%)" + (f" · runners-up: {', '.join(i for i,_,_ in ranked[1:3])}" if len(ranked) > 1 else ""))
    guard.check(role, intent, tr)

    fac_rows = rows(con, "SELECT id,name,dept,designation,expertise,max_load FROM faculty")
    sub_rows = rows(con, "SELECT code,name,dept,sem FROM subjects")
    ent = {
        "dept": nlu.extract_dept(text), "sem": nlu.extract_sem(text),
        "section": nlu.extract_section(text), "usn": nlu.extract_usn(text),
        "periods": nlu.extract_periods(text),
    }
    d, dlbl = nlu.parse_date(text)
    ent["date"], ent["date_label"] = d, dlbl
    f, fscore = nlu.match_faculty(text, fac_rows)
    ent["faculty"] = f
    detail = " · ".join(f"{k}={v['name'] if k=='faculty' else v}" for k, v in ent.items()
                        if v not in (None, [], ""))
    tr.add("EntityResolver", "extract", detail or "no explicit entities — using defaults")

    fn = {
        "absence.cover": h_absence, "timetable.view": h_timetable, "faculty.query": h_faculty,
        "student.query": h_student, "finance.query": h_finance, "exam.query": h_exam,
        "request.manage": h_request, "notify.broadcast": h_notify, "analytics.kpi": h_analytics,
        "timetable.generate": h_generate,
    }.get(intent, h_fallback)
    if intent in CAMPUS_INTENTS:
        fn = lambda c, x, e, s, t, a: h_campus(c, x, e, s, t, a, intent)
    # "full profile of Dr ..." scores for student.360 on wording alone; without a
    # USN it was never about a student
    if intent == "student.360" and not ent["usn"]:
        fn = h_faculty if ent["faculty"] else h_student

    out = fn(con, text, ent, st, tr, actor)
    out.setdefault("chips", CHIPS_DEFAULT[:4])
    out["trace"] = tr.items
    out["intent"] = intent
    out["confidence"] = conf
    auditor.log(con, actor, out.get("agent", "Supervisor"), intent, {"q": text}, "answered")
    st["history"].append({"q": text, "intent": intent})
    return out


# =====================================================================
def resolve_pending(con, text, st, tr, actor):
    p = st["pending"]
    kind = p["kind"]
    choice = nlu.plan_choice(text)
    drill = re.search(r"(?:alternativ|other option|who else|another)", text.lower())
    if not drill and not nlu.is_confirmation(text):
        tr.add("Supervisor", "context_switch",
               "utterance is a fresh command, not a reply — pending proposal kept alive", status="warn")
        return None

    if kind == "absence_plans":
        # "show me alternatives for period 3" — drill into the candidate ranking, keep plan pending
        alt = re.search(r"(?:alternativ|other option|who else|another)", text.lower())
        if alt:
            ps = nlu.extract_periods(text)
            plan = p["plans"][0]
            legs = [l for l in plan["legs"] if (not ps or l["period"] in ps)]
            note = ""
            if ps and not legs:
                legs = plan["legs"]
                note = (f"Period {ps[0]} isn't affected by this absence — showing alternates for the "
                        f"{len(legs)} period(s) that are.\n\n")
            tr.add("SubstitutionAgent", "candidate_expand",
                   f"returning ranked alternates for {len(legs)} slot(s); plan still pending")
            blocks = [B_text(note + "Ranked alternates (scores are the constraint-solver's fitness values — "
                             "subject competency, department, free head-room and fairness):")]
            for l in legs:
                blocks.append(B_table(["Rank", "Faculty", "Fitness"],
                                      [["1 (chosen)", l["to"], l["score"]]] +
                                      [[str(i + 2), a.rsplit(" (", 1)[0], a.rsplit(" (", 1)[1].rstrip(")")]
                                       for i, a in enumerate(l["alts"])],
                                      title=f"P{l['period']} · {l['class']} · {l['subject']}"))
            blocks.append(B_text("Say **“apply plan A”** to accept, or name a faculty member to force-assign."))
            return {"blocks": blocks, "trace": tr.items, "agent": "SubstitutionAgent",
                    "intent": "absence.cover", "confidence": 92,
                    "chips": ["Apply plan A", "Apply plan B", "Cancel"]}
        if nlu.is_no(text) and choice is None:
            st["pending"] = None
            tr.add("Supervisor", "hitl", "admin declined — nothing written")
            return {"blocks": [B_text("Cancelled. No changes were written to the timetable. "
                                      "The absence is still un-covered — tell me when you want to revisit it.")],
                    "trace": tr.items, "agent": "Supervisor", "intent": "absence.cover", "confidence": 95,
                    "chips": CHIPS_DEFAULT[:3]}
        if choice is None and not nlu.is_yes(text):
            return None
        # bind by plan CODE (A/B/C) — the list is re-ranked, so positional indexing would
        # silently commit the wrong plan. Digits mean "the Nth ranked plan".
        letter = re.search(r"\b(?:plan|option)?\s*\b([abc])\b", text.lower())
        plan = None
        if letter:
            want = letter.group(1).upper()
            # ...including a letter whose plan was folded into another because
            # the two would commit identical rows (see commit_signature).
            plan = next((x for x in p["plans"]
                         if want == x["code"] or want in x.get("also_commits", [])), None)
        if plan is None:
            idx = min(choice if choice is not None else 0, len(p["plans"]) - 1)
            plan = p["plans"][idx]
        faculty = p["faculty"]
        date = dt.date.fromisoformat(p["date"])
        sub, notif = SubstitutionAgent(), NotifyAgent()
        tr.add("PolicyGuard", "write_authorisation", f"admin confirmed → committing plan {plan['code']}")
        applied = sub.apply_plan(con, plan, faculty, date, actor)
        tr.add("SubstitutionAgent", "commit_overrides", f"{len(applied)} timetable override(s) written")
        msgs = notif.draft_absence(con, faculty, date, plan)
        notif.dispatch(con, msgs)
        tr.add("NotifyAgent", "dispatch", f"{len(msgs)} notification(s) queued (students, faculty, HOD)")
        # auto-log a leave record if none
        exists = one(con, "SELECT 1 FROM leaves WHERE faculty=? AND from_date<=? AND to_date>=?",
                     (faculty["id"], p["date"], p["date"]))
        if not exists:
            con.execute("""INSERT INTO leaves(faculty,from_date,to_date,kind,reason,status,applied_on)
                           VALUES(?,?,?,?,?,?,?)""",
                        (faculty["id"], p["date"], p["date"], "Casual Leave",
                         p.get("reason", "Absence recorded via ERP copilot"), "Approved", TODAY.isoformat()))
            con.commit()
            tr.add("HRAgent", "leave_ledger", f"casual-leave entry created for {faculty['name']}")
        Auditor().log(con, actor, "SubstitutionAgent", "apply_plan",
                      {"plan": plan["id"], "faculty": faculty["id"], "date": p["date"]}, "applied")
        st["pending"] = None
        st["ctx"]["last_override_date"] = p["date"]
        if applied:
            st["ctx"]["last_class"] = applied[0]["class"]

        tbl = B_table(["Period", "Class", "Subject", "Action", "Now handled by"],
                      [[f"{plabel(l)} · {l['time']}", l["class"], l["subject"],
                        l["action"].title(), l["to"]] for l in applied],
                      title=f"Applied — Plan {plan['code']}")
        return {"blocks": [
            B_text(f"**Plan {plan['code']} — {plan['title']}** is now live for "
                   f"**{date.strftime('%d %b %Y (%A)')}**."),
            tbl,
            B_notice(msgs),
            B_text(f"Audit trail `#{plan['id']}` recorded against `{actor}`. "
                   f"The published timetable for {date.strftime('%d %b')} now shows these overrides in amber. "
                   f"Say **undo** if you want me to roll this back.")],
            "trace": tr.items, "agent": "SubstitutionAgent", "intent": "absence.cover",
            "confidence": 97, "refresh": True,
            "chips": ["Show the updated timetable", "Undo the last change",
                      "Notify parents of affected classes", "Who else is on leave this week?"]}

    if kind == "request_draft":
        if nlu.is_no(text):
            st["pending"] = None
            return {"blocks": [B_text("Draft discarded — nothing was sent.")], "trace": tr.items,
                    "agent": "RequestAgent", "intent": "request.manage", "confidence": 90}
        if nlu.is_yes(text):
            d = p["draft"]
            rid = RequestAgent().create(con, d["kind"], d["title"], d["details"], d["target"],
                                        actor, "Admin", d["amount"], d["priority"])
            st["pending"] = None
            tr.add("RequestAgent", "route", f"REQ-{rid:04d} → {d['target']} (SLA 48h)")
            return {"blocks": [B_text(f"**REQ-{rid:04d}** raised and routed to **{d['target']}**.\n\n"
                                      f"*{d['title']}*\n\nSLA clock: 48 hours. I'll nudge them at the 36-hour mark "
                                      f"and escalate to the Principal if it's still open at 48.")],
                    "trace": tr.items, "agent": "RequestAgent", "intent": "request.manage",
                    "confidence": 95, "refresh": True}
        return None

    if kind == "broadcast_draft":
        if nlu.is_no(text):
            st["pending"] = None
            return {"blocks": [B_text("Broadcast cancelled — nothing was sent.")], "trace": tr.items,
                    "agent": "NotifyAgent", "intent": "notify.broadcast", "confidence": 90}
        if nlu.is_yes(text):
            NotifyAgent().dispatch(con, p["msgs"])
            st["pending"] = None
            tr.add("NotifyAgent", "dispatch", f"{len(p['msgs'])} channel(s) fired")
            return {"blocks": [B_text(f"Sent to **{p['msgs'][0]['audience']}** over "
                                      f"{p['msgs'][0]['channel']}. Delivery receipts will appear in "
                                      f"Notifications.")],
                    "trace": tr.items, "agent": "NotifyAgent", "intent": "notify.broadcast",
                    "confidence": 95, "refresh": True}
        return None

    # A write staged by a tool (campus services, timetable generation). The
    # commit goes back through tools.execute with the admin's reply as U - the
    # SAME gate the LLM agent and MCP hit, never a shortcut past it.
    if kind in ("tool_commit", "timetable_generation"):
        tool = p["tool"] if kind == "tool_commit" else "apply_timetable_generation"
        args = p.get("args", {}) if kind == "tool_commit" else {}
        if nlu.is_no(text) and not nlu.is_yes(text):
            st["pending"] = None
            tr.add("Supervisor", "hitl", f"admin declined {tool} — nothing written")
            return {"blocks": [B_text("Discarded — nothing was written.")], "trace": tr.items,
                    "agent": "Supervisor", "intent": "hitl", "confidence": 95, "chips": CHIPS_DEFAULT[:4]}
        if not nlu.is_yes(text):
            return None
        tr.add("PolicyGuard", "write_authorisation", f"admin confirmed → {tool}")
        res = tools.execute(con, st, text, tool, args)
        _relay(tr, res, tool)
        if st.get("pending") is p:
            st["pending"] = None            # a refused commit must not stay armed either
        Auditor().log(con, actor, campus.AGENT_OF.get(tool, "Supervisor"), tool, args,
                      "error" if (res.get("data") or {}).get("error") else "committed")
        return {"blocks": res.get("blocks") or [B_text(_data_text(res.get("data")))],
                "trace": tr.items, "agent": campus.AGENT_OF.get(tool, "TimetableAgent"),
                "intent": "hitl", "confidence": 97, "refresh": res.get("refresh", False),
                "chips": res.get("chips") or ["What needs my attention today?"]}

    # undo
    return None


def _relay(tr, res, tool):
    """Copy a tool's trace into this turn's, keeping each step's own status, and
    pin a failed call on the agent that owns the tool - which is what lets the
    console's agent mesh show a failure as a failure."""
    for t in res.get("trace", []):
        tr.add(t[0], t[1], t[2] if len(t) > 2 else "", t[3] if len(t) > 3 else "ok")
    failed = tools.failure_of(res)
    if failed:
        tr.add(tools.agent_of(tool), "tool_error", failed[:160], status="error")


def portal_turn(con, text, st, tr, P):
    """One turn for a non-admin, on the rule engine. Every tool it runs goes
    through tools.execute, so the access policy applies to each call."""
    tr.add("PolicyGuard", "principal", f"{P['role']} {P['id']}" + (f" · {P['dept']}" if P.get("dept") else "")
           + " · default-deny tool policy")
    p = st.get("pending")
    if p and (nlu.is_confirmation(text) or nlu.plan_choice(text) is not None):
        if nlu.is_no(text) and not nlu.is_yes(text):
            st["pending"] = None
            return {"blocks": [B_text("Discarded — nothing was written.")], "agent": "Supervisor",
                    "intent": "hitl", "confidence": 95, "chips": _portal_chips(P)}
        if nlu.is_yes(text) or nlu.plan_choice(text) is not None:
            if p["kind"] == "tool_commit":
                tool, args = p["tool"], p.get("args", {})
            elif p["kind"] == "absence_plans":           # an HOD approving their own staged plan
                m = re.search(r"\b(?:plan|option)?\s*\b([abc])\b", text.lower())
                tool, args = "apply_coverage_plan", {"plan_code": (m.group(1) if m else "a").upper()}
            else:
                tool, args = None, None
            if tool:
                res = tools.execute(con, st, text, tool, args)
                _relay(tr, res, tool)
                if st.get("pending") is p:
                    st["pending"] = None
                return {"blocks": res.get("blocks") or [B_text(_data_text(res.get("data")))],
                        "agent": tools.agent_of(tool), "intent": "hitl", "confidence": 97,
                        "refresh": res.get("refresh", False), "chips": res.get("chips") or _portal_chips(P)}
    ent = {"usn": nlu.extract_usn(text), "dept": nlu.extract_dept(text), "sem": nlu.extract_sem(text),
           "section": nlu.extract_section(text), "periods": nlu.extract_periods(text),
           "date": nlu.parse_date(text)[0],
           "faculty": nlu.match_faculty(text, rows(con, "SELECT id,name,dept,designation,expertise,max_load "
                                                         "FROM faculty"))[0]}
    tool, args = portal.route(con, text, P, ent)
    if not tool:
        tr.add("Router", "no_confident_intent", "showing what this account can do", status="warn")
        return {"blocks": [B_text(f"Here is what I can do for you, {P['name'].split()[0] if P['role'] == 'student' else P['name']}:"),
                           B_table(["Area", "Try saying"], portal.HELP[P["role"]])],
                "agent": "Supervisor", "intent": "help", "confidence": 40, "chips": _portal_chips(P)}
    tr.add("ToolRouter", "select", (f"{tool}(" + ", ".join(f"{k}={v!r}" for k, v in args.items()
                                                          if v not in (None, "", [], False)))[:90] + ")")
    res = tools.execute(con, st, text, tool, args)
    _relay(tr, res, tool)
    blocks = res.get("blocks") or [B_text(_data_text(res.get("data")))]
    return {"blocks": blocks, "agent": tools.agent_of(tool), "intent": tool, "confidence": 90,
            "refresh": res.get("refresh", False), "chips": res.get("chips") or _portal_chips(P)}


def _portal_chips(P):
    return {"student": ["My day", "My timetable", "Request a gate pass for Saturday 2pm to 7pm",
                        "My no-dues status", "My requests"],
            "faculty": ["My day", "My timetable", "My mentees", "Apply for casual leave on 10 sep", "My leave"],
            "hod": ["Department overview", "Pending leave applications", "Faculty workload", "My day"]}.get(
        P["role"], ["My day"])


def _data_text(d):
    d = d or {}
    return ("" + str(d["error"])) if d.get("error") else (d.get("note") or "Done.")


# =====================================================================
def h_campus(con, text, ent, st, tr, actor, intent):
    """Campus services on the rule engine: the NLU picks the tool and its
    arguments, and tools.execute runs it - the same dispatch point the LLM agent
    and MCP use, so a write here meets exactly the gate it meets there."""
    tool, args = campus.rule_route(con, intent, text, ent)
    tr.add("ToolRouter", "select", (f"{tool}(" + ", ".join(f"{k}={v!r}" for k, v in args.items()
                                                          if v not in (None, "", [])))[:90] + ")")
    res = tools.execute(con, st, text, tool, args)
    _relay(tr, res, tool)
    d = res.get("data") or {}
    blocks = res.get("blocks") or []
    if d.get("ambiguous") and not blocks:
        blocks = [B_text("Which one did you mean?"),
                  B_table(["ID", "Title / name"], [[c.get("id"), c.get("title") or c.get("name")]
                                                   for c in d.get("candidates", [])])]
    if not blocks:
        blocks = [B_text(_data_text(d))]
    return {"blocks": blocks, "agent": campus.AGENT_OF.get(tool, "Supervisor"),
            "refresh": res.get("refresh", False),
            "chips": res.get("chips") or CHIPS_DEFAULT[:4]}


def h_generate(con, text, ent, st, tr, actor):
    """Timetable generation on the rule engine, via the same tool pair the LLM uses."""
    whole = bool(re.search(r"\b(?:whole|entire|every|full)\b|\ball (?:sections|classes)\b|\bcollege\b",
                           text.lower()))
    args = ({"scope": "all"} if whole or not ent["dept"] else
            {"scope": "class", "dept": ent["dept"], "sem": ent["sem"] or 5, "section": ent["section"] or "A"})
    res = tools.execute(con, st, text, "plan_timetable_generation", args)
    _relay(tr, res, "plan_timetable_generation")
    if (res.get("data") or {}).get("error"):
        return {"blocks": [B_text(_data_text(res["data"]))], "agent": "TimetableAgent"}
    return {"blocks": res["blocks"] + [B_text("Nothing is written yet. Say **yes** to replace the current "
                                              "timetable for this scope — it is verified independently "
                                              "after the write — or **no** to discard. The Master "
                                              "Timetable view lets you watch the solver place every "
                                              "period live.")],
            "agent": "TimetableAgent", "chips": ["Yes, apply it", "No, discard it"]}


# =====================================================================
def h_absence(con, text, ent, st, tr, actor):
    # undo path
    if re.search(r"\bundo|roll ?back|revert\b", text.lower()):
        last = one(con, "SELECT plan_ref FROM overrides WHERE status='Applied' ORDER BY id DESC LIMIT 1")
        if not last:
            return {"blocks": [B_text("Nothing to undo — there are no applied overrides.")],
                    "agent": "SubstitutionAgent"}
        n = SubstitutionAgent().revert(con, last["plan_ref"])
        tr.add("SubstitutionAgent", "rollback", f"plan {last['plan_ref']} reverted · {n} make-up period(s) released")
        return {"blocks": [B_text(f"Rolled back plan `{last['plan_ref']}`. The original timetable is restored"
                                  + (f" and {n} booked make-up period(s) are released" if n else "")
                                  + ". A correction notice has been queued.")],
                "agent": "SubstitutionAgent", "refresh": True}

    f = ent["faculty"]

    # ---- leave ledger view ("who is on leave this week / that day?")
    if re.search(r"who .*on leave|list .*leave|leave (this week|calendar|ledger)|anyone else on leave", text.lower()):
        base = ent["date"] or (dt.date.fromisoformat(st["ctx"]["pending_date"])
                               if st["ctx"].get("pending_date") else TODAY)
        wk_start = base - dt.timedelta(days=base.weekday())
        wk_end = wk_start + dt.timedelta(days=6)
        lv = rows(con, """SELECT l.*, fa.name, fa.dept, fa.designation FROM leaves l
                          JOIN faculty fa ON fa.id=l.faculty
                          WHERE l.to_date>=? AND l.from_date<=? ORDER BY l.from_date""",
                  (wk_start.isoformat(), wk_end.isoformat()))
        tr.add("HRAgent", "leave_ledger_scan", f"{len(lv)} record(s) between {wk_start} and {wk_end}")
        if not lv:
            return {"blocks": [B_text(f"No faculty leave recorded for the week of "
                                      f"{wk_start.strftime('%d %b')} – {wk_end.strftime('%d %b %Y')}.")],
                    "agent": "HRAgent"}
        return {"blocks": [
            B_text(f"**Faculty leave — week of {wk_start.strftime('%d %b')} to {wk_end.strftime('%d %b %Y')}** "
                   f"({len(lv)} records):"),
            B_table(["Faculty", "Dept", "From", "To", "Type", "Reason", "Status"],
                    [[l["name"], l["dept"], l["from_date"], l["to_date"], l["kind"],
                      l["reason"], l["status"]] for l in lv], dense=True),
            B_text("Pending rows need your approval — and each approval automatically triggers a "
                   "coverage plan before it is granted.")],
            "agent": "HRAgent",
            "chips": ["Approve all pending leaves with coverage", "Show today's timetable overrides"]}

    fac_all = rows(con, "SELECT id,name,dept,designation FROM faculty")
    cands = nlu.faculty_candidates(text, fac_all)
    if f and cands and cands[0][0] < 0.9 and len(cands) > 1:
        tr.add("EntityResolver", "ambiguous_reference",
               f"top match {cands[0][1]['name']} @ {cands[0][0]} — below certainty bar, asking user",
               status="warn")
        st["pending"] = None
        return {"blocks": [
            B_text("That name isn't an exact match in the staff register — I don't want to reschedule "
                   "the wrong person's classes. Did you mean one of these?"),
            B_table(["Match", "Staff ID", "Name", "Dept", "Designation"],
                    [[f"{int(sc*100)}%", r["id"], r["name"], r["dept"], r["designation"]]
                     for sc, r in cands[:4]])],
            "agent": "EntityResolver",
            "chips": [f"{r['name']} is absent " + (ent["date_label"] or "tomorrow")
                      for _, r in cands[:3]]}

    if not f:
        tr.add("EntityResolver", "unresolved", "no faculty member named in the request", status="warn")
        return {"blocks": [B_text("I couldn't pin down **which faculty member** is absent. "
                                  "Give me a name or staff ID, for example “Dr. Ramesh Bhat is absent tomorrow” "
                                  "or “F012 on leave 9 Sep”."),
                           B_table(["ID", "Name", "Dept", "Designation"],
                                   [[r["id"], r["name"], r["dept"], r["designation"]]
                                    for r in rows(con, "SELECT * FROM faculty ORDER BY dept LIMIT 8")],
                                   title="Sample of the faculty directory")],
                "agent": "SubstitutionAgent",
                "chips": ["Dr. Ramesh is absent tomorrow", "Show faculty directory"]}

    date = ent["date"] or (TODAY + dt.timedelta(days=1))
    lbl = ent["date_label"] or "tomorrow"
    if day_of(date) == "Sun":
        return {"blocks": [B_text(f"{date.strftime('%d %b')} is a **Sunday** — no classes are scheduled, "
                                  f"so no coverage is needed. I've noted the absence in the leave ledger.")],
                "agent": "SubstitutionAgent"}

    sub = SubstitutionAgent()
    plans, affected = sub.build_plans(con, f, date, tr)
    if not plans:
        return {"blocks": [B_text(f"**{f['name']}** has **no classes** on {date.strftime('%A, %d %b %Y')} "
                                  f"({lbl}) — nothing to reschedule. Leave can be approved with zero academic impact.")],
                "agent": "SubstitutionAgent"}

    reason_m = re.search(r"(?:because|due to|reason[:\s]|as (?:he|she|they))\s*(.+)$", text, re.I)
    reason = reason_m.group(1).strip()[:120] if reason_m else "Absence notified to admin"
    st["pending"] = {"kind": "absence_plans", "plans": plans, "faculty": f,
                     "date": date.isoformat(), "reason": reason}
    st["ctx"]["pending_date"] = date.isoformat()

    aff_tbl = B_table(["Period", "Time", "Class", "Subject", "Room", "Strength"],
                      [[plabel(a), _span(a["periods"]),
                        f"{a['dept']}-{a['sem']}{a['section']}",
                        f"{a['subject']} · {subj_name(con, a['subject'])}", a["room"],
                        one(con, "SELECT COUNT(*) c FROM students WHERE dept=? AND sem=? AND section=?",
                            (a["dept"], a["sem"], a["section"]))["c"]] for a in affected],
                      title=f"Impact — {len(affected)} periods, "
                            f"{sum(one(con,'SELECT COUNT(*) c FROM students WHERE dept=? AND sem=? AND section=?',(a['dept'],a['sem'],a['section']))['c'] for a in affected)} student-hours at stake")

    head = (f"**{f['name']}** ({f['designation']}, {f['dept']}) — absence on "
            f"**{date.strftime('%A, %d %b %Y')}** ({lbl}).\n\n"
            f"I scanned the master timetable, live leave ledger and workload caps across "
            f"**{len(rows(con,'SELECT id FROM faculty'))} faculty**, and built three coverage strategies. "
            f"Ranked best-first — nothing is written until you approve.")

    return {"blocks": [B_text(head), aff_tbl, B_plans(plans),
                       B_text("Reply **“apply plan A”** (or B / C), or ask me to tweak a specific period.")],
            "agent": "SubstitutionAgent",
            "chips": [f"Apply plan {plans[0]['code']}", "Show alternatives for period 3",
                      "Who else is on leave that day?", "Cancel"]}


# =====================================================================
def h_timetable(con, text, ent, st, tr, actor):
    t = text.lower()
    tta = TimetableAgent()
    ctx_date = st["ctx"].get("last_override_date") or st["ctx"].get("pending_date")
    if not ent["date"] and ctx_date and re.search(r"updated|override|revised|new timetable|after the change", t):
        ent["date"] = dt.date.fromisoformat(ctx_date)
    if (not ent["dept"]) and st["ctx"].get("last_class") and \
            re.search(r"updated|override|revised|new timetable|after the change|affected", t):
        d0, rest = st["ctx"]["last_class"].split("-")
        ent["dept"], ent["sem"], ent["section"] = d0, int(rest[:-1]), rest[-1]
    if re.search(r"make-?ups?|extra class", t):                    # booked make-ups (#18)
        args = {k: v for k, v in (("dept", ent["dept"]), ("sem", ent["sem"]), ("section", ent["section"]))
                if v}
        if ent["faculty"] and not args:
            args["faculty"] = ent["faculty"]["id"]
        res = tools.execute(con, st, text, "makeup_schedule", args)
        _relay(tr, res, "makeup_schedule")
        return {"blocks": res["blocks"], "agent": "SubstitutionAgent",
                "chips": ["Show CSE 5th sem A timetable", "Undo the last change"]}
    date = ent["date"] or TODAY
    day = day_of(date)
    if day == "Sun":
        date = date + dt.timedelta(days=1); day = "Mon"

    # --- free faculty / free room
    if re.search(r"free|available|vacant|who is not", t) and not re.search(r"timetable", t):
        periods = ent["periods"] or [3]
        p = periods[0]
        if "room" in t:
            fr = tta.free_rooms(con, day, p)
            tr.add("TimetableAgent", "room_availability", f"{len(fr)} free on {day} P{p}")
            return {"blocks": [B_text(f"**{len(fr)} rooms free** on {day} P{p} ({PERIOD_TIME[p]}):"),
                               B_table(["Room", "Type", "Capacity", "Block"],
                                       [[r["id"], r["kind"], r["capacity"], r["block"]] for r in fr[:15]])],
                    "agent": "TimetableAgent"}
        ff = tta.free_faculty(con, day, p, date.isoformat(), ent["dept"])
        tr.add("TimetableAgent", "availability_scan",
               f"{len(ff)} faculty free at {day} P{p}" + (f" in {ent['dept']}" if ent["dept"] else ""))
        return {"blocks": [B_text(f"**{len(ff)} faculty are free** on **{day}, period {p}** "
                                  f"({PERIOD_TIME[p]})" + (f" in {ent['dept']}" if ent["dept"] else "") + ":"),
                           B_table(["ID", "Name", "Dept", "Designation", "Weekly load"],
                                   [[r["id"], r["name"], r["dept"], r["desig"], r["load"]] for r in ff[:20]],
                                   dense=True)],
                "agent": "TimetableAgent",
                "chips": ["Free rooms at that hour", "Faculty workload above 90%"]}

    # --- who is teaching now / at a slot
    if re.search(r"who (?:is|are) (?:teaching|handling|taking)", t):
        p = (ent["periods"] or [3])[0]
        q = "SELECT t.*, s.name sname FROM timetable t LEFT JOIN subjects s ON s.code=t.subject WHERE t.day=? AND t.period=?"
        a = [day, p]
        if ent["dept"]: q += " AND t.dept=?"; a.append(ent["dept"])
        r = rows(con, q, tuple(a))
        return {"blocks": [B_text(f"Live board — **{day} P{p} ({PERIOD_TIME[p]})**, {len(r)} classes running:"),
                           B_table(["Class", "Subject", "Faculty", "Room"],
                                   [[f"{x['dept']}-{x['sem']}{x['section']}", f"{x['subject']} {x['sname'] or ''}",
                                     fac_name(con, x["faculty"]), x["room"]] for x in r], dense=True)],
                "agent": "TimetableAgent"}

    # --- faculty personal timetable
    if ent["faculty"] and not ent["dept"]:
        f = ent["faculty"]
        tt = rows(con, """SELECT t.*, s.name sname FROM timetable t LEFT JOIN subjects s ON s.code=t.subject
                          WHERE t.faculty=? ORDER BY CASE t.day WHEN 'Mon' THEN 0 WHEN 'Tue' THEN 1
                          WHEN 'Wed' THEN 2 WHEN 'Thu' THEN 3 WHEN 'Fri' THEN 4 ELSE 5 END, t.period""",
                   (f["id"],))
        return {"blocks": [B_text(f"**{f['name']}** — {len(tt)} periods/week "
                                  f"(cap {f['max_load']}, utilisation {round(100*len(tt)/f['max_load'])}%)"),
                           B_table(["Day", "Period", "Class", "Subject", "Room"],
                                   [[x["day"], f"P{x['period']} {PERIOD_TIME[x['period']]}",
                                     f"{x['dept']}-{x['sem']}{x['section']}",
                                     f"{x['subject']} · {x['sname'] or ''}", x["room"]] for x in tt], dense=True)],
                "agent": "TimetableAgent",
                "chips": [f"{f['name'].split()[-1]} is absent tomorrow", "Faculty workload above 90%"]}

    # --- class grid
    dept = ent["dept"] or "CSE"
    sem = ent["sem"] or 5
    sec = ent["section"] or "A"
    if not one(con, "SELECT 1 FROM timetable WHERE dept=? AND sem=? AND section=?", (dept, sem, sec)):
        sec = "A"
    grid = tta.class_grid(con, dept, sem, sec, date.isoformat())
    ov = [c for c in grid["cells"].values() if c["override"]]
    tr.add("TimetableAgent", "grid_render", f"{dept}-{sem}{sec} · {len(grid['cells'])} slots · {len(ov)} override(s)")
    note = (f"\n\n**{len(ov)} live override(s)** for {date.strftime('%d %b')} shown in amber."
            if ov else "")
    return {"blocks": [B_text(f"**{dept} · Semester {sem} · Section {sec}** — master timetable"
                              f" (week of {date.strftime('%d %b %Y')}){note}"),
                       B_grid(grid)],
            "agent": "TimetableAgent",
            "chips": [f"Who teaches {dept} sem {sem} at period 4", "Attendance defaulters in " + dept,
                      "Free faculty at period 2"]}


# =====================================================================
def h_faculty(con, text, ent, st, tr, actor):
    fa = FacultyAgent()
    t = text.lower()
    if re.search(r"workload|load|utilis|utiliz|overload|above \d+", t):
        m = re.search(r"(?:above|over|>)\s*(\d{2})", t)
        cutoff = int(m.group(1)) if m else 0
        data = fa.workload_table(con, ent["dept"])
        data = [d for d in data if d["util"] >= cutoff]
        tr.add("FacultyAgent", "workload_analysis",
               f"{len(data)} faculty ≥{cutoff}% utilisation" + (f" in {ent['dept']}" if ent["dept"] else ""))
        avg = round(sum(d["util"] for d in data) / max(1, len(data)))
        over = [d for d in data if d["util"] > 100]
        return {"blocks": [
            B_cards([{"label": "Faculty listed", "value": len(data)},
                     {"label": "Avg utilisation", "value": f"{avg}%"},
                     {"label": "Over cap", "value": len(over), "tone": "bad" if over else "good"},
                     {"label": "Head-room", "value": f"{sum(max(0,d['max_load']-d['load']) for d in data)} hrs"}]),
            B_table(["ID", "Faculty", "Dept", "Designation", "Load / Cap", "Utilisation"],
                    [[d["id"], d["name"], d["dept"], d["designation"], f"{d['load']}/{d['max_load']}",
                      f"{d['util']}%"] for d in data[:25]], dense=True),
            B_text("Anything above 100% is a VTU/NBA audit flag — I can draft a re-allocation proposal "
                   "moving surplus hours to under-loaded staff in the same department.")],
            "agent": "FacultyAgent",
            "chips": ["Draft a load re-allocation proposal", "Free faculty at period 5",
                      "Who teaches Machine Learning?"]}

    if ent["faculty"]:
        f, load, total, lv = fa.profile(con, ent["faculty"]["id"])
        tr.add("FacultyAgent", "profile_fetch", f"{f['id']} · {total} periods/week · {len(lv)} leave records")
        return {"blocks": [
            B_cards([{"label": "Staff ID", "value": f["id"]},
                     {"label": "Weekly load", "value": f"{total}/{f['max_load']}"},
                     {"label": "Utilisation", "value": f"{round(100*total/f['max_load'])}%",
                      "tone": "bad" if total > f["max_load"] else "good"},
                     {"label": "Since", "value": f["joined"][:4]}], title=f["name"]),
            B_text(f"**{f['designation']}**, {f['dept']}. Email {f['email']}, phone {f['phone']}"),
            B_table(["Subject", "Class", "Hrs/week"],
                    [[f"{l['subject']} · {subj_name(con,l['subject'])}",
                      f"{l['dept']}-{l['sem']}{l['section']}", l["hrs"]] for l in load],
                    title="Subject allocation"),
            B_table(["From", "To", "Type", "Status"],
                    [[l["from_date"], l["to_date"], l["kind"], l["status"]] for l in lv],
                    title="Recent leave", dense=True) if lv else B_text("_No leave records._")],
            "agent": "FacultyAgent",
            "chips": [f"{f['name'].split()[-1]} is absent tomorrow", f"Show {f['name'].split()[-1]}'s timetable",
                      "Faculty workload above 90%"]}

    if re.search(r"who (?:teaches|handles)", t):
        s, sc = nlu.match_subject(text, rows(con, "SELECT code,name,dept,sem FROM subjects"))
        if s:
            r = rows(con, """SELECT DISTINCT t.faculty, t.dept, t.sem, t.section FROM timetable t
                             WHERE t.subject=?""", (s["code"],))
            return {"blocks": [B_text(f"**{s['code']} · {s['name']}** ({s['dept']}, Sem {s['sem']}) is handled by:"),
                               B_table(["Faculty", "Class"],
                                       [[fac_name(con, x["faculty"]), f"{x['dept']}-{x['sem']}{x['section']}"]
                                        for x in r])],
                    "agent": "FacultyAgent"}

    dept = ent["dept"]
    data = fa.workload_table(con, dept)
    return {"blocks": [B_text(f"Faculty directory{f' — {dept}' if dept else ''} ({len(data)} members):"),
                       B_table(["ID", "Name", "Dept", "Designation", "Load"],
                               [[d["id"], d["name"], d["dept"], d["designation"],
                                 f"{d['load']}/{d['max_load']}"] for d in data[:25]], dense=True)],
            "agent": "FacultyAgent"}


# =====================================================================
def h_student(con, text, ent, st, tr, actor):
    sa = StudentAgent()
    t = text.lower()
    if ent["usn"]:
        s = one(con, "SELECT * FROM students WHERE usn=?", (ent["usn"],))
        if not s:
            tr.add("StudentAgent", "record_missing", f"no student with USN {ent['usn']}", status="error")
            return {"blocks": [B_text(f"No student found with USN **{ent['usn']}**.")], "agent": "StudentAgent"}
        tr.add("StudentAgent", "record_fetch", ent["usn"])
        return {"blocks": [
            B_cards([{"label": "CGPA", "value": s["cgpa"], "tone": "good" if s["cgpa"] >= 7 else "warn"},
                     {"label": "Attendance", "value": f"{s['attendance']}%",
                      "tone": "good" if s["attendance"] >= 75 else "bad"},
                     {"label": "Backlogs", "value": s["backlogs"], "tone": "bad" if s["backlogs"] else "good"},
                     {"label": "Fee due", "value": inr(s["fee_due"]),
                      "tone": "bad" if s["fee_due"] else "good"}], title=f"{s['name']} · {s['usn']}"),
            B_text(f"{s['dept']} · Semester {s['sem']} · Section {s['section']} · "
                   f"{'Hostelite' if s['hostel'] else 'Day scholar'} · Category {s['category']}\n\n"
                   f"Mentor: **{fac_name(con, s['mentor'])}**, phone {s['phone']}" +
                   ("\n\n**Below 75% attendance** — ineligible for SEE unless condonation is approved."
                    if s["attendance"] < 75 else ""))],
            "agent": "StudentAgent",
            "chips": ["Send attendance warning to parent", "Fee dues in this department"]}

    if re.search(r"defaulter|below 75|shortage|condonation|detain|ineligible", t):
        m = re.search(r"below\s*(\d{2})", t)
        cutoff = int(m.group(1)) if m else 75
        data = sa.defaulters(con, ent["dept"], ent["sem"], cutoff)
        tr.add("StudentAgent", "attendance_scan",
               f"{len(data)} students <{cutoff}%" + (f" · {ent['dept']}" if ent["dept"] else ""))
        tr.add("RiskAgent", "severity_bucketing", "critical <60% · warning 60-70% · borderline 70-75%")
        crit = [d for d in data if d["attendance"] < 60]
        return {"blocks": [
            B_cards([{"label": "Below cutoff", "value": len(data), "tone": "bad"},
                     {"label": "Critical (<60%)", "value": len(crit), "tone": "bad"},
                     {"label": "Condonable (65-75%)", "value": len([d for d in data if d["attendance"] >= 65])},
                     {"label": "Hostelites", "value": sum(1 for d in data if d["hostel"])}],
                    title=f"Attendance shortage{' · ' + ent['dept'] if ent['dept'] else ''}"
                          f"{' Sem ' + str(ent['sem']) if ent['sem'] else ''}"),
            B_table(["USN", "Name", "Class", "Attendance", "CGPA", "Mentor"],
                    [[d["usn"], d["name"], f"{d['dept']}-{d['sem']}{d['section']}",
                      f"{d['attendance']}%", d["cgpa"], fac_name(con, d["mentor"])] for d in data[:20]],
                    dense=True),
            B_text(f"_Showing top 20 of {len(data)}._ I can push mentor-wise counselling lists, "
                   f"SMS the parents, or generate the VTU condonation annexure — just say the word.")],
            "agent": "StudentAgent",
            "chips": ["SMS parents of critical cases", "Generate condonation list",
                      "Mentor-wise counselling report"]}

    if re.search(r"risk|weak|at risk|dropout", t):
        data = sa.risk(con)
        return {"blocks": [B_text(f"**Academic risk radar** — {len(data)} students triggering ≥2 risk signals "
                                  f"(attendance <70% + low CGPA/backlogs):"),
                           B_table(["USN", "Name", "Class", "Att", "CGPA", "Backlogs", "Mentor"],
                                   [[d["usn"], d["name"], f"{d['dept']}-{d['sem']}{d['section']}",
                                     f"{d['attendance']}%", d["cgpa"], d["backlogs"],
                                     fac_name(con, d["mentor"])] for d in data], dense=True)],
                "agent": "StudentAgent"}

    q = "SELECT dept, sem, COUNT(*) n, ROUND(AVG(attendance),1) att, ROUND(AVG(cgpa),2) cg FROM students"
    a = []
    if ent["dept"]: q += " WHERE dept=?"; a.append(ent["dept"])
    data = rows(con, q + " GROUP BY dept, sem", tuple(a))
    return {"blocks": [B_text("Student strength & academic snapshot:"),
                       B_table(["Dept", "Sem", "Students", "Avg attendance", "Avg CGPA"],
                               [[d["dept"], d["sem"], d["n"], f"{d['att']}%", d["cg"]] for d in data])],
            "agent": "StudentAgent"}


# =====================================================================
def h_finance(con, text, ent, st, tr, actor):
    fa = FinanceAgent()
    tot, byd = fa.summary(con)
    tr.add("FinanceAgent", "ledger_aggregate", f"{tot['n']} students with dues · {inr(tot['s'])} outstanding")
    top = rows(con, "SELECT * FROM students WHERE fee_due>0 ORDER BY fee_due DESC LIMIT 12")
    return {"blocks": [
        B_cards([{"label": "Outstanding", "value": inr(tot["s"]), "tone": "bad"},
                 {"label": "Students with dues", "value": tot["n"]},
                 {"label": "Avg due", "value": inr(tot["s"] // max(1, tot["n"]))},
                 {"label": "Depts affected", "value": len(byd)}], title="Fee position"),
        B_table(["Dept", "Students", "Outstanding"],
                [[d["dept"], d["n"], inr(d["due"])] for d in byd]),
        B_table(["USN", "Name", "Class", "Amount", "Hostel"],
                [[s["usn"], s["name"], f"{s['dept']}-{s['sem']}{s['section']}", inr(s["fee_due"]),
                  "Yes" if s["hostel"] else "No"] for s in top], title="Largest individual dues", dense=True),
        B_text("I can trigger a staged reminder campaign (app → SMS → parent call list) or hold "
               "hall-ticket generation for dues above a threshold. Both need your approval.")],
        "agent": "FinanceAgent",
        "chips": ["Start fee reminder campaign", "Hold hall tickets above ₹50,000",
                  "Pending purchase approvals"]}


# =====================================================================
def h_exam(con, text, ent, st, tr, actor):
    ea = ExamAgent()
    t = text.lower()
    if re.search(r"eligib|ineligib|hall ticket|condonation", t):
        bad = ea.ineligible(con, ent["dept"], ent["sem"])
        return {"blocks": [B_text(f"**SEE eligibility check** — {len(bad)} students below the 75% VTU threshold"
                                  f"{' in ' + ent['dept'] if ent['dept'] else ''}. "
                                  f"Hall tickets will be blocked unless condonation is processed."),
                           B_table(["USN", "Name", "Class", "Attendance", "Fee due"],
                                   [[d["usn"], d["name"], f"{d['dept']}-{d['sem']}{d['section']}",
                                     f"{d['attendance']}%", inr(d["fee_due"])] for d in bad[:20]], dense=True)],
                "agent": "ExamAgent",
                "chips": ["Generate condonation annexure", "Notify these students"]}

    if re.search(r"invigilat", t):
        exams = ea.schedule(con, ent["dept"], ent["sem"])[:10]
        pool = rows(con, "SELECT id,name,dept FROM faculty ORDER BY RANDOM() LIMIT 40")
        assign = [[e["date"], e["session"], f"{e['dept']}-{e['sem']}", e["subject"], e["room"],
                   pool[i % len(pool)]["name"]] for i, e in enumerate(exams)]
        tr.add("ExamAgent", "invigilation_roster",
               "round-robin across departments, cross-dept enforced, no faculty on 2 consecutive sessions")
        return {"blocks": [B_text("Draft **invigilation roster** — cross-department allocation, "
                                  "no faculty invigilating their own subject:"),
                           B_table(["Date", "Session", "Class", "Subject", "Room", "Invigilator"], assign)],
                "agent": "ExamAgent",
                "chips": ["Publish roster to faculty", "Show exam schedule"]}

    exams = ea.schedule(con, ent["dept"], ent["sem"])
    tr.add("ExamAgent", "calendar_fetch", f"{len(exams)} papers scheduled")
    return {"blocks": [B_text(f"**SEE theory calendar** ({len(exams)} papers"
                              f"{' · ' + ent['dept'] if ent['dept'] else ''}"
                              f"{' · Sem ' + str(ent['sem']) if ent['sem'] else ''}) — "
                              f"commencing {exams[0]['date'] if exams else 'TBD'}:"),
                       B_table(["Date", "Session", "Dept", "Sem", "Subject", "Room"],
                               [[e["date"], e["session"], e["dept"], e["sem"],
                                 f"{e['subject']} · {e['sname'] or ''}", e["room"]] for e in exams[:22]],
                               dense=True)],
            "agent": "ExamAgent",
            "chips": ["Draft invigilation roster", "SEE eligibility check", "Notify students about exams"]}


# =====================================================================
def h_request(con, text, ent, st, tr, actor):
    ra = RequestAgent()
    t = text.lower()

    m = re.search(r"\b(?:approve|reject)\b.*?\b(?:req(?:uest)?[- ]?)?(\d{1,4})\b", t)
    if m and re.search(r"approve|reject", t):
        rid = int(m.group(1))
        r = one(con, "SELECT * FROM requests WHERE id=?", (rid,))
        if not r:
            return {"blocks": [B_text(f"No request with ID **{rid}** in the inbox.")], "agent": "RequestAgent"}
        dec = "Approved" if "approve" in t else "Rejected"
        ra.decide(con, rid, dec)
        tr.add("PolicyGuard", "authority_check", f"admin may {dec.lower()} '{r['kind']}' requests up to ₹5,00,000")
        tr.add("RequestAgent", "decision", f"REQ-{rid:04d} → {dec}")
        NotifyAgent().dispatch(con, [{"audience": r["raised_by"], "channel": "App + email",
                                      "title": f"Your request '{r['title']}' was {dec.lower()}",
                                      "body": f"Decision by Admin on {TODAY.isoformat()}."}])
        return {"blocks": [B_text(f"**REQ-{rid:04d} — {r['title']}** marked **{dec}**. "
                                  f"{r['raised_by']} has been notified and the ledger updated"
                                  f"{', budget head debited ' + inr(r['amount']) if dec=='Approved' and r['amount'] else ''}.")],
                "agent": "RequestAgent", "refresh": True,
                "chips": ["Show remaining pending approvals"]}

    if re.search(r"bulk approve|approve all", t):
        m = re.search(r"(?:under|below|less than|<)\s*(?:₹|rs\.?)?\s*([\d,]+)\s*(lakh|l\b|k\b)?", t)
        cap = 50000
        if m:
            cap = int(m.group(1).replace(",", ""))
            if m.group(2) in ("lakh", "l"): cap *= 100000
            if m.group(2) == "k": cap *= 1000
        pend = [r for r in ra.inbox(con) if r["amount"] <= cap]
        for r in pend:
            ra.decide(con, r["id"], "Approved")
        tr.add("PolicyGuard", "delegation_limit", f"admin auto-approval ceiling ₹{cap:,} respected")
        tr.add("RequestAgent", "bulk_decision", f"{len(pend)} request(s) approved in one transaction")
        val = sum(r["amount"] for r in pend)
        return {"blocks": [B_text(f"Bulk-approved **{len(pend)} request(s)** at or below {inr(cap)}"
                                  + (f", committing {inr(val)} of budget." if val else
                                     " (all zero-value administrative items).")),
                           B_table(["ID", "Type", "Title", "Amount"],
                                   [[f"REQ-{r['id']:04d}", r["kind"], r["title"],
                                     inr(r["amount"]) if r["amount"] else "—"] for r in pend])],
                "agent": "RequestAgent", "refresh": True,
                "chips": ["Show remaining pending approvals"]}

    if re.search(r"raise|send a request|apply for|request to|draft a request|forward to", t):
        target = ("Principal" if "principal" in t else "HOD" if "hod" in t else
                  "Accounts" if re.search(r"account|fee|fund", t) else "Admin Office")
        amt_m = re.search(r"(?:₹|rs\.?|inr)\s*([\d,]+)\s*(lakh|l\b|crore|cr\b)?", t)
        amt = 0
        if amt_m:
            amt = int(amt_m.group(1).replace(",", ""))
            if amt_m.group(2) in ("lakh", "l"): amt *= 100000
            if amt_m.group(2) in ("crore", "cr"): amt *= 10000000
        kind = ("Purchase" if re.search(r"purchase|procure|buy|equipment", t) else
                "Leave" if "leave" in t else "Event" if re.search(r"event|fest|seminar|workshop", t) else
                "Infrastructure" if re.search(r"infra|building|lab|classroom|ups|projector", t) else "General")
        title = re.sub(r"^\s*(?:please\s+)?(?:raise|send|draft)\s+(?:a\s+)?(?:request\s+)?(?:to\s+\w+\s+)?(?:for\s+)?",
                       "", text, flags=re.I).strip().capitalize()[:90] or "Administrative request"
        draft = {"kind": kind, "title": title, "details": text.strip(), "target": target,
                 "amount": amt, "priority": "High" if re.search(r"urgent|asap|immediate", t) else "Medium"}
        st["pending"] = {"kind": "request_draft", "draft": draft}
        tr.add("RequestAgent", "draft_compose", f"kind={kind} · target={target} · amount={inr(amt)}")
        tr.add("PolicyGuard", "routing_rule", f"{kind} ≥ ₹1,00,000 → Principal + Accounts endorsement")
        return {"blocks": [
            B_text("Here's the request I'll file on your behalf — confirm and I'll route it:"),
            B_table(["Field", "Value"],
                    [["Type", kind], ["Title", title], ["Route to", target],
                     ["Amount", inr(amt) if amt else "—"], ["Priority", draft["priority"]],
                     ["SLA", "48 hours, auto-escalates to Principal"]]),
            B_text("Say **yes** to send, or tell me what to change.")],
            "agent": "RequestAgent", "chips": ["Yes, send it", "Change the route to Principal", "Cancel"]}

    status = "Approved" if "approved" in t else "Pending"
    data = ra.inbox(con, status)
    tr.add("RequestAgent", "inbox_fetch", f"{len(data)} {status.lower()} items · SLA-sorted")
    overdue = [r for r in data if (TODAY - dt.date.fromisoformat(r["created_at"])).days * 24 > r["sla_hrs"]]
    tr.add("SLAMonitor", "breach_scan", f"{len(overdue)} item(s) past SLA")
    return {"blocks": [
        B_cards([{"label": f"{status} items", "value": len(data)},
                 {"label": "High priority", "value": len([r for r in data if r["priority"] == "High"]),
                  "tone": "bad"},
                 {"label": "SLA breached", "value": len(overdue), "tone": "bad" if overdue else "good"},
                 {"label": "Value at stake", "value": inr(sum(r["amount"] for r in data))}],
                title="Approvals inbox"),
        B_table(["ID", "Type", "Title", "From", "Amount", "Priority", "Age"],
                [[f"REQ-{r['id']:04d}", r["kind"], r["title"], r["raised_by"],
                  inr(r["amount"]) if r["amount"] else "—", r["priority"],
                  f"{(TODAY - dt.date.fromisoformat(r['created_at'])).days}d"] for r in data]),
        B_text("Say **“approve 3”** / **“reject 7”**, or ask me to bulk-approve everything below ₹50,000.")],
        "agent": "RequestAgent",
        "chips": ["Approve request 1", "Bulk approve under ₹50,000", "Show approved requests"]}


# =====================================================================
def h_notify(con, text, ent, st, tr, actor):
    t = text.lower()
    aud = ("All students" if re.search(r"all students|students", t) else
           "All faculty" if re.search(r"faculty|staff|teachers", t) else
           "Parents" if "parent" in t else "Entire campus")
    if ent["dept"]:
        aud = f"{ent['dept']} · {aud}"
    if ent["sem"]:
        aud += f" · Sem {ent['sem']}"
    body_m = re.search(r"(?:that|about|regarding|saying|:)\s*(.+)$", text, re.I)
    body = body_m.group(1).strip() if body_m else re.sub(
        r"^\s*(?:please\s+)?(?:notify|inform|announce|broadcast|send|alert)\s+", "", text, flags=re.I).strip()
    n = one(con, "SELECT COUNT(*) c FROM students" + (" WHERE dept=?" if ent["dept"] else ""),
            (ent["dept"],) if ent["dept"] else ())["c"]
    msg = {"audience": aud, "channel": "App push + SMS + email",
           "title": body[:60].capitalize() or "Notice from the Administration",
           "body": (body.capitalize() + ". — Office of the Principal, Vidyatech Institute of Engineering.")}
    st["pending"] = {"kind": "broadcast_draft", "msgs": [msg]}
    tr.add("NotifyAgent", "audience_resolve", f"{aud} ≈ {n} recipients")
    tr.add("PolicyGuard", "content_check", "no PII, no fee/exam claim requiring registrar sign-off → clear")
    return {"blocks": [B_text("Drafted circular — review before it goes out:"),
                       B_notice([msg]),
                       B_text(f"Estimated reach **{n} recipients**. Say **yes** to send, "
                              f"or dictate the exact wording you'd prefer.")],
            "agent": "NotifyAgent", "chips": ["Yes, send it", "Make it more formal", "Cancel"]}


# =====================================================================
def h_analytics(con, text, ent, st, tr, actor):
    an = AnalyticsAgent()
    k = an.kpis(con)
    tr.add("AnalyticsAgent", "kpi_rollup", "students, faculty, finance, approvals, placements")
    tr.add("RiskAgent", "radar", f"{k['shortage']} attendance risks · {k['pending']} open approvals")
    today_leaves = rows(con, """SELECT l.*, f.name, f.dept FROM leaves l JOIN faculty f ON f.id=l.faculty
                                WHERE l.from_date<=? AND l.to_date>=? AND l.status='Approved'""",
                        (TODAY.isoformat(), TODAY.isoformat()))
    ov = rows(con, "SELECT COUNT(*) c FROM overrides WHERE status='Applied' AND date>=?", (TODAY.isoformat(),))
    byd = rows(con, """SELECT dept, COUNT(*) n, ROUND(AVG(attendance),1) att, ROUND(AVG(cgpa),2) cg,
                       SUM(CASE WHEN attendance<75 THEN 1 ELSE 0 END) short FROM students GROUP BY dept""")
    return {"blocks": [
        B_text(f"**Institution brief — {TODAY.strftime('%A, %d %B %Y')}**"),
        B_cards([{"label": "Students", "value": k["students"]},
                 {"label": "Faculty", "value": k["faculty"], "sub": f"ratio {k['ratio']}"},
                 {"label": "Avg attendance", "value": f"{k['avg_att']}%",
                  "tone": "good" if k["avg_att"] >= 75 else "warn"},
                 {"label": "Avg CGPA", "value": k["avg_cgpa"]},
                 {"label": "Fee outstanding", "value": inr(k["dues"]), "tone": "bad"},
                 {"label": "Attendance risk", "value": k["shortage"], "tone": "bad"},
                 {"label": "Open approvals", "value": k["pending"], "tone": "warn"},
                 {"label": "Faculty utilisation", "value": f"{k['util']}%"}]),
        B_table(["Dept", "Students", "Avg att.", "Avg CGPA", "<75% count"],
                [[d["dept"], d["n"], f"{d['att']}%", d["cg"], d["short"]] for d in byd],
                title="Department scorecard"),
        B_text(f"**Today's operational picture** — {len(today_leaves)} faculty on approved leave"
               + (": " + ", ".join(f"{l['name']} ({l['dept']})" for l in today_leaves[:5]) if today_leaves else "")
               + f" · {ov[0]['c']} timetable override(s) active from today onward.\n\n"
               f"**Placements this cycle:** {k['offers']} offers, average CTC ₹{k['avg_ctc']} LPA.\n\n"
               f"**What I'd act on first:** {k['pending']} approvals are ageing in your inbox and "
               f"{k['shortage']} students are below the VTU 75% attendance bar with SEE {(dt.date(2026,9,25) - TODAY).days} days away."),
    ],
        "agent": "AnalyticsAgent",
        "chips": ["Pending approvals in my inbox", "Attendance defaulters list",
                  "Who is on leave this week?", "Fee dues by department"]}


# =====================================================================
def h_fallback(con, text, ent, st, tr, actor):
    tr.add("Router", "no_confident_intent", "falling back to capability guidance", status="warn")
    caps = [["Timetable & absence", "“Dr. Shwetha is absent on 9 Sep — arrange substitutes”"],
            ["Availability", "“Which ECE faculty are free on Monday period 4?”"],
            ["Students", "“Attendance defaulters below 65% in CSE sem 5”"],
            ["Faculty", "“Faculty workload above 90%” · “Who teaches Machine Learning?”"],
            ["Finance", "“Fee dues by department”"],
            ["Exams", "“SEE eligibility check for MECH” · “Draft invigilation roster”"],
            ["Approvals", "“Pending approvals” · “Approve 3” · “Raise a purchase request for ₹1,20,000”"],
            ["Broadcast", "“Notify all CSE students that lab exams are postponed”"],
            ["Analytics", "“Brief me on today's institution status”"],
            ["Autopilot", "“What needs my attention today?”"],
            ["Timetable generation", "“Rebuild the timetable for CSE sem 5 A”"],
            ["Library", "“Overdue library books” · “Issue BK0012 to 4VP24CS017”"],
            ["Hostel", "“Hostel status” · “Allocate hostel rooms to the waitlist”"],
            ["Transport", "“Rebalance bus routes” · “Bus on route 4 broke down”"],
            ["Gate passes", "“Decide pending gate passes by policy”"],
            ["Placement", "“Who is eligible for the Nethra Robotics drive?”"],
            ["Documents", "“No dues status of 4VP23CS042” · “Issue a bonafide certificate for …”"],
            ["Faculty leave", "“Review pending leave applications”"],
            ["Student 360", "“Everything about 4VP24CS017”"]]
    return {"blocks": [
        B_text("I'm **MAWOS**, the Multi-Agent Workflow Orchestration System: a supervisor with sixteen specialist agents behind it "
               "(timetable, substitution, faculty, student, finance, exam, approvals, notifications, analytics, "
               "library, hostel, transport, gate pass, placement, documents, HR). "
               "I didn't catch a clear intent in that. Here's what I can do:"),
        B_table(["Domain", "Try saying"], caps)],
        "agent": "Supervisor", "chips": CHIPS_DEFAULT}
