"""
VidyaERP :: Campus-services agents

The paperwork a college office actually drowns in, each owned by a specialist:

  LibraryAgent    catalogue, issue / return, overdue fines, reminder runs
  HostelAgent     occupancy, waitlist allotment, complaint dispatch
  TransportAgent  route loads, rebalancing at shared stops, breakdown cover
  GatePassAgent   screens every outing / home-visit pass against written policy
  PlacementAgent  drive eligibility (CGPA, backlogs, branch, dream-offer rule)
  DocumentAgent   no-dues clearance across four ledgers, certificates with serials
  HRAgent         pending faculty leave, assessed against real coverage plans
  OpsRadar        scans every agent above for what needs a decision today
  Student360      one student, every ledger

Same contract as tools.py, and the same guard. Every write here is a single
tool that PROPOSES when the admin has not approved in this turn - it computes
the change, stages it in S["pending"] and writes nothing - and COMMITS only when
`approved_this_turn(U)` holds, where U is the admin's own words. Each gated tool
calls that function directly, which is what tests/mcp_parity.py:15c derives
GATED_WRITES from; a write that routed its check through a helper would drop
out of that derivation and the suite would say so.

After every commit the agent re-reads what it wrote before claiming success -
the same rule the timetable solver follows ("never report a write as clean on
the writer's own word").
"""
import re, datetime as dt
from collections import Counter
import db, nlu, campus_data as cd
from nlu import TODAY, day_of
from guard import approved_this_turn
from agents import (rows, one, fac_name, NotifyAgent, Auditor, SubstitutionAgent,
                    B_text, B_table, B_cards, B_notice, B_actions, B_document,
                    B_checklist, B_bars)

NOW = dt.datetime.combine(TODAY, dt.time(9, 30))   # the demo's operational clock
INSTITUTE = "Vidyatech Institute of Engineering"
ACTOR = "admin@vidyatech"


def inr(n):
    n = int(n or 0)
    if n >= 10_000_000: return f"₹{n/10_000_000:.2f} Cr"
    if n >= 100_000:    return f"₹{n/100_000:.2f} L"
    return f"₹{n:,}"


def _usn(x):
    m = re.search(r"\b4vp\d{2}[a-z]{2}\d{3}\b", str(x or ""), re.I)
    return m.group(0).upper() if m else None


def _stu(con, usn):
    return one(con, "SELECT * FROM students WHERE usn=?", ((usn or "").upper(),))


def _cls(s):
    return f"{s['dept']}-{s['sem']}{s['section']}"


def _when(iso):
    return dt.datetime.fromisoformat(iso).strftime("%a %d %b %H:%M")


def _date_words(d):
    return f"{d.day} {d.strftime('%b')}"


def _propose(S, name, args, blocks, summary, trace):
    """Stage a write for the admin to confirm. Writes NOTHING.

    Deliberately not where the gate is checked - every caller has already
    called approved_this_turn() itself. This only shapes the refusal: the
    proposal the admin needs to decide, and a BLOCKED key so every entry point
    classifies it identically."""
    S["pending"] = {"kind": "tool_commit", "tool": name,
                    "args": {k: v for k, v in (args or {}).items() if v is not None},
                    "summary": summary}
    return {"data": {"BLOCKED": "PolicyGuard: nothing has been written. This is a proposal - "
                                "show it to the admin and ask them to confirm. The same call "
                                "commits once they approve in their own message.",
                     "proposal": summary},
            "blocks": blocks + [B_text("**Nothing has been written yet.** Say **yes** to commit "
                                       "this, or **no** to discard it.")],
            "trace": list(trace) + [("PolicyGuard", "write_blocked",
                                     "proposal staged · awaiting explicit admin approval")],
            "chips": ["Yes, go ahead", "No, discard it"]}


def _done(S, name):
    if (S.get("pending") or {}).get("tool") == name:
        S["pending"] = None


def _err(msg, **extra):
    return {"data": {"error": msg, **extra}, "blocks": [B_text(f"{msg}")], "trace": []}


def _notify(con, msgs):
    if msgs:
        NotifyAgent().dispatch(con, msgs)


# ======================================================================= LIBRARY
STOP = {"search", "find", "library", "book", "books", "the", "for", "about", "any", "have", "do",
        "we", "show", "available", "copies", "copy", "of", "on", "is", "are", "in", "and", "with",
        "title", "titles", "please", "me", "what", "which", "there", "catalogue", "catalog", "list",
        "get", "check", "our", "all", "need", "want", "text", "textbook", "textbooks", "reference"}


class LibraryAgent:
    name = "LibraryAgent"

    def catalogue(self, con, where="", args=()):
        return rows(con, f"""SELECT b.*, b.copies - (SELECT COUNT(*) FROM book_loans l
                             WHERE l.book_id=b.id AND l.returned_on IS NULL) avail
                             FROM books b {where} ORDER BY b.id""", args)

    def active(self, con, member=None):
        q = """SELECT l.*, b.title, b.subject FROM book_loans l JOIN books b ON b.id=l.book_id
               WHERE l.returned_on IS NULL"""
        return rows(con, q + (" AND l.member=?" if member else "") + " ORDER BY l.due_on",
                    (member,) if member else ())

    def available(self, con, book_id):
        b = self.catalogue(con, "WHERE b.id=?", (book_id,))
        return b[0]["avail"] if b else 0

    @staticmethod
    def days_over(loan, on=TODAY):
        return max(0, (on - dt.date.fromisoformat(loan["due_on"])).days)

    def fine(self, loan, on=TODAY):
        return self.days_over(loan, on) * cd.FINE_PER_DAY

    def search(self, con, query):
        q = (query or "").strip()
        m = re.search(r"\bbk\d{3,4}\b", q, re.I)
        if m:
            return self.catalogue(con, "WHERE b.id=?", (m.group(0).upper(),))
        hits = []
        s, _sc = nlu.match_subject(q, rows(con, "SELECT code,name,dept,sem FROM subjects WHERE dept<>'ALL'"))
        if s:
            hits = self.catalogue(con, "WHERE b.subject=?", (s["code"],))
        toks = [t for t in re.findall(r"[a-z]{3,}", q.lower()) if t not in STOP]
        if toks:
            like = " AND ".join("(LOWER(b.title) LIKE ? OR LOWER(b.author) LIKE ?)" for _ in toks)
            more = self.catalogue(con, "WHERE " + like,
                                  tuple(x for t in toks for x in (f"%{t}%", f"%{t}%")))
            seen = {h["id"] for h in hits}
            hits += [h for h in more if h["id"] not in seen]
        return hits

    def resolve_book(self, con, ref):
        hits = self.search(con, ref)
        if not hits:
            return None, {"error": f"No book in the catalogue matches '{ref}'."}
        exact = [h for h in hits if h["title"].lower() == str(ref).lower().strip()]
        if len(hits) == 1 or exact:
            return (exact or hits)[0], None
        return None, {"ambiguous": True,
                      "candidates": [{"id": h["id"], "title": h["title"], "available": h["avail"]}
                                     for h in hits[:6]],
                      "instruction": "Ask the admin which title they mean (by BK id). Do not guess."}

    def member(self, con, ref):
        usn = _usn(ref)
        if usn:
            s = _stu(con, usn)
            if not s:
                return None, {"error": f"No student with USN {usn}."}
            return {"id": usn, "kind": "Student", "name": s["name"], "dept": s["dept"]}, None
        fac = rows(con, "SELECT id,name,dept,designation,expertise,max_load FROM faculty")
        f, sc = nlu.match_faculty(str(ref or ""), fac)
        if f and sc >= 0.8:
            return {"id": f["id"], "kind": "Faculty", "name": f["name"], "dept": f["dept"]}, None
        return None, {"error": f"No library member matches '{ref}' - give a USN or a staff name."}

    def overdue(self, con, dept=None):
        out = []
        for l in self.active(con):
            d = self.days_over(l)
            if d <= 0:
                continue
            if l["member_kind"] == "Student":
                s = _stu(con, l["member"])
                who, mdept, cls = s["name"], s["dept"], _cls(s)
            else:
                f = one(con, "SELECT name, dept FROM faculty WHERE id=?", (l["member"],))
                who, mdept, cls = f["name"], f["dept"], "Faculty"
            if dept and mdept != dept:
                continue
            out.append({**l, "days": d, "fine_due": d * cd.FINE_PER_DAY, "who": who,
                        "mdept": mdept, "cls": cls})
        out.sort(key=lambda x: -x["days"])
        return out

    def checks_for_issue(self, con, book, mem):
        """The circulation desk's rules, one row each, so a refusal says which."""
        mine = self.active(con, mem["id"])
        late = [l for l in mine if self.days_over(l) > 0]
        limit = cd.LOAN_LIMIT[mem["kind"]]
        return [
            {"agent": "LibraryAgent", "label": "Copy on the shelf", "ok": book["avail"] > 0,
             "detail": f"{book['avail']} of {book['copies']} available"},
            {"agent": "LibraryAgent", "label": f"Under the {mem['kind'].lower()} limit of {limit}",
             "ok": len(mine) < limit, "detail": f"holds {len(mine)}"},
            {"agent": "LibraryAgent", "label": "No overdue books", "ok": not late,
             "detail": (f"{len(late)} overdue, oldest {max(self.days_over(l) for l in late)} days"
                        if late else "clear")},
            {"agent": "LibraryAgent", "label": "Not already holding this title",
             "ok": book["id"] not in {l["book_id"] for l in mine}, "detail": book["id"]},
        ]


def t_library_overview(con, S, U, **kw):
    la = LibraryAgent()
    cat = la.catalogue(con)
    act = la.active(con)
    od = la.overdue(con)
    out = [b for b in cat if b["avail"] <= 0]
    demand = Counter(l["book_id"] for l in act)
    top = [next(b for b in cat if b["id"] == bid) | {"n": n} for bid, n in demand.most_common(8)]
    fines = sum(o["fine_due"] for o in od)
    return {"data": {"titles": len(cat), "copies": sum(b["copies"] for b in cat), "on_loan": len(act),
                     "overdue": len(od), "overdue_borrowers": len({o["member"] for o in od}),
                     "fines_accrued": fines, "titles_fully_out": len(out),
                     "fine_per_day": cd.FINE_PER_DAY},
            "blocks": [B_cards([{"label": "Titles", "value": len(cat),
                                 "sub": f"{sum(b['copies'] for b in cat)} copies"},
                                {"label": "On loan", "value": len(act)},
                                {"label": "Overdue loans", "value": len(od), "tone": "bad" if od else "good",
                                 "sub": f"{len({o['member'] for o in od})} borrowers"},
                                {"label": "Fines accrued", "value": inr(fines), "tone": "warn",
                                 "sub": f"₹{cd.FINE_PER_DAY}/day/book"},
                                {"label": "Titles fully out", "value": len(out),
                                 "tone": "warn" if out else "good"}], title="Central Library"),
                       B_table(["ID", "Title", "Subject", "On loan", "Copies"],
                               [[b["id"], b["title"], b["subject"] or "General", b["n"], b["copies"]]
                                for b in top], title="Most borrowed right now", dense=True),
                       B_table(["ID", "Title", "Copies", "Shelf"],
                               [[b["id"], b["title"], b["copies"], b["shelf"]] for b in out[:10]],
                               title="No copy on the shelf — reservation queue candidates", dense=True)],
            "trace": [("LibraryAgent", "circulation_scan",
                       f"{len(act)} active loans · {len(od)} overdue · {len(out)} titles fully out")],
            "chips": ["Overdue library books", "Send overdue library reminders",
                      "Search the library for machine learning"]}


def t_library_search(con, S, U, query="", **kw):
    hits = LibraryAgent().search(con, query)
    return {"data": {"query": query, "count": len(hits),
                     "books": [{"id": b["id"], "title": b["title"], "author": b["author"],
                                "available": b["avail"], "copies": b["copies"]} for b in hits[:15]]},
            "blocks": [B_table(["ID", "Title", "Author", "Subject", "Available", "Shelf"],
                               [[b["id"], b["title"], b["author"], b["subject"] or "General",
                                 f"{b['avail']}/{b['copies']}", b["shelf"]] for b in hits[:15]],
                               title=f"Catalogue — {len(hits)} match(es) for “{query}”", dense=True)
                       if hits else B_text(f"No title in the catalogue matches “{query}”.")],
            "trace": [("LibraryAgent", "catalogue_search", f"'{query[:40]}' → {len(hits)} title(s)")]}


def t_library_overdue(con, S, U, dept=None, **kw):
    od = LibraryAgent().overdue(con, dept.upper() if dept else None)
    fines = sum(o["fine_due"] for o in od)
    return {"data": {"count": len(od), "borrowers": len({o["member"] for o in od}), "fines": fines,
                     "loans": [{"member": o["member"], "name": o["who"], "book": o["title"],
                                "days_overdue": o["days"], "fine": o["fine_due"]} for o in od[:25]]},
            "blocks": [B_cards([{"label": "Overdue loans", "value": len(od), "tone": "bad"},
                                {"label": "Borrowers", "value": len({o['member'] for o in od})},
                                {"label": "Fines accrued", "value": inr(fines), "tone": "warn"},
                                {"label": "Worst", "value": f"{od[0]['days']} days" if od else "—"}],
                               title="Overdue circulation" + (f" · {dept.upper()}" if dept else "")),
                       B_table(["Member", "Name", "Class", "Book", "Due", "Days over", "Fine"],
                               [[o["member"], o["who"], o["cls"], f"{o['book_id']} · {o['title']}",
                                 o["due_on"], o["days"], inr(o["fine_due"])] for o in od[:25]],
                               dense=True)],
            "trace": [("LibraryAgent", "overdue_scan", f"{len(od)} overdue · {inr(fines)} accrued at "
                                                       f"₹{cd.FINE_PER_DAY}/day")],
            "chips": ["Send overdue library reminders", "Library status"]}


