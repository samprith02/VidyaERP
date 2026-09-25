"""
VidyaERP :: Campus-services data layer

Library, hostel, transport, gate passes, placement drives, the certificate
register and per-subject attendance — the parts of a college that are not the
timetable but generate most of the office's daily paperwork.

Two properties this module is built around, both asserted by
tests/campus_test.py:

  * It never perturbs the core institution. db.seed() draws the faculty,
    students and timetable from the GLOBAL `random` stream (pinned by
    random.seed(20)); everything here draws from its OWN Random instance, and
    runs after the core seed has finished. The core tables hash identically
    with or without this module.

  * It is additive to an existing database. `ensure()` creates what is
    missing and populates only empty tables, so a `college.db` that predates
    this module picks the services up on the next start with no reseed and no
    migration step - the same trick mcp_server uses for its approvals table.

All of it is synthetic. Drivers are drawn from the same name pools as
faculty; companies running UPCOMING drives are invented; stops are town names.
"""
import random, math, datetime as dt
import db

TODAY = dt.date(2026, 9, 4)          # mirrors nlu.TODAY; db must not import nlu
RNG_SEED = 9004

FINE_PER_DAY = 2                     # rupees per overdue day, per book
LOAN_DAYS = {"Student": 14, "Faculty": 30}
LOAN_LIMIT = {"Student": 4, "Faculty": 10}
BED_PER_ROOM = 3

FEMALE_FIRST = set(db.FIRST_F) | {"Tanvi", "Sanjana", "Aditi", "Keerthana", "Ananya"}


def gender_of(name):
    """Synthetic data has no gender column; hostels are segregated, so it is
    inferred from which synthetic first-name pool the name was drawn from. That
    is only sound BECAUSE every name here was generated from those pools."""
    first = (name or "").replace("Dr. ", "").replace("Prof. ", "").split()[0] if name else ""
    return "F" if first in FEMALE_FIRST else "M"


SCHEMA = """
CREATE TABLE IF NOT EXISTS books(
  id TEXT PRIMARY KEY, title TEXT, author TEXT, subject TEXT, dept TEXT, copies INT,
  shelf TEXT, year INT);
CREATE TABLE IF NOT EXISTS book_loans(
  id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT, member TEXT, member_kind TEXT,
  issued_on TEXT, due_on TEXT, returned_on TEXT, fine_paid INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS hostel_rooms(
  id TEXT PRIMARY KEY, block TEXT, gender TEXT, floor INT, capacity INT);
CREATE TABLE IF NOT EXISTS hostel_allocations(
  usn TEXT PRIMARY KEY, room TEXT, since TEXT, mess TEXT, fee_due INT);
CREATE TABLE IF NOT EXISTS hostel_complaints(
  id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, usn TEXT, category TEXT, details TEXT,
  priority TEXT, status TEXT, raised_on TEXT, assigned_to TEXT, sla_hrs INT);
CREATE TABLE IF NOT EXISTS bus_routes(
  id TEXT PRIMARY KEY, name TEXT, bus TEXT, capacity INT, driver TEXT, driver_phone TEXT,
  departs TEXT, km INT, status TEXT);
CREATE TABLE IF NOT EXISTS bus_stops(
  route TEXT, seq INT, stop TEXT, pickup TEXT, PRIMARY KEY(route, seq));
CREATE TABLE IF NOT EXISTS transport_riders(
  usn TEXT PRIMARY KEY, route TEXT, stop TEXT, fee_due INT);
CREATE TABLE IF NOT EXISTS gate_passes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, usn TEXT, kind TEXT, out_at TEXT, return_by TEXT,
  reason TEXT, parent_consent INT, status TEXT, requested_at TEXT, decided_by TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS placement_drives(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT, role TEXT, ctc REAL, drive_date TEXT,
  min_cgpa REAL, max_backlogs INT, depts TEXT, dream INT, status TEXT);
CREATE TABLE IF NOT EXISTS student_offers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, usn TEXT, company TEXT, ctc REAL, offered_on TEXT);
CREATE TABLE IF NOT EXISTS drive_registrations(
  drive_id INT, usn TEXT, status TEXT, registered_at TEXT, PRIMARY KEY(drive_id, usn));
CREATE TABLE IF NOT EXISTS certificates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT UNIQUE, usn TEXT, kind TEXT, purpose TEXT,
  issued_on TEXT, issued_by TEXT, body TEXT, request_id INT);
"""

