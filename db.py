"""
VidyaERP :: Data Layer
Realistic Indian engineering-college dataset; its timetable is built by solver.py.

Timetable rules, enforced by the solver and checked by verify() (this is how real colleges run):
  • Every working day is a solid block of back-to-back periods — NO free holes in between.
  • Mon–Fri : 7 periods (09:00 → 16:30) · Sat : 4 periods (09:00 → 13:00)
  • 20-min short break after P2, 45-min lunch after P4.
  • Labs occupy 3 CONSECUTIVE periods inside one session (P1–P3 morning or P5–P7 afternoon)
    and never straddle lunch.
  • A section larger than a lab seats is split into lab batches (#20), and its two labs
    ROTATE: in one block B1 takes lab X while B2 takes lab Y, in the other they change
    over. Each student still does each lab once a week; each lab teacher teaches it
    once per batch, in a room that seats the batch.
  • Every class gets its full weekly quota of every subject (credits + 1 for theory).
  • A faculty member is never in two rooms at once; a room is never double-booked, and
    seats the class or batch in it.
  • Any slot that curriculum hours can't fill is given a real academic activity
    (placement training, mentoring, library, project, sports) — never left blank.
"""
import os, sqlite3, random, datetime as dt

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path=ENV_PATH):
    """Minimal .env loader — no python-dotenv dependency.

    It lives HERE, in the lowest module, rather than in llm.py where it began,
    because DB_PATH below is computed at import time: `app.py` imports db before
    llm, and `mcp_server.py` never imported llm at all, so a VIDYAERP_DB set in
    .env was silently ignored by both. Worse than ignored — had one entry point
    honoured it and the other not, the console and the MCP server would open
    different databases, and an approval code minted in one would be invisible
    to the other. Read the file before anything reads a setting out of it.

    setdefault, never overwrite: a real environment variable always wins.
    """
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            out[k.strip()] = v
            if v:
                os.environ.setdefault(k.strip(), v)
    return out


load_env()

# Beside the code by default, which is what makes `git clone && uvicorn` work
# with no configuration. VIDYAERP_DB moves it — point it at a mounted disk to
# survive a redeploy, because a platform's default filesystem is ephemeral and
# every applied override would otherwise vanish on the next restart.
DB_PATH = (os.environ.get("VIDYAERP_DB") or "").strip() or \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "college.db")
random.seed(20)

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# 7 teaching periods; breaks are gaps in the clock, not empty slots
PERIODS = {
    1: "09:00-09:55", 2: "09:55-10:50",
    3: "11:10-12:05", 4: "12:05-13:00",          # 20-min break before P3
    5: "13:45-14:40", 6: "14:40-15:35", 7: "15:35-16:30",   # 45-min lunch before P5
}
BREAK_AFTER = {2: "Short break · 10:50-11:10", 4: "Lunch · 13:00-13:45"}
# Junior batches sit the full day; final-year batches finish early for project work.
# The day is ALWAYS a solid block starting at P1 — it just ends sooner.
SEM_DAY_PERIODS = {
    3: {"Mon": 7, "Tue": 7, "Wed": 7, "Thu": 7, "Fri": 7, "Sat": 4},   # 39 / week
    5: {"Mon": 6, "Tue": 6, "Wed": 6, "Thu": 6, "Fri": 6, "Sat": 4},   # 34 / week
    7: {"Mon": 5, "Tue": 5, "Wed": 5, "Thu": 5, "Fri": 5, "Sat": 3},   # 28 / week
}
DAY_PERIODS = SEM_DAY_PERIODS[3]


def day_periods(sem, day):
    return SEM_DAY_PERIODS.get(int(sem), SEM_DAY_PERIODS[3])[day]

# lab blocks must sit inside one session (no lunch straddle)
LAB_WINDOWS = {"Mon": [(1, 3), (5, 7)], "Tue": [(1, 3), (5, 7)], "Wed": [(1, 3), (5, 7)],
               "Thu": [(1, 3), (5, 7)], "Fri": [(1, 3), (5, 7)], "Sat": [(1, 3)]}

DEPTS = {
    "CSE":   "Computer Science & Engineering",
    "ISE":   "Information Science & Engineering",
    "ECE":   "Electronics & Communication Engineering",
    "MECH":  "Mechanical Engineering",
    "CIVIL": "Civil Engineering",
}

FIRST_M = ["Ramesh", "Sudhir", "Ganesh", "Naveen", "Prashanth", "Vinayak", "Harish", "Manjunath",
           "Girish", "Sathish", "Dinesh", "Rakesh", "Chethan", "Anil", "Karthik", "Srinivas",
           "Mohan", "Vivek", "Suresh", "Yogesh", "Nithin", "Praveen"]