def t_issue_book(con, S, U, book="", member="", **kw):
    la = LibraryAgent()
    b, err = la.resolve_book(con, book)
    if err:
        return {"data": err, "blocks": [B_text(f"{err.get('error') or 'Which title did you mean?'}")]
                + ([B_table(["ID", "Title", "Available"], [[c["id"], c["title"], c["available"]]
                                                           for c in err.get("candidates", [])])]
                   if err.get("candidates") else []), "trace": []}
    m, err = la.member(con, member)
    if err:
        return _err(err["error"])
    checks = la.checks_for_issue(con, b, m)
    due = TODAY + dt.timedelta(days=cd.LOAN_DAYS[m["kind"]])
    tr = [("LibraryAgent", "circulation_rules", " · ".join(
        f"{c['label']}={'ok' if c['ok'] else 'FAIL'}" for c in checks))]
    if not all(c["ok"] for c in checks):
        failed = [c["label"] for c in checks if not c["ok"]]
        return {"data": {"error": "Issue refused by circulation rules: " + "; ".join(failed),
                         "checks": checks},
                "blocks": [B_checklist(checks, title=f"{b['id']} → {m['name']} — refused")],
                "trace": tr}
    args = {"book": b["id"], "member": m["id"]}
    if not approved_this_turn(U):
        return _propose(S, "issue_book", args,
                        [B_checklist(checks, title=f"Issue {b['id']} · {b['title']}"),
                         B_table(["Field", "Value"], [["Borrower", f"{m['name']} ({m['id']}, {m['kind']})"],
                                                      ["Due back", due.isoformat()],
                                                      ["Fine if late", f"₹{cd.FINE_PER_DAY}/day"]])],
                        f"issue {b['id']} '{b['title']}' to {m['id']}, due {due}", tr)
    con.execute("INSERT INTO book_loans(book_id,member,member_kind,issued_on,due_on) VALUES(?,?,?,?,?)",
                (b["id"], m["id"], m["kind"], TODAY.isoformat(), due.isoformat()))
    con.commit()
    left = la.available(con, b["id"])                  # verify by re-reading, not by arithmetic
    if left < 0:
        con.execute("DELETE FROM book_loans WHERE id=(SELECT MAX(id) FROM book_loans)")
        con.commit()
        out = _err("Issue rolled back: the shelf count went negative on re-read.")
        out["trace"] = [("LibraryAgent", "verify", "shelf count negative on re-read - rolled back", "error")]
        return out
    Auditor().log(con, ACTOR, "LibraryAgent", "library.issue", args, "Applied")
    _done(S, "issue_book")
    return {"data": {"issued": True, "book": b["id"], "member": m["id"], "due": due.isoformat(),
                     "copies_left": left},
            "blocks": [B_text(f"**{b['title']}** ({b['id']}) issued to **{m['name']}** — due back "
                              f"**{due.strftime('%d %b %Y')}**. {left} cop{'y' if left == 1 else 'ies'} "
                              f"left on the shelf (re-read after the write).")],
            "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("LibraryAgent", "issue", f"{b['id']} → {m['id']}"),
                           ("LibraryAgent", "verify", f"shelf count re-read: {left}", "ok"),
                           ("Auditor", "ledger_write", "library.issue")]}


def t_return_book(con, S, U, member="", book=None, **kw):
    la = LibraryAgent()
    m, err = la.member(con, member)
    if err:
        return _err(err["error"])
    loans = la.active(con, m["id"])
    if book:
        ref = str(book).upper()
        loans = [l for l in loans if l["book_id"] == ref or str(book).lower() in l["title"].lower()]
    if not loans:
        return _err(f"{m['name']} has no matching book on loan.")
    total = sum(la.fine(l) for l in loans)
    args = {"member": m["id"], "book": book}
    tbl = B_table(["Book", "Issued", "Due", "Days over", "Fine"],
                  [[f"{l['book_id']} · {l['title']}", l["issued_on"], l["due_on"], la.days_over(l),
                    inr(la.fine(l))] for l in loans], title=f"Return — {m['name']} ({m['id']})")
    tr = [("LibraryAgent", "fine_compute", f"{len(loans)} book(s) · {inr(total)} at ₹{cd.FINE_PER_DAY}/day")]
    if not approved_this_turn(U):
        return _propose(S, "return_book", args, [tbl],
                        f"return {len(loans)} book(s) from {m['id']}, collect {inr(total)}", tr)
    for l in loans:
        con.execute("UPDATE book_loans SET returned_on=?, fine_paid=? WHERE id=?",
                    (TODAY.isoformat(), la.fine(l), l["id"]))
    con.commit()
    still = [l for l in la.active(con, m["id"]) if l["id"] in {x["id"] for x in loans}]
    Auditor().log(con, ACTOR, "LibraryAgent", "library.return", {**args, "fine": total},
                  "Applied" if not still else "Partial")
    _done(S, "return_book")
    return {"data": {"returned": len(loans) - len(still), "fine_collected": total},
            "blocks": [tbl, B_text(f"{len(loans) - len(still)} book(s) checked in"
                                   + (f"; collect **{inr(total)}** in fines at the counter." if total
                                      else " — no fine due."))],
            "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("LibraryAgent", "verify", f"{len(still)} of those still on loan after re-read",
                            "error" if still else "ok"),
                           ("Auditor", "ledger_write", "library.return")]}


def t_library_remind_overdue(con, S, U, **kw):
    od = LibraryAgent().overdue(con)
    by = {}
    for o in od:
        by.setdefault(o["member"], []).append(o)
    msgs = []
    for mem, ls in sorted(by.items()):
        fine = sum(l["fine_due"] for l in ls)
        msgs.append({"audience": f"{ls[0]['member_kind']} · {mem} ({ls[0]['who']})",
                     "channel": "App push + SMS",
                     "title": f"Library: {len(ls)} overdue book(s)",
                     "body": "; ".join(f"{l['title']} ({l['days']} days late)" for l in ls)
                             + f". Fine so far {inr(fine)}, rising ₹{cd.FINE_PER_DAY}/day per book. "
                               f"Please return at the circulation desk."})
    if not msgs:
        return {"data": {"note": "No overdue loans — nothing to send."},
                "blocks": [B_text("No overdue loans — nobody to remind.")], "trace": []}
    tr = [("LibraryAgent", "overdue_scan", f"{len(od)} loans across {len(msgs)} borrowers"),
          ("NotifyAgent", "draft_compose", f"{len(msgs)} personalised reminders")]
    if not approved_this_turn(U):
        return _propose(S, "library_remind_overdue", {},
                        [B_text(f"**{len(msgs)} personalised reminders** drafted, one per borrower. "
                                f"First three:"), B_notice(msgs[:3])],
                        f"send {len(msgs)} overdue reminders", tr)
    _notify(con, msgs)
    Auditor().log(con, ACTOR, "LibraryAgent", "library.remind", {"reminders": len(msgs)}, "Applied")
    _done(S, "library_remind_overdue")
    return {"data": {"sent": len(msgs)},
            "blocks": [B_text(f"**{len(msgs)} reminders sent** to overdue borrowers "
                              f"(app push + SMS).")], "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("NotifyAgent", "dispatch", f"{len(msgs)} messages")]}


# ======================================================================== HOSTEL
class HostelAgent:
    name = "HostelAgent"

    def occupancy(self, con):
        return {r["room"]: r["n"] for r in rows(con, "SELECT room, COUNT(*) n FROM hostel_allocations GROUP BY room")}

    def blocks(self, con):
        occ = self.occupancy(con)
        out = {}
        for r in rows(con, "SELECT * FROM hostel_rooms ORDER BY id"):
            b = out.setdefault(r["block"], {"block": r["block"], "gender": r["gender"], "rooms": 0,
                                            "beds": 0, "occupied": 0})
            b["rooms"] += 1
            b["beds"] += r["capacity"]
            b["occupied"] += occ.get(r["id"], 0)
        return list(out.values())

    def waitlist(self, con):
        return rows(con, """SELECT * FROM students WHERE hostel=1 AND usn NOT IN
                            (SELECT usn FROM hostel_allocations) ORDER BY usn""")

    def mates(self, con):
        m = {}
        for r in rows(con, """SELECT a.room, s.* FROM hostel_allocations a JOIN students s ON s.usn=a.usn
                              ORDER BY a.room, s.usn"""):
            m.setdefault(r["room"], []).append(r)
        return m

    def best_room(self, con, s, extra=None, mates=None):
        """Same gender, a free bed, then: batch-mates first, then branch-mates,
        then fill part-filled rooms before opening empty ones, lowest floor."""
        extra = extra or {}
        mates = mates if mates is not None else self.mates(con)
        occ = self.occupancy(con)
        g = cd.gender_of(s["name"])
        best, key = None, None
        for r in rows(con, "SELECT * FROM hostel_rooms WHERE gender=? ORDER BY id", (g,)):
            n = occ.get(r["id"], 0) + extra.get(r["id"], 0)
            if n >= r["capacity"]:
                continue
            ms = mates.get(r["id"], [])
            k = (sum(1 for x in ms if x["dept"] == s["dept"] and x["sem"] == s["sem"]),
                 sum(1 for x in ms if x["dept"] == s["dept"]), n, -r["floor"])
            if key is None or k > key:
                best, key = r, k
        return best


def t_hostel_status(con, S, U, **kw):
    ha = HostelAgent()
    blk = ha.blocks(con)
    wl = ha.waitlist(con)
    comp = rows(con, "SELECT * FROM hostel_complaints WHERE status='Open'")
    beds, occ = sum(b["beds"] for b in blk), sum(b["occupied"] for b in blk)
    vac = {g: sum(b["beds"] - b["occupied"] for b in blk if b["gender"] == g) for g in ("M", "F")}
    wlg = Counter(cd.gender_of(s["name"]) for s in wl)
    dues = one(con, "SELECT SUM(fee_due) s, COUNT(*) n FROM hostel_allocations WHERE fee_due>0")
    return {"data": {"beds": beds, "occupied": occ, "occupancy_pct": round(100 * occ / max(1, beds), 1),
                     "vacant_beds": vac, "waitlist": {"total": len(wl), **wlg},
                     "open_complaints": len(comp), "hostel_fee_due": dues["s"] or 0,
                     "blocks": blk},
            "blocks": [B_cards([{"label": "Occupancy", "value": f"{round(100 * occ / max(1, beds))}%",
                                 "sub": f"{occ}/{beds} beds"},
                                {"label": "Vacant beds", "value": vac["M"] + vac["F"],
                                 "sub": f"boys {vac['M']} · girls {vac['F']}"},
                                {"label": "Waitlist", "value": len(wl), "tone": "warn" if wl else "good",
                                 "sub": f"boys {wlg.get('M', 0)} · girls {wlg.get('F', 0)}"},
                                {"label": "Open complaints", "value": len(comp),
                                 "tone": "bad" if comp else "good"},
                                {"label": "Hostel fee due", "value": inr(dues["s"] or 0),
                                 "sub": f"{dues['n']} residents"}], title="Hostels"),
                       B_bars([{"label": b["block"], "value": b["occupied"], "max": b["beds"]} for b in blk],
                              title="Occupancy by block", unit="beds"),
                       B_table(["USN", "Name", "Class", "Gender"],
                               [[s["usn"], s["name"], _cls(s), cd.gender_of(s["name"])] for s in wl],
                               title="Allotment waitlist", dense=True) if wl else B_text("Waitlist is empty.")],
            "trace": [("HostelAgent", "occupancy_scan", f"{occ}/{beds} beds · waitlist {len(wl)} · "
                                                        f"{len(comp)} open complaints")],
            "chips": ["Allocate hostel rooms to the waitlist", "Show open hostel complaints",
                      "Dispatch open hostel complaints"]}


def t_hostel_complaints(con, S, U, status="Open", **kw):
    st = (status or "Open").title()
    c = rows(con, "SELECT * FROM hostel_complaints WHERE status=? ORDER BY raised_on", (st,))
    for x in c:
        x["age_h"] = int((NOW - dt.datetime.fromisoformat(x["raised_on"] + "T00:00")).total_seconds() // 3600)
        x["breach"] = x["age_h"] > x["sla_hrs"]
    return {"data": {"status": st, "count": len(c), "past_sla": sum(1 for x in c if x["breach"]),
                     "complaints": [{"id": x["id"], "room": x["room"], "category": x["category"],
                                     "details": x["details"], "age_hours": x["age_h"]} for x in c]},
            "blocks": [B_table(["ID", "Room", "Category", "Details", "Priority", "Age", "SLA"],
                               [[x["id"], x["room"], x["category"], x["details"], x["priority"],
                                 f"{x['age_h']} h", ("**breached**" if x["breach"] else f"{x['sla_hrs']} h")]
                                for x in c], title=f"{st} hostel complaints", dense=True)],
            "trace": [("HostelAgent", "complaint_scan", f"{len(c)} {st.lower()} · "
                                                        f"{sum(1 for x in c if x['breach'])} past SLA")],
            "chips": ["Dispatch open hostel complaints", "Hostel status"]}


def t_allocate_hostel_room(con, S, U, usn=None, **kw):
    ha = HostelAgent()
    if usn:
        s = _stu(con, _usn(usn) or usn)
        if not s:
            return _err(f"No student with USN {usn}.")
        if not s["hostel"]:
            return _err(f"{s['name']} ({s['usn']}) is a day scholar — not registered for the hostel.")
        cur = one(con, "SELECT room FROM hostel_allocations WHERE usn=?", (s["usn"],))
        if cur:
            return _err(f"{s['name']} already lives in {cur['room']}.")
        todo = [s]
    else:
        todo = ha.waitlist(con)
        if not todo:
            return {"data": {"note": "The allotment waitlist is empty."},
                    "blocks": [B_text("The hostel waitlist is empty — nothing to allot.")], "trace": []}
    extra, mates, plan, unplaced = {}, ha.mates(con), [], []
    for s in todo:
        r = ha.best_room(con, s, extra, mates)
        if not r:
            unplaced.append(s)
            continue
        extra[r["id"]] = extra.get(r["id"], 0) + 1
        plan.append({"usn": s["usn"], "name": s["name"], "cls": _cls(s), "room": r["id"],
                     "block": r["block"], "mates": [m["name"] for m in mates.get(r["id"], [])]})
        mates.setdefault(r["id"], []).append(s)
    tr = [("HostelAgent", "match_rooms", f"{len(plan)} matched · {len(unplaced)} with no free bed · "
                                         f"rule: same gender → batch-mates → branch-mates → fill part-rooms")]
    if not plan:
        return _err("No vacant bed of the right block for " + ", ".join(s["usn"] for s in unplaced))
    tbl = B_table(["USN", "Name", "Class", "Room", "Block", "Roommates"],
                  [[p["usn"], p["name"], p["cls"], p["room"], p["block"], ", ".join(p["mates"]) or "—"]
                   for p in plan], title="Proposed allotment")
    args = {"usn": usn}
    if not approved_this_turn(U):
        return _propose(S, "allocate_hostel_room", args, [tbl],
                        f"allot {len(plan)} hostel bed(s)", tr)
    for p in plan:
        con.execute("INSERT INTO hostel_allocations VALUES(?,?,?,?,?)",
                    (p["usn"], p["room"], TODAY.isoformat(), "To be chosen", 0))
    con.commit()
    bad = rows(con, """SELECT r.id, r.capacity, COUNT(a.usn) n FROM hostel_rooms r JOIN hostel_allocations a
                       ON a.room=r.id GROUP BY r.id HAVING n > r.capacity""")
    wrong = [p for p in plan if one(con, "SELECT gender FROM hostel_rooms WHERE id=?", (p["room"],))["gender"]
             != cd.gender_of(p["name"])]
    _notify(con, [{"audience": f"Student · {p['usn']}", "channel": "App push + email",
                   "title": "Hostel room allotted", "body": f"Room {p['room']}, {p['block']}. Report to "
                   f"the warden's office with your ID card to collect the key."} for p in plan])
    Auditor().log(con, ACTOR, "HostelAgent", "hostel.allocate", {"n": len(plan)},
                  "Applied" if not bad and not wrong else "Applied with problems")
    _done(S, "allocate_hostel_room")
    return {"data": {"allotted": len(plan), "verification": "clean" if not bad and not wrong else
                     {"over_capacity": bad, "gender_mismatch": wrong}},
            "blocks": [tbl, B_text(f"**{len(plan)} bed(s) allotted** and students notified. Re-read "
                                   f"after the write: {len(bad)} room(s) over capacity, {len(wrong)} "
                                   f"gender mismatches.")], "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("HostelAgent", "verify", f"capacity breaches {len(bad)} · gender mismatches {len(wrong)}",
                            "error" if bad or wrong else "ok"),
                           ("NotifyAgent", "dispatch", f"{len(plan)} allotment letters")]}


