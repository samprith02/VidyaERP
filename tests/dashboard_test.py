"""The student dashboard (#96, #97): attendance and internal marks each come
from their own record and are recomputed here independently; missing marks
say so; the attention list appears only when something needs doing; the
quick-access buttons are read-only questions. No server, no API cost
(section 7 runs the renderer under Node when Node is installed).

    python tests/dashboard_test.py
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_dashboard_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import academics                                                   # noqa: E402
import access                                                      # noqa: E402
import auth                                                        # noqa: E402
import campus_data as cd                                           # noqa: E402
import guard                                                       # noqa: E402
import nlu                                                         # noqa: E402
import portal                                                      # noqa: E402
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
auth.ensure(con)
academics.ensure_cie(con)
TODAY = nlu.TODAY


def home(usn):
    S = {"pending": None, "history": [], "ctx": {}, "user": auth.principal(con, usn)}
    return tools.execute(con, S, "", "my_home", {})


def block(r, kind, title=None):
    return next((b for b in r["blocks"] if b["type"] == kind and (title is None or b.get("title", "").startswith(title))),
                None)


def marks_by_hand(usn, code, kind):
    """CIE so far, straight from cie_marks and the scheme - not via academics.so_far."""
    got = of = 0.0
    for comp, mx, w, _d in academics.CIE_SCHEME[kind]:
        r = one(con, "SELECT marks FROM cie_marks WHERE usn=? AND subject=? AND component=?", (usn, code, comp))
        if r:
            got += w * float(r["marks"] or 0)
            of += w * mx
    return (round(got, 1), round(of, 1)) if of else None


# ============================================ 1 · two measures, two sources (#96)
print("\n1 · attendance and internal marks, each recomputed from its own table")
print("-" * 78)
STU = "4VP24CS009"
r = home(STU)
perf = block(r, "perf")
check("1a the dashboard has a subject-performance block, one row per subject",
      perf and len(perf["items"]) == len({x["subject"] for x in rows(con, "SELECT subject FROM attendance WHERE usn=?",
                                                                       (STU,))}), str(perf and len(perf["items"])))
bad_att, bad_mk = [], []
for usn in [x["usn"] for x in rows(con, "SELECT usn FROM students WHERE dept='CSE' AND sem=5 ORDER BY usn")]:
    s = one(con, "SELECT * FROM students WHERE usn=?", (usn,))
    kinds = {x["code"]: x["kind"] for x in rows(con, "SELECT code, kind FROM subjects")}
    for p in portal.subject_performance(con, s):
        a = one(con, "SELECT attended, held FROM attendance WHERE usn=? AND subject=?", (usn, p["code"]))
        if a and (p["attendance"].get("attended"), p["attendance"].get("held")) != (a["attended"], a["held"]):
            bad_att.append((usn, p["code"]))
        if "got" in p["marks"] and (p["marks"]["got"], p["marks"]["of"]) != marks_by_hand(usn, p["code"], kinds[p["code"]]):
            bad_mk.append((usn, p["code"], p["marks"]))
check("1b every attendance figure is that subject's attended/held, for every CSE sem-5 student", not bad_att,
      str(bad_att[:3]))
check("1c every marks figure is the CIE scored so far over what was assessed, recomputed by hand", not bad_mk,
      str(bad_mk[:3]))
p0 = perf["items"][0]
check("1d the two are never the same number by construction (the first subject differs)",
      p0["attendance"]["pct"] != p0["marks"].get("pct"), json.dumps(p0)[:160])
cards = {c["label"]: c for c in block(r, "cards")["cards"]}
marked = [p["marks"] for p in perf["items"] if "got" in p["marks"]]
avg = round(100 * sum(m["got"] for m in marked) / sum(m["of"] for m in marked), 1)
check("1e the internal-marks card is the weighted average of what has been assessed",
      cards["Internal marks"]["value"] == f"{avg:g}%", f"{cards['Internal marks']['value']} vs {avg}")
first = one(con, "SELECT MIN(date) d FROM exams WHERE dept='CSE' AND sem=5 AND date>=?", (TODAY.isoformat(),))["d"]
check("1f the next-exam card is the earliest SEE paper from today on",
      cards["Next exam"]["value"] == dt.date.fromisoformat(first).strftime("%d %b"), cards["Next exam"]["value"])

# =============================================================== 2 · gaps say so
print("\n2 · a missing figure is shown as missing, never invented")
print("-" * 78)
c = academics._courses(con, "CSE", 5, "A")[0]
con.execute("DELETE FROM cie_marks WHERE usn=? AND subject=?", (STU, c["subject"]))
con.execute("INSERT INTO attendance(usn, subject, attended, held) VALUES(?, 'ACT-PT', 10, 12)", (STU,))
con.commit()
items = {p["code"]: p for p in portal.subject_performance(con, one(con, "SELECT * FROM students WHERE usn=?", (STU,)))}
check("2a a course with no marks entered says 'Not entered yet' and carries no percentage",
      items[c["subject"]]["marks"] == {"none": "Not entered yet"}, str(items[c["subject"]]["marks"]))
check("2b ...while its attendance is still shown", "pct" in items[c["subject"]]["attendance"])
check("2c a subject with no CIE at all says so", items["ACT-PT"]["marks"] == {"none": "No internal marks for this course"},
      str(items["ACT-PT"]["marks"]))
db.seed(force=True)
academics.ensure_cie(con)

# ============================================================ 3 · the arithmetic
print("\n3 · classes to reach 75%, and where today's periods stand")
print("-" * 78)
wrong = [(a, h) for h in range(1, 60) for a in range(0, h + 1)
         if portal.classes_to_reach(a, h) != max(0, 3 * h - 4 * a)]
check("3a 'attend the next N classes' is the smallest N reaching 75%, for every attended/held up to 60",
      not wrong, str(wrong[:5]))
check("3b 26 of 36 needs 4 more in a row (30 of 40)", portal.classes_to_reach(26, 36) == 4)
at = lambda h, m: dt.datetime.combine(TODAY, dt.time(h, m))     # noqa: E731
ps = [1, 2, 3, 4, 5, 6]
check("3c at 09:30, P1 is now, P2 next, the rest later",
      portal.class_status(ps, at(9, 30)) == ["Now", "Next", "Later", "Later", "Later", "Later"])
check("3d at 12:10, three are done, P4 is now and P5 next",
      portal.class_status(ps, at(12, 10)) == ["Done", "Done", "Done", "Now", "Next", "Later"])
check("3e in the lunch break nothing is 'now' and the afternoon's first is next",
      portal.class_status(ps, at(13, 10)) == ["Done"] * 4 + ["Next", "Later"])
check("3f after the last period everything is done", portal.class_status(ps, at(17, 0)) == ["Done"] * 6)
today = block(r, "table", "Today")
check("3g the table uses the demo clock and says which time it is read at",
      today["title"].endswith("as of 09:30") and [x[-1] for x in today["rows"]][:2] == ["Now", "Next"],
      today["title"])

# ======================================================= 4 · needs your attention
print("\n4 · the attention list: only what needs doing, each item measured")
print("-" * 78)
check("4a a student with nothing to act on gets no attention list", block(r, "checklist") is None
      and r["data"]["needs_attention"] == 0)
LOW = "4VP23CS001"
r2 = home(LOW)
todo = block(r2, "checklist")
lows = rows(con, """SELECT a.subject, a.attended, a.held FROM attendance a WHERE a.usn=?
                    AND 100.0*a.attended/a.held < 75""", (LOW,))
labels = " | ".join(i["label"] + " :: " + i["detail"] for i in (todo or {"items": []})["items"])
check("4b each subject below 75% is listed with the classes needed to reach it",
      lows and all(f"{x['subject']} " in labels and f"next {max(0, 3 * x['held'] - 4 * x['attended'])} class" in labels
                   for x in lows), labels[:300])
loan = one(con, "SELECT l.*, b.title FROM book_loans l JOIN books b ON b.id=l.book_id WHERE l.member=? "
                "AND l.returned_on IS NULL AND l.due_on<?", (LOW, TODAY.isoformat()))
days = (TODAY - dt.date.fromisoformat(loan["due_on"])).days
check("4c an overdue book is listed with its days late and the fine (days x the daily fine)",
      f"overdue by {days} day(s)" in labels and f"₹{days * cd.FINE_PER_DAY:,}" in labels.replace(",", ","), labels[-200:])
FEE = one(con, "SELECT usn, fee_due FROM students WHERE fee_due>0 ORDER BY usn")
r3 = home(FEE["usn"])
check("4d fee due is listed when there is any",
      any(i["label"].startswith("Fee due") for i in block(r3, "checklist")["items"]))
risky = academics.cie_risk(con, "CSE", 7)
ru = risky[0]["usn"] if risky else None
r4 = home(ru) if ru else None
check("4e a course at risk of the CIE minimum is listed with what it still needs",
      r4 and any("internal marks" in i["label"] for i in block(r4, "checklist")["items"]), str(ru))

# ======================================================= 5 · coming up, library
print("\n5 · what is coming, and books on loan")
print("-" * 78)
up = block(r, "table", "Coming up")
dates = [dt.datetime.strptime(x[0] + " 2026", "%a %d %b %Y").date() for x in up["rows"]]
check("5a everything coming up is from today on, soonest first", all(d >= TODAY for d in dates) and dates == sorted(dates),
      str(dates))
whats = [x[2] for x in up["rows"]]
check("5b one CIE assessment held on one day is one line, naming its courses",
      sum(1 for w in whats if w.startswith("Assignment (AAT)")) == 1
      and all(cc["subject"] in next(w for w in whats if w.startswith("Assignment"))
              for cc in academics._courses(con, "CSE", 5, "A") if cc["kind"] == "T"), str(whats))
lib = block(r, "table", "Library books")
due = one(con, "SELECT due_on FROM book_loans WHERE member=? AND returned_on IS NULL", (STU,))["due_on"]
left = (dt.date.fromisoformat(due) - TODAY).days
# Defect: a book due in ten days was shown as "Late by 0 d".
check("5c a book not yet due shows the days left, not 'late by 0 d'",
      lib["rows"][0][-1] == f"{left} day(s) left" and left > 0, str(lib["rows"]))
lib2 = block(r2, "table", "Library books")
check("5d an overdue one shows the days and the fine", any(x[-1].startswith(f"Overdue by {days} day(s) · fine")
                                                           for x in lib2["rows"]), str(lib2["rows"]))

# ============================================================== 6 · quick access
print("\n6 · quick-access buttons are read-only questions")
print("-" * 78)
quick = block(r, "quick")
asks = [i["ask"] for i in quick["items"]]
check("6a no button says an approval word (a click is the student's own turn)",
      not [a for a in asks if guard.APPROVAL_RX.search(a)], str(asks))
P = auth.principal(con, STU)
routed = {a: portal.route(con, a, P, {"usn": None, "dept": None, "sem": None, "section": None, "periods": [],
                                      "date": None, "faculty": None})[0] for a in asks}
check("6b each routes to a tool a student may call, and none is a write",
      all(t in access.POLICY["student"] and t not in tools.GATED_WRITES for t in routed.values()), str(routed))
check("6c ...and each to the tool its words name",
      routed == {"My attendance": "student_360", "My internal marks": "cie_marks", "Exam schedule": "exam_schedule",
                 "My library books": "library_loans", "My no-dues status": "no_dues_status",
                 "My requests": "my_requests"}, str(routed))
check("6d the composer's chips start as the same questions", r["chips"] == asks)

# ================================================== 7 · the renderer (#96 layout)
print("\n7 · the page draws attendance and marks as two labelled meters")
print("-" * 78)
node = shutil.which("node")
if not node:
    print("  skip 7a-7d: node is not installed, so the renderer cannot be executed here")
else:
    BLOCKS = open(os.path.join(HERE, "static", "blocks.js"), encoding="utf-8").read()
    sample = dict(perf)
    sample["items"] = perf["items"][:2] + [{"code": "X<b>1", "name": "<script>x</script>",
                                            "attendance": {"none": "Not recorded"},
                                            "marks": {"none": "Not entered yet"}}]
    prog = BLOCKS + "\nconst B=" + json.dumps(sample) + ";\nconsole.log(JSON.stringify(rBlock(B)));"
    run = subprocess.run([node, "-e", prog], capture_output=True, text=True, encoding="utf-8")
    try:
        html = json.loads(run.stdout)
    except ValueError:                      # a renderer that throws fails the checks below by name
        html = ""
        print("  renderer error:", (run.stderr or "")[:300])
    rowsh = html.split('<div class="prow">')[1:]
    check("7a every subject row has an Attendance meter and an Internal marks meter, in that order",
          len(rowsh) == 3 and all(-1 < x.find('class="pm att') < x.find('class="pm mk') for x in rowsh), html[:200])
    a0 = perf["items"][0]
    att_h, _s, mk_h = (rowsh[0] if rowsh else "").partition('class="pm mk')
    check("7b the attendance meter carries hours, the marks meter carries marks",
          f"{a0['attendance']['attended']}/{a0['attendance']['held']} hrs" in att_h and "so far" not in att_h
          and f"{a0['marks']['got']:g}/{a0['marks']['of']:g} so far" in mk_h and "hrs" not in mk_h, rowsh[0][:400])
    check("7c a missing figure is drawn as its words, with no bar", "Not entered yet" in rowsh[2]
          and '<span class="pt"' not in rowsh[2])
    check("7d a subject name is escaped", "<script>" not in html and "&lt;script&gt;" in html)

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
