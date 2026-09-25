"""
VidyaERP :: NLU layer
Lightweight, dependency-free natural-language understanding for the ERP copilot:
intent scoring, entity resolution (faculty / dept / sem / section / subject / student),
and Indian-context date parsing ("tomorrow", "next monday", "12 sep", "9/9").
"""
import re, difflib, datetime as dt

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
}

CONFIRM_YES = ["yes", "yeah", "yep", "ok", "okay", "approve", "apply", "confirm", "go ahead",
               "do it", "proceed", "accept", "sure", "publish", "commit"]
CONFIRM_NO = ["no", "cancel", "abort", "discard", "don't", "dont", "stop", "reject", "drop it"]

# Regex rules that dominate keyword scoring (verb-first phrasing, disambiguation)
PRIORITY = [
    (r"^\s*(?:please\s+|pls\s+)?(?:notify|inform|announce|broadcast|circulate|alert|"
     r"send (?:a |an )?(?:message|sms|notice|circular|whatsapp))", "notify.broadcast", 14),
    (r"\b(?:who(?:'s| is| are)?\s+(?:all\s+)?free|are free|is free|free faculty|free staff|"
     r"available faculty|free (?:room|slot|hall|classroom)|room availability|which rooms)\b",
     "timetable.view", 12),
    (r"\b(?:work ?load|teaching load|utilis|utiliz|over ?loaded)\b", "faculty.query", 12),
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
    (r"\bautopilot\b|\bneeds? (?:my )?attention\b|\baction items?\b|\bops radar\b|"
     r"\bwhat should i (?:do|act on|focus on|look at)\b|\bto-?do list\b", "ops.radar", 18),
    (r"(?:\b360\b|everything about|full profile|complete profile|subject[- ]wise attendance)"
     r".*\b4vp\d{2}[a-z]{2}\d{3}\b|\b4vp\d{2}[a-z]{2}\d{3}\b.*"
     r"(?:\b360\b|everything|full profile|complete profile|subject[- ]wise)", "student.360", 22),
]


def classify(text):
    """Return ranked list of (intent, score, matched_keywords). Regex priority rules
    break ties that pure keyword counting gets wrong (e.g. 'notify students about exams')."""
    t = " " + text.lower() + " "
    scores, hitmap = {}, {}
    for intent, cfg in INTENTS.items():
        s, hits = 0, []
        for kw, w in cfg["kw"]:
            if kw in t:
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
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", t)
    if m:
        day, mon = int(m.group(1)), MONTHS[m.group(2)]
        yr = today.year if (mon, day) >= (today.month, today.day) else today.year + 1
        try:
            return dt.date(yr, mon, day), f"{day} {m.group(2).title()}"
        except ValueError:
            return None, None
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)
    if m:
        day, mon = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3) or today.year)
        yr = yr + 2000 if yr < 100 else yr
        try:
            return dt.date(yr, mon, day), f"{day:02d}-{mon:02d}"
        except ValueError:
            return None, None
    return None, None


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
            if k in " " + t + " ":
                return code
    return None


def extract_sem(text):
    m = re.search(r"(?:sem|semester)\s*[-: ]?\s*([1-8])", text.lower())
    if m:
        return int(m.group(1))
    m = re.search(r"\b([1-8])(?:st|nd|rd|th)\s*(?:sem|semester)", text.lower())
    if m:
        return int(m.group(1))
    return None


def extract_section(text):
    m = re.search(r"\b(?:sec|section)\s*[-: ]?\s*([abcd])\b", text.lower())
    if m:
        return m.group(1).upper()
    m = re.search(r"\b[1-8]\s*(?:sem|semester)?\s*['\"]?([ab])\b", text.lower())
    if m:
        return m.group(1).upper()
    return None


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