def t_dispatch_hostel_complaints(con, S, U, **kw):
    c = rows(con, "SELECT * FROM hostel_complaints WHERE status='Open' ORDER BY priority, raised_on")
    if not c:
        return {"data": {"note": "No open complaints."}, "blocks": [B_text("No open hostel complaints.")],
                "trace": []}
    crews = Counter(cd.CREW[x["category"]] for x in c)
    tbl = B_table(["ID", "Room", "Issue", "Routed to", "SLA"],
                  [[x["id"], x["room"], f"{x['category']} — {x['details']}", cd.CREW[x["category"]],
                    f"{x['sla_hrs']} h"] for x in c], title="Triage", dense=True)
    tr = [("HostelAgent", "triage", " · ".join(f"{k}: {v}" for k, v in crews.items()))]
    if not approved_this_turn(U):
        return _propose(S, "dispatch_hostel_complaints", {}, [tbl],
                        f"route {len(c)} complaint(s) to {len(crews)} crew(s)", tr)
    for x in c:
        con.execute("UPDATE hostel_complaints SET status='Assigned', assigned_to=? WHERE id=?",
                    (cd.CREW[x["category"]], x["id"]))
    con.commit()
    left = one(con, "SELECT COUNT(*) n FROM hostel_complaints WHERE status='Open'")["n"]
    _notify(con, [{"audience": crew, "channel": "App + SMS", "title": f"{n} hostel job(s) assigned",
                   "body": "; ".join(f"#{x['id']} {x['room']}: {x['details']}" for x in c
                                     if cd.CREW[x["category"]] == crew)} for crew, n in crews.items()])
    Auditor().log(con, ACTOR, "HostelAgent", "hostel.dispatch", {"n": len(c)}, "Applied")
    _done(S, "dispatch_hostel_complaints")
    return {"data": {"assigned": len(c), "crews": dict(crews), "still_open": left},
            "blocks": [tbl, B_text(f"**{len(c)} complaint(s) dispatched** to {len(crews)} crew(s); "
                                   f"{left} still open after re-read.")], "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("HostelAgent", "verify", f"{left} still open after re-read", "error" if left else "ok"),
                           ("NotifyAgent", "dispatch", f"{len(crews)} crew notices")]}


# ===================================================================== TRANSPORT
class TransportAgent:
    name = "TransportAgent"

    def routes(self, con, active_only=True):
        return rows(con, "SELECT * FROM bus_routes" + (" WHERE status='Active'" if active_only else "")
                    + " ORDER BY id")

    def load(self, con):
        return {r["route"]: r["n"] for r in rows(con, "SELECT route, COUNT(*) n FROM transport_riders GROUP BY route")}

    def stops(self, con):
        out = {}
        for r in rows(con, "SELECT * FROM bus_stops ORDER BY route, seq"):
            out.setdefault(r["route"], []).append(r)
        return out

    def resolve(self, con, ref):
        t = str(ref or "").lower()
        m = re.search(r"\br?0?(\d{1,2})\b", t)
        if m and one(con, "SELECT 1 FROM bus_routes WHERE id=?", (f"R{int(m.group(1)):02d}",)):
            return f"R{int(m.group(1)):02d}"
        for r, ss in self.stops(con).items():
            if any(s["stop"].lower() in t for s in ss):
                return r
        return None

    def plan_rebalance(self, con, only=None, load=None):
        """Move riders off an over-full bus at stops ANOTHER active route also
        serves, never pushing that route past its own seats. Riders at a stop no
        other bus visits cannot be moved - that residual is reported, not hidden."""
        routes = {r["id"]: r for r in self.routes(con)}
        load = dict(load or self.load(con))
        stops = self.stops(con)
        moves, moved = [], set()
        order = sorted(routes, key=lambda r: -(load.get(r, 0) - routes[r]["capacity"]))
        for rid in order:
            if only and rid != only:
                continue
            over = load.get(rid, 0) - routes[rid]["capacity"]
            for st in stops.get(rid, []):
                if over <= 0:
                    break
                for oid in sorted(routes):
                    if oid == rid or over <= 0:
                        continue
                    theirs = next((x for x in stops.get(oid, []) if x["stop"] == st["stop"]), None)
                    spare = routes[oid]["capacity"] - load.get(oid, 0)
                    if not theirs or spare <= 0:
                        continue
                    riders = [r for r in rows(con, "SELECT usn FROM transport_riders WHERE route=? AND stop=? "
                                                   "ORDER BY usn", (rid, st["stop"])) if r["usn"] not in moved]
                    for r in riders[:min(over, spare)]:
                        moves.append({"usn": r["usn"], "from": rid, "to": oid, "stop": st["stop"],
                                      "pickup": theirs["pickup"]})
                        moved.add(r["usn"])
                        load[rid] -= 1
                        load[oid] = load.get(oid, 0) + 1
                        over -= 1
        residual = {r: load.get(r, 0) - routes[r]["capacity"] for r in routes
                    if load.get(r, 0) > routes[r]["capacity"]}
        return moves, residual, load


def t_transport_status(con, S, U, route=None, **kw):
    ta = TransportAgent()
    load = ta.load(con)
    allr = ta.routes(con, active_only=False)
    act = [r for r in allr if r["status"] == "Active"]
    cap = sum(r["capacity"] for r in act)
    riders = sum(load.values())
    over = [r for r in act if load.get(r["id"], 0) > r["capacity"]]
    dues = one(con, "SELECT SUM(fee_due) s, COUNT(*) n FROM transport_riders WHERE fee_due>0")
    blocks = [B_cards([{"label": "Riders", "value": riders, "sub": f"{len(act)} active routes"},
                       {"label": "Seat utilisation", "value": f"{round(100 * riders / max(1, cap))}%",
                        "sub": f"{cap} seats"},
                       {"label": "Over capacity", "value": len(over), "tone": "bad" if over else "good",
                        "sub": ", ".join(f"{r['id']} +{load[r['id']] - r['capacity']}" for r in over) or "none"},
                       {"label": "Spare buses", "value": sum(1 for r in allr if r["status"] == "Spare")},
                       {"label": "Transport fee due", "value": inr(dues["s"] or 0),
                        "sub": f"{dues['n']} riders"}], title="College transport"),
              B_bars([{"label": f"{r['id']} {r['name']}", "value": load.get(r["id"], 0), "max": r["capacity"]}
                      for r in act], title="Seats taken per route", unit="riders")]
    rid = ta.resolve(con, route) if route else None
    if rid:
        r = next(x for x in allr if x["id"] == rid)
        per = Counter(x["stop"] for x in rows(con, "SELECT stop FROM transport_riders WHERE route=?", (rid,)))
        blocks.append(B_table(["#", "Stop", "Pickup", "Riders"],
                              [[s["seq"], s["stop"], s["pickup"], per.get(s["stop"], 0)]
                               for s in ta.stops(con).get(rid, [])],
                              title=f"{rid} · {r['name']} · {r['bus']} · driver {r['driver']} ({r['driver_phone']})"))
    else:
        blocks.append(B_table(["Route", "Name", "Bus", "Departs", "Riders", "Status", "Driver"],
                              [[r["id"], r["name"], r["bus"], r["departs"] or "—",
                                f"{load.get(r['id'], 0)}/{r['capacity']}", r["status"], r["driver"]]
                               for r in allr], dense=True))
    return {"data": {"riders": riders, "seats": cap, "over_capacity": {r["id"]: load[r["id"]] - r["capacity"]
                                                                        for r in over},
                     "routes": [{"id": r["id"], "name": r["name"], "riders": load.get(r["id"], 0),
                                 "capacity": r["capacity"], "status": r["status"]} for r in allr]},
            "blocks": blocks,
            "trace": [("TransportAgent", "load_scan", f"{riders} riders on {cap} seats · "
                                                      f"{len(over)} route(s) over capacity")],
            "chips": ["Rebalance bus routes", "Bus on route 4 broke down", "Show route R03"]}


def t_rebalance_bus_routes(con, S, U, **kw):
    ta = TransportAgent()
    before = ta.load(con)
    moves, residual, after = ta.plan_rebalance(con)
    if not moves:
        return {"data": {"note": "No rider can be moved: every over-full route's stops are either "
                                 "unique to it or their other routes are full.", "residual": residual},
                "blocks": [B_text("No safe move exists — " + (
                    "over-capacity routes have no stop shared with a route that has seats. "
                    "Deploy a spare bus instead." if residual else "every route is within capacity."))],
                "trace": [("TransportAgent", "rebalance", "0 moves")]}
    routes = {r["id"]: r for r in ta.routes(con)}
    tbl = B_table(["Route", "Before", "After", "Seats"],
                  [[r, before.get(r, 0), after.get(r, 0), routes[r]["capacity"]] for r in sorted(routes)
                   if before.get(r, 0) != after.get(r, 0)], title="Load change")
    mv = B_table(["USN", "Stop", "From", "To", "New pickup"],
                 [[m["usn"], m["stop"], m["from"], m["to"], m["pickup"]] for m in moves],
                 title=f"{len(moves)} rider move(s) at shared stops", dense=True)
    note = (B_text("Still over capacity after the moves: " + ", ".join(f"**{r} +{n}**" for r, n in residual.items())
                   + " — riders at stops no other bus serves. A spare bus is the honest fix.")
            if residual else B_text("Every route ends within its seats."))
    tr = [("TransportAgent", "rebalance_plan", f"{len(moves)} moves · residual overflow "
                                               f"{sum(residual.values())}")]
    if not approved_this_turn(U):
        return _propose(S, "rebalance_bus_routes", {}, [tbl, mv, note],
                        f"move {len(moves)} riders between routes", tr)
    for m in moves:
        con.execute("UPDATE transport_riders SET route=? WHERE usn=? AND route=?", (m["to"], m["usn"], m["from"]))
    con.commit()
    now = ta.load(con)
    broke = [r for r in routes if before.get(r, 0) <= routes[r]["capacity"] < now.get(r, 0)]
    _notify(con, [{"audience": f"Student · {m['usn']}", "channel": "App push + SMS",
                   "title": f"Bus change from Monday: {m['to']}",
                   "body": f"You now board {m['to']} at {m['stop']}, pickup {m['pickup']}. Your pass is "
                           f"updated; no action needed."} for m in moves])
    Auditor().log(con, ACTOR, "TransportAgent", "transport.rebalance", {"moves": len(moves)},
                  "Applied" if not broke else "Applied with problems")
    _done(S, "rebalance_bus_routes")
    return {"data": {"moved": len(moves), "load_after": now, "residual": residual,
                     "verification": "clean" if not broke else {"newly_over": broke}},
            "blocks": [tbl, note, B_text(f"**{len(moves)} riders moved** and notified. Re-read after the "
                                         f"write: {len(broke)} route(s) pushed over capacity by the move.")],
            "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("TransportAgent", "verify", f"routes newly over capacity: {len(broke)}",
                            "error" if broke else "ok"),
                           ("NotifyAgent", "dispatch", f"{len(moves)} rider notices")]}


