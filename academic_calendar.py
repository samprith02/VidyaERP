"""
VidyaERP :: the academic calendar (#104)

One calendar, three sources, one visibility rule.

  * calendar_events - what the Registrar puts on it: holidays, college events,
    and exams the SEE timetable does not hold (an IA re-test, a lab exam).
  * exams           - the SEE timetable, read as it stands (ExamAgent's table).
  * the CIE scheme  - academics.CIE_SCHEME's assessment dates, per class.

Only the first is written here. The other two are shown read-only, so the
calendar cannot disagree with the exam timetable or with the dashboard's
"Coming up", which read the same rows.

WHO SEES WHAT is decided by `reaches()` and by nothing else. Every entry names
its audience as (dept, sem, section), each None for "all", and a student sees
an entry only when every named part is theirs: a CSE-7A internal exam reaches
CSE-7A, never CSE-7B, CSE-5A or ISE-7A. The student's class is read from their
own record, never from an argument, and access.py narrows any student call to
that record before it arrives here. The page draws what the server returns;
it decides nothing.

An entry that names a semester belongs to a term. "Semester 7" means whoever
is in the 7th semester NOW, so an entry dated in another academic year would
reach today's 7th-semester students with next year's exam. Such an entry is
refused when it is written and, should one reach the table some other way,
not shown to students either.

Nothing is assumed to be college-wide. An entry with no audience is refused
unless the Registrar says college-wide in so many words, and an exam must
name its class.

Writes follow the campus shape (campus._propose): one tool, which PROPOSES
without approval in the admin's own turn and commits with it, then re-reads
what it wrote before saying so.
"""
import re, datetime as dt
import db, nlu, solver
from nlu import TODAY
from guard import approved_this_turn
from agents import rows, one, B_text, B_table, Auditor
from campus import _propose, _done, _err

ACTOR = "registrar"
AGENT = "CalendarAgent"

# The kinds of entry, in the order the page lists them. The labels follow the
# terms the rest of the ERP already uses (CIE, SEE).
KINDS = {"cie": "Internal exam (CIE)", "see": "Semester exam (SEE)", "practical": "Practical exam",
         "lab": "Lab exam", "exam": "Exam", "event": "College event", "holiday": "Holiday", "other": "Other"}
EXAM_KINDS = ("cie", "see", "practical", "lab", "exam")
PLURAL = {"cie": "internal exams (CIE)", "see": "SEE papers", "practical": "practical exams", "lab": "lab exams",
          "exam": "exams", "event": "college events", "holiday": "holidays", "other": "other entries"}
SCOPES = ("college", "department", "semester", "section")


def academic_year(d):
    """'2026-27' for any date from 1 July 2026 to 30 June 2027."""
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def term_of(d):
    """(academic year, first day, last day) of the half-year term holding `d`:
    July-December is the odd semester, January-June the even one. A policy
    constant like campus.CURFEW, stated on every refusal it causes."""
    if d.month >= 7:
        return academic_year(d), dt.date(d.year, 7, 1), dt.date(d.year, 12, 31)
    return academic_year(d), dt.date(d.year, 1, 1), dt.date(d.year, 6, 30)


TERM = term_of(TODAY)                 # the term the students on the rolls are in

DDL = """CREATE TABLE IF NOT EXISTS calendar_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT, kind TEXT,
  date TEXT, end_date TEXT, start_time TEXT, end_time TEXT, venue TEXT,
  scope TEXT, dept TEXT, sem INT, section TEXT, academic_year TEXT, subject TEXT,
  status TEXT, created_by TEXT, created_at TEXT, updated_by TEXT, updated_at TEXT)"""
FIELDS = ("title", "description", "kind", "date", "end_date", "start_time", "end_time", "venue",
          "scope", "dept", "sem", "section", "academic_year", "subject")


# ===================================================================== seed
# Synthetic, like everything else: no real event, person or venue. Holidays
# come from db.HOLIDAYS, the same list the SEE seed avoids (#106).
SEED = [
    # (title, kind, date, start, end, venue, dept, sem, section, subject, description)
    ("Placement orientation for final-year students", "event", "2026-09-09", "14:00", "16:00", "Seminar hall 1",
     None, 7, None, None, "Training and placement cell: drive calendar, eligibility rules and documents to carry."),
    ("IA re-test: Machine Learning", "cie", "2026-09-10", "09:30", "10:30", "CSE-7A home room",
     "CSE", 7, "A", "BCS702", "For students absent from IA test 2 with an approved reason."),
    ("Computer Networks lab exam", "lab", "2026-09-12", "09:00", "12:00", "CSE networks lab",
     "CSE", 5, "B", "BCSL506", "Bring the lab record, signed up to the last experiment."),
    ("Engineers' Day celebration", "event", "2026-09-15", "10:00", "12:00", "Main auditorium",
     None, None, None, None, "Project exhibition and inter-department quiz. Classes resume after lunch."),
    ("Department tech talk: cloud-native systems", "event", "2026-09-18", "14:00", "15:30", "CSE seminar hall",
     "CSE", None, None, None, "An alumni talk for every CSE semester."),
    ("Annual sports meet", "event", "2026-09-19", "09:00", "16:00", "College grounds",
     None, None, None, None, "Track and field events; inter-department relay finals at 15:00."),
    ("Project phase-I review", "other", "2026-09-22", "09:30", "12:30", "CSE project lab",
     "CSE", 7, None, "BCS705", "Each team presents its synopsis and literature survey to the review panel."),
]


def ensure(con=None):
    """Create the table and seed it once. Idempotent; additive to an old DB."""
    own = con is None
    con = con or db.connect()
    con.execute(DDL)
    if not con.execute("SELECT 1 FROM calendar_events LIMIT 1").fetchone():
        _seed(con)
    con.commit()
    if own:
        con.close()


