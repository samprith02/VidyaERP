"""
VidyaERP :: the teaching desk - a teacher's day, and the attendance register (#116, #117)

The timetable is the context provider (#117). Every class hour a teacher can
open is resolved here, by `resolve()`, from what the institution already holds:
the weekly timetable, that date's applied overrides (a substitute takes the
hour, a swap brings in another subject, a vacated or released hour is
cancelled) and booked make-ups (#18). Nothing about a class - subject,
semester, section, period, room - is taken from the page or the model. A
caller names a class hour (date, period, class) and this module decides
whether it runs, what it is and whose it is.

Attendance (#116) is recorded one class hour at a time (attendance_sessions,
attendance_marks) and rolls into the per-subject totals the student dashboard
has always read (`attendance`: hours held, hours attended), then into the
headline `students.attendance`, recomputed from those totals. A correction
moves only `attended`: the hour was held either way. Its rules are policy
constants, stated wherever they refuse something:

  * never for a future date, and today only once the period has begun
    (against campus.NOW, the demo's one clock, never the server's);
  * recorded or corrected up to ATT_WINDOW_DAYS days after the class, then
    closed - a later correction is the HOD's or the Registrar's business;
  * one record per class hour - taking it again is a correction of the first,
    proposed as the changes it would make. A split section's lab hour is one
    class hour PER BATCH (#20): each batch has its own teacher, room, roster
    and register;
  * only by the teacher delivering that hour on that date: a substitute takes
    it, the absent teacher does not. access.py narrows `faculty` to the caller,
    and an HOD gets the same desk for the classes they actually teach - the
    HOD role itself opens no class.

Writes follow the self-service shape (portal._staged): the tool PROPOSES, and
commits only on the person's confirmation in a later turn; it then re-reads the
session, its marks and the totals before saying it worked.

The marks half of the desk lives in academics (cie_sheet, submit_cie_marks),
next to the register it reads and locks.
"""
import re, datetime as dt
import db, nlu, solver
from nlu import TODAY, day_of
from guard import approved_this_turn
from agents import rows, one, fac_name, B_text, B_table, B_cards, B_checklist, Auditor, ensure_makeups
from campus import _propose, _done, _err, NOW
from portal import _staged, class_status

ACTOR = "registrar"
ATT_WINDOW_DAYS = 2           # attendance can be recorded or corrected this many days after the class
ATT_KINDS = ("T", "L", "P")   # subjects with an attendance register; activity hours (kind A) keep none
KIND_LABEL = {"T": "Theory", "L": "Lab", "P": "Project", "A": "Activity"}
USN_RX = re.compile(r"\b4vp\d{2}[a-z]{2}\d{3}\b", re.I)
NOBODY_RX = re.compile(r"^\s*(?:none|nobody|no ?one|nil|-|all present|everyone present|everybody present)\s*$", re.I)

SESSION_COLS = ("id, date, period, dept, sem, section, subject, faculty, source, present, absent, taken_by, "
                "taken_at, updated_by, updated_at")
DDL = """CREATE TABLE IF NOT EXISTS attendance_sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, period INT, dept TEXT, sem INT, section TEXT,
  subject TEXT, faculty TEXT, source TEXT, present INT, absent INT, taken_by TEXT, taken_at TEXT,
  updated_by TEXT, updated_at TEXT, batch TEXT)"""
# one register per class hour, and a split lab hour is one class hour per batch (#20)
HOUR_IX = """CREATE UNIQUE INDEX IF NOT EXISTS attendance_hour
  ON attendance_sessions(date, dept, sem, section, period, IFNULL(batch, ''))"""
MARKS_DDL = """CREATE TABLE IF NOT EXISTS attendance_marks(
  session_id INT, usn TEXT, present INT, PRIMARY KEY(session_id, usn))"""


def ensure(con=None):
    """Create the register's tables. Nothing is seeded: the per-subject totals
    are the register to date, and sessions start with the first one recorded."""
    own = con is None
    con = con or db.connect()
    con.execute(DDL)
    if "batch" not in {r[1] for r in con.execute("PRAGMA table_info(attendance_sessions)")}:
        # an older register, unique per (date, class, period): rebuilt with the batch in its key
        con.execute("ALTER TABLE attendance_sessions RENAME TO attendance_sessions_old")
        con.execute(DDL)
        con.execute(f"INSERT INTO attendance_sessions({SESSION_COLS}) SELECT {SESSION_COLS} FROM attendance_sessions_old")
        con.execute("DROP TABLE attendance_sessions_old")
    con.execute(HOUR_IX)
    con.execute(MARKS_DDL)
    con.commit()
    if own:
        con.close()


def _denied(msg):
    """A scope refusal - another teacher's class. The guard working: DENIED with
    a warn step, never drawn as a failure."""
    return {"data": {"DENIED": msg}, "blocks": [B_text(msg)],
            "trace": [("PolicyGuard", "access_denied", msg, "warn")]}


def _fac(con, ref):
    import academics
    return academics._fac(con, ref)


def _date(v):
    if isinstance(v, dt.date):
        return v
    s = str(v or "").strip()
    if not s or s.lower() == "today":
        return TODAY
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        if s.lower() == "yesterday":
            return TODAY - dt.timedelta(days=1)
        if s.lower() == "tomorrow":
            return TODAY + dt.timedelta(days=1)
        return nlu.parse_date(s)[0] or nlu.parse_date("on " + s)[0]


def _start(period):
    return dt.time.fromisoformat(db.PERIODS[int(period)].split("-")[0])


def _on_leave(con, fid, d):
    return one(con, "SELECT id FROM leaves WHERE faculty=? AND status='Approved' AND from_date<=? AND to_date>=?",
               (fid, d.isoformat(), d.isoformat()))


