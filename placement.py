"""
VidyaERP :: placement drives, end to end (#115)

A drive is a company's visit with its OWN eligibility criteria. The Registrar's
office (the placement cell, in this system) creates it as a draft, publishes it,
closes registration, completes, cancels or archives it; students see the drives
meant for them, are told criterion by criterion why they may or may not apply,
and apply; the office shortlists, selects and rejects applicants.

The eligibility engine is `evaluate()`, and it is the only one. Every answer -
the student's page, the agent's "am I eligible?", the Registrar's eligible
count, the existing drive_eligibility / shortlist / Ops radar views
(PlacementAgent.eligibility now delegates here) and the application write itself
- computes the same rows from the same data, and each row names the criterion,
what was required, what the student has and whether it is met. Three layers,
reported apart, as the issue asks:

  academic   CGPA, backlogs, 10th, 12th (or diploma for a lateral entrant)
  cohort     department, semester, section, graduating batch
  policy     the existing one-offer rule (PlacementAgent.DREAM_MULTIPLE)

and on top of them the application state: published, registration open, not
already applied, not cancelled. Nothing is assumed: a criterion a student has
no record for (a 10th percentage that is not on file) is not met, and says so.

10th, 12th and diploma percentages are new data (student_school_marks), seeded
from their own Random after everything else, drawn from each student's CGPA so
they are not independent of the record. About one student in twelve is a
lateral entrant from a diploma, with no 12th. Synthetic, like everything here.

Writes follow the campus shape: PROPOSE without approval, commit with it,
re-read before reporting. apply_to_drive acts only for the signed-in student
and needs its proposal staged first (portal._staged), because "apply" is an
approval word. Publishing a drive puts it on the academic calendar through the
calendar's own validation (academic_calendar.resolve), one entry per class
audience the drive is open to, and an edit moves those same entries rather
than adding new ones.
"""
import re, json, random, datetime as dt
from collections import Counter
import db, nlu
from nlu import TODAY
from guard import approved_this_turn
from agents import rows, one, B_text, B_table, B_cards, Auditor, NotifyAgent
from campus import _propose, _done, _err, _stu, _cls, NOW, PlacementAgent
import academic_calendar as cal

ACTOR = "registrar"
AGENT = "PlacementAgent"
STATUSES = ("Draft", "Published", "Completed", "Cancelled", "Archived")
DECISIONS = {"Shortlisted": "shortlist", "Not shortlisted": "not shortlist", "Selected": "select",
             "Rejected": "reject"}
OPEN_STATES = ("Applied", "Shortlisted")          # an application still being decided
FINAL_STATES = ("Selected", "Rejected", "Not shortlisted", "Withdrawn")
EMPLOYMENT = ("Full-time", "Internship", "Internship + full-time")
WORK_MODES = ("On-site", "Hybrid", "Remote")
REQUIRED = ("company", "role", "ctc", "drive_date", "reg_end", "depts", "sems")

DRIVE_COLS = {"description": "TEXT", "website": "TEXT", "location": "TEXT", "industry": "TEXT",
              "job_description": "TEXT", "job_location": "TEXT", "employment_type": "TEXT", "work_mode": "TEXT",
              "reg_start": "TEXT", "reg_end": "TEXT", "interview_date": "TEXT", "drive_time": "TEXT",
              "venue": "TEXT", "selection_process": "TEXT", "openings": "INT", "criteria": "TEXT",
              "reg_closed": "INT DEFAULT 0", "calendar_ids": "TEXT", "created_by": "TEXT", "created_at": "TEXT",
              "updated_by": "TEXT", "updated_at": "TEXT"}
REG_COLS = {"submitted": "TEXT", "updated_by": "TEXT", "updated_at": "TEXT"}
SCHOOL_DDL = """CREATE TABLE IF NOT EXISTS student_school_marks(
  usn TEXT PRIMARY KEY, entry TEXT, tenth REAL, twelfth REAL, diploma REAL)"""
LATERAL_SHARE = 1 / 12

# What the seeded drives' companies say about themselves. Invented, like the
# companies; the websites are under .example, which can never be a real site.
SEED_DETAILS = {
    "Nethra Robotics": ("Industrial automation and robot cells for small manufacturers.", "Manufacturing automation",
                        "Mangaluru", "Commission and maintain robot cells at customer plants; PLC and vision setup.",
                        "On-site", 12),
    "Konkan Cloud Systems": ("Cloud infrastructure and managed Kubernetes for regional banks.", "Cloud software",
                             "Bengaluru", "Build and run backend services in Go and Python on Kubernetes.",
                             "Hybrid", 8),
    "Sahyadri Infra Projects": ("Roads, bridges and water works across coastal Karnataka.", "Infrastructure",
                                "Udupi", "Site execution, quantity surveying and quality checks on road works.",
                                "On-site", 15),
    "Coastal Analytics": ("Analytics for fisheries, ports and coastal logistics.", "Data and analytics",
                          "Mangaluru", "Build dashboards and forecasting models over logistics data in SQL and Python.",
                          "Hybrid", 10),
    "Tulunadu Semiconductors": ("Mixed-signal chip design for power electronics.", "Semiconductors",
                                "Bengaluru", "RTL design and verification for power-management ICs.", "On-site", 6),
}


# ===================================================================== data
def ensure(con=None):
    """Additive to any older college.db: the new drive and registration
    columns, the school-marks table and its seed, the seeded drives' details
    and their calendar entries. Idempotent."""
    own = con is None
    con = con or db.connect()
    have = {r["name"] for r in rows(con, "PRAGMA table_info(placement_drives)")}
    for c, t in DRIVE_COLS.items():
        if c not in have:
            con.execute(f"ALTER TABLE placement_drives ADD COLUMN {c} {t}")
    have = {r["name"] for r in rows(con, "PRAGMA table_info(drive_registrations)")}
    for c, t in REG_COLS.items():
        if c not in have:
            con.execute(f"ALTER TABLE drive_registrations ADD COLUMN {c} {t}")
    con.execute(SCHOOL_DDL)
    if not con.execute("SELECT 1 FROM student_school_marks LIMIT 1").fetchone():
        _seed_school(con, random.Random(1150))
    con.execute("UPDATE placement_drives SET status='Published' WHERE status='Upcoming'")   # the old word for it
    for d in rows(con, "SELECT * FROM placement_drives WHERE criteria IS NULL ORDER BY id"):
        _backfill(con, d)
    con.commit()
    if own:
        con.close()


def _clamp(x, lo, hi):
    return round(min(hi, max(lo, x)), 1)


def _seed_school(con, rng):
    out = []
    for s in rows(con, "SELECT usn, cgpa FROM students ORDER BY usn"):
        lateral = rng.random() < LATERAL_SHARE
        tenth = _clamp(rng.gauss(50 + 4.6 * s["cgpa"], 7), 40, 99)
        if lateral:
            out.append((s["usn"], "Lateral", tenth, None, _clamp(rng.gauss(48 + 4.8 * s["cgpa"], 7), 45, 97)))
        else:
            out.append((s["usn"], "Regular", tenth, _clamp(rng.gauss(46 + 4.8 * s["cgpa"], 8), 40, 98), None))
    con.executemany("INSERT INTO student_school_marks VALUES(?,?,?,?,?)", out)


