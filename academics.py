"""
VidyaERP :: academic standing data, as agents (#19, #32)

Two things live here: standing faculty availability (#19, first) and CIE, the
continuous internal evaluation marks (#32, second half of the file).

Standing faculty availability. A weekly window a teacher cannot teach in:
a visiting professor's off-campus days, a council they sit on, a research
afternoon. The timetable is a weekly pattern, and this is the input the solver
can honour for it; a one-off absence is leave, and drives rescheduling instead.

Read by three planners, so a window means the same thing everywhere:
  * solver.build_input - generation never places a class in it, and
    solver.verify reports any committed class that sits in one;
  * SubstitutionAgent.busy_faculty - nobody is handed a substitute class then;
  * SubstitutionAgent._busy_at - no make-up is booked for them then.

Writes follow the campus shape (campus._propose): one tool, which PROPOSES
without approval in the admin's own turn and commits with it, then re-reads
what it wrote before saying so.
"""
import re, random, datetime as dt
import db, nlu
from nlu import DAY_FULL
from guard import approved_this_turn
from agents import (rows, one, fac_name, B_text, B_table, B_cards, Auditor, NotifyAgent, ensure_availability)
from campus import _propose, _done, _err

ACTOR = "registrar"
DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# Synthetic reasons of the kind a real college records. Seeded windows are
# placed only where the seeded timetable already leaves the teacher free, so
# the institution starts consistent with them (tests/academics_test.py 1b).
SEED_REASONS = ["University Board of Studies meeting", "Research afternoon (PhD supervision)",
                "NSS programme officer duty", "Visiting faculty: off campus", "IQAC coordination",
                "Industry consultancy (sanctioned)"]
SEED_WINDOWS = [("Mon", 5, 7), ("Tue", 5, 7), ("Wed", 5, 7), ("Thu", 5, 7), ("Fri", 5, 7),
                ("Mon", 1, 2), ("Wed", 1, 2), ("Fri", 1, 2), ("Sat", 1, 2)]


def ensure(con=None):
    """Create the tables and seed them once. Idempotent; additive to an old DB."""
    own = con is None
    con = con or db.connect()
    ensure_availability(con)
    if not con.execute("SELECT 1 FROM faculty_availability LIMIT 1").fetchone():
        _seed(con, random.Random(1919))
    ensure_cie(con)
    if not con.execute("SELECT 1 FROM cie_marks LIMIT 1").fetchone():
        _seed_cie(con, random.Random(3232))
    con.commit()
    if own:
        con.close()


