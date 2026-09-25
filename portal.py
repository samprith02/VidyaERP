"""
VidyaERP :: self-service for students, faculty and HODs (#27)

Tools that act for the SIGNED-IN person, read from S["user"] - never from an
argument, so nobody can file a gate pass "as" someone else. Which of these a
role may call at all is access.py's decision, enforced by tools.execute.

The three writes - request_gate_pass, request_certificate, apply_leave - use
the campus shape: without approval in the person's own turn they PROPOSE
(writing nothing), with it they commit and re-read. Each carries an agent's
pre-check, which is the point: a student learns before filing that the pass
overlaps class hours; a teacher applies for leave with the timetable impact
already worked out, so the HOD decides with it in front of them.
"""
import re, datetime as dt
import db, nlu, campus_data as cd
from nlu import TODAY, day_of
from guard import approved_this_turn
from agents import (rows, one, fac_name, subj_name, NotifyAgent, Auditor, ensure_makeups,
                    B_text, B_table, B_cards, B_checklist, B_bars)
from campus import (_propose, _done, _err, _stu, _cls, _when, inr, GatePassAgent, DocumentAgent,
                    LeaveAgent, PlacementAgent, LibraryAgent, VERDICT_MD, CERT_KINDS, cert_kind, NOW)

GP_KINDS = {"outing": "Outing", "home": "Home visit", "medical": "Medical", "emergency": "Emergency",
            "early": "Early leave"}
LEAVE_KINDS = ["Casual Leave", "Medical Leave", "On Duty (Conference)", "Earned Leave"]


def _staged(S, name, args):
    """Was THIS proposal - same tool, same normalised arguments - already shown
    to the person and staged in their session?

    A self-service write commits only on an approving turn that comes AFTER its
    proposal. Approval words alone are not enough here: people ask for leave by
    saying "apply for leave", and "apply" is an approval word - so without this,
    the request sentence itself would count as the confirmation and the leave
    would be filed before the timetable impact was ever shown. Found by driving
    the portal in a browser (#27). The Registrar's tools keep the older rule on
    purpose: "approve 3" is an explicit instruction, not a request."""
    p = (S or {}).get("pending") or {}
    return (p.get("kind") == "tool_commit" and p.get("tool") == name
            and p.get("args") == {k: v for k, v in args.items() if v is not None})


def _me(S, *roles):
    P = (S or {}).get("user")
    if not P or P.get("role") not in roles:
        return None, _err(f"This is for {' / '.join(roles)} accounts — sign in as one.")
    return P, None


def _when_parse(text, default_hm, base=None):
    """ISO ('2026-09-05T14:00', '2026-09-05 2pm') or natural ('tomorrow 2pm',
    'sat 14:00', '12 sep 5:30 pm'). An ISO date is taken literally - handing
    '2026-09-05' to the natural-language parser reads it as 9 May."""
    s = str(text or "").strip()
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        pass
    iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", s)
    if iso:
        d, rest = dt.date.fromisoformat(iso.group(1)), s.replace(iso.group(1), " ")
    else:
        # a bare weekday ('sat 2pm') only parses with 'on' in front of it
        d, rest = nlu.parse_date(s)[0] or nlu.parse_date("on " + s)[0], s
    d = d or base or TODAY
    h, mi = map(int, default_hm.split(":"))
    m = (re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", rest.lower())
         or re.search(r"\b(\d{1,2}):(\d{2})\b()", rest.lower()))
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3) == "pm" and h < 12:
            h += 12
        if m.group(3) == "am" and h == 12:
            h = 0
    return dt.datetime.combine(d, dt.time(min(h, 23), min(mi, 59)))


# ================================================================= home
def _makeups_block(con, where, args, teacher=True):
    """Upcoming booked make-up classes (#18), for a class or for a teacher."""
    ensure_makeups(con)
    ms = rows(con, f"""SELECT m.*, sb.name sname FROM makeup_sessions m LEFT JOIN subjects sb ON sb.code=m.subject
                      WHERE m.status='Scheduled' AND m.date>=? AND m.{where.replace(' AND ', ' AND m.')}
                      ORDER BY m.date, m.period""", (TODAY.isoformat(), *args))
    if not ms:
        return []
    return [B_table(["When", "Subject", "Class" if not teacher else "Teacher", "Room"],
                    [[f"{m['date']} · {m['day']} P{m['period']}", f"{m['subject']} · {m['sname'] or ''}",
                      fac_name(con, m["faculty"]) if teacher else f"{m['dept']}-{m['sem']}{m['section']}", m["room"]]
                     for m in ms], title="Make-up classes booked", dense=True)]