# The core db.SCHEMA drops these on a forced reseed so they rebuild with it.
TABLES = ["books", "book_loans", "hostel_rooms", "hostel_allocations", "hostel_complaints",
          "bus_routes", "bus_stops", "transport_riders", "gate_passes", "placement_drives",
          "student_offers", "drive_registrations", "certificates"]

# ---------------------------------------------------------------- transport
ROUTES = [
    ("R01", "Udupi – Kalsanka", "07:05", 24,
     ["Udupi Bus Stand", "Kalsanka", "Ambalpady", "Santhekatte", "Kallianpur"]),
    ("R02", "Parkala – Manipal", "07:00", 27,
     ["Parkala", "Hiriadka", "Manipal", "Laxmindra Nagar", "Kalsanka"]),
    ("R03", "Kundapura – Brahmavar", "06:35", 41,
     ["Kundapura", "Koteshwara", "Kota", "Saligrama", "Brahmavar"]),
    ("R04", "Hebri – Karkala", "06:40", 38,
     ["Hebri", "Ajekar", "Karkala", "Bailur", "Hiriadka"]),
    ("R05", "Mangaluru – Mulki", "06:30", 46,
     ["Mangaluru", "Kuloor", "Surathkal", "Mukka", "Mulki"]),
    ("R06", "Mulki – Katapady", "06:55", 31,
     ["Mulki", "Padubidri", "Uchila", "Kaup", "Katapady"]),
    ("R07", "Barkur – Brahmavar", "07:00", 26,
     ["Barkur", "Handady", "Brahmavar", "Uppoor", "Santhekatte"]),
    ("R08", "Shirva – Katapady", "07:05", 22,
     ["Shirva", "Belle", "Katapady", "Udyavara", "Ambalpady"]),
]
# relative pull of each route on day scholars - R03 and R06 are deliberately
# oversubscribed, which is the problem the TransportAgent's rebalancer solves
ROUTE_WEIGHT = {"R01": 1.0, "R02": 1.0, "R03": 1.25, "R04": 0.85, "R05": 0.75,
                "R06": 1.2, "R07": 0.8, "R08": 0.85}
BUS_CAPACITY = 52

# ---------------------------------------------------------------- placement
UPCOMING_DRIVES = [
    # company (fictional), role, CTC LPA, days from TODAY, min CGPA, max backlogs, depts, dream
    ("Nethra Robotics", "Graduate Engineer Trainee", 6.5, 7, 7.0, 0, "ECE,MECH,CSE", 0),
    ("Konkan Cloud Systems", "Software Engineer", 12.0, 11, 8.0, 0, "CSE,ISE", 1),
    ("Sahyadri Infra Projects", "Site Engineer", 5.4, 14, 6.5, 1, "CIVIL,MECH", 0),
    ("Coastal Analytics", "Data Analyst", 7.2, 18, 7.0, 0, "CSE,ISE,ECE", 0),
    ("Tulunadu Semiconductors", "Design Engineer", 10.0, 27, 7.5, 0, "ECE", 1),
]
# which branches each already-completed recruiter actually drew from
COMPANY_DEPTS = {"Infosys": "CSE,ISE,ECE,MECH,CIVIL", "TCS Digital": "CSE,ISE,ECE",
                 "Bosch": "MECH,ECE,CSE", "Mphasis": "CSE,ISE", "Zoho": "CSE,ISE",
                 "L&T Construction": "CIVIL,MECH", "Cognizant": "CSE,ISE,ECE,MECH",
                 "Quest Global": "MECH,ECE"}