def _backfill(con, d):
    """A seeded (or pre-#115) drive gets its criteria as one record, its
    details, a registration window and - when published - its calendar entries."""
    desc, ind, loc, job, mode, openings = SEED_DETAILS.get(d["company"], ("", "", "", "", "On-site", None))
    crit = {"min_cgpa": d["min_cgpa"], "max_backlogs": d["max_backlogs"], "depts": d["depts"].split(","),
            "sems": [7], "sections": [], "batches": [], "min_tenth": None, "min_twelfth": None, "min_diploma": None}
    drive_day = dt.date.fromisoformat(d["drive_date"])
    slug = re.sub(r"[^a-z0-9]+", "-", d["company"].lower()).strip("-")
    con.execute("""UPDATE placement_drives SET criteria=?, description=?, industry=?, location=?, job_description=?,
                   job_location=?, work_mode=?, employment_type='Full-time', openings=?, website=?, reg_start=?,
                   reg_end=?, drive_time='09:30', venue='Placement cell, seminar hall 2',
                   selection_process='Aptitude test · technical interview · HR interview', reg_closed=0,
                   created_by='seed', created_at='2026-08-20T10:00:00' WHERE id=?""",
                (json.dumps(crit), desc, ind, loc, job, loc, mode, openings, f"https://{slug}.example",
                 "2026-08-25", (drive_day - dt.timedelta(days=4)).isoformat(), d["id"]))
    d = one(con, "SELECT * FROM placement_drives WHERE id=?", (d["id"],))
    if d["status"] == "Published":
        # entries a previous backfill already put up for this drive are reused,
        # never duplicated (a campus table rebuilt in an existing college.db) -
        # and looked for first, because the calendar refuses a duplicate entry
        have = [r["id"] for r in rows(con, "SELECT id FROM calendar_events WHERE status='Active' AND title=? AND "
                                           "date=? AND description LIKE ? ORDER BY id",
                                      (f"Placement drive: {d['company']} ({d['role']})", d["drive_date"],
                                       f"Drive #{d['id']}.%"))]
        if have and len(have) == len(_audiences(d)):
            ids = have
        else:
            evs, _why = calendar_plan(con, d)
            ids = [cal._insert(con, e, "seed", "2026-08-20T10:00:00") for e in evs or []]
        if ids:
            con.execute("UPDATE placement_drives SET calendar_ids=? WHERE id=?", (",".join(map(str, ids)), d["id"]))


def school(con, usn):
    return one(con, "SELECT * FROM student_school_marks WHERE usn=?", (usn,))


def grad_year(sem):
    """The year a student in `sem` graduates: an 8-semester degree, two
    semesters a year, counted from this term's academic year."""
    return int(cal.TERM[0][:4]) + (8 - int(sem)) // 2 + 1


def criteria(d):
    c = json.loads(d["criteria"]) if d.get("criteria") else {}
    c.setdefault("min_cgpa", d.get("min_cgpa") or 0)
    c.setdefault("max_backlogs", d.get("max_backlogs") if d.get("max_backlogs") is not None else 0)
    c.setdefault("depts", (d.get("depts") or "").split(",") if d.get("depts") else [])
    for k in ("sems", "sections", "batches"):
        c.setdefault(k, [])
    for k in ("min_tenth", "min_twelfth", "min_diploma"):
        c.setdefault(k, None)
    return c