def t_my_home(con, S, U, **kw):
    P = (S or {}).get("user")
    if not P or P.get("role") == "admin":
        return _err("The Registrar's home is the console.")
    dy = day_of(TODAY)
    if P["role"] == "student":
        s = _stu(con, P["usn"])
        today = rows(con, """SELECT t.*, sb.name sname FROM timetable t LEFT JOIN subjects sb ON sb.code=t.subject
                             WHERE t.dept=? AND t.sem=? AND t.section=? AND t.day=? ORDER BY t.period""",
                     (s["dept"], s["sem"], s["section"], dy))
        att = rows(con, """SELECT a.*, sb.name sname FROM attendance a JOIN subjects sb ON sb.code=a.subject
                           WHERE a.usn=? ORDER BY a.subject""", (s["usn"],))
        loans = LibraryAgent().active(con, s["usn"])
        passes = rows(con, "SELECT * FROM gate_passes WHERE usn=? AND status='Pending'", (s["usn"],))
        nxt = one(con, "SELECT MIN(date) d FROM exams WHERE dept=? AND sem=?", (s["dept"], s["sem"]))["d"]
        short = [a for a in att if a["held"] and 100 * a["attended"] / a["held"] < 75]
        return {"data": {"attendance": s["attendance"], "cgpa": s["cgpa"], "fee_due": s["fee_due"],
                         "classes_today": len(today), "subjects_below_75": [a["subject"] for a in short],
                         "books_on_loan": len(loans), "next_exam": nxt},
                "blocks": [B_cards([{"label": "Attendance", "value": f"{s['attendance']}%",
                                     "tone": "good" if s["attendance"] >= 75 else "bad",
                                     "sub": f"{len(short)} subject(s) below 75%" if short else "all subjects ≥75%"},
                                    {"label": "CGPA", "value": s["cgpa"]},
                                    {"label": "Fee due", "value": inr(s["fee_due"]), "tone": "bad" if s["fee_due"] else "good"},
                                    {"label": "Next exam", "value": nxt or "—", "sub": "SEE begins"}],
                                   title=f"Good morning, {s['name'].split()[0]} · {_cls(s)}"),
                           B_table(["Period", "Time", "Subject", "Faculty", "Room"],
                                   [[f"P{r['period']}", db.PERIODS[r["period"]], f"{r['subject']} · {r['sname'] or ''}",
                                     fac_name(con, r["faculty"]) if r["faculty"] else "Class mentor", r["room"]]
                                    for r in today], title=f"Today · {TODAY.strftime('%A %d %b')}", dense=True),
                           B_bars([{"label": f"{a['subject']} {a['sname']}", "value": a["attended"], "max": a["held"]}
                                   for a in att], title="Attendance by subject (hours)", unit="hrs")]
                          + ([B_table(["Book", "Due", "Late by"], [[l["title"], l["due_on"], f"{LibraryAgent.days_over(l)} d"]
                                                                   for l in loans], title="Library books with you", dense=True)]
                             if loans else [])
                          + _makeups_block(con, "dept=? AND sem=? AND section=?", (s["dept"], s["sem"], s["section"]))
                          + ([B_text(f"{len(passes)} gate pass request(s) waiting for the warden.")] if passes else []),
                "trace": [("StudentAgent", "home", s["usn"]), ("AttendanceAgent", "subject_rollup", f"{len(short)} below 75%"),
                          ("TimetableAgent", "today", f"{len(today)} periods"), ("LibraryAgent", "member_loans", str(len(loans)))],
                "chips": ["Request a gate pass for tomorrow 2pm to 7pm", "Request a bonafide certificate for passport",
                          "Am I eligible for any placement drive?", "My requests"]}
    f = one(con, "SELECT * FROM faculty WHERE id=?", (P["fid"],))
    today = rows(con, """SELECT t.*, sb.name sname FROM timetable t LEFT JOIN subjects sb ON sb.code=t.subject
                         WHERE t.faculty=? AND t.day=? ORDER BY t.period""", (f["id"], dy))
    load = one(con, "SELECT COUNT(*) n FROM timetable WHERE faculty=?", (f["id"],))["n"]
    mentees = rows(con, "SELECT * FROM students WHERE mentor=?", (f["id"],))
    risk = [m for m in mentees if m["attendance"] < 75 or m["backlogs"]]
    my_lv = rows(con, "SELECT * FROM leaves WHERE faculty=? AND to_date>=? ORDER BY from_date", (f["id"], TODAY.isoformat()))
    cards = [{"label": "Classes today", "value": len(today)},
             {"label": "Weekly load", "value": f"{load}/{f['max_load']}"},
             {"label": "Mentees at risk", "value": len(risk), "tone": "bad" if risk else "good", "sub": f"of {len(mentees)}"},
             {"label": "Upcoming leave", "value": len(my_lv), "sub": ", ".join(l["status"] for l in my_lv) or "none"}]
    blocks = []
    if P["role"] == "hod":
        pend = one(con, """SELECT COUNT(*) n FROM leaves l JOIN faculty x ON x.id=l.faculty
                           WHERE l.status='Pending' AND x.dept=?""", (f["dept"],))["n"]
        cards.append({"label": f"{f['dept']} leave to decide", "value": pend, "tone": "warn" if pend else "good"})
    blocks.append(B_cards(cards, title=f"Good morning, {f['name']} · {f['dept']}"
                                        + (" · Head of Department" if P["role"] == "hod" else "")))
    blocks.append(B_table(["Period", "Time", "Class", "Subject", "Room"],
                          [[f"P{r['period']}", db.PERIODS[r["period"]], f"{r['dept']}-{r['sem']}{r['section']}",
                            f"{r['subject']} · {r['sname'] or ''}", r["room"]] for r in today],
                          title=f"Today · {TODAY.strftime('%A %d %b')}", dense=True) if today
                  else B_text(f"No classes on {TODAY.strftime('%A')}."))
    blocks += _makeups_block(con, "faculty=?", (f["id"],), teacher=False)
    if risk:
        blocks.append(B_table(["USN", "Name", "Class", "Attendance", "Backlogs"],
                              [[m["usn"], m["name"], _cls(m), f"{m['attendance']}%", m["backlogs"]] for m in risk[:8]],
                              title="Mentees who need a conversation", dense=True))
    return {"data": {"classes_today": len(today), "load": load, "mentees_at_risk": len(risk)},
            "blocks": blocks,
            "trace": [("FacultyAgent", "home", f["id"]), ("TimetableAgent", "today", f"{len(today)} periods"),
                      ("StudentAgent", "mentees", f"{len(risk)}/{len(mentees)} at risk")],
            "chips": (["Department overview", "Pending leave applications"] if P["role"] == "hod" else [])
                     + ["Apply for casual leave on 10 sep for a family function", "My mentees", "My timetable"]}