FIRST_F = ["Shwetha", "Deepa", "Anitha", "Rashmi", "Sunitha", "Pooja", "Vidya", "Sushma",
           "Ashwini", "Roopa", "Meghana", "Kavya", "Sneha", "Divya", "Chaithra", "Bhavya",
           "Lakshmi", "Vandana", "Namratha", "Triveni"]
LAST = ["Bhat", "Shetty", "Hegde", "Kamath", "Rao", "Nayak", "Pai", "Acharya", "Gowda", "Prabhu",
        "Kulkarni", "Desai", "Patil", "Shenoy", "Bangera", "Poojary", "Kotian", "Adiga",
        "Karanth", "Upadhyaya", "Ballal", "Mallya"]
STUD_FIRST = FIRST_M + FIRST_F + ["Aakash", "Rohit", "Tanvi", "Ishaan", "Sanjana", "Nihal",
                                  "Aditi", "Varun", "Keerthana", "Sahil", "Ananya", "Rithesh"]

# ---------------------------------------------------------------- curriculum
SUBJECTS = {
    "CSE": {
        3: [("BCS301", "Mathematics for CS-III", 4, "T"), ("BCS302", "Digital Design & CO", 4, "T"),
            ("BCS303", "Operating Systems", 4, "T"), ("BCS304", "Data Structures", 3, "T"),
            ("BCS305", "OOP with Java", 3, "T"), ("BCSL306", "Data Structures Lab", 1, "L"),
            ("BCSL307", "Java Programming Lab", 1, "L")],
        5: [("BCS501", "Software Engineering & PM", 4, "T"), ("BCS502", "Computer Networks", 4, "T"),
            ("BCS503", "Theory of Computation", 4, "T"), ("BCS504", "Artificial Intelligence", 3, "T"),
            ("BCS515", "Cloud Computing", 3, "T"), ("BCSL506", "Computer Networks Lab", 1, "L"),
            ("BCSL507", "AI & ML Lab", 1, "L")],
        7: [("BCS701", "Big Data Analytics", 4, "T"), ("BCS702", "Machine Learning", 4, "T"),
            ("BCS703", "Cryptography & Net Security", 3, "T"), ("BCS714", "Blockchain Technology", 3, "T"),
            ("BCS705", "Project Phase-I", 2, "P")],
    },
    "ISE": {
        3: [("BIS301", "Discrete Mathematics", 4, "T"), ("BIS302", "Digital Electronics", 4, "T"),
            ("BIS303", "Data Structures", 4, "T"), ("BIS304", "Python Programming", 3, "T"),
            ("BIS305", "Unix Shell Programming", 3, "T"), ("BISL306", "Python Lab", 1, "L"),
            ("BISL307", "Data Structures Lab", 1, "L")],
        5: [("BIS501", "Database Management Systems", 4, "T"), ("BIS502", "Full Stack Development", 4, "T"),
            ("BIS503", "Automata Theory", 3, "T"), ("BIS504", "Data Mining", 3, "T"),
            ("BIS505", "Operating Systems", 3, "T"), ("BISL506", "DBMS Lab", 1, "L"),
            ("BISL507", "Full Stack Lab", 1, "L")],
        7: [("BIS701", "Deep Learning", 4, "T"), ("BIS702", "Internet of Things", 4, "T"),
            ("BIS703", "Software Testing", 3, "T"), ("BIS704", "Cyber Security", 3, "T"),
            ("BIS705", "Project Phase-I", 2, "P")],
    },
    "ECE": {
        3: [("BEC301", "Network Analysis", 4, "T"), ("BEC302", "Electronic Devices", 4, "T"),
            ("BEC303", "Digital System Design", 4, "T"), ("BEC304", "Signals & Systems", 3, "T"),
            ("BEC305", "Engineering Electromagnetics", 3, "T"), ("BECL306", "Analog Circuits Lab", 1, "L"),
            ("BECL307", "Digital Design Lab", 1, "L")],
        5: [("BEC501", "Digital Signal Processing", 4, "T"), ("BEC502", "Microcontrollers", 4, "T"),
            ("BEC503", "Antennas & Propagation", 3, "T"), ("BEC504", "VLSI Design", 3, "T"),
            ("BEC505", "Information Theory & Coding", 3, "T"), ("BECL506", "DSP Lab", 1, "L"),
            ("BECL507", "Microcontroller Lab", 1, "L")],
        7: [("BEC701", "Embedded Systems", 4, "T"), ("BEC702", "Optical Communication", 3, "T"),
            ("BEC703", "Wireless Networks", 3, "T"), ("BEC704", "Radar Engineering", 3, "T"),
            ("BEC705", "Project Phase-I", 2, "P")],
    },
    "MECH": {
        3: [("BME301", "Thermodynamics", 4, "T"), ("BME302", "Material Science", 4, "T"),
            ("BME303", "Mechanics of Materials", 4, "T"), ("BME304", "Machine Drawing", 3, "T"),
            ("BMEL305", "Foundry & Forging Lab", 1, "L"), ("BMEL306", "Material Testing Lab", 1, "L")],
        5: [("BME501", "Design of Machine Elements", 4, "T"), ("BME502", "Heat Transfer", 4, "T"),
            ("BME503", "Fluid Mechanics & Machinery", 3, "T"), ("BME504", "Manufacturing Process", 3, "T"),
            ("BMEL505", "CAD/CAM Lab", 1, "L"), ("BMEL506", "Heat Transfer Lab", 1, "L")],
        7: [("BME701", "Finite Element Analysis", 4, "T"), ("BME702", "Control Engineering", 3, "T"),
            ("BME703", "Additive Manufacturing", 3, "T"), ("BME704", "Project Phase-I", 2, "P")],
    },
    "CIVIL": {
        3: [("BCV301", "Strength of Materials", 4, "T"), ("BCV302", "Surveying", 4, "T"),
            ("BCV303", "Fluid Mechanics", 3, "T"), ("BCV304", "Building Materials", 3, "T"),
            ("BCVL305", "Surveying Lab", 1, "L"), ("BCVL306", "Materials Testing Lab", 1, "L")],
        5: [("BCV501", "Design of RC Structures", 4, "T"), ("BCV502", "Geotechnical Engineering", 4, "T"),
            ("BCV503", "Transportation Engineering", 3, "T"), ("BCV504", "Water Supply Engineering", 3, "T"),
            ("BCVL505", "Concrete Technology Lab", 1, "L"), ("BCVL506", "Geotech Lab", 1, "L")],
        7: [("BCV701", "Design of Steel Structures", 4, "T"), ("BCV702", "Estimation & Costing", 3, "T"),
            ("BCV703", "Environmental Engineering", 3, "T"), ("BCV704", "Project Phase-I", 2, "P")],
    },
}