def t_handle_bus_breakdown(con, S, U, route="", **kw):
    ta = TransportAgent()
    rid = ta.resolve(con, route)
    if not rid:
        return _err(f"Which route? '{route}' does not name a route (R01–R08) or one of its stops.")
    r = one(con, "SELECT * FROM bus_routes WHERE id=?", (rid,))
    n = ta.load(con).get(rid, 0)
    spares = rows(con, "SELECT * FROM bus_routes WHERE status='Spare' ORDER BY capacity")
    fit = [s for s in spares if s["capacity"] >= n]
    spare = fit[0] if fit else (spares[-1] if spares else None)
    if not spare:
        return _err("No spare bus is available — every spare is already deployed.")
    overflow = max(0, n - spare["capacity"])
    moves = []
    if overflow:
        # The planner measures "over" against the route's listed seats, so feed it
        # a load inflated by the seats the smaller spare lacks: over == overflow.
        load = ta.load(con)
        load[rid] = n + (r["capacity"] - spare["capacity"])
        moves = ta.plan_rebalance(con, only=rid, load=load)[0][:overflow]
    tr = [("TransportAgent", "breakdown_plan",
           f"{rid}: {n} riders · spare {spare['bus']} ({spare['capacity']} seats) · overflow {overflow}")]
    tbl = B_table(["Field", "Value"],
                  [["Route", f"{rid} · {r['name']}"], ["Broken bus", r["bus"]],
                   ["Replacement", f"{spare['bus']} · {spare['capacity']} seats · driver {spare['driver']}"],
                   ["Riders", str(n)], ["Overflow moved to other routes", str(len(moves))],
                   ["Pickup times", "unchanged, expect ~15 min delay on the first run"]],
                  title="Breakdown cover")
    msg = {"audience": f"Transport riders · {rid} ({n} students)", "channel": "App push + SMS",
           "title": f"{rid}: replacement bus {spare['bus']} today",
           "body": f"{r['bus']} is off the road. {spare['bus']} runs {r['name']} on the usual stops; "
                   f"expect a delay of about 15 minutes on the first pickup."}
    if not approved_this_turn(U):
        return _propose(S, "handle_bus_breakdown", {"route": rid}, [tbl, B_notice([msg])],
                        f"deploy {spare['bus']} on {rid} and notify {n} riders", tr)
    con.execute("UPDATE bus_routes SET bus=?, capacity=?, driver=?, driver_phone=? WHERE id=?",
                (spare["bus"], spare["capacity"], spare["driver"], spare["driver_phone"], rid))
    con.execute("UPDATE bus_routes SET status=?, name=? WHERE id=?",
                (f"Deployed on {rid}", f"Covering {rid} ({r['bus']} under repair)", spare["id"]))
    for m in moves:
        con.execute("UPDATE transport_riders SET route=? WHERE usn=?", (m["to"], m["usn"]))
    con.commit()
    after = one(con, "SELECT * FROM bus_routes WHERE id=?", (rid,))
    seated = ta.load(con).get(rid, 0) <= after["capacity"]
    _notify(con, [msg, {"audience": f"Driver · {spare['driver']}", "channel": "SMS",
                        "title": f"Run {rid} today", "body": f"Take {spare['bus']} on {r['name']}, "
                                                             f"first pickup {r['departs']}."}])
    Auditor().log(con, ACTOR, "TransportAgent", "transport.breakdown", {"route": rid, "spare": spare["id"]},
                  "Applied" if seated else "Applied — still over capacity")
    _done(S, "handle_bus_breakdown")
    return {"data": {"route": rid, "replacement": spare["bus"], "riders_seated": seated,
                     "overflow_moved": len(moves)},
            "blocks": [tbl, B_notice([msg]), B_text(f"**{spare['bus']} is now running {rid}.** Riders and the "
                                                    f"driver are notified. Re-read: every rider has a seat — "
                                                    f"**{'yes' if seated else 'NO'}**.")],
            "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("TransportAgent", "verify", f"seated={seated}", "ok" if seated else "error"),
                           ("NotifyAgent", "dispatch", "riders + driver")]}


# ===================================================================== GATE PASS
CURFEW = dt.time(20, 0)
OUTING_CLASS_HOURS_BAR = 85        # attendance a student needs before a class-hour outing is even reviewable


class GatePassAgent:
    """Screens passes against written policy so the warden decides only the
    genuinely arguable ones. The policy is the table below, not a model."""
    name = "GatePassAgent"

    @staticmethod
    def class_window(sem, day):
        dy = day_of(day)
        if dy == "Sun":
            return None
        last = db.day_periods(sem, dy)
        end = dt.time.fromisoformat(db.PERIODS[last].split("-")[1])
        return dt.datetime.combine(day, dt.time(9, 0)), dt.datetime.combine(day, end)

    def class_days_missed(self, sem, out, ret):
        ret = max(ret, out.replace(hour=23, minute=59) if ret <= out else ret)
        n, d = [], out.date()
        while d <= ret.date():
            w = self.class_window(sem, d)
            if w and out < w[1] and ret > w[0]:
                n.append(d)
            d += dt.timedelta(days=1)
        return n

    def evaluate(self, con, gp):
        s = _stu(con, gp["usn"])
        if not s:
            return "reject", ["Student not on the rolls"]
        out, ret = dt.datetime.fromisoformat(gp["out_at"]), dt.datetime.fromisoformat(gp["return_by"])
        missed = self.class_days_missed(s["sem"], out, ret)
        att, kind = s["attendance"], gp["kind"]
        days = ", ".join(d.strftime("%a %d") for d in missed)
        if kind == "Emergency":
            return "approve", ["Emergency — approved on policy; parent and warden informed on exit"]
        if kind == "Medical":
            return "approve", ["Medical — approved on policy; hours are recorded as medical leave"]
        if not gp["parent_consent"]:
            return "review", ["No parent consent on record — the warden must call the parent first"]
        if kind == "Outing":
            if ret.date() > out.date() or ret.time() > CURFEW:
                return "reject", [f"Returns {ret.strftime('%H:%M')}, after the {CURFEW.strftime('%H:%M')} curfew"]
            if missed:
                if att < OUTING_CLASS_HOURS_BAR:
                    return "reject", [f"Overlaps class hours on {days}; attendance {att}% is below the "
                                      f"{OUTING_CLASS_HOURS_BAR}% bar for a class-hour outing"]
                return "review", [f"Overlaps class hours on {days}; attendance {att}% clears the bar — "
                                  f"mentor to decide"]
            return "approve", ["Outside class hours and back before curfew"]
        if kind == "Home visit":
            if not missed:
                return "approve", ["No class day missed"]
            if att < 75:
                return "reject", [f"Would miss {len(missed)} class day(s) ({days}) while attendance is "
                                  f"{att}% — below the VTU 75% bar"]
            if len(missed) > 2:
                return "review", [f"Misses {len(missed)} class days ({days}) — longer than the 2-day "
                                  f"limit the warden can clear alone"]
            return "approve", [f"Misses {len(missed)} class day(s) ({days}); attendance {att}% — mentor informed"]
        if kind == "Early leave":
            if missed and att < 75:
                return "reject", [f"Leaves during class hours with attendance {att}% (below 75%)"]
            return "review", ["Leaves during class hours — mentor to confirm with the parent"]
        return "review", [f"Unrecognised pass type '{kind}'"]

    def queue(self, con):
        out = []
        for g in rows(con, "SELECT * FROM gate_passes WHERE status='Pending' ORDER BY out_at"):
            v, why = self.evaluate(con, g)
            s = _stu(con, g["usn"]) or {}
            out.append({**g, "verdict": v, "why": why, "name": s.get("name"),
                        "cls": _cls(s) if s else "?", "att": s.get("attendance"),
                        "hostelite": bool(one(con, "SELECT 1 FROM hostel_allocations WHERE usn=?", (g["usn"],)))})
        return out


VERDICT_MD = {"approve": "**Approve**", "reject": "**Reject**", "review": "Review"}


def t_gate_pass_queue(con, S, U, **kw):
    q = GatePassAgent().queue(con)
    n = Counter(x["verdict"] for x in q)
    return {"data": {"pending": len(q), "recommend": dict(n),
                     "passes": [{"id": x["id"], "usn": x["usn"], "kind": x["kind"], "out": x["out_at"],
                                 "return_by": x["return_by"], "recommendation": x["verdict"],
                                 "reason": x["why"][0]} for x in q]},
            "blocks": [B_cards([{"label": "Pending passes", "value": len(q)},
                                {"label": "Policy-approvable", "value": n.get("approve", 0), "tone": "good"},
                                {"label": "Policy-rejectable", "value": n.get("reject", 0), "tone": "bad"},
                                {"label": "Needs a human", "value": n.get("review", 0), "tone": "warn"}],
                               title="Gate-pass queue — screened by GatePassAgent"),
                       B_table(["ID", "Student", "Class", "Type", "Out", "Back by", "Agent says", "Why"],
                               [[x["id"], f"{x['name']} ({x['usn']})", x["cls"], x["kind"], _when(x["out_at"]),
                                 _when(x["return_by"]), VERDICT_MD[x["verdict"]], x["why"][0]] for x in q],
                               dense=True)],
            "trace": [("GatePassAgent", "policy_screen",
                       f"{len(q)} passes · approve {n.get('approve', 0)} · reject {n.get('reject', 0)} · "
                       f"review {n.get('review', 0)}")],
            "chips": ["Decide pending gate passes by policy", "Hostel status"]}


def t_decide_gate_passes(con, S, U, ids=None, decision=None, **kw):
    ga = GatePassAgent()
    q = {x["id"]: x for x in ga.queue(con)}
    if isinstance(ids, (int, str)):
        ids = [ids]
    want = [int(re.sub(r"\D", "", str(i)) or 0) for i in (ids or [])]
    if want:
        missing = [i for i in want if i not in q]
        if missing:
            return _err(f"No PENDING gate pass with id {', '.join(map(str, missing))}.")
        dec = (decision or "").lower()
        if dec.startswith("rej"):
            plan = [(q[i], "reject", ["Rejected by the admin"]) for i in want]
        elif dec.startswith("app"):
            plan = [(q[i], "approve", ["Approved by the admin"] + (
                [f"agent had said {q[i]['verdict']}: {q[i]['why'][0]}"] if q[i]["verdict"] != "approve" else []))
                    for i in want]
        else:
            plan = [(q[i], q[i]["verdict"], q[i]["why"]) for i in want if q[i]["verdict"] != "review"]
    else:
        # "approve the compliant ones" approves only; "reject ..." rejects only;
        # no direction applies every verdict the policy is sure of.
        dec = (decision or "").lower()
        keep = {"approve"} if dec.startswith("app") else {"reject"} if dec.startswith("rej") \
            else {"approve", "reject"}
        plan = [(x, x["verdict"], x["why"]) for x in q.values() if x["verdict"] in keep]
    if not plan:
        return {"data": {"note": "Every pending pass needs a human decision; nothing is decidable by policy."},
                "blocks": [B_text("Nothing is decidable by policy — every pending pass needs the warden.")],
                "trace": []}
    tbl = B_table(["ID", "Student", "Type", "Out", "Decision", "Reason"],
                  [[x["id"], f"{x['name']} ({x['usn']})", x["kind"], _when(x["out_at"]),
                    VERDICT_MD[v], w[0]] for x, v, w in plan], title="Gate-pass decisions", dense=True)
    left = len(q) - len(plan)
    tr = [("GatePassAgent", "decide_plan", f"{len(plan)} decision(s) · {left} left for the warden")]
    args = {"ids": want or None, "decision": decision}
    if not approved_this_turn(U):
        return _propose(S, "decide_gate_passes", args, [tbl],
                        f"decide {len(plan)} gate pass(es)", tr)
    msgs = []
    for x, v, w in plan:
        st = "Approved" if v == "approve" else "Rejected"
        con.execute("UPDATE gate_passes SET status=?, decided_by=?, note=? WHERE id=? AND status='Pending'",
                    (st, "Registrar (agent-screened)", "; ".join(w), x["id"]))
        msgs.append({"audience": f"Student · {x['usn']} & parent" + (" & warden" if x["hostelite"] else ""),
                     "channel": "App push + SMS", "title": f"Gate pass #{x['id']} {st.lower()}",
                     "body": f"{x['kind']} {_when(x['out_at'])} → {_when(x['return_by'])}. {w[0]}."})
    con.commit()
    done = one(con, f"SELECT COUNT(*) n FROM gate_passes WHERE id IN ({','.join('?' * len(plan))}) "
                    f"AND status<>'Pending'", tuple(x["id"] for x, _v, _w in plan))["n"]
    _notify(con, msgs)
    Auditor().log(con, ACTOR, "GatePassAgent", "gatepass.decide", {"n": len(plan)},
                  "Applied" if done == len(plan) else "Partial")
    _done(S, "decide_gate_passes")
    return {"data": {"decided": done, "left_for_warden": left},
            "blocks": [tbl, B_text(f"**{done} pass(es) decided** and students, parents and wardens "
                                   f"notified. {left} left for a human.")], "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("GatePassAgent", "verify", f"{done}/{len(plan)} no longer pending",
                            "ok" if done == len(plan) else "error"),
                           ("NotifyAgent", "dispatch", f"{len(msgs)} notices")]}