# =============================================================== student
def t_my_placement(con, S, U, **kw):
    P, e = _me(S, "student")
    if e:
        return e
    s = _stu(con, P["usn"])
    if s["sem"] != 7:
        return {"data": {"note": "Placement drives open in the 7th semester."},
                "blocks": [B_text(f"Placement drives open in the 7th semester — you are in semester {s['sem']}. "
                                  f"Your CGPA is **{s['cgpa']}**; most drives ask for 6.5–8.0 with no backlogs.")],
                "trace": [("PlacementAgent", "not_yet", f"sem {s['sem']}")]}
    pa = PlacementAgent()
    out = []
    for d in pa.drives(con):
        ok, _w = pa.eligibility(con, d)
        me = next((x for x in ok if x["usn"] == s["usn"]), None)
        reg = one(con, "SELECT status FROM drive_registrations WHERE drive_id=? AND usn=?", (d["id"], s["usn"]))
        why = []
        if s["dept"] not in d["depts"].split(","):
            why.append(f"open to {d['depts']} only")
        if s["cgpa"] < d["min_cgpa"]:
            why.append(f"needs CGPA {d['min_cgpa']}")
        if s["backlogs"] > d["max_backlogs"]:
            why.append(f"allows ≤{d['max_backlogs']} backlog(s)")
        if not me and not why:
            why.append("one-offer rule: you already hold an offer")
        out.append({"drive": d, "ok": bool(me), "why": why, "reg": reg["status"] if reg else None})
    offers = rows(con, "SELECT * FROM student_offers WHERE usn=?", (s["usn"],))
    return {"data": {"eligible_for": [o["drive"]["company"] for o in out if o["ok"]],
                     "offers": [o["company"] for o in offers]},
            "blocks": ([B_text("Offers: " + ", ".join(f"**{o['company']}** ₹{o['ctc']} LPA" for o in offers))] if offers else [])
                      + [B_table(["Company", "Role", "CTC", "Date", "You", "Why / status"],
                                 [[o["drive"]["company"], o["drive"]["role"], f"₹{o['drive']['ctc']} LPA", o["drive"]["drive_date"],
                                   "**Eligible**" if o["ok"] else "—",
                                   (o["reg"] or "shortlist not published yet") if o["ok"] else "; ".join(o["why"])]
                                  for o in out], title="Upcoming drives and you")],
            "trace": [("PlacementAgent", "self_eligibility", f"{sum(o['ok'] for o in out)}/{len(out)} drives")]}