def _ord(n):
    n = int(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ===================================================== the eligibility engine
def evaluate(con, d, s, best=None, reg=None, sch=None):
    """One student against one drive, criterion by criterion. Never a bare
    yes/no: every row says what was required, what the student has, and
    whether it is met, and the layers are reported apart."""
    c = criteria(d)
    sch = sch if sch is not None else school(con, s["usn"]) or {}
    out = []

    def row(key, label, layer, required, actual, ok):
        out.append({"key": key, "label": label, "layer": layer, "required": required, "actual": actual, "ok": bool(ok)})

    row("dept", "Department", "cohort", ", ".join(c["depts"]) or "none", s["dept"], s["dept"] in c["depts"])
    if c["sems"]:
        row("sem", "Semester", "cohort", ", ".join(_ord(x) for x in c["sems"]), _ord(s["sem"]), s["sem"] in c["sems"])
    if c["sections"]:
        row("section", "Section", "cohort", ", ".join(c["sections"]), s["section"], s["section"] in c["sections"])
    if c["batches"]:
        gy = grad_year(s["sem"])
        row("batch", "Graduating batch", "cohort", ", ".join(map(str, c["batches"])), str(gy), gy in c["batches"])
    row("cgpa", "CGPA", "academic", f"{float(c['min_cgpa']):.2f} or more", f"{s['cgpa']:.2f}",
        s["cgpa"] >= float(c["min_cgpa"]))
    row("backlogs", "Backlogs", "academic", f"{int(c['max_backlogs'])} or fewer", str(s["backlogs"]),
        s["backlogs"] <= int(c["max_backlogs"]))
    if c["min_tenth"] is not None:
        v = sch.get("tenth")
        row("tenth", "10th", "academic", f"{c['min_tenth']:g}% or more", "not on record" if v is None else f"{v:g}%",
            v is not None and v >= c["min_tenth"])
    if c["min_twelfth"] is not None or c["min_diploma"] is not None:
        if sch.get("entry") == "Lateral":
            need = c["min_diploma"] if c["min_diploma"] is not None else c["min_twelfth"]
            v = sch.get("diploma")
            row("diploma", "Diploma (lateral entry, in place of 12th)", "academic", f"{need:g}% or more",
                "not on record" if v is None else f"{v:g}%", v is not None and v >= need)
        elif c["min_twelfth"] is not None:
            v = sch.get("twelfth")
            row("twelfth", "12th", "academic", f"{c['min_twelfth']:g}% or more",
                "not on record" if v is None else f"{v:g}%", v is not None and v >= c["min_twelfth"])
    b = (best if best is not None else PlacementAgent().best_offer(con)).get(s["usn"])
    if b:
        ok = bool(d["dream"]) and d["ctc"] >= PlacementAgent.DREAM_MULTIPLE * b
        row("policy", "One-offer rule", "policy",
            f"no offer yet, or a dream drive paying {PlacementAgent.DREAM_MULTIPLE:g}x the best offer",
            f"holds an offer of ₹{b:g} LPA", ok)
    else:
        row("policy", "One-offer rule", "policy", "no offer yet, or a dream drive", "no offer yet", True)
    layer_ok = {k: all(r["ok"] for r in out if r["layer"] == k) for k in ("cohort", "academic", "policy")}
    eligible = all(layer_ok.values())
    reg = reg if reg is not None else one(con, "SELECT * FROM drive_registrations WHERE drive_id=? AND usn=?",
                                          (d["id"], s["usn"]))
    window = registration(d)
    if reg:
        action = reg["status"]
    elif not eligible:
        action = "Not eligible"
    elif window != "Open":
        action = window
    else:
        action = "Apply"
    return {"drive": d["id"], "usn": s["usn"], "criteria": out, "cohort_ok": layer_ok["cohort"],
            "academic_ok": layer_ok["academic"], "policy_ok": layer_ok["policy"], "eligible": eligible,
            "failed": [r for r in out if not r["ok"]], "registration": window,
            "application": reg["status"] if reg else None, "action": action, "can_apply": action == "Apply"}


def registration(d):
    """Whether a drive is taking applications today, in words."""
    if d["status"] == "Cancelled":
        return "Cancelled"
    if d["status"] in ("Completed", "Archived"):
        return "Drive closed"
    if d["status"] == "Draft":
        return "Not published"
    if d.get("reg_closed") or (d.get("reg_end") and TODAY.isoformat() > d["reg_end"]) \
            or TODAY.isoformat() > d["drive_date"]:
        return "Registration closed"
    if d.get("reg_start") and TODAY.isoformat() < d["reg_start"]:
        return f"Opens {d['reg_start']}"
    return "Open"


def reasons(ev):
    """'CGPA 6.40, needs 7.00 or more' for every unmet criterion."""
    return [f"{r['label']}: {r['actual']}, needs {r['required']}" for r in ev["failed"]]


def cohort_eligibility(con, d):
    """(eligible students, Counter of why the rest of the cohort is not) - the
    numbers the Registrar sees for a drive. The cohort is everyone the drive's
    department, semester, section and batch criteria take in; each of them who
    misses counts once against every criterion they miss, the one-offer rule
    as 'already placed' as it always was."""
    best = PlacementAgent().best_offer(con)
    sch = {r["usn"]: r for r in rows(con, "SELECT * FROM student_school_marks")}
    regs = {r["usn"]: r for r in rows(con, "SELECT * FROM drive_registrations WHERE drive_id=?", (d["id"],))}
    c = criteria(d)
    q, a = "SELECT * FROM students WHERE 1=1", []
    if c["depts"]:
        q += f" AND dept IN ({','.join('?' * len(c['depts']))})"
        a += c["depts"]
    if c["sems"]:
        q += f" AND sem IN ({','.join('?' * len(c['sems']))})"
        a += [int(x) for x in c["sems"]]
    ok, why = [], Counter()
    for s in rows(con, q + " ORDER BY cgpa DESC, usn", tuple(a)):
        ev = evaluate(con, d, s, best=best, reg=regs.get(s["usn"]) or {}, sch=sch.get(s["usn"]) or {})
        if not ev["cohort_ok"]:
            continue
        if ev["eligible"]:
            ok.append({**s, "best_offer": best.get(s["usn"])})
        for r in ev["failed"]:
            why["already placed" if r["key"] == "policy" else r["key"]] += 1
    return ok, why


WHY_LABEL = {"cgpa": "Below CGPA cut-off", "backlogs": "Backlogs", "tenth": "Below 10th cut-off",
             "twelfth": "Below 12th cut-off", "diploma": "Below diploma cut-off",
             "already placed": "Held by one-offer rule"}


def resolve(con, ref, student=False):
    """A drive by id or company name. A student never finds a draft or an
    archived drive: to them it does not exist yet, or any more."""
    t = str(ref or "").strip().lower()
    ds = rows(con, "SELECT * FROM placement_drives ORDER BY id")
    if student:
        ds = [d for d in ds if d["status"] not in ("Draft", "Archived")]
    m = re.fullmatch(r"#?\s*(\d+)", t)
    if m:
        return next((d for d in ds if d["id"] == int(m.group(1))), None)
    hit = [d for d in ds if d["company"].lower() in t or (len(t) >= 4 and t in d["company"].lower())]
    if not hit:
        hit = [d for d in ds if any(w in t for w in re.findall(r"[a-z]{5,}", d["company"].lower()))]
    live = [d for d in hit if d["status"] in ("Published", "Draft")]
    return (live or hit or [None])[-1]


# ========================================================== calendar sync
def _audiences(d):
    """(dept, sem) pairs a drive's calendar entries name; sem None = every semester."""
    c = criteria(d)
    return [(dept, int(sem) if sem is not None else None) for dept in c["depts"] for sem in (c["sems"] or [None])]


def calendar_plan(con, d):
    """The calendar entries a published drive puts up, each validated by the
    calendar's own rule (academic_calendar.resolve): one per department and
    semester it is open to, so it reaches exactly those classes."""
    evs = []
    for dept, sem in _audiences(d):
        ev, why = cal.resolve(con, {"title": f"Placement drive: {d['company']} ({d['role']})", "kind": "event",
                                    "date": d["drive_date"], "start_time": d.get("drive_time") or None,
                                    "venue": d.get("venue") or None, "dept": dept, "sem": sem,
                                    "description": f"Drive #{d['id']}. Registration closes {d.get('reg_end') or '—'}. "
                                                   f"Open Placement drives in the portal for eligibility and to apply."})
        if why:
            return None, f"The calendar refuses the drive date: {why}"
        evs.append(ev)
    return evs, None


def _cal_ids(d):
    return [int(x) for x in str(d.get("calendar_ids") or "").split(",") if x.strip().isdigit()]


def _sync_calendar(con, d, who, now):
    """Put the drive on the calendar, or move its entries: the same ids are
    updated, never duplicated. Returns the ids now holding it."""
    evs, why = calendar_plan(con, d)
    if why:
        raise ValueError(why)
    have = [i for i in _cal_ids(d) if one(con, "SELECT 1 FROM calendar_events WHERE id=?", (i,))]
    ids = []
    for i, ev in enumerate(evs):
        if i < len(have):
            con.execute(f"UPDATE calendar_events SET {', '.join(f + '=?' for f in cal.FIELDS)}, status='Active', "
                        f"updated_by=?, updated_at=? WHERE id=?", [ev[f] for f in cal.FIELDS] + [who, now, have[i]])
            ids.append(have[i])
        else:
            ids.append(cal._insert(con, ev, who, now))
    for extra in have[len(evs):]:                     # an audience the edit dropped
        con.execute("UPDATE calendar_events SET status='Cancelled', updated_by=?, updated_at=? WHERE id=?",
                    (who, now, extra))
    con.execute("UPDATE placement_drives SET calendar_ids=? WHERE id=?", (",".join(map(str, ids)), d["id"]))
    return ids


# =================================================================== views
def _drive_rows(d):
    c = criteria(d)
    def pct(k):
        return f"{c[k]:g}%" if c.get(k) is not None else "—"
    return [["Company", d["company"]], ["About", d.get("description") or "—"], ["Industry", d.get("industry") or "—"],
            ["Website", d.get("website") or "—"], ["Company location", d.get("location") or "—"],
            ["Role", d["role"]], ["Job description", d.get("job_description") or "—"],
            ["Job location", d.get("job_location") or "—"], ["Employment", d.get("employment_type") or "—"],
            ["Work mode", d.get("work_mode") or "—"], ["CTC", f"₹{d['ctc']:g} LPA" + (" · dream drive" if d["dream"] else "")],
            ["Drive", f"{d['drive_date']}" + (f" {d['drive_time']}" if d.get("drive_time") else "")
             + (f" · {d['venue']}" if d.get("venue") else "")],
            ["Registration", f"{d.get('reg_start') or '—'} to {d.get('reg_end') or '—'}"],
            ["Interview", d.get("interview_date") or "—"], ["Selection process", d.get("selection_process") or "—"],
            ["Openings", d.get("openings") or "—"],
            ["Departments", ", ".join(c["depts"])], ["Semesters", ", ".join(_ord(x) for x in c["sems"]) or "any"],
            ["Sections", ", ".join(c["sections"]) or "all"], ["Graduating batch", ", ".join(map(str, c["batches"])) or "any"],
            ["Minimum CGPA", f"{float(c['min_cgpa']):.2f}"], ["Backlogs allowed", c["max_backlogs"]],
            ["10th", pct("min_tenth")], ["12th", pct("min_twelfth")], ["Diploma (lateral entry)", pct("min_diploma")],
            ["Status", d["status"] + ("" if d["status"] != "Published" else f" · registration {registration(d).lower()}")]]


def _public(d):
    c = criteria(d)
    return {"id": d["id"], "company": d["company"], "role": d["role"], "ctc": d["ctc"], "drive_date": d["drive_date"],
            "drive_time": d.get("drive_time"), "venue": d.get("venue"), "location": d.get("location"),
            "job_location": d.get("job_location"), "work_mode": d.get("work_mode"),
            "employment_type": d.get("employment_type"), "description": d.get("description"),
            "job_description": d.get("job_description"), "industry": d.get("industry"), "website": d.get("website"),
            "reg_start": d.get("reg_start"), "reg_end": d.get("reg_end"), "interview_date": d.get("interview_date"),
            "selection_process": d.get("selection_process"), "openings": d.get("openings"), "dream": bool(d["dream"]),
            "status": d["status"], "registration": registration(d), "criteria": c}


def t_placement_drives(con, S, U, usn=None, status=None, **kw):
    ensure(con)
    if usn:
        s = _stu(con, str(usn).upper())
        if not s:
            return _err(f"No student with USN {usn}.")
        best = PlacementAgent().best_offer(con)
        groups = {"eligible": [], "applied": [], "other": [], "closed": []}
        for d in rows(con, "SELECT * FROM placement_drives WHERE status NOT IN ('Draft','Archived') "
                           "ORDER BY drive_date, id"):
            ev = evaluate(con, d, s, best=best)
            item = {**_public(d), "eligible": ev["eligible"], "action": ev["action"], "reasons": reasons(ev),
                    "in_cohort": ev["cohort_ok"]}
            taking = d["status"] == "Published" and (ev["registration"] == "Open"
                                                     or ev["registration"].startswith("Opens"))
            if ev["application"]:
                groups["applied"].append(item)
            elif not taking:
                groups["closed"].append(item)
            elif ev["eligible"]:
                groups["eligible"].append(item)
            else:
                groups["other"].append(item)
        offers = rows(con, "SELECT company, ctc FROM student_offers WHERE usn=? ORDER BY ctc DESC", (s["usn"],))
        blocks = []
        if offers:
            blocks.append(B_text("Offers: " + ", ".join(f"**{o['company']}** ₹{o['ctc']:g} LPA" for o in offers)))
        for k, title in (("eligible", "Drives you can apply for"), ("applied", "Your applications"),
                         ("other", "Open drives you are not eligible for"), ("closed", "Closed drives")):
            if groups[k]:
                blocks.append(B_table(["Company", "Role", "CTC", "Drive", "You"],
                                      [[x["company"], x["role"], f"₹{x['ctc']:g} LPA", x["drive_date"],
                                        x["action"] if k != "other" else "; ".join(x["reasons"][:2])]
                                       for x in groups[k]], title=title, dense=True))
        if not any(groups.values()):
            blocks.append(B_text("No placement drives are published yet."))
        return {"data": {"usn": s["usn"], "class": _cls(s), **groups, "offers": offers,
                         "eligible_for": [x["company"] for x in groups["eligible"]],
                         "applied_to": [{"company": x["company"], "status": x["action"]} for x in groups["applied"]]},
                "blocks": blocks,
                "trace": [(AGENT, "student_drives", f"{s['usn']}: {len(groups['eligible'])} eligible · "
                                                    f"{len(groups['applied'])} applied")]}
    q = "SELECT * FROM placement_drives" + (" WHERE status=?" if status else "") + " ORDER BY drive_date, id"
    ds = rows(con, q, (str(status).title(),) if status else ())
    out = []
    for d in ds:
        ok, why = cohort_eligibility(con, d)
        cnt = Counter(r["status"] for r in rows(con, "SELECT status FROM drive_registrations WHERE drive_id=?",
                                                (d["id"],)))
        out.append({**_public(d), "eligible": len(ok), "applications": sum(cnt.values()),
                    "shortlisted": cnt.get("Shortlisted", 0), "selected": cnt.get("Selected", 0)})
    return {"data": {"drives": out},
            "blocks": [B_table(["#", "Company", "Role", "CTC", "Drive", "Status", "Eligible", "Applied", "Shortlisted",
                                "Selected"],
                               [[x["id"], x["company"], x["role"], f"₹{x['ctc']:g} LPA", x["drive_date"],
                                 x["status"] + (f" · {x['registration'].lower()}" if x["status"] == "Published" else ""),
                                 x["eligible"], x["applications"], x["shortlisted"], x["selected"]] for x in out],
                               title="Placement drives", dense=True)],
            "trace": [(AGENT, "drives", f"{len(out)} drive(s)")]}


def t_drive_details(con, S, U, drive="", usn=None, **kw):
    ensure(con)
    student = bool(usn)
    d = resolve(con, drive, student=student)
    if not d:
        return _err(f"No placement drive matches '{drive}'.",
                    drives=[x["company"] for x in rows(con, "SELECT company FROM placement_drives WHERE status "
                                                            "NOT IN ('Draft','Archived')")])
    blocks = [B_table(["", ""], _drive_rows(d), title=f"{d['company']} · {d['role']}")]
    data = {"drive": _public(d)}
    if student:
        s = _stu(con, str(usn).upper())
        if not s:
            return _err(f"No student with USN {usn}.")
        ev = evaluate(con, d, s)
        data["you"] = ev
        head = (f"You are eligible for {d['company']}." if ev["eligible"] else
                f"You are not eligible for {d['company']}.")
        layers = (f"Academic criteria: {'met' if ev['academic_ok'] else 'not met'} · "
                  f"department, semester and batch: {'met' if ev['cohort_ok'] else 'not met'} · "
                  f"placement policy: {'met' if ev['policy_ok'] else 'not met'}.")
        blocks.insert(0, B_table(["Criterion", "Required", "You", ""],
                                 [[r["label"], r["required"], r["actual"], "Met" if r["ok"] else "Not met"]
                                  for r in ev["criteria"]], title=head))
        blocks.insert(1, B_text(layers + (" " + "; ".join(reasons(ev)) + "." if ev["failed"] else
                                          " Every criterion is met.")
                                + f" Application: **{ev['action']}**."))
        trace = [(AGENT, "evaluate", f"{s['usn']} → {d['company']}: {'eligible' if ev['eligible'] else 'not eligible'}"
                                     f" · {ev['action']}")]
        chips = [f"Apply for the {d['company']} drive"] if ev["can_apply"] else ["Show my placement applications"]
    else:
        ok, why = cohort_eligibility(con, d)
        data.update({"eligible": len(ok), "excluded": dict(why)})
        blocks.insert(0, B_cards([{"label": "Eligible", "value": len(ok), "tone": "good"}]
                                 + [{"label": WHY_LABEL.get(k, k), "value": v} for k, v in sorted(why.items())],
                                 title=f"{d['company']} · {d['status']}"))
        trace = [(AGENT, "drive", f"{d['company']}: {len(ok)} eligible · excluded {dict(why)}")]
        chips = [f"Applications for the {d['company']} drive"]
    return {"data": data, "blocks": blocks, "trace": trace, "chips": chips}


def t_drive_applications(con, S, U, drive="", dept=None, sem=None, status=None, min_cgpa=None, max_backlogs=None,
                         **kw):
    ensure(con)
    d = resolve(con, drive)
    if not d:
        return _err(f"No placement drive matches '{drive}'.")
    q = """SELECT r.usn, r.status, r.registered_at, s.name, s.dept, s.sem, s.section, s.cgpa, s.backlogs
           FROM drive_registrations r JOIN students s ON s.usn=r.usn WHERE r.drive_id=?"""
    a = [d["id"]]
    for col, op, v in (("s.dept", "=", str(dept).upper() if dept else None), ("s.sem", "=", int(sem) if sem else None),
                       ("r.status", "=", str(status).capitalize() if status else None),
                       ("s.cgpa", ">=", float(min_cgpa) if min_cgpa not in (None, "") else None),
                       ("s.backlogs", "<=", int(max_backlogs) if max_backlogs not in (None, "") else None)):
        if v is not None:
            q += f" AND {col}{op}?"
            a.append(v)
    got = rows(con, q + " ORDER BY s.cgpa DESC, r.usn", tuple(a))
    allc = Counter(r["status"] for r in rows(con, "SELECT status FROM drive_registrations WHERE drive_id=?",
                                             (d["id"],)))
    ok, _why = cohort_eligibility(con, d)
    cards = [{"label": "Applications", "value": sum(allc.values())}, {"label": "Eligible", "value": len(ok)},
             {"label": "Shortlisted", "value": allc.get("Shortlisted", 0)},
             {"label": "Selected", "value": allc.get("Selected", 0), "tone": "good"}]
    return {"data": {"drive": d["company"], "drive_id": d["id"], "counts": dict(allc), "eligible": len(ok),
                     "applicants": [{k: r[k] for k in ("usn", "name", "dept", "sem", "section", "cgpa", "backlogs",
                                                       "status", "registered_at")} for r in got]},
            "blocks": [B_cards(cards, title=f"{d['company']} · {d['role']}"),
                       B_table(["USN", "Name", "Class", "CGPA", "Backlogs", "Status"],
                               [[r["usn"], r["name"], f"{r['dept']}-{r['sem']}{r['section']}", f"{r['cgpa']:.2f}",
                                 r["backlogs"], r["status"]] for r in got],
                               title=f"Applicants ({len(got)} shown)", dense=True)
                       if got else B_text("No applicants match.")],
            "trace": [(AGENT, "applications", f"{d['company']}: {len(got)} shown of {sum(allc.values())}")]}


# ================================================================== writes
def _num(v, lo, hi, what, whole=False):
    if v in (None, ""):
        return None
    try:
        x = int(v) if whole else float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number, not {v!r}.")
    if (whole and float(v) != int(float(v))) or not (lo <= x <= hi):
        raise ValueError(f"{what} must be {'a whole number ' if whole else ''}between {lo:g} and {hi:g}.")
    return x


def _list(v):
    if v in (None, ""):
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,/;]|\band\b", str(v)) if x.strip()]


