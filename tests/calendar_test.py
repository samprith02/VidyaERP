"""The academic calendar (#104): every entry reaches exactly the classes it
names, students only read, the Registrar's writes are proposed, validated,
approved and re-read, and the agent answers a student from their own calendar.
No server, no API cost.

    python tests/calendar_test.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import json

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_calendar_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import academic_calendar as ac                                     # noqa: E402
import access                                                      # noqa: E402
import auth                                                        # noqa: E402
import nlu                                                         # noqa: E402
import orchestrator                                                # noqa: E402
import portal                                                      # noqa: E402
import solver                                                      # noqa: E402
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
TERM = {"from_date": "2026-07-01", "to_date": "2026-12-31"}


def S(user=None):
    return {"pending": None, "history": [], "ctx": {}, "user": user}


def call(name, args, user=None, U=""):
    return tools.execute(con, S(user), U, name, args)


def student(dept, sem, section):
    r = one(con, "SELECT usn FROM students WHERE dept=? AND sem=? AND section=? ORDER BY usn LIMIT 1",
            (dept, sem, section))
    return auth.principal(con, r["usn"])


def cal(user, **a):
    return call("calendar_events", {**TERM, **a}, user)


def ids(r):
    return {str(e["id"]) for e in ((r.get("data") or {}).get("events") or [])}


def n_rows():
    return one(con, "SELECT COUNT(*) c FROM calendar_events")["c"]


def add(**a):
    """The Registrar adds an entry: proposed, then approved in their own words."""
    s = S()
    r = tools.execute(con, s, "add it to the calendar", "save_calendar_event", a)
    if "BLOCKED" not in (r.get("data") or {}):            # written without approval: 2-0 names it
        return str((r.get("data") or {}).get("id"))
    r = tools.execute(con, s, "yes, go ahead", "save_calendar_event", s["pending"]["args"])
    assert (r.get("data") or {}).get("saved"), r.get("data")
    return str(r["data"]["id"])


def refused(a):
    """(error, rows written): the error a save is refused with, even approved."""
    before = n_rows()
    r = call("save_calendar_event", a, U="yes, go ahead")
    return (r.get("data") or {}).get("error"), n_rows() - before


CSE7A, CSE7B, CSE5A, CSE5B = student("CSE", 7, "A"), student("CSE", 7, "B"), student("CSE", 5, "A"), \
    student("CSE", 5, "B")
ISE7A, ISE5A, CSE3A, ISE3A = student("ISE", 7, "A"), student("ISE", 5, "A"), student("CSE", 3, "A"), \
    student("ISE", 3, "A")
F_CSE = auth.principal(con, "F002")

# ====================================================================== 1 · data
print("\n1 · the calendar's data")
print("-" * 78)
seeded = rows(con, "SELECT * FROM calendar_events")
check("1a the calendar is seeded", len(seeded) >= 10, str(len(seeded)))
hol = {(r["date"][5:7], r["date"][8:]) for r in seeded if r["kind"] == "holiday" and r["scope"] == "college"}
check("1b every fixed holiday in db.HOLIDAYS is a college-wide holiday on it (#106)",
      {(f"{m:02d}", f"{d:02d}") for m, d in db.HOLIDAYS} <= hol, str(hol))
bad = [(r["id"], why) for r in seeded for _ev, why in [ac.resolve(con, r, self_id=r["id"])] if why]
check("1c every seeded entry passes the validation an admin's entry must pass", not bad, str(bad))
check("1d no SEE paper falls on a fixed holiday (#106)",
      not [r for r in rows(con, "SELECT date FROM exams") if db.holiday(ac.parse_date(r["date"]))])
check("1e db.verify() is clean on a fresh seed", not db.verify(), str(db.verify()[:3]))
check("1f db.SCHEMA drops calendar_events, so a reset cannot keep stale entries (#64)",
      "DROP TABLE IF EXISTS calendar_events" in db.SCHEMA)
kinds = {e["kind"] for e in ac.entries(con)}
check("1g the SEE timetable and the CIE scheme are on the calendar, read-only",
      {"see", "cie", "lab"} <= kinds and all(not e["editable"] for e in ac.entries(con) if e["source"] != "calendar"))
check("1h the calendar's SEE entries are the exams table, one for one",
      sorted(e["date"] for e in ac.entries(con) if e["source"] == "see")
      == sorted(r["date"] for r in rows(con, "SELECT date FROM exams")))

# =============================================================== 2 · visibility
print("\n2 · each entry reaches exactly the classes it names")
print("-" * 78)
before = n_rows()
s0 = S()
r0 = tools.execute(con, s0, "put it on the calendar", "save_calendar_event",
                   {"title": "Gate probe", "kind": "event", "date": "2026-09-16", "scope": "college"})
check("2-0 the Registrar's entry is only PROPOSED until they approve it in their own words",
      "BLOCKED" in r0["data"] and n_rows() == before and (s0["pending"] or {}).get("tool") == "save_calendar_event",
      str(r0["data"])[:120])
x_cse7a = add(title="Internal exam: Cryptography", kind="cie", date="2026-09-29", start_time="09:30",
              end_time="10:30", dept="CSE", sem=7, section="A", subject="BCS703")
x_cse5 = add(title="CSE 5th semester internal exam", kind="cie", date="2026-09-23", start_time="09:30",
             end_time="10:30", dept="CSE", sem=5, subject="BCS502")
x_sem7 = add(title="Final-year project expo", kind="event", date="2026-09-24", sem=7)
x_cse = add(title="CSE alumni meet", kind="event", date="2026-09-26", dept="CSE")
x_all = add(title="Founders' Day lecture", kind="event", date="2026-09-17", scope="college")


def sees(p, x):
    return x in ids(cal(p))


check("2a a CSE 7th-sem section A exam is visible to a CSE-7A student", sees(CSE7A, x_cse7a))
check("2b ...and NOT to section B of the same class", not sees(CSE7B, x_cse7a))
check("2c ...NOT to a CSE 5th-sem student", not sees(CSE5A, x_cse7a) and not sees(CSE5B, x_cse7a))
check("2d ...NOT to an ISE 7th-sem student", not sees(ISE7A, x_cse7a))
check("2e a CSE 5th-sem exam reaches both CSE-5 sections and no other class",
      sees(CSE5A, x_cse5) and sees(CSE5B, x_cse5) and not any(sees(p, x_cse5) for p in (CSE7A, ISE5A, CSE3A)))
check("2f a semester-7 entry for every department reaches ISE-7A and CSE-7B, not CSE-5A",
      sees(ISE7A, x_sem7) and sees(CSE7B, x_sem7) and not sees(CSE5A, x_sem7))
check("2g a CSE department entry reaches every CSE semester and no ISE class",
      all(sees(p, x_cse) for p in (CSE3A, CSE5A, CSE7B)) and not any(sees(p, x_cse) for p in (ISE3A, ISE7A)))
classes = solver.classes(con)
reach = [c for c in classes if sees(student(*c), x_all)]
check("2h a college-wide entry reaches a student of every class on the rolls", len(reach) == len(classes),
      f"{len(reach)}/{len(classes)}")


def expect(e, d, s, x):
    """The rule restated independently of ac.reaches: every part the entry
    names is the student's, and a semester entry is this academic year's."""
    return (e["dept"] is None or e["dept"] == d) and (e["sem"] is None or e["sem"] == s) and \
        (e["section"] is None or e["section"] == x) and (e["sem"] is None or e["academic_year"] == "2026-27")


every = ac.entries(con, TERM["from_date"], TERM["to_date"])
diff = []
for d, s, x in classes:
    got = ids(cal(student(d, s, x)))
    want = {str(e["id"]) for e in every if expect(e, d, s, x)}
    if got != want:
        diff.append((f"{d}-{s}{x}", sorted(got - want)[:3], sorted(want - got)[:3]))
check(f"2i for all {len(classes)} classes, a student's calendar is exactly the entries that name them", not diff,
      str(diff[:3]))
mine = cal(CSE5A, kind="see")["data"]["events"]
check("2j a CSE-5A student's SEE papers are exactly CSE semester 5's",
      len(mine) == one(con, "SELECT COUNT(*) c FROM exams WHERE dept='CSE' AND sem=5")["c"]
      and all((e["dept"], e["sem"]) == ("CSE", 5) for e in mine), str(len(mine)))
# A semester-7 entry from another academic year: "semester 7" then is today's
# 5th-semester class, so the students in semester 7 now must not see it.
con.execute("""INSERT INTO calendar_events(title,kind,date,scope,dept,sem,academic_year,status)
               VALUES('Next year''s sem 7 exam','exam','2027-09-20','semester','CSE',7,'2027-28','Active')""")
con.commit()
x_ny = str(one(con, "SELECT MAX(id) i FROM calendar_events")["i"])
check("2k an entry for semester 7 of another academic year is not on a 7th-sem student's calendar",
      x_ny not in ids(call("calendar_events", {"from_date": "2027-09-01", "to_date": "2027-09-30"}, CSE7A)))
check("2l ...while the Registrar still sees it, so nothing is hidden from them",
      x_ny in ids(call("calendar_events", {"from_date": "2027-09-01", "to_date": "2027-09-30"})))
con.execute("DELETE FROM calendar_events WHERE id=?", (x_ny,))
con.commit()
fac = ids(cal(F_CSE))
check("2m a CSE teacher sees the CSE entries and the college-wide ones",
      {x_cse7a, x_cse5, x_cse, x_all, x_sem7} <= fac)
isedept = add(title="ISE department talk", kind="event", date="2026-09-16", dept="ISE")
check("2n ...and not an ISE-only one", isedept not in fac and isedept not in ids(cal(F_CSE)))

# ============================================================== 3 · permissions
print("\n3 · students read; only the Registrar writes")
print("-" * 78)
check("3a a student can read their calendar", cal(CSE7A)["data"]["count"] > 0)
before = n_rows()
r1 = call("save_calendar_event", {"title": "x", "kind": "event", "date": "2026-09-30", "scope": "college"},
          CSE7A, U="yes, go ahead")
check("3b a student cannot create an entry, even saying yes", "DENIED" in r1["data"] and n_rows() == before,
      str(r1["data"]))
r2 = call("save_calendar_event", {"event_id": x_cse7a, "date": "2026-09-30"}, CSE7A, U="yes, go ahead")
moved = one(con, "SELECT date FROM calendar_events WHERE id=?", (x_cse7a,))["date"]
check("3c a student cannot change one", "DENIED" in r2["data"] and moved == "2026-09-29", str(r2["data"]))
r3 = call("cancel_calendar_event", {"event_id": x_cse7a}, CSE7A, U="yes, go ahead")
st = one(con, "SELECT status FROM calendar_events WHERE id=?", (x_cse7a,))["status"]
check("3d a student cannot cancel one", "DENIED" in r3["data"] and st == "Active", str(r3["data"]))
check("3e no student, teacher or HOD is allowed either calendar write",
      not any(w in access.POLICY[role] for role in access.POLICY for w in ac.GATED_WRITES))
for k, v in (("dept", "ISE"), ("sem", 5), ("section", "B"), ("usn", CSE5A["usn"])):
    r = call("calendar_events", {k: v}, CSE7A)
    check(f"3f a student asking for another class's calendar by {k} is refused, not answered", "DENIED" in r["data"],
          str(r["data"])[:100])
r = call("calendar_events", {"dept": "CSE", "sem": 7, "section": "A"}, CSE7A)
check("3g ...while naming their own class is fine", x_cse7a in ids(r) and r["data"]["count"] > 0)
r = call("calendar_events", {"event_id": x_cse5}, CSE7A)
check("3h a student cannot open another class's entry by its number", "error" in r["data"], str(r["data"]))
r = call("calendar_events", {"dept": "ISE"}, F_CSE)
check("3i a teacher asking for another department's calendar is refused", "DENIED" in r["data"], str(r["data"]))
check("3j the HTTP gate: a student reaches /api/me/calendar",
      auth.gate("/api/me/calendar", CSE7A) is None)
check("3k ...but not the Registrar's propose endpoint or the console panel",
      auth.gate("/api/calendar/propose", CSE7A) == (403, "the Registrar's console only")
      and auth.gate("/api/panel/calendar_events", CSE7A) is not None)

import app                                                         # noqa: E402  (seeds nothing: the DB exists)


class Req:
    def __init__(self, user):
        self.state = type("st", (), {"user": user})()


got = app.my_calendar(Req(CSE5A), month="2026-09", event="")
check("3l /api/me/calendar answers with the signed-in student's own calendar",
      got["data"]["for"].startswith(CSE5A["name"]) and x_cse7a not in ids(got) and x_cse5 in ids(got))
reg = auth.principal(con, "registrar")
before = n_rows()
out = app.calendar_propose(Req(reg), {"action": "save", "session": "t3m", "fields": {
    "title": "Smuggled", "kind": "event", "date": "2026-09-30", "scope": "college", "U": "yes", "approved": True}})
check("3m the console form only proposes: an approval slipped into its fields is dropped, nothing written",
      "BLOCKED" in (out["data"] or {}) and n_rows() == before, str(out["data"])[:120])
sess = orchestrator.sess(app._sid(reg, "t3m"))
check("3n ...and the proposal waits in the Registrar's own chat session for their yes",
      (sess.get("pending") or {}).get("tool") == "save_calendar_event")
res = orchestrator.handle(con, "Yes, go ahead", app._sid(reg, "t3m"), "admin", actor="registrar")
vr = [t for t in res["trace"] if t["agent"] == "CalendarAgent" and t["action"] == "verify"]
check("3o their yes in the chat commits it, and the re-read is reported", bool(vr) and vr[0]["status"] == "ok"
      and one(con, "SELECT 1 FROM calendar_events WHERE title='Smuggled'") is not None, str(vr))
try:
    app.calendar_propose(Req(reg), {"action": "delete", "fields": {"event_id": "1"}})
    bad_action = False
except app.BadInput:
    bad_action = True
check("3p an unknown action is a 400, not a write", bad_action)
# change and cancel, by the Registrar
s = S()
r = tools.execute(con, s, "move it", "save_calendar_event", {"event_id": x_cse, "date": "2026-09-25"})
chg = [b for b in r["blocks"] if b.get("title") == "What changes"]
check("3q a change is proposed with what changes, and nothing written yet",
      "BLOCKED" in r["data"] and chg and chg[0]["rows"] == [["date", "2026-09-26", "2026-09-25"]]
      and one(con, "SELECT date FROM calendar_events WHERE id=?", (x_cse,))["date"] == "2026-09-26",
      str(chg[0]["rows"] if chg else r["data"]))
r = tools.execute(con, s, "yes, go ahead", "save_calendar_event", s["pending"]["args"])
check("3r ...and made on approval, keeping everything it did not change",
      r["data"].get("saved") and one(con, "SELECT date, dept, title FROM calendar_events WHERE id=?", (x_cse,))
      == {"date": "2026-09-25", "dept": "CSE", "title": "CSE alumni meet"})
s = S()
tools.execute(con, s, "cancel it", "cancel_calendar_event", {"event_id": x_cse5})
check("3s a cancellation is only proposed until approved", sees(CSE5A, x_cse5))
r = tools.execute(con, s, "yes, go ahead", "cancel_calendar_event", s["pending"]["args"])
check("3t ...then it leaves every calendar, and the record is kept as cancelled",
      r["data"].get("cancelled") and not sees(CSE5A, x_cse5)
      and one(con, "SELECT status FROM calendar_events WHERE id=?", (x_cse5,))["status"] == "Cancelled")
for ref in ("see-1", "cie-IA1-CSE-5"):
    r = call("cancel_calendar_event", {"event_id": ref}, U="yes, go ahead")
    check(f"3u {ref} is changed in its own timetable, not on the calendar", "error" in r["data"], str(r["data"]))

# ================================================================ 4 · validation
print("\n4 · nothing incorrect is saved, nothing is assumed")
print("-" * 78)
CASES = [
    ("4a an exam with no audience is not assumed college-wide",
     {"title": "IA", "kind": "cie", "date": "2026-09-16"}, "Who is this for"),
    ("4b an event with no audience is not assumed college-wide either",
     {"title": "Talk", "kind": "event", "date": "2026-09-16"}, "Who is this for"),
    ("4c college-wide and a named class at once",
     {"title": "x", "kind": "event", "date": "2026-09-16", "scope": "college", "dept": "CSE"}, "college-wide entry"),
    ("4d a scope that disagrees with the audience given",
     {"title": "x", "kind": "event", "date": "2026-09-16", "scope": "department", "dept": "CSE", "sem": 7},
     "scope says"),
    ("4e a section with no class", {"title": "x", "kind": "event", "date": "2026-09-16", "section": "A"},
     "belongs to one class"),
    ("4f a semester not on the rolls", {"title": "x", "kind": "event", "date": "2026-09-16", "dept": "CSE",
                                        "sem": 9}, "semester 9"),
    ("4g a section that does not run", {"title": "x", "kind": "event", "date": "2026-09-16", "dept": "ISE",
                                        "sem": 7, "section": "B"}, "no ISE-7B"),
    ("4h a department that does not exist", {"title": "x", "kind": "event", "date": "2026-09-16", "dept": "EEE"},
     "no EEE"),
    ("4i a 5th-semester subject on a 7th-semester exam",
     {"kind": "cie", "date": "2026-09-16", "dept": "CSE", "sem": 7, "subject": "BCS501"}, "semester 5 course"),
    ("4j an unknown subject code", {"kind": "cie", "date": "2026-09-16", "dept": "CSE", "sem": 7, "subject": "BXX999"},
     "No course"),
    ("4k a subject's exam made college-wide",
     {"kind": "cie", "date": "2026-09-16", "scope": "college", "subject": "BCS501"}, "not college-wide"),
    ("4l an end time before the start", {"title": "x", "kind": "event", "date": "2026-09-16", "scope": "college",
                                          "start_time": "11:00", "end_time": "10:00"}, "cannot end at"),
    ("4m an end time with no start", {"title": "x", "kind": "event", "date": "2026-09-16", "scope": "college",
                                       "end_time": "10:00"}, "start time"),
    ("4n an end date before the start date", {"title": "x", "kind": "event", "date": "2026-09-16",
                                               "end_date": "2026-09-14", "scope": "college"}, "before it starts"),
    ("4o an impossible date", {"title": "x", "kind": "event", "date": "2026-09-31", "scope": "college"},
     "not a date"),
    ("4p a time that is not one", {"title": "x", "kind": "event", "date": "2026-09-16", "scope": "college",
                                    "start_time": "25:00"}, "not a time"),
    ("4q an exam on a Sunday (#101)", {"title": "IA", "kind": "cie", "date": "2026-09-27", "dept": "CSE",
                                       "sem": 7}, "Sunday"),
    ("4r an exam on a holiday", {"title": "IA", "kind": "cie", "date": "2026-10-02", "dept": "CSE", "sem": 7},
     "holiday"),
    ("4s an exam that clashes with another for the same students",
     {"title": "IA", "kind": "cie", "date": "2026-09-29", "dept": "CSE", "sem": 7, "start_time": "10:00",
      "end_time": "11:00"}, "clashes"),
    ("4t an exam that clashes with a SEE paper", {"title": "IA", "kind": "cie", "date": "2026-09-25", "dept": "CSE",
                                                  "sem": 7, "section": "B"}, "SEE"),
    ("4u a holiday on a day with exams", {"title": "Closed", "kind": "holiday", "date": "2026-09-25",
                                          "scope": "college"}, "exam(s) are on that day"),
    ("4v a semester entry dated in another term",
     {"title": "x", "kind": "event", "date": "2027-02-16", "dept": "CSE", "sem": 7}, "2026-27 term"),
    ("4w an academic year that is not the date's",
     {"title": "x", "kind": "event", "date": "2026-09-16", "scope": "college", "academic_year": "2027-28"},
     "academic year 2026-27"),
    ("4x the same entry twice", {"title": "Founders' Day lecture", "kind": "event", "date": "2026-09-17",
                                 "scope": "college"}, "already on the calendar"),
    ("4y an exam spread over days", {"title": "IA", "kind": "cie", "date": "2026-09-15", "end_date": "2026-09-16",
                                     "dept": "CSE", "sem": 7}, "one day"),
    ("4z no kind", {"title": "x", "date": "2026-09-16", "scope": "college"}, "What kind"),
]
for name, a, want in CASES:
    why, wrote = refused(a)
    check(f"{name}: refused, nothing written", why and want.lower() in why.lower() and wrote == 0,
          f"{why!r} wrote={wrote}")
ok = call("calendar_events", {"dept": "CSE", "sem": 9})
check("4za a read for a class not on the rolls is refused too, never widened (#94)", "error" in ok["data"],
      str(ok["data"]))
check("4zb an exam for a whole class in a free hour is accepted",
      not refused({"title": "IA", "kind": "cie", "date": "2026-09-29", "dept": "CSE", "sem": 7, "section": "B",
                   "start_time": "09:30", "end_time": "10:30"})[0])

# ===================================================================== 5 · kinds
print("\n5 · each kind of entry reads as itself")
print("-" * 78)
x_prac = add(title="Mechanics practical exam", kind="practical", date="2026-09-21", start_time="09:00",
             end_time="12:00", dept="MECH", sem=5, section="A")
x_lab = add(kind="lab", date="2026-09-22", start_time="14:00", end_time="16:00", dept="ISE", sem=5,
            section="A", subject="BISL506")
byid = {str(e["id"]): e for e in ac.entries(con)}
see1 = next(e for e in byid.values() if e["source"] == "see")
hol1 = next(e for e in byid.values() if e["kind"] == "holiday")
for name, e, label in (("5a an internal exam", byid[x_cse7a], "Internal exam (CIE)"),
                       ("5b a SEE paper", see1, "Semester exam (SEE)"),
                       ("5c a practical exam", byid[x_prac], "Practical exam"),
                       ("5d a lab exam", byid[x_lab], "Lab exam"),
                       ("5e a college event", byid[x_all], "College event"),
                       ("5f a holiday", hol1, "Holiday")):
    d = dict(ac.detail_rows(e))
    check(f"{name} is labelled {label!r} and says who it is for", e["kind_label"] == label and d["Type"] == label
          and d["For"] == ac.audience(e), str((e["kind_label"], d)))
check("5g an exam's details carry its subject; an event's carry no subject row",
      "BCS703" in dict(ac.detail_rows(byid[x_cse7a]))["Subject"] and "Subject" not in dict(ac.detail_rows(byid[x_all])))
check("5h a lab exam with no title is named after its course",
      byid[x_lab]["title"] == "Lab exam: DBMS Lab", byid[x_lab]["title"])
check("5i an assignment due date is not an exam (the CIE scheme's AAT)",
      all(e["kind"] == "other" for e in byid.values() if e["title"] == "Assignment (AAT) due"))

# ============================================================== 6 · the agent
print("\n6 · the agent answers from the student's own calendar")
print("-" * 78)


def ask(p, q):
    sid = f"t6::{p['id']}::{q}"
    orchestrator.SESS.pop(sid, None)
    orchestrator.sess(sid)["user"] = p
    out = orchestrator.handle(con, q, sid, p["role"], actor=p["id"])
    reads = [t for t in out["trace"] if t["agent"] == "CalendarAgent"]
    denied = [t for t in out["trace"] if t["action"] == "access_denied"]
    return out, reads, denied


def shown(out):
    return [r for b in out["blocks"] if b["type"] == "table" for r in b["rows"]]


QS = ["What exams do I have this month?", "When is my next internal exam?",
      "What events are happening in college this week?", "Do I have an exam on September 29?",
      "Holidays in October"]
for q in QS:
    out, reads, denied = ask(CSE5A, q)
    check(f"6a {q!r} reaches the CalendarAgent", out.get("intent") == "calendar_events" and reads and not denied,
          str(out.get("intent")))
out, _r, _d = ask(CSE7A, "Do I have an exam on September 29?")
check("6b a CSE-7A student asking about 29 Sep is told about their internal exam",
      any("Cryptography" in str(r) for r in shown(out)) and "Yes" in out["blocks"][0].get("md", ""))
out, _r, _d = ask(CSE5A, "Do I have an exam on September 29?")
check("6c a CSE-5A student asking about the same day is not", not shown(out)
      and out["blocks"][0].get("md", "").startswith("No exams"), str(out["blocks"][0]))
out, _r, _d = ask(CSE5A, "What exams do I have this month?")
check("6d a 5th-sem student's exams this month hold no 7th-semester exam",
      shown(out) and not any("7A" in str(r) or "semester 7" in str(r) for r in shown(out)), str(shown(out)[:2]))
out, _r, denied = ask(CSE5A, "When is the 7th semester CSE exam?")
check("6e asking for the 7th-semester CSE exams as a 5th-sem student is refused, not answered",
      denied and not shown(out), str(out["blocks"][:1]))
out, reads, denied = ask(CSE7A, "When is the 7th semester CSE exam?")
check("6f ...and answered for a 7th-semester CSE student", reads and not denied)
for q, tool in (("Exam schedule", "exam_schedule"), ("My internal marks", "cie_marks"),
                ("My internal exam marks", "cie_marks"), ("Gate pass for Saturday 2pm to 7pm, parents know",
                                                          "request_gate_pass"),
                # found by diffing every quoted utterance's route against main: "event" took these
                ("Create a request for the hackathon event", "my_requests"),
                ("Inform all students about the fest", None)):
    ent = {"usn": None, "dept": None, "sem": None, "section": None, "date": None, "periods": [], "faculty": None}
    check(f"6g {q!r} still reaches {tool}", portal.route(con, q, CSE5A, ent)[0] == tool,
          str(portal.route(con, q, CSE5A, ent)))
for q, intent in (("Show the academic calendar", "calendar"), ("Holidays in October", "calendar"),
                  ("Declare 3 Oct a holiday college-wide", "calendar"), ("Cancel event 12", "calendar"),
                  ("Who is on leave this week", "absence.cover"), ("leave calendar", "absence.cover"),
                  ("Create a request for the hackathon event", "request.manage"),
                  ("Notify all students that 2 Oct is a holiday", "notify.broadcast"),
                  ("Exam schedule", "exam.query")):
    got = nlu.classify(q)
    check(f"6h the Registrar's {q!r} is {intent}", got and got[0][0] == intent, str(got[:2]))
before = n_rows()
for q in ("Declare 3 Oct a holiday college-wide", "Cancel event 1"):
    out, _r, denied = ask(CSE7A, q)
    check(f"6m a student's {q!r} meets the write and is refused by the policy, not answered as a question",
          denied and "Registrar" in out["blocks"][0].get("md", "") and n_rows() == before, str(out["blocks"][:1]))
a = ac.event_args('Add a college-wide event "Founders\' Day lecture" on 24 Sep 11am to 12:30pm in the Main auditorium')
check("6i a title in quotes keeps its apostrophe, and the sentence's audience is read",
      a.get("title") == "Founders' Day lecture" and a.get("scope") == "college" and a.get("date") == "2026-09-24"
      and a.get("venue") == "Main auditorium", str(a))
a = ac.event_args("Add a CSE-7A internal exam for BCS702 on 29 Sep 9:30am to 10:30am")
check("6j an exam's class, subject and times are read from the sentence",
      {k: a.get(k) for k in ("kind", "dept", "sem", "section", "subject", "start_time", "end_time")}
      == {"kind": "cie", "dept": "CSE", "sem": 7, "section": "A", "subject": "BCS702", "start_time": "9:30am",
          "end_time": "10:30am"}, str(a))
check("6k 'Declare 3 Oct a holiday' names no audience, so the tool will ask, not assume",
      "scope" not in ac.event_args("Declare 3 Oct a holiday") and "dept" not in ac.event_args("Declare 3 Oct a holiday"))
check("6l dates: Sundays kept, the academic year's own 15 Aug, Jan in the next year, '5 marks' is not a date",
      (str(ac.parse_date("27 Sep")), str(ac.parse_date("15 aug")), str(ac.parse_date("jan 26")),
       ac.parse_date("out of 5 marks")) == ("2026-09-27", "2026-08-15", "2027-01-26", None))

# ======================================================================= 7 · UI
print("\n7 · the pages")
print("-" * 78)
pg = open(os.path.join(HERE, "static", "portal.html"), encoding="utf-8").read()
ix = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
js = open(os.path.join(HERE, "static", "calendar.js"), encoding="utf-8").read()
check("7a the portal's navigation has a Calendar view, on a phone and on a desktop",
      pg.count('data-t="cal"') == 2 and "/static/calendar.js" in pg)
check("7b the console's sidebar has a Calendar view", 'data-v="cal"' in ix and "/static/calendar.js" in ix)
check("7c the portal asks only /api/me/calendar: the student's page holds no write",
      "/api/calendar/propose" not in pg and "/api/me/calendar" in pg)
check("7d the console's form proposes, and its approval goes through the chat as the Registrar's own words",
      "/api/calendar/propose" in ix and "reply('Yes, go ahead')" in ix and "engine:'rule'" in ix)
check("7e nothing on the calendar is drawn violet (violet means approval)",
      "violet" not in open(os.path.join(HERE, "static", "mawos.css"), encoding="utf-8").read()
      .split("academic calendar (#104)")[1].split("*/", 1)[1].split("/* ====")[0])
node = shutil.which("node")
if not node:
    print("  skip 7f-7k: node is not installed, so the renderer cannot be executed here")
else:
    prog = r"""