def _seed(con):
    now = "2026-06-15T09:00:00"
    ay, lo, _hi = TERM
    first = int(ay[:4])
    for (m, d), name in sorted(db.HOLIDAYS.items(), key=lambda x: (x[0][0] < 7, x[0])):
        day = dt.date(first if m >= 7 else first + 1, m, d)
        _insert(con, {"title": name, "kind": "holiday", "date": day.isoformat(), "scope": "college",
                      "academic_year": academic_year(day), "description": "College closed."}, "seed", now)
    for title, kind, date, st, en, venue, dept, sem, sec, subj, desc in SEED:
        d = dt.date.fromisoformat(date)
        _insert(con, {"title": title, "kind": kind, "date": date, "start_time": st, "end_time": en, "venue": venue,
                      "dept": dept, "sem": sem, "section": sec, "subject": subj, "description": desc,
                      "scope": _scope_of(dept, sem, sec), "academic_year": academic_year(d)}, "seed", now)


def _insert(con, ev, who, now):
    cols = [f for f in FIELDS if ev.get(f) is not None]
    cur = con.execute(f"INSERT INTO calendar_events({','.join(cols)},status,created_by,created_at) "
                      f"VALUES({','.join('?' * len(cols))},'Active',?,?)", [ev[f] for f in cols] + [who, now])
    return cur.lastrowid


# ================================================================ audience
def _scope_of(dept, sem, section):
    return "section" if section else "semester" if sem else "department" if dept else "college"


def audience(e):
    """The audience in words, as the page and the agent both print it."""
    d, s, x = e.get("dept"), e.get("sem"), e.get("section")
    if x:
        return f"{d}-{s}{x}"
    if d and s:
        return f"{d} semester {s}, all sections"
    if s:
        return f"Semester {s}, every department"
    if d:
        return f"{d} department, every semester"
    return "College-wide"


def for_whom(e):
    """The audience after "for": 'everyone' rather than 'College-wide'."""
    a = audience(e)
    return "everyone" if a == "College-wide" else a


def reaches(e, dept, sem, section):
    """THE visibility rule: does entry `e` belong on the calendar of a student
    in class (dept, sem, section)? Every part the entry names must be theirs,
    and an entry that names a semester must be in the current term's academic
    year, because "semester 7" means whoever is in it now."""
    if e.get("dept") not in (None, dept) or e.get("sem") not in (None, sem) or \
            e.get("section") not in (None, section):
        return False
    return e.get("sem") is None or e.get("academic_year") == TERM[0]


def overlaps(a, b):
    """Could one person be in both audiences? None is 'all' on either side."""
    return all(x is None or y is None or x == y for x, y in
               ((a.get("dept"), b.get("dept")), (a.get("sem"), b.get("sem")), (a.get("section"), b.get("section"))))


def reach_count(con, e):
    """How many students on the rolls `e` reaches - by the same rule."""
    return sum(1 for s in rows(con, "SELECT dept, sem, section FROM students")
               if reaches(e, s["dept"], s["sem"], s["section"]))


# ================================================================= entries
def _clock(session):
    """'FN (09:30-12:30)' -> ('09:30', '12:30')."""
    m = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", session or "")
    return (f"{int(m.group(1)[:-3]):02d}{m.group(1)[-3:]}", f"{int(m.group(2)[:-3]):02d}{m.group(2)[-3:]}") \
        if m else (None, None)


def _shape(e, source, editable):
    e = dict(e)
    e.update({"source": source, "editable": editable, "kind_label": KINDS.get(e.get("kind"), e.get("kind")),
              "audience": audience(e), "scope": e.get("scope") or _scope_of(e.get("dept"), e.get("sem"),
                                                                             e.get("section"))})
    for k in ("created_by", "created_at", "updated_by", "updated_at"):
        e.pop(k, None)
    return e


def entries(con, lo=None, hi=None, include_cancelled=False):
    """Every entry from the three sources, dated within [lo, hi] (ISO strings,
    either may be None), in date order. The one list every reader filters."""
    con.execute(DDL)
    subj = {r["code"]: r for r in rows(con, "SELECT code, name, dept, sem, kind FROM subjects")}
    out = []
    q = "SELECT * FROM calendar_events" + ("" if include_cancelled else " WHERE status='Active'")
    for r in rows(con, q):
        r["subject_name"] = subj[r["subject"]]["name"] if r.get("subject") in subj else None
        out.append(_shape(r, "calendar", True))
    for r in rows(con, "SELECT * FROM exams"):
        st, en = _clock(r["session"])
        out.append(_shape({"id": f"see-{r['id']}", "title": f"SEE: {subj[r['subject']]['name']}"
                           if r["subject"] in subj else f"SEE: {r['subject']}",
                           "kind": "see", "date": r["date"], "start_time": st, "end_time": en,
                           "venue": r["room"], "dept": r["dept"], "sem": r["sem"], "section": None,
                           "subject": r["subject"], "subject_name": subj.get(r["subject"], {}).get("name"),
                           "academic_year": academic_year(dt.date.fromisoformat(r["date"])), "status": "Active",
                           "description": f"{r['kind']} · {r['session']}"}, "see", False))
    out += _cie_entries(subj)
    out = [e for e in out if (lo is None or (e.get("end_date") or e["date"]) >= lo) and (hi is None or e["date"] <= hi)]
    out.sort(key=lambda e: (e["date"], e.get("start_time") or "", e["title"], str(e["id"])))
    return out


# The CIE components as calendar kinds. The IA tests and the lab test are sat;
# an assignment is handed in and a lab record is checked, so neither answers
# "when is my next internal exam?".
CIE_KIND = {"IA1": "cie", "IA2": "cie", "LABTEST": "lab", "AAT": "other", "RECORD": "other"}


def _cie_entries(subj):
    """academics.CIE_SCHEME's dates, one entry per class and component, for the
    courses that class actually takes. Read-only: the scheme is changed there."""
    import academics                         # lazily: academics imports campus, which this module does too
    out = []
    classes = sorted({(s["dept"], s["sem"]) for s in subj.values() if s["dept"] and s["sem"]})
    for dept, sem in classes:
        for kind, comps in academics.CIE_SCHEME.items():
            codes = sorted(c for c, s in subj.items() if s["dept"] == dept and s["sem"] == sem and s["kind"] == kind)
            if not codes:
                continue
            for code, mx, _w, d in comps:
                label = academics.COMP_LABEL[code] + (" due" if code == "AAT" else "")
                out.append(_shape({"id": f"cie-{code}-{dept}-{sem}", "title": label,
                                   "kind": CIE_KIND[code], "date": d, "venue": None,
                                   "start_time": None, "end_time": None, "dept": dept, "sem": sem, "section": None,
                                   "academic_year": academic_year(dt.date.fromisoformat(d)), "status": "Active",
                                   "subject": None, "subject_name": None,
                                   "description": f"Out of {mx}, for {', '.join(codes)}. From the CIE scheme."},
                                  "cie", False))
    return out


