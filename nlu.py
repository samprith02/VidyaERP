"""
VidyaERP :: NLU layer
Lightweight, dependency-free natural-language understanding for the ERP copilot:
intent scoring, entity resolution (faculty / dept / sem / section / subject / student),
and Indian-context date parsing ("tomorrow", "next monday", "12 sep", "9/9").
"""
import re, difflib, functools, datetime as dt

TODAY = dt.date(2026, 9, 4)          # demo "system date" (Friday)
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
DAY_FULL = {"monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu",
            "friday": "Fri", "saturday": "Sat", "sunday": "Sun",
            "mon": "Mon", "tue": "Tue", "tues": "Tue", "wed": "Wed", "thu": "Thu",
            "thur": "Thu", "thurs": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

# ------------------------------------------------------------------ intents
INTENTS = {
    "absence.cover": {
        "kw": [("absent", 5), ("on leave", 4), ("won't be", 3), ("wont be", 3), ("not coming", 4),
               ("unavailable", 4), ("substitute", 5), ("substitution", 5), ("cover", 3),
               ("reschedule", 4), ("arrange", 3), ("replacement", 4), ("sick", 3),
               ("emergency leave", 5), ("proxy", 4), ("adjust classes", 4),
               ("undo", 6), ("rollback", 6), ("roll back", 6), ("revert", 6)],
        "desc": "Handle faculty absence → build substitution / reschedule plan"},
    "timetable.view": {
        "kw": [("timetable", 5), ("time table", 5), ("schedule of", 3), ("classes for", 3),
               ("who is teaching", 4), ("which class", 3), ("period", 2),
               ("free faculty", 5), ("who is free", 5), ("available faculty", 4), ("free slot", 4),
               ("room availability", 4), ("free room", 4), ("vacant", 3)],
        "desc": "Read timetable, workload, free faculty/rooms"},
    "timetable.generate": {
        "kw": [("generate timetable", 6), ("create timetable", 6), ("build timetable", 6),
               ("new timetable", 5), ("regenerate", 6), ("rebuild", 5), ("from scratch", 5),
               ("re-generate", 6), ("timetable generation", 6), ("run the solver", 5),
               ("prepare the timetable", 5)],
        "desc": "Build a timetable from scratch with the constraint solver"},
    "student.query": {
        "kw": [("student", 4), ("usn", 5), ("attendance", 5), ("defaulter", 5), ("shortage", 4),
               ("cgpa", 4), ("backlog", 4), ("mentor", 3), ("hostel", 3), ("condonation", 4),
               ("below 75", 5), ("detained", 4), ("marks", 3)],
        "desc": "Student records, attendance shortage, academic risk"},
    "faculty.query": {
        "kw": [("faculty", 4), ("teacher", 4), ("professor", 3), ("staff", 3), ("hod", 4),
               ("designation", 3), ("teaching load", 5), ("expertise", 3), ("who handles", 4),
               ("who teaches", 4), ("contact", 2), ("phone", 2), ("email", 2)],
        "desc": "Faculty directory, load, subject allocation"},
    "finance.query": {
        "kw": [("fee", 5), ("fees", 5), ("dues", 5), ("pending payment", 4), ("collection", 4),
               ("revenue", 4), ("budget", 4), ("scholarship", 4), ("outstanding", 4), ("crore", 2),
               ("lakh", 2), ("purchase", 2), ("expenditure", 4)],
        "desc": "Fee dues, collections, budget"},
    "exam.query": {
        "kw": [("exam", 5), ("see ", 3), ("cie", 4), ("internal", 3), ("hall ticket", 5),
               ("invigilation", 5), ("results", 3), ("valuation", 4), ("exam schedule", 5),
               ("time table for exam", 5)],
        "desc": "Examination schedule, invigilation, eligibility"},
    "request.manage": {
        "kw": [("request", 4), ("approve", 5), ("reject", 5), ("pending approval", 5),
               ("inbox", 4), ("sanction", 4), ("raise a", 4), ("send a request", 5),
               ("apply for", 4), ("forward to", 4), ("escalate", 4), ("nod", 2), ("permission", 3)],
        "desc": "Approvals inbox, raise/route requests"},
    "notify.broadcast": {
        "kw": [("notify", 5), ("inform", 4), ("announce", 5), ("broadcast", 5), ("circular", 5),
               ("send message", 5), ("sms", 3), ("whatsapp", 3), ("alert", 3), ("intimate", 4)],
        "desc": "Push notices to students / faculty / parents"},
    "analytics.kpi": {
        "kw": [("kpi", 5), ("summary", 4), ("overview", 4), ("dashboard", 4), ("how is", 2),
               ("statistics", 4), ("report", 4), ("naac", 5), ("nba", 4), ("accreditation", 5),
               ("placement", 4), ("trend", 3), ("brief me", 5), ("status of college", 5),
               ("risk", 3), ("insight", 4)],
        "desc": "Institution analytics, NAAC/NBA metrics, risk radar"},
    # ---- accreditation.py ----
    "accreditation": {
        "kw": [("evidence pack", 8), ("self study report", 8), ("criterion-wise", 6), ("criteria-wise", 6)],
        "desc": "NAAC / NBA evidence computed from the records, with the gaps named (#29)"},
    # ---- campus services (campus.py) ----
    "ops.radar": {
        "kw": [("autopilot", 8), ("needs my attention", 8), ("need my attention", 8),
               ("action items", 8), ("what should i", 5), ("to-do", 5), ("todo", 5),
               ("anything urgent", 6), ("ops radar", 8), ("what is pending", 4)],
        "desc": "Ask every agent what needs a human decision today"},
    "student.360": {
        "kw": [("360", 8), ("everything about", 7), ("full profile", 5), ("complete profile", 5),
               ("subject-wise attendance", 8), ("subject wise attendance", 8)],
        "desc": "One student across every ledger"},
    "library": {
        "kw": [("library", 7), ("book", 5), ("books", 5), ("borrow", 5), ("overdue", 3),
               ("catalogue", 6), ("textbook", 5), ("librarian", 6)],
        "desc": "Catalogue, circulation, overdue fines"},
    "hostel": {
        "kw": [("hostel", 7), ("warden", 5), ("mess", 3), ("room allot", 6), ("waitlist", 4),
               ("complaint", 3)],
        "desc": "Occupancy, allotment, complaints"},
    "transport": {
        "kw": [("bus", 6), ("buses", 6), ("transport", 7), ("route", 2), ("driver", 4),
               ("pickup", 4), ("broke down", 6), ("breakdown", 6)],
        "desc": "Routes, loads, breakdowns"},
    "gatepass": {
        "kw": [("gate pass", 9), ("gatepass", 9), ("outing", 6), ("out pass", 7), ("home visit", 6)],
        "desc": "Outing / home-visit passes screened against policy"},
    "placement": {
        "kw": [("placement", 6), ("drive", 4), ("recruit", 5), ("shortlist", 6), ("offer", 3),
               ("ctc", 4), ("lpa", 3), ("eligible for", 3)],
        "desc": "Drives, eligibility, shortlists"},
    "document": {
        "kw": [("certificate", 7), ("bonafide", 9), ("no dues", 9), ("no-dues", 9), ("noc", 7),
               ("transfer certificate", 9), ("clearance", 5)],
        "desc": "No-dues clearance and certificates"},
    "leave.review": {
        "kw": [("leave application", 8), ("leave applications", 8), ("pending leave", 8),
               ("leave requests", 7)],
        "desc": "Pending faculty leave priced against coverage"},
    # ---- academics.py ----
    "availability": {
        "kw": [("availability", 8), ("standing", 4), ("every week", 4), ("visiting faculty", 6)],
        "desc": "Standing weekly windows a teacher cannot be timetabled in (#19)"},
    "cie": {
        "kw": [("internal marks", 9), ("internal assessment", 9), ("ia marks", 8), ("ia1", 7),
               ("ia2", 7), ("ia 1", 7), ("ia 2", 7), ("ia test", 8), ("assignment marks", 7), ("lab record", 6),
               ("marks entry", 9), ("marks", 4)],
        "desc": "Internal marks: entry, standing, students at risk of the CIE minimum (#32)"},
    # ---- academic_calendar.py ----
    "calendar": {
        "kw": [("academic calendar", 8), ("college event", 6), ("college events", 6), ("ia re-test", 6),
               ("lab exam", 4), ("practical exam", 4)],
        "desc": "The academic calendar: holidays, events and exams, each for the classes it names (#104)"},
}

CONFIRM_YES = ["yes", "yeah", "yep", "ok", "okay", "approve", "apply", "confirm", "go ahead",
               "do it", "proceed", "accept", "sure", "publish", "commit"]
CONFIRM_NO = ["no", "cancel", "abort", "discard", "don't", "dont", "stop", "reject", "drop it"]

# Regex rules that dominate keyword scoring (verb-first phrasing, disambiguation)
PRIORITY = [
    # the verb must end there: "Information Science ..." is a department, not an order (#68)
    (r"^\s*(?:please\s+|pls\s+)?(?:notify|inform|announce|broadcast|circulate|alert|"
     r"send (?:a |an )?(?:message|sms|notice|circular|whatsapp))\b", "notify.broadcast", 14),
    # A broadcast that names its audience is a broadcast whatever its message
    # mentions (#87). Without this, "notify all students that the library closes
    # at 6" scored library +16 over notify +14, and one variant staged overdue
    # reminders: a different gated write that the admin's "yes" would commit.
    # Worth more than any campus rule. "Notify overdue borrowers" has no
    # audience phrase and still belongs to the library.
    (r"^\s*(?:please\s+|pls\s+)?(?:notify|inform|announce|broadcast|circulate|alert|"
     r"send (?:a |an )?(?:message|sms|notice|circular|whatsapp))"
     r"(?:\s*:|\s+(?:to\s+)?(?:everyone|everybody|"
     r"(?:all|every|the whole|the entire)\b[^.?!:]{0,40}?\b(?:students?|faculty|staff|teachers|"
     r"parents|hostellers|riders|campus|departments?|classes|sections?)\b))", "notify.broadcast", 30),
    (r"\b(?:who(?:'s| is| are)?\s+(?:all\s+)?free|are free|is free|free faculty|free staff|"
     r"available faculty|free (?:room|slot|hall|classroom)|room availability|which rooms)\b",
     "timetable.view", 12),
    (r"\b(?:work ?load|teaching load|utilis|utiliz|over ?loaded)\b", "faculty.query", 12),
    # whole words: "nba" sits inside "unbalanced" (#29)
    (r"\b(?:naac|nba|accreditation|accreditations|iqac|aqar|ssr)\b", "accreditation", 18),
    # the early-warning list (#79); not bare "at risk" - that is also CIE's phrase
    (r"\b(?:academic(?:ally)? (?:at )?risk|early[- ]warning|dropouts?|counsell?ing (?:list|report)|"
     r"mentor[- ]?wise)\b", "student.query", 14),
    (r"\b(?:is|are|will be)\s+absent\b|\bon leave (?:tomorrow|today|on|from)\b|"
     r"\barrange (?:a )?(?:substitute|cover|coverage|proxy)\b", "absence.cover", 14),
    (r"\b(?:who(?:'s| is| are)?\s+on leave|list .*leaves?|leave (?:this week|calendar|ledger)|"
     r"anyone else on leave)\b", "absence.cover", 10),
    (r"^\s*(?:approve|reject|sanction)\b|\bpending approvals?\b|\bbulk approve\b", "request.manage", 12),
    (r"\b(?:undo|roll ?back|revert)\b", "absence.cover", 12),
    (r"\b(?:generate|regenerate|re-generate|rebuild|re-build|build|create|prepare|draw up)\b"
     r"[^.?!]{0,40}\b(?:time ?table|schedule)\b|"
     r"\btime ?table\b[^.?!]{0,25}\bfrom scratch\b", "timetable.generate", 16),
    (r"\btime ?table\b", "timetable.view", 8),
    (r"\bmake-?ups?\b.*\b(?:class|classes|session|schedule|booked)\b|\bbooked make-?ups?\b|\bextra class",
     "timetable.view", 14),
    (r"\battendance\b.*\b(?:defaulter|shortage|below|less than|<)\b", "student.query", 10),
    # ---- campus services. Bonuses clear the generic verb rules above ("approve"
    # +12 for request.manage), because "approve gate pass 12" is a gate pass.
    (r"\bgate ?pass(?:es)?\b|\bout ?pass(?:es)?\b|\bouting\b|\bhome visit\b", "gatepass", 18),
    (r"\bno[- ]?dues?\b|\bbona ?fide\b|\btransfer certificate\b|\bcertificates?\b|\bnoc\b",
     "document", 18),
    (r"\blibrary\b|\bbk\d{3,4}\b|\boverdue (?:books?|loans?)\b|\bbooks?\b.*\b(?:issue|return|lend)|"
     r"\b(?:issue|return|lend)\b.*\bbooks?\b", "library", 16),
    (r"\bhostels?\b|\bwarden\b|\broom allot", "hostel", 14),
    (r"\bbus(?:es)?\b|\btransport\b|\broute\s*r?\d\b|\bbroke ?down\b|\bbreakdown\b", "transport", 16),
    (r"\bplacements?\b|\b(?:recruitment|campus) drive\b|\bdrive\b.*\b(?:eligib|shortlist)|"
     r"\bshortlist\b", "placement", 14),
    (r"\bleave (?:application|request)s?\b|\bpending (?:faculty )?leaves?\b|"
     r"\b(?:approve|reject|grant|sanction)\s+(?:all\s+)?(?:the\s+)?(?:pending\s+)?leaves?\b|"
     r"\breview (?:the )?(?:pending )?leaves?\b|\b(?:approve|reject) leave\s*#?\d", "leave.review", 20),
    # A STANDING window, not a one-off absence: it has to say "every" / "each" /
    # a plural weekday / "weekly", or talk about availability itself (#19).
    (r"\bavailability\b|\bavailable again\b|"
     r"\b(?:not available|unavailable|can(?:no|')?t teach|cannot teach|busy)\b[^.?!]{0,40}"
     r"\b(?:every|each|weekly|(?:mon|tues|wednes|thurs|fri|satur)days)\b", "availability", 22),
    # "cie" as a whole word: a keyword would fire inside "science" and "efficient" (#32)
    (r"\bcie\b", "cie", 9),
    # entering marks is a CIE write, whatever else the sentence mentions (#32)
    (r"\b(?:enter|record|upload|update|correct|change|fill|post)\b[^.?!]{0,40}\bmarks?\b|"
     r"\b(?:ia ?[12]|cie|internal)\b[^.?!]{0,30}\b4vp\d{2}[a-z]{2}\d{3}\s*[:=-]?\s*(?:\d|ab)", "cie", 20),
    (r"\bautopilot\b|\bneeds? (?:my )?attention\b|\baction items?\b|\bops radar\b|"
     r"\bwhat should i (?:do|act on|focus on|look at)\b|\bto-?do list\b", "ops.radar", 18),
    (r"(?:\b360\b|everything about|full profile|complete profile|subject[- ]wise attendance)"
     r".*\b4vp\d{2}[a-z]{2}\d{3}\b|\b4vp\d{2}[a-z]{2}\d{3}\b.*"
     r"(?:\b360\b|everything|full profile|complete profile|subject[- ]wise)", "student.360", 22),
    # "show me the details of <USN>" is the same request as "Student <USN> details";
    # without this it scored nothing and fell to smalltalk (#91).
    (r"\b(?:details?|record|profile) (?:of|for|on|about)\b[^.?!]*\b4vp\d{2}[a-z]{2}\d{3}\b", "student.query", 12),
    # A student's books, by USN, belong to the library, not to the 360 (#91).
    # "how much fine does <USN> have?" used to fall to smalltalk.
    (r"(?:\bborrow(?:ed|ing|s)?\b|\bon loan\b|\bloans?\b|\b(?:library )?fines?\b|\bbooks?\b)"
     r"[^.?!]*\b4vp\d{2}[a-z]{2}\d{3}\b|\b4vp\d{2}[a-z]{2}\d{3}\b[^.?!]*"
     r"(?:\bborrow(?:ed|ing|s)?\b|\bon loan\b|\bloans?\b|\bfines?\b|\bbooks?\b)", "library", 24),
]


# Verbs that are also the start of unrelated nouns: "inform" / "information",
# "drive" / "driver". These must END at a word too (an inflection allowed).
WHOLE_WORD = {"inform", "drive"}


@functools.lru_cache(maxsize=None)
def _kw_rx(kw):
    """A keyword must START a word (#68): as a bare substring "cie" fired inside
    "science" - two department names - and "hod" inside "methodology". It may
    still run on ("recruit" -> "recruitment"), except the WHOLE_WORD verbs."""
    tail = r"(?:s|ed|ing)?\b" if kw in WHOLE_WORD else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(kw) + tail)