COMPANY_MIN = {"Zoho": 8.0, "Bosch": 7.5, "TCS Digital": 7.5, "Quest Global": 7.0}

TITLE_FORMS = ["{s}: Principles and Practice", "A Textbook of {s}", "Fundamentals of {s}",
               "{s} — Concepts and Problems", "Applied {s}"]
GENERAL_BOOKS = [("Quantitative Aptitude & Reasoning Workbook", "GEN"),
                 ("Engineering Mathematics Handbook", "GEN"),
                 ("Technical Communication for Engineers", "GEN"),
                 ("Environmental Studies", "GEN"),
                 ("Professional Ethics & Constitution", "GEN"),
                 ("Research Methodology & IPR", "GEN"),
                 ("Interview Preparation Guide", "GEN"),
                 ("Project Management for Engineers", "GEN")]

COMPLAINTS = [("Electrical", "Fan not working"), ("Electrical", "Tube light flickering"),
              ("Plumbing", "Tap leaking in washroom"), ("Plumbing", "No hot water since morning"),
              ("Network", "Wi-Fi drops every evening"), ("Network", "LAN port dead"),
              ("Housekeeping", "Room not cleaned for 3 days"), ("Mess", "Breakfast served cold"),
              ("Carpentry", "Cupboard door hinge broken"), ("Electrical", "Power socket sparking")]
CREW = {"Electrical": "Electrical maintenance crew", "Plumbing": "Plumbing crew",
        "Network": "Campus IT / network team", "Housekeeping": "Housekeeping supervisor",
        "Mess": "Mess committee & caterer", "Carpentry": "Estate carpentry crew"}
COMPLAINT_SLA = {"Electrical": 12, "Plumbing": 12, "Network": 24, "Housekeeping": 24,
                 "Mess": 24, "Carpentry": 48}


def weekly_hours(kind, credits):
    """Contact hours per week, exactly as db.seed() quotas them."""
    return 6 if kind == "P" else 3 if kind == "L" else int(credits) + 1


# ====================================================================== ensure
def ensure(con=None):
    """Create missing campus tables and populate the empty ones. Idempotent."""
    own = con is None
    con = con or db.connect()
    con.executescript(SCHEMA)
    # Each populate step has its own derived stream so adding a new service later
    # does not reshuffle the data of the ones before it.
    steps = [("books", _seed_library), ("hostel_rooms", _seed_hostel),
             ("bus_routes", _seed_transport), ("gate_passes", _seed_gatepasses),
             ("placement_drives", _seed_placement), ("certificates", _seed_certificates)]
    for i, (table, fn) in enumerate(steps):
        if not con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            fn(con, random.Random(RNG_SEED * 31 + i))
    if not con.execute("SELECT 1 FROM attendance LIMIT 1").fetchone():
        _seed_attendance(con, random.Random(RNG_SEED * 97))
    con.commit()
    if own:
        con.close()


def _students(con, where="", args=()):
    return [dict(r) for r in con.execute(
        "SELECT * FROM students " + where + " ORDER BY usn", args).fetchall()]


def _syn_name(rng):
    pool = db.FIRST_M if rng.random() < .8 else db.FIRST_F
    return f"{rng.choice(pool)} {rng.choice(db.LAST)}"


def _phone(rng):
    return "9" + "".join(rng.choice("0123456789") for _ in range(9))