def find(con, ref):
    """One entry by id ('12', '#12', 'see-4', 'cie-IA2-CSE-5'), cancelled ones included."""
    ref = str(ref or "").strip().lstrip("#")
    return next((e for e in entries(con, include_cancelled=True) if str(e["id"]).lower() == ref.lower()), None)


# ================================================================= parsing
MONTH_FULL = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                               "august", "september", "october", "november", "december"])}
_MON = nlu.CAL_MON


def _in_year(m, d):
    """A day and month with no year, placed in the current academic year:
    July-December this year, January-June the next. A calendar is read across
    the year, so '15 Aug' is the one in this academic year, not the next to come."""
    first = int(TERM[0][:4])
    return dt.date(first if m >= 7 else first + 1, m, d)


def parse_date(ref):
    """ISO, '27 Sep', '27th September 2026', 'Sep 27', 'September 27, 2026' -> date.
    None when there is no date; ValueError for an impossible one. Unlike
    nlu.parse_date this keeps Sundays: a calendar has seven days."""
    if isinstance(ref, dt.date):
        return ref
    t = str(ref or "").strip().lower()
    if not t:
        return None
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MON + r"\b,?(?:\s+(\d{4}))?", t) or \
        re.search(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b,?(?:\s+(\d{4}))?", t)
    if m:
        g = m.groups()
        day, mon = (int(g[0]), nlu.MONTHS[g[1][:3]]) if g[0].isdigit() else (int(g[1]), nlu.MONTHS[g[0][:3]])
        return dt.date(int(g[2]), mon, day) if g[2] else _in_year(mon, day)
    if "day after tomorrow" in t:
        return TODAY + dt.timedelta(days=2)
    if "tomorrow" in t:
        return TODAY + dt.timedelta(days=1)
    if "today" in t:
        return TODAY
    return None


def parse_time(ref):
    """'9am', '9:30', '09:30', '2 pm', '14:00', '9.30 am' -> 'HH:MM'. None when
    empty; ValueError for one that is not a time."""
    t = str(ref or "").strip().lower()
    if not t:
        return None
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", t)
    if not m:
        raise ValueError(f"'{ref}' is not a time. Use 24-hour HH:MM, e.g. 09:30 or 14:00.")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if ap and not 1 <= h <= 12 or not ap and not m.group(2) or h > 23 or mi > 59:
        raise ValueError(f"'{ref}' is not a time. Use 24-hour HH:MM, e.g. 09:30 or 14:00.")
    h = h % 12 + (12 if ap == "pm" else 0) if ap else h
    return f"{h:02d}:{mi:02d}"


def parse_kind(ref):
    t = str(ref or "").strip().lower()
    if not t:
        return None
    if t in KINDS:
        return t
    for k, lbl in KINDS.items():
        if t == lbl.lower():
            return k
    for rx, k in ((r"\bcie\b|internal|\bia\b|\bia ?[12]\b|ia test|ia re-?test", "cie"),
                  (r"\bsee\b|semester exam|end[- ]sem|semester[- ]end", "see"),
                  (r"practical", "practical"), (r"\blab (?:exams?|tests?|internals?)\b", "lab"),
                  (r"holiday|closed|vacation", "holiday"),
                  (r"\bexams?\b|\btests?\b|\bquiz\b", "exam"),
                  (r"event|fest|function|celebration|seminar|talk|workshop|orientation|\bmeet\b|day\b|ceremony|"
                   r"conference|hackathon|programme|program", "event"), (r"\bother\b|review|meeting", "other")):
        if re.search(rx, t):
            return k
    return None


def _scope_word(ref):
    t = str(ref or "").strip().lower()
    if not t:
        return None
    if re.search(r"college|everyone|all students|whole|entire|institution|campus", t):
        return "college"
    for s in ("department", "semester", "section"):
        if t.startswith(s[:4]):
            return s
    if t in ("class", "dept", "sem", "sec"):
        return {"class": "section", "dept": "department", "sem": "semester", "sec": "section"}[t]
    raise ValueError(f"'{ref}' is not a scope. Say college-wide, department, semester or section.")


def _sem(ref):
    if ref in (None, ""):
        return None
    m = re.fullmatch(r"\s*(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:sem(?:ester)?)?\s*", str(ref), re.I)
    if not m:
        raise ValueError(f"Semester must be a number, not {ref!r}.")
    return int(m.group(1))


# ============================================================== validation
def _no_section(running, dept, sem, section):
    """None if dept-sem-section is a class on the rolls, else why not - naming
    the ones that are, so nothing is widened to a class that exists (#94)."""
    if (dept, sem, section) in running:
        return None
    have = [f"{d}-{s}{x}" for d, s, x in running if d == dept and s == sem] or \
        [f"{d}-{s}{x}" for d, s, x in running if d == dept]
    return f"There is no {dept}-{sem}{section} on the rolls this term. {dept} classes: {', '.join(have)}."


def _subject(con, code):
    return one(con, "SELECT code, name, dept, sem, kind FROM subjects WHERE code=?", (str(code).strip().upper(),))


def resolve(con, a, self_id=None):
    """Arguments -> (the event exactly as it would be stored, None), or
    (None, why it cannot be). Writes nothing. Every rule here is a refusal:
    nothing is defaulted that changes who sees the event."""
    kind = parse_kind(a.get("kind"))
    if not kind:
        return None, ("What kind of entry is it? One of: " + ", ".join(KINDS.values()) + ".")
    try:
        date, end = parse_date(a.get("date")), parse_date(a.get("end_date"))
    except ValueError as e:
        return None, f"That is not a date on the calendar ({e})."
    if not date:
        return None, "Which date? For example 2026-09-29 or 29 Sep."
    try:
        st, en = parse_time(a.get("start_time")), parse_time(a.get("end_time"))
        sem, scope = _sem(a.get("sem")), _scope_word(a.get("scope"))
    except ValueError as e:
        return None, str(e)
    dept = str(a.get("dept") or "").strip().upper() or None
    section = str(a.get("section") or "").strip().upper() or None
    code = str(a.get("subject") or "").strip().upper() or None
    title = re.sub(r"\s+", " ", str(a.get("title") or "")).strip()
    venue = re.sub(r"\s+", " ", str(a.get("venue") or "")).strip() or None
    desc = str(a.get("description") or "").strip() or None

    # ---- when
    if end and end < date:
        return None, f"It cannot end ({end:%a %d %b %Y}) before it starts ({date:%a %d %b %Y})."
    if end == date:
        end = None
    if kind in EXAM_KINDS and end:
        return None, "An exam is held on one day. Enter each paper as its own entry."
    if en and not st:
        return None, "Give the start time as well as the end time."
    if st and en and en <= st:
        return None, f"It cannot end at {en} when it starts at {st}."

    # ---- who: named exactly, never assumed
    if dept and not one(con, "SELECT 1 FROM departments WHERE code=?", (dept,)):
        have = ", ".join(r["code"] for r in rows(con, "SELECT code FROM departments ORDER BY code"))
        return None, f"There is no {dept} department. Departments: {have}."
    running = solver.classes(con)
    if sem is not None and not any(s == sem and dept in (None, d) for d, s, _x in running):
        on = sorted({s for d, s, _x in running if dept in (None, d)})
        return None, (f"No {dept + ' ' if dept else ''}semester {sem} is on the rolls this term. "
                      f"Semesters running: {', '.join(map(str, on))}.")
    if section and (not dept or sem is None):
        return None, "A section belongs to one class: name the department and semester too, e.g. CSE-7A."
    if section:
        why = _no_section(running, dept, sem, section)
        if why:
            return None, why
    named = _scope_of(dept, sem, section)
    if scope == "college" and named != "college":
        return None, (f"A college-wide entry cannot also name {audience({'dept': dept, 'sem': sem, 'section': section})}. "
                      f"Remove the class, or do not call it college-wide.")
    if scope and scope != named:
        return None, (f"The scope says {scope}, but the audience given is {named} "
                      f"({audience({'dept': dept, 'sem': sem, 'section': section})}). Make them agree.")
    if named == "college" and scope != "college":
        return None, ("Who is this for? Name the class (e.g. CSE-7A), a semester, or a department, or say "
                      "college-wide. Nothing is put on every student's calendar unless you say so.")

    # ---- what
    subj = None
    if code:
        subj = _subject(con, code)
        if not subj:
            return None, f"No course has the code {code}."
        if named == "college":
            return None, f"{code} is a {subj['dept']} semester {subj['sem']} course: its exam is not college-wide."
        if dept != subj["dept"] or sem != subj["sem"]:
            return None, (f"{code} {subj['name']} is a {subj['dept']} semester {subj['sem']} course, but this entry "
                          f"is for {audience({'dept': dept, 'sem': sem, 'section': section})}. Name the class "
                          f"that takes it.")
    if not title:
        if subj:
            title = f"{KINDS[kind]}: {subj['name']}"
        else:
            return None, "Give it a title, e.g. \"Engineers' Day celebration\"."
    if len(title) > 120 or len(venue or "") > 80 or len(desc or "") > 600:
        return None, "Keep the title under 120 characters, the venue under 80 and the description under 600."

    ay = academic_year(date)
    if a.get("academic_year") not in (None, "") and str(a["academic_year"]).strip() != ay:
        return None, f"{date:%d %b %Y} is in academic year {ay}, not {a['academic_year']}."
    if sem is not None and not (TERM[1] <= date <= TERM[2] and (not end or end <= TERM[2])):
        return None, (f"Semester {sem} as it is on the rolls now runs in the {TERM[0]} term, {TERM[1]:%d %b %Y} to "
                      f"{TERM[2]:%d %b %Y}. A date outside it would reach whoever is in semester {sem} now, not the "
                      f"class it is meant for. Use a department or college-wide entry for a later term.")

    ev = {"title": title, "description": desc, "kind": kind, "date": date.isoformat(),
          "end_date": end.isoformat() if end else None, "start_time": st, "end_time": en, "venue": venue,
          "scope": named, "dept": dept, "sem": sem, "section": section, "academic_year": ay,
          "subject": subj["code"] if subj else None}

    # ---- conflicts with what is already on the calendar
    others = [e for e in entries(con, date.isoformat(), (end or date).isoformat())
              if str(e["id"]) != str(self_id) and overlaps(e, ev)]
    for o in others:
        if o["source"] == "calendar" and o["kind"] == kind and o["date"] == ev["date"] and \
                o["title"].lower() == title.lower() and audience(o) == audience(ev):
            return None, f"That is already on the calendar as #{o['id']}: {o['title']}, {audience(o)}."
    if kind in EXAM_KINDS:
        if date.weekday() == 6:
            return None, f"{date:%a %d %b %Y} is a Sunday. No exam is held on a Sunday (#101)."
        hol = [o for o in others if o["kind"] == "holiday"]
        if db.holiday(date) or hol:
            return None, (f"{date:%a %d %b %Y} is a holiday ({db.holiday(date) or hol[0]['title']}) for "
                          f"{for_whom(hol[0]) if hol else 'everyone'}. Choose a working day.")
        for o in others:
            if o["kind"] in EXAM_KINDS and o["date"] == ev["date"] and _times_overlap(o, ev):
                return None, (f"It clashes with {o['title']} ({o['kind_label']}, {audience(o)}"
                              f"{', ' + _when(o) if o.get('start_time') else ', the whole day'}) for the same students. "
                              f"Move one of them.")
    if kind == "holiday":
        ex = [o for o in others if o["kind"] in EXAM_KINDS]
        if ex:
            return None, (f"{len(ex)} exam(s) are on that day for the same students, e.g. {ex[0]['title']} "
                          f"({audience(ex[0])}). Move them before declaring a holiday.")
    return ev, None


def _times_overlap(a, b):
    """A missing time is the whole day."""
    if not a.get("start_time") or not b.get("start_time"):
        return True
    ae, be = a.get("end_time") or a["start_time"], b.get("end_time") or b["start_time"]
    return a["start_time"] < be and b["start_time"] < ae or a["start_time"] == b["start_time"]


def _when(e):
    if not e.get("start_time"):
        return "time not set"
    return e["start_time"] + (f"-{e['end_time']}" if e.get("end_time") else "")


def _day(iso):
    return dt.date.fromisoformat(iso).strftime("%a %d %b %Y")


def _dates(e):
    return _day(e["date"]) + (f" to {_day(e['end_date'])}" if e.get("end_date") else "")


def detail_rows(e):
    """Only the fields that apply to this entry, in reading order."""
    out = [["Type", e.get("kind_label") or KINDS.get(e["kind"], e["kind"])], ["Date", _dates(e)]]
    if e.get("start_time"):
        out.append(["Time", _when(e)])
    if e.get("subject"):
        out.append(["Subject", f"{e['subject']} {e.get('subject_name') or ''}".strip()])
    out.append(["For", audience(e)])
    if e.get("venue"):
        out.append(["Where", e["venue"]])
    if e.get("description"):
        out.append(["Details", e["description"]])
    return out


# ==================================================================== tools
def _month(ref):
    """'2026-09', 'september', 'this month', 'next month' -> (first, last)."""
    t = str(ref or "").strip().lower()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if not 1 <= mo <= 12:
            raise ValueError(f"{ref} is not a month.")
    elif "next month" in t:
        y, mo = (TODAY.year + (TODAY.month == 12), TODAY.month % 12 + 1)
    elif not t or "this month" in t:
        y, mo = TODAY.year, TODAY.month
    else:
        hit = next((MONTH_FULL[w] for w in re.findall(r"[a-z]+", t) if w in MONTH_FULL), None)
        if not hit:
            raise ValueError(f"'{ref}' is not a month. Use YYYY-MM, e.g. 2026-09.")
        yr = re.search(r"\b(\d{4})\b", t)
        y, mo = (int(yr.group(1)), hit) if yr else (_in_year(hit, 1).year, hit)
    first = dt.date(y, mo, 1)
    return first, (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)


def _kinds(ref):
    t = str(ref or "").strip().lower()
    if not t or t in ("all", "any", "everything"):
        return None
    if t in ("exam", "exams", "exams only", "tests"):
        return set(EXAM_KINDS)
    if t in ("events", "event"):
        return {"event"}
    k = parse_kind(t)
    if not k:
        raise ValueError(f"'{ref}' is not a kind of entry. One of: {', '.join(KINDS.values())}, or exams.")
    return set(EXAM_KINDS) if k == "exam" else {k}


def t_calendar_events(con, S, U, month=None, date=None, from_date=None, to_date=None, kind=None,
                      upcoming=None, usn=None, dept=None, sem=None, section=None, event_id=None, **kw):
    """The calendar as one person or one class sees it. For a student access.py
    has already replaced every argument that says whose it is with their own."""
    try:
        upcoming = str(upcoming).lower() in ("1", "true", "yes")
        sem = _sem(sem)
        kinds = _kinds(kind)
        if event_id not in (None, ""):
            lo = hi = None
            label = "this entry"
        elif date:
            d = parse_date(date)
            if not d:
                return _err(f"'{date}' is not a date. Use YYYY-MM-DD or 27 Sep.")
            lo = hi = d
            label = f"on {d:%a %d %b %Y}"
        elif from_date or to_date:
            lo, hi = parse_date(from_date) or TODAY, parse_date(to_date) or (parse_date(from_date) or TODAY) + \
                dt.timedelta(days=30)
            if hi < lo:
                return _err("The range ends before it starts.")
            label = f"from {lo:%d %b} to {hi:%d %b %Y}"
        elif upcoming:
            lo, hi = TODAY, TODAY + dt.timedelta(days=180)
            label = "coming up"
        else:
            lo, hi = _month(month)
            label = f"in {lo:%B %Y}"
    except ValueError as e:
        return _err(str(e))

    # ---- whose calendar
    if usn:
        s = one(con, "SELECT usn, name, dept, sem, section FROM students WHERE usn=?", (str(usn).strip().upper(),))
        if not s:
            return _err(f"No student has the USN {usn}.")
        for k, v in (("dept", dept), ("sem", sem), ("section", section)):
            if v not in (None, "") and str(v).upper() != str(s[k]).upper():
                return _err(f"{s['usn']} is in {s['dept']}-{s['sem']}{s['section']}, so that is not their calendar.")
        cls = (s["dept"], s["sem"], s["section"])
        keep = lambda e: reaches(e, *cls)
        whose = f"{s['name']} ({s['usn']}, {s['dept']}-{s['sem']}{s['section']})"
        mine = bool((S or {}).get("user") and S["user"].get("usn") == s["usn"])
        cal = "your calendar" if mine else f"the calendar of {whose}"
    else:
        dept = str(dept or "").strip().upper() or None
        section = str(section or "").strip().upper() or None
        if dept and not one(con, "SELECT 1 FROM departments WHERE code=?", (dept,)):
            return _err(f"There is no {dept} department.")
        if section:
            why = _no_section(solver.classes(con), dept, sem, section) if dept and sem is not None else \
                "A section belongs to one class: name the department and semester too, e.g. CSE-7A."
            if why:
                return _err(why)
        elif sem is not None and not any(s == sem and dept in (None, d) for d, s, _x in solver.classes(con)):
            return _err(f"No {dept + ' ' if dept else ''}semester {sem} is on the rolls this term.")
        want = {"dept": dept, "sem": sem, "section": section}
        keep = lambda e: overlaps(e, want)
        whose = audience(want) if any(want.values()) else "the whole college"
        mine = False
        cal = f"the calendar of {whose}" if any(want.values()) else "the calendar"

    got = entries(con, lo and lo.isoformat(), hi and hi.isoformat())
    if event_id not in (None, ""):
        ref = str(event_id).strip().lstrip("#").lower()
        got = [e for e in got if str(e["id"]).lower() == ref and keep(e)]
        if not got:
            return _err(f"Entry {event_id} is not on {cal}.")
        e = got[0]
        return {"data": {"event": e}, "blocks": [B_table(["", ""], detail_rows(e), title=e["title"])],
                "trace": [(AGENT, "event_read", f"{e['id']} · {audience(e)}")]}
    got = [e for e in got if keep(e) and (kinds is None or e["kind"] in kinds)]
    if upcoming:
        got = got[:10]
    what = "exams" if kinds == set(EXAM_KINDS) else PLURAL[next(iter(kinds))] if kinds else "entries"
    head = f"{'Your ' + what if mine else what[:1].upper() + what[1:]} {label}"
    if not got:
        blocks = [B_text(f"No {what} on {cal} {label}." if kinds else f"Nothing on {cal} {label}.")]
    else:
        lead = []
        if upcoming:
            n = got[0]
            days = (dt.date.fromisoformat(n["date"]) - TODAY).days
            lead = [B_text(f"**Next: {n['title']}** · {n['kind_label']}, {_day(n['date'])}"
                           f"{', ' + _when(n) if n.get('start_time') else ''}"
                           f" — {'today' if days == 0 else 'tomorrow' if days == 1 else f'in {days} days'}.")]
        elif date:
            one_, many = ("exam", "exams") if kinds == set(EXAM_KINDS) else ("entry", "entries")
            lead = [B_text(f"**Yes: {len(got)} {one_ if len(got) == 1 else many}** {label}.")]
        blocks = lead + [B_table(["Date", "Time", "What", "For", "Where"],
                                 [[_dates(e), _when(e) if e.get("start_time") else "—",
                                   f"{e['title']} · {e['kind_label']}", audience(e), e.get("venue") or "—"]
                                  for e in got],
                                 title=f"{head} ({len(got)})", dense=True)]
    return {"data": {"for": whose, "from": lo and lo.isoformat(), "to": hi and hi.isoformat(), "count": len(got),
                     "today": TODAY.isoformat(),
                     "term": {"academic_year": TERM[0], "from": TERM[1].isoformat(), "to": TERM[2].isoformat()},
                     "events": got},
            "blocks": blocks,
            "trace": [(AGENT, "calendar_read", f"{len(got)} entr{'y' if len(got) == 1 else 'ies'} {label} · {whose}")]}


def _event_table(con, ev, title):
    s = _subject(con, ev["subject"]) if ev.get("subject") else None
    e = dict(ev, kind_label=KINDS[ev["kind"]], subject_name=s["name"] if s else None)
    return B_table(["", ""], detail_rows(e), title=title)


def t_save_calendar_event(con, S, U, title=None, kind=None, date=None, end_date=None, start_time=None,
                          end_time=None, venue=None, description=None, scope=None, dept=None, sem=None,
                          section=None, subject=None, academic_year=None, event_id=None, **kw):
    con.execute(DDL)
    given = {"title": title, "kind": kind, "date": date, "end_date": end_date, "start_time": start_time,
             "end_time": end_time, "venue": venue, "description": description, "scope": scope, "dept": dept,
             "sem": sem, "section": section, "subject": subject, "academic_year": academic_year}
    old = None
    if event_id not in (None, ""):
        old = find(con, event_id)
        if not old:
            return _err(f"There is no calendar entry {event_id}.")
        if not old["editable"]:
            return _err(f"{old['title']} comes from the {'SEE timetable' if old['source'] == 'see' else 'CIE scheme'}"
                        f" and is changed there, not on the calendar.")
        if old["status"] != "Active":
            return _err(f"Entry #{old['id']} was cancelled; add it again instead.")
        # what is not given is kept; an empty string clears it
        merged = {k: (old.get(k) if v is None else v) for k, v in given.items()}
        if academic_year is None:
            merged["academic_year"] = None              # derived from the (possibly new) date
        if scope is None and any(given[k] is not None for k in ("dept", "sem", "section")):
            merged["scope"] = None                      # a new audience brings its own scope
        given = merged
    ev, why = resolve(con, given, self_id=old["id"] if old else None)
    if why:
        return _err(why)

    n = reach_count(con, ev)
    blocks = [_event_table(con, ev, "Calendar entry as it will read" if not old else
                           f"Entry #{old['id']} as it will read")]
    if old:
        changed = [[f, str(old.get(f) if old.get(f) is not None else "—"), str(ev[f] if ev[f] is not None else "—")]
                   for f in FIELDS if f != "academic_year" and (old.get(f) or None) != (ev[f] or None)]
        if not changed:
            return _err(f"Nothing would change: entry #{old['id']} already reads like that.")
        blocks.append(B_table(["Field", "Now", "Becomes"], changed, title="What changes"))
    blocks.append(B_text(f"**Who will see it:** {for_whom(ev)}: {n} student(s) on the rolls, and the teachers "
                         f"of {ev['dept'] or 'every department'}."
                         + ("" if ev["scope"] == "college" else " Nobody else's calendar shows it.")))
    args = {k: v for k, v in ev.items() if k not in ("academic_year",)}
    if old:
        args["event_id"] = old["id"]
    summary = (f"{'update' if old else 'add'} {ev['title']} ({KINDS[ev['kind']]}) on {_day(ev['date'])} "
               f"for {audience(ev)}")
    tr = [(AGENT, "event_plan", f"{summary[:110]} · reaches {n} student(s)")]
    if not approved_this_turn(U):
        return _propose(S, "save_calendar_event", args, blocks, summary, tr)

    now = dt.datetime.now().isoformat(timespec="seconds")
    if old:
        con.execute(f"UPDATE calendar_events SET {', '.join(f + '=?' for f in FIELDS)}, updated_by=?, updated_at=? "
                    f"WHERE id=?", [ev[f] for f in FIELDS] + [ACTOR, now, old["id"]])
        rid = old["id"]
    else:
        rid = _insert(con, ev, ACTOR, now)
    con.commit()
    back = one(con, "SELECT * FROM calendar_events WHERE id=?", (rid,))
    ok = bool(back and back["status"] == "Active" and all(back[f] == ev[f] for f in FIELDS)
              and reach_count(con, back) == n)
    Auditor().log(con, ACTOR, AGENT, "calendar.update" if old else "calendar.add",
                  {"id": rid, **{k: v for k, v in ev.items() if v is not None}}, "Applied" if ok else "Failed")
    _done(S, "save_calendar_event")
    return {"data": {"saved": ok, "id": rid, "audience": audience(ev), "students": n},
            "blocks": [_event_table(con, ev, f"Entry #{rid}: {ev['title']}"),
                       B_text(f"**{'Updated' if old else 'Added'}** as #{rid}. " +
                              (f"It is on everyone's calendar ({n} students)." if ev["scope"] == "college" else
                               f"It is on the calendar of {audience(ev)} ({n} student(s)) and nobody else's."))],
            "refresh": True, "chips": ["Show the academic calendar"],
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           (AGENT, "verify", f"#{rid} re-read: fields and audience as approved" if ok else
                            f"#{rid} does not read back as approved", "ok" if ok else "error")]}