# ================================================================ resolution
def _pick(got, batch):
    """The row for `batch` among one hour's rows, else the whole-section one."""
    return next((x for x in got if x["batch"] == batch), None) or next((x for x in got if x["batch"] is None), None)


def label(r):
    """'CSE-5A', or 'CSE-5A · batch B1' for one batch of a split lab hour."""
    return r["cls"] + (f" · batch {r['batch']}" if r.get("batch") else "")


def hours_at(con, d, dept, sem, section, period):
    """The class hours in one period of one class: [None] for the section, or
    one entry per lab batch when the hour is split (#20)."""
    ensure_makeups(con)
    key = (str(dept).upper(), int(sem), str(section).upper())
    got = ({r["batch"] for r in rows(con, """SELECT batch FROM makeup_sessions WHERE status='Scheduled' AND date=?
                                             AND dept=? AND sem=? AND section=? AND period=?""",
                                     (d.isoformat(), *key, int(period)))}
           or {r["batch"] for r in rows(con, "SELECT batch FROM timetable WHERE dept=? AND sem=? AND section=? "
                                             "AND day=? AND period=?", (*key, day_of(d), int(period)))})
    return sorted(got, key=lambda b: b or "")


def resolve(con, d, dept, sem, section, period, batch=None):
    """What runs in one class hour on one date, or None if the class has no
    such hour that day. `batch` names one lab batch of a split hour (#20); the
    whole-section row answers when the hour is not split. The answer carries
    `cancelled` (a holiday, a vacated or released hour) or `uncovered` (its
    teacher is on approved leave and no cover was arranged) when nobody can take
    attendance for it."""
    dept, section, sem, period = str(dept).upper(), str(section).upper(), int(sem), int(period)
    if day_of(d) == "Sun" or period not in db.PERIODS:
        return None
    base = {"date": d.isoformat(), "day": day_of(d), "period": period, "time": db.PERIODS[period],
            "dept": dept, "sem": sem, "section": section, "cls": f"{dept}-{sem}{section}", "slot_id": None}
    ensure_makeups(con)
    mk = _pick(rows(con, """SELECT * FROM makeup_sessions WHERE status='Scheduled' AND date=? AND dept=? AND sem=?
                            AND section=? AND period=?""", (d.isoformat(), dept, sem, section, period)), batch)
    if mk:
        out = {**base, "subject": mk["subject"], "faculty": mk["faculty"], "room": mk["room"], "source": "makeup",
               "batch": mk["batch"], "weekly_faculty": None, "note": f"make-up class · {mk['reason'] or 'booked'}"}
    else:
        t = _pick(rows(con, "SELECT * FROM timetable WHERE dept=? AND sem=? AND section=? AND day=? AND period=?",
                       (dept, sem, section, day_of(d), period)), batch)
        if not t:
            return None
        out = {**base, "slot_id": t["id"], "subject": t["subject"], "faculty": t["faculty"], "room": t["room"],
               "source": "timetable", "batch": t["batch"], "weekly_faculty": t["faculty"], "note": None}
        o = one(con, "SELECT * FROM overrides WHERE date=? AND slot_id=? AND status='Applied' ORDER BY id DESC LIMIT 1",
                (d.isoformat(), t["id"]))
        if o and o["action"] in ("VACATED", "MAKEUP"):
            out["cancelled"] = "moved by an approved schedule change" + (f": {o['reason']}" if o["reason"] else "")
        elif o:
            out.update(faculty=o["new_faculty"] or t["faculty"], subject=o["new_subject"] or t["subject"],
                       room=o["new_room"] or t["room"], source="swap" if o["new_subject"] else "substitute",
                       note=o["reason"])
        elif t["faculty"] and _on_leave(con, t["faculty"], d):
            out["uncovered"] = f"{fac_name(con, t['faculty'])} is on approved leave and no cover was arranged"
    hol = db.holiday(d)
    if hol:
        out["cancelled"] = f"{hol}: the college is closed"
    s = one(con, "SELECT name, kind FROM subjects WHERE code=?", (out["subject"],))
    out["sname"], out["kind"] = (s["name"], s["kind"]) if s else (out["subject"], "T")
    return out


def window(d, period):
    """(open, why not) for recording one class hour's attendance."""
    if d > TODAY:
        return False, f"{d.strftime('%a %d %b')} has not come yet — attendance is taken once the class has begun."
    if d == TODAY and NOW.time() < _start(period):
        return False, f"P{period} begins at {_start(period).strftime('%H:%M')}; its attendance opens then."
    if (TODAY - d).days > ATT_WINDOW_DAYS:
        return False, (f"Attendance for {d.strftime('%a %d %b')} closed {ATT_WINDOW_DAYS} days after the class "
                       f"(policy). A late correction goes through the HOD or the Registrar's office.")
    return True, ""


def _session(con, d, dept, sem, section, period, batch=None):
    ensure(con)
    return one(con, "SELECT * FROM attendance_sessions WHERE date=? AND dept=? AND sem=? AND section=? AND period=? "
                    "AND IFNULL(batch, '')=?", (d.isoformat(), dept, sem, section, period, batch or ""))


def _students(con, dept, sem, section, batch=None):
    """The roll of a class hour: the section, or one lab batch of it (#20)."""
    return rows(con, "SELECT usn, name FROM students WHERE dept=? AND sem=? AND section=?"
                + (" AND batch=?" if batch else "") + " ORDER BY usn", (dept, sem, section, *([batch] if batch else [])))