# ===================================================================== PLACEMENT
class PlacementAgent:
    name = "PlacementAgent"
    DREAM_MULTIPLE = 1.5          # a placed student may sit only drives paying 1.5x their best offer

    def best_offer(self, con):
        return {r["usn"]: r["c"] for r in rows(con, "SELECT usn, MAX(ctc) c FROM student_offers GROUP BY usn")}

    def drives(self, con, status="Upcoming"):
        return rows(con, "SELECT * FROM placement_drives WHERE status=? ORDER BY drive_date", (status,))

    def resolve(self, con, ref):
        t = str(ref or "").lower()
        ds = rows(con, "SELECT * FROM placement_drives ORDER BY id")
        m = re.fullmatch(r"\s*#?(\d+)\s*", t)
        if m:
            return next((d for d in ds if d["id"] == int(m.group(1))), None)
        for d in ds:
            if d["company"].lower() in t:
                return d
        for d in ds:
            if any(w in t for w in re.findall(r"[a-z]{5,}", d["company"].lower())):
                return d
        return None

    def eligibility(self, con, d):
        best = self.best_offer(con)
        depts = d["depts"].split(",")
        ok, why = [], Counter()
        for s in rows(con, "SELECT * FROM students WHERE sem=7 ORDER BY cgpa DESC, usn"):
            if s["dept"] not in depts:
                continue
            r = []
            if s["cgpa"] < d["min_cgpa"]:
                r.append("cgpa")
            if s["backlogs"] > d["max_backlogs"]:
                r.append("backlogs")
            b = best.get(s["usn"])
            if b and not (d["dream"] and d["ctc"] >= self.DREAM_MULTIPLE * b):
                r.append("already placed")
            if r:
                for x in r:
                    why[x] += 1
            else:
                ok.append({**s, "best_offer": b})
        return ok, why


def t_placement_overview(con, S, U, **kw):
    pa = PlacementAgent()
    best = pa.best_offer(con)
    byd = []
    for d in db.DEPTS:
        fin = rows(con, "SELECT usn FROM students WHERE sem=7 AND dept=?", (d,))
        placed = [best[s["usn"]] for s in fin if s["usn"] in best]
        byd.append({"dept": d, "final_year": len(fin), "placed": len(placed),
                    "pct": round(100 * len(placed) / max(1, len(fin)), 1),
                    "avg": round(sum(placed) / len(placed), 2) if placed else 0})
    fin_n = sum(x["final_year"] for x in byd)
    pl_n = sum(x["placed"] for x in byd)
    ctcs = sorted(best.values())
    up = []
    for d in pa.drives(con):
        ok, _w = pa.eligibility(con, d)
        reg = one(con, "SELECT COUNT(*) n FROM drive_registrations WHERE drive_id=?", (d["id"],))["n"]
        up.append({**d, "eligible": len(ok), "registered": reg})
    return {"data": {"final_year": fin_n, "placed": pl_n, "placement_pct": round(100 * pl_n / max(1, fin_n), 1),
                     "highest_lpa": max(ctcs) if ctcs else 0,
                     "median_lpa": ctcs[len(ctcs) // 2] if ctcs else 0, "by_dept": byd,
                     "upcoming": [{"id": d["id"], "company": d["company"], "date": d["drive_date"], "ctc": d["ctc"],
                                   "eligible": d["eligible"], "shortlist_published": d["registered"] > 0}
                                  for d in up]},
            "blocks": [B_cards([{"label": "Final-year students", "value": fin_n},
                                {"label": "Placed", "value": pl_n, "sub": f"{round(100 * pl_n / max(1, fin_n))}%",
                                 "tone": "good"},
                                {"label": "Highest CTC", "value": f"₹{max(ctcs) if ctcs else 0} LPA"},
                                {"label": "Median CTC", "value": f"₹{ctcs[len(ctcs) // 2] if ctcs else 0} LPA"},
                                {"label": "Upcoming drives", "value": len(up)}], title="Training & Placement"),
                       B_bars([{"label": x["dept"], "value": x["placed"], "max": x["final_year"]} for x in byd],
                              title="Placed / final-year strength", unit="students"),
                       B_table(["#", "Company", "Role", "CTC", "Date", "Branches", "Cut-off", "Eligible", "Shortlist"],
                               [[d["id"], d["company"], d["role"], f"₹{d['ctc']} LPA", d["drive_date"],
                                 d["depts"], f"{d['min_cgpa']} CGPA · ≤{d['max_backlogs']} backlog"
                                 + (" · dream" if d["dream"] else ""), d["eligible"],
                                 f"{d['registered']} published" if d["registered"] else "not yet"] for d in up],
                               title="Upcoming drives", dense=True)],
            "trace": [("PlacementAgent", "stats", f"{pl_n}/{fin_n} placed · {len(up)} upcoming drives")],
            "chips": [f"Who is eligible for the {up[0]['company']} drive?" if up else "Placement status",
                      f"Prepare the shortlist for the {up[0]['company']} drive" if up else "Placement status"]}


def t_drive_eligibility(con, S, U, drive="", **kw):
    pa = PlacementAgent()
    d = pa.resolve(con, drive)
    if not d:
        return _err(f"No placement drive matches '{drive}'.",
                    drives=[x["company"] for x in pa.drives(con)])
    ok, why = pa.eligibility(con, d)
    return {"data": {"drive": d["company"], "date": d["drive_date"], "eligible": len(ok),
                     "excluded": dict(why), "policy": f"one offer per student unless the drive pays "
                                                      f"≥{pa.DREAM_MULTIPLE}x their best offer (dream)",
                     "top": [{"usn": s["usn"], "name": s["name"], "cgpa": s["cgpa"]} for s in ok[:15]]},
            "blocks": [B_cards([{"label": "Eligible", "value": len(ok), "tone": "good"},
                                {"label": "Below CGPA cut-off", "value": why.get("cgpa", 0)},
                                {"label": "Backlogs", "value": why.get("backlogs", 0)},
                                {"label": "Held by one-offer rule", "value": why.get("already placed", 0)}],
                               title=f"{d['company']} · {d['role']} · ₹{d['ctc']} LPA · {d['drive_date']}"),
                       B_table(["USN", "Name", "Class", "CGPA", "Backlogs", "Current offer"],
                               [[s["usn"], s["name"], _cls(s), s["cgpa"], s["backlogs"],
                                 f"₹{s['best_offer']} LPA" if s["best_offer"] else "—"] for s in ok[:40]],
                               title="Eligible students", dense=True)],
            "trace": [("PlacementAgent", "eligibility",
                       f"{d['company']}: {len(ok)} eligible · excluded {dict(why)}")],
            "chips": [f"Prepare the shortlist for the {d['company']} drive", "Placement status"]}


def t_publish_drive_shortlist(con, S, U, drive="", **kw):
    pa = PlacementAgent()
    d = pa.resolve(con, drive)
    if not d:
        return _err(f"No placement drive matches '{drive}'.")
    ok, _w = pa.eligibility(con, d)
    have = {r["usn"] for r in rows(con, "SELECT usn FROM drive_registrations WHERE drive_id=?", (d["id"],))}
    new = [s for s in ok if s["usn"] not in have]
    if not new:
        return {"data": {"note": f"The {d['company']} shortlist is already published ({len(have)} students)."},
                "blocks": [B_text(f"The {d['company']} shortlist is already out — {len(have)} students.")],
                "trace": []}
    by = Counter(s["dept"] for s in new)
    msg = {"audience": f"Eligible final-year students · {d['company']} ({len(new)})",
           "channel": "App push + email",
           "title": f"Shortlisted: {d['company']} — {d['role']} (₹{d['ctc']} LPA)",
           "body": f"You meet the criteria for the {d['company']} drive on {d['drive_date']}. Report to the "
                   f"Placement Cell by 9:00 with your resume and ID card."}
    tr = [("PlacementAgent", "shortlist", f"{len(new)} eligible · " + ", ".join(f"{k} {v}" for k, v in by.items()))]
    if not approved_this_turn(U):
        return _propose(S, "publish_drive_shortlist", {"drive": d["company"]},
                        [B_table(["Branch", "Students"], [[k, v] for k, v in sorted(by.items())],
                                 title=f"{d['company']} shortlist — {len(new)} students"), B_notice([msg])],
                        f"shortlist {len(new)} students for {d['company']} and notify them", tr)
    con.executemany("INSERT OR IGNORE INTO drive_registrations VALUES(?,?,?,?)",
                    [(d["id"], s["usn"], "Shortlisted", NOW.isoformat(timespec="minutes")) for s in new])
    con.commit()
    n = one(con, "SELECT COUNT(*) n FROM drive_registrations WHERE drive_id=?", (d["id"],))["n"]
    _notify(con, [msg])
    Auditor().log(con, ACTOR, "PlacementAgent", "placement.shortlist", {"drive": d["id"], "n": len(new)}, "Applied")
    _done(S, "publish_drive_shortlist")
    return {"data": {"drive": d["company"], "shortlisted": n},
            "blocks": [B_text(f"**{n} students shortlisted** for {d['company']} and notified.")],
            "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("PlacementAgent", "verify", f"{n} registrations on re-read",
                            "ok" if n >= len(new) else "error"),
                           ("NotifyAgent", "dispatch", msg["audience"])]}


# ===================================================================== DOCUMENTS
CERT_KINDS = {  # canonical kind: (serial code, request-inbox kind it can fulfil)
    "Bonafide": ("BON", "Bonafide"), "Conduct": ("CON", "Conduct"),
    "Fee Structure": ("FEE", "Fee Structure"), "Transfer Certificate": ("TC", "Transfer Certificate"),
    "No Dues": ("NDC", "No Dues"), "Internship NOC": ("NOC", "Internship")}
CERT_ALIASES = [(r"bona ?fide", "Bonafide"), (r"conduct|character", "Conduct"),
                (r"fee structure|fee certificate|fee letter|loan", "Fee Structure"),
                (r"transfer certificate|\btc\b|leaving certificate", "Transfer Certificate"),
                (r"no[- ]?dues?", "No Dues"), (r"\bnoc\b|no objection|internship", "Internship NOC")]
# Synthetic, and printed as such. Tuition etc. per academic year.
FEE_HEADS = [("Tuition fee", 108000), ("University & examination fee", 12000),
             ("Development & library fee", 20000)]
SEM_WORD = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
            8: "eighth"}


def cert_phrase(req_kind):
    """'Internship' -> 'an internship NOC', 'Conduct' -> 'a conduct certificate' - the sentence a
    radar button sends, which must route back to the DocumentAgent and carry no approval word."""
    if req_kind == "Internship":
        return "an internship NOC"
    noun = req_kind.lower() if req_kind.endswith("Certificate") else f"{req_kind.lower()} certificate"
    return ("an " if noun[0] in "aeiou" else "a ") + noun


def cert_kind(text):
    for rx, k in CERT_ALIASES:
        if re.search(rx, str(text or ""), re.I):
            return k
    return None