def classify(text):
    """Return ranked list of (intent, score, matched_keywords). Regex priority rules
    break ties that pure keyword counting gets wrong (e.g. 'notify students about exams')."""
    t = " " + text.lower() + " "
    scores, hitmap = {}, {}
    for intent, cfg in INTENTS.items():
        s, hits = 0, []
        for kw, w in cfg["kw"]:
            if _kw_rx(kw).search(t):
                s += w
                hits.append(kw)
        if s:
            scores[intent] = s
            hitmap[intent] = hits
    for rx, intent, bonus in PRIORITY:
        if re.search(rx, t):
            scores[intent] = scores.get(intent, 0) + bonus
            hitmap.setdefault(intent, []).append("rule:" + rx[:24])
    out = [(i, s, hitmap[i]) for i, s in scores.items()]
    out.sort(key=lambda x: -x[1])
    return out


# ------------------------------------------------------------------ dates
def parse_date(text, today=None):
    """Return (date, human_label) or (None, None)."""
    today = today or TODAY
    t = text.lower()
    # A written calendar date is the most specific thing in the sentence, so it
    # wins over every relative phrase (#17). "Absent on Monday 14 Sep" used to
    # take the "on Monday" branch and return the 7th - a week early. When the
    # weekday word names a different day, date_conflict() says so and the
    # caller asks rather than guessing. The bare numeric dd-mm form is NOT
    # promoted: "for 2-3 days" would become 2 March.
    d = _explicit_date(t, today)
    if d:
        return d
    if "day after tomorrow" in t:
        d = today + dt.timedelta(days=2); return d, "day after tomorrow"
    if "tomorrow" in t or "tmrw" in t:
        d = today + dt.timedelta(days=1); return d, "tomorrow"
    if "today" in t:
        return today, "today"
    if "yesterday" in t:
        return today - dt.timedelta(days=1), "yesterday"
    m = re.search(r"\b(next|this|coming)\s+(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*", t)
    if m:
        target = DAY_FULL[m.group(2)]
        idx = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].index(target)
        # "next Monday", "this Monday", "coming Monday" and "on Monday" all mean
        # the SAME thing: the next Monday to occur. `next` used to add a further
        # week, so on Fri 04 Sep it skipped the 7th and returned the 14th.
        #
        # Three reasons that went:
        #   * it already collapsed for one weekday in seven - when the target is
        #     today's own weekday both readings land +7, so the distinction was
        #     never honoured consistently anyway;
        #   * the failure costs are asymmetric. Resolving a week LATE leaves the
        #     real absence uncovered on the day it happens, with nothing to
        #     notice it; resolving early is caught by the date echoed back in
        #     the proposal before anything commits;
        #   * the coming Monday is the dominant reading, and more so in Indian
        #     English than in the one the old branch encoded.
        # Today never counts as "the next one" - say "today" for that.
        delta = (idx - today.weekday()) % 7 or 7
        d = today + dt.timedelta(days=delta)
        return d, f"{m.group(1)} {target}"
    m = re.search(r"\bon\s+(mon|tue|tues|wed|thu|thur|thurs|fri|sat)[a-z]*", t)
    if m:
        target = DAY_FULL[m.group(1)]
        idx = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].index(target)
        delta = (idx - today.weekday()) % 7 or 7
        d = today + dt.timedelta(days=delta)
        return d, target
    # "gate pass for Saturday", "timetable for Monday" (#61). Stricter than the
    # `on` branch: the weekday must be a whole word, so "for months" and
    # "for Mondays" (a standing pattern, not a date) do not match. Like `on`,
    # no Sunday - the college does not run on Sunday.
    m = re.search(r"\bfor\s+(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|"
                  r"friday|fri|saturday|sat)\b", t)
    if m:
        return next_occurrence(m.group(1), today), DAY_FULL[m.group(1)]
    m = re.search(r"(?<![\d/-])\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b(?![/-]\d)", t)
    if m:
        day, mon = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3) or today.year)
        yr = yr + 2000 if yr < 100 else yr
        try:
            return dt.date(yr, mon, day), f"{day:02d}-{mon:02d}"
        except ValueError:
            return None, None
    return None, None