def _date(v, what):
    if v in (None, ""):
        return None
    s = str(v).strip()
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        d, _l = nlu.parse_date(s)
        if not d:
            raise ValueError(f"{what}: '{s}' is not a date I can read; use YYYY-MM-DD.")
        return d


def _shape(con, f):
    """Form fields -> (the drive as it would be stored, None) or (None, why).
    Every rule is a refusal; nothing that changes who may apply is defaulted."""
    try:
        company = re.sub(r"\s+", " ", str(f.get("company") or "")).strip()
        role = re.sub(r"\s+", " ", str(f.get("role") or "")).strip()
        missing = [k for k in REQUIRED if f.get(k) in (None, "", [])]
        if missing:
            return None, "Required before anything is proposed: " + ", ".join(m.replace("_", " ") for m in missing) + "."
        if len(company) > 80 or len(role) > 80:
            return None, "Company and role are names, at most 80 characters each."
        ctc = _num(f.get("ctc"), 0.5, 100, "CTC (lakh per annum)")
        drive_day, reg_end = _date(f.get("drive_date"), "Drive date"), _date(f.get("reg_end"), "Registration end")
        reg_start = _date(f.get("reg_start"), "Registration start") or TODAY
        interview = _date(f.get("interview_date"), "Interview date")
        depts = [x.upper() for x in _list(f.get("depts"))]
        sems = [_num(x, 1, 8, "Semester", whole=True) for x in _list(f.get("sems"))]
        sections = [x.upper() for x in _list(f.get("sections"))]
        batches = [_num(x, 2000, 2100, "Graduating batch", whole=True) for x in _list(f.get("batches"))]
        crit = {"min_cgpa": _num(f.get("min_cgpa"), 0, 10, "Minimum CGPA") or 0.0,
                "max_backlogs": _num(f.get("max_backlogs"), 0, 20, "Backlogs allowed", whole=True) or 0,
                "min_tenth": _num(f.get("min_tenth"), 0, 100, "10th percentage"),
                "min_twelfth": _num(f.get("min_twelfth"), 0, 100, "12th percentage"),
                "min_diploma": _num(f.get("min_diploma"), 0, 100, "Diploma percentage"),
                "depts": depts, "sems": sems, "sections": sections, "batches": batches}
        openings = _num(f.get("openings"), 1, 5000, "Openings", whole=True)
        tm = str(f.get("drive_time") or "").strip()
        drive_time = cal.parse_time(tm) if tm else None
    except ValueError as e:
        return None, str(e)
    bad = [x for x in depts if x not in db.DEPTS]
    if bad:
        return None, f"No department {', '.join(bad)}. Departments: {', '.join(db.DEPTS)}."
    on_rolls = {r["sem"] for r in rows(con, "SELECT DISTINCT sem FROM students")}
    if [x for x in sems if x not in on_rolls]:
        return None, (f"Nobody is on the rolls in semester {', '.join(str(x) for x in sems if x not in on_rolls)}. "
                      f"Semesters running: {', '.join(str(x) for x in sorted(on_rolls))}.")
    secs = {r["section"] for r in rows(con, "SELECT DISTINCT section FROM students")}
    if [x for x in sections if x not in secs]:
        return None, f"No section {', '.join(x for x in sections if x not in secs)}. Sections: {', '.join(sorted(secs))}."
    years = {grad_year(x) for x in on_rolls}
    if [x for x in batches if x not in years]:
        return None, (f"No student graduates in {', '.join(str(x) for x in batches if x not in years)}. "
                      f"Batches on the rolls: {', '.join(str(x) for x in sorted(years))}.")
    if drive_day < TODAY:
        return None, f"The drive date {drive_day} has passed."
    if not (reg_start <= reg_end <= drive_day):
        return None, (f"Registration must open ({reg_start}) on or before it closes ({reg_end}), and close on or "
                      f"before the drive ({drive_day}).")
    if interview and interview < drive_day:
        return None, f"The interview ({interview}) cannot be before the drive ({drive_day})."
    web = str(f.get("website") or "").strip()
    if web and not re.match(r"https?://[^\s/]+\.[^\s]+$", web):
        return None, "The website must be an address starting http:// or https://."
    emp = str(f.get("employment_type") or "Full-time").strip()
    mode = str(f.get("work_mode") or "On-site").strip()
    emp = next((x for x in EMPLOYMENT if x.lower() == emp.lower()), None)
    mode = next((x for x in WORK_MODES if x.lower() == mode.lower()), None)
    if not emp or not mode:
        return None, f"Employment is one of {', '.join(EMPLOYMENT)}; work mode one of {', '.join(WORK_MODES)}."
    txt = {k: (re.sub(r"[ \t]+", " ", str(f.get(k) or "")).strip() or None)
           for k in ("description", "industry", "location", "job_description", "job_location", "selection_process",
                     "venue")}
    return {"company": company, "role": role, "ctc": ctc, "drive_date": drive_day.isoformat(),
            "reg_start": reg_start.isoformat(), "reg_end": reg_end.isoformat(),
            "interview_date": interview.isoformat() if interview else None, "drive_time": drive_time,
            "openings": openings, "website": web or None, "employment_type": emp, "work_mode": mode,
            "dream": 1 if str(f.get("dream")).lower() in ("1", "true", "yes", "on") else 0,
            "criteria": crit, **txt}, None