# non-lecture academic slots used to fill a full day (real colleges do exactly this)
ACTIVITIES = [
    ("ACT-PT",  "Placement / Aptitude Training", "A", 3),
    ("ACT-MP",  "Mini Project / Skill Lab",      "A", 2),
    ("ACT-TS",  "Technical Seminar",             "A", 1),
    ("ACT-MC",  "Mentoring & Counselling",       "A", 1),
    ("ACT-LIB", "Library / Self Study",          "A", 2),
    ("ACT-SPT", "Sports & Physical Education",   "A", 1),
    ("ACT-RM",  "Remedial / Doubt Clearing",     "A", 2),
]

SECTIONS = {"CSE": ["A", "B"], "ISE": ["A"], "ECE": ["A", "B"], "MECH": ["A"], "CIVIL": ["A"]}
SEMS = [3, 5, 7]
DESIGNATIONS = [("Professor", 16), ("Associate Professor", 18), ("Assistant Professor", 22)]
FAC_COUNT = {"CSE": 15, "ISE": 9, "ECE": 13, "MECH": 8, "CIVIL": 8}

# Fixed-date national and state holidays: (month, day) -> name. Only those whose
# date never moves; festivals that follow the lunar calendar are left out rather
# than guessed. The SEE seed skips them (#106) and the academic calendar seeds
# its holidays from this list (#104), so the two cannot disagree.
HOLIDAYS = {(1, 26): "Republic Day", (5, 1): "May Day", (8, 15): "Independence Day",
            (10, 2): "Gandhi Jayanti", (11, 1): "Kannada Rajyotsava", (12, 25): "Christmas"}


def holiday(d):
    """The fixed holiday on date `d`, or None."""
    return HOLIDAYS.get((d.month, d.day))


LAB_SEATS = 36            # every lab room seats 36; a section above it is split into batches (#20)