const C = require(process.argv[1]);
const out = {};
out.sep = C.grid('2026-09').slice(0, 2);
let ok = true;
for (let k = 0; k < 12; k++) {
  const m = C.shift('2026-07', k), g = C.grid(m), days = g.filter(Boolean);
  const y = +m.slice(0, 4), mo = +m.slice(5);
  if (g.length % 7 || days.length !== C.daysIn(y, mo) || days[0] !== m + '-01' ||
      g.indexOf(days[0]) !== C.weekday(days[0])) ok = false;
  for (let i = 1; i < days.length; i++) if (days[i] <= days[i - 1]) ok = false;
}
out.months = ok;
out.shift = [C.shift('2026-12', 1), C.shift('2027-01', -1), C.shift('2026-01', -13)];
out.sunday = C.weekday('2026-09-27');
out.covers = [C.covers({date: '2026-09-28', end_date: '2026-09-30'}, '2026-09-30'),
              C.covers({date: '2026-09-28'}, '2026-09-29')];
const el = {innerHTML: ''};
C.render(el, {month: '2026-09', today: '2026-09-04', events: [
  {id: 1, kind: 'event', title: '<img src=x onerror=alert(1)>', date: '2026-09-10', audience: '<b>x</b>'},
  {id: 2, kind: 'cie', title: 'IA', date: '2026-09-10', audience: 'CSE-7A'}]});