SAVE_FIELDS = ("company", "role", "ctc", "drive_date", "reg_start", "reg_end", "interview_date", "drive_time",
               "openings", "website", "employment_type", "work_mode", "dream", "description", "industry", "location",
               "job_description", "job_location", "selection_process", "venue")
CRIT_FIELDS = ("min_cgpa", "max_backlogs", "min_tenth", "min_twelfth", "min_diploma", "depts", "sems", "sections",
               "batches")


def _preview(con, nd, did=0):
    probe = {**nd, "id": did, "criteria": json.dumps(nd["criteria"]), "status": "Published", "reg_closed": 0,
             "min_cgpa": nd["criteria"]["min_cgpa"], "max_backlogs": nd["criteria"]["max_backlogs"],
             "depts": ",".join(nd["criteria"]["depts"])}
    return cohort_eligibility(con, probe)


def t_save_placement_drive(con, S, U, drive=None, **f):
    """Create a drive (as a Draft) or edit one. Validated, previewed with the
    eligible population its criteria give, written only with approval."""
    ensure(con)
    old = None
    if drive not in (None, ""):
        old = resolve(con, drive)
        if not old:
            return _err(f"No placement drive matches '{drive}'.")
        if old["status"] in ("Completed", "Cancelled", "Archived"):
            return _err(f"{old['company']} is {old['status'].lower()}; it can no longer be edited.")
        oc = criteria(old)
        base = {k: old.get(k) for k in SAVE_FIELDS}
        base.update({k: oc.get(k) for k in CRIT_FIELDS})
        f = {**base, **{k: v for k, v in f.items() if v is not None}}
    nd, why = _shape(con, f)
    if why:
        return _err(why)
    ok, excl = _preview(con, nd, old["id"] if old else 0)
    rowsx = _drive_rows({**nd, "criteria": json.dumps(nd["criteria"]), "status": old["status"] if old else "Draft",
                         "reg_closed": old.get("reg_closed") if old else 0, "id": old["id"] if old else 0})
    blocks = [B_table(["", ""], rowsx, title=("Edit: " if old else "New drive: ") + f"{nd['company']} · {nd['role']}"),
              B_cards([{"label": "Eligible on today's records", "value": len(ok), "tone": "good"}]
                      + [{"label": WHY_LABEL.get(k, k), "value": v} for k, v in sorted(excl.items())],
                      title="Who these criteria take in")]
    changed = []
    if old:
        cold = criteria(old)
        changed = [[k.replace("_", " "), str(old.get(k)), str(nd[k])] for k in SAVE_FIELDS
                   if str(old.get(k) or "") != str(nd[k] or "")]
        changed += [[k.replace("_", " "), str(cold.get(k)), str(nd["criteria"][k])] for k in CRIT_FIELDS
                    if cold.get(k) != nd["criteria"][k]]
        if not changed:
            return _err(f"Nothing would change: {old['company']} already reads like that.")
        blocks.append(B_table(["Field", "Now", "Becomes"], changed, title="What changes"))
        if old["status"] == "Published" and nd["drive_date"] != old["drive_date"]:
            blocks.append(B_text(f"The drive moves from {old['drive_date']} to {nd['drive_date']}: its calendar "
                                 f"entries move with it, and everyone who applied is told."))
    else:
        blocks.append(B_text("It is saved as a **draft**: no student sees it until it is published."))
    args = {"drive": old["id"] if old else None,
            **{k: nd[k] for k in SAVE_FIELDS}, **{k: nd["criteria"][k] for k in CRIT_FIELDS}}
    summary = f"{'update' if old else 'create'} the {nd['company']} drive ({nd['role']}, {nd['drive_date']})"
    tr = [(AGENT, "drive_plan", f"{summary} · {len(ok)} eligible on today's records")]
    if not approved_this_turn(U):
        return _propose(S, "save_placement_drive", args, blocks, summary, tr)
    now = NOW.isoformat(timespec="seconds")
    cols = {**{k: nd[k] for k in SAVE_FIELDS}, "criteria": json.dumps(nd["criteria"]),
            "min_cgpa": nd["criteria"]["min_cgpa"], "max_backlogs": nd["criteria"]["max_backlogs"],
            "depts": ",".join(nd["criteria"]["depts"])}
    if old:
        con.execute(f"UPDATE placement_drives SET {', '.join(k + '=?' for k in cols)}, updated_by=?, updated_at=? "
                    f"WHERE id=?", list(cols.values()) + [ACTOR, now, old["id"]])
        did = old["id"]
    else:
        did = con.execute(f"INSERT INTO placement_drives({','.join(cols)},status,reg_closed,created_by,created_at) "
                          f"VALUES({','.join('?' * len(cols))},'Draft',0,?,?)",
                          list(cols.values()) + [ACTOR, now]).lastrowid
    d = one(con, "SELECT * FROM placement_drives WHERE id=?", (did,))
    note = ""
    if d["status"] == "Published":
        try:
            _sync_calendar(con, d, ACTOR, now)
        except ValueError as e:
            con.rollback()
            return _err(str(e))
        if old and nd["drive_date"] != old["drive_date"]:
            who = [r["usn"] for r in rows(con, "SELECT usn FROM drive_registrations WHERE drive_id=? AND status IN "
                                               "('Applied','Shortlisted')", (did,))]
            if who:
                NotifyAgent().dispatch(con, [{"audience": f"Applicants · {d['company']} ({len(who)})",
                                              "channel": "App + email",
                                              "title": f"{d['company']} drive moved to {d['drive_date']}",
                                              "body": f"The {d['company']} ({d['role']}) drive is now on "
                                                      f"{d['drive_date']}."}])
                note = f" {len(who)} applicant(s) told of the new date."
    con.commit()
    back = one(con, "SELECT * FROM placement_drives WHERE id=?", (did,))
    good = bool(back) and all(str(back[k] or "") == str(nd[k] or "") for k in SAVE_FIELDS) \
        and json.loads(back["criteria"]) == nd["criteria"]
    if back and back["status"] == "Published":
        good = good and all(one(con, "SELECT date FROM calendar_events WHERE id=? AND status='Active'", (i,))
                            and one(con, "SELECT date FROM calendar_events WHERE id=?", (i,))["date"] == back["drive_date"]
                            for i in _cal_ids(back))
    Auditor().log(con, ACTOR, AGENT, "placement.drive.update" if old else "placement.drive.create",
                  {"id": did, "company": nd["company"], **({"changes": changed} if old else {})},
                  "Applied" if good else "Failed")
    _done(S, "save_placement_drive")
    return {"data": {"saved": good, "id": did, "status": back["status"] if back else None, "eligible": len(ok)},
            "blocks": blocks[:1] + [B_text(f"**{'Updated' if old else 'Saved as a draft'}** — drive #{did}, "
                                           f"{nd['company']}.{note} " + ("" if old else "Publish it to open it to "
                                                                                       "students."))],
            "refresh": True, "chips": [f"Publish the {nd['company']} drive"] if not old else [],
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           (AGENT, "verify", "drive re-read as approved" + (", calendar entries moved"
                                                                            if old else "") if good
                            else "drive does not read back as approved", "ok" if good else "error")]}


