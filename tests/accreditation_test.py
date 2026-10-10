"""Accreditation evidence (#29): every figure is computed from the records and
moves with them, every gap is named, and the pack writes nothing. No server,
no API cost.

    python tests/accreditation_test.py
"""
import hashlib
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_accreditation_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import accreditation as AC                                         # noqa: E402
import auth                                                        # noqa: E402
import nlu                                                         # noqa: E402
import tools                                                       # noqa: E402
from agents import one, rows                                       # noqa: E402

FAILED, PASSED = [], 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


con = db.connect()


def pack(fw="NAAC", **kw):
    return tools.execute(con, {"pending": None, "history": [], "ctx": {}}, "", "accreditation_evidence",
                         {"framework": fw, **kw})


def metric(name, fw_data=None):
    d = fw_data or pack()["data"]
    return next((x for x in d["metrics"] if x["metric"].startswith(name)), None)


def db_hash():
    h = hashlib.sha256()
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        for r in con.execute(f"SELECT * FROM {t}"):
            h.update(json.dumps(list(r), default=str).encode())
    return h.hexdigest()


# ------------------------------------------------------------------ 1 · shape
print("\n1 · every figure has a source; every gap has a reason")
print("-" * 78)
before = db_hash()
d = pack()["data"]
check("1a the NAAC pack evidences metrics and names gaps", len(d["metrics"]) >= 20 and len(d["gaps"]) >= 10,
      f"{len(d['metrics'])} metrics, {len(d['gaps'])} gaps")
check("1b every metric carries a value and the tables it came from",
      all(str(x["value"]).strip() and x["source"].strip() for x in d["metrics"]))
check("1c every gap says why the records cannot evidence it", all(g["why"].strip() for g in d["gaps"]))
check("1d criteria 1-6 have computed metrics; 7 is honestly all gaps",
      {x["criterion"] for x in d["metrics"]} == {1, 2, 3, 4, 5, 6}
      and any(g["criterion"] == 7 for g in d["gaps"]))
# Pass percentage cannot be computed - there are no results - and must never be
# filled with a plausible number.
check("1e results are a gap, not a figure", any("Pass percentage" in g["metric"] for g in d["gaps"])
      and not any("pass" in x["metric"].lower() for x in d["metrics"]))

# ------------------------------------------------------------------ 2 · recomputed
print("\n2 · the figures, recomputed independently")
print("-" * 78)
S_ = one(con, "SELECT COUNT(*) n FROM students")["n"]
F_ = one(con, "SELECT COUNT(*) n FROM faculty")["n"]
check("2a student : teacher ratio = students ÷ faculty",
      metric("Student : full-time teacher ratio")["value"] == f"{S_ / F_:.1f} : 1", metric("Student")["value"])
dr = one(con, "SELECT COUNT(*) n FROM faculty WHERE name LIKE 'Dr.%'")["n"]
check("2b doctorates counted from the register", metric("Full-time teachers with a doctorate")["value"]
      .startswith(f"{dr} of {F_}"))
fin = one(con, "SELECT COUNT(*) n FROM students WHERE sem=7")["n"]
placed = one(con, """SELECT COUNT(DISTINCT o.usn) n FROM student_offers o JOIN students s ON s.usn=o.usn
                     WHERE s.sem=7""")["n"]
check("2c outgoing students placed", metric("Outgoing students placed")["value"].startswith(f"{placed} of {fin}"))
intake = one(con, "SELECT SUM(intake) n FROM departments")["n"]
sem3 = one(con, "SELECT COUNT(*) n FROM students WHERE sem=3")["n"]
check("2d enrolment against intake", metric("Enrolment against sanctioned intake")["value"]
      .startswith(f"{sem3} of {intake}"))
lab_rows = rows(con, "SELECT t.*, r.capacity FROM timetable t JOIN rooms r ON r.id=t.room WHERE t.kind='L'")
group = lambda r: one(con, "SELECT COUNT(*) n FROM students WHERE dept=? AND sem=? AND section=?"
                           + (" AND batch=?" if r["batch"] else ""),
                      (r["dept"], r["sem"], r["section"], *([r["batch"]] if r["batch"] else [])))["n"]
over = sum(1 for r in lab_rows if group(r) > r["capacity"])
whole = sum(1 for r in lab_rows if group({**r, "batch": None}) > r["capacity"])
check("2e lab periods over capacity, counted by the group in the room: 0 now that labs run in batches (#20), "
      "where counting the whole section would not be",
      metric("Lab periods where the group outnumbers")["value"] == f"{over} of {len(lab_rows)}" and over == 0
      and whole > 0, f"{over} by group, {whole} by section")

# ------------------------------------------------------------------ 3 · they move
print("\n3 · a measurement that never varies is a constant wearing a function's clothes")
print("-" * 78)
s0 = metric("Student : full-time teacher ratio")["value"]
con.execute("INSERT INTO faculty(id,name,dept,designation,max_load,joined) "
            "VALUES('F999','Dr. Probe Teacher','CSE','Professor',16,'2020-07-01')")