def _explicit_date(t, today):
    """(date, label) for a written calendar date, (None, None) for an
    impossible one, or None when the text holds no calendar date at all."""
    # ISO first (#55). The dd-mm pattern in parse_date used to match the
    # "09-08" inside "2026-09-08" and return 9 August - a Sunday, so the reply
    # said no cover was needed. ISO is what the console's own date fields produce.
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None, None
        return d, d.strftime("%d %b")
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", t)
    if m:
        day, mon = int(m.group(1)), MONTHS[m.group(2)]
        yr = today.year if (mon, day) >= (today.month, today.day) else today.year + 1
        try:
            return dt.date(yr, mon, day), f"{day} {m.group(2).title()}"
        except ValueError:
            return None, None
    return None


WEEKDAY_WORD = re.compile(r"\b(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|"
                          r"friday|fri|saturday|sat|sunday|sun)\b")


def next_occurrence(word, today=None):
    """The next date falling on weekday `word` ('sat', 'Saturday'); today never
    counts as the next one. Sunday included - callers decide whether it is a day."""
    today = today or TODAY
    idx = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].index(DAY_FULL[word.lower()])
    return today + dt.timedelta(days=(idx - today.weekday()) % 7 or 7)


def date_conflict(text, d):
    """The weekday the text names, when it is not the day `d` falls on (#17).

    "Absent Monday 15 Sep" when the 15th is a Tuesday is two different
    instructions; picking either one silently covers the wrong day. Returns
    None when the text names no weekday or names d's own weekday."""
    if not d:
        return None
    said = {DAY_FULL[w] for w in WEEKDAY_WORD.findall(text.lower())}
    if not said or day_of(d) in said:
        return None
    return sorted(said, key=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].index)[0]


