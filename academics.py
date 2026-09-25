"""
VidyaERP :: academic standing data, as agents (#19)

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
from agents import (rows, one, fac_name, B_text, B_table, Auditor, ensure_availability)
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
    """Create the table and seed it once. Idempotent; additive to an old DB."""
    own = con is None
    con = con or db.connect()
    ensure_availability(con)
    if not con.execute("SELECT 1 FROM faculty_availability LIMIT 1").fetchone():
        _seed(con, random.Random(1919))
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
]
GATED_WRITES = ("set_faculty_availability",)
AGENT_OF = {"faculty_availability": "TimetableAgent", "set_faculty_availability": "TimetableAgent"}
TOOL_GROUPS = {"availability": ["faculty_availability", "set_faculty_availability", "faculty_timetable",
                                "plan_timetable_generation"]}
INTENTS = {"availability"}

WRITE_RX = re.compile(r"\b(?:not available|unavailable|can(?:no|')t teach|cannot teach|block(?:ed)? out|"
                      r"available again|no longer (?:busy|unavailable)|remove|lift|clear)\b")


def rule_route(con, intent, text, ent):
    """utterance -> (tool, args) for the rule engine; execution goes through
    tools.execute like every other caller."""
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