con.commit()
s1 = metric("Student : full-time teacher ratio")["value"]
check("3a one more teacher moves the ratio", s1 == f"{S_ / (F_ + 1):.1f} : 1" and s1 != s0, f"{s0} -> {s1}")
check("3b ...and the doctorate count", metric("Full-time teachers with a doctorate")["value"].startswith(f"{dr + 1} of"))
con.execute("DELETE FROM faculty WHERE id='F999'")
unplaced = one(con, """SELECT usn FROM students s WHERE sem=7 AND NOT EXISTS
                       (SELECT 1 FROM student_offers o WHERE o.usn=s.usn) LIMIT 1""")["usn"]
con.execute("INSERT INTO student_offers(usn,company,ctc,offered_on) VALUES(?, 'Probe Ltd', 6.0, '2026-09-04')",
            (unplaced,))
con.commit()
check("3c one more offer moves placement", metric("Outgoing students placed")["value"].startswith(f"{placed + 1} of"))
con.execute("DELETE FROM student_offers WHERE company='Probe Ltd'")
con.commit()
if AC._has(con, "cie_marks"):
    # the CIE register (#32) is part of this build: its table exists from the seed
    d2 = pack()["data"]
    check("3d the CIE register exists here, so it is a metric...",
          metric("Continuous internal evaluation", d2) is not None)
    check("3e ...and not also listed as a gap",
          not any("Continuous internal evaluation" in g["metric"] for g in d2["gaps"]))
else:
    check("3d the CIE register is a gap while it does not exist...",
          any("Continuous internal evaluation" in g["metric"] for g in pack()["data"]["gaps"]))
    con.execute("CREATE TABLE cie_marks(usn TEXT, subject TEXT, component TEXT, marks REAL)")
    con.execute("INSERT INTO cie_marks VALUES('x','y','IA1',1)")
    con.commit()
    d2 = pack()["data"]
    check("3e ...and a metric once it does (#32)", metric("Continuous internal evaluation", d2) is not None
          and not any("Continuous internal evaluation" in g["metric"] for g in d2["gaps"]))
    con.execute("DROP TABLE cie_marks")
    con.commit()

# ------------------------------------------------------------------ 4 · NBA
print("\n4 · NBA, by programme")
print("-" * 78)
before = db_hash()
nb = pack("NBA")["data"]
progs = nb["programmes"]
check("4a one row per programme, adding up to the institution",
      len(progs) == 5 and sum(p["students"] for p in progs) == S_ and sum(p["faculty"] for p in progs) == F_)
check("4b the 20:1 flag is exactly the ratio against the norm",
      all(p["sfr_ok"] == (p["sfr"] <= AC.SFR_NORM) for p in progs)
      and sorted(nb["above_sfr_norm"]) == sorted(p["dept"] for p in progs if p["sfr"] > AC.SFR_NORM), str(progs))
check("4c each cadre adds up to the department's faculty", all(sum(p["cadre"]) == p["faculty"] for p in progs))
one_ = pack("NBA", dept="CSE")["data"]["programmes"]
check("4d a department filter returns that programme alone", [p["dept"] for p in one_] == ["CSE"])
check("4e an unknown department is an error, not an empty pack", "error" in pack("NBA", dept="XYZ")["data"])
check("4f the pack writes nothing, NAAC or NBA", db_hash() == before)

# ------------------------------------------------------------------ 5 · who, and how it is asked
print("\n5 · the Registrar's; routed by whole words")
print("-" * 78)
for role, uid in (("student", "4VP24CS009"), ("faculty", "F002"),
                  ("hod", one(con, "SELECT id FROM faculty WHERE is_hod=1 LIMIT 1")["id"])):
    r = tools.execute(con, {"pending": None, "history": [], "ctx": {}, "user": auth.principal(con, uid)}, "",
                      "accreditation_evidence", {})
    check(f"5a a {role} is refused (default deny)", "DENIED" in r["data"], str(r["data"])[:100])
top = lambda q: (nlu.classify(q) or [("none",)])[0][0]                 # noqa: E731
check("5b 'NAAC evidence pack' and 'NBA evidence for CSE' reach the IQACAgent",
      top("NAAC evidence pack") == "accreditation" and top("NBA evidence for CSE") == "accreditation")
check("5c 'the workload is unbalanced' does not (nba inside a word)", top("the workload is unbalanced") != "accreditation")
check("5d the rule route picks NBA and the department from the sentence",
      AC.rule_route(con, "accreditation", "NBA evidence for CSE", {"dept": "CSE"})
      == ("accreditation_evidence", {"framework": "NBA", "dept": "CSE"}))
import app as A                                                     # noqa: E402
check("5e it is a read-only console panel", "accreditation_evidence" in A.PANELS
      and "accreditation_evidence" not in tools.GATED_WRITES)

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