# ---------------------------------------------------------------- library
def _seed_library(con, rng):
    subs = [dict(r) for r in con.execute(
        "SELECT * FROM subjects WHERE dept<>'ALL' ORDER BY dept, sem, code").fetchall()]
    bid = 1
    books = []
    for s in subs:
        n = 1 if s["kind"] in ("L", "P") else 2
        forms = rng.sample(TITLE_FORMS, n)
        for f in forms:
            a1 = f"{rng.choice('ABCDGHKMNPRSV')}. {rng.choice(db.LAST)}"
            author = a1 if rng.random() < .55 else f"{a1} & {rng.choice('ABCDGHKMNPRSV')}. {rng.choice(db.LAST)}"
            copies = rng.randint(6, 16) if s["kind"] == "T" else rng.randint(2, 5)
            books.append((f"BK{bid:04d}", f.format(s=s["name"]), author, s["code"], s["dept"],
                          copies, f"{s['dept']}-R{rng.randint(1, 8)}-S{rng.randint(1, 5)}",
                          rng.randint(2014, 2025)))
            bid += 1
    for title, dept in GENERAL_BOOKS:
        books.append((f"BK{bid:04d}", title, f"{rng.choice('ABCDGHKMNPRSV')}. {rng.choice(db.LAST)}",
                      None, dept, rng.randint(6, 15), f"GEN-R{rng.randint(1, 4)}-S{rng.randint(1, 5)}",
                      rng.randint(2015, 2025)))
        bid += 1
    con.executemany("INSERT INTO books VALUES(?,?,?,?,?,?,?,?)", books)

    out = {b[0]: 0 for b in books}
    copies = {b[0]: b[5] for b in books}
    by_sub = {}
    for b in books:
        by_sub.setdefault(b[3], []).append(b[0])
    gen = [b[0] for b in books if b[4] == "GEN"]
    subj_of = {}
    for r in con.execute("SELECT code, dept, sem FROM subjects WHERE dept<>'ALL'"):
        subj_of.setdefault((r["dept"], r["sem"]), []).append(r["code"])
    loans = []
    for s in _students(con):
        k = rng.choices([0, 1, 2, 3], weights=[48, 30, 16, 6])[0]
        pool = [b for c in subj_of.get((s["dept"], s["sem"]), []) for b in by_sub.get(c, [])] + gen
        for b in rng.sample(pool, min(k, len(pool))):
            if out[b] >= copies[b]:
                continue
            # most loans are inside their 14 days; a fifth have run over
            back = rng.randint(0, 13) if rng.random() < .8 else rng.randint(15, 40)
            issued = TODAY - dt.timedelta(days=back)
            loans.append((b, s["usn"], "Student", issued.isoformat(),
                          (issued + dt.timedelta(days=LOAN_DAYS["Student"])).isoformat()))
            out[b] += 1
    dept_books = {}
    for b in books:
        dept_books.setdefault(b[4], []).append(b[0])
    for f in con.execute("SELECT id, dept FROM faculty ORDER BY id").fetchall():
        if rng.random() < .35:
            b = rng.choice(dept_books[f["dept"]])
            if out[b] < copies[b]:
                issued = TODAY - dt.timedelta(days=rng.randint(1, 40))
                loans.append((b, f["id"], "Faculty", issued.isoformat(),
                              (issued + dt.timedelta(days=LOAN_DAYS["Faculty"])).isoformat()))
                out[b] += 1
    con.executemany("INSERT INTO book_loans(book_id,member,member_kind,issued_on,due_on) "
                    "VALUES(?,?,?,?,?)", loans)


# ---------------------------------------------------------------- hostel
BLOCKS = [("KAV", "Kaveri Boys Hostel", "M"), ("NET", "Netravati Boys Hostel", "M"),
          ("SHA", "Sharavathi Girls Hostel", "F")]