def date_range(text, today=None):
    """Detect 'from X to Y' / 'for 3 days'."""
    today = today or TODAY
    d1, lbl = parse_date(text, today)
    m = re.search(r"for\s+(\d+)\s+day", text.lower())
    if d1 and m:
        return d1, d1 + dt.timedelta(days=int(m.group(1)) - 1)
    m = re.search(r"(\d{1,2})\s*(?:to|-|–|till|until)\s*(\d{1,2})\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                  text.lower())
    if m:
        mon = MONTHS[m.group(3)]
        y = today.year
        return dt.date(y, mon, int(m.group(1))), dt.date(y, mon, int(m.group(2)))
    return d1, d1


def day_of(d):
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d.weekday()]


# ------------------------------------------------------------------ entities
def extract_periods(text):
    t = text.lower()
    ps = set()
    for m in re.finditer(r"(?:period|hour|slot)s?\s*#?\s*(\d)", t):
        ps.add(int(m.group(1)))
    for m in re.finditer(r"(\d)(?:st|nd|rd|th)\s+(?:period|hour)", t):
        ps.add(int(m.group(1)))
    return sorted(p for p in ps if 1 <= p <= 7)


def extract_dept(text):
    t = text.upper()
    alias = {"CSE": ["CSE", "COMPUTER SCIENCE", "COMP SCI", "CS DEPT", " CS "],
             "ISE": ["ISE", "INFORMATION SCIENCE", " IS DEPT"],
             "ECE": ["ECE", "ELECTRONICS", "E&C", "EC DEPT"],
             "MECH": ["MECH", "MECHANICAL", " ME DEPT"],
             "CIVIL": ["CIVIL", " CV DEPT"]}
    for code, keys in alias.items():
        for k in keys:
            # a whole word only: "ISE" sits inside advise, otherwise and
            # mentor-wise, which once scoped those answers to ISE (#80)
            # ("CSE5A" still counts; the short spaced aliases like " CS " keep
            # digits out too, or the CS inside a USN would read as a department)
            guard = "A-Z0-9" if k != k.strip() else "A-Z"
            if re.search(rf"(?<![{guard}])" + re.escape(k.strip()) + rf"(?![{guard}])", t):
                return code
    return None