def day_classes(con, fid, d):
    """Every class hour on `d` that is, or was meant to be, this teacher's:
    their own (or a cancelled or covered one), a substitution or swap handed to
    them, a make-up they booked. Each carries its status against campus.NOW and
    its attendance state."""
    ensure(con)
    if day_of(d) == "Sun":
        return []
    cand = {(r["dept"], r["sem"], r["section"], r["period"], r["batch"]) for r in rows(
        con, "SELECT dept, sem, section, period, batch FROM timetable WHERE faculty=? AND day=?", (fid, day_of(d)))}
    cand |= {(r["dept"], r["sem"], r["section"], r["period"], r["batch"]) for r in rows(
        con, """SELECT t.dept, t.sem, t.section, t.period, t.batch FROM overrides o JOIN timetable t ON t.id=o.slot_id
                WHERE o.date=? AND o.status='Applied' AND o.new_faculty=?""", (d.isoformat(), fid))}
    ensure_makeups(con)
    cand |= {(r["dept"], r["sem"], r["section"], r["period"], r["batch"]) for r in rows(
        con, "SELECT dept, sem, section, period, batch FROM makeup_sessions WHERE status='Scheduled' AND date=? "
             "AND faculty=?", (d.isoformat(), fid))}
    out = []
    for k in sorted(cand, key=lambda x: (x[3], x[0], x[1], x[2], x[4] or "")):
        r = resolve(con, d, *k)
        if not r:
            continue
        if r["faculty"] != fid:
            if r.get("weekly_faculty") != fid:
                continue                            # not theirs in any sense on this date
            if not r.get("cancelled"):
                r["covered"] = f"covered by {fac_name(con, r['faculty'])}"
        out.append(r)
    live = [r for r in out if not (r.get("cancelled") or r.get("covered"))]
    st = (class_status([r["period"] for r in live]) if d == TODAY else
          ["Done"] * len(live) if d < TODAY else ["Later"] * len(live))
    status = {id(r): s for r, s in zip(live, st)}
    sizes = {}
    for r in out:
        if r.get("cancelled"):
            r["status"] = "Holiday" if db.holiday(d) else "Cancelled"
        elif r.get("covered"):
            r["status"] = "Covered"
        else:
            r["status"] = "On leave" if r.get("uncovered") else status[id(r)]
        key = (r["dept"], r["sem"], r["section"], r.get("batch"))
        if key not in sizes:
            sizes[key] = len(_students(con, *key))
        r["size"] = sizes[key]
        r["attendance"] = _att_state(con, r, d)
    return out


def _att_state(con, r, d):
    """The attendance cell for one class hour: recorded (and still correctable),
    open to take, not yet open, closed, or not kept for this kind of hour."""
    if r.get("cancelled") or r.get("covered") or r.get("uncovered"):
        return {"state": "None", "label": "—", "can_take": False}
    if r["kind"] not in ATT_KINDS:
        return {"state": "None", "label": f"No register ({KIND_LABEL.get(r['kind'], 'activity').lower()} hour)",
                "can_take": False}
    s = _session(con, d, r["dept"], r["sem"], r["section"], r["period"], r.get("batch"))
    ok, why = window(d, r["period"])
    if s:
        return {"state": "Recorded", "label": f"Recorded {s['present']}/{s['present'] + s['absent']}",
                "present": s["present"], "absent": s["absent"], "session": s["id"], "can_take": ok,
                "correctable": ok, "why": why}
    if ok:
        return {"state": "Pending", "label": "Not recorded", "can_take": True}
    if d > TODAY or (d == TODAY and NOW.time() < _start(r["period"])):
        return {"state": "Not yet", "label": f"Opens {_start(r['period']).strftime('%H:%M')}"
                if d == TODAY else "Not yet", "can_take": False, "why": why}
    return {"state": "Closed", "label": "Entry closed", "can_take": False, "why": why}


def pending_attendance(con, fid):
    """Class hours in the window that have begun and have no attendance yet."""
    out = []
    for i in range(ATT_WINDOW_DAYS, -1, -1):
        d = TODAY - dt.timedelta(days=i)
        out += [r for r in day_classes(con, fid, d) if r["attendance"]["state"] == "Pending"]
    return out


