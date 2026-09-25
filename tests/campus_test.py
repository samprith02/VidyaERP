"""Campus services. No server, no LLM, no API cost.

    python tests/campus_test.py

Runs against a freshly seeded database in the system temp directory.

What this holds down, in the order it matters:

  1. the campus data layer never perturbs the core institution, is additive to
     an old database, and is internally consistent;
  2. each agent's RULES - circulation limits, hostel gender and capacity, the
     bus rebalancer's seat bound, the gate-pass policy, placement eligibility,
     no-dues gating of a transfer certificate, leave pricing - pinned case by
     case, so changing a rule is a deliberate act with a failing test attached;
  3. the rule engine routes the new vocabulary without stealing the old, and
     its propose -> yes / no loop writes exactly when the admin says so;
  4. the surfaces: read-only panels, and a console that still loads nothing.

Guard parity for the campus writes lives in tests/mcp_parity.py (section 16).
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db                                                          # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_campus_test.db")
# A FRESH seed, not a copy of college.db: the cases below assert on seeded
# state (a pending bonafide request, a waitlist, open complaints), and the live
# database's state depends on how the app has been used - smoke.py's bulk
# approval alone closes the request 7d needs.
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import campus                                                      # noqa: E402
import campus_data as cd                                           # noqa: E402
import nlu                                                         # noqa: E402
import orchestrator as orch                                        # noqa: E402
import tools                                                       # noqa: E402
from agents import one, rows                                       # noqa: E402
from nlu import TODAY                                              # noqa: E402

FAILED, PASSED = [], 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


def S():
    return {"pending": None, "history": [], "ctx": {}}


def run(name, args=None, U=""):
    return tools.execute(con, S(), U, name, args or {})


def table_hash(c, t):
    rs = [list(r) for r in c.execute(f"SELECT * FROM {t}")]
    rs.sort(key=lambda r: json.dumps(r, default=str))
    return hashlib.sha256(json.dumps(rs, default=str).encode()).hexdigest()[:16]


con = db.connect()

# ============================================================== 1 · data layer
print("\n1 · the campus data layer")
print("-" * 78)

# Fingerprints of the core tables as seeded BEFORE campus_data.py existed
# (recorded at the commit that introduced it). If the core generator is ever
# changed on purpose these must be re-recorded; if they move by accident, the
# campus layer has started drawing from the core's random stream.
CORE_BEFORE = {"departments": "7efe9382b9c8b4e3", "faculty": "67edc5d63dfad417",
               "subjects": "bdd0f4967bf5f247", "rooms": "62f84e7ff4ae41b3",
               "students": "7d9127ed8d4ac6f7", "timetable": "096aa88cd3f8fcc0",
               "leaves": "cfd3933e747f6a46", "requests": "d1f416a9743bf9df",
               "exams": "eb4480e1507f25fb", "placements": "fd42282b0b344f8b"}


def fresh(name):
    p = os.path.join(tempfile.gettempdir(), name)
    if os.path.exists(p):
        os.remove(p)
    keep = db.DB_PATH
    db.DB_PATH = p
    db.seed(force=True)
    db.DB_PATH = keep
    import sqlite3
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    return c, p


c1, p1 = fresh("vidyaerp_campus_a.db")
moved = [t for t, h in CORE_BEFORE.items() if table_hash(c1, t) != h]
check("1a the core institution is byte-identical with the campus layer present", not moved,
      f"moved: {moved}")
c2, p2 = fresh("vidyaerp_campus_b.db")
diff = [t for t in cd.TABLES + ["attendance"] if table_hash(c1, t) != table_hash(c2, t)]
check("1b campus seeding is deterministic across fresh seeds", not diff, str(diff))
before = {t: table_hash(c1, t) for t in cd.TABLES}
cd.ensure(c1)
check("1c ensure() is idempotent on a populated database",
      before == {t: table_hash(c1, t) for t in cd.TABLES})
for t in cd.TABLES:
    c2.execute(f"DROP TABLE {t}")
c2.execute("DELETE FROM attendance")
c2.commit()
c2.close()
keep = db.DB_PATH
db.DB_PATH = p2
db.seed()                                  # the non-force path an existing install takes
db.DB_PATH = keep
import sqlite3                                                     # noqa: E402
c2 = sqlite3.connect(p2)
check("1d an older college.db gains every campus table on the next start",
      all(c2.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
          ["books", "hostel_rooms", "bus_routes", "gate_passes", "placement_drives", "certificates"]))
check("1e ...with the same content a fresh seed produces",
      all(table_hash(c1, t) == table_hash(c2, t) for t in cd.TABLES))
c1.close(); c2.close()
os.remove(p1); os.remove(p2)

bad = []
for s in rows(con, "SELECT usn, attendance FROM students"):
    h = one(con, "SELECT SUM(held) h, SUM(attended) a FROM attendance WHERE usn=?", (s["usn"],))
    if not h["h"] or abs(100 * h["a"] / h["h"] - s["attendance"]) > 0.5:
        bad.append(s["usn"])
check("1f per-subject attendance reproduces every student's headline figure (±0.5)", not bad,
      f"{len(bad)} students, e.g. {bad[:3]}")
check("1g individual offers add up exactly to the placements aggregate",
      one(con, "SELECT COUNT(*) n FROM student_offers")["n"]
      == one(con, "SELECT SUM(offers) n FROM placements")["n"])
check("1h no title has more copies on loan than it owns",
      not rows(con, """SELECT b.id FROM books b WHERE b.copies < (SELECT COUNT(*) FROM book_loans l
                       WHERE l.book_id=b.id AND l.returned_on IS NULL)"""))
check("1i no hostel room is over capacity",
      not rows(con, """SELECT r.id FROM hostel_rooms r JOIN hostel_allocations a ON a.room=r.id
                       GROUP BY r.id HAVING COUNT(*) > r.capacity"""))
mism = [a["usn"] for a in rows(con, """SELECT a.usn, s.name, r.gender FROM hostel_allocations a
                                       JOIN students s ON s.usn=a.usn JOIN hostel_rooms r ON r.id=a.room""")
        if cd.gender_of(a["name"]) != a["gender"]]
check("1j every resident is in a block of their own gender", not mism, str(mism[:3]))
check("1k only hostel-registered students hold a bed",
      not rows(con, "SELECT a.usn FROM hostel_allocations a JOIN students s ON s.usn=a.usn WHERE s.hostel=0"))
check("1l only day scholars hold a bus pass",
      not rows(con, "SELECT r.usn FROM transport_riders r JOIN students s ON s.usn=r.usn WHERE s.hostel=1"))

# ================================================================= 2 · library
print("\n2 · library circulation rules")
print("-" * 78)
la = campus.LibraryAgent()
full = next(b for b in la.catalogue(con) if b["avail"] <= 0)
clean = next(s["usn"] for s in rows(con, "SELECT usn FROM students ORDER BY usn")
             if not la.active(con, s["usn"]))
r = run("issue_book", {"book": full["id"], "member": clean}, U="yes")
check("2a a title with no copy on the shelf is refused even WITH approval",
      "error" in r["data"] and "Copy on the shelf" in r["data"]["error"], str(r["data"])[:140])
late = next(o for o in la.overdue(con) if o["member_kind"] == "Student")
avail = next(b for b in la.catalogue(con) if b["avail"] > 1)
r = run("issue_book", {"book": avail["id"], "member": late["member"]}, U="yes")
check("2b a borrower with an overdue book is refused", "No overdue books" in str(r["data"].get("error")),
      str(r["data"])[:140])
check("2c a fine is days late × the published rate",
      la.fine(late) == (TODAY - dt.date.fromisoformat(late["due_on"])).days * cd.FINE_PER_DAY > 0)
n0 = la.available(con, avail["id"])
r = run("issue_book", {"book": avail["id"], "member": clean})
check("2d without approval an issue only proposes", "BLOCKED" in r["data"]
      and la.available(con, avail["id"]) == n0)
r = run("issue_book", {"book": avail["id"], "member": clean}, U="yes, issue it")
check("2e with approval it issues, and the shelf count drops by exactly one",
      r["data"].get("issued") and la.available(con, avail["id"]) == n0 - 1, str(r["data"])[:140])
r = run("return_book", {"member": late["member"]}, U="yes")
check("2f a return checks every held book in and records the fine",
      r["data"].get("returned") and not la.active(con, late["member"])
      and one(con, "SELECT fine_paid f FROM book_loans WHERE id=?", (late["id"],))["f"] == la.fine(late),
      str(r["data"]))

# ================================================================== 3 · hostel
print("\n3 · hostel allotment")
print("-" * 78)
ha = campus.HostelAgent()
wl = ha.waitlist(con)
r = run("allocate_hostel_room", {"usn": one(con, "SELECT usn FROM students WHERE hostel=0 LIMIT 1")["usn"]}, U="yes")
check("3a a day scholar is refused a bed", "day scholar" in str(r["data"].get("error")), str(r["data"]))
# Constructed, because the seeded rooms that hold a waitlisted student's
# batch-mates happen to be full: open ONE bed in such a room, and the matcher
# must choose it over every other free bed of the right gender.
s0 = wl[0]
g0 = cd.gender_of(s0["name"])
host = next(r for r in rows(con, """SELECT a.room, a.usn FROM hostel_allocations a JOIN students s ON s.usn=a.usn
                                  JOIN hostel_rooms h ON h.id=a.room WHERE s.dept=? AND s.sem=? AND h.gender=?
                                  ORDER BY a.room DESC""", (s0["dept"], s0["sem"], g0))
            if sum(1 for m in ha.mates(con)[r["room"]] if m["dept"] == s0["dept"] and m["sem"] == s0["sem"]) >= 2)
saved = one(con, "SELECT * FROM hostel_allocations WHERE usn=?", (host["usn"],))
con.execute("DELETE FROM hostel_allocations WHERE usn=?", (host["usn"],))
room = ha.best_room(con, s0)
con.execute("INSERT INTO hostel_allocations VALUES(?,?,?,?,?)", tuple(saved.values()))
con.commit()
check("3b the matcher puts a student with their batch-mates when that room has a free bed",
      room["id"] == host["room"], f"picked {room['id']}, expected {host['room']}")
r = run("allocate_hostel_room", {}, U="yes")
got = rows(con, """SELECT a.usn, s.name, r.gender FROM hostel_allocations a JOIN students s ON s.usn=a.usn
                   JOIN hostel_rooms r ON r.id=a.room WHERE a.usn IN (%s)""" % ",".join("?" * len(wl)),
           tuple(s["usn"] for s in wl))
check("3c the waitlist is allotted, gender-matched, within capacity",
      r["data"].get("verification") == "clean" and len(got) == r["data"]["allotted"] == len(wl)
      and all(cd.gender_of(g["name"]) == g["gender"] for g in got), str(r["data"])[:160])
check("3d ...and the waitlist is empty afterwards", not ha.waitlist(con))

# =============================================================== 4 · transport
print("\n4 · bus rebalancing")
print("-" * 78)
ta = campus.TransportAgent()
routes = {x["id"]: x for x in ta.routes(con)}
stops = {k: {s["stop"] for s in v} for k, v in ta.stops(con).items()}
load0 = ta.load(con)
moves, residual, after = ta.plan_rebalance(con)
check("4a every move is to a route that actually stops where the rider boards",
      moves and all(m["stop"] in stops[m["to"]] for m in moves), str(moves[:2]))
check("4b no receiving route is planned past its seats",
      all(after[m["to"]] <= routes[m["to"]]["capacity"] for m in moves))
check("4c the plan only takes riders OFF over-full routes",
      all(load0[m["from"]] > routes[m["from"]]["capacity"] for m in moves))
full = {k: routes[k]["capacity"] + 1 for k in routes}      # every bus over-full by one
full["R03"] += 10
check("4f when every other bus is already over-full, nobody is moved onto one",
      not ta.plan_rebalance(con, load=full)[0], str(ta.plan_rebalance(con, load=full)[0][:2]))
r = run("rebalance_bus_routes", {}, U="yes")
now = ta.load(con)
check("4d committed: the over-capacity total fell and no route was pushed over",
      r["data"].get("verification") == "clean"
      and sum(max(0, now[k] - routes[k]["capacity"]) for k in routes)
      < sum(max(0, load0[k] - routes[k]["capacity"]) for k in routes), str(r["data"])[:160])
r = run("handle_bus_breakdown", {"route": "Karkala bus broke down"}, U="yes")
rt = one(con, "SELECT * FROM bus_routes WHERE id='R04'")
check("4e a stop name resolves the route, and after breakdown cover every rider has a seat",
      r["data"].get("route") == "R04" and r["data"].get("riders_seated")
      and ta.load(con)["R04"] <= rt["capacity"] and rt["bus"].startswith("VT-BUS-S"), str(r["data"]))

# ============================================================= 5 · gate passes
print("\n5 · gate-pass policy, case by case")
print("-" * 78)
ga = campus.GatePassAgent()


def stu(where):
    return one(con, f"SELECT * FROM students WHERE {where} ORDER BY usn LIMIT 1")


hi = stu("hostel=1 AND attendance>=85 AND sem=3")        # sem 3: Fri ends 16:30, Sat 13:00
mid = stu("hostel=1 AND attendance>=75 AND attendance<85 AND sem=3")
low = stu("hostel=1 AND attendance<75 AND sem=3")
sen = stu("hostel=1 AND attendance>=75 AND sem=7")        # sem 7: Fri ends 14:40
D = lambda d, hm: (TODAY + dt.timedelta(days=d)).isoformat() + "T" + hm   # TODAY is a Friday


def gp(s, kind, out, ret, consent=1):
    return ga.evaluate(con, {"usn": s["usn"], "kind": kind, "out_at": out, "return_by": ret,
                             "parent_consent": consent})[0]


CASES = [
    ("emergency always clears", gp(low, "Emergency", D(0, "11:00"), D(3, "18:00")), "approve"),
    ("medical always clears", gp(low, "Medical", D(0, "10:00"), D(0, "12:00")), "approve"),
    ("Saturday-afternoon outing, back 19:30", gp(mid, "Outing", D(1, "14:00"), D(1, "19:30")), "approve"),
    ("outing back after the 20:00 curfew", gp(hi, "Outing", D(2, "17:00"), D(2, "22:30")), "reject"),
    ("class-hour outing below the 85% bar", gp(mid, "Outing", D(0, "11:00"), D(0, "15:00")), "reject"),
    ("class-hour outing above the 85% bar goes to a human", gp(hi, "Outing", D(0, "11:00"), D(0, "15:00")), "review"),
    ("no parent consent goes to the warden", gp(hi, "Home visit", D(1, "14:00"), D(2, "18:00"), 0), "review"),
    ("home visit missing Mon-Wed below 75%", gp(low, "Home visit", D(3, "08:00"), D(5, "19:00")), "reject"),
    ("Friday-evening home visit misses Saturday - allowed at ≥75%",
     gp(mid, "Home visit", D(0, "16:45"), D(2, "18:00")), "approve"),
    ("home visit over 3 class days goes to a human", gp(mid, "Home visit", D(3, "08:00"), D(5, "19:00")), "review"),
    ("Friday 15:00 is AFTER class for a sem-7 day", gp(sen, "Outing", D(0, "15:00"), D(0, "19:00")), "approve"),
    ("...but inside class for a sem-3 day", gp(mid, "Outing", D(0, "15:00"), D(0, "19:00")), "reject"),
]
for label, got, want in CASES:
    check(f"5 {label} → {want}", got == want, f"got {got}")
missed = ga.class_days_missed(3, dt.datetime.fromisoformat(D(0, "16:45")), dt.datetime.fromisoformat(D(2, "18:00")))
check("5x Saturday counts as a class day and Sunday does not",
      [d.strftime("%a") for d in missed] == ["Sat"], str(missed))
q = ga.queue(con)
r = run("decide_gate_passes", {"decision": "approve"}, U="approve them")
check("5y 'approve the compliant ones' approves only approve-verdicts and rejects nothing",
      one(con, "SELECT COUNT(*) n FROM gate_passes WHERE status='Rejected'")["n"] == 0
      and r["data"]["decided"] == sum(1 for x in q if x["verdict"] == "approve"), str(r["data"]))

# =============================================================== 6 · placement
print("\n6 · placement eligibility")
print("-" * 78)
pa = campus.PlacementAgent()
best = pa.best_offer(con)
for d in pa.drives(con):
    ok, _w = pa.eligibility(con, d)
    wrong = [s["usn"] for s in ok if s["cgpa"] < d["min_cgpa"] or s["backlogs"] > d["max_backlogs"]
             or s["dept"] not in d["depts"].split(",") or s["sem"] != 7
             or (s["usn"] in best and not (d["dream"] and d["ctc"] >= pa.DREAM_MULTIPLE * best[s["usn"]]))]
    check(f"6a {d['company']}: every eligible student meets every rule", not wrong, str(wrong[:3]))
nd = next(d for d in pa.drives(con) if not d["dream"])
placed_ok = [s for s in rows(con, "SELECT * FROM students WHERE sem=7") if s["usn"] in best
             and s["dept"] in nd["depts"] and s["cgpa"] >= nd["min_cgpa"] and s["backlogs"] <= nd["max_backlogs"]]
check("6b the one-offer rule holds a placed student out of a non-dream drive",
      placed_ok and not ({s["usn"] for s in placed_ok} & {s["usn"] for s in pa.eligibility(con, nd)[0]}))
n_ok = len(pa.eligibility(con, nd)[0])
r = run("publish_drive_shortlist", {"drive": nd["company"]}, U="yes publish")
r2 = run("publish_drive_shortlist", {"drive": nd["company"]}, U="yes publish")
check("6c a shortlist registers exactly the eligible set, and publishing twice adds nobody",
      r["data"].get("shortlisted") == n_ok and "already" in r2["data"].get("note", ""), f"{r['data']} / {r2['data']}")

# =============================================================== 7 · documents
print("\n7 · no-dues and certificates")
print("-" * 78)
debtor = stu("fee_due>0")
r = run("issue_certificate", {"usn": debtor["usn"], "kind": "Transfer Certificate"}, U="yes")
check("7a a transfer certificate is refused while any ledger shows a due",
      "error" in r["data"] and "Accounts" in r["data"]["error"], str(r["data"])[:140])
da = campus.DocumentAgent()
clear = next(s for s in rows(con, "SELECT * FROM students ORDER BY usn")
             if all(i["ok"] for i in da.no_dues(con, s["usn"])))
check("7b no-dues is CLEAR exactly when all four ledgers are", run("no_dues_status", {"usn": clear["usn"]})["data"]["clear"]
      and not run("no_dues_status", {"usn": debtor["usn"]})["data"]["clear"])
req = one(con, "SELECT * FROM requests WHERE kind='Bonafide' AND status='Pending'")
a = run("issue_certificate", {"usn": req["raised_by"], "kind": "bonafide", "purpose": "passport"}, U="yes")
b = run("issue_certificate", {"usn": clear["usn"], "kind": "Bonafide"}, U="yes")
na, nb = int(a["data"]["serial"].rsplit("/", 1)[1]), int(b["data"]["serial"].rsplit("/", 1)[1])
check("7c serials continue the register and never repeat", nb == na + 1
      and one(con, "SELECT COUNT(*) n FROM certificates")["n"]
      == one(con, "SELECT COUNT(DISTINCT serial) n FROM certificates")["n"], f"{na} → {nb}")
check("7d issuing a certificate closes the student's matching inbox request",
      a["data"]["request_closed"] == req["id"]
      and one(con, "SELECT status FROM requests WHERE id=?", (req["id"],))["status"] == "Approved")
doc = next(x for x in a["blocks"] if x["type"] == "document")
check("7e the issued document names the student and carries its serial",
      req["raised_by"] in doc["body"] and doc["serial"] == a["data"]["serial"] and doc["status"] == "issued")

# ================================================================== 8 · leave
print("\n8 · faculty leave, priced against the timetable")
print("-" * 78)
lv = campus.LeaveAgent()
pend = lv.pending(con)
assessed = [(l, lv.assess(con, l)) for l in pend]
check("8a every pending leave is assessed day by day", assessed and all(a["days"] for _l, a in assessed))
check("8b 'approve' is recommended only when every day is fully coverable (or has no classes)",
      all((a["rec"] == "approve") == (a["periods"] == 0 or a["worst"] == 100) for _l, a in assessed))
r = run("decide_leave", {}, U="approve all")
check("8c bulk approval touches only the fully-coverable ones",
      r["data"]["decided"] == sum(1 for _l, a in assessed if a["rec"] == "approve"))
r = run("decide_leave", {"decision": "reject"}, U="reject them")
check("8d bulk rejection is refused — a rejection needs a named leave", "error" in r["data"])

# ================================================== 9 · the rule engine's routes
print("\n9 · routing: new vocabulary, old routes kept")
print("-" * 78)
top = lambda t: (nlu.classify(t) or [("none",)])[0][0]
OLD = {"Brief me on today's institution status": "analytics.kpi",
       "Pending approvals in my inbox": "request.manage", "approve 3": "request.manage",
       "Attendance defaulters in ECE sem 5": "student.query", "Faculty workload above 90%": "faculty.query",
       "Show CSE 5th sem A timetable": "timetable.view", "Who is on leave this week?": "absence.cover",
       "Which faculty are free on Monday period 4?": "timetable.view",
       "Dr. Ramesh is absent tomorrow": "absence.cover",
       "Notify all CSE students that lab exams are postponed": "notify.broadcast",
       "Fee dues by department": "finance.query", "SEE eligibility check": "exam.query",
       "undo the last change": "absence.cover", "Rebuild the timetable for CSE sem 5 A": "timetable.generate"}
for t, want in OLD.items():
    check(f"9a kept: “{t}” → {want}", top(t) == want, top(t))
NEW = {"What needs my attention today?": "ops.radar", "Overdue library books": "library",
       "Issue BK0012 to 4VP24CS017": "library", "Hostel status": "hostel",
       "Allocate hostel rooms to the waitlist": "hostel", "Rebalance bus routes": "transport",
       "Bus on route 4 broke down": "transport", "approve gate pass 12": "gatepass",
       "Who is eligible for the Nethra Robotics drive?": "placement",
       "No dues status of 4VP23CS042": "document", "Issue a bonafide certificate for 4VP24CS017": "document",
       "Review pending leave applications": "leave.review", "Approve all pending leaves with coverage": "leave.review",
       "Everything about 4VP24CS017": "student.360"}
for t, want in NEW.items():
    check(f"9b new: “{t}” → {want}", top(t) == want, top(t))
AGENT_INTENT = {"RequestAgent": "request.manage", "HRAgent": "leave.review", "GatePassAgent": "gatepass",
                "LibraryAgent": "library", "HostelAgent": "hostel", "TransportAgent": "transport",
                "PlacementAgent": "placement", "DocumentAgent": "document", "ExamAgent": "exam.query"}
radar_cmds = []
# a FRESH seed, because sections 2-8 above cleared most of what the radar
# reports - and never the live college.db, whose state depends on usage
cr, pr = fresh("vidyaerp_campus_radar.db")
rad = tools.execute(cr, S(), "", "ops_radar", {})
for b in rad["blocks"]:
    if b["type"] == "actions":
        radar_cmds = b["items"]
check("9c the radar finds work on a fresh day", len(radar_cmds) >= 8, str(len(radar_cmds)))
for i in radar_cmds:
    want = AGENT_INTENT.get(i["agent"])
    check(f"9d radar command routes to its agent: “{i['command'][:48]}”", top(i["command"]) == want,
          f"{top(i['command'])} ≠ {want}")
    check(f"9e radar command proposes, never commits: “{i['command'][:48]}”",
          not tools.approved_this_turn(i["command"]))

# ------------------------------------------------ the rule engine's HITL loop
sid = "campus-hitl"
orch.SESS.pop(sid, None)
n_notes = one(cr, "SELECT COUNT(*) n FROM notifications")["n"]
out = orch.handle(cr, "Send overdue library reminders", sid)
check("9f a campus write from the rule engine proposes and stages", orch.sess(sid)["pending"]
      and orch.sess(sid)["pending"]["tool"] == "library_remind_overdue"
      and one(cr, "SELECT COUNT(*) n FROM notifications")["n"] == n_notes)
orch.handle(cr, "Show CSE 5th sem A timetable", sid)
check("9g an unrelated command neither commits nor drops the proposal",
      orch.sess(sid)["pending"] and one(cr, "SELECT COUNT(*) n FROM notifications")["n"] == n_notes)
out = orch.handle(cr, "no", sid)
check("9h 'no' discards it and writes nothing",
      orch.sess(sid)["pending"] is None and one(cr, "SELECT COUNT(*) n FROM notifications")["n"] == n_notes)
orch.handle(cr, "Send overdue library reminders", sid)
out = orch.handle(cr, "yes", sid)
check("9i 'yes' commits through tools.execute", one(cr, "SELECT COUNT(*) n FROM notifications")["n"] > n_notes
      and orch.sess(sid)["pending"] is None, str([b.get("md", b["type"]) for b in out["blocks"]])[:140])
cr.close()
os.remove(pr)

# ============================================================== 10 · surfaces
print("\n10 · surfaces")
print("-" * 78)
import app                                                          # noqa: E402
check("10a no gated write is reachable as a read-only panel", not (app.PANELS & set(tools.GATED_WRITES)),
      str(app.PANELS & set(tools.GATED_WRITES)))
check("10b every panel is a registered tool", app.PANELS <= set(tools.FUNCS), str(app.PANELS - set(tools.FUNCS)))
html = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
check("10c the console still loads nothing external",
      not re.findall(r"""(?:src|href)\s*=\s*["']?(?:https?:)?//""", html) and "@import" not in html
      and not re.search(r"fetch\(\s*['\"`]https?:", html))
m = re.search(r"const APPROVING=/(.+?)/i;", html)
check("10d the deep-link guard mirrors guard.APPROVAL_RX exactly",
      m and m.group(1) == tools.APPROVAL_RX.pattern, m and m.group(1)[:60])
print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