def t_request_gate_pass(con, S, U, kind="Outing", out_at="", return_by="", reason="", parent_consent=False, **kw):
    P, e = _me(S, "student")
    if e:
        return e
    s = _stu(con, P["usn"])
    k = next((v for key, v in GP_KINDS.items() if key in str(kind).lower()), "Outing")
    out = _when_parse(out_at, "14:00")
    if return_by:
        ret = _when_parse(return_by, "19:30", base=out.date())
    elif k == "Early leave":
        ret = out                                   # leaving for the day, not coming back
    else:
        ret = max(dt.datetime.combine(out.date(), dt.time(19, 30)), out + dt.timedelta(hours=1))
    if ret < out:
        return _err("The return time is before the time you leave.")
    if out < NOW - dt.timedelta(hours=1):
        return _err("That time has already passed.")
    clash = one(con, """SELECT id FROM gate_passes WHERE usn=? AND status IN ('Pending','Approved')
                        AND out_at < ? AND return_by > ?""", (s["usn"], ret.isoformat(timespec="minutes"),
                                                                out.isoformat(timespec="minutes")))
    if clash:
        return _err(f"You already have gate pass #{clash['id']} for that time.")
    gp = {"usn": s["usn"], "kind": k, "out_at": out.isoformat(timespec="minutes"),
          "return_by": ret.isoformat(timespec="minutes"), "parent_consent": 1 if parent_consent else 0}
    verdict, why = GatePassAgent().evaluate(con, gp)
    tbl = B_table(["Field", "Value"], [["Type", k], ["Leave", _when(gp["out_at"])], ["Back by", _when(gp["return_by"])],
                                       ["Reason", reason or "—"], ["Parent consent", "yes" if parent_consent else "not stated"],
                                       ["Policy pre-check", f"{VERDICT_MD[verdict]} — {why[0]}"]],
                  title="Your gate pass request")
    tr = [("GatePassAgent", "pre_check", f"{verdict}: {why[0]}")]
    args = {"kind": k, "out_at": gp["out_at"], "return_by": gp["return_by"], "reason": reason,
            "parent_consent": bool(parent_consent)}
    if not approved_this_turn(U) or not _staged(S, "request_gate_pass", args):
        return _propose(S, "request_gate_pass", args, [tbl], f"file a {k.lower()} gate pass", tr)
    cur = con.execute("""INSERT INTO gate_passes(usn,kind,out_at,return_by,reason,parent_consent,status,requested_at,
                         decided_by,note) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (s["usn"], k, gp["out_at"], gp["return_by"], reason, gp["parent_consent"], "Pending",
                       NOW.isoformat(timespec="minutes"), None, None))
    con.commit()
    gid = cur.lastrowid
    ok = one(con, "SELECT 1 FROM gate_passes WHERE id=? AND usn=?", (gid, s["usn"]))
    NotifyAgent().dispatch(con, [{"audience": "Chief Warden" if s["hostel"] else f"Mentor · {fac_name(con, s['mentor'])}",
                                  "channel": "App", "title": f"Gate pass #{gid} requested by {s['usn']}",
                                  "body": f"{k} {_when(gp['out_at'])} → {_when(gp['return_by'])}. Pre-check: {verdict}."}])
    Auditor().log(con, s["usn"], "GatePassAgent", "gatepass.request", {"id": gid, "kind": k}, "Applied" if ok else "FAILED")
    _done(S, "request_gate_pass")
    return {"data": {"filed": bool(ok), "id": gid, "pre_check": verdict},
            "blocks": [tbl, B_text(f"Gate pass **#{gid}** filed. The warden decides; the policy pre-check says "
                                   f"**{verdict}**. You will get a notification either way.")], "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "the student confirmed in this turn"),
                           ("GatePassAgent", "verify", "filed" if ok else "MISSING", "ok" if ok else "error")]}


def t_request_certificate(con, S, U, kind="Bonafide", purpose="", **kw):
    P, e = _me(S, "student")
    if e:
        return e
    s = _stu(con, P["usn"])
    k = kind if kind in CERT_KINDS else cert_kind(kind)
    if not k:
        return _err(f"Unknown certificate '{kind}'. You can ask for: {', '.join(CERT_KINDS)}.")
    req_kind = CERT_KINDS[k][1]
    dup = one(con, "SELECT id FROM requests WHERE raised_by=? AND kind=? AND status='Pending'", (s["usn"], req_kind))
    if dup:
        return _err(f"You already asked for this — REQ-{dup['id']:04d} is with the Registrar.")
    checks = DocumentAgent().eligibility(con, s, k)
    tr = [("DocumentAgent", "pre_check", " · ".join(f"{c['label']}={'ok' if c['ok'] else 'FAIL'}" for c in checks))]
    if not all(c["ok"] for c in checks):
        return {"data": {"error": f"{k} cannot be issued yet — " + "; ".join(
                    f"{c['label']}: {c['detail']}" for c in checks if not c["ok"]), "checks": checks},
                "blocks": [B_checklist(checks, title=f"{k} — what is blocking it")], "trace": tr}
    title = f"{k} certificate for {(purpose or 'personal use').strip()}"
    args = {"kind": k, "purpose": purpose}
    if not approved_this_turn(U) or not _staged(S, "request_certificate", args):
        return _propose(S, "request_certificate", args,
                        [B_checklist(checks, title=f"{k} — pre-check passed"),
                         B_table(["Field", "Value"], [["Certificate", k], ["Purpose", purpose or "personal use"],
                                                      ["Goes to", "Registrar · 24 h service level"]])],
                        f"request a {k} certificate", tr)
    cur = con.execute("""INSERT INTO requests(kind,raised_by,role,target,title,details,amount,status,priority,created_at,sla_hrs)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (req_kind, s["usn"], "Student", "Admin", title, f"Requested via the student portal ({_cls(s)})",
                       0, "Pending", "Medium", TODAY.isoformat(), 24))
    con.commit()
    rid = cur.lastrowid
    Auditor().log(con, s["usn"], "DocumentAgent", "certificate.request", {"id": rid, "kind": k}, "Applied")
    _done(S, "request_certificate")
    return {"data": {"filed": True, "request": rid},
            "blocks": [B_text(f"**REQ-{rid:04d}** — {title} — is with the Registrar. The DocumentAgent has already "
                              f"checked you are eligible, so it should be a one-click issue on their side.")],
            "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "the student confirmed in this turn"),
                           ("RequestAgent", "route", f"REQ-{rid:04d} → Registrar")]}