def batch_plan(n, seats=LAB_SEATS):
    """Batch sizes for a section of `n` students: as few batches as fit the lab
    seats, as even as possible. [] means the section is not split."""
    k = -(-n // seats) if n > seats else 1
    if k <= 1:
        return []
    return [n // k + (1 if i < n % k else 0) for i in range(k)]


def assign_batches(con):
    """Give every student of a split section a lab batch (B1, B2, ...), in roll
    order. Only students with none yet, and only for a section that needs it, so
    an older database picks batches up without anyone being moved. Draws nothing
    from `random`."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(students)")}
    if "batch" not in cols:
        con.execute("ALTER TABLE students ADD COLUMN batch TEXT")
    for r in con.execute("SELECT dept, sem, section, COUNT(*) n, COUNT(batch) b FROM students "
                         "GROUP BY dept, sem, section").fetchall():
        if r["b"]:
            continue
        sizes = batch_plan(r["n"])
        usns = [x["usn"] for x in con.execute("SELECT usn FROM students WHERE dept=? AND sem=? AND section=? "
                                              "ORDER BY usn", (r["dept"], r["sem"], r["section"]))]
        i = 0
        for b, size in enumerate(sizes, 1):
            con.executemany("UPDATE students SET batch=? WHERE usn=?", [(f"B{b}", u) for u in usns[i:i + size]])
            i += size


def batches_of(con, dept, sem, section):
    """{batch: headcount} for a split section, {} for one taught whole."""
    return {r["batch"]: r["n"] for r in con.execute(
        "SELECT batch, COUNT(*) n FROM students WHERE dept=? AND sem=? AND section=? AND batch IS NOT NULL "
        "GROUP BY batch ORDER BY batch", (dept, int(sem), section))}


def connect():
    d = os.path.dirname(os.path.abspath(DB_PATH))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)      # a disk mount point may not exist yet
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


SCHEMA = """
DROP TABLE IF EXISTS departments; DROP TABLE IF EXISTS faculty; DROP TABLE IF EXISTS students;
DROP TABLE IF EXISTS subjects; DROP TABLE IF EXISTS rooms; DROP TABLE IF EXISTS timetable;
DROP TABLE IF EXISTS leaves; DROP TABLE IF EXISTS overrides; DROP TABLE IF EXISTS requests;
DROP TABLE IF EXISTS notifications; DROP TABLE IF EXISTS audit; DROP TABLE IF EXISTS exams;
DROP TABLE IF EXISTS attendance; DROP TABLE IF EXISTS placements; DROP TABLE IF EXISTS cie_marks;
DROP TABLE IF EXISTS books; DROP TABLE IF EXISTS book_loans; DROP TABLE IF EXISTS hostel_rooms;
DROP TABLE IF EXISTS hostel_allocations; DROP TABLE IF EXISTS hostel_complaints;
DROP TABLE IF EXISTS bus_routes; DROP TABLE IF EXISTS bus_stops; DROP TABLE IF EXISTS transport_riders;
DROP TABLE IF EXISTS gate_passes; DROP TABLE IF EXISTS placement_drives;
DROP TABLE IF EXISTS student_offers; DROP TABLE IF EXISTS drive_registrations;
DROP TABLE IF EXISTS certificates;
DROP TABLE IF EXISTS makeup_sessions; DROP TABLE IF EXISTS faculty_availability;
DROP TABLE IF EXISTS calendar_events;
DROP TABLE IF EXISTS attendance_sessions; DROP TABLE IF EXISTS attendance_marks;
DROP TABLE IF EXISTS cie_submissions; DROP TABLE IF EXISTS student_school_marks;

CREATE TABLE departments(code TEXT PRIMARY KEY, name TEXT, hod TEXT, intake INT);
CREATE TABLE faculty(
  id TEXT PRIMARY KEY, name TEXT, dept TEXT, designation TEXT, email TEXT, phone TEXT,
  max_load INT, expertise TEXT, joined TEXT, is_hod INT DEFAULT 0);
CREATE TABLE subjects(code TEXT PRIMARY KEY, name TEXT, dept TEXT, sem INT, credits INT, kind TEXT);
CREATE TABLE rooms(id TEXT PRIMARY KEY, kind TEXT, capacity INT, block TEXT);
CREATE TABLE students(
  usn TEXT PRIMARY KEY, name TEXT, dept TEXT, sem INT, section TEXT, cgpa REAL,
  attendance REAL, fee_due INT, mentor TEXT, phone TEXT, hostel INT, category TEXT, backlogs INT,
  batch TEXT);
CREATE TABLE timetable(
  id INTEGER PRIMARY KEY AUTOINCREMENT, dept TEXT, sem INT, section TEXT, day TEXT, period INT,
  subject TEXT, faculty TEXT, room TEXT, kind TEXT, batch TEXT);
CREATE TABLE leaves(
  id INTEGER PRIMARY KEY AUTOINCREMENT, faculty TEXT, from_date TEXT, to_date TEXT, kind TEXT,
  reason TEXT, status TEXT, applied_on TEXT);
CREATE TABLE overrides(
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, slot_id INT, action TEXT, new_faculty TEXT,
  new_room TEXT, new_subject TEXT, reason TEXT, status TEXT, created_by TEXT, created_at TEXT,
  plan_ref TEXT);
CREATE TABLE requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, raised_by TEXT, role TEXT, target TEXT,
  title TEXT, details TEXT, amount INT, status TEXT, priority TEXT, created_at TEXT, sla_hrs INT);
CREATE TABLE notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, audience TEXT, channel TEXT, title TEXT, body TEXT,
  created_at TEXT, status TEXT, attempts INT DEFAULT 0, delivered_at TEXT, error TEXT);
