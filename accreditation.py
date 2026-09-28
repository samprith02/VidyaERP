"""
VidyaERP :: accreditation evidence, as an agent (#29)

Every cycle an Indian college's IQAC (Internal Quality Assurance Cell) builds
its NAAC and NBA evidence by hand, from a dozen registers. The IQACAgent
computes what the live records can support and names, in the same answer,
what they cannot.

Two rules hold this module down (tests/accreditation_test.py):
  * every figure is computed from the tables, and says which one - nothing
    here is typed in, estimated or carried over from a previous year;
  * a metric the data cannot evidence is listed as a GAP with the reason,
    never filled with a plausible number.

Metric wording follows the NAAC criteria and the NBA self-assessment headings.
Their numbering differs between manuals and cycles, so the pack groups by
criterion and names each metric rather than quoting a number it could get
wrong. The norms printed beside a figure (AICTE's 1:20 and 1:2:6) are stated
as norms, not findings. Reads only: nothing here writes.
"""
import datetime as dt
import re
import statistics
import nlu
from agents import rows, one, B_text, B_table, B_cards

AGENT = "IQACAgent"
SFR_NORM = 20                      # AICTE: one full-time teacher per 20 students
CADRE_NORM = (1, 2, 6)             # AICTE: Professor : Associate : Assistant
CADRES = ("Professor", "Associate Professor", "Assistant Professor")
CRITERIA = {1: "Curricular aspects", 2: "Teaching-learning and evaluation", 3: "Research, innovations and extension",
            4: "Infrastructure and learning resources", 5: "Student support and progression",
            6: "Governance, leadership and management", 7: "Institutional values and best practices"}


def _pct(a, b):
    return round(100 * a / b, 1) if b else 0.0