def t_my_requests(con, S, U, **kw):
    P = (S or {}).get("user")
    if not P or P.get("role") == "admin":
        return _err("For student and staff accounts.")
    me = P.get("usn") or P.get("fid")
    reqs = rows(con, "SELECT * FROM requests WHERE raised_by=? ORDER BY id DESC", (me,))
    blocks = [B_table(["Request", "Type", "Title", "Status", "Filed"],
                      [[f"REQ-{r['id']:04d}", r["kind"], r["title"], r["status"], r["created_at"]] for r in reqs],
                      title="My requests", dense=True) if reqs else B_text("No requests filed.")]
    if P["role"] == "student":
        gp = rows(con, "SELECT * FROM gate_passes WHERE usn=? ORDER BY id DESC LIMIT 10", (me,))
        certs = rows(con, "SELECT * FROM certificates WHERE usn=? ORDER BY id DESC", (me,))
        blocks.append(B_table(["Pass", "Type", "Out", "Back by", "Status", "Note"],
                              [[g["id"], g["kind"], _when(g["out_at"]), _when(g["return_by"]), g["status"], g["note"] or "—"]
                               for g in gp], title="My gate passes", dense=True) if gp else B_text("No gate passes."))
        if certs:
            blocks.append(B_table(["Serial", "Certificate", "Issued"], [[c["serial"], c["kind"], c["issued_on"]] for c in certs],
                                  title="Certificates issued to me", dense=True))
    else:
        lv = rows(con, "SELECT * FROM leaves WHERE faculty=? ORDER BY from_date DESC LIMIT 10", (me,))
        blocks.append(B_table(["From", "To", "Type", "Status"], [[l["from_date"], l["to_date"], l["kind"], l["status"]] for l in lv],
                              title="My leave", dense=True) if lv else B_text("No leave on record."))
    return {"data": {"requests": [{"id": r["id"], "kind": r["kind"], "status": r["status"]} for r in reqs]},
            "blocks": blocks, "trace": [("RequestAgent", "mine", f"{len(reqs)} request(s)")]}


# =============================================================== faculty
def t_my_mentees(con, S, U, **kw):
    P, e = _me(S, "faculty", "hod")
    if e:
        return e
    ms = rows(con, "SELECT * FROM students WHERE mentor=? ORDER BY attendance", (P["fid"],))
    risk = [m for m in ms if m["attendance"] < 75 or m["backlogs"] or m["cgpa"] < 6.5]
    return {"data": {"mentees": len(ms), "at_risk": [m["usn"] for m in risk]},
            "blocks": [B_cards([{"label": "Mentees", "value": len(ms)},
                                {"label": "Below 75% attendance", "value": sum(1 for m in ms if m["attendance"] < 75), "tone": "bad"},
                                {"label": "With backlogs", "value": sum(1 for m in ms if m["backlogs"]), "tone": "warn"}],
                               title="My mentees"),
                       B_table(["USN", "Name", "Class", "Attendance", "CGPA", "Backlogs", "Fee due"],
                               [[m["usn"], m["name"], _cls(m), f"{m['attendance']}%", m["cgpa"], m["backlogs"],
                                 inr(m["fee_due"]) if m["fee_due"] else "—"] for m in ms], dense=True)],
            "trace": [("StudentAgent", "mentees", f"{len(risk)}/{len(ms)} need attention")]}


def t_my_leaves(con, S, U, **kw):
    P, e = _me(S, "faculty", "hod")
    if e:
        return e
    lv = rows(con, "SELECT * FROM leaves WHERE faculty=? ORDER BY from_date DESC", (P["fid"],))
    return {"data": {"leaves": [{"from": l["from_date"], "to": l["to_date"], "status": l["status"]} for l in lv]},
            "blocks": [B_table(["ID", "From", "To", "Type", "Reason", "Status"],
                               [[l["id"], l["from_date"], l["to_date"], l["kind"], l["reason"], l["status"]] for l in lv],
                               title="My leave", dense=True) if lv else B_text("No leave on record.")],
            "trace": [("HRAgent", "mine", f"{len(lv)} record(s)")]}