def t_cancel_calendar_event(con, S, U, event_id="", reason=None, **kw):
    con.execute(DDL)
    e = find(con, event_id)
    if not e:
        return _err(f"There is no calendar entry {event_id or '(none given)'}. Give its number, e.g. 12.")
    if not e["editable"]:
        return _err(f"{e['title']} comes from the {'SEE timetable' if e['source'] == 'see' else 'CIE scheme'} "
                    f"and is changed there, not on the calendar.")
    if e["status"] != "Active":
        return _err(f"Entry #{e['id']} is already cancelled.")
    n = reach_count(con, e)
    blocks = [B_table(["", ""], detail_rows(e), title=f"Entry #{e['id']}: {e['title']}"),
              B_text(f"Cancelling takes it off the calendar of {audience(e)} ({n} student(s)). "
                     f"The record is kept, marked cancelled.")]
    args = {"event_id": e["id"], "reason": reason or None}
    summary = f"cancel #{e['id']} {e['title']} on {_day(e['date'])} for {audience(e)}"
    tr = [(AGENT, "cancel_plan", summary[:120])]
    if not approved_this_turn(U):
        return _propose(S, "cancel_calendar_event", args, blocks, summary, tr)
    now = dt.datetime.now().isoformat(timespec="seconds")
    con.execute("UPDATE calendar_events SET status='Cancelled', updated_by=?, updated_at=? WHERE id=?",
                (ACTOR, now, e["id"]))
    con.commit()
    back = one(con, "SELECT status FROM calendar_events WHERE id=?", (e["id"],))
    gone = not any(str(x["id"]) == str(e["id"]) for x in entries(con, e["date"], e["date"]))
    ok = bool(back and back["status"] == "Cancelled" and gone)
    Auditor().log(con, ACTOR, AGENT, "calendar.cancel", {"id": e["id"], "reason": reason}, "Applied" if ok else "Failed")
    _done(S, "cancel_calendar_event")
    return {"data": {"cancelled": ok, "id": e["id"]},
            "blocks": [B_text(f"**Cancelled** #{e['id']} {e['title']}. It no longer shows on any calendar.")],
            "refresh": True, "chips": ["Show the academic calendar"],
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           (AGENT, "verify", "re-read: cancelled and off every calendar" if ok else
                            "still shows after cancelling", "ok" if ok else "error")]}