def extract_sem(text):
    # 9 is read too, so that "sem 9" reaches a tool that says no such semester
    # runs, rather than being dropped and answered for the whole department (#94)
    m = re.search(r"(?:sem|semester)\s*[-: ]?\s*([1-9])(?!\d)", text.lower())
    if m:
        return int(m.group(1))
    m = re.search(r"\b([1-9])(?:st|nd|rd|th)\s*(?:sem|semester)", text.lower())
    if m:
        return int(m.group(1))
    # the console's own way of naming a class: CSE-7A, CSE 7A, cse7a, CSE-7 (#98).
    # Without it the semester was None and "rebuild CSE-7A" proposed CSE-5A.
    # A bare "CSE 7" is not a class (it needs the hyphen or a section letter),
    # and a USN's "CS" is not a department.
    m = CLASS_RX.search(text.lower())
    if m:
        return int(m.group(1) or m.group(2))
    return None


CLASS_RX = re.compile(r"(?<![a-z0-9])(?:cse|ise|ece|mech|civil)(?:\s*-\s*([1-9])(?:\s*[a-h])?|\s*([1-9])\s*[a-h])"
                      r"(?![a-z0-9])")


def extract_section(text):
    m = re.search(r"\b(?:sec|section)\s*[-: ]?\s*([a-h])\b", text.lower())
    if m:
        return m.group(1).upper()
    m = re.search(r"\b[1-9]\s*(?:sem|semester)?\s*['\"]?([ab])\b", text.lower())
    if m:
        return m.group(1).upper()
    return None


# A class written as one token, for a department this college may not have:
# "EEE-3A" is a question about EEE, not about every department's 3A (#94).
ANY_CLASS_RX = re.compile(r"(?<![a-z0-9])([a-z]{2,5})\s*-?\s*([1-9])([a-h])(?![a-z0-9])")
NOT_DEPT = {"sem", "semes", "sec", "class", "top", "best", "the", "in", "of", "for", "year", "batch", "div",
            "room", "lab", "block", "bus", "route", "ia", "and", "to", "is", "at", "on", "p", "period"}


def cohort(text, ent=None):
    """dept / sem / section as the sentence names them - including a class
    that does not exist, so the tool can say so instead of widening."""
    ent = ent or {}
    a = {k: ent.get(k) for k in ("dept", "sem", "section") if ent.get(k)}
    m = ANY_CLASS_RX.search(text.lower())
    if m and m.group(1) not in NOT_DEPT:
        a.setdefault("dept", extract_dept(m.group(1)) or m.group(1).upper())
        a.setdefault("sem", int(m.group(2)))
        a.setdefault("section", m.group(3).upper())
    return a


# ----------------------------------------------- strength and ranking (#94, #95)
# Topics that own a "how many students ..." of their own: fees, hostel, books,
# a drive, attendance and CGPA each have a tool that counts their students.
_NOT_STRENGTH = (r"(?!.*\b(?:faculty|staff|teachers?|professors?|lecturers?|fees?|dues|hostels?|bus(?:es)?|"
                 r"library|books?|fines?|placed|placements?|offers?|drives?|backlogs?|attendance|defaulters?|"
                 r"cgpa|gpa|gate ?pass(?:es)?|eligib\w*|absent|risk)\b)")
STRENGTH_RX = re.compile(
    r"^" + _NOT_STRENGTH + r".*(?:\bstrength\b|\bhead ?counts?\b|\benrol(?:l)?ments?\b|\bclass size\b|"
    r"\b(?:number|no\.?|count|total) of students\b|\bstudents? (?:count|strength|numbers?)\b|"
    r"\bhow many students\b)", re.I)
