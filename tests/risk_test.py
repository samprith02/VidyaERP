"""The early-warning list (#79) and whole-word departments (#80): counted in
full, every signal recomputed, both engines the same answer, scoped by role,
and honest that it is not a prediction. No server, no API cost.

    python tests/risk_test.py
"""
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_risk_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import agents                                                      # noqa: E402
import auth                                                        # noqa: E402
import nlu                                                         # noqa: E402
import orchestrator as O                                           # noqa: E402
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


def call(args=None, user=None):
    S = {"pending": None, "history": [], "ctx": {}}
    if user:
        S["user"] = auth.principal(con, user)
    return tools.execute(con, S, "", "academic_risk", args or {})


W = {k: w for k, _l, w in agents.RISK_SIGNALS}


def score_of(s, subj):
    """The index, recomputed from the raw tables without StudentAgent."""
    sc = 0
    sc += W["att75"] if s["attendance"] < 75 else 0
    sc += W["att60"] if s["attendance"] < 60 else 0
    sc += W["subj"] if subj.get(s["usn"], 0) >= 2 else 0
    sc += W["backlog"] if s["backlogs"] > 0 else 0
    sc += W["cgpa"] if s["cgpa"] < 6.0 else 0
    sc += W["fees"] if s["fee_due"] > 50_000 else 0
    return sc


# ------------------------------------------------------------------ 1 · counted in full
print("\n1 · every student is counted; the list only limits what is shown")
print("-" * 78)
subj = {}
for r in rows(con, "SELECT usn, held, attended FROM attendance WHERE held>0"):
    if 100.0 * r["attended"] / r["held"] < 75:
        subj[r["usn"]] = subj.get(r["usn"], 0) + 1
studs = rows(con, "SELECT * FROM students")
indep = {s["usn"]: score_of(s, subj) for s in studs}
d = call()["data"]
check("1a the count is every student with a signal, recomputed from the raw tables",
      d["with_any_signal"] == sum(1 for v in indep.values() if v > 0), f"{d['with_any_signal']}")
# Defect (#79): the old radar ended in LIMIT 25 and reported len() of that.
check("1b ...and it is not capped at the 25 rows it shows", d["with_any_signal"] > 25 and len(d["students"]) == 25)
bands = {b: 0 for b, _f in agents.RISK_BANDS}
for v in indep.values():
    b = next((b for b, f in agents.RISK_BANDS if v >= f), None)
    if b:
        bands[b] += 1
check("1c High / Medium / Watch match the recomputed scores",
      (d["high"], d["medium"], d["watch"]) == (bands["High"], bands["Medium"], bands["Watch"]), f"{d} vs {bands}")
top = d["students"][0]
check("1d each listed student's score and signals are the recomputed ones",
      all(x["score"] == indep[x["usn"]] and len(x["signals"]) >= 1 for x in d["students"]), str(top))
check("1e the weights are declared in the answer, and it says it is not a prediction",
      d["weights"] == {l: w for _k, l, w in agents.RISK_SIGNALS} and "not a trained prediction" in d["note"])
# Policy, documented in the README: changing a weight is a deliberate act with a
# failing test attached, like the plan-ranking weights.
check("1f the weights and bands are the documented policy",
      W == {"att75": 3, "att60": 2, "subj": 1, "backlog": 2, "cgpa": 2, "fees": 1, "cie": 2}
      and agents.RISK_BANDS == (("High", 5), ("Medium", 3), ("Watch", 1)), str(W))

# ------------------------------------------------------------------ 2 · it moves
print("\n2 · the index moves with the record")
print("-" * 78)
calm = next(s for s in studs if indep[s["usn"]] == 0)
con.execute("UPDATE students SET attendance=55 WHERE usn=?", (calm["usn"],))
con.commit()
row = next((x for x in agents.StudentAgent().risk(con) if x["usn"] == calm["usn"]), None)
check("2a a student falling to 55% attendance appears, scored 3+2",
      row and row["score"] == W["att75"] + W["att60"] and row["band"] == "High", str(row and row["signals"]))