out.escaped = !el.innerHTML.includes('<img') && el.innerHTML.includes('&lt;img');
out.filter = [C.matches({kind: 'cie'}, 'exams'), C.matches({kind: 'event'}, 'exams'), C.matches({kind: 'holiday'}, '')];
const d = C.detail({id: 3, kind: 'event', title: 't', date: '2026-09-10', audience: 'College-wide', source: 'calendar', editable: true});
out.detail = [d.includes('Subject'), d.includes('data-cal-edit'), C.detail({id: 3, kind: 'event', title: 't',
  date: '2026-09-10', audience: 'x', source: 'calendar', editable: true}, {admin: true}).includes('data-cal-edit')];
console.log(JSON.stringify(out));
"""
    run = subprocess.run([node, "-e", prog, os.path.join(HERE, "static", "calendar.js")], capture_output=True,
                         text=True, encoding="utf-8")
    try:
        o = json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        o = {}
    check("7f the grid: September 2026 starts on a Tuesday, Monday first",
          o.get("sep") == [None, "2026-09-01"], run.stderr[-300:] or str(o.get("sep")))
    check("7g every month of the academic year is whole weeks, each date once, in order, on its own weekday",
          o.get("months") is True)
    check("7h month arithmetic crosses the year both ways", o.get("shift") == ["2027-01", "2026-12", "2024-12"],
          str(o.get("shift")))
    check("7i 27 Sep 2026 is drawn in the Sunday column; a multi-day entry covers its last day only",
          o.get("sunday") == 6 and o.get("covers") == [True, False], str((o.get("sunday"), o.get("covers"))))
    check("7j a title or audience carrying markup is drawn as text (#85)", o.get("escaped") is True)
    check("7k the type filter, and a student's details: no subject row, no change button",
          o.get("filter") == [True, False, True] and o.get("detail") == [False, False, True],
          str((o.get("filter"), o.get("detail"))))

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