RANK_RX = re.compile(
    r"^(?!.*\b(?:drives?|placements?|eligib\w*|attendance|books?|fines?|marks|internals?|cie|ia ?[12]?)\b).*(?:"
    r"\b(?:top|best|highest|bottom|lowest|toppers?|rank(?:ed|ing|s)?|merit list)\b[^.?!]{0,50}"
    r"\b(?:students?|cgpa|gpa|scorers?|performers?|rankers?)\b|\btoppers?\b|"
    r"\b(?:students?|names?|who)\b[^.?!]{0,60}\bc?gpa\b[^.?!]{0,12}\b(?:above|over|greater|more|higher|below|"
    r"under|less|lower|at least|atleast|at most|between|>|<)|"
    r"\bc?gpa\s*(?:of\s*)?(?:above|over|greater than|more than|higher than|below|under|less than|lower than|"
    r"at least|atleast|at most|between|>=?|<=?)\s*\d)", re.I)

# Scored like the PRIORITY rules: "What is the strength of the CSE 7th sem
# SEC A" matched no keyword at all and fell to smalltalk (#94).
PRIORITY += [(STRENGTH_RX.pattern, "student.query", 16), (RANK_RX.pattern, "student.query", 18)]

# ----------------------------------------------------- the academic calendar (#104)
# Whole month words only: a bare "mar[a-z]*" reads "5 marks" as 5 March.
CAL_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
           r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b")
# A question about dates: what is on, when the next one is, is there one that day.
CAL_QUESTION_RX = re.compile(
    r"\bcalendar\b|\bholidays?\b|\bevents?\b|\bfests?\b|\bhappening\b|\bwhat'?s on\b|"
    r"\b(?:next|upcoming|coming)\b[^?.!]{0,40}\b(?:exams?|tests?|ia|internals?|holidays?|events?|papers?)\b|"
    r"\b(?:exams?|tests?|papers?)\b[^?.!]{0,30}\b(?:this|next) (?:week|month|term)\b|"
    r"\b(?:exams?|tests?|papers?)\b[^?.!]{0,30}\bon\s+(?:\d|" + CAL_MON + r"|today|tomorrow|the\s+\d)|"
    r"\bdo i have (?:an? |any )?(?:exams?|tests?|ia|internals?|holiday|papers?)\b|"
    r"\bwhen(?:'s| is| are| do| will)\b[^?.!]{0,40}\b(?:exams?|tests?|ia|internals?|holidays?|events?|papers?)\b",
    re.I)
# Putting something on it. Never a request ("create a request for the hackathon
# event" is the inbox's), and the verb must start the sentence.
CAL_WRITE_RX = re.compile(
    r"^\s*(?:please\s+)?(?:add|create|schedule|declare|put|post|mark|set up|enter)\b(?![^.?!]*\brequests?\b)"
    r"[^.?!]{0,80}\b(?:holidays?|events?|exams?|ia (?:test|re-?test)|internals?|practicals?|lab exams?|"
    r"calendar|celebration|orientation|fest)\b", re.I)
CAL_CANCEL_RX = re.compile(r"\b(?:cancel|delete|remove|withdraw|call off)\b[^.?!]{0,40}\b(?:calendar )?"
                           r"(?:event|entry)\s*#?\s*(\d{1,5})\b", re.I)
CAL_EDIT_RX = re.compile(r"\b(?:move|reschedule|change|edit|update|postpone|prepone|shift)\b[^.?!]{0,30}"
                         r"\b(?:calendar )?(?:event|entry)\s*#?\s*(\d{1,5})\b", re.I)
PRIORITY += [(CAL_WRITE_RX.pattern, "calendar", 16), (CAL_CANCEL_RX.pattern, "calendar", 16),
             (CAL_EDIT_RX.pattern, "calendar", 16),
             # "leave calendar" is the leave ledger's (absence.cover)
             (r"(?<!leave )\bcalendar\b|\bholidays?\b", "calendar", 15)]

_NUM = r"(\d{1,2}(?:\.\d{1,2})?)"


def rank_args(text, ent=None):
    """'top 10 students with CGPA above 8 in CSE' -> top_students arguments.
    'above' is strict and 'at least' / '8 and above' is not; the tool says
    which one it used, because one CSE student has exactly 8.00."""
    t = text.lower()
    a = cohort(text, ent)
    m = (re.search(r"\b(?:top|best|bottom|lowest|first|last)\s+(\d{1,3})\b", t)
         or re.search(r"\b(\d{1,3})\s+(?:top |best |bottom |lowest )?(?:students|toppers|rankers|names|scorers)\b", t))
    if m:
        a["limit"] = int(m.group(1))
    elif re.search(r"\b(?:the )?(?:topper|highest|best student|top student|first rank|rank ?1)\b", t) \
            and not re.search(r"\b(?:toppers|students|names)\b", t):
        a["limit"] = 1
    # "least" alone, not "at least 8.5" - a floor, which once read as the bottom of the list
    if re.search(r"\b(?:bottom|lowest|worst|weakest)\b|(?<!at )\bleast\b", t):
        a["order"] = "bottom"
    if re.search(r"\bc?gpa\b|grade point|pointer", t):
        bounds = (("cgpa_at_least", r"(?:at ?least|minimum(?: of)?|not less than|>=)\s*" + _NUM),
                  ("cgpa_at_least", _NUM + r"\s*(?:and|or|&)\s*(?:above|more|higher|over)"),
                  ("cgpa_above", r"(?:above|over|greater than|more than|higher than|>)\s*" + _NUM),
                  ("cgpa_at_most", r"(?:at ?most|maximum(?: of)?|not more than|<=)\s*" + _NUM),
                  ("cgpa_at_most", _NUM + r"\s*(?:and|or|&)\s*(?:below|less|lower|under)"),
                  ("cgpa_below", r"(?:below|under|less than|lower than|<)\s*" + _NUM))
        b = re.search(r"between\s*" + _NUM + r"\s*(?:and|to|-)\s*" + _NUM, t)
        if b:
            a["cgpa_at_least"], a["cgpa_at_most"] = sorted((float(b.group(1)), float(b.group(2))))
        for key, rx in bounds:
            lo_side = key in ("cgpa_at_least", "cgpa_above")
            if b or any(k in a for k in (("cgpa_at_least", "cgpa_above") if lo_side
                                          else ("cgpa_at_most", "cgpa_below"))):
                continue
            m = re.search(rx, t)
            if m and float(m.group(1)) <= 10:
                a[key] = float(m.group(1))
    return a