con.execute("UPDATE students SET attendance=? WHERE usn=?", (calm["attendance"], calm["usn"]))
con.commit()

# ------------------------------------------------------------------ 3 · one answer
print("\n3 · the rule engine and the tool give the same answer")
print("-" * 78)
out = O.handle(con, "Which students are at academic risk?", "r1", "admin", "registrar")
cards = next(b for b in out["blocks"] if b["type"] == "cards")
check("3a the rule engine reports the tool's counts, not the rows it shows",
      [c["value"] for c in cards["cards"][:3]] == [d["high"], d["medium"], d["watch"]], str(cards["cards"]))
out = O.handle(con, "Mentor-wise counselling report", "r2", "admin", "registrar")
titles = [b.get("title") for b in out["blocks"]]
# It used to route to the institution brief ("report"), and after that fix
# the "ISE" inside "mentor-wISE" scoped it to one department (#80).
check("3b 'Mentor-wise counselling report' is the counselling list, for the whole institution",
      "Mentor-wise counselling list" in titles and "Early warning — the institution" in titles, str(titles))
mt = next(b for b in out["blocks"] if b.get("title") == "Mentor-wise counselling list")
check("3c the mentor rows add up to everyone with a signal",
      sum(r[1] + r[2] + r[3] for r in mt["rows"]) == d["with_any_signal"])

# ------------------------------------------------------------------ 4 · who may
print("\n4 · a teacher sees their mentees, an HOD their department, a student nothing")
print("-" * 78)
fac = one(con, "SELECT mentor FROM students GROUP BY mentor ORDER BY COUNT(*) DESC LIMIT 1")["mentor"]
r = call({}, fac)
mine = {s["usn"] for s in rows(con, "SELECT usn FROM students WHERE mentor=?", (fac,))}
check("4a a teacher's list is exactly their mentees with a signal",
      r["data"]["with_any_signal"] == sum(1 for u in mine if indep[u] > 0)
      and all(x["mentor"] == fac for x in r["data"]["students"]), str(r["data"])[:120])
other = one(con, "SELECT id FROM faculty WHERE id<>? AND is_hod=0 LIMIT 1", (fac,))["id"]
check("4b ...and is refused another teacher's", "DENIED" in call({"mentor": other}, fac)["data"])
hod = one(con, "SELECT id, dept FROM faculty WHERE is_hod=1 AND dept='ECE'")
r = call({"dept": "CSE"}, hod["id"])
check("4c an HOD is refused another department", "DENIED" in r["data"])
r = call({}, hod["id"])
check("4d ...and gets their own", r["data"]["scope"] == "ECE" and r["data"]["with_any_signal"]
      == sum(1 for s in studs if s["dept"] == "ECE" and indep[s["usn"]] > 0))
check("4e a student is refused", "DENIED" in call({}, "4VP24CS009")["data"])

# ------------------------------------------------------------------ 5 · departments are words (#80)
print("\n5 · a department is a whole word")
print("-" * 78)
cases = {"Advise me on attendance defaulters": None, "Mentor-wise report": None, "otherwise": None,
         "civilian": None, "mechanism": None, "4VP24CS009 profile": None, "CSE5A timetable": "CSE",
         "Information Science": "ISE", "E&C timetable": "ECE", "the CS department": "CSE", "MECHANICAL": "MECH"}
got = {q: nlu.extract_dept(q) for q in cases}
# Defect: substring matching read "ISE" inside advise / otherwise / -wise.
check("5a whole words only, and the forms that should still match do", got == cases,
      str({q: g for q, g in got.items() if g != cases[q]}))
out = O.handle(con, "Advise me on attendance defaulters", "r5", "admin", "registrar")
check("5b 'Advise me on attendance defaulters' is the whole institution, not ISE",
      not any("ISE" in (b.get("title") or "") for b in out["blocks"]), json.dumps([b.get("title") for b in out["blocks"]]))

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