def _seed_hostel(con, rng):
    host = _students(con, "WHERE hostel=1")
    by_g = {"M": [], "F": []}
    for s in host:
        by_g[gender_of(s["name"])].append(s)
    # Late admissions in the junior batch are the realistic waitlist: they are
    # what an allotment officer is actually chasing in the first weeks.
    wait_n = {"M": 6, "F": 4}
    rooms = []
    for g, members in by_g.items():
        juniors = [s for s in members if s["sem"] == 3]
        waitlist = set(s["usn"] for s in juniors[-wait_n[g]:])
        placed = [s for s in members if s["usn"] not in waitlist]
        placed.sort(key=lambda s: (s["sem"], s["dept"], s["section"], s["usn"]))
        blocks = [b for b in BLOCKS if b[2] == g]
        need_rooms = math.ceil(len(placed) / BED_PER_ROOM) + 5 * len(blocks)
        per_block = math.ceil(need_rooms / len(blocks))
        g_rooms = []
        for code, name, _g in blocks:
            for i in range(per_block):
                floor = i // 20 + 1
                rid = f"{code}-{floor}{(i % 20) + 1:02d}"
                g_rooms.append(rid)
                rooms.append((rid, name, g, floor, BED_PER_ROOM))
        # fill rooms in order, but leave a scattering of beds free so the
        # waitlist has real vacancies to be matched against
        slot, filled = 0, {}
        for s in placed:
            while True:
                rid = g_rooms[slot % len(g_rooms)]
                cap = BED_PER_ROOM - (1 if rng.random() < .06 and filled.get(rid, 0) == 2 else 0)
                if filled.get(rid, 0) < cap:
                    break
                slot += 1
            filled[rid] = filled.get(rid, 0) + 1
            con.execute("INSERT INTO hostel_allocations VALUES(?,?,?,?,?)",
                        (s["usn"], rid, f"20{25 if s['sem'] == 3 else 24 if s['sem'] == 5 else 23}-08-01",
                         rng.choice(["Veg", "Non-veg", "Non-veg"]),
                         rng.choice([0, 0, 0, 0, 0, 15000, 30000, 45000])))
            if filled[rid] >= BED_PER_ROOM:
                slot += 1
    con.executemany("INSERT INTO hostel_rooms VALUES(?,?,?,?,?)", rooms)
    alloc = [dict(r) for r in con.execute("SELECT * FROM hostel_allocations ORDER BY usn")]
    for i in range(14):
        a = rng.choice(alloc)
        cat, det = rng.choice(COMPLAINTS)
        raised = TODAY - dt.timedelta(days=rng.randint(0, 5))
        status = "Open" if i < 10 else rng.choice(["Assigned", "Resolved"])
        con.execute("""INSERT INTO hostel_complaints(room,usn,category,details,priority,status,
                       raised_on,assigned_to,sla_hrs) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (a["room"], a["usn"], cat, det,
                     "High" if cat == "Electrical" and "spark" in det else rng.choice(["Medium", "Low"]),
                     status, raised.isoformat(), CREW[cat] if status != "Open" else None,
                     COMPLAINT_SLA[cat]))


# ---------------------------------------------------------------- transport
def _seed_transport(con, rng):
    for rid, name, dep, km, stops in ROUTES:
        con.execute("INSERT INTO bus_routes VALUES(?,?,?,?,?,?,?,?,?)",
                    (rid, name, f"VT-BUS-{rid[1:]}", BUS_CAPACITY, _syn_name(rng), _phone(rng),
                     dep, km, "Active"))
        h, m = map(int, dep.split(":"))
        for i, st in enumerate(stops):
            t = h * 60 + m + i * rng.randint(7, 11)
            con.execute("INSERT INTO bus_stops VALUES(?,?,?,?)",
                        (rid, i + 1, st, f"{t // 60:02d}:{t % 60:02d}"))
    for n in (1, 2):
        con.execute("INSERT INTO bus_routes VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"SP{n}", f"Spare bus {n}", f"VT-BUS-S{n}", 40 if n == 1 else 52,
                     _syn_name(rng), _phone(rng), None, 0, "Spare"))
    routes = [r[0] for r in ROUTES]
    weights = [ROUTE_WEIGHT[r] for r in routes]
    stops = {r[0]: r[4] for r in ROUTES}
    for s in _students(con, "WHERE hostel=0"):
        if rng.random() > .55:
            continue                              # walks, cycles or own vehicle
        r = rng.choices(routes, weights=weights)[0]
        con.execute("INSERT INTO transport_riders VALUES(?,?,?,?)",
                    (s["usn"], r, rng.choice(stops[r]), rng.choice([0, 0, 0, 0, 0, 0, 9000, 18000])))


# ---------------------------------------------------------------- gate passes
def _seed_gatepasses(con, rng):
    host = [s for s in _students(con, "WHERE hostel=1")
            if con.execute("SELECT 1 FROM hostel_allocations WHERE usn=?", (s["usn"],)).fetchone()]
    day_sch = _students(con, "WHERE hostel=0")
    low = [s for s in host if s["attendance"] < 75]
    ok = [s for s in host if s["attendance"] >= 75]
    D = lambda days, hm: (TODAY + dt.timedelta(days=days)).isoformat() + "T" + hm
    # (student pool, kind, out, return, reason, consent) - each row is a case the
    # policy has to get right, and tests/campus_test.py pins the recommendation.
    cases = [
        (ok, "Outing", D(1, "14:00"), D(1, "19:30"), "Shopping in Udupi", 1),
        (ok, "Outing", D(2, "10:00"), D(2, "18:00"), "Temple visit with friends", 1),
        (ok, "Outing", D(0, "11:00"), D(0, "15:00"), "Bank work", 1),
        (ok, "Home visit", D(0, "16:45"), D(2, "18:00"), "Weekend at home", 1),
        (low, "Home visit", D(3, "08:00"), D(5, "19:00"), "Family function", 1),
        (ok, "Home visit", D(1, "13:30"), D(2, "20:00"), "Sister's engagement", 0),
        (host, "Medical", D(0, "10:30"), D(0, "13:00"), "Dental appointment at district hospital", 1),
        (host, "Emergency", D(0, "12:15"), D(3, "18:00"), "Grandparent hospitalised", 1),
        (ok, "Outing", D(2, "17:00"), D(2, "22:30"), "Friend's birthday dinner", 1),
        (ok, "Outing", D(1, "15:00"), D(1, "19:00"), "Haircut and groceries", 1),
        (low, "Outing", D(3, "10:00"), D(3, "13:00"), "Passport office appointment", 1),
        (ok, "Home visit", D(8, "13:30"), D(9, "19:00"), "Weekend at home", 1),
        (day_sch, "Early leave", D(0, "13:00"), D(0, "13:00"), "Leaving after lunch for a wedding", 1),
        (day_sch, "Medical", D(0, "11:10"), D(0, "11:10"), "Fever, parent picking up", 1),
    ]
    used = set()
    for pool, kind, out_at, ret, reason, consent in cases:
        s = rng.choice([p for p in pool if p["usn"] not in used])
        used.add(s["usn"])
        req = (dt.datetime.fromisoformat(out_at) - dt.timedelta(hours=rng.randint(6, 30)))
        req = min(req, dt.datetime.combine(TODAY, dt.time(8, 30)))
        con.execute("""INSERT INTO gate_passes(usn,kind,out_at,return_by,reason,parent_consent,
                       status,requested_at,decided_by,note) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (s["usn"], kind, out_at, ret, reason, consent, "Pending",
                     req.isoformat(timespec="minutes"), None, None))
    # a little history, so the register is not empty on day one
    for i in range(10):
        s = rng.choice(host)
        d = TODAY - dt.timedelta(days=rng.randint(1, 12))
        con.execute("""INSERT INTO gate_passes(usn,kind,out_at,return_by,reason,parent_consent,
                       status,requested_at,decided_by,note) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (s["usn"], "Outing", d.isoformat() + "T15:00", d.isoformat() + "T19:30",
                     "Evening outing", 1, "Returned", d.isoformat() + "T08:00", "Chief Warden",
                     "Returned on time"))


# ---------------------------------------------------------------- placement
def _seed_placement(con, rng):
    for comp, role, ctc, days, mc, mb, depts, dream in UPCOMING_DRIVES:
        con.execute("""INSERT INTO placement_drives(company,role,ctc,drive_date,min_cgpa,max_backlogs,
                       depts,dream,status) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (comp, role, ctc, (TODAY + dt.timedelta(days=days)).isoformat(), mc, mb,
                     depts, dream, "Upcoming"))
    # Individual offers that add up EXACTLY to the aggregate placements table, so
    # the dashboard KPI and the per-student view can never disagree.
    finals = _students(con, "WHERE sem=7")
    held = {}
    for p in con.execute("SELECT * FROM placements ORDER BY date, company").fetchall():
        depts = COMPANY_DEPTS.get(p["company"], "CSE,ISE,ECE,MECH,CIVIL").split(",")
        floor = COMPANY_MIN.get(p["company"], 6.0)
        pool = [s for s in finals if s["dept"] in depts and s["cgpa"] >= floor and s["backlogs"] == 0
                and held.get(s["usn"], 0) < 2]
        if len(pool) < p["offers"]:                  # widen before ever short-changing the total
            pool = [s for s in finals if s["dept"] in depts and held.get(s["usn"], 0) < 2]
        if len(pool) < p["offers"]:
            pool = [s for s in finals if held.get(s["usn"], 0) < 2]
        w = [(s["cgpa"] - 4.5) ** 2 * (0.35 if held.get(s["usn"]) else 1.0) for s in pool]
        chosen = []
        while len(chosen) < p["offers"]:             # weighted sampling WITHOUT replacement
            i = rng.choices(range(len(pool)), weights=w)[0]
            chosen.append(pool[i]["usn"])
            w[i] = 0.0
        for usn in chosen:
            held[usn] = held.get(usn, 0) + 1
            con.execute("INSERT INTO student_offers(usn,company,ctc,offered_on) VALUES(?,?,?,?)",
                        (usn, p["company"], p["ctc"], p["date"]))