# ================================================================= routing
# A question about the calendar, as a student or a teacher asks it. Shared by
# the portal and the Registrar's rule engine.
QUESTION_RX = nlu.CAL_QUESTION_RX


def question_args(text, ent=None):
    """utterance -> calendar_events arguments. A class the sentence names is
    passed on, so access.py can refuse a student someone else's calendar
    rather than this quietly answering with their own."""
    t = text.lower()
    a = {}
    if re.search(r"\binternals?\b|\bcie\b|\bia\b|ia ?[12]\b|ia test", t):
        a["kind"] = "cie"
    elif re.search(r"\bsee\b|semester exam|end[- ]sem|semester[- ]end", t):
        a["kind"] = "see"
    elif re.search(r"\bpracticals?\b", t):
        a["kind"] = "practical"
    elif re.search(r"\blab (?:exams?|tests?)\b", t):
        a["kind"] = "lab"
    elif re.search(r"\bexams?\b|\btests?\b|\bpapers?\b", t):
        a["kind"] = "exams"
    elif re.search(r"\bholidays?\b", t):
        a["kind"] = "holiday"
    elif re.search(r"\bevents?\b|\bfests?\b|\bcelebrations?\b", t):
        a["kind"] = "event"
    d = None
    try:
        d = parse_date(text)
    except ValueError:
        pass
    if d:
        a["date"] = d.isoformat()
    elif re.search(r"\b(?:this|the) week\b", t):
        mon = TODAY - dt.timedelta(days=TODAY.weekday())
        a["from_date"], a["to_date"] = mon.isoformat(), (mon + dt.timedelta(days=6)).isoformat()
    elif "next week" in t:
        mon = TODAY - dt.timedelta(days=TODAY.weekday()) + dt.timedelta(days=7)
        a["from_date"], a["to_date"] = mon.isoformat(), (mon + dt.timedelta(days=6)).isoformat()
    elif re.search(r"\b(?:this|next) month\b", t):
        a["month"] = "next month" if "next month" in t else "this month"
    elif any(w in MONTH_FULL and (w != "may" or re.search(r"\b(?:in|of) may\b", t))
             for w in re.findall(r"[a-z]+", t)):
        a["month"] = next(w for w in re.findall(r"[a-z]+", t) if w in MONTH_FULL)
    elif re.search(r"\b(?:next|upcoming|coming|when|soon|left)\b", t):
        a["upcoming"] = True
    c = nlu.cohort(text, ent or {})
    for k in ("dept", "sem", "section"):
        if c.get(k):
            a[k] = c[k]
    return a