ACTIONS = {"publish": ("Draft",), "close_registration": ("Published",), "complete": ("Published",),
           "cancel": ("Draft", "Published"), "archive": ("Completed", "Cancelled")}
ACTION_WORDS = [("close_registration", r"close (?:the )?registration|stop (?:taking )?(?:applications|registrations)"),
                ("publish", r"\bpublish|\bopen (?:the )?drive"), ("complete", r"\bcomplete|\bheld\b|\bdone\b"),
                ("cancel", r"\bcancel|\bcall off"), ("archive", r"\barchive")]


def t_set_drive_status(con, S, U, drive="", action="", **kw):
    ensure(con)
    d = resolve(con, drive)
    if not d:
        return _err(f"No placement drive matches '{drive}'.")
    a = str(action or "").strip().lower().replace(" ", "_")
    a = next((k for k, rx in ACTION_WORDS if k == a or re.search(rx, a.replace("_", " "))), None)
    if not a:
        return _err("Which action? publish, close registration, complete, cancel or archive.")
    if d["status"] not in ACTIONS[a]:
        return _err(f"{d['company']} is {d['status'].lower()}: it cannot be {a.replace('_', ' ')}d from there"
                    + (f" (only from {' or '.join(s.lower() for s in ACTIONS[a])})." if ACTIONS[a] else "."))
    ok, _why = cohort_eligibility(con, d)
    apps = rows(con, "SELECT usn, status FROM drive_registrations WHERE drive_id=?", (d["id"],))
    live = [r for r in apps if r["status"] in OPEN_STATES]
    blocks = [B_table(["", ""], _drive_rows(d), title=f"{d['company']} · {d['role']}")]
    msgs, cal_plan = [], None
    if a == "publish":
        if TODAY.isoformat() > d["reg_end"]:
            return _err(f"Registration was to close on {d['reg_end']}, which has passed. Edit the dates first.")
        cal_plan, why = calendar_plan(con, d)
        if why:
            return _err(why)
        blocks.append(B_table(["Calendar entry", "Date", "Reaches"],
                              [[e["title"], e["date"] + (f" {e['start_time']}" if e.get("start_time") else ""),
                                f"{cal.audience(e)} ({cal.reach_count(con, e)} students)"] for e in cal_plan],
                              title="On the academic calendar"))
        msgs.append({"audience": f"Eligible students · {d['company']} ({len(ok)})", "channel": "App + email",
                     "title": f"New placement drive: {d['company']} — {d['role']} (₹{d['ctc']:g} LPA)",
                     "body": f"You meet the criteria for the {d['company']} drive on {d['drive_date']}. "
                             f"Registration closes {d['reg_end']}. Apply from Placement drives in your portal."})
        text = f"Publishing opens {d['company']} to students: {len(ok)} are eligible on today's records and are told."
    elif a == "close_registration":
        text = f"No new applications after this. {len(apps)} already applied."
    elif a == "complete":
        if TODAY.isoformat() < d["drive_date"]:
            return _err(f"The {d['company']} drive is on {d['drive_date']}; it can be completed once it is held.")
        text = f"Marks the drive held. {len(live)} application(s) are still undecided."
    elif a == "cancel":
        if live:
            msgs.append({"audience": f"Applicants · {d['company']} ({len(live)})", "channel": "App + email",
                         "title": f"{d['company']} drive cancelled",
                         "body": f"The {d['company']} ({d['role']}) drive on {d['drive_date']} is cancelled."})
        text = (f"Cancelling takes it off every calendar" + (f" and tells its {len(live)} applicant(s)." if live
                                                             else ".") + " The record is kept.")
    else:
        text = "Archiving hides it from students; the record and its applications are kept."
    blocks.append(B_text(text))
    args = {"drive": d["id"], "action": a}
    summary = f"{a.replace('_', ' ')} the {d['company']} drive"
    tr = [(AGENT, "status_plan", f"{summary}: {d['status']} → " + {"publish": "Published", "complete": "Completed",
                                                                   "cancel": "Cancelled", "archive": "Archived",
                                                                   "close_registration": "registration closed"}[a])]
    if not approved_this_turn(U):
        return _propose(S, "set_drive_status", args, blocks, summary, tr)
    now = NOW.isoformat(timespec="seconds")
    if a == "close_registration":
        con.execute("UPDATE placement_drives SET reg_closed=1, updated_by=?, updated_at=? WHERE id=?",
                    (ACTOR, now, d["id"]))
    else:
        new = {"publish": "Published", "complete": "Completed", "cancel": "Cancelled", "archive": "Archived"}[a]
        con.execute("UPDATE placement_drives SET status=?, updated_by=?, updated_at=? WHERE id=?",
                    (new, ACTOR, now, d["id"]))
    d2 = one(con, "SELECT * FROM placement_drives WHERE id=?", (d["id"],))
    if a == "publish":
        _sync_calendar(con, d2, ACTOR, now)
    if a == "cancel":
        for i in _cal_ids(d):
            con.execute("UPDATE calendar_events SET status='Cancelled', updated_by=?, updated_at=? WHERE id=?",
                        (ACTOR, now, i))
    con.commit()
    back = one(con, "SELECT * FROM placement_drives WHERE id=?", (d["id"],))
    good = bool(back) and (back["reg_closed"] == 1 if a == "close_registration" else back["status"] == new)
    if a == "publish":
        ids = _cal_ids(back)
        good = good and len(ids) == len(cal_plan) and all(
            one(con, "SELECT 1 FROM calendar_events WHERE id=? AND status='Active' AND date=?", (i, back["drive_date"]))
            for i in ids)
    if a == "cancel":
        good = good and not any(one(con, "SELECT 1 FROM calendar_events WHERE id=? AND status='Active'", (i,))
                                for i in _cal_ids(back))
    if msgs:
        NotifyAgent().dispatch(con, msgs)
    Auditor().log(con, ACTOR, AGENT, f"placement.drive.{a}", {"id": d["id"], "company": d["company"]},
                  "Applied" if good else "Failed")
    _done(S, "set_drive_status")
    return {"data": {"done": good, "drive": d["company"], "action": a, "status": back["status"],
                     "registration": registration(back)},
            "blocks": [B_text(f"**Done.** {d['company']}: {summary.split(' the ')[0]} — now "
                              f"{back['status'].lower()}, registration {registration(back).lower()}.")],
            "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           (AGENT, "verify", "status re-read" + (" and calendar entries checked"
                                                                 if a in ("publish", "cancel") else "")
                            if good else "does not read back as approved", "ok" if good else "error")]
                     + ([("NotifyAgent", "dispatch", msgs[0]["audience"])] if msgs else [])}