# ---------------------------------------------------------------- certificates
def _seed_certificates(con, rng):
    """A short history in the register, so serials visibly continue from it."""
    studs = _students(con)
    for i, (kind, purpose) in enumerate([("Bonafide", "Education loan"),
                                         ("Bonafide", "Scholarship application"),
                                         ("Fee Structure", "Bank loan disbursement"),
                                         ("Conduct", "Internship onboarding")]):
        s = rng.choice(studs)
        d = TODAY - dt.timedelta(days=rng.randint(3, 20))
        code = {"Bonafide": "BON", "Fee Structure": "FEE", "Conduct": "CON"}[kind]
        serial = f"VIE/{code}/{d.year}/{i + 1:04d}"
        con.execute("""INSERT INTO certificates(serial,usn,kind,purpose,issued_on,issued_by,body,request_id)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (serial, s["usn"], kind, purpose, d.isoformat(), "Registrar", "", None))


# ---------------------------------------------------------------- attendance
def _seed_attendance(con, rng):
    """Per-subject attendance that is CONSISTENT with the headline figure.

    students.attendance is the one number the rest of the system has always
    used. Rows here are drawn around it and then reconciled so that the
    hours-weighted mean over a student's subjects reproduces it to within
    rounding - the subject view refines the headline, it never contradicts it.
    It inherits the headline's weakness: one independent draw per student.
    """
    WEEKS = 9                                           # early July to 04 Sep
    subj = {}
    for r in con.execute("SELECT * FROM subjects WHERE dept<>'ALL' ORDER BY code"):
        subj.setdefault((r["dept"], r["sem"]), []).append(dict(r))
    rows = []
    for s in _students(con):
        subs = subj.get((s["dept"], s["sem"]), [])
        held = [weekly_hours(x["kind"], x["credits"]) * WEEKS for x in subs]
        pct = [min(100.0, max(25.0, s["attendance"] + rng.gauss(0, 7))) for _ in subs]
        att = [round(h * p / 100) for h, p in zip(held, pct)]
        target = round(sum(held) * s["attendance"] / 100)
        i = 0
        while sum(att) != target and i < 10000:        # reconcile, one hour at a time
            j = i % len(att)
            if sum(att) < target and att[j] < held[j]:
                att[j] += 1
            elif sum(att) > target and att[j] > 0:
                att[j] -= 1
            i += 1
        for x, h, a in zip(subs, held, att):
            rows.append((s["usn"], x["code"], h, a))
    con.executemany("INSERT INTO attendance(usn,subject,held,attended) VALUES(?,?,?,?)", rows)