WRITE_RX, CANCEL_RX, EDIT_RX = nlu.CAL_WRITE_RX, nlu.CAL_CANCEL_RX, nlu.CAL_EDIT_RX
SUBJ_RX = re.compile(r"\b(b[a-z]{2,3}l?\d{3})\b", re.I)
TIME_RX = re.compile(r"\b(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2})\b", re.I)


def event_args(text, ent=None):
    """'Add a CSE-7A internal exam for BCS702 on 29 Sep 9:30am to 10:30am in
    room MB-101' -> save_calendar_event arguments. What it cannot find it leaves
    out, and the tool asks for it; it never fills in an audience."""
    t = text.lower()
    a = {}
    q = re.search(r"\"([^\"]{3,120})\"|“([^”]{3,120})”|(?<![a-z])'([^']{3,120})'(?![a-z])", text, re.I)
    if q:
        a["title"] = next(g for g in q.groups() if g).strip()
    a["kind"] = parse_kind(t.replace(q.group(0).lower(), " ") if q else t) or ""
    ds = [m.group(0) for m in re.finditer(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?"
                                          + _MON + r"(?:\s+\d{4})?|\b" + _MON + r"\s+\d{1,2}(?:st|nd|rd|th)?"
                                          r"(?:,?\s+\d{4})?", t)]
    try:
        if ds:
            a["date"] = parse_date(ds[0]).isoformat()
            if len(ds) > 1:
                a["end_date"] = parse_date(ds[1]).isoformat()
        elif parse_date(t):
            a["date"] = parse_date(t).isoformat()
    except ValueError:
        a["date"] = ds[0] if ds else ""
    times = TIME_RX.findall(re.sub(r"\b\d{4}-\d{1,2}-\d{1,2}\b", " ", t))
    if times:
        a["start_time"] = times[0]
        if len(times) > 1:
            a["end_time"] = times[1]
    m = SUBJ_RX.search(text)
    if m:
        a["subject"] = m.group(1).upper()
    if re.search(r"college[- ]wide|whole college|entire college|all students|everyone|every student", t):
        a["scope"] = "college"
    c = nlu.cohort(text, ent or {})
    for k in ("dept", "sem", "section"):
        if c.get(k):
            a[k] = c[k]
    v = re.search(r"\b(?:in|at|venue:?)\s+(?:the\s+)?((?:[a-z0-9\-]+\s+){0,3}(?:auditorium|hall|grounds?|lab|room|"
                  r"block|campus|library|stadium|court)(?:\s+[a-z0-9\-]+)?)", text, re.I)
    if v:
        a["venue"] = v.group(1).strip()
    return {k: v for k, v in a.items() if v not in (None, "") or k == "kind"}