def _hour(con, date, period, dept, sem, section, faculty, batch=None):
    """(date, class hour, None) for a class hour this caller may open, else
    (None, None, refusal). One rule for the read and the write. A split lab
    hour (#20) is the caller's own batch: the one they teach, or the one named."""
    d = _date(date)
    if not d:
        return None, None, _err(f"Which date? '{date}' is not one I can read; use YYYY-MM-DD.")
    try:
        period = int(re.sub(r"\D", "", str(period)) or 0)
    except ValueError:
        period = 0
    if period not in db.PERIODS:
        return None, None, _err("Which period? P1 to P7.")
    cls, why = solver.resolve_class(con, dept, sem, section)
    if why:
        return None, None, _err(f"No section {str(dept or '').upper()}-{sem}{str(section or '').upper()} runs "
                                f"this semester.")
    want = str(batch).strip().upper() if batch else None
    opts = [x for x in (resolve(con, d, *cls, period, b) for b in hours_at(con, d, *cls, period)) if x]
    if want:
        named = [o for o in opts if o["batch"] == want]
        if opts and not named:
            return None, None, _err(f"{cls[0]}-{cls[1]}{cls[2]} P{period} on {d.strftime('%a %d %b')} has no batch "
                                    f"{want}" + (f"; its batches are {', '.join(o['batch'] for o in opts if o['batch'])}."
                                                 if any(o["batch"] for o in opts) else " - the section is taught whole."))
        opts = named
    if not opts:
        return None, None, _err(f"{cls[0]}-{cls[1]}{cls[2]} has no P{period} on {d.strftime('%a %d %b')}.")
    f = None
    if faculty:
        f = _fac(con, faculty)
        if not f:
            return None, None, _err(f"No faculty member matches '{faculty}'.")
    split = len(opts) > 1
    if split:
        who = "; ".join(f"batch {o['batch']}: {o['subject']} with {fac_name(con, o['faculty']) if o['faculty'] else 'nobody'}"
                        for o in opts)
        mine = [o for o in opts if f and o["faculty"] == f["id"]]
        if not f:
            return None, None, _err(f"{cls[0]}-{cls[1]}{cls[2]} P{period} on {d.strftime('%a %d %b')} is split into "
                                    f"lab batches ({who}). Name the batch.")
        if not mine:
            return None, None, _denied(f"{cls[0]}-{cls[1]}{cls[2]} P{period} on {d.strftime('%a %d %b')} is taught in "
                                       f"lab batches ({who}) — only the teacher delivering a batch can take its "
                                       f"attendance.")
        opts = mine
    r = opts[0]
    if r.get("cancelled"):
        return None, None, _err(f"{r['cls']} P{period} on {d.strftime('%a %d %b')} is cancelled — {r['cancelled']}.")
    if r["kind"] not in ATT_KINDS:
        return None, None, _err(f"{r['subject']} {r['sname']} is an activity hour; no attendance register is kept.")
    if f:
        if r["faculty"] != f["id"]:
            return None, None, _denied(
                f"{label(r)} P{period} on {d.strftime('%a %d %b')} ({r['subject']}) is taught by "
                f"{fac_name(con, r['faculty']) if r['faculty'] else 'nobody'}"
                + (" as a substitution" if r["source"] in ("substitute", "swap") else "")
                + " — only the teacher delivering that hour can take its attendance.")
    if r.get("uncovered"):
        return None, None, _err(f"{r['cls']} P{period} on {d.strftime('%a %d %b')} has no class: {r['uncovered']}.")
    return d, r, None


# ===================================================================== tools
def t_teaching_day(con, S, U, faculty=None, date=None, **kw):
    import academics
    f = _fac(con, faculty) if faculty else None
    if not f:
        return _err("Whose day? Name a teacher by staff ID." if not faculty else f"No faculty member matches '{faculty}'.")
    d = _date(date)
    if not d:
        return _err(f"Which date? '{date}' is not one I can read; use YYYY-MM-DD.")
    items = day_classes(con, f["id"], d)
    live = [r for r in items if r["status"] not in ("Cancelled", "Covered", "Holiday", "On leave")]
    count = {s: sum(1 for r in live if r["status"] == s) for s in ("Done", "Now", "Next", "Later")}
    pend = pending_attendance(con, f["id"])
    marks = academics.marks_pending(con, f["id"])
    hol = db.holiday(d)
    head = (f"{f['name']} · {d.strftime('%A %d %b %Y')}" + (f" · {hol}" if hol else "")
            + (f" · as of {NOW.strftime('%H:%M')}" if d == TODAY else ""))
    blocks = [B_cards([{"label": "Classes", "value": len(live)},
                       {"label": "Now", "value": count["Now"], "tone": "good" if count["Now"] else None},
                       {"label": "Upcoming", "value": count["Next"] + count["Later"]},
                       {"label": "Completed", "value": count["Done"]},
                       {"label": "Attendance to take", "value": len(pend), "tone": "warn" if pend else "good",
                        "sub": f"last {ATT_WINDOW_DAYS + 1} days"}], title=head)]
    if items:
        blocks.append(B_table(["Period", "Time", "Class", "Subject", "Room", "Status", "Attendance"],
                              [[f"P{r['period']}", r["time"], f"Sem {r['sem']} · {r['dept']} · Section {r['section']}"
                                + (f" · Batch {r['batch']}" if r.get("batch") else ""),
                                f"{r['subject']} · {r['sname']}" + (f" ({r['source']})" if r["source"] != "timetable" else ""),
                                r["room"] or "—", r["status"] + (f": {r.get('cancelled') or r.get('covered') or r.get('uncovered')}"
                                                                 if r["status"] in ("Cancelled", "Covered", "On leave")
                                                                 else ""),
                                r["attendance"]["label"]] for r in items], dense=True))
    else:
        blocks.append(B_text(f"No classes on {d.strftime('%A %d %b')}." + (f" {hol}: the college is closed." if hol else "")))
    todo = [{"ok": False, "agent": "AttendanceAgent",
             "label": f"Attendance: {r['subject']} {r['sname']} · {label(r)} · P{r['period']}",
             "detail": f"{dt.date.fromisoformat(r['date']).strftime('%a %d %b')} · not recorded"} for r in pend]
    todo += [{"ok": False, "agent": "ExamAgent",
              "label": f"Marks: {x['component']} {x['subject']} {x['sname']} · {x['cls']}",
              "detail": ("enter the marks — " + x["state"].lower()) if x["action"] == "enter"
              else "complete — submit to lock it"} for x in marks]
    if todo:
        blocks.append(B_checklist(todo, title="Academic actions"))
    return {"data": {"faculty": f["id"], "name": f["name"], "date": d.isoformat(), "day": day_of(d), "holiday": hol,
                     "today": TODAY.isoformat(), "now": NOW.strftime("%H:%M") if d == TODAY else None,
                     "window_days": ATT_WINDOW_DAYS,
                     "classes": [{k: r.get(k) for k in ("date", "period", "time", "dept", "sem", "section", "cls", "batch",
                                                         "subject", "sname", "kind", "room", "source", "note",
                                                         "status", "size", "attendance", "cancelled", "covered",
                                                         "uncovered")} for r in items],
                     "summary": {"classes": len(live), **{k.lower(): v for k, v in count.items()}},
                     "pending_attendance": [{k: r.get(k) for k in ("date", "period", "cls", "batch", "dept", "sem", "section",
                                                                 "subject", "sname", "time")} for r in pend],
                     "marks_pending": [{k: x[k] for k in ("subject", "sname", "cls", "section", "component", "label",
                                                          "action", "state")} for x in marks]},
            "blocks": blocks,
            "chips": ["My pending attendance", "My marks to enter", "Attendance history"],
            "trace": [("TimetableAgent", "teaching_day", f"{f['id']} {d.isoformat()}: {len(live)} class(es)"),
                      ("AttendanceAgent", "pending", f"{len(pend)} hour(s) to record"),
                      ("ExamAgent", "marks_pending", f"{len(marks)} component(s)")]}