class DocumentAgent:
    name = "DocumentAgent"

    def no_dues(self, con, usn):
        """One row per ledger, each owned by the agent that keeps it."""
        s = _stu(con, usn)
        la = LibraryAgent()
        loans = la.active(con, usn)
        fines = sum(la.fine(l) for l in loans)
        h = one(con, "SELECT * FROM hostel_allocations WHERE usn=?", (usn,))
        t = one(con, "SELECT * FROM transport_riders WHERE usn=?", (usn,))
        return [
            {"agent": "FinanceAgent", "label": "Accounts — semester fee", "ok": not s["fee_due"],
             "detail": inr(s["fee_due"]) + " outstanding" if s["fee_due"] else "paid"},
            {"agent": "LibraryAgent", "label": "Library — books & fines", "ok": not loans,
             "detail": (f"{len(loans)} book(s) on loan, fine {inr(fines)}" if loans else "nothing on loan")},
            {"agent": "HostelAgent", "label": "Hostel", "ok": not (h and h["fee_due"]),
             "detail": ("not a resident" if not h else inr(h["fee_due"]) + " due" if h["fee_due"] else f"{h['room']} · paid")},
            {"agent": "TransportAgent", "label": "Transport", "ok": not (t and t["fee_due"]),
             "detail": ("not a rider" if not t else inr(t["fee_due"]) + " due" if t["fee_due"] else f"{t['route']} · paid")},
        ]

    def eligibility(self, con, s, kind):
        checks = [{"agent": "StudentAgent", "label": "On the rolls", "ok": True, "detail": _cls(s)}]
        if kind in ("Transfer Certificate", "No Dues"):
            checks += self.no_dues(con, s["usn"])
        if kind == "Internship NOC":
            checks += [{"agent": "StudentAgent", "label": "Attendance ≥ 75%", "ok": s["attendance"] >= 75,
                        "detail": f"{s['attendance']}%"},
                       {"agent": "ExamAgent", "label": "No backlogs", "ok": s["backlogs"] == 0,
                        "detail": f"{s['backlogs']} backlog(s)"}]
        return checks

    def next_serial(self, con, code, year):
        n = one(con, "SELECT COUNT(*) n FROM certificates WHERE serial LIKE ?", (f"VIE/{code}/{year}/%",))["n"]
        return f"VIE/{code}/{year}/{n + 1:04d}"

    def body(self, con, s, kind, purpose):
        dept = db.DEPTS.get(s["dept"], s["dept"])
        who = f"**{s['name']}** (USN **{s['usn']}**)"
        prog = (f"in the {SEM_WORD.get(s['sem'], s['sem'])} semester of the Bachelor of Engineering "
                f"programme in {dept}")
        why = f" This certificate is issued at the student's request for the purpose of {purpose}." if purpose else ""
        if kind == "Bonafide":
            return f"This is to certify that {who} is a bonafide student of this Institute, studying {prog} during the academic year 2026–27.{why}"
        if kind == "Conduct":
            return f"This is to certify that {who}, studying {prog}, has borne good conduct and character during the period of study at this Institute, to the best of the Institute's records.{why}"
        if kind == "Fee Structure":
            heads = list(FEE_HEADS)
            if one(con, "SELECT 1 FROM hostel_allocations WHERE usn=?", (s["usn"],)):
                heads.append(("Hostel & mess (annual)", 96000))
            if one(con, "SELECT 1 FROM transport_riders WHERE usn=?", (s["usn"],)):
                heads.append(("College transport (annual)", 18000))
            lines = "\n".join(f"• {h}: {inr(a)}" for h, a in heads)
            return (f"This is to certify that {who} is studying {prog}. The fees payable for the academic year "
                    f"2026–27 are:\n{lines}\n• **Total: {inr(sum(a for _h, a in heads))}**{why}")
        if kind == "Transfer Certificate":
            return (f"This is to certify that {who} was a student of this Institute, {prog}. The student has no "
                    f"dues outstanding with Accounts, Library, Hostel or Transport, and the Institute has no "
                    f"objection to the student's transfer.{why}")
        if kind == "No Dues":
            return (f"This is to certify that {who}, studying {prog}, has no dues outstanding with Accounts, "
                    f"the Central Library, the Hostels or College Transport as on {TODAY.strftime('%d %B %Y')}.{why}")
        if kind == "Internship NOC":
            return (f"The Institute has no objection to {who}, studying {prog}, undertaking an internship"
                    f"{' — ' + purpose if purpose else ''}, provided it does not conflict with the student's "
                    f"examinations. The student's attendance stands at {s['attendance']}% with no backlogs.")
        return ""


def t_no_dues_status(con, S, U, usn="", **kw):
    s = _stu(con, _usn(usn) or usn)
    if not s:
        return _err(f"No student with USN {usn}.")
    items = DocumentAgent().no_dues(con, s["usn"])
    clear = all(i["ok"] for i in items)
    return {"data": {"usn": s["usn"], "name": s["name"], "clear": clear,
                     "ledgers": [{"ledger": i["label"], "clear": i["ok"], "detail": i["detail"]} for i in items]},
            "blocks": [B_checklist(items, title=f"No-dues clearance — {s['name']} ({s['usn']}) · "
                                                f"{'CLEAR' if clear else 'BLOCKED'}")],
            "trace": [("DocumentAgent", "fan_out", "asking 4 ledger owners")]
                     + [(i["agent"], "ledger_check", f"{i['label']}: {'clear' if i['ok'] else i['detail']}")
                        for i in items],
            "chips": ([f"Issue a no dues certificate for {s['usn']}"] if clear
                      else [f"Everything about {s['usn']}"]) + [f"Issue a bonafide certificate for {s['usn']}"]}


def t_issue_certificate(con, S, U, usn="", kind="Bonafide", purpose="", **kw):
    s = _stu(con, _usn(usn) or usn)
    if not s:
        return _err(f"No student with USN {usn}.")
    k = kind if kind in CERT_KINDS else cert_kind(kind)
    if not k:
        return _err(f"Unknown certificate '{kind}'. I can issue: {', '.join(CERT_KINDS)}.")
    da = DocumentAgent()
    checks = da.eligibility(con, s, k)
    tr = [("DocumentAgent", "eligibility", f"{k}: " + " · ".join(
        f"{c['label']}={'ok' if c['ok'] else 'FAIL'}" for c in checks))]
    if not all(c["ok"] for c in checks):
        return {"data": {"error": f"{k} refused — " + "; ".join(f"{c['label']}: {c['detail']}"
                                                                 for c in checks if not c["ok"]),
                         "checks": checks},
                "blocks": [B_checklist(checks, title=f"{k} for {s['name']} — cannot be issued yet")],
                "trace": tr, "chips": [f"No dues status of {s['usn']}"]}
    code, req_kind = CERT_KINDS[k]
    serial = da.next_serial(con, code, TODAY.year)
    body = da.body(con, s, k, (purpose or "").strip())
    req = one(con, "SELECT * FROM requests WHERE raised_by=? AND kind=? AND status='Pending'", (s["usn"], req_kind))
    doc = {"serial": serial, "kind": k, "title": f"{k} Certificate" if k not in ("Transfer Certificate",
                                                                                   "Internship NOC") else k,
           "institute": INSTITUTE, "date": TODAY.strftime("%d %B %Y"), "body": body,
           "to": f"{s['name']} · {s['usn']}", "signatory": "Registrar"}
    args = {"usn": s["usn"], "kind": k, "purpose": purpose or None}
    if not approved_this_turn(U):
        return _propose(S, "issue_certificate", args,
                        [B_checklist(checks, title="Eligibility"), B_document({**doc, "status": "draft"})]
                        + ([B_text(f"This also closes **REQ-{req['id']:04d}** — “{req['title']}”.")] if req else []),
                        f"issue {k} {serial} to {s['usn']}", tr)
    con.execute("""INSERT INTO certificates(serial,usn,kind,purpose,issued_on,issued_by,body,request_id)
                   VALUES(?,?,?,?,?,?,?,?)""", (serial, s["usn"], k, purpose or "", TODAY.isoformat(),
                                                "Registrar", body, req["id"] if req else None))
    if req:
        con.execute("UPDATE requests SET status='Approved' WHERE id=?", (req["id"],))
    con.commit()
    ok = one(con, "SELECT 1 FROM certificates WHERE serial=?", (serial,))
    _notify(con, [{"audience": f"Student · {s['usn']}", "channel": "App + email",
                   "title": f"{k} certificate ready — {serial}",
                   "body": "Collect the signed copy from the Registrar's office; a digital copy is in your app."}])
    Auditor().log(con, ACTOR, "DocumentAgent", "certificate.issue", {"serial": serial, "usn": s["usn"], "kind": k},
                  "Applied" if ok else "FAILED")
    _done(S, "issue_certificate")
    return {"data": {"issued": bool(ok), "serial": serial, "request_closed": req["id"] if req else None},
            "blocks": [B_document({**doc, "status": "issued"}),
                       B_text(f"**{serial}** entered in the certificate register"
                              + (f" and **REQ-{req['id']:04d}** closed." if req else "."))],
            "refresh": True,
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("DocumentAgent", "register", serial),
                           ("DocumentAgent", "verify", "serial present on re-read" if ok else "MISSING", "ok" if ok else "error"),
                           ("NotifyAgent", "dispatch", s["usn"])]}


def t_certificate_register(con, S, U, usn=None, **kw):
    u = _usn(usn) if usn else None
    c = rows(con, "SELECT c.*, s.name FROM certificates c LEFT JOIN students s ON s.usn=c.usn"
             + (" WHERE c.usn=?" if u else "") + " ORDER BY c.id DESC LIMIT 40", (u,) if u else ())
    kinds = tuple(v[1] for v in CERT_KINDS.values())
    pend = rows(con, f"SELECT * FROM requests WHERE status='Pending' AND kind IN ({','.join('?' * len(kinds))})", kinds)
    return {"data": {"issued": len(c), "pending_requests": [{"id": r["id"], "kind": r["kind"],
                                                             "from": r["raised_by"], "title": r["title"]} for r in pend]},
            "blocks": [B_table(["Serial", "Kind", "Student", "Purpose", "Issued"],
                               [[x["serial"], x["kind"], f"{x['name']} ({x['usn']})", x["purpose"] or "—",
                                 x["issued_on"]] for x in c], title="Certificate register", dense=True)]
                      + ([B_table(["Request", "Kind", "From", "Title"],
                                  [[f"REQ-{r['id']:04d}", r["kind"], r["raised_by"], r["title"]] for r in pend],
                                  title="Certificate requests waiting in the inbox")] if pend else []),
            "trace": [("DocumentAgent", "register_read", f"{len(c)} issued · {len(pend)} requested")],
            "chips": [f"Issue {cert_phrase(r['kind'])} for {r['raised_by']}" for r in pend if _usn(r["raised_by"])][:3]}


# ======================================================================== LEAVES
class LeaveAgent:
    name = "HRAgent"

    def working_days(self, a, b):
        out, d = [], a
        while d <= b:
            if day_of(d) != "Sun":
                out.append(d)
            d += dt.timedelta(days=1)
        return out

    def assess(self, con, lv):
        """Price a leave in what it would actually cost the timetable - by
        running the substitution planner for every working day it spans."""
        f = one(con, "SELECT * FROM faculty WHERE id=?", (lv["faculty"],))

        class _T:
            def add(self, *a, **k): pass
        days, worst, periods, best_codes = [], 100, 0, set()
        sub = SubstitutionAgent()
        for d in self.working_days(dt.date.fromisoformat(lv["from_date"]), dt.date.fromisoformat(lv["to_date"])):
            plans, affected = sub.build_plans(con, f, d, _T())
            n = sum(len(a["periods"]) for a in affected)
            periods += n
            if plans:
                cov = plans[0]["coverage"]
                worst = min(worst, cov)
                best_codes.add(plans[0]["code"])
                days.append({"date": d.isoformat(), "periods": n, "best_plan": plans[0]["code"], "coverage": cov})
            else:
                days.append({"date": d.isoformat(), "periods": 0, "best_plan": None, "coverage": 100})
        if periods == 0:
            rec, why = "approve", "no classes on those days — zero academic impact"
        elif worst == 100:
            rec, why = "approve", (f"{periods} period(s), every day fully coverable "
                                   f"(best plan {'/'.join(sorted(best_codes))})")
        else:
            rec, why = "review", f"{periods} period(s); the best plan covers only {worst}% on the worst day"
        retro = lv["from_date"] < TODAY.isoformat()
        return {"faculty": f, "days": days, "periods": periods, "worst": worst, "rec": rec,
                "why": why + (" · applied retroactively" if retro else "")}

    def pending(self, con, dept=None):
        if dept:                           # an HOD sees their own department's queue (#27)
            return rows(con, """SELECT l.* FROM leaves l JOIN faculty f ON f.id=l.faculty
                                  WHERE l.status='Pending' AND f.dept=? ORDER BY l.from_date""", (dept,))
        return rows(con, "SELECT * FROM leaves WHERE status='Pending' ORDER BY from_date")


def t_review_pending_leaves(con, S, U, dept=None, **kw):
    la = LeaveAgent()
    out = []
    for lv in la.pending(con, dept):
        a = la.assess(con, lv)
        out.append((lv, a))
    return {"data": {"pending": len(out),
                     "leaves": [{"id": lv["id"], "faculty": a["faculty"]["name"], "from": lv["from_date"],
                                 "to": lv["to_date"], "type": lv["kind"], "periods_affected": a["periods"],
                                 "worst_day_coverage": a["worst"], "recommendation": a["rec"],
                                 "why": a["why"]} for lv, a in out]},
            "blocks": [B_table(["ID", "Faculty", "Dates", "Type", "Periods", "Worst-day cover", "Agent says", "Why"],
                               [[lv["id"], a["faculty"]["name"], f"{lv['from_date']} → {lv['to_date']}", lv["kind"],
                                 a["periods"], f"{a['worst']}%", VERDICT_MD[a["rec"]], a["why"]]
                                for lv, a in out], title="Pending faculty leave — priced against the timetable",
                               dense=True)] if out else [B_text("No pending leave applications.")],
            "trace": [("HRAgent", "leave_queue", f"{len(out)} pending")]
                     + [("SubstitutionAgent", "what_if", f"{a['faculty']['name']}: {a['periods']} period(s), "
                                                         f"worst-day coverage {a['worst']}%") for _lv, a in out],
            "chips": ["Approve all pending leaves with coverage", "Who is on leave this week?"]}