def _has(con, table):
    return bool(one(con, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def _n(con, q, a=()):
    return one(con, q, a)["n"] or 0


# ================================================================ NAAC
def naac(con):
    """-> (metrics, gaps). metric: {criterion, metric, value, norm, source}."""
    M, G = [], []

    def m(c, metric, value, source, norm=None):
        M.append({"criterion": c, "metric": metric, "value": value, "norm": norm, "source": source})

    def gap(c, metric, why):
        G.append({"criterion": c, "metric": metric, "why": why})

    today = nlu.TODAY
    students = _n(con, "SELECT COUNT(*) n FROM students")
    fac = rows(con, "SELECT * FROM faculty")

    # ---------------------------------------------------------- 1 · curricular
    offered = rows(con, """SELECT s.kind, COUNT(DISTINCT s.code) n FROM subjects s
                           WHERE EXISTS (SELECT 1 FROM timetable t WHERE t.subject=s.code) GROUP BY s.kind""")
    k = {r["kind"]: r["n"] for r in offered}
    m(1, "Courses on this semester's timetable (theory · lab · project)",
      f"{k.get('T', 0)} · {k.get('L', 0)} · {k.get('P', 0)}", "subjects ⋈ timetable")
    m(1, "Share of courses with a laboratory component", f"{_pct(k.get('L', 0), k.get('T', 0) + k.get('L', 0))}%",
      "subjects ⋈ timetable")
    gap(1, "Syllabus revision, value-added and certificate courses", "no curriculum or course-approval register")
    gap(1, "Structured feedback on the curriculum", "no feedback records")

    # ------------------------------------------------ 2 · teaching-learning
    cohort = rows(con, """SELECT d.code, d.intake, COUNT(s.usn) n FROM departments d
                          LEFT JOIN students s ON s.dept=d.code AND s.sem=3 GROUP BY d.code""")
    intake, admitted = sum(r["intake"] for r in cohort), sum(r["n"] for r in cohort)
    m(2, "Enrolment against sanctioned intake (current second-year cohort)",
      f"{admitted} of {intake} ({_pct(admitted, intake)}%)", "departments.intake, students (sem 3)")
    m(2, "Student : full-time teacher ratio", f"{students / len(fac):.1f} : 1" if fac else "—",
      "students, faculty", f"{SFR_NORM} : 1 or lower (AICTE)")
    dr = sum(1 for f in fac if f["name"].startswith("Dr."))
    m(2, "Full-time teachers with a doctorate", f"{dr} of {len(fac)} ({_pct(dr, len(fac))}%)",
      "faculty register (title)")
    exp = [(today - dt.date.fromisoformat(f["joined"])).days / 365.25 for f in fac if f["joined"]]
    m(2, "Average service at this institution", f"{statistics.mean(exp):.1f} years" if exp else "—",
      "faculty.joined")
    cad = {c: sum(1 for f in fac if f["designation"] == c) for c in CADRES}
    m(2, "Cadre: Professor : Associate : Assistant", " : ".join(str(cad[c]) for c in CADRES), "faculty.designation",
      "1 : 2 : 6 (AICTE)")
    mentors = _n(con, "SELECT COUNT(DISTINCT mentor) n FROM students WHERE mentor IS NOT NULL")
    m(2, "Students per mentor", f"{students / mentors:.1f}" if mentors else "—", "students.mentor")
    att = one(con, "SELECT AVG(attendance) a, SUM(CASE WHEN attendance<75 THEN 1 ELSE 0 END) short FROM students")
    m(2, "Mean attendance · students below 75%", f"{att['a']:.1f}% · {att['short']} ({_pct(att['short'], students)}%)",
      "students.attendance")
    load = rows(con, """SELECT f.id, f.max_load, COUNT(t.id) n FROM faculty f LEFT JOIN timetable t
                        ON t.faculty=f.id AND t.kind!='A' GROUP BY f.id""")
    util = [100 * r["n"] / r["max_load"] for r in load if r["max_load"]]
    m(2, "Teaching load against sanctioned load (mean)", f"{statistics.mean(util):.0f}%" if util else "—",
      "timetable, faculty.max_load")
    if _has(con, "cie_marks"):
        # continuous evaluation, when the marks register exists (#32)
        n = _n(con, "SELECT COUNT(*) n FROM cie_marks")
        m(2, "Continuous internal evaluation marks on record", f"{n:,}", "cie_marks")
    else:
        gap(2, "Continuous internal evaluation", "no internal-marks register in this database")
    gap(2, "Pass percentage and results", "SEE results are not modelled yet (#66)")
    gap(2, "Student satisfaction survey", "no survey records")

    # --------------------------------------------------------- 3 · research
    od = rows(con, """SELECT COUNT(*) n, COUNT(DISTINCT faculty) f FROM leaves
                      WHERE kind LIKE 'On Duty%' AND status='Approved'""")[0]
    m(3, "Approved on-duty leave for conferences and workshops", f"{od['n']} (by {od['f']} teachers)",
      "leaves (On Duty)")
    if _has(con, "faculty_availability"):
        rs = _n(con, """SELECT COUNT(DISTINCT faculty) n FROM faculty_availability WHERE status='Active'
                        AND (reason LIKE '%research%' OR reason LIKE '%consultancy%' OR reason LIKE '%PhD%')""")
        m(3, "Teachers with standing research or consultancy time", str(rs), "faculty_availability (reason)")
    gap(3, "Publications, citations and patents", "no research register")
    gap(3, "Research grants and consultancy revenue", "no grants or accounts ledger for research")
    gap(3, "Extension and outreach activities", "no activity register")

    # --------------------------------------------------- 4 · infrastructure
    rk = {r["kind"]: r for r in rows(con, "SELECT kind, COUNT(*) n, SUM(capacity) cap FROM rooms GROUP BY kind")}
    m(4, "Classrooms · laboratories (seats)",
      " · ".join(f"{rk[x]['n']} ({rk[x]['cap']})" for x in ("Classroom", "Lab") if x in rk), "rooms")
    slots = _n(con, "SELECT COUNT(DISTINCT day || '-' || period) n FROM timetable")
    booked = _n(con, "SELECT COUNT(*) n FROM timetable WHERE room IS NOT NULL")
    nrooms = sum(r["n"] for r in rk.values())
    m(4, "Room utilisation across the teaching week", f"{_pct(booked, nrooms * slots)}%",
      f"timetable ({slots} weekly slots) ÷ rooms")
    over = _n(con, """SELECT COUNT(*) n FROM timetable t JOIN rooms r ON r.id=t.room JOIN
                      (SELECT dept, sem, section, COUNT(*) size FROM students GROUP BY dept, sem, section) c
                      ON c.dept=t.dept AND c.sem=t.sem AND c.section=t.section
                      WHERE t.kind='L' AND c.size > r.capacity""")
    labs = _n(con, "SELECT COUNT(*) n FROM timetable WHERE kind='L'")
    m(4, "Lab periods where the section outnumbers the lab's seats", f"{over} of {labs}",
      "timetable ⋈ rooms ⋈ students", "0 (batches not modelled, #20)")
    if _has(con, "books"):
        b = one(con, "SELECT COUNT(*) titles, SUM(copies) vols FROM books")
        m(4, "Library titles · volumes · volumes per student",
          f"{b['titles']} · {b['vols']:,} · {b['vols'] / students:.2f}", "books")
        since = (today - dt.timedelta(days=30)).isoformat()
        m(4, "Loans issued in the last 30 days", str(_n(con, "SELECT COUNT(*) n FROM book_loans WHERE issued_on>=?",
                                                         (since,))), "book_loans")
    gap(4, "ICT-enabled classrooms, bandwidth, e-resources", "no infrastructure asset register")
    gap(4, "Maintenance expenditure", "no accounts ledger for maintenance")

    # ------------------------------------------------ 5 · student support
    final = _n(con, "SELECT COUNT(*) n FROM students WHERE sem=7")
    if _has(con, "student_offers"):
        placed = _n(con, "SELECT COUNT(DISTINCT o.usn) n FROM student_offers o JOIN students s ON s.usn=o.usn "
                         "WHERE s.sem=7")
        ctc = [r["best"] for r in rows(con, """SELECT MAX(o.ctc) best FROM student_offers o JOIN students s
                                               ON s.usn=o.usn WHERE s.sem=7 GROUP BY o.usn""")]
        m(5, "Outgoing students placed", f"{placed} of {final} ({_pct(placed, final)}%)", "student_offers, students")
        if ctc:
            m(5, "Median best offer (placed students)", f"₹{statistics.median(ctc):g} LPA", "student_offers")
        m(5, "Recruiters with an offer this year",
          str(_n(con, "SELECT COUNT(DISTINCT company) n FROM student_offers")), "student_offers")
    res = one(con, """SELECT SUM(CASE WHEN category IN ('SC','ST') THEN 1 ELSE 0 END) scst,
                      SUM(CASE WHEN category<>'GM' THEN 1 ELSE 0 END) rsv FROM students""")
    m(5, "Students from reserved categories (SC/ST · all reserved)",
      f"{_pct(res['scst'], students)}% · {_pct(res['rsv'], students)}%", "students.category")
    if _has(con, "hostel_rooms"):
        beds = _n(con, "SELECT SUM(capacity) n FROM hostel_rooms")
        occ = _n(con, "SELECT COUNT(*) n FROM hostel_allocations")
        m(5, "Hostel beds · occupied", f"{beds} · {occ} ({_pct(occ, beds)}%)", "hostel_rooms, hostel_allocations")
        hc = one(con, "SELECT COUNT(*) n, SUM(CASE WHEN status='Resolved' THEN 1 ELSE 0 END) r FROM hostel_complaints")
        m(5, "Hostel grievances resolved", f"{hc['r'] or 0} of {hc['n']}", "hostel_complaints")
    if _has(con, "transport_riders"):
        m(5, "Students on college transport", str(_n(con, "SELECT COUNT(*) n FROM transport_riders")),
          "transport_riders")
    gap(5, "Scholarships and freeships awarded", "no scholarship register")
    gap(5, "Progression to higher education and competitive-exam qualifiers", "no alumni or progression records")
    gap(5, "Sports and cultural awards", "no awards register")

    # ------------------------------------------------------- 6 · governance
    areas = [("Administration (approvals)", "requests"), ("Examination", "exams"), ("Faculty leave (HR)", "leaves"),
             ("Timetabling and substitution", "overrides"), ("Library", "book_loans"), ("Hostel", "hostel_allocations"),
             ("Transport", "transport_riders"), ("Placement", "placement_drives"), ("Certificates", "certificates"),
             ("Internal marks", "cie_marks")]
    live = [(a, _n(con, f"SELECT COUNT(*) n FROM {t}")) for a, t in areas if _has(con, t)]
    m(6, "Areas of operation run on this system (records)", "; ".join(f"{a} ({n:,})" for a, n in live),
      "row counts per table")
    m(6, "Decisions and agent actions in the audit ledger", f"{_n(con, 'SELECT COUNT(*) n FROM audit'):,}", "audit")
    pend = rows(con, "SELECT created_at, sla_hrs FROM requests WHERE status='Pending'")
    late = sum(1 for r in pend if (today - dt.date.fromisoformat(r["created_at"])).days * 24 > r["sla_hrs"])
    m(6, "Pending requests past their service level", f"{late} of {len(pend)}", "requests")
    gap(6, "Budget and its utilisation", "no finance ledger beyond fee dues")
    gap(6, "Faculty development programmes, IQAC meetings", "no FDP or meeting register")

    # --------------------------------------------------------- 7 · values
    gap(7, "Gender equity, environment and green practices", "no records of these initiatives")
    gap(7, "Best practices and institutional distinctiveness", "narrative, written by the IQAC")
    return M, G


# ================================================================= NBA
def nba(con, dept=None):
    """Per programme (department): the NBA figures the records can support."""
    out = []
    for d in rows(con, "SELECT * FROM departments ORDER BY code"):
        if dept and d["code"] != dept:
            continue
        st = _n(con, "SELECT COUNT(*) n FROM students WHERE dept=?", (d["code"],))
        fac = rows(con, "SELECT * FROM faculty WHERE dept=?", (d["code"],))
        cad = [sum(1 for f in fac if f["designation"] == c) for c in CADRES]
        sem3 = _n(con, "SELECT COUNT(*) n FROM students WHERE dept=? AND sem=3", (d["code"],))
        fin = _n(con, "SELECT COUNT(*) n FROM students WHERE dept=? AND sem=7", (d["code"],))
        placed = (_n(con, """SELECT COUNT(DISTINCT o.usn) n FROM student_offers o JOIN students s ON s.usn=o.usn
                             WHERE s.dept=? AND s.sem=7""", (d["code"],)) if _has(con, "student_offers") else None)
        att = one(con, "SELECT AVG(attendance) a, SUM(CASE WHEN attendance<75 THEN 1 ELSE 0 END) s FROM students "
                       "WHERE dept=?", (d["code"],))
        sfr = st / len(fac) if fac else None
        out.append({"dept": d["code"], "programme": d["name"], "students": st, "faculty": len(fac),
                    "sfr": round(sfr, 1) if sfr else None, "sfr_ok": bool(sfr and sfr <= SFR_NORM),
                    "cadre": cad, "doctorates": sum(1 for f in fac if f["name"].startswith("Dr.")),
                    "enrolment": _pct(sem3, d["intake"]), "intake": d["intake"], "admitted": sem3,
                    "placed_pct": _pct(placed, fin) if placed is not None else None,
                    "attendance": round(att["a"] or 0, 1), "below_75": att["s"] or 0})
    return out


NBA_GAPS = [("Student success rate and academic performance", "SEE results are not modelled yet (#66)"),
            ("Course-outcome and programme-outcome attainment", "no CO-PO mapping or attainment records"),
            ("Faculty publications and sponsored research", "no research register"),
            ("Professional-society chapters and student activities", "no activity register")]


# ================================================================ the tool
def t_accreditation_evidence(con, S, U, framework="NAAC", dept=None, **kw):
    fw = "NBA" if re.search(r"nba", str(framework or ""), re.I) else "NAAC"
    dept = str(dept).upper() if dept else None
    if dept and not one(con, "SELECT 1 FROM departments WHERE code=?", (dept,)):
        return {"data": {"error": f"No department '{dept}'."}, "blocks": [B_text(f"No department '{dept}'.")],
                "trace": []}
    if fw == "NBA":
        progs = nba(con, dept)
        blocks = [B_table(["Programme", "Students", "Faculty", "Student : faculty", "Prof : Assoc : Asst",
                           "Doctorates", "Enrolment (sem 3 vs intake)", "Placed (sem 7)", "Attendance · <75%"],
                          [[p["dept"], p["students"], p["faculty"],
                            f"{p['sfr']:g} : 1" + ("" if p["sfr_ok"] else f" (above {SFR_NORM} : 1)"),
                            " : ".join(str(x) for x in p["cadre"]), f"{p['doctorates']} of {p['faculty']}",
                            f"{p['admitted']} of {p['intake']} ({p['enrolment']:g}%)",
                            "—" if p["placed_pct"] is None else f"{p['placed_pct']:g}%",
                            f"{p['attendance']:g}% · {p['below_75']}"] for p in progs],
                          title=f"NBA evidence by programme — as of {nlu.TODAY.strftime('%d %b %Y')}", dense=True),
                  B_table(["Not evidenced", "Why"], [list(g) for g in NBA_GAPS], title="Gaps", dense=True),
                  B_text(f"Norms quoted (AICTE): student : faculty {SFR_NORM} : 1 or lower; cadre 1 : 2 : 6. Every figure is computed "
                         f"from the live records; the gaps are what those records cannot show.")]
        over = [p["dept"] for p in progs if not p["sfr_ok"]]
        return {"data": {"framework": "NBA", "programmes": progs, "gaps": [g[0] for g in NBA_GAPS],
                         "above_sfr_norm": over},
                "blocks": blocks, "chips": ["NAAC evidence pack", "Faculty workload", "Placement overview"],
                "trace": [(AGENT, "nba_compile", f"{len(progs)} programme(s) · {len(over)} above {SFR_NORM} : 1")]}
    M, G = naac(con)
    blocks = [B_cards([{"label": "Metrics evidenced", "value": len(M), "tone": "good"},
                       {"label": "Gaps named", "value": len(G), "tone": "warn" if G else "good",
                        "sub": "what the records cannot show"},
                       {"label": "Criteria covered", "value": f"{len({x['criterion'] for x in M})} of 7"}],
                      title=f"NAAC evidence pack — computed from the live records, {nlu.TODAY.strftime('%d %b %Y')}")]
    for c, name in CRITERIA.items():
        mc = [x for x in M if x["criterion"] == c]
        if mc:
            blocks.append(B_table(["Metric", "Value", "Norm", "Source"],
                                  [[x["metric"], x["value"], x["norm"] or "", x["source"]] for x in mc],
                                  title=f"Criterion {c} · {name}", dense=True))
    blocks.append(B_table(["Criterion", "Not evidenced", "Why"],
                          [[f"{g['criterion']} · {CRITERIA[g['criterion']]}", g["metric"], g["why"]] for g in G],
                          title=f"Gaps ({len(G)})", dense=True))
    blocks.append(B_text("Grouped by NAAC criterion and named rather than numbered: metric numbering differs "
                         "between manuals and cycles. Norms are AICTE's, quoted for reference."))
    return {"data": {"framework": "NAAC", "metrics": M, "gaps": G},
            "blocks": blocks, "chips": ["NBA evidence by programme", "Placement overview", "Library status"],
            "trace": [(AGENT, "naac_compile", f"{len(M)} metrics from the records · {len(G)} gaps named")]}


# ============================================================ registry
REGISTRY = [
    (t_accreditation_evidence, "accreditation_evidence",
     "Accreditation evidence computed from the live records: framework='NAAC' (institution, criterion-wise "
     "metrics with their source tables, and the gaps the records cannot evidence) or 'NBA' (per programme: "
     "student-faculty ratio, cadre, doctorates, enrolment, placement, attendance). Reads only.",
     {"type": "object", "properties": {"framework": {"type": "string"}, "dept": {"type": "string"}},
      "required": []}),
]
GATED_WRITES = ()
AGENT_OF = {"accreditation_evidence": AGENT}
TOOL_GROUPS = {"accreditation": ["accreditation_evidence", "institution_overview", "faculty_workload",
                                 "placement_overview"]}
INTENTS = {"accreditation"}


def rule_route(con, intent, text, ent):
    fw = "NBA" if re.search(r"\bnba\b|programme|program-wise|per programme", text, re.I) else "NAAC"
    return "accreditation_evidence", {"framework": fw, "dept": ent.get("dept")}