def extract_usn(text):
    m = re.search(r"\b(4vp\d{2}[a-z]{2}\d{3})\b", text.lower())
    return m.group(1).upper() if m else None


def faculty_candidates(text, faculty_rows, n=5):
    """Ranked fuzzy matches — used to disambiguate ('did you mean...')."""
    t = text.lower()
    scored = []
    for r in faculty_rows:
        clean = r["name"].replace("Dr. ", "").replace("Prof. ", "")
        first, last = clean.split()[0].lower(), clean.split()[-1].lower()
        if clean.lower() in t:
            sc = 1.0
        elif first in t and last in t:
            sc = 0.97
        elif re.search(rf"\b{re.escape(last)}\b", t):
            sc = 0.86
        elif re.search(rf"\b{re.escape(first)}\b", t):
            sc = 0.82
        else:
            sc = 0.0
            for tok in re.findall(r"[a-z]{4,}", t):
                sc = max(sc, difflib.SequenceMatcher(None, tok, first).ratio() * 0.9,
                         difflib.SequenceMatcher(None, tok, last).ratio() * 0.88)
        scored.append((round(sc, 3), r))
    scored.sort(key=lambda x: -x[0])
    return [(s, r) for s, r in scored[:n] if s > 0.55]


def match_faculty(text, faculty_rows, threshold=0.72):
    """Fuzzy-resolve a faculty mention. faculty_rows: list of dict(id,name,dept)."""
    t = text.lower()
    # explicit ID
    m = re.search(r"\bf0?(\d{2,3})\b", t)
    if m:
        fid = f"F{int(m.group(1)):03d}"
        for r in faculty_rows:
            if r["id"] == fid:
                return r, 1.0
    best, score = None, 0.0
    for r in faculty_rows:
        clean = r["name"].replace("Dr. ", "").replace("Prof. ", "")
        first, last = clean.split()[0].lower(), clean.split()[-1].lower()
        full = clean.lower()
        if full in t:
            return r, 1.0
        cand = 0.0
        # both parts present anywhere
        if first in t and last in t:
            cand = 0.97
        elif re.search(rf"\b{re.escape(last)}\b", t) and ("dr" in t or "prof" in t or "madam" in t
                                                          or "sir" in t or "faculty" in t or "absent" in t):
            cand = 0.84
        elif re.search(rf"\b{re.escape(first)}\b", t):
            cand = 0.80
        else:
            for tok in re.findall(r"[a-z]{4,}", t):
                cand = max(cand, difflib.SequenceMatcher(None, tok, first).ratio() * 0.9,
                           difflib.SequenceMatcher(None, tok, last).ratio() * 0.88)
        if cand > score:
            best, score = r, cand
    return (best, score) if score >= threshold else (None, score)


def match_subject(text, subject_rows, threshold=0.75):
    t = text.lower()
    for r in subject_rows:
        if r["code"].lower() in t:
            return r, 1.0
    best, sc = None, 0.0
    for r in subject_rows:
        nm = r["name"].lower()
        if nm in t:
            return r, 1.0
        toks = [w for w in re.findall(r"[a-z]{4,}", nm)]
        if toks and all(w in t for w in toks[:2]):
            cand = 0.9
        else:
            cand = difflib.SequenceMatcher(None, t, nm).ratio()
        if cand > sc:
            best, sc = r, cand
    return (best, sc) if sc >= threshold else (None, sc)


def is_confirmation(text):
    """True only when the utterance is a reply to a pending proposal, not a fresh command."""
    t = text.lower().strip()
    if plan_choice(t) is not None:
        return True
    if len(t.split()) > 7:
        return False
    ranked = classify(t)
    if ranked and ranked[0][1] >= 10 and not re.fullmatch(
            r"\W*(yes|ok|okay|sure|confirm|approve( it)?|apply( it)?|go ahead|do it|proceed|"
            r"send it|yes,? send it|publish|commit|no|cancel|abort|reject( it)?|discard|stop)\W*", t):
        return False
    return is_yes(t) or is_no(t)


def is_yes(text):
    t = text.lower().strip()
    return any(re.search(rf"\b{re.escape(w)}\b", t) for w in CONFIRM_YES)


def is_no(text):
    t = text.lower().strip()
    return any(re.search(rf"\b{re.escape(w)}\b", t) for w in CONFIRM_NO)


def plan_choice(text):
    m = re.search(r"\b(?:plan|option)\s*([abc1-3])\b", text.lower())
    if m:
        g = m.group(1)
        return {"a": 0, "1": 0, "b": 1, "2": 1, "c": 2, "3": 2}[g]
    m = re.search(r"^\s*([abc])\s*$", text.lower().strip())
    if m:
        return {"a": 0, "b": 1, "c": 2}[m.group(1)]
    return None