def t_apply_to_drive(con, S, U, drive="", **kw):
    """The signed-in student applies. Eligibility is computed again from the
    records at the moment of the write, never taken from the page."""
    from portal import _me, _staged                  # portal does not import this module at load
    ensure(con)
    P, e = _me(S, "student")
    if e:
        return e
    s = _stu(con, P["usn"])
    d = resolve(con, drive, student=True)
    if not d:
        return _err(f"No placement drive matches '{drive}'.")
    ev = evaluate(con, d, s)
    crit = B_table(["Criterion", "Required", "You", ""],
                   [[r["label"], r["required"], r["actual"], "Met" if r["ok"] else "Not met"] for r in ev["criteria"]],
                   title=f"{d['company']} · {d['role']}")
    tr = [(AGENT, "evaluate", f"{s['usn']} → {d['company']}: {ev['action']}")]
    if ev["application"]:
        return {"data": {"error": f"You have already applied to {d['company']}: {ev['application']}."},
                "blocks": [B_text(f"You have already applied to {d['company']}. Status: **{ev['application']}**.")],
                "trace": tr}
    if not ev["eligible"]:
        return {"data": {"error": f"You are not eligible for {d['company']}: " + "; ".join(reasons(ev)) + ".",
                         "criteria": ev["criteria"]},
                "blocks": [crit, B_text("You are not eligible: " + "; ".join(reasons(ev)) + ".")], "trace": tr}
    if ev["registration"] != "Open":
        return _err(f"The {d['company']} drive is not taking applications: {ev['registration'].lower()}.")
    sch = school(con, s["usn"]) or {}
    form = [["Company", d["company"]], ["Role", d["role"]], ["CTC", f"₹{d['ctc']:g} LPA"],
            ["Drive", f"{d['drive_date']}" + (f" {d['drive_time']}" if d.get("drive_time") else "")],
            ["Student", s["name"]], ["USN", s["usn"]], ["Class", _cls(s)], ["CGPA", f"{s['cgpa']:.2f}"],
            ["Backlogs", s["backlogs"]], ["10th", f"{sch['tenth']:g}%" if sch.get("tenth") is not None else "—"],
            ["12th" if sch.get("entry") != "Lateral" else "Diploma",
             f"{(sch.get('twelfth') if sch.get('entry') != 'Lateral' else sch.get('diploma')):g}%"
             if (sch.get("twelfth") if sch.get("entry") != "Lateral" else sch.get("diploma")) is not None else "—"],
            ["Documents", "your resume and college ID on the drive day; nothing is uploaded here"]]
    tbl = B_table(["", ""], form, title="Placement application — filled from your record")
    args = {"drive": d["id"]}
    if not approved_this_turn(U) or not _staged(S, "apply_to_drive", args):
        return _propose(S, "apply_to_drive", args, [crit, tbl], f"apply to the {d['company']} drive", tr)
    now = NOW.isoformat(timespec="seconds")
    snap = {r["key"]: r["actual"] for r in ev["criteria"]}
    con.execute("INSERT INTO drive_registrations(drive_id,usn,status,registered_at,submitted,updated_by,updated_at) "
                "VALUES(?,?,?,?,?,?,?)", (d["id"], s["usn"], "Applied", now, json.dumps(snap), s["usn"], now))
    con.commit()
    back = one(con, "SELECT * FROM drive_registrations WHERE drive_id=? AND usn=?", (d["id"], s["usn"]))
    good = bool(back and back["status"] == "Applied")
    NotifyAgent().dispatch(con, [{"audience": f"Student · {s['usn']}", "channel": "App",
                                  "title": f"Application submitted: {d['company']}",
                                  "body": f"Your application for {d['role']} at {d['company']} (drive {d['drive_date']}) "
                                          f"is with the placement cell."}])
    Auditor().log(con, s["usn"], AGENT, "placement.apply", {"drive": d["id"], "company": d["company"]},
                  "Applied" if good else "Failed")
    _done(S, "apply_to_drive")
    return {"data": {"applied": good, "drive": d["company"], "status": back["status"] if back else None,
                     "reference": f"APP-{d['id']:03d}-{s['usn']}"},
            "blocks": [tbl, B_text(f"**Applied.** Reference APP-{d['id']:03d}-{s['usn']}. It is with the placement "
                                   f"cell; you will be told when you are shortlisted.")],
            "refresh": True, "chips": ["Show my placement applications"],
            "trace": tr + [("PolicyGuard", "write_authorisation", "the student confirmed in this turn"),
                           (AGENT, "verify", "application re-read" if good else "application missing after write",
                            "ok" if good else "error")]}