def t_decide_leave(con, S, U, leave_id=None, decision="approve", dept=None, **kw):
    la = LeaveAgent()
    dec = "Rejected" if str(decision or "").lower().startswith("rej") else "Approved"
    if leave_id not in (None, ""):
        lid = int(re.sub(r"\D", "", str(leave_id)) or 0)
        lv = one(con, "SELECT * FROM leaves WHERE id=? AND status='Pending'", (lid,))
        if not lv:
            return _err(f"No pending leave with id {lid}.")
        todo = [(lv, la.assess(con, lv))]
    else:
        if dec == "Rejected":
            return _err("Name the leave to reject (by id) — I do not bulk-reject.")
        todo = [(lv, a) for lv in la.pending(con, dept) for a in [la.assess(con, lv)] if a["rec"] == "approve"]
        if not todo:
            return _err("No pending leave is fully coverable — review them one by one.")
    tbl = B_table(["ID", "Faculty", "Dates", "Periods", "Decision", "Why"],
                  [[lv["id"], a["faculty"]["name"], f"{lv['from_date']} → {lv['to_date']}", a["periods"], dec,
                    a["why"]] for lv, a in todo], title="Leave decisions")
    tr = [("HRAgent", "decide_plan", f"{len(todo)} leave(s) → {dec}")]
    args = {"leave_id": leave_id, "decision": decision}
    if not approved_this_turn(U):
        return _propose(S, "decide_leave", args, [tbl], f"mark {len(todo)} leave(s) {dec.lower()}", tr)
    for lv, _a in todo:
        con.execute("UPDATE leaves SET status=? WHERE id=? AND status='Pending'", (dec, lv["id"]))
    con.commit()
    done = sum(1 for lv, _a in todo if one(con, "SELECT status FROM leaves WHERE id=?", (lv["id"],))["status"] == dec)
    _notify(con, [{"audience": f"Faculty · {a['faculty']['name']}", "channel": "Email + app",
                   "title": f"Leave {lv['from_date']} → {lv['to_date']} {dec.lower()}",
                   "body": f"{lv['kind']}. " + ("Class adjustments will be circulated before the day."
                                               if dec == "Approved" and a["periods"] else "")} for lv, a in todo])
    Auditor().log(con, ACTOR, "HRAgent", "leave.decide", {"ids": [lv["id"] for lv, _a in todo], "decision": dec},
                  "Applied" if done == len(todo) else "Partial")
    _done(S, "decide_leave")
    follow = [(a["faculty"], next(d for d in a["days"] if d["periods"])) for lv, a in todo
              if dec == "Approved" and a["periods"]]
    return {"data": {"decided": done, "decision": dec,
                     "needs_coverage": [{"faculty": f["name"], "date": d["date"]} for f, d in follow]},
            "blocks": [tbl, B_text(f"**{done} leave(s) {dec.lower()}** and staff notified."
                                   + (f" {len(follow)} of them have classes to cover — plan each one next."
                                      if follow else ""))],
            "refresh": True,
            "chips": [f"Arrange coverage for {f['name']} on {_date_words(dt.date.fromisoformat(d['date']))}"
                      for f, d in follow][:3],
            "trace": tr + [("PolicyGuard", "write_authorisation", "admin approved in this turn"),
                           ("HRAgent", "verify", f"{done}/{len(todo)} status re-read",
                            "ok" if done == len(todo) else "error")]}


# =================================================================== STUDENT 360
def t_student_360(con, S, U, usn="", **kw):
    s = _stu(con, _usn(usn) or usn)
    if not s:
        return _err(f"No student with USN {usn}.")
    att = rows(con, """SELECT a.*, sb.name sname FROM attendance a JOIN subjects sb ON sb.code=a.subject
                       WHERE a.usn=? ORDER BY a.subject""", (s["usn"],))
    la = LibraryAgent()
    loans = la.active(con, s["usn"])
    h = one(con, "SELECT a.*, r.block FROM hostel_allocations a JOIN hostel_rooms r ON r.id=a.room WHERE a.usn=?",
            (s["usn"],))
    t = one(con, "SELECT r.*, b.name rname, b.bus FROM transport_riders r JOIN bus_routes b ON b.id=r.route "
                 "WHERE r.usn=?", (s["usn"],))
    offers = rows(con, "SELECT * FROM student_offers WHERE usn=? ORDER BY ctc DESC", (s["usn"],))
    passes = rows(con, "SELECT * FROM gate_passes WHERE usn=? ORDER BY id DESC LIMIT 3", (s["usn"],))
    certs = rows(con, "SELECT * FROM certificates WHERE usn=? ORDER BY id DESC", (s["usn"],))
    nd = DocumentAgent().no_dues(con, s["usn"])
    short = [a for a in att if a["held"] and 100 * a["attended"] / a["held"] < 75]
    life = [f"**Mentor** {fac_name(con, s['mentor'])}",
            f"**Hostel** {h['room']}, {h['block']}" if h else ("**Hostel** waitlisted" if s["hostel"] else "**Day scholar**"),
            f"**Bus** {t['route']} {t['rname']} from {t['stop']}" if t else None,
            f"**Library** {len(loans)} book(s) on loan" + (f", {sum(1 for l in loans if la.days_over(l))} overdue"
                                                           if loans else ""),
            f"**Offers** " + ", ".join(f"{o['company']} ₹{o['ctc']} LPA" for o in offers) if offers else None,
            f"**Certificates** " + ", ".join(c["serial"] for c in certs) if certs else None]
    return {"data": {"usn": s["usn"], "name": s["name"], "class": _cls(s), "cgpa": s["cgpa"],
                     "attendance": s["attendance"], "backlogs": s["backlogs"], "fee_due": s["fee_due"],
                     "subjects_below_75": [a["subject"] for a in short],
                     "hostel": h["room"] if h else None, "bus": t["route"] if t else None,
                     "books_on_loan": len(loans), "offers": [o["company"] for o in offers],
                     "no_dues_clear": all(i["ok"] for i in nd)},
            "blocks": [B_cards([{"label": "CGPA", "value": s["cgpa"], "tone": "good" if s["cgpa"] >= 7 else "warn"},
                                {"label": "Attendance", "value": f"{s['attendance']}%",
                                 "tone": "good" if s["attendance"] >= 75 else "bad",
                                 "sub": f"{len(short)} subject(s) below 75%"},
                                {"label": "Backlogs", "value": s["backlogs"], "tone": "bad" if s["backlogs"] else "good"},
                                {"label": "Fee due", "value": inr(s["fee_due"]), "tone": "bad" if s["fee_due"] else "good"}],
                               title=f"{s['name']} · {s['usn']} · {_cls(s)}"),
                       B_text(" · ".join(x for x in life if x)),
                       B_bars([{"label": f"{a['subject']} {a['sname']}", "value": a["attended"], "max": a["held"],
                                "pct": round(100 * a["attended"] / a["held"], 1) if a["held"] else 0}
                               for a in att], title="Subject-wise attendance (hours attended / held)", unit="hrs"),
                       B_checklist(nd, title="No-dues position")]
                      + ([B_table(["Pass", "Type", "Out", "Status"], [[p["id"], p["kind"], _when(p["out_at"]),
                                                                       p["status"]] for p in passes],
                                  title="Recent gate passes", dense=True)] if passes else []),
            "trace": [("StudentAgent", "record_fetch", s["usn"]),
                      ("AttendanceAgent", "subject_rollup", f"{len(att)} subjects · {len(short)} below 75%"),
                      ("LibraryAgent", "member_loans", f"{len(loans)} on loan"),
                      ("HostelAgent", "allocation", h["room"] if h else "none"),
                      ("TransportAgent", "pass", t["route"] if t else "none"),
                      ("PlacementAgent", "offers", str(len(offers))),
                      ("DocumentAgent", "no_dues", "clear" if all(i["ok"] for i in nd) else "blocked")],
            "chips": [f"No dues status of {s['usn']}", f"Issue a bonafide certificate for {s['usn']}",
                      "Attendance defaulters in " + s["dept"]]}


# ===================================================================== OPS RADAR
def t_ops_radar(con, S, U, **kw):
    """Every agent is asked the same question - what needs a human today? -
    and the answers come back as one ranked queue, each item carrying the exact
    sentence that sets its agent to work. Reads only; each command it offers
    PROPOSES first, so clicking one never writes anything by itself."""
    items, tr = [], []

    def add(sev, domain, agent, title, detail, command, records):
        items.append({"severity": sev, "domain": domain, "agent": agent, "title": title,
                      "detail": detail, "command": command, "records": records})

    reqs = rows(con, "SELECT * FROM requests WHERE status='Pending'")
    late = [r for r in reqs if (TODAY - dt.date.fromisoformat(r["created_at"])).days * 24 > r["sla_hrs"]]
    if late:
        add("high", "Approvals", "RequestAgent", f"{len(late)} request(s) past their SLA",
            ", ".join(f"REQ-{r['id']:04d} {r['kind']}" for r in late[:4]), "Pending approvals in my inbox", len(late))
    tr.append(("RequestAgent", "sla_scan", f"{len(late)}/{len(reqs)} past SLA"))

    lvs = rows(con, "SELECT l.*, f.name FROM leaves l JOIN faculty f ON f.id=l.faculty WHERE l.status='Pending'")
    if lvs:
        add("high" if any(l["from_date"] <= (TODAY + dt.timedelta(days=3)).isoformat() for l in lvs) else "medium",
            "Faculty leave", "HRAgent", f"{len(lvs)} faculty leave application(s) undecided",
            ", ".join(f"{l['name']} {l['from_date']}" for l in lvs[:3]), "Review pending leave applications", len(lvs))
    tr.append(("HRAgent", "leave_scan", f"{len(lvs)} pending"))

    gq = GatePassAgent().queue(con)
    auto = [g for g in gq if g["verdict"] != "review"]
    if gq:
        add("high" if any(g["out_at"][:10] == TODAY.isoformat() for g in gq) else "medium",
            "Gate passes", "GatePassAgent", f"{len(gq)} gate pass(es) waiting — {len(auto)} decidable by policy",
            f"{sum(1 for g in gq if g['out_at'][:10] == TODAY.isoformat())} leave campus today",
            "Decide pending gate passes by policy", len(auto))
    tr.append(("GatePassAgent", "policy_screen", f"{len(gq)} pending · {len(auto)} policy-decidable"))

    od = LibraryAgent().overdue(con)
    if od:
        add("medium", "Library", "LibraryAgent", f"{len(od)} overdue loan(s) across "
            f"{len({o['member'] for o in od})} borrowers", f"{inr(sum(o['fine_due'] for o in od))} in fines accrued",
            "Send overdue library reminders", len({o["member"] for o in od}))
    tr.append(("LibraryAgent", "overdue_scan", f"{len(od)} overdue"))

    ha = HostelAgent()
    wl = ha.waitlist(con)
    if wl:
        add("medium", "Hostel", "HostelAgent", f"{len(wl)} student(s) waiting for a hostel bed",
            "vacancies exist in the matching blocks", "Allocate hostel rooms to the waitlist", len(wl))
    comp = rows(con, "SELECT * FROM hostel_complaints WHERE status='Open'")
    if comp:
        add("high" if any(c["priority"] == "High" for c in comp) else "medium", "Hostel", "HostelAgent",
            f"{len(comp)} open hostel complaint(s)", ", ".join(sorted({c['category'] for c in comp})),
            "Dispatch open hostel complaints", len(comp))
    tr.append(("HostelAgent", "scan", f"waitlist {len(wl)} · open complaints {len(comp)}"))

    ta = TransportAgent()
    load = ta.load(con)
    over = [r for r in ta.routes(con) if load.get(r["id"], 0) > r["capacity"]]
    if over:
        add("medium", "Transport", "TransportAgent", f"{len(over)} bus route(s) over capacity",
            ", ".join(f"{r['id']} {load[r['id']]}/{r['capacity']}" for r in over), "Rebalance bus routes",
            sum(load[r["id"]] - r["capacity"] for r in over))
    tr.append(("TransportAgent", "load_scan", f"{len(over)} over capacity"))

    pa = PlacementAgent()
    for d in pa.drives(con):
        days = (dt.date.fromisoformat(d["drive_date"]) - TODAY).days
        if days <= 14 and not one(con, "SELECT 1 FROM drive_registrations WHERE drive_id=?", (d["id"],)):
            ok, _w = pa.eligibility(con, d)
            add("high" if days <= 7 else "medium", "Placement", "PlacementAgent",
                f"{d['company']} drive in {days} day(s) — shortlist not published",
                f"{len(ok)} eligible students", f"Prepare the shortlist for the {d['company']} drive", len(ok))
    tr.append(("PlacementAgent", "drive_scan", "shortlists due within 14 days"))

    kinds = tuple(v[1] for v in CERT_KINDS.values())
    for r in rows(con, f"SELECT * FROM requests WHERE status='Pending' AND kind IN ({','.join('?' * len(kinds))})",
                  kinds):
        add("medium", "Documents", "DocumentAgent", f"{r['kind']} certificate requested by {r['raised_by']}",
            r["title"], f"Issue {cert_phrase(r['kind'])} for {r['raised_by']} for {r['title'].split(' for ')[-1]}", 1)
    tr.append(("DocumentAgent", "request_scan", "certificate requests in the inbox"))

    short = one(con, "SELECT COUNT(*) n FROM students WHERE attendance<75")["n"]
    first = one(con, "SELECT MIN(date) d FROM exams")["d"]
    if short and first:
        add("high" if (dt.date.fromisoformat(first) - TODAY).days <= 30 else "low", "Examinations", "ExamAgent",
            f"{short} students below 75% with SEE from {first}", "hall tickets will be withheld unless condoned",
            "SEE eligibility check", short)
    tr.append(("ExamAgent", "eligibility_scan", f"{short} below 75%"))

    rank = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda x: (rank[x["severity"]], -x["records"]))
    n = sum(i["records"] for i in items)
    return {"data": {"actions": [{k: i[k] for k in ("severity", "domain", "title", "command", "records")}
                                 for i in items], "records_in_scope": n},
            "blocks": [B_text(f"**{len(items)} things need a decision today**, spanning **{n} records**. Each "
                              f"button hands the job to the agent that owns it — it prepares the change and "
                              f"shows you exactly what it would do; nothing is written until you say yes."),
                       B_actions(items, title="Ops radar")],
            "trace": [("OpsRadar", "fan_out", "asking 9 agents what needs a human today")] + tr
                     + [("OpsRadar", "rank", f"{len(items)} items · severity, then records affected")],
            "chips": [i["command"] for i in items[:4]]}