INTENTS = {"calendar"}


def rule_route(con, intent, text, ent):
    """The Registrar's rule engine: utterance -> (tool, args). Execution goes
    through tools.execute like every other caller."""
    m = CANCEL_RX.search(text)
    if m:
        return "cancel_calendar_event", {"event_id": m.group(1)}
    m = EDIT_RX.search(text)
    if m:
        a = event_args(text, ent)
        a = {k: v for k, v in a.items() if k in ("date", "start_time", "end_time", "venue", "title") and v}
        if "date" not in a and parse_date(text):
            a["date"] = parse_date(text).isoformat()
        return "save_calendar_event", {"event_id": m.group(1), **a}
    if WRITE_RX.search(text):
        return "save_calendar_event", event_args(text, ent)
    a = question_args(text, ent)
    m = re.search(r"\b(?:event|entry)\s*#?\s*(\d{1,5})\b", text, re.I)
    if m:
        return "calendar_events", {"event_id": m.group(1)}
    if ent.get("usn"):
        a["usn"] = ent["usn"]
    return "calendar_events", a


# ================================================================= registry
def _p(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_S, _I, _B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
_KIND = {"type": "string", "description": "cie, see, practical, lab, exam, event, holiday or other"}

REGISTRY = [
    (t_calendar_events, "calendar_events",
     "The academic calendar: holidays, college events and exams (CIE, SEE, practical, lab), each shown only to "
     "the classes it is for. A month (YYYY-MM, default this month), one date, a from/to range, or upcoming=true "
     "for what is next. usn = one student's own calendar; dept/sem/section = a class's. kind narrows it "
     "('exams' for every exam). event_id = one entry's details.",
     _p({"month": _S, "date": _S, "from_date": _S, "to_date": _S, "kind": _S, "upcoming": _B, "usn": _S,
         "dept": _S, "sem": _I, "section": _S, "event_id": _S})),
    (t_save_calendar_event, "save_calendar_event",
     "Add an entry to the academic calendar, or with event_id change one. The audience must be named: "
     "dept/sem/section (e.g. CSE, 7, A), or scope='college' for everyone - nothing is assumed college-wide, "
     "and an exam must name its class. Validates the class, the subject's class, times, the term, holidays and "
     "clashes, and says how many students it reaches. Without admin approval this turn it only PROPOSES.",
     _p({"title": _S, "kind": _KIND, "date": _S, "end_date": _S, "start_time": _S, "end_time": _S, "venue": _S,
         "description": _S, "scope": _S, "dept": _S, "sem": _I, "section": _S, "subject": _S, "event_id": _S},
        ["kind", "date"])),
    (t_cancel_calendar_event, "cancel_calendar_event",
     "Cancel an entry on the academic calendar by its number; the record is kept, marked cancelled. SEE and CIE "
     "dates come from their own timetables and are not cancelled here. Without approval this turn it only PROPOSES.",
     _p({"event_id": _S, "reason": _S}, ["event_id"])),
]
GATED_WRITES = ("save_calendar_event", "cancel_calendar_event")
AGENT_OF = {"calendar_events": AGENT, "save_calendar_event": AGENT, "cancel_calendar_event": AGENT}
TOOL_GROUPS = {"calendar": ["calendar_events", "save_calendar_event", "cancel_calendar_event", "exam_schedule"]}