def t_attendance_sheet(con, S, U, date=None, period=None, dept=None, sem=None, section=None, faculty=None,
                       batch=None, **kw):
    d, r, why = _hour(con, date, period, dept, sem, section, faculty, batch)
    if why:
        return why
    ok, closed = window(d, r["period"])
    students = _students(con, r["dept"], r["sem"], r["section"], r["batch"])
    s = _session(con, d, r["dept"], r["sem"], r["section"], r["period"], r["batch"])
    marks = {m["usn"]: m["present"] for m in rows(con, "SELECT usn, present FROM attendance_marks WHERE session_id=?",
                                                  (s["id"],))} if s else {}
    last = one(con, """SELECT * FROM attendance_sessions WHERE dept=? AND sem=? AND section=? AND subject=?
                       AND IFNULL(batch, '')=? AND (date<? OR (date=? AND period<?))
                       ORDER BY date DESC, period DESC LIMIT 1""",
               (r["dept"], r["sem"], r["section"], r["subject"], r["batch"] or "", d.isoformat(), d.isoformat(),
                r["period"]))
    last_absent = [m["usn"] for m in rows(con, "SELECT usn FROM attendance_marks WHERE session_id=? AND present=0 "
                                               "ORDER BY usn", (last["id"],))] if last else None
    head = (f"{r['subject']} {r['sname']} · Sem {r['sem']} · {r['dept']} · Section {r['section']} · "
            + (f"Batch {r['batch']} · " if r["batch"] else "") + f"{d.strftime('%a %d %b')} · P{r['period']} {r['time']}")
    state = ("Recorded" if s else "Open") if ok else ("Recorded, closed" if s else "Closed")
    return {"data": {"date": d.isoformat(), "period": r["period"], "time": r["time"], "dept": r["dept"], "sem": r["sem"],
                     "section": r["section"], "cls": r["cls"], "batch": r["batch"], "subject": r["subject"],
                     "sname": r["sname"], "kind": r["kind"], "room": r["room"], "source": r["source"],
                     "faculty": r["faculty"],
                     "teacher": fac_name(con, r["faculty"]), "open": ok, "why": closed, "state": state,
                     "window_days": ATT_WINDOW_DAYS, "recorded": bool(s),
                     "taken_by": s["taken_by"] if s else None, "taken_at": s["taken_at"] if s else None,
                     "updated_at": s["updated_at"] if s else None,
                     "students": [{"usn": x["usn"], "name": x["name"],
                                   "present": (bool(marks[x["usn"]]) if x["usn"] in marks else None)} for x in students],
                     "last_absent": last_absent,
                     "last_class": {"date": last["date"], "period": last["period"]} if last else None},
            "blocks": [B_cards([{"label": "Students", "value": len(students)},
                                {"label": "Present", "value": s["present"] if s else "—"},
                                {"label": "Absent", "value": s["absent"] if s else "—"},
                                {"label": "Register", "value": state, "sub": closed or (
                                    f"open for {ATT_WINDOW_DAYS} days after the class")}], title=head),
                       B_table(["USN", "Name", "Attendance"],
                               [[x["usn"], x["name"], "—" if x["usn"] not in marks else
                                 "Present" if marks[x["usn"]] else "Absent"] for x in students], dense=True)],
            "trace": [("TimetableAgent", "resolve_hour", f"{label(r)} P{r['period']} {d.isoformat()}: {r['subject']} "
                                                         f"({r['source']}) · {r['faculty']}"),
                      ("AttendanceAgent", "sheet", f"{len(students)} students · {state}")]}


def _absentees(absent):
    """'4VP24CS001, 4VP24CS007' / ['4VP24CS001'] / 'none' -> (set of USNs, problems)."""
    if isinstance(absent, (list, tuple)):
        toks = [str(x) for x in absent]
    else:
        s = str(absent if absent is not None else "")
        if NOBODY_RX.match(s):
            return set(), []
        toks = [x for x in re.split(r"[\s,;]+", s) if x]
    out, bad = set(), []
    for t in toks:
        u = t.strip().upper()
        if not USN_RX.fullmatch(u):
            bad.append(f"'{t}' is not a USN")
        elif u in out:
            bad.append(f"{u} appears twice")
        else:
            out.add(u)
    return out, bad


def _totals(con, usns, subject):
    got = {r["usn"]: (r["held"], r["attended"]) for r in rows(
        con, f"SELECT usn, held, attended FROM attendance WHERE subject=? AND usn IN ({','.join('?' * len(usns))})",
        (subject, *usns))} if usns else {}
    return {u: got.get(u, (0, 0)) for u in usns}