# ========================================================================= KPIs
def kpis(con):
    """Campus-service numbers for the dashboard, from the same agents."""
    la, ha, ta, pa = LibraryAgent(), HostelAgent(), TransportAgent(), PlacementAgent()
    blk = ha.blocks(con)
    beds, occ = sum(b["beds"] for b in blk), sum(b["occupied"] for b in blk)
    load = ta.load(con)
    act = ta.routes(con)
    seats = sum(r["capacity"] for r in act)
    fin = one(con, "SELECT COUNT(*) n FROM students WHERE sem=7")["n"]
    placed = one(con, "SELECT COUNT(DISTINCT usn) n FROM student_offers")["n"]
    od = la.overdue(con)
    return {"library_overdue": len(od), "library_fines": sum(o["fine_due"] for o in od),
            "on_loan": len(la.active(con)),
            "hostel_occupancy": round(100 * occ / max(1, beds), 1), "hostel_waitlist": len(ha.waitlist(con)),
            "hostel_complaints": one(con, "SELECT COUNT(*) n FROM hostel_complaints WHERE status='Open'")["n"],
            "bus_utilisation": round(100 * sum(load.get(r["id"], 0) for r in act) / max(1, seats), 1),
            "routes_over": sum(1 for r in act if load.get(r["id"], 0) > r["capacity"]),
            "gatepass_pending": one(con, "SELECT COUNT(*) n FROM gate_passes WHERE status='Pending'")["n"],
            "placement_pct": round(100 * placed / max(1, fin), 1), "drives_upcoming": len(pa.drives(con)),
            "certificates": one(con, "SELECT COUNT(*) n FROM certificates")["n"],
            "leaves_pending": one(con, "SELECT COUNT(*) n FROM leaves WHERE status='Pending'")["n"]}


# ===================================================================== registry
def _p(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_S, _I = {"type": "string"}, {"type": "integer"}
_IDS = {"type": "array", "items": {"type": "integer"}}

REGISTRY = [
    (t_ops_radar, "ops_radar",
     "Autopilot: ask every agent what needs a human decision today (SLA breaches, leave, gate passes, "
     "overdue books, hostel waitlist, bus overcrowding, placement shortlists, certificate requests, SEE "
     "eligibility). Returns a ranked action queue.", _p({})),
    (t_student_360, "student_360",
     "Everything about one student: grades, subject-wise attendance, fees, library, hostel, bus, gate "
     "passes, offers, certificates, no-dues.", _p({"usn": _S}, ["usn"])),
    (t_library_overview, "library_overview", "Library circulation: loans, overdue, fines, titles fully out.", _p({})),
    (t_library_search, "library_search", "Search the library catalogue by title, subject, author or BK id.",
     _p({"query": _S}, ["query"])),
    (t_library_overdue, "library_overdue", "Overdue loans with fines, optionally for one department.",
     _p({"dept": _S})),
    (t_issue_book, "issue_book",
     "Issue a book (BK id or title) to a member (USN or staff name). Checks circulation rules. Without "
     "admin approval this turn it only PROPOSES.", _p({"book": _S, "member": _S}, ["book", "member"])),
    (t_return_book, "return_book", "Check in a member's books and compute fines. PROPOSES without approval.",
     _p({"member": _S, "book": _S}, ["member"])),
    (t_library_remind_overdue, "library_remind_overdue",
     "Send one personalised reminder to every overdue borrower. PROPOSES without approval.", _p({})),
    (t_hostel_status, "hostel_status", "Hostel occupancy by block, vacancies, waitlist, complaints, dues.", _p({})),
    (t_hostel_complaints, "hostel_complaints", "Hostel maintenance complaints by status (Open/Assigned/Resolved).",
     _p({"status": _S})),
    (t_allocate_hostel_room, "allocate_hostel_room",
     "Allot hostel beds - to one student (usn) or, with no usn, the whole waitlist - matching gender, "
     "batch-mates and branch-mates. PROPOSES without approval.", _p({"usn": _S})),
    (t_dispatch_hostel_complaints, "dispatch_hostel_complaints",
     "Route every open hostel complaint to the right maintenance crew with an SLA. PROPOSES without approval.",
     _p({})),
    (t_transport_status, "transport_status", "Bus routes, riders vs seats, spares; one route's stops if named.",
     _p({"route": _S})),
    (t_rebalance_bus_routes, "rebalance_bus_routes",
     "Move riders off over-full buses at stops another route also serves. PROPOSES without approval.", _p({})),
    (t_handle_bus_breakdown, "handle_bus_breakdown",
     "A bus broke down: deploy a spare, move any overflow, notify riders and driver. PROPOSES without approval.",
     _p({"route": _S}, ["route"])),
    (t_gate_pass_queue, "gate_pass_queue",
     "Pending gate passes, each screened against hostel policy (approve/reject/review with the reason).", _p({})),
    (t_decide_gate_passes, "decide_gate_passes",
     "Decide gate passes. With no ids, applies the policy verdicts and leaves 'review' ones for the warden. "
     "PROPOSES without approval.", _p({"ids": _IDS, "decision": _S})),
    (t_placement_overview, "placement_overview",
     "Placement statistics by branch and upcoming drives with eligible counts.", _p({})),
    (t_drive_eligibility, "drive_eligibility",
     "Who is eligible for a placement drive (company name or id): CGPA, backlogs, branch, one-offer rule.",
     _p({"drive": _S}, ["drive"])),
    (t_publish_drive_shortlist, "publish_drive_shortlist",
     "Register every eligible student for a drive and notify them. PROPOSES without approval.",
     _p({"drive": _S}, ["drive"])),
    (t_no_dues_status, "no_dues_status",
     "No-dues clearance for a student across accounts, library, hostel and transport.", _p({"usn": _S}, ["usn"])),
    (t_issue_certificate, "issue_certificate",
     "Issue a certificate (Bonafide, Conduct, Fee Structure, Transfer Certificate, No Dues, Internship NOC) "
     "with a register serial; checks eligibility; closes the matching inbox request. PROPOSES without approval.",
     _p({"usn": _S, "kind": _S, "purpose": _S}, ["usn", "kind"])),
    (t_certificate_register, "certificate_register",
     "Certificates issued (optionally for one USN) and certificate requests waiting.", _p({"usn": _S})),
    (t_review_pending_leaves, "review_pending_leaves",
     "Pending faculty leave, each priced by running the substitution planner for every day it spans.",
     _p({"dept": _S})),
    (t_decide_leave, "decide_leave",
     "Approve/reject a leave by id; with no id, approves every pending leave that is fully coverable. "
     "PROPOSES without approval.", _p({"leave_id": _I, "decision": _S, "dept": _S})),
]

GATED_WRITES = ("issue_book", "return_book", "library_remind_overdue", "allocate_hostel_room",
                "dispatch_hostel_complaints", "rebalance_bus_routes", "handle_bus_breakdown",
                "decide_gate_passes", "publish_drive_shortlist", "issue_certificate", "decide_leave")

TOOL_GROUPS = {
    "ops.radar":    ["ops_radar", "institution_overview", "review_pending_leaves", "gate_pass_queue",
                     "list_requests"],
    "student.360":  ["student_360", "no_dues_status", "student_lookup"],
    "library":      ["library_overview", "library_search", "library_overdue", "issue_book", "return_book",
                     "library_remind_overdue"],
    "hostel":       ["hostel_status", "hostel_complaints", "allocate_hostel_room",
                     "dispatch_hostel_complaints", "student_360"],
    "transport":    ["transport_status", "rebalance_bus_routes", "handle_bus_breakdown", "broadcast_notice"],
    "gatepass":     ["gate_pass_queue", "decide_gate_passes", "student_360"],
    "placement":    ["placement_overview", "drive_eligibility", "publish_drive_shortlist"],
    "document":     ["no_dues_status", "issue_certificate", "certificate_register", "student_360"],
    "leave.review": ["review_pending_leaves", "decide_leave", "plan_absence_coverage", "list_leaves"],
}

AGENT_OF = {"ops_radar": "OpsRadar", "student_360": "StudentAgent"}
for _fn, _name, _d, _s in REGISTRY:
    AGENT_OF.setdefault(_name, next((a for pre, a in (
        ("library", "LibraryAgent"), ("issue_book", "LibraryAgent"), ("return_book", "LibraryAgent"),
        ("hostel", "HostelAgent"), ("allocate_hostel", "HostelAgent"), ("dispatch_hostel", "HostelAgent"),
        ("transport", "TransportAgent"), ("rebalance", "TransportAgent"), ("handle_bus", "TransportAgent"),
        ("gate_pass", "GatePassAgent"), ("decide_gate", "GatePassAgent"), ("placement", "PlacementAgent"),
        ("drive", "PlacementAgent"), ("publish_drive", "PlacementAgent"), ("no_dues", "DocumentAgent"),
        ("issue_cert", "DocumentAgent"), ("certificate", "DocumentAgent"), ("review_pending", "HRAgent"),
        ("decide_leave", "HRAgent")) if _name.startswith(pre)), "Supervisor"))


# ================================================================ rule routing
def rule_route(con, intent, text, ent):
    """The deterministic engine's half: utterance -> (tool, args). Execution
    goes through tools.execute like every other caller, so the rule engine and
    the LLM reach these services by the same gate."""
    t = text.lower()
    usn = ent.get("usn") or _usn(text)
    dept = ent.get("dept")
    if intent == "ops.radar":
        return "ops_radar", {}
    if intent == "student.360":
        return "student_360", {"usn": usn or ""}
    if intent == "library":
        if re.search(r"remind|reminder|nudge|notify|chase", t):
            return "library_remind_overdue", {}
        if re.search(r"\breturn", t):
            bk = re.search(r"\bbk\d{3,4}\b", t)
            return "return_book", {"member": usn or text, "book": bk.group(0).upper() if bk else None}
        if re.search(r"\b(?:issue|lend|check ?out|borrow)\b", t) and (usn or ent.get("faculty")):
            bk = re.search(r"\bbk\d{3,4}\b", t)
            if bk:
                ref = bk.group(0)
            else:
                m = re.search(r"(?:issue|lend|check ?out|borrow)\s+(?:the\s+|a\s+)?(?:book\s+)?(.+?)\s+"
                              r"(?:book\s+)?(?:to|for|by)\b", text, re.I)
                ref = m.group(1) if m else text
            member = usn or ent["faculty"]["name"]
            return "issue_book", {"book": ref.strip(" '\""), "member": member}
        if re.search(r"overdue|fine|late|defaulter", t):
            return "library_overdue", {"dept": dept}
        q = " ".join(w for w in re.findall(r"[a-z0-9]+", t) if w not in STOP)
        if len(q) < 3 or re.search(r"\b(?:status|overview|summary|circulation|dashboard)\b", t):
            return "library_overview", {}
        return "library_search", {"query": q}
    if intent == "hostel":
        if re.search(r"\b(?:allot|allocate|assign)\b", t) and not re.search(r"complaint", t):
            return "allocate_hostel_room", {"usn": usn}
        if re.search(r"complaint", t):
            if re.search(r"dispatch|assign|route|triage|fix|resolve|send", t):
                return "dispatch_hostel_complaints", {}
            return "hostel_complaints", {"status": "Open"}
        return "hostel_status", {}
    if intent == "transport":
        if re.search(r"broke|breakdown|broken|not running|puncture|accident", t):
            return "handle_bus_breakdown", {"route": text}
        if re.search(r"rebalanc|optimi|overcrowd|over ?capacity|over-full|redistribut", t):
            return "rebalance_bus_routes", {}
        m = re.search(r"\broute\s*r?0?(\d{1,2})\b|\br0?(\d)\b", t)
        return "transport_status", {"route": f"R{int(m.group(1) or m.group(2)):02d}" if m else None}
    if intent == "gatepass":
        ids = [int(x) for x in re.findall(r"(?:pass(?:es)?|#|id)\s*#?\s*(\d{1,4})", t)]
        ids += [int(x) for x in re.findall(r"(?:,|\band)\s*#?(\d{1,4})\b", t)] if ids else []
        if re.search(r"\b(?:approve|reject|decide|process|clear|screen and decide)\b", t):
            dec = "reject" if re.search(r"\breject", t) else "approve" if re.search(r"\bapprove", t) else None
            return "decide_gate_passes", {"ids": ids or None, "decision": dec}
        return "gate_pass_queue", {}
    if intent == "placement":
        pa = PlacementAgent()
        d = pa.resolve(con, text)
        if d and re.search(r"shortlist|publish|register|notify", t):
            return "publish_drive_shortlist", {"drive": d["company"]}
        if d:
            return "drive_eligibility", {"drive": d["company"]}
        return "placement_overview", {}
    if intent == "document":
        kind = cert_kind(text)
        if re.search(r"register|issued certificates|list (?:of )?certificates", t) or (not usn):
            return "certificate_register", {"usn": usn}
        if re.search(r"no[- ]?dues?", t) and not re.search(r"\b(?:issue|generate|prepare|make|print)\b", t):
            return "no_dues_status", {"usn": usn}
        m = re.search(r"\b(?:for|purpose[:\s]+)\s+(?:the\s+|a\s+|an\s+|his\s+|her\s+|their\s+)?"
                      r"(?!4vp)([a-z0-9][a-z0-9 \-&]{2,50})$", t)
        return "issue_certificate", {"usn": usn, "kind": kind or "Bonafide",
                                     "purpose": (m.group(1).strip() if m else "")}
    if intent == "leave.review":
        m = re.search(r"\bleave\s*(?:#|id|no\.?)?\s*(\d{1,4})\b", t)
        if re.search(r"\b(?:approve|reject|grant|sanction|decide)\b", t):
            return "decide_leave", {"leave_id": int(m.group(1)) if m else None,
                                    "decision": "reject" if re.search(r"\breject", t) else "approve"}
        return "review_pending_leaves", {}
    return "ops_radar", {}