CREATE TABLE audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, agent TEXT, action TEXT,
  payload TEXT, outcome TEXT);
CREATE TABLE exams(
  id INTEGER PRIMARY KEY AUTOINCREMENT, dept TEXT, sem INT, subject TEXT, date TEXT,
  session TEXT, room TEXT, kind TEXT);
CREATE TABLE attendance(
  id INTEGER PRIMARY KEY AUTOINCREMENT, usn TEXT, subject TEXT, held INT, attended INT);
CREATE TABLE placements(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT, ctc REAL, date TEXT, offers INT, dept TEXT);
"""


def _name(used):
    while True:
        nm = (random.choice(FIRST_M) if random.random() < .55 else random.choice(FIRST_F)) \
             + " " + random.choice(LAST)
        if nm not in used:
            used.add(nm)
            return nm


# ====================================================================== SEED
def seeded():
    """True when the file holds an institution: its first and last core tables
    have rows. A file that merely exists (0 bytes after a stray connect(), or
    a seed killed part-way) used to count, and the app then failed at import
    on "no such table" (#102)."""
    if not os.path.exists(DB_PATH):
        return False
    try:
        c = sqlite3.connect(DB_PATH)
        try:
            return all(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("students", "placements"))
        finally:
            c.close()
    except sqlite3.Error:
        return False


def seed(force=False):
    if not force and seeded():
        _campus()                 # an older college.db picks the campus services up here
        return
    # Re-pin the stream on EVERY seed, not just once at import. It used to be
    # pinned only by the module-level random.seed(20), so the first seed in a
    # process was reproducible and the second was not: /api/seed/reset on a
    # running server rebuilt a DIFFERENT institution from the one it booted
    # with. Measured by tests/campus_test.py:1b (two fresh seeds, one process).
    random.seed(20)
    con = connect()
    cur = con.cursor()
    cur.executescript(SCHEMA)
    today = dt.date(2026, 9, 4)

    # ---------------------------------------------------------- faculty
    fac_by_dept, fid, used = {}, 1, set()
    for code, name in DEPTS.items():
        lst = []
        for i in range(FAC_COUNT[code]):
            nm = _name(used)
            desig, load = DESIGNATIONS[0] if i == 0 else random.choice(DESIGNATIONS[1:])
            f = f"F{fid:03d}"; fid += 1
            title = "Dr. " if desig != "Assistant Professor" or random.random() < .3 else "Prof. "
            pool = [s[0] for sem in SUBJECTS[code] for s in SUBJECTS[code][sem]]
            cur.execute("INSERT INTO faculty VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (f, title + nm, code, desig,
                         f"{nm.split()[0].lower()}.{nm.split()[1].lower()}@vidyatech.edu.in",
                         "9" + "".join(random.choice("0123456789") for _ in range(9)), load,
                         ",".join(random.sample(pool, k=min(5, len(pool)))),
                         f"20{random.randint(6,22):02d}-07-01", 1 if i == 0 else 0))
            lst.append(f)
        fac_by_dept[code] = lst
        cur.execute("INSERT INTO departments VALUES(?,?,?,?)",
                    (code, name,
                     cur.execute("SELECT name FROM faculty WHERE id=?", (lst[0],)).fetchone()["name"],
                     {"CSE": 180, "ISE": 120, "ECE": 120, "MECH": 60, "CIVIL": 60}[code]))

    # ---------------------------------------------------------- subjects
    for d, sems in SUBJECTS.items():
        for sem, subs in sems.items():
            for c, nm, cr, kind in subs:
                cur.execute("INSERT OR IGNORE INTO subjects VALUES(?,?,?,?,?,?)", (c, nm, d, sem, cr, kind))
    for c, nm, kind, _ in ACTIVITIES:
        cur.execute("INSERT OR IGNORE INTO subjects VALUES(?,?,?,?,?,?)", (c, nm, "ALL", 0, 0, kind))

    # ---------------------------------------------------------- rooms
    class_rooms, labs = [], []
    for blk, pfx, n in [("Main Block", "MB", 14), ("Science Block", "SB", 10), ("Workshop Block", "WB", 8)]:
        for i in range(1, n + 1):
            rid = f"{pfx}-{100+i}"
            kind = "Lab" if (pfx == "WB" or i % 3 == 0) else "Classroom"
            cur.execute("INSERT INTO rooms VALUES(?,?,?,?)",
                        (rid, kind, 70 if kind == "Classroom" else 36, blk))
            (labs if kind == "Lab" else class_rooms).append(rid)

    # ---------------------------------------------------------- students
    for d in DEPTS:
        for sem in SEMS:
            for sec in SECTIONS[d]:
                cnt = random.randint(52, 64) if d in ("CSE", "ISE", "ECE") else random.randint(34, 46)
                for i in range(1, cnt + 1):
                    yr = {3: 25, 5: 24, 7: 23}[sem]
                    tag = {"CSE": "CS", "ISE": "IS", "ECE": "EC", "MECH": "ME", "CIVIL": "CV"}[d]
                    num = i + (100 if sec == "B" else 0)
                    usn = f"4VP{yr}{tag}{num:03d}"
                    cur.execute("""INSERT INTO students(usn,name,dept,sem,section,cgpa,attendance,fee_due,mentor,
                                   phone,hostel,category,backlogs) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (usn, random.choice(STUD_FIRST) + " " + random.choice(LAST), d, sem, sec,
                                 round(min(9.9, max(5.0, random.gauss(7.6, .9))), 2),
                                 round(min(99, max(46, random.gauss(80, 12))), 1),
                                 random.choice([0, 0, 0, 0, 25000, 48000, 62000, 118000]),
                                 random.choice(fac_by_dept[d]),
                                 "9" + "".join(random.choice("0123456789") for _ in range(9)),
                                 1 if random.random() < .38 else 0,
                                 random.choice(["GM", "GM", "2A", "3B", "SC", "ST", "OBC"]),
                                 0 if random.random() < .78 else random.randint(1, 3)))
    assign_batches(con)

    # ================================================== CONTIGUOUS TIMETABLE
    # Built by the solver (#20), the same code path the console streams and a
    # rebuild commits: solver.py places every curriculum period (theory, the lab
    # batch rotations, the projects) under every hard rule, then fills each day
    # solid with activities. A greedy generator lived here before; measured, it
    # left 5 class-subjects an hour short of their weekly quota, and 10 once lab
    # batches doubled the lab teachers' hours (CSE-7B's BCS703 had none at all),
    # because when every classroom is taught in at an hour only a search can move
    # something to make room. The step limits bound the solver, so an unlimited
    # time budget keeps the seed identical on a slow machine. It draws from its
    # own Random, never the global stream the rest of the seed uses.
    con.commit()
    import solver
    res = solver.run(solver.build_input(con, seed=7, time_budget_s=float("inf")))
    solver.apply(con, res)

    # ---------------------------------------------------------- leaves
    all_fac = [f for d in fac_by_dept for f in fac_by_dept[d]]
    for _ in range(16):
        f = random.choice(all_fac)
        start = today + dt.timedelta(days=random.randint(-6, 12))
        if start.weekday() == 6:                # nobody applies for leave from a Sunday; no extra draw
            start += dt.timedelta(days=1)
        end = start + dt.timedelta(days=random.choice([0, 0, 1, 2]))
        cur.execute("INSERT INTO leaves(faculty,from_date,to_date,kind,reason,status,applied_on) VALUES(?,?,?,?,?,?,?)",
                    (f, start.isoformat(), end.isoformat(),
                     random.choice(["Casual Leave", "Medical Leave", "On Duty (Conference)", "Earned Leave"]),
                     random.choice(["Family function", "Fever & throat infection", "IEEE conference at NITK",
                                    "Personal work", "NAAC document work at VTU Belagavi"]),
                     random.choice(["Approved", "Pending", "Approved"]),
                     (start - dt.timedelta(days=3)).isoformat()))

    # ---------------------------------------------------------- requests
    req_seed = [
        ("Leave", "F004", "Faculty", "Principal", "Medical leave 08-10 Sep", "Viral fever, doctor advised rest.", 0, "Pending", "High", 24),
        ("Purchase", "F011", "Faculty", "Admin", "Procure 15 Raspberry Pi 5 kits for IoT lab", "Budget head: R&D 2026-27", 142000, "Pending", "Medium", 72),
        ("Event", "F021", "Faculty", "Principal", "Approval for 2-day Hackathon 'CodeStorm 26'", "Expected 400 participants, need Main Auditorium", 85000, "Pending", "High", 48),
        ("Bonafide", "4VP24CS017", "Student", "Admin", "Bonafide certificate for passport", "Urgent - appointment on 12 Sep", 0, "Pending", "Low", 24),
        ("Fee", "4VP23ME009", "Student", "Accounts", "Instalment extension for semester fee", "Family medical emergency", 62000, "Pending", "Medium", 48),
        ("Maintenance", "F033", "Faculty", "Admin", "Projector not working in MB-104", "Since 2 days, affecting 3 sections", 0, "Pending", "High", 12),
        ("Infrastructure", "F002", "Faculty", "Principal", "Additional 60 KVA UPS for CSE block", "Frequent power cuts affecting labs", 310000, "Pending", "Medium", 96),
        ("Internship", "4VP23CS042", "Student", "Placement", "NOC for 6-month internship at Bosch", "Stipend 35k, Bengaluru", 0, "Pending", "Medium", 48),
        ("Duty Leave", "4VP24EC021", "Student", "Admin", "OD for inter-college fest at NITK", "Representing college in robotics", 0, "Approved", "Low", 24),
        ("Purchase", "F048", "Faculty", "Admin", "Consumables for Concrete lab", "Cement, admixtures for Sem-5 experiments", 46000, "Approved", "Low", 72),
    ]
    for r in req_seed:
        cur.execute("""INSERT INTO requests(kind,raised_by,role,target,title,details,amount,status,priority,created_at,sla_hrs)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    r[:9] + ((today - dt.timedelta(days=random.randint(0, 5))).isoformat(), r[9]))

    # ---------------------------------------------------------- exams
    ex_start = today + dt.timedelta(days=21)
    for d in DEPTS:
        for sem in SEMS:
            day = ex_start
            for c, nm, cr, kind in SUBJECTS[d][sem]:
                if kind != "T":
                    continue
                # every other day, never a Sunday: "+2 days" once put 15 papers on Sun 27 Sep (#101),
                # and never a holiday: it also put 13 on Gandhi Jayanti, Fri 2 Oct (#106)
                while day.weekday() == 6 or holiday(day):
                    day += dt.timedelta(days=1)
                cur.execute("INSERT INTO exams(dept,sem,subject,date,session,room,kind) VALUES(?,?,?,?,?,?,?)",
                            (d, sem, c, day.isoformat(),
                             "FN (09:30-12:30)", random.choice(class_rooms), "SEE Theory"))
                day += dt.timedelta(days=2)

    for comp, ctc, offers in [("Infosys", 4.5, 62), ("TCS Digital", 7.0, 24), ("Bosch", 8.5, 11),
                              ("Mphasis", 5.2, 18), ("Zoho", 9.0, 6), ("L&T Construction", 6.0, 9),
                              ("Cognizant", 4.8, 38), ("Quest Global", 6.5, 14)]:
        cur.execute("INSERT INTO placements(company,ctc,date,offers,dept) VALUES(?,?,?,?,?)",
                    (comp, ctc, (today - dt.timedelta(days=random.randint(10, 120))).isoformat(), offers, "ALL"))

    con.commit()
    con.close()
    _campus()