def t_take_attendance(con, S, U, date=None, period=None, dept=None, sem=None, section=None, absent=None,
                      faculty=None, batch=None, **kw):
    """Record one class hour's attendance, or correct one already recorded.
    PROPOSES without approval (and, for a teacher, until they have seen this
    proposal); commits with it, then re-reads the session, its marks and the
    per-subject totals."""
    ensure(con)
    d, r, why = _hour(con, date, period, dept, sem, section, faculty, batch)
    if why:
        return why
    ok, closed = window(d, r["period"])
    if not ok:
        return _err(closed)
    if absent is None or (not isinstance(absent, (list, tuple)) and not str(absent).strip()):
        return _err("Who was absent? List their USNs, or say none if everyone was present.")
    out_set, bad = _absentees(absent)
    students = _students(con, r["dept"], r["sem"], r["section"], r["batch"])
    names = {x["usn"]: x["name"] for x in students}
    bad += [f"{u} is not in {label(r)}" for u in sorted(out_set) if u not in names]
    if not students:
        bad.append(f"{label(r)} has nobody on its rolls")
    if bad:
        return _err(f"Nothing proposed — {len(bad)} problem(s): " + "; ".join(bad[:8]) + (" …" if len(bad) > 8 else ""),
                    problems=bad)
    want = {u: int(u not in out_set) for u in names}
    s = _session(con, d, r["dept"], r["sem"], r["section"], r["period"], r["batch"])
    old = {m["usn"]: m["present"] for m in rows(con, "SELECT usn, present FROM attendance_marks WHERE session_id=?",
                                                (s["id"],))} if s else {}
    changes = [(u, old.get(u), v) for u, v in sorted(want.items()) if old.get(u) != v] if s else []
    if s and not changes:
        return _err(f"Attendance for {r['cls']} P{r['period']} on {d.strftime('%a %d %b')} is already recorded "
                    f"exactly so — nothing to change.")
    present = sum(want.values())
    head = f"{r['subject']} {r['sname']} · {label(r)} · {d.strftime('%a %d %b')} · P{r['period']} {r['time']}"
    if s:
        blocks = [B_table(["USN", "Name", "Recorded", "Corrected to"],
                          [[u, names[u], "—" if was is None else "Present" if was else "Absent",
                            "Present" if v else "Absent"] for u, was, v in changes],
                          title=f"Correction · {head} — {len(changes)} change(s)", dense=True),
                  B_text(f"After this, **{present} of {len(students)}** present. The hour stays counted as held; "
                         f"only these students' attended hours move.")]
    else:
        blocks = [B_cards([{"label": "Students", "value": len(students)}, {"label": "Present", "value": present},
                           {"label": "Absent", "value": len(students) - present,
                            "tone": "warn" if len(students) - present else "good"}], title=f"Attendance · {head}")]
        blocks.append(B_table(["USN", "Name"], [[u, names[u]] for u in sorted(out_set)], title="Absent", dense=True)
                      if out_set else B_text("Everyone present."))
        blocks.append(B_text(f"Committing adds this hour to {r['subject']}'s attendance for every student in "
                             f"{label(r)}: held for all {len(students)}, attended for the {present} present."))
    tr = [("TimetableAgent", "resolve_hour", f"{label(r)} P{r['period']} {d.isoformat()}: {r['subject']} ({r['source']})"),
          ("AttendanceAgent", "correction_plan" if s else "register_plan",
           f"{len(changes)} change(s)" if s else f"{present}/{len(students)} present")]
    args = {"date": d.isoformat(), "period": r["period"], "dept": r["dept"], "sem": r["sem"], "section": r["section"],
            **({"batch": r["batch"]} if r["batch"] else {}),
            "absent": ", ".join(sorted(out_set)) or "none", "faculty": faculty}
    P = (S or {}).get("user")
    self_service = bool(P and P.get("role") != "admin")
    if not approved_this_turn(U) or (self_service and not _staged(S, "take_attendance", args)):
        return _propose(S, "take_attendance", args, blocks,
                        (f"correct {len(changes)} mark(s) in " if s else "record attendance for ") + head, tr)

    who = P["fid"] if self_service and P.get("fid") else ACTOR
    now = NOW.isoformat(timespec="seconds")         # the demo's one clock, never the server's
    usns = sorted(names)
    before = _totals(con, usns, r["subject"])
    if s:
        sid = s["id"]
        con.executemany("UPDATE attendance_marks SET present=? WHERE session_id=? AND usn=?",
                        [(v, sid, u) for u, _was, v in changes])
        con.execute("UPDATE attendance_sessions SET present=?, absent=?, updated_by=?, updated_at=? WHERE id=?",
                    (present, len(usns) - present, who, now, sid))
        delta = {u: v - (was or 0) for u, was, v in changes}
        held_step = 0
    else:
        sid = con.execute("""INSERT INTO attendance_sessions(date,period,dept,sem,section,subject,faculty,source,
                             present,absent,taken_by,taken_at,batch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (d.isoformat(), r["period"], r["dept"], r["sem"], r["section"], r["subject"], r["faculty"],
                           r["source"], present, len(usns) - present, who, now, r["batch"])).lastrowid
        con.executemany("INSERT INTO attendance_marks(session_id,usn,present) VALUES(?,?,?)",
                        [(sid, u, want[u]) for u in usns])
        delta = dict(want)
        held_step = 1
    for u in usns:
        if not held_step and not delta.get(u):
            continue
        cur = con.execute("UPDATE attendance SET held=held+?, attended=attended+? WHERE usn=? AND subject=?",
                          (held_step, delta.get(u, 0), u, r["subject"]))
        if not cur.rowcount:
            con.execute("INSERT INTO attendance(usn,subject,held,attended) VALUES(?,?,?,?)",
                        (u, r["subject"], held_step, delta.get(u, 0)))
    moved = [u for u in usns if held_step or delta.get(u)]
    for u in moved:                                # the headline follows the per-subject totals
        t = one(con, "SELECT SUM(held) h, SUM(attended) a FROM attendance WHERE usn=?", (u,))
        if t and t["h"]:
            con.execute("UPDATE students SET attendance=? WHERE usn=?", (round(100 * t["a"] / t["h"], 1), u))
    con.commit()

    # re-read what was written, never the writer's own word
    back = {m["usn"]: m["present"] for m in rows(con, "SELECT usn, present FROM attendance_marks WHERE session_id=?",
                                                 (sid,))}
    after = _totals(con, usns, r["subject"])
    sess = one(con, "SELECT * FROM attendance_sessions WHERE id=?", (sid,))
    problems = [u for u in usns if back.get(u) != want[u]]
    problems += [f"{u} totals" for u in usns
                 if after[u] != (before[u][0] + held_step, before[u][1] + delta.get(u, 0))]
    if not sess or sess["present"] != present or len(back) != len(usns):
        problems.append("session row")
    good = not problems
    Auditor().log(con, who, "AttendanceAgent", "attendance.correct" if s else "attendance.take",
                  {"session": sid, "class": r["cls"], "batch": r["batch"], "subject": r["subject"], "date": d.isoformat(),
                   "period": r["period"], "present": present, "of": len(usns),
                   **({"changes": [[u, was, v] for u, was, v in changes]} if s else {})},
                  "Applied" if good else "Partial")
    _done(S, "take_attendance")
    return {"data": {"recorded": good, "session": sid, "class": r["cls"], "batch": r["batch"], "subject": r["subject"],
                     "date": d.isoformat(), "period": r["period"], "present": present, "of": len(usns),
                     "corrected": len(changes) if s else 0},
            "blocks": blocks[:1] + [B_text(
                (f"**Corrected.** {len(changes)} mark(s) changed; " if s else "**Recorded.** ")
                + f"{present} of {len(usns)} present in {label(r)}, re-read from the register, and every student's "
                  f"{r['subject']} attendance updated.")],
            "refresh": True, "chips": ["My pending attendance", "Attendance history"],
            "trace": tr + [("PolicyGuard", "write_authorisation",
                            "the teacher confirmed in this turn" if self_service else "admin approved in this turn"),
                           ("AttendanceAgent", "verify",
                            f"{len(back)} mark(s) and {len(usns)} total(s) re-read" if good
                            else f"re-read found {len(problems)} problem(s): {', '.join(problems[:4])}",
                            "ok" if good else "error")]}


def t_attendance_history(con, S, U, faculty=None, subject=None, section=None, from_date=None, to_date=None, **kw):
    ensure(con)
    f = _fac(con, faculty) if faculty else None
    if faculty and not f:
        return _err(f"No faculty member matches '{faculty}'.")
    q, a = "SELECT * FROM attendance_sessions WHERE 1=1", []
    for col, v in (("faculty", f["id"] if f else None), ("subject", str(subject).upper() if subject else None),
                   ("section", str(section).upper() if section else None)):
        if v:
            q += f" AND {col}=?"
            a.append(v)
    a0, a1 = (_date(from_date) if from_date else None), (_date(to_date) if to_date else None)
    if a0:
        q += " AND date>=?"
        a.append(a0.isoformat())
    if a1:
        q += " AND date<=?"
        a.append(a1.isoformat())
    got = rows(con, q + " ORDER BY date DESC, period DESC LIMIT 200", tuple(a))
    who = f"{f['name']}'s classes" if f else "every class"
    return {"data": {"faculty": f["id"] if f else None, "sessions": [
                {"date": x["date"], "period": x["period"], "class": f"{x['dept']}-{x['sem']}{x['section']}",
                 "batch": x["batch"],
                 "subject": x["subject"], "present": x["present"], "of": x["present"] + x["absent"],
                 "taken_by": x["taken_by"], "corrected": bool(x["updated_at"])} for x in got]},
            "blocks": [B_table(["Date", "Hour", "Class", "Subject", "Present", "Recorded"],
                               [[dt.date.fromisoformat(x["date"]).strftime("%a %d %b"), f"P{x['period']}",
                                 f"{x['dept']}-{x['sem']}{x['section']}" + (f" {x['batch']}" if x["batch"] else ""),
                                 x["subject"],
                                 f"{x['present']}/{x['present'] + x['absent']}",
                                 f"{x['taken_at'][:16].replace('T', ' ')}" + (" · corrected" if x["updated_at"] else "")]
                                for x in got], title=f"Attendance history · {who}", dense=True)
                       if got else B_text(f"No attendance recorded yet for {who} in that range.")],
            "trace": [("AttendanceAgent", "history", f"{len(got)} session(s)")]}


# ================================================================== registry
def _p(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_S, _I = {"type": "string"}, {"type": "integer"}
REGISTRY = [
    (t_teaching_day, "teaching_day",
     "A teacher's classes on one date (default today), from the timetable, that day's substitutions and make-ups: "
     "time, subject, semester, department, section, room, Done/Now/Next/Later or Cancelled/Covered, and whether "
     "attendance is recorded; plus attendance and marks still to do.", _p({"faculty": _S, "date": _S})),
    (t_attendance_sheet, "attendance_sheet",
     "The attendance register of one class hour (date, period, dept, sem, section): the class's students, what is "
     "recorded, the last class's absentees, and whether it is open. Refuses an hour the teacher is not delivering. "
     "A split lab hour is one register per lab batch (B1, B2): the teacher's own batch, or `batch`.",
     _p({"date": _S, "period": _I, "dept": _S, "sem": _I, "section": _S, "faculty": _S, "batch": _S},
        ["period", "dept", "sem", "section"])),
    (t_take_attendance, "take_attendance",
     "Record attendance for one class hour, or correct it: absent = the absentees' USNs, comma-separated, or "
     "'none'. Only the teacher delivering that hour, not for a future class, within the correction window. "
     "A split lab hour is recorded per lab batch (`batch`, B1/B2). Without approval this turn it only PROPOSES.",
     _p({"date": _S, "period": _I, "dept": _S, "sem": _I, "section": _S, "absent": _S, "faculty": _S, "batch": _S},
        ["period", "dept", "sem", "section", "absent"])),
    (t_attendance_history, "attendance_history",
     "Attendance hours recorded, newest first: for a teacher, a subject, a section, a date range.",
     _p({"faculty": _S, "subject": _S, "section": _S, "from_date": _S, "to_date": _S})),
]
GATED_WRITES = ("take_attendance",)
AGENT_OF = {"teaching_day": "TimetableAgent", "attendance_sheet": "AttendanceAgent",
            "take_attendance": "AttendanceAgent", "attendance_history": "AttendanceAgent"}
TOOL_GROUPS = {"teaching": ["teaching_day", "attendance_sheet", "take_attendance", "attendance_history",
                            "cie_sheet", "submit_cie_marks"]}


# =================================================================== routing
TAKE_RX = re.compile(r"\b(?:take|mark|record|enter|fill|correct|update|put)\b.*\battendance\b|\battendance\b.*\b(?:absent|all present)\b|\bwas absent\b|\bwere absent\b")
PENDING_ATT_RX = re.compile(r"\bpending attendance\b|\battendance (?:pending|to (?:take|record)|not (?:yet )?(?:taken|recorded))\b")
DAY_RX = re.compile(r"\b(?:today|tomorrow|yesterday|now|next class|my day's classes|classes (?:on|today|tomorrow)|"
                    r"(?:mon|tues|wednes|thurs|fri|satur)day)\b")


def _subject_hint(text, items):
    """The class hours a sentence names by subject code, name or initials."""
    t = text.lower()
    out = []
    for r in items:
        name = (r.get("sname") or "").lower()
        initials = "".join(w[0] for w in re.findall(r"[a-z]+", name) if w not in ("and", "of", "the", "for", "in"))
        if r["subject"].lower() in t or (name and name in t) or (len(initials) >= 2 and re.search(rf"\b{initials}\b", t)):
            out.append(r)
    return out


def route(con, text, P, ent):
    """utterance -> (tool, args) for a teacher's desk, or None. Every tool still
    passes access.py, which fills in `faculty` from the session."""
    t = text.lower()
    if PENDING_ATT_RX.search(t):
        return "teaching_day", {}
    if re.search(r"attendance (?:history|record(?:ed|s)? (?:so far|this week))|attendance i(?:'ve| have) (?:taken|recorded)", t):
        return "attendance_history", {}
    if TAKE_RX.search(t):
        d = ent.get("date") or TODAY
        if con is None:                              # routed without a database: the day it would resolve from
            return "teaching_day", {"date": d.isoformat()}
        items = [r for r in day_classes(con, P["fid"], d) if r["attendance"]["can_take"]]
        # "P3" is how a teacher names an hour; nlu.extract_periods reads "period 3" / "3rd hour"
        periods = ent.get("periods") or [int(p) for p in re.findall(r"\bp([1-7])\b", t)]
        if periods:
            items = [r for r in items if r["period"] in periods]
        if ent.get("dept"):
            items = [r for r in items if r["dept"] == ent["dept"]]
        if ent.get("sem"):
            items = [r for r in items if r["sem"] == ent["sem"]]
        if ent.get("section") and len({r["section"] for r in items}) > 1:
            items = [r for r in items if r["section"] == ent["section"]]
        named = _subject_hint(text, items)
        items = named or items
        if len(items) > 1:                           # "take attendance" in class: the hour that is on now
            now = [r for r in items if r["status"] == "Now"]
            items = now or items
        if len(items) != 1:
            return "teaching_day", {"date": d.isoformat()}
        r = items[0]
        hour = {"date": r["date"], "period": r["period"], "dept": r["dept"], "sem": r["sem"], "section": r["section"],
                **({"batch": r["batch"]} if r.get("batch") else {})}
        absent = [u.upper() for u in USN_RX.findall(text)]
        if absent or re.search(r"\b(?:all|everyone|everybody) (?:is |are |was |were )?present\b|\bnobody (?:is |was )?absent\b|\bno absentees\b", t):
            return "take_attendance", {**hour, "absent": ", ".join(absent) or "none"}
        return "attendance_sheet", hour
    if re.search(r"\b(?:time ?table|classes|schedule|teaching)\b", t) and (ent.get("date") or DAY_RX.search(t)):
        return "teaching_day", {"date": (ent.get("date") or TODAY).isoformat()}
    if re.search(r"\bmarks?\b.*\b(?:pending|to (?:enter|submit)|not (?:yet )?(?:entered|submitted)|history)\b|"
                 r"\b(?:pending|which) marks\b|\bmarks submissions?\b|\bmy marks to enter\b", t):
        return "cie_sheet", {}
    # "Enter Internal 1 marks for my DBMS class": the register to fill in. With
    # USN-mark pairs in it, it is an entry, and academics.cie_route has it.
    if re.search(r"\b(?:enter|record|fill|upload|add|open)\b.*\b(?:marks?|ia ?[12]|internals?)\b", t) \
            and not USN_RX.search(t):
        import academics
        if con is None:
            return "cie_sheet", {}
        mine = academics._courses(con, teacher=P["fid"])
        named = _subject_hint(text, mine)
        if ent.get("section") and len(named) > 1:
            named = [c for c in named if c["section"] == ent["section"]] or named
        if len(named) == 1:
            c = named[0]
            comp = academics._component(t, c["kind"])
            return "cie_sheet", {k: v for k, v in (("subject", c["subject"]), ("section", c["section"]),
                                                   ("component", comp)) if v}
        return "cie_sheet", {}
    return None