def t_update_applications(con, S, U, drive="", usns="", status="", **kw):
    ensure(con)
    d = resolve(con, drive)
    if not d:
        return _err(f"No placement drive matches '{drive}'.")
    st = next((k for k, w in DECISIONS.items() if str(status).strip().lower() in (k.lower(), w, w + "ed", w + "d")),
              None)
    if not st:
        return _err("Which decision? Shortlisted, Not shortlisted, Selected or Rejected.")
    want = sorted({u.upper() for u in re.findall(r"\b4vp\d{2}[a-z]{2}\d{3}\b", " ".join(_list(usns)), re.I)})
    if not want:
        return _err("Whose applications? Give their USNs.")
    regs = {r["usn"]: r for r in rows(con, "SELECT * FROM drive_registrations WHERE drive_id=?", (d["id"],))}
    bad = [f"{u} did not apply" for u in want if u not in regs]
    bad += [f"{u} is already {regs[u]['status'].lower()}" for u in want if u in regs and regs[u]["status"]
            in FINAL_STATES]
    bad += [f"{u} is already {st.lower()}" for u in want if u in regs and regs[u]["status"] == st]
    if bad:
        return _err(f"Nothing proposed — {len(bad)} problem(s): " + "; ".join(bad[:8]), problems=bad)
    names = {r["usn"]: r for r in rows(con, f"SELECT usn, name, dept, sem, section, cgpa FROM students WHERE usn IN "
                                            f"({','.join('?' * len(want))})", tuple(want))}
    tbl = B_table(["USN", "Name", "Class", "CGPA", "Now", "Becomes"],
                  [[u, names[u]["name"], f"{names[u]['dept']}-{names[u]['sem']}{names[u]['section']}",
                    f"{names[u]['cgpa']:.2f}", regs[u]["status"], st] for u in want],
                  title=f"{d['company']} · {len(want)} application(s)")
    extra = [B_text(f"Selected students get an offer of ₹{d['ctc']:g} LPA on record, and the one-offer rule then "
                    f"holds them out of other non-dream drives.")] if st == "Selected" else []
    args = {"drive": d["id"], "usns": ", ".join(want), "status": st}
    summary = f"mark {len(want)} {d['company']} application(s) {st.lower()}"
    tr = [(AGENT, "decision_plan", summary)]
    if not approved_this_turn(U):
        return _propose(S, "update_applications", args, [tbl] + extra, summary, tr)
    now = NOW.isoformat(timespec="seconds")
    con.executemany("UPDATE drive_registrations SET status=?, updated_by=?, updated_at=? WHERE drive_id=? AND usn=?",
                    [(st, ACTOR, now, d["id"], u) for u in want])
    if st == "Selected":
        for u in want:
            if not one(con, "SELECT 1 FROM student_offers WHERE usn=? AND company=?", (u, d["company"])):
                con.execute("INSERT INTO student_offers(usn,company,ctc,offered_on) VALUES(?,?,?,?)",
                            (u, d["company"], d["ctc"], TODAY.isoformat()))
    con.commit()
    got = {r["usn"]: r["status"] for r in rows(con, "SELECT usn, status FROM drive_registrations WHERE drive_id=?",
                                                (d["id"],))}
    good = all(got.get(u) == st for u in want)
    if st == "Selected":
        good = good and all(one(con, "SELECT 1 FROM student_offers WHERE usn=? AND company=?", (u, d["company"]))
                            for u in want)
    NotifyAgent().dispatch(con, [{"audience": f"Students · {d['company']} {st.lower()} ({len(want)})",
                                  "channel": "App + email", "title": f"{d['company']}: {st}",
                                  "body": f"Your application for {d['role']} at {d['company']} is {st.lower()}."}])
    Auditor().log(con, ACTOR, AGENT, "placement.decide", {"drive": d["id"], "status": st, "usns": want},
                  "Applied" if good else "Partial")
    _done(S, "update_applications")
    return {"data": {"updated": sum(1 for u in want if got.get(u) == st), "of": len(want), "status": st},
            "blocks": [tbl, B_text(f"**Recorded.** {len(want)} application(s) {st.lower()} and the students told.")],
            "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           (AGENT, "verify", f"{sum(1 for u in want if got.get(u) == st)}/{len(want)} re-read"
                                             + (" with offers on record" if st == "Selected" else ""),
                            "ok" if good else "error")]}


# ================================================================ registry
def _p(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_S, _N, _B = {"type": "string"}, {"type": "number"}, {"type": "boolean"}
_DRIVE_PROPS = {"drive": {"type": "string", "description": "drive id or company, to edit; omit to create"},
                "company": _S, "role": _S, "ctc": _N, "drive_date": _S, "drive_time": _S, "reg_start": _S,
                "reg_end": _S, "interview_date": _S, "openings": {"type": "integer"}, "website": _S,
                "employment_type": _S, "work_mode": _S, "dream": _B, "description": _S, "industry": _S,
                "location": _S, "job_description": _S, "job_location": _S, "selection_process": _S, "venue": _S,
                "min_cgpa": _N, "max_backlogs": {"type": "integer"}, "min_tenth": _N, "min_twelfth": _N,
                "min_diploma": _N, "depts": _S, "sems": _S, "sections": _S, "batches": _S}
REGISTRY = [
    (t_placement_drives, "placement_drives",
     "Placement drives. With a student's usn: the drives they can apply for, those they applied to (with status), "
     "open drives they are not eligible for (with why) and closed ones. Without: every drive with its status, "
     "eligible count, applications, shortlisted and selected.", _p({"usn": _S, "status": _S})),
    (t_drive_details, "drive_details",
     "One drive (id or company): company, role, CTC, dates, process and its criteria. With a student's usn: every "
     "criterion with what is required, what they have and whether it is met (academic, department/semester/batch, "
     "the one-offer rule) and whether they can apply. Without: the eligible count and why the rest are out.",
     _p({"drive": _S, "usn": _S}, ["drive"])),
    (t_drive_applications, "drive_applications",
     "Applicants of a drive with their CGPA, backlogs and status; filters dept, sem, status, min_cgpa, "
     "max_backlogs.", _p({"drive": _S, "dept": _S, "sem": {"type": "integer"}, "status": _S, "min_cgpa": _N,
                          "max_backlogs": {"type": "integer"}}, ["drive"])),
    (t_save_placement_drive, "save_placement_drive",
     "Create a placement drive (saved as a Draft) or edit one (drive=id): company, job and drive details and its own "
     "eligibility criteria (min CGPA, backlogs allowed, 10th/12th/diploma %, departments, semesters, sections, "
     "graduating batch). Shows the eligible population. Without approval this turn it only PROPOSES.",
     _p(_DRIVE_PROPS)),
    (t_set_drive_status, "set_drive_status",
     "publish (puts it on the academic calendar and tells eligible students), close_registration, complete, cancel "
     "(takes it off the calendar, tells applicants) or archive a drive. Without approval this turn it only PROPOSES.",
     _p({"drive": _S, "action": _S}, ["drive", "action"])),
    (t_apply_to_drive, "apply_to_drive",
     "Student: apply to a drive. Eligibility is checked again from the records; the application is filled from "
     "their profile. PROPOSES until the student confirms.", _p({"drive": _S}, ["drive"])),
    (t_update_applications, "update_applications",
     "Mark applicants of a drive Shortlisted, Not shortlisted, Selected (records the offer) or Rejected. "
     "usns = their USNs. Without approval this turn it only PROPOSES.",
     _p({"drive": _S, "usns": _S, "status": _S}, ["drive", "usns", "status"])),
]
GATED_WRITES = ("save_placement_drive", "set_drive_status", "apply_to_drive", "update_applications")
AGENT_OF = {name: AGENT for _f, name, _d, _s in REGISTRY}
TOOL_GROUPS = {"placement": ["placement_overview", "placement_drives", "drive_details", "drive_applications",
                             "drive_eligibility", "save_placement_drive", "set_drive_status", "update_applications",
                             "publish_drive_shortlist"]}


# ================================================================= routing
STATUS_Q = re.compile(r"\b(?:have i applied|did i apply|my (?:placement )?applications?|application status|"
                      r"my applications|applied (?:to|for))\b")


def student_route(con, text, P):
    """A student's placement question -> (tool, args). access.py fills in the USN."""
    t = text.lower()
    d = resolve(con, text, student=True) if con is not None else None    # routed without a database: the list
    if d and re.search(r"\bapply\b|\bregister\b|\bsign me up\b", t) and not re.search(r"\b(?:have|did|can|could|am)\s+i\b", t):
        return "apply_to_drive", {"drive": d["id"]}
    if d:
        return "drive_details", {"drive": d["id"]}
    return "placement_drives", {}


def admin_route(con, text):
    """The Registrar's placement sentences that are about drives as records."""
    t = text.lower()
    d = resolve(con, text)
    m = re.search(r"\b(shortlist|select|reject)\b", t)
    usns = re.findall(r"\b4vp\d{2}[a-z]{2}\d{3}\b", t)
    if d and m and usns:
        return "update_applications", {"drive": d["id"], "usns": ", ".join(u.upper() for u in usns),
                                       "status": {"shortlist": "Shortlisted", "select": "Selected",
                                                  "reject": "Rejected"}[m.group(1)]}
    if d and re.search(r"\bapplica(?:nts?|tions?)\b|who (?:has |have )?applied", t):
        return "drive_applications", {"drive": d["id"]}
    for a, rx in ACTION_WORDS:
        if d and re.search(rx, t) and re.search(r"\bdrive\b|\bregistration\b", t):
            return "set_drive_status", {"drive": d["id"], "action": a}
    if re.search(r"\b(?:all|list|show)\b.*\bdrives\b|\bplacement drives\b", t) and not d:
        return "placement_drives", {}
    return None