def _campus():
    """Library, hostel, transport, gate passes, placement drives, certificates
    and per-subject attendance. Seeded AFTER the core institution and from its
    own Random, so the core tables are byte-identical with or without it -
    tests/campus_test.py pins that. Imported lazily: campus_data imports db."""
    con = connect()
    if "batch" not in {r[1] for r in con.execute("PRAGMA table_info(timetable)")}:
        con.execute("ALTER TABLE timetable ADD COLUMN batch TEXT")   # an older database: whole-section rows (#20)
    assign_batches(con)
    con.commit()
    con.close()
    import campus_data
    campus_data.ensure()
    import academics
    academics.ensure()
    import academic_calendar               # holidays, events and exams on one calendar (#104)
    academic_calendar.ensure()
    import teaching                        # the attendance register, one class hour at a time (#116)
    teaching.ensure()
    import placement                       # drive criteria, school marks, calendar entries (#115)
    placement.ensure()
    import notify_gateway                 # delivery columns on an older notifications table (#26)
    con = connect()
    notify_gateway.ensure(con)
    con.close()


# ====================================================================== check
def batch_problems(con, where="", args=()):
    """The batch rules (#20), read from the rows alone: a class hour is either
    ONE row for the whole section, or one row per lab batch of the section, each
    batch once; and every teaching row's room seats the people in it (the batch
    when it names one, the section when not). Shared by db.verify() and
    solver.verify(), so the seed and a regenerated timetable meet one standard."""
    out = []
    want = {}
    for r in con.execute("SELECT dept, sem, section, batch, COUNT(*) n FROM students GROUP BY 1, 2, 3, 4"):
        want.setdefault((r["dept"], r["sem"], r["section"]), {})[r["batch"]] = r["n"]
    hours = {}
    for r in con.execute("SELECT t.*, rm.capacity cap FROM timetable t LEFT JOIN rooms rm ON rm.id=t.room" + where, args):
        cls = (r["dept"], r["sem"], r["section"])
        hours.setdefault((cls, r["day"], r["period"]), []).append(r["batch"])
        size = want.get(cls, {})
        n = size.get(r["batch"], 0) if r["batch"] else sum(size.values())
        if r["kind"] in ("T", "L") and r["cap"] is not None and n > r["cap"]:
            out.append(f"OVER CAPACITY {cls} {r['day']}P{r['period']} {r['subject']}"
                       f"{' ' + r['batch'] if r['batch'] else ''}: {n} in {r['room']} (seats {r['cap']})")
    for (cls, day, p), bs in hours.items():
        named = sorted(b for b in want.get(cls, {}) if b)
        if bs == [None] or (None not in bs and sorted(bs) == named):
            continue
        out.append(f"BATCHES {cls} {day}P{p}: {sorted(bs, key=str)} (section's batches: {named})")
    return out