def t_apply_leave(con, S, U, from_date="", to_date="", kind="Casual Leave", reason="", **kw):
    P, e = _me(S, "faculty", "hod")
    if e:
        return e

    def day(v):
        try:
            return dt.date.fromisoformat(str(v))
        except ValueError:
            d, _l = nlu.parse_date(str(v))
            return d
    a = day(from_date)
    b = day(to_date) if to_date else a
    if not a:
        return _err("Which date? e.g. “casual leave on 10 Sep”.")
    if b < a:
        return _err("The leave ends before it starts.")
    if a < TODAY:
        return _err("That date has passed — speak to the HR office for a retroactive entry.")
    if (b - a).days > 30:
        return _err("More than 30 days — that goes through the HR office, not self-service.")
    k = next((x for x in LEAVE_KINDS if x.split()[0].lower() in str(kind).lower()), "Casual Leave")
    clash = one(con, """SELECT id, status FROM leaves WHERE faculty=? AND status IN ('Pending','Approved')
                        AND from_date<=? AND to_date>=?""", (P["fid"], b.isoformat(), a.isoformat()))
    if clash:
        return _err(f"You already have leave #{clash['id']} ({clash['status']}) overlapping those dates.")
    assess = LeaveAgent().assess(con, {"faculty": P["fid"], "from_date": a.isoformat(), "to_date": b.isoformat()})
    hod = one(con, "SELECT name FROM faculty WHERE dept=? AND is_hod=1", (P["dept"],))
    tbl = B_table(["Day", "Periods", "Best cover plan", "Coverage"],
                  [[d["date"], d["periods"], d["best_plan"] or "—", f"{d['coverage']}%"] for d in assess["days"]],
                  title=f"{k} · {a.isoformat()} → {b.isoformat()} — the timetable impact your HOD will see")
    tr = [("HRAgent", "assess", assess["why"])] + [("SubstitutionAgent", "what_if", f"{d['date']}: {d['periods']} period(s), "
                                                    f"cover {d['coverage']}%") for d in assess["days"] if d["periods"]]
    args = {"from_date": a.isoformat(), "to_date": b.isoformat(), "kind": k, "reason": reason}
    if not approved_this_turn(U) or not _staged(S, "apply_leave", args):
        return _propose(S, "apply_leave", args,
                        [tbl, B_text(f"Agent's read: **{assess['why']}**. It goes to **{hod['name'] if hod else 'your HOD'}** "
                                     f"with this attached.")], f"apply for {k.lower()} {a} → {b}", tr)
    cur = con.execute("""INSERT INTO leaves(faculty,from_date,to_date,kind,reason,status,applied_on)
                         VALUES(?,?,?,?,?,?,?)""", (P["fid"], a.isoformat(), b.isoformat(), k, reason or "—", "Pending",
                                                     TODAY.isoformat()))
    con.commit()
    lid = cur.lastrowid
    NotifyAgent().dispatch(con, [{"audience": f"HOD · {P['dept']}", "channel": "App + email",
                                  "title": f"Leave application #{lid} — {P['name']}",
                                  "body": f"{k} {a} → {b}. {assess['why']}."}])
    Auditor().log(con, P["fid"], "HRAgent", "leave.apply", {"id": lid, **args}, "Applied")
    _done(S, "apply_leave")
    return {"data": {"filed": True, "leave_id": lid, "assessment": assess["why"]},
            "blocks": [tbl, B_text(f"Leave **#{lid}** is with {hod['name'] if hod else 'your HOD'}, with the coverage "
                                   f"impact attached.")], "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "the teacher confirmed in this turn"),
                           ("HRAgent", "verify", f"leave #{lid} on record", "ok")]}


# ==================================================================== HOD
def t_dept_overview(con, S, U, **kw):
    P, e = _me(S, "hod")
    if e:
        return e
    d = P["dept"]
    st = one(con, """SELECT COUNT(*) n, ROUND(AVG(attendance),1) a, ROUND(AVG(cgpa),2) g,
                     SUM(CASE WHEN attendance<75 THEN 1 ELSE 0 END) s FROM students WHERE dept=?""", (d,))
    fac = rows(con, """SELECT f.id, f.name, f.max_load, (SELECT COUNT(*) FROM timetable t WHERE t.faculty=f.id) load
                       FROM faculty f WHERE f.dept=? ORDER BY load DESC""", (d,))
    pend = rows(con, """SELECT l.*, f.name FROM leaves l JOIN faculty f ON f.id=l.faculty
                        WHERE l.status='Pending' AND f.dept=?""", (d,))
    away = rows(con, """SELECT f.name FROM leaves l JOIN faculty f ON f.id=l.faculty WHERE l.status='Approved'
                        AND f.dept=? AND l.from_date<=? AND l.to_date>=?""", (d, TODAY.isoformat(), TODAY.isoformat()))
    return {"data": {"dept": d, "students": st["n"], "avg_attendance": st["a"], "below_75": st["s"],
                     "pending_leave": len(pend), "on_leave_today": [a["name"] for a in away]},
            "blocks": [B_cards([{"label": "Students", "value": st["n"]}, {"label": "Avg attendance", "value": f"{st['a']}%"},
                                {"label": "Below 75%", "value": st["s"], "tone": "bad"}, {"label": "Avg CGPA", "value": st["g"]},
                                {"label": "Leave to decide", "value": len(pend), "tone": "warn" if pend else "good"},
                                {"label": "Staff away today", "value": len(away)}], title=f"{db.DEPTS[d]}"),
                       B_bars([{"label": x["name"], "value": x["load"], "max": x["max_load"]} for x in fac],
                              title="Teaching load", unit="periods")],
            "trace": [("AnalyticsAgent", "dept_rollup", d), ("FacultyAgent", "workload", f"{len(fac)} staff"),
                      ("HRAgent", "pending", f"{len(pend)} leave")],
            "chips": ["Pending leave applications", "Attendance defaulters", "Faculty workload"]}