def _seed(con, rng):
    fac = [r["id"] for r in rows(con, "SELECT id FROM faculty ORDER BY id")]
    now = "2026-06-01T09:00:00"
    for i, fid in enumerate(sorted(rng.sample(fac, 6))):
        wins = SEED_WINDOWS[:]
        rng.shuffle(wins)
        for day, a, b in wins:
            if not one(con, "SELECT 1 FROM timetable WHERE faculty=? AND day=? AND period BETWEEN ? AND ?",
                       (fid, day, a, b)):
                con.execute("""INSERT INTO faculty_availability(faculty,day,p_from,p_to,reason,status,
                               created_by,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                            (fid, day, a, b, SEED_REASONS[i % len(SEED_REASONS)], "Active", "seed", now))
                break


# ================================================================== parsing
def _fac(con, ref):
    fac = rows(con, "SELECT id,name,dept,designation,expertise,max_load FROM faculty")
    m = re.search(r"\bf0?(\d{2,3})\b", str(ref or ""), re.I)
    if m:
        return next((f for f in fac if f["id"] == f"F{int(m.group(1)):03d}"), None)
    f, sc = nlu.match_faculty(str(ref or ""), fac)
    return f if f and sc >= 0.8 else None


def _day(ref):
    m = re.search(r"\b(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|"
                  r"friday|fri|saturday|sat)s?\b", str(ref or "").lower())
    return DAY_FULL[m.group(1)] if m else None


def _periods(ref):
    """'P5-P7', '5-7', '5', 'afternoon', 'morning', 'all day' -> (from, to)."""
    t = str(ref or "").lower()
    m = re.search(r"p?(\d)\s*(?:-|to|–)\s*p?(\d)", t)
    if m:
        a, b = sorted((int(m.group(1)), int(m.group(2))))
        return (a, b) if 1 <= a and b <= 7 else None
    m = re.search(r"\bp(\d)\b|\bperiod\s*(\d)\b", t)
    if m:
        p = int(m.group(1) or m.group(2))
        return (p, p) if 1 <= p <= 7 else None
    if "afternoon" in t:
        return (5, 7)
    if "morning" in t:
        return (1, 4)
    if "all day" in t or "whole day" in t or "full day" in t:
        return (1, 7)
    return None


def _span(a, b):
    return f"P{a}" if a == b else f"P{a}-P{b}"


# ==================================================================== tools
def t_faculty_availability(con, S, U, faculty=None, dept=None, **kw):
    ensure_availability(con)
    q, args = """SELECT a.*, f.name, f.dept FROM faculty_availability a JOIN faculty f ON f.id=a.faculty
                 WHERE a.status='Active'""", []
    if faculty:
        f = _fac(con, faculty)
        if not f:
            return _err(f"No faculty member matches '{faculty}'.")
        q += " AND a.faculty=?"
        args.append(f["id"])
    if dept:
        q += " AND f.dept=?"
        args.append(str(dept).upper())
    got = rows(con, q, tuple(args))
    got.sort(key=lambda r: (r["dept"], r["name"], DAY_ORDER.index(r["day"]) if r["day"] in DAY_ORDER else 9,
                            r["p_from"]))
    data = [{"id": r["id"], "faculty": r["name"], "faculty_id": r["faculty"], "dept": r["dept"],
             "day": r["day"], "periods": _span(r["p_from"], r["p_to"]), "reason": r["reason"]} for r in got]
    blocks = ([B_table(["Faculty", "Dept", "Every", "Periods", "Why"],
                       [[d["faculty"], d["dept"], d["day"], d["periods"], d["reason"]] for d in data],
                       title="Standing availability: weekly windows the timetable must avoid")]
              if data else [B_text("No standing unavailability is recorded"
                                   + (" for that selection." if (faculty or dept) else "."))])
    return {"data": {"count": len(data), "windows": data}, "blocks": blocks,
            "trace": [("TimetableAgent", "availability_read", f"{len(data)} standing window(s)")]}


def t_set_faculty_availability(con, S, U, faculty="", day="", periods="", reason="", remove=False, **kw):
    ensure_availability(con)
    f = _fac(con, faculty)
    if not f:
        return _err(f"No faculty member matches '{faculty}'. Give a name or staff ID.")
    d = _day(day)
    if not d:
        return _err("Which weekday? A standing window repeats every week, e.g. 'every Friday afternoon'.")
    span = _periods(periods) or ((1, 7) if remove else None)
    if not span:
        return _err("Which periods? For example P5-P7, 'afternoon' or 'all day'.")
    a, b = span
    remove = str(remove).lower() in ("1", "true", "yes")
    args = {"faculty": f["id"], "day": d, "periods": _span(a, b), "reason": reason or None,
            "remove": remove or None}

    if remove:
        hit = rows(con, """SELECT * FROM faculty_availability WHERE status='Active' AND faculty=? AND day=?
                           AND p_from<=? AND p_to>=?""", (f["id"], d, b, a))
        if not hit:
            return _err(f"{f['name']} has no standing window on {d} {_span(a, b)} to remove.")
        tbl = B_table(["Faculty", "Every", "Periods", "Why"],
                      [[f["name"], h["day"], _span(h["p_from"], h["p_to"]), h["reason"]] for h in hit],
                      title="Windows to remove")
        tr = [("TimetableAgent", "availability_plan", f"remove {len(hit)} window(s) for {f['id']}")]
        if not approved_this_turn(U):
            return _propose(S, "set_faculty_availability", args, [tbl],
                            f"remove {len(hit)} standing window(s) for {f['name']}", tr)
        for h in hit:
            con.execute("UPDATE faculty_availability SET status='Removed' WHERE id=?", (h["id"],))
        con.commit()
        left = sum(1 for h in hit if one(con, "SELECT status FROM faculty_availability WHERE id=?",
                                         (h["id"],))["status"] == "Active")
        Auditor().log(con, ACTOR, "TimetableAgent", "availability.remove", {"ids": [h["id"] for h in hit]},
                      "Applied" if not left else "Partial")
        _done(S, "set_faculty_availability")
        return {"data": {"removed": len(hit) - left},
                "blocks": [tbl, B_text(f"**Removed.** {f['name']} can be timetabled on {d} again.")],
                "refresh": True,
                "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                               ("TimetableAgent", "verify", f"{len(hit) - left}/{len(hit)} window(s) re-read as removed",
                                "ok" if not left else "error")]}

    # ---- add: show what the window collides with BEFORE anything is written
    clash = rows(con, """SELECT t.*, s.name sname FROM timetable t LEFT JOIN subjects s ON s.code=t.subject
                         WHERE t.faculty=? AND t.day=? AND t.period BETWEEN ? AND ? ORDER BY t.period""",
                 (f["id"], d, a, b))
    sections = sorted({f"{c['dept']}-{c['sem']}{c['section']}" for c in clash})
    blocks = [B_table(["Faculty", "Every", "Periods", "Why"], [[f["name"], d, _span(a, b), reason or "not stated"]],
                      title="Standing window to record")]
    if clash:
        blocks.append(B_table(["Period", "Class", "Subject"],
                              [[f"P{c['period']}", f"{c['dept']}-{c['sem']}{c['section']}",
                                f"{c['subject']} {c['sname'] or ''}".strip()] for c in clash],
                              title=f"{len(clash)} of their current classes fall inside it"))
        blocks.append(B_text(f"Recording the window does not move these classes. It stops every agent from "
                             f"adding more in that time, and a rebuild of "
                             f"{', '.join(sections)} will move them out."))
    tr = [("TimetableAgent", "availability_plan",
           f"{f['id']} {d} {_span(a, b)}: {len(clash)} current class(es) inside the window")]
    if not approved_this_turn(U):
        return _propose(S, "set_faculty_availability", args, blocks,
                        f"record {f['name']} as unavailable every {d} {_span(a, b)}", tr)
    now = dt.datetime.now().isoformat(timespec="seconds")
    cur = con.execute("""INSERT INTO faculty_availability(faculty,day,p_from,p_to,reason,status,created_by,
                         created_at) VALUES(?,?,?,?,?,?,?,?)""",
                      (f["id"], d, a, b, reason or "not stated", "Active", ACTOR, now))
    con.commit()
    back = one(con, "SELECT * FROM faculty_availability WHERE id=?", (cur.lastrowid,))
    ok = bool(back and back["status"] == "Active" and (back["faculty"], back["day"], back["p_from"], back["p_to"])
              == (f["id"], d, a, b))
    Auditor().log(con, ACTOR, "TimetableAgent", "availability.add", {"id": cur.lastrowid, **args},
                  "Applied" if ok else "Failed")
    _done(S, "set_faculty_availability")
    chips = [f"Rebuild the {s.split('-')[0]} sem {s.split('-')[1][:-1]} section {s[-1]} timetable" for s in sections[:2]]
    return {"data": {"recorded": ok, "id": cur.lastrowid, "classes_inside": len(clash), "sections": sections},
            "blocks": blocks[:1] + [B_text(f"**Recorded.** No agent will place {f['name']} in a class every "
                                          f"{d} {_span(a, b)} from now on."
                                          + (f" {len(clash)} existing class(es) still sit inside it until "
                                             f"{', '.join(sections)} is rebuilt." if clash else ""))],
            "refresh": True, "chips": chips + ["Show standing availability"],
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("TimetableAgent", "verify", "window re-read" if ok else "window missing after write",
                            "ok" if ok else "error")]}


# ================================================================ CIE (#32)
# Continuous internal evaluation. The scheme is ONE college's plausible policy,
# stated on every CIE answer and pinned by tests/cie_test.py - a policy
# constant like campus.CURFEW, not a transcription of any university's rules.
#
#   theory (kind T): the average of two IA tests of 25, plus an alternate
#                    assessment (assignment) of 25            -> CIE out of 50
#   lab    (kind L): the lab record, 30, plus a lab test, 20  -> CIE out of 50
#
# A student needs CIE_MIN of CIE_MAX in a course to sit its SEE.
# Each component: (code, out of, weight in the CIE total, date assessed).
CIE_MAX, CIE_MIN = 50, 20
CIE_SCHEME = {"T": (("IA1", 25, 0.5, "2026-07-29"), ("IA2", 25, 0.5, "2026-08-26"), ("AAT", 25, 1.0, "2026-09-11")),
              "L": (("RECORD", 30, 1.0, "2026-08-28"), ("LABTEST", 20, 1.0, "2026-09-14"))}
COMP_LABEL = {"IA1": "IA test 1", "IA2": "IA test 2", "AAT": "Assignment (AAT)", "RECORD": "Lab record",
              "LABTEST": "Lab test"}
SCHEME_MD = (f"Scheme: theory CIE = average of IA tests 1 and 2 (each /25) + assignment (/25); lab CIE = record "
             f"(/30) + lab test (/20). {CIE_MIN}/{CIE_MAX} is needed to sit the SEE.")
ENTRY_DAYS = 7              # marks are due in the system a week after the assessment
IA2_ENTERED_SHARE = 0.8     # seed: the share of theory courses whose IA2 marks are already in
RISKY = ("Cannot reach", "At risk")

CIE_DDL = """CREATE TABLE IF NOT EXISTS cie_marks(
  usn TEXT, subject TEXT, component TEXT, marks REAL, absent INT DEFAULT 0,
  entered_by TEXT, entered_at TEXT, PRIMARY KEY(usn, subject, component))"""
SUBJ_RX = re.compile(r"\b(b[a-z]{2,3}l?\d{3})\b", re.I)
CLASS_RX = re.compile(r"\b(cse|ise|ece|mech|civil)[\s-]*(?:sem(?:ester)?\s*)?([1-8])\s*-?\s*([ab])?\b", re.I)


def ensure_cie(con):
    con.execute(CIE_DDL)


def _denied(msg):
    """A scope refusal: another teacher's course, another department's student.
    The guard working, so DENIED with a warn step - never drawn as a failure."""
    return {"data": {"DENIED": msg}, "blocks": [B_text(msg)],
            "trace": [("PolicyGuard", "access_denied", msg, "warn")]}


def _held(c):
    return dt.date.fromisoformat(c[3])


def _courses(con, dept=None, sem=None, section=None, subject=None, teacher=None):
    """Every examined course (a theory or lab subject taught to one section),
    with its teacher of record: whoever takes most of its periods in the
    CURRENT timetable, so a rebuilt timetable moves the marks register with it."""
    q = """SELECT t.dept, t.sem, t.section, t.subject, s.name sname, s.kind, t.faculty, COUNT(*) n
           FROM timetable t JOIN subjects s ON s.code=t.subject
           WHERE s.kind IN ('T','L') AND t.faculty IS NOT NULL"""
    a = []
    for col, v in (("t.dept", str(dept).upper() if dept else None), ("t.sem", int(sem) if sem else None),
                   ("t.section", str(section).upper() if section else None),
                   ("t.subject", str(subject).upper() if subject else None)):
        if v is not None:
            q += f" AND {col}=?"
            a.append(v)
    best = {}
    for r in rows(con, q + " GROUP BY t.dept, t.sem, t.section, t.subject, t.faculty", tuple(a)):
        k = (r["dept"], r["sem"], r["section"], r["subject"])
        if k not in best or (r["n"], best[k]["faculty"]) > (best[k]["n"], r["faculty"]):
            best[k] = r
    size = {(r["dept"], r["sem"], r["section"]): r["n"]
            for r in rows(con, "SELECT dept, sem, section, COUNT(*) n FROM students GROUP BY dept, sem, section")}
    names = {r["id"]: r["name"] for r in rows(con, "SELECT id, name FROM faculty")}
    out = [{"subject": r["subject"], "sname": r["sname"], "kind": r["kind"], "dept": r["dept"], "sem": r["sem"],
            "section": r["section"], "cls": f"{r['dept']}-{r['sem']}{r['section']}", "teacher": r["faculty"],
            "tname": names.get(r["faculty"], r["faculty"]), "size": size.get(k[:3], 0)}
           for k, r in best.items()]
    if teacher:
        out = [c for c in out if c["teacher"] == teacher]
    return sorted(out, key=lambda c: (c["dept"], c["sem"], c["section"], c["kind"] != "T", c["subject"]))


def _book(con, courses):
    """{(subject, usn): {component: row}} for the given courses, in one read."""
    ensure_cie(con)
    subs = sorted({c["subject"] for c in courses})
    out = {}
    for i in range(0, len(subs), 400):
        part = subs[i:i + 400]
        for r in rows(con, f"SELECT * FROM cie_marks WHERE subject IN ({','.join('?' * len(part))})", tuple(part)):
            out.setdefault((r["subject"], r["usn"]), {})[r["component"]] = r
    return out


def _class_students(con, c):
    return rows(con, "SELECT usn, name FROM students WHERE dept=? AND sem=? AND section=? ORDER BY usn",
                (c["dept"], c["sem"], c["section"]))


def standing(kind, m):
    """Where a student stands in one course, from the components entered so far.

    secured   - CIE already banked: the unentered components counted as 0
    ceiling   - the most still reachable: the unentered counted as full marks
    projected - the total if they keep scoring at their rate so far
    status    - Secured (>= minimum already) / Cannot reach (ceiling below it) /
                At risk (projected below it) / On track / Not assessed"""
    sec = hi = got = got_max = 0.0
    for code, mx, w, _d in CIE_SCHEME[kind]:
        r = m.get(code)
        if r is None:
            hi += w * mx
            continue
        v = float(r["marks"] or 0)
        sec += w * v
        hi += w * v
        got += w * v
        got_max += w * mx
    proj = CIE_MAX * got / got_max if got_max else None
    status = ("Not assessed" if proj is None else "Secured" if sec >= CIE_MIN else "Cannot reach" if hi < CIE_MIN
              else "At risk" if proj < CIE_MIN else "On track")
    return {"secured": round(sec, 1), "ceiling": round(hi, 1),
            "projected": round(proj, 1) if proj is not None else None, "status": status,
            "needs": round(max(0.0, CIE_MIN - sec), 1)}


def so_far(kind, m):
    """(scored, out of) over the components assessed and entered so far, on the
    CIE scale - what a student's "marks" are before the CIE is complete. An
    absence counts as assessed and scored 0. None when nothing is entered."""
    got = of = 0.0
    for code, mx, w, _d in CIE_SCHEME[kind]:
        r = m.get(code)
        if r is not None:
            got += w * float(r["marks"] or 0)
            of += w * mx
    return (round(got, 1), round(of, 1)) if of else None


def _entry(c, comp, entered):
    """The entry state of one component of one course, as of TODAY."""
    code, mx, _w, _d = comp
    held = _held(comp)
    due = held + dt.timedelta(days=ENTRY_DAYS)
    if held > nlu.TODAY:
        state = f"Scheduled {held.strftime('%d %b')}"
    elif entered >= c["size"]:
        state = "Entered"
    elif nlu.TODAY > due:
        state = f"Overdue {(nlu.TODAY - due).days}d" + (f" ({entered}/{c['size']})" if entered else "")
    else:
        state = f"Due {due.strftime('%d %b')}" + (f" ({entered}/{c['size']})" if entered else "")
    return {"component": code, "state": state, "entered": entered, "size": c["size"], "due": due.isoformat(),
            "overdue_days": (nlu.TODAY - due).days if held <= nlu.TODAY and nlu.TODAY > due and entered < c["size"]
            else 0}


def _fmt(r, mx):
    if r is None:
        return "—"
    return "AB" if r["absent"] else f"{float(r['marks']):g}/{mx}"


def _course_state(con, c, book=None, students=None):
    book = book if book is not None else _book(con, [c])
    students = students if students is not None else _class_students(con, c)
    per = [(s, book.get((c["subject"], s["usn"]), {})) for s in students]
    per = [(s, m, standing(c["kind"], m)) for s, m in per]
    entry = [_entry(c, comp, sum(1 for _s, m, _st in per if comp[0] in m)) for comp in CIE_SCHEME[c["kind"]]]
    return per, entry


def cie_risk(con, dept=None, sem=None):
    """Every (student, course) whose CIE standing is At risk or Cannot reach -
    for the exam-eligibility check, the Ops radar and Student 360."""
    courses = _courses(con, dept=dept, sem=sem)
    book = _book(con, courses)
    out = []
    for c in courses:
        for s in _class_students(con, c):
            st = standing(c["kind"], book.get((c["subject"], s["usn"]), {}))
            if st["status"] in RISKY:
                out.append({"usn": s["usn"], "name": s["name"], "cls": c["cls"], "subject": c["subject"],
                            "sname": c["sname"], **st})
    return sorted(out, key=lambda x: (x["status"] != "Cannot reach", x["projected"] or 0, x["usn"]))


def overdue_entries(con, dept=None):
    out = []
    courses = _courses(con, dept=dept)
    book = _book(con, courses)
    for c in courses:
        _per, entry = _course_state(con, c, book)
        out += [{**c, **e} for e in entry if e["overdue_days"] > 0]
    return sorted(out, key=lambda x: (-x["overdue_days"], x["cls"], x["subject"]))


def _seed_cie(con, rng):
    """IA1 and the lab record are in for every course. IA2 was held on 26 Aug
    and is in for most theory courses; the rest are overdue, which is what the
    Ops radar and the entry-status view are for. The assignment and the lab
    test fall after TODAY. Marks follow the student's CGPA and their attendance
    in that subject - so a CIE risk is not independent of the rest of the record,
    unlike the headline attendance draw (#21)."""
    att = {(r["usn"], r["subject"]): r["attended"] / r["held"]
           for r in rows(con, "SELECT usn, subject, held, attended FROM attendance WHERE held>0")}
    stu = {r["usn"]: r for r in rows(con, "SELECT usn, cgpa, backlogs, attendance FROM students")}
    batch = []
    for c in _courses(con):
        comps = list(CIE_SCHEME[c["kind"]])
        if c["kind"] == "T" and rng.random() >= IA2_ENTERED_SHARE:
            comps = comps[:1]                          # IA2 held, marks not yet entered
        comps = [x for x in comps if _held(x) <= nlu.TODAY]
        for s in _class_students(con, c):
            r = stu[s["usn"]]
            a = att.get((s["usn"], c["subject"]), r["attendance"] / 100)
            mu = 0.18 + 0.07 * r["cgpa"] + 0.4 * (a - 0.8) - 0.02 * r["backlogs"]
            for code, mx, _w, held in comps:
                lab = code in ("RECORD", "LABTEST")
                f = min(1.0, max(0.0, rng.gauss(mu + (0.12 if lab else 0.0), 0.08 if lab else 0.12)))
                absent = int(not lab and rng.random() < (0.045 if a < 0.75 else 0.012))
                on = dt.date.fromisoformat(held) + dt.timedelta(days=rng.randint(1, ENTRY_DAYS - 1))
                batch.append((s["usn"], c["subject"], code, 0 if absent else round(f * mx), absent,
                              c["teacher"], f"{on.isoformat()}T16:00:00"))
    con.executemany("INSERT OR REPLACE INTO cie_marks(usn,subject,component,marks,absent,entered_by,entered_at) "
                    "VALUES(?,?,?,?,?,?,?)", batch)


def _subject(con, ref, dept=None):
    m = SUBJ_RX.search(str(ref or ""))
    if m:
        s = one(con, "SELECT * FROM subjects WHERE code=?", (m.group(1).upper(),))
    else:
        q = str(ref or "").strip()
        cand = rows(con, "SELECT * FROM subjects WHERE kind IN ('T','L') AND name LIKE ?"
                    + (" AND dept=?" if dept else ""), (f"%{q}%",) + ((dept,) if dept else ())) if len(q) >= 3 else []
        if len(cand) > 1:
            return _err(f"'{q}' matches {len(cand)} subjects — give the code.",
                        candidates=[{"id": x["code"], "name": f"{x['name']} ({x['dept']} sem {x['sem']})"}
                                    for x in cand[:6]])
        s = cand[0] if cand else None
    if not s:
        return _err(f"No subject matches '{ref}'. Give its code, e.g. BCS501.")
    if s["kind"] not in CIE_SCHEME:
        return _err(f"{s['code']} {s['name']} has no CIE marks — only theory and lab courses do.")
    if dept and s["dept"] != dept:
        return _denied(f"{s['code']} is a {s['dept']} subject, not {dept}.")
    return dict(s)


def _component(ref, kind=None):
    t = str(ref or "").lower()
    code = ("IA1" if re.search(r"\bia\s*-?\s*(?:test\s*)?(?:1|i|one)\b|\b(?:first|1st) (?:ia|internal|test)\b|"
                               r"\binternal (?:test )?(?:1|i)\b|\btest ?1\b", t) else
            "IA2" if re.search(r"\bia\s*-?\s*(?:test\s*)?(?:2|ii|two)\b|\b(?:second|2nd) (?:ia|internal|test)\b|"
                               r"\binternal (?:test )?(?:2|ii)\b|\btest ?2\b", t) else
            "AAT" if re.search(r"\baat\b|assignment|alternate assessment", t) else
            "LABTEST" if re.search(r"lab ?test|lab internal|lab exam", t) else
            "RECORD" if re.search(r"\brecord\b", t) else None)
    if code and kind and not any(x[0] == code for x in CIE_SCHEME[kind]):
        return None
    return code


def _parse_marks(marks):
    """'4VP24CS001 18, 4VP24CS002 AB' or {usn: mark} -> ([(USN, float|'AB')], [problems])."""
    if isinstance(marks, dict):
        items = [(str(k), str(v)) for k, v in marks.items()]
    elif isinstance(marks, list):
        items = [(str(x.get("usn", "")), str(x.get("marks", ""))) if isinstance(x, dict) else (str(x), "")
                 for x in marks]
    else:
        items = [(m.group(1), m.group(2) or "") for m in
                 re.finditer(r"\b(4vp\d{2}[a-z]{2}\d{3})\b\s*(?:[:=]|->|-|–|gets?|scored?|to)?\s*"
                             r"(ab(?:sent)?\b|\d{1,2}(?:\.\d)?\b)?", str(marks or ""), re.I)]
    out, bad = [], []
    for u, v in items:
        u, v = u.strip().upper(), v.strip().lower()
        if not re.fullmatch(r"4VP\d{2}[A-Z]{2}\d{3}", u):
            bad.append(f"'{u}' is not a USN")
        elif v.startswith("ab"):
            out.append((u, "AB"))
        elif re.fullmatch(r"\d{1,3}(?:\.\d+)?", v):
            out.append((u, float(v)))
        else:
            bad.append(f"{u} has no mark")
    return out, bad


def _canon(entries):
    return ", ".join(f"{u} {v if v == 'AB' else f'{v:g}'}" for u, v in sorted(entries))


# ------------------------------------------------------------------ CIE reads
def t_cie_marks(con, S, U, usn=None, subject=None, dept=None, sem=None, section=None, teacher=None, **kw):
    ensure_cie(con)
    dept = str(dept).upper() if dept else None
    tf = None
    if teacher:
        tf = _fac(con, teacher)
        if not tf:
            return _err(f"No faculty member matches '{teacher}'.")
    if usn:
        return _cie_student(con, usn, dept, tf)
    sub = None
    if subject:
        sub = _subject(con, subject, dept)
        if "data" in sub:
            return sub
    courses = _courses(con, dept=dept, sem=sem, section=section, subject=sub["code"] if sub else None,
                       teacher=tf["id"] if tf else None)
    if not courses:
        return _err(("No course taught by " + tf["name"] if tf else "No course") + " matches that.")
    if len(courses) == 1:
        return _cie_course(con, courses[0])
    if sub or sem or section or tf:
        who = (f"{tf['name']}'s courses" if tf else f"{sub['code']} {sub['name']}" if sub
               else " ".join(str(x) for x in (dept, f"sem {sem}" if sem else None, section) if x))
        return _cie_courses(con, courses, who)
    return _cie_overview(con, courses, dept)


def _cie_student(con, ref, dept, tf):
    m = re.search(r"\b4vp\d{2}[a-z]{2}\d{3}\b", str(ref), re.I)
    s = one(con, "SELECT * FROM students WHERE usn=?", (m.group(0).upper() if m else str(ref).upper(),))
    if not s:
        return _err(f"No student with USN {ref}.")
    if dept and s["dept"] != dept:
        return _denied(f"{s['usn']} is not in {dept}.")
    cs = _courses(con, s["dept"], s["sem"], s["section"], teacher=tf["id"] if tf else None)
    if tf and not cs:
        return _denied(f"{tf['name']} does not teach {s['usn']}.")
    book = _book(con, cs)
    subs = []
    for c in cs:
        m = book.get((c["subject"], s["usn"]), {})
        subs.append({"subject": c["subject"], "name": c["sname"], "kind": c["kind"],
                     "components": {k: (None if k not in m else "AB" if m[k]["absent"] else m[k]["marks"])
                                    for k, *_r in CIE_SCHEME[c["kind"]]},
                     "shown": " · ".join(f"{k} {_fmt(m.get(k), mx)}" for k, mx, _w, _d in CIE_SCHEME[c["kind"]]),
                     **standing(c["kind"], m)})
    risk = [x for x in subs if x["status"] in RISKY]
    cls = f"{s['dept']}-{s['sem']}{s['section']}"
    blocks = [B_cards([{"label": "Courses", "value": len(subs)},
                       {"label": "At risk", "value": len(risk), "tone": "bad" if risk else "good",
                        "sub": f"below {CIE_MIN}/{CIE_MAX} on current form"},
                       {"label": "Secured", "value": sum(1 for x in subs if x["status"] == "Secured"),
                        "tone": "good", "sub": "minimum already reached"}],
                      title=f"Internal marks (CIE) · {s['name']} · {s['usn']} · {cls}"),
              B_table(["Course", "Components", "Secured", "Projected", "Standing"],
                      [[f"{x['subject']} {x['name']}", x["shown"], f"{x['secured']:g}/{CIE_MAX}",
                        "—" if x["projected"] is None else f"{x['projected']:g}", x["status"]] for x in subs],
                      dense=True)]
    if risk:
        blocks.append(B_text(" ".join(f"**{x['subject']}** needs {x['needs']:g} more CIE marks from what is left"
                                      f" (up to {x['ceiling'] - x['secured']:g})." for x in risk)))
    blocks.append(B_text(SCHEME_MD))
    return {"data": {"usn": s["usn"], "name": s["name"], "class": cls,
                     "subjects": [{k: x[k] for k in ("subject", "name", "components", "secured", "projected",
                                                     "status", "needs")} for x in subs],
                     "at_risk": [x["subject"] for x in risk], "scheme": SCHEME_MD},
            "blocks": blocks,
            "trace": [("ExamAgent", "cie_read", f"{s['usn']}: {len(subs)} course(s), {len(risk)} at risk")]}


def _cie_course(con, c):
    per, entry = _course_state(con, c)
    comps = CIE_SCHEME[c["kind"]]
    risk = [(s, st) for s, _m, st in per if st["status"] in RISKY]
    means = {}
    for code, mx, _w, _d in comps:
        v = [float(m[code]["marks"]) for _s, m, _st in per if code in m and not m[code]["absent"]]
        means[code] = round(sum(v) / len(v), 1) if v else None
    head = f"{c['subject']} {c['sname']} · {c['cls']} · {c['tname']}"
    blocks = [B_cards([{"label": COMP_LABEL[e["component"]], "value": f"{e['entered']}/{e['size']}",
                        "sub": e["state"] + (f" · mean {means[e['component']]:g}" if means[e["component"]] is not None
                                             else ""),
                        "tone": "bad" if e["overdue_days"] else "good" if e["state"] == "Entered" else None}
                       for e in entry]
                      + [{"label": "At risk", "value": len(risk), "tone": "bad" if risk else "good",
                          "sub": f"below {CIE_MIN}/{CIE_MAX} on current form"}], title=head),
              B_table(["USN", "Name"] + [k for k, *_r in comps] + ["Secured", "Projected", "Standing"],
                      [[s["usn"], s["name"]] + [_fmt(m.get(k), mx) for k, mx, _w, _d in comps]
                       + [f"{st['secured']:g}", "—" if st["projected"] is None else f"{st['projected']:g}", st["status"]]
                       for s, m, st in per], dense=True)]
    if risk:
        blocks.append(B_text(f"**{len(risk)} at risk:** " + ", ".join(f"{s['usn']} ({st['projected']:g})"
                                                                    for s, st in risk[:12])
                             + (" …" if len(risk) > 12 else "")))
    blocks.append(B_text(SCHEME_MD))
    late = [e for e in entry if e["overdue_days"]]
    return {"data": {"course": {k: c[k] for k in ("subject", "sname", "cls", "tname", "size")},
                     "entry": [{k: e[k] for k in ("component", "state", "entered", "size")} for e in entry],
                     "means": means,
                     "at_risk": [{"usn": s["usn"], "name": s["name"], "projected": st["projected"],
                                  "needs": st["needs"], "status": st["status"]} for s, st in risk],
                     "scheme": SCHEME_MD},
            "blocks": blocks,
            "chips": ([f"Internal marks of {risk[0][0]['usn']}"] if risk else [])
                     + [f"Internal marks for {c['dept']}", "SEE eligibility check"],
            "trace": [("ExamAgent", "cie_read", f"{head}: {len(per)} students · {len(risk)} at risk · "
                                               + ", ".join(f"{e['component']} {e['state']}" for e in entry))]}


def _cie_courses(con, courses, who):
    book = _book(con, courses)
    out = []
    for c in courses:
        per, entry = _course_state(con, c, book)
        proj = [st["projected"] for _s, _m, st in per if st["projected"] is not None]
        out.append({**c, "entry": entry, "risk": sum(1 for *_x, st in per if st["status"] in RISKY),
                    "mean": round(sum(proj) / len(proj), 1) if proj else None})
    return {"data": {"courses": [{"subject": x["subject"], "class": x["cls"], "teacher": x["tname"],
                                  "entry": {e["component"]: e["state"] for e in x["entry"]},
                                  "mean_projected": x["mean"], "at_risk": x["risk"]} for x in out],
                     "scheme": SCHEME_MD},
            "blocks": [B_table(["Course", "Class", "Teacher", "Marks entry", "Mean (projected)", "At risk"],
                               [[f"{x['subject']} {x['sname']}", x["cls"], x["tname"],
                                 " · ".join(f"{e['component']} {e['state']}" for e in x["entry"]),
                                 "—" if x["mean"] is None else f"{x['mean']:g}/{CIE_MAX}", x["risk"]] for x in out],
                               title=f"Internal marks (CIE) · {who}", dense=True), B_text(SCHEME_MD)],
            "trace": [("ExamAgent", "cie_read", f"{len(out)} course(s) · {sum(x['risk'] for x in out)} "
                                               f"student-course pairs at risk")]}


def _cie_overview(con, courses, dept):
    book = _book(con, courses)
    by, late, risky = {}, [], {}
    for c in courses:
        per, entry = _course_state(con, c, book)
        d = by.setdefault(c["dept"], {"courses": 0, "overdue": 0, "pairs": 0, "students": set()})
        d["courses"] += 1
        for e in entry:
            if e["overdue_days"]:
                d["overdue"] += 1
                late.append({**c, **e})
        for s, _m, st in per:
            if st["status"] in RISKY:
                d["pairs"] += 1
                d["students"].add(s["usn"])
                risky.setdefault(s["usn"], {"usn": s["usn"], "name": s["name"], "cls": c["cls"], "subjects": []})
                risky[s["usn"]]["subjects"].append(c["subject"])
    late.sort(key=lambda x: (-x["overdue_days"], x["cls"], x["subject"]))
    top = sorted(risky.values(), key=lambda x: (-len(x["subjects"]), x["usn"]))
    blocks = [B_table(["Dept", "Courses", "Entries overdue", "Students at risk", "Student-course pairs"],
                      [[k, v["courses"], v["overdue"], len(v["students"]), v["pairs"]] for k, v in sorted(by.items())],
                      title=f"Internal marks (CIE){' · ' + dept if dept else ''} — as of "
                            f"{nlu.TODAY.strftime('%a %d %b')}")]
    if late:
        blocks.append(B_table(["Course", "Class", "Teacher", "Component", "Entered", "Overdue"],
                              [[f"{x['subject']} {x['sname']}", x["cls"], x["tname"], x["component"],
                                f"{x['entered']}/{x['size']}", f"{x['overdue_days']} day(s)"] for x in late[:15]],
                              title=f"Marks entry overdue ({len(late)})", dense=True))
    if top:
        blocks.append(B_table(["USN", "Name", "Class", "Courses at risk"],
                              [[x["usn"], x["name"], x["cls"], ", ".join(x["subjects"])] for x in top[:12]],
                              title=f"Most at risk of missing the CIE minimum ({len(top)} students)", dense=True))
    blocks.append(B_text(SCHEME_MD))
    return {"data": {"departments": [{"dept": k, "courses": v["courses"], "entries_overdue": v["overdue"],
                                      "students_at_risk": len(v["students"])} for k, v in sorted(by.items())],
                     "overdue": [{"subject": x["subject"], "class": x["cls"], "teacher": x["tname"],
                                  "component": x["component"], "days": x["overdue_days"]} for x in late[:20]],
                     "students_at_risk": len(top),
                     "top_at_risk": [{"usn": x["usn"], "class": x["cls"], "subjects": x["subjects"]} for x in top[:10]],
                     "scheme": SCHEME_MD},
            "blocks": blocks,
            "chips": [f"Internal marks of {top[0]['usn']}" if top else "SEE eligibility check",
                      f"Internal marks for {late[0]['subject']} {late[0]['cls'].split('-')[0]} section "
                      f"{late[0]['section']}" if late else "Exam schedule"],
            "trace": [("ExamAgent", "cie_scan", f"{len(courses)} courses · {len(late)} entries overdue · "
                                               f"{len(top)} students at risk")]}


# ------------------------------------------------------------------ CIE write
def t_record_cie_marks(con, S, U, subject="", component="", marks="", section=None, dept=None, teacher=None, **kw):
    """Enter or correct one component's marks for students of one course.
    PROPOSES without approval (a self-service caller must also have seen the
    proposal first - portal._staged); commits with it and re-reads every mark."""
    from portal import _staged                 # portal imports this module's callers, not this module
    ensure_cie(con)
    dept = str(dept).upper() if dept else None
    sub = _subject(con, subject, dept)
    if "data" in sub:
        return sub
    comp = _component(component, sub["kind"]) or (str(component).upper()
                                                  if any(x[0] == str(component).upper()
                                                         for x in CIE_SCHEME[sub["kind"]]) else None)
    if not comp:
        return _err(f"Which component? {sub['code']} has " + ", ".join(
            f"{k} ({COMP_LABEL[k]}, /{mx})" for k, mx, _w, _d in CIE_SCHEME[sub["kind"]]) + ".")
    spec = next(x for x in CIE_SCHEME[sub["kind"]] if x[0] == comp)
    _c, mx, _w, held = spec
    if dt.date.fromisoformat(held) > nlu.TODAY:
        return _err(f"{COMP_LABEL[comp]} for {sub['code']} is on {dt.date.fromisoformat(held).strftime('%a %d %b')} "
                    f"— marks can be entered once it has been held.")
    entries, bad = _parse_marks(marks)
    if not entries and not bad:
        return _err("Give the marks as USN and mark pairs, e.g. “4VP24CS001 18, 4VP24CS002 AB”.")
    seen = set()
    for u, _v in entries:
        if u in seen:
            bad.append(f"{u} appears twice")
        seen.add(u)
    stu = {r["usn"]: r for r in rows(con, f"SELECT * FROM students WHERE usn IN ({','.join('?' * len(seen))})",
                                     tuple(seen))} if seen else {}
    sec = str(section).upper() if section else None
    secs = {stu[u]["section"] for u in seen if u in stu and stu[u]["dept"] == sub["dept"]
            and stu[u]["sem"] == sub["sem"]}
    if not sec:
        if len(secs) > 1:
            return _err(f"Those USNs span sections {', '.join(sorted(secs))} of {sub['dept']} sem {sub['sem']} — "
                        f"enter one section at a time.")
        sec = next(iter(secs), None)
    for u, v in entries:
        s = stu.get(u)
        if not s:
            bad.append(f"no student {u}")
        elif (s["dept"], s["sem"], s["section"]) != (sub["dept"], sub["sem"], sec):
            bad.append(f"{u} is in {s['dept']}-{s['sem']}{s['section']}, not {sub['dept']}-{sub['sem']}{sec}")
        elif v != "AB" and not (0 <= v <= mx):
            bad.append(f"{u}: {v:g} is outside 0-{mx}")
    if bad:
        return _err(f"Nothing proposed — {len(bad)} problem(s): " + "; ".join(bad[:8]) + (" …" if len(bad) > 8 else ""),
                    problems=bad)
    cs = _courses(con, sub["dept"], sub["sem"], sec, sub["code"])
    if not cs:
        return _err(f"{sub['code']} is not on the timetable of {sub['dept']}-{sub['sem']}{sec}.")
    c = cs[0]
    tf = _fac(con, teacher) if teacher else None
    if teacher and (not tf or tf["id"] != c["teacher"]):
        return _denied(f"{sub['code']} for {c['cls']} is taught by {c['tname']} — only its teacher of record, the HOD "
                    f"or the Registrar can enter its marks.")
    args = {"subject": sub["code"], "component": comp, "marks": _canon(entries), "section": sec,
            "dept": dept, "teacher": teacher}
    book = _book(con, [c])
    students = _class_students(con, c)
    names = {s["usn"]: s["name"] for s in students}
    rowsx, changes = [], []
    for u, v in sorted(entries):
        old = book.get((c["subject"], u), {}).get(comp)
        was = None if old is None else ("AB" if old["absent"] else float(old["marks"]))
        if was == v:
            continue
        changes.append((u, was, v))
        rowsx.append([u, names.get(u, ""), "—" if was is None else was if was == "AB" else f"{was:g}",
                      v if v == "AB" else f"{v:g}", "new" if was is None else "correction"])
    if not changes:
        return _err(f"Those {comp} marks are already recorded exactly so — nothing to change.")
    before_n = sum(1 for s in students if comp in book.get((c["subject"], s["usn"]), {}))
    after_n = before_n + sum(1 for _u, was, _v in changes if was is None)
    after = {}
    for u, _was, v in changes:
        m = dict(book.get((c["subject"], u), {}))
        m[comp] = {"marks": 0 if v == "AB" else v, "absent": int(v == "AB")}
        after[u] = standing(c["kind"], m)
    newly = [u for u in after if after[u]["status"] in RISKY
             and standing(c["kind"], book.get((c["subject"], u), {}))["status"] not in RISKY]
    fixes = sum(1 for _u, was, _v in changes if was is not None)
    blocks = [B_table(["USN", "Name", "Now", "New", ""], rowsx,
                      title=f"{COMP_LABEL[comp]} (/{mx}) · {c['subject']} {c['sname']} · {c['cls']} — "
                            f"{len(changes) - fixes} new, {fixes} correction(s)", dense=True),
              B_text(f"After this, **{after_n}/{c['size']}** of {c['cls']} have {comp} marks"
                     + (f"; {c['size'] - after_n} still missing." if after_n < c["size"] else " — complete.")
                     + (" Corrections overwrite marks already entered; the old values go to the audit ledger."
                        if fixes else ""))]
    if newly:
        blocks.append(B_text(f"**{len(newly)} student(s)** would fall below the CIE minimum on current form: "
                             + ", ".join(newly[:10]) + "."))
    tr = [("ExamAgent", "cie_plan", f"{c['subject']} {c['cls']} {comp}: {len(changes)} mark(s), {fixes} correction(s)")]
    P = (S or {}).get("user")
    self_service = bool(P and P.get("role") != "admin")
    if not approved_this_turn(U) or (self_service and not _staged(S, "record_cie_marks", args)):
        return _propose(S, "record_cie_marks", args, blocks,
                        f"record {len(changes)} {comp} mark(s) for {c['subject']} {c['cls']}", tr)
    who = P["fid"] if self_service and P.get("fid") else ACTOR
    now = dt.datetime.now().isoformat(timespec="seconds")
    con.executemany("INSERT OR REPLACE INTO cie_marks(usn,subject,component,marks,absent,entered_by,entered_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    [(u, c["subject"], comp, 0 if v == "AB" else v, int(v == "AB"), who, now) for u, _w, v in changes])
    con.commit()
    ok = 0
    for u, _was, v in changes:
        r = one(con, "SELECT marks, absent FROM cie_marks WHERE usn=? AND subject=? AND component=?",
                (u, c["subject"], comp))
        ok += bool(r and r["absent"] == int(v == "AB") and (v == "AB" or float(r["marks"]) == v))
    Auditor().log(con, who, "ExamAgent", "cie.record",
                  {"subject": c["subject"], "class": c["cls"], "component": comp,
                   "changes": [[u, was, v] for u, was, v in changes]}, "Applied" if ok == len(changes) else "Partial")
    if after_n >= c["size"] > before_n:
        NotifyAgent().dispatch(con, [{"audience": f"Students · {c['cls']}", "channel": "App",
                                      "title": f"{comp} marks published — {c['subject']}",
                                      "body": f"{COMP_LABEL[comp]} marks for {c['subject']} {c['sname']} are in your "
                                              f"portal."}])
    _done(S, "record_cie_marks")
    return {"data": {"recorded": ok, "of": len(changes), "course": f"{c['subject']} {c['cls']}", "component": comp,
                     "entered": after_n, "class_size": c["size"]},
            "blocks": blocks[:1] + [B_text(f"**Recorded.** {ok} {comp} mark(s) for {c['subject']} {c['cls']} re-read "
                                           f"as entered; {after_n}/{c['size']} of the class now have them.")],
            "refresh": True, "chips": [f"Internal marks for {c['subject']} section {c['section']}"],
            "trace": tr + [("PolicyGuard", "write_authorisation",
                            "the teacher confirmed in this turn" if self_service else "admin approved in this turn"),
                           ("ExamAgent", "verify", f"{ok}/{len(changes)} mark(s) re-read",
                            "ok" if ok == len(changes) else "error")]}


def cie_route(con, text, ent, admin=True):
    """utterance -> (tool, args) for CIE, shared by the Registrar's rule engine
    and the portal. A faculty caller's scope is access.py's business."""
    t = text.lower()
    sm = SUBJ_RX.search(text)
    cm = CLASS_RX.search(text)
    sec = ent.get("section") or (cm.group(3).upper() if cm and cm.group(3) else None)
    entries, _bad = _parse_marks(text)
    single = re.search(r"\bto\s+(\d{1,2}(?:\.\d)?|ab(?:sent)?)\b", t)
    usn = ent.get("usn")
    verb = re.search(r"\b(?:enter|record|upload|update|correct|change|fill|post|put|add|save)\b", t)
    if entries or (verb and usn and single):
        if not entries:
            entries = [(usn, "AB" if single.group(1).startswith("ab") else float(single.group(1)))]
        return "record_cie_marks", {"subject": sm.group(1).upper() if sm else "", "component": _component(t) or "",
                                    "marks": _canon(entries), "section": sec}
    if usn:
        return "cie_marks", {"usn": usn}
    args = {"subject": sm.group(1).upper() if sm else None,
            "dept": ent.get("dept") or (cm.group(1).upper() if cm else None),
            "sem": ent.get("sem") or (int(cm.group(2)) if cm else None), "section": sec}
    if admin and ent.get("faculty") and not sm and not cm:
        args["teacher"] = ent["faculty"]["id"]
    return "cie_marks", {k: v for k, v in args.items() if v}


# ================================================================= registry
def _p(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_S, _B = {"type": "string"}, {"type": "boolean"}

REGISTRY = [
    (t_faculty_availability, "faculty_availability",
     "Standing weekly windows in which a teacher cannot be timetabled (visiting days, councils, research "
     "afternoons). Optionally for one faculty member or department.", _p({"faculty": _S, "dept": _S})),
    (t_set_faculty_availability, "set_faculty_availability",
     "Record (or with remove=true, lift) a standing weekly window a teacher cannot teach in, e.g. every "
     "Friday P5-P7. Shows which of their current classes fall inside it. The solver, the substitution "
     "planner and make-up booking all respect it. Without admin approval this turn it only PROPOSES.",
     _p({"faculty": _S, "day": _S, "periods": _S, "reason": _S, "remove": _B}, ["faculty", "day"])),
    (t_cie_marks, "cie_marks",
     "Internal marks (CIE): one student's standing in every course (usn); one course's marks register "
     "(subject, with section); a class or a teacher's courses (dept/sem/section, teacher); or, with nothing, "
     "the institution: marks entry overdue and students at risk of missing the CIE minimum for the SEE.",
     _p({"usn": _S, "subject": _S, "dept": _S, "sem": {"type": "integer"}, "section": _S, "teacher": _S})),
    (t_record_cie_marks, "record_cie_marks",
     "Enter or correct one CIE component (IA1, IA2, AAT for theory; RECORD, LABTEST for labs) for students of "
     "one course. marks = 'USN mark' pairs, e.g. '4VP24CS001 18, 4VP24CS002 AB'. Validates range, class and "
     "teacher, shows new vs corrected marks. Without approval this turn it only PROPOSES.",
     _p({"subject": _S, "component": _S, "marks": _S, "section": _S}, ["subject", "component", "marks"])),
]
GATED_WRITES = ("set_faculty_availability", "record_cie_marks")
AGENT_OF = {"faculty_availability": "TimetableAgent", "set_faculty_availability": "TimetableAgent",
            "cie_marks": "ExamAgent", "record_cie_marks": "ExamAgent"}
TOOL_GROUPS = {"availability": ["faculty_availability", "set_faculty_availability", "faculty_timetable",
                                "plan_timetable_generation"],
               "cie": ["cie_marks", "record_cie_marks", "exam_eligibility", "student_360"]}
INTENTS = {"availability", "cie"}

WRITE_RX = re.compile(r"\b(?:not available|unavailable|can(?:no|')t teach|cannot teach|block(?:ed)? out|"
                      r"available again|no longer (?:busy|unavailable)|remove|lift|clear)\b")


def rule_route(con, intent, text, ent):
    """utterance -> (tool, args) for the rule engine; execution goes through
    tools.execute like every other caller."""
    if intent == "cie":
        return cie_route(con, text, ent)
    t = text.lower()
    fac = ent.get("faculty")
    if WRITE_RX.search(t) and fac:
        remove = bool(re.search(r"\bavailable again\b|\bno longer\b|\bremove\b|\blift\b|\bclear\b", t))
        why = re.search(r"(?:because of|because|due to|owing to|for)\s+(?:the\s+|a\s+|an\s+|his\s+|her\s+|their\s+)?(.+)$",
                        text, re.I)
        return "set_faculty_availability", {
            "faculty": fac["id"], "day": _day(t) or "", "periods": text,
            "reason": (why.group(1).strip(" .") if why and not remove else None), "remove": remove or None}
    return "faculty_availability", {"faculty": fac["id"] if fac else None, "dept": ent.get("dept")}