# ------------------------------------------------------------------ languages (#31)
# Kannada and Hindi - in their own scripts and romanised - for the words a
# college office actually types. The sentence is normalised to English BEFORE
# the NLU sees it, so every rule, date parser and guard below runs unchanged.
#
# Deliberate limits, each pinned by tests/nlu_test.py:
#   * no translation ever yields an approval word. A write is confirmed with
#     "yes" / "approve" in English, on purpose: the write gate's vocabulary is
#     not something a lookup table should widen;
#   * Hindi "kal" means both tomorrow and yesterday. It is read forward, the
#     only reading an absence plan or a gate pass can act on, and the resolved
#     date is echoed back before anything commits;
#   * romanised forms that are also common first names (Indu, "today" in
#     Kannada) are left out - only the Kannada-script form is read.
# A native-script form matches as a word STEM: Kannada and Hindi add case
# endings (ನಾಳೆಗೆ, "for tomorrow"), which the stem absorbs.
LEXICON = [
    ("day after tomorrow", ["ನಾಡಿದ್ದು", "naadiddu", "परसों", "parson", "parso"]),
    ("tomorrow", ["ನಾಳೆ", "naale", "nale", "कल", "kal"]),
    ("today", ["ಇಂದು", "ಇವತ್ತು", "ivattu", "आज", "aaj"]),
    ("yesterday", ["ನಿನ್ನೆ", "ninne"]),
    ("on monday", ["ಸೋಮವಾರ", "somavara", "somvara", "सोमवार", "somvar", "somwar"]),
    ("on tuesday", ["ಮಂಗಳವಾರ", "mangalavara", "मंगलवार", "mangalvar", "mangalwar"]),
    ("on wednesday", ["ಬುಧವಾರ", "budhavara", "बुधवार", "budhvar", "budhwar"]),
    ("on thursday", ["ಗುರುವಾರ", "guruvara", "गुरुवार", "guruvar", "guruwar"]),
    ("on friday", ["ಶುಕ್ರವಾರ", "shukravara", "शुक्रवार", "shukravar", "shukrawar"]),
    ("on saturday", ["ಶನಿವಾರ", "shanivara", "शनिवार", "shanivar", "shaniwar"]),
    ("is absent", ["ಬರೊಲ್ಲ", "ಬರಲ್ಲ", "barolla", "baralla", "नहीं आएंगे", "नहीं आएगा", "नहीं आएगी",
                   "nahi aayenge", "nahin aayenge", "nahi aayega", "nahi aayegi"]),
    ("absent", ["ಗೈರುಹಾಜರು", "ಗೈರು", "gairuhajaru", "gairu", "अनुपस्थित", "anupasthit", "गैरहाज़िर", "gairhazir"]),
    ("leave", ["ರಜೆ", "raje", "छुट्टी", "chutti", "chhutti"]),
    ("timetable", ["ವೇಳಾಪಟ್ಟಿ", "velapatti", "समय सारणी", "samay sarni", "samay saarini", "samay sarini"]),
    ("attendance", ["ಹಾಜರಾತಿ", "hajarati", "उपस्थिति", "upasthiti", "हाज़िरी", "हाजिरी", "haziri", "hazri"]),
    ("exam", ["ಪರೀಕ್ಷೆ", "pareekshe", "परीक्षा", "pariksha"]),
    ("fees", ["ಶುಲ್ಕ", "shulka", "शुल्क", "shulk", "फीस", "phees"]),
    ("library", ["ಗ್ರಂಥಾಲಯ", "granthalaya", "पुस्तकालय", "pustakalaya"]),
    ("books", ["ಪುಸ್ತಕ", "pustaka", "किताब", "kitab", "kitaab", "पुस्तक", "pustak"]),
    ("hostel", ["ವಸತಿ ನಿಲಯ", "छात्रावास", "chhatravas", "chatravas"]),
    ("students", ["ವಿದ್ಯಾರ್ಥಿ", "vidyarthi", "विद्यार्थी", "छात्र", "chhatra"]),
    ("faculty", ["ಉಪನ್ಯಾಸಕ", "upanyasaka", "ಶಿಕ್ಷಕ", "shikshaka", "शिक्षक", "shikshak", "अध्यापक", "adhyapak"]),
    ("substitute", ["ಬದಲಿ", "badali", "बदली", "badli"]),
    ("show", ["ತೋರಿಸಿ", "torisi", "दिखाओ", "दिखाइए", "dikhao", "dikhaiye", "बताओ", "बताइए", "batao", "bataiye"]),
    ("gate pass", ["ಗೇಟ್ ಪಾಸ್", "गेट पास"]),
]
# Words people use to CONFIRM in these languages. They are recognised only so
# the answer can say why they do not commit anything (see LEXICON's first limit).
NATIVE_YES = re.compile(r"(?:ಹೌದು|ಸರಿ|हाँ|हां|ठीक है|(?<![a-z])(?:haan|haa|howdu|houdu|sari|theek hai|thik hai)(?![a-z]))", re.I)
_NATIVE = re.compile(r"[\u0900-\u097F\u0C80-\u0CFF]")


def _lex_rx(form):
    if _NATIVE.search(form):
        # a stem: absorb the case ending that follows it
        return re.escape(form).replace(r"\ ", r"\s+") + r"[\u0900-\u097F\u0C80-\u0CFF]*"
    return r"(?<![a-z])" + re.escape(form).replace(r"\ ", r"\s+") + r"(?![a-z])"


_LEX = sorted(((_lex_rx(f), en, f) for en, forms in LEXICON for f in forms), key=lambda x: -len(x[2]))
_LEX = [(re.compile(rx, re.I), en, f) for rx, en, f in _LEX]


def normalize(text):
    """-> (english text, [(form, english), ...]). Text with nothing to
    translate comes back unchanged, so English input is never touched."""
    out, hits = str(text or ""), []
    for rx, en, form in _LEX:
        if rx.search(out):
            hits.append((form, en))
            out = rx.sub(f" {en} ", out)
    return (re.sub(r"[ \t]+", " ", out).strip() if hits else out), hits