# ================================================================ registry
def _p(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_S, _B = {"type": "string"}, {"type": "boolean"}
REGISTRY = [
    (t_my_home, "my_home", "The signed-in student's or teacher's day: classes, attendance, dues, loans, mentees.", _p({})),
    (t_my_requests, "my_requests", "The signed-in person's own requests, gate passes, certificates or leave.", _p({})),
    (t_my_placement, "my_placement", "Student: which upcoming drives they are eligible for, and why not.", _p({})),
    (t_request_gate_pass, "request_gate_pass",
     "Student: file a gate pass (Outing / Home visit / Medical / Emergency / Early leave) with ISO or natural "
     "times; pre-checked against hostel policy. PROPOSES until the student confirms.",
     _p({"kind": _S, "out_at": _S, "return_by": _S, "reason": _S, "parent_consent": _B}, ["out_at"])),
    (t_request_certificate, "request_certificate",
     "Student: ask the Registrar for a certificate (Bonafide, Conduct, Fee Structure, No Dues, Transfer "
     "Certificate, Internship NOC); eligibility pre-checked. PROPOSES until the student confirms.",
     _p({"kind": _S, "purpose": _S}, ["kind"])),
    (t_my_mentees, "my_mentees", "Teacher: their mentees with attendance, CGPA and backlogs.", _p({})),
    (t_my_leaves, "my_leaves", "Teacher: their own leave history.", _p({})),
    (t_apply_leave, "apply_leave",
     "Teacher: apply for leave; the timetable impact and best coverage plan are attached for the HOD. "
     "PROPOSES until the teacher confirms.", _p({"from_date": _S, "to_date": _S, "kind": _S, "reason": _S}, ["from_date"])),
    (t_dept_overview, "dept_overview", "HOD: their department's students, load, pending leave, staff away.", _p({})),
]
GATED_WRITES = ("request_gate_pass", "request_certificate", "apply_leave")
AGENT_OF = {"my_home": "StudentAgent", "my_requests": "RequestAgent", "my_placement": "PlacementAgent",
            "request_gate_pass": "GatePassAgent", "request_certificate": "DocumentAgent", "my_mentees": "StudentAgent",
            "my_leaves": "HRAgent", "apply_leave": "HRAgent", "dept_overview": "AnalyticsAgent"}


# ============================================================== routing
def route(con, text, P, ent):
    """Rule-engine routing for a non-admin: utterance -> (tool, args), or
    (None, None) for help. Every tool it can name still passes access.py."""
    t = text.lower()
    role = P["role"]
    usn = ent.get("usn")
    if re.search(r"\b(?:my day|home|dashboard|overview of me|good morning)\b", t) and "department" not in t:
        return "my_home", {}
    if re.search(r"make-?up|extra class|extra hour|rescheduled class", t):
        return "makeup_schedule", {}
    if role == "student":
        if re.search(r"gate ?pass|outing|out ?pass|home visit|go home|leave (?:the )?campus", t):
            k = ("Home visit" if re.search(r"home", t) else "Medical" if re.search(r"medical|doctor|hospital", t) else
                 "Emergency" if "emergency" in t else "Early leave" if re.search(r"early|after lunch", t) else "Outing")
            times = re.findall(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b", t)
            d, _l = nlu.parse_date(text)
            ds = (d or TODAY).isoformat()
            rng = re.search(r"\bto\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*", t)
            back = None
            if rng:
                bd, _ = nlu.parse_date("on " + rng.group(1))
                back = f"{bd.isoformat()} {times[1] if len(times) > 1 else '6pm'}" if bd else None
            return "request_gate_pass", {
                "kind": k, "out_at": f"{ds} {times[0] if times else '2pm'}",
                "return_by": back or (f"{ds} {times[1]}" if len(times) > 1 else ""),
                "reason": (re.search(r"\b(?:for|to attend|because)\s+(.+)$", text, re.I) or [None, ""])[1][:120],
                "parent_consent": bool(re.search(r"parents? (?:know|agreed|consent|approved|permission|are ok)", t))}
        if re.search(r"certificate|bonafide|\bnoc\b|no[- ]?dues? cert|transfer cert", t) and not re.search(r"status|position", t):
            m = re.search(r"\bfor\s+(?:a |an |my |the )?([a-z0-9][a-z0-9 \-&]{2,50})$", t)
            return "request_certificate", {"kind": cert_kind(text) or "Bonafide", "purpose": m.group(1) if m else ""}
        if re.search(r"no[- ]?dues|dues|fees?\b|clearance", t):
            return "no_dues_status", {}
        if re.search(r"placement|drive|eligible|offer|company|recruit", t):
            return "my_placement", {}
        if re.search(r"time ?table|classes|schedule|period", t):
            return "get_timetable", {"date": (nlu.parse_date(text)[0] or TODAY).isoformat()}
        if re.search(r"exam|see\b|hall ticket", t):
            return "exam_schedule", {}
        if re.search(r"library|book", t):
            q = re.sub(r"\b(search|find|library|books?|for|on|about|do you have|any|the|a)\b", " ", t).strip()
            return "library_search", {"query": q or "engineering"}
        if re.search(r"request|status|my pass|application", t):
            return "my_requests", {}
        if re.search(r"attendance|cgpa|marks|record|profile|backlog|360|everything", t):
            return "student_360", {}
        return None, None
    # faculty and HOD
    if re.search(r"\b(?:apply|applying|take|need|want)\b.*\bleave\b|\bleave (?:on|from|for)\b|\bi(?:'m| am| will be) (?:absent|on leave)", t) \
            and not re.search(r"pending|applications|approve|reject", t):
        a, b = nlu.date_range(text)
        k = ("Medical Leave" if re.search(r"medical|sick|fever|doctor", t) else "On Duty (Conference)"
             if re.search(r"on duty|conference|workshop|\bod\b", t) else "Earned Leave" if "earned" in t else "Casual Leave")
        rs = re.search(r"\b(?:for|because|due to)\s+(?:a |an |my )?([a-z][a-z \-]{2,60})$", t)
        return "apply_leave", {"from_date": (a or TODAY + dt.timedelta(days=1)).isoformat(),
                               "to_date": (b or a or TODAY + dt.timedelta(days=1)).isoformat(),
                               "kind": k, "reason": rs.group(1) if rs else ""}
    if role == "hod":
        if re.search(r"department|dept overview|my dept|overview|who(?:'s| is| else is)? on leave|away today", t):
            return "dept_overview", {}
        m = re.search(r"\b(approve|reject)\b.*\bleave\s*#?\s*(\d{1,4})\b", t)
        if m:
            return "decide_leave", {"leave_id": int(m.group(2)), "decision": m.group(1)}
        if re.search(r"\b(?:approve|grant)\b.*\bleaves?\b", t):
            return "decide_leave", {"decision": "approve"}
        if re.search(r"pending leave|leave applications|leave requests", t):
            return "review_pending_leaves", {}
        if re.search(r"is absent|are absent|won't be|not coming|arrange (?:a )?(?:cover|substitute)", t) and ent.get("faculty"):
            return "plan_absence_coverage", {"faculty_name": ent["faculty"]["id"],
                                             "date": (ent.get("date") or TODAY + dt.timedelta(days=1)).isoformat()}
        if re.search(r"workload|load|utilis", t):
            return "faculty_workload", {}
        if re.search(r"defaulter|shortage|below 75|attendance", t) and not usn:
            return "attendance_defaulters", {}
        if re.search(r"eligib|hall ticket", t):
            return "exam_eligibility", {}
        if usn:
            return "student_360", {"usn": usn}
        if ent.get("faculty") and ent["faculty"]["id"] != P["fid"] and re.search(r"timetable|schedule", t):
            return "faculty_timetable", {"name": ent["faculty"]["id"]}
        if ent.get("faculty") and ent["faculty"]["id"] != P["fid"]:
            return "faculty_profile", {"name": ent["faculty"]["id"]}
    if re.search(r"mentee|mentoring|my students", t):
        return "my_mentees", {}
    if re.search(r"my leave|leave history|leave status", t):
        return "my_leaves", {}
    if re.search(r"free room|vacant room|empty room", t):
        return "find_free_rooms", {"period": (ent.get("periods") or [3])[0]}
    if ent.get("dept") and ent.get("sem") and re.search(r"time ?table", t):
        return "get_timetable", {"dept": ent["dept"], "sem": ent["sem"], "section": ent.get("section") or "A"}
    if re.search(r"time ?table|my classes|schedule|teaching", t):
        return "faculty_timetable", {}
    if re.search(r"profile|my load|workload", t):
        return "faculty_profile", {}
    if re.search(r"exam|see\b", t):
        return "exam_schedule", {}
    if re.search(r"library|book", t):
        q = re.sub(r"\b(search|find|library|books?|for|on|about|do you have|any|the|a)\b", " ", t).strip()
        return "library_search", {"query": q or "engineering"}
    if re.search(r"request|status", t):
        return "my_requests", {}
    return None, None


HELP = {
    "student": [["My day", "“Good morning” · “My attendance”"], ["Gate pass", "“Gate pass for Saturday 2pm to 7pm, parents know”"],
                ["Certificates", "“Request a bonafide certificate for passport”"], ["Fees & dues", "“My no-dues status”"],
                ["Placements", "“Am I eligible for any drive?”"], ["Timetable & exams", "“My timetable” · “Exam schedule”"],
                ["Library", "“Library books on machine learning”"], ["Tracking", "“My requests”"]],
    "faculty": [["My day", "“Good morning”"], ["Leave", "“Apply for casual leave on 10 sep for a family function”"],
                ["Mentees", "“My mentees”"], ["Timetable", "“My timetable” · “CSE sem 5 A timetable”"],
                ["Rooms", "“Free rooms at period 4”"], ["Tracking", "“My leave” · “My requests”"]],
}
HELP["hod"] = [["Department", "“Department overview”"], ["Leave", "“Pending leave applications” · “Approve leave 4”"],
               ["Absence cover", "“Dr. X is absent tomorrow”"], ["Workload & attendance", "“Faculty workload” · “Attendance defaulters”"]] \
              + HELP["faculty"]