def verify():
    con = connect()
    c = con.cursor()
    problems = []
    classes = [(r["dept"], r["sem"], r["section"]) for r in
               c.execute("SELECT DISTINCT dept,sem,section FROM timetable")]
    for cls in classes:
        for day in DAYS:
            ps = sorted(r["period"] for r in c.execute(
                "SELECT DISTINCT period FROM timetable WHERE dept=? AND sem=? AND section=? AND day=?", (*cls, day)))
            want = list(range(1, day_periods(cls[1], day) + 1))
            if ps != want:
                problems.append(f"GAP {cls} {day}: {ps} != {want}")
    problems += batch_problems(con)
    # every student gets each subject's full weekly hours: a batch counts its own
    # lab rows and the section's rows (#20). Silent before: the greedy generator
    # this replaced left 5 class-subjects an hour short and nothing said so.
    for cls in classes:
        groups = [b for b in batches_of(con, *cls)] or [None]
        for code, _nm, cr, kind in SUBJECTS.get(cls[0], {}).get(cls[1], []):
            need = 6 if kind == "P" else 3 if kind == "L" else cr + 1
            got = {g: c.execute("SELECT COUNT(*) FROM timetable WHERE dept=? AND sem=? AND section=? AND subject=? "
                                "AND (batch IS NULL OR batch=?)", (*cls, code, g)).fetchone()[0] for g in groups}
            if len(set(got.values())) == 1:                  # the same for everyone: one line, not one per batch
                got = {None: next(iter(got.values()))}
            problems += [f"QUOTA {cls}{' ' + g if g else ''} {code}: {n} of {need} periods a week"
                         for g, n in got.items() if n != need]
    for r in c.execute("""SELECT day,period,faculty,COUNT(*) n FROM timetable WHERE faculty IS NOT NULL
                          GROUP BY day,period,faculty HAVING n>1"""):
        problems.append(f"FACULTY CLASH {r['faculty']} {r['day']}P{r['period']} x{r['n']}")
    for r in c.execute("""SELECT day,period,room,COUNT(*) n FROM timetable
                          GROUP BY day,period,room HAVING n>1"""):
        problems.append(f"ROOM CLASH {r['room']} {r['day']}P{r['period']} x{r['n']}")
    for r in c.execute("""SELECT f.id,f.name,f.max_load,COUNT(t.id) load FROM faculty f
                          LEFT JOIN timetable t ON t.faculty=f.id GROUP BY f.id
                          HAVING load > f.max_load"""):
        problems.append(f"OVERLOAD {r['name']} {r['load']}/{r['max_load']}")
    for r in c.execute("""SELECT date, COUNT(*) n FROM exams WHERE strftime('%w', date)='0' GROUP BY date"""):
        problems.append(f"SUNDAY EXAM {r['date']} x{r['n']}")              # #101
    for r in c.execute("SELECT date, COUNT(*) n FROM exams GROUP BY date"):
        if holiday(dt.date.fromisoformat(r["date"])):
            problems.append(f"HOLIDAY EXAM {r['date']} {holiday(dt.date.fromisoformat(r['date']))} x{r['n']}")  # #106
    con.close()
    return problems


if __name__ == "__main__":
    seed(force=True)
    con = connect()
    for t in ["faculty", "students", "timetable", "subjects", "requests", "leaves"]:
        print(f"{t:12}", con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"])
    ld = [dict(r) for r in con.execute("""SELECT f.name,f.max_load,COUNT(t.id) load FROM faculty f
                                          LEFT JOIN timetable t ON t.faculty=f.id GROUP BY f.id
                                          ORDER BY load DESC""")]
    print("load        max %d  min %d  avg %.1f" % (ld[0]["load"], ld[-1]["load"],
                                                    sum(x["load"] for x in ld) / len(ld)))
    p = verify()
    print("integrity  ", "✔ contiguous, clash-free, within load caps" if not p else f"✘ {len(p)} issues")
    for x in p[:10]:
        print("   ", x)
