"""VidyaERP :: FastAPI application

Deployment note, stated plainly because it decides how this may be used:
**the console has no authentication.** There is no login, and /api/chat can
drive every write the copilot offers. Anyone who can reach the URL is the
Registrar. That is correct for a single-admin console on a laptop or a college
LAN, and it is the whole security model — a public deployment is a public demo
over synthetic data, nothing more.

VIDYAERP_ADMIN_TOKEN gates the two endpoints that are NOT console features:
/api/seed/reset (wipes and rebuilds the database) and /api/mcp/approval (mints
the human half of the MCP write gate). Those are out-of-band operator controls,
so leaving them open on a public URL would contradict a claim the project makes
about itself. Unset, they stay open, which is what keeps `git clone && uvicorn`
working with no configuration.
"""
import os, json, datetime as dt
from fastapi import FastAPI, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import db, orchestrator as orch, llm, llm_agent, solver, mcp_server
from agents import (rows, one, fac_name, PERIOD_TIME, TimetableAgent, AnalyticsAgent,
                    RequestAgent, Auditor)
from nlu import TODAY, DAYS, day_of

HERE = os.path.dirname(os.path.abspath(__file__))
db.seed()
con = db.connect()

app = FastAPI(title="VidyaERP", docs_url="/api/docs")

ADMIN_TOKEN = (os.environ.get("VIDYAERP_ADMIN_TOKEN") or "").strip()


def _denied(request):
    """None if the caller may use an operator endpoint, else a 401 response."""
    if not ADMIN_TOKEN:
        return None                      # unset: local development, stays open
    if request.headers.get("X-Admin-Token", "") == ADMIN_TOKEN:
        return None
    return JSONResponse({"error": "operator endpoint — send the X-Admin-Token header"},
                        status_code=401)


@app.get("/health")
def health():
    """Liveness and readiness, for the platform health check and for a human.

    It answers from the database, not from the fact that the process is up: a
    health check that passes while the data layer is unreachable is worse than
    no health check, because it keeps a broken instance in rotation.

    Nothing secret is returned. Whether a key is configured, never what it is.
    """
    try:
        counts = {t: one(con, f"SELECT COUNT(*) c FROM {t}")["c"]
                  for t in ("students", "faculty", "timetable")}
        ok = all(counts.values())
    except Exception as e:
        return JSONResponse({"status": "unhealthy", "service": "vidyaerp",
                             "db": {"ok": False, "error": f"{type(e).__name__}: {e}"}},
                            status_code=503)
    cfg = llm.CFG.reload()
    return JSONResponse({
        "status": "ok" if ok else "unhealthy",
        "service": "vidyaerp",
        "today": TODAY.isoformat(),
        "db": {"ok": ok, **counts,
               "path": os.path.basename(db.DB_PATH),
               # A default filesystem is ephemeral on most platforms: the data
               # re-seeds on restart and applied overrides do not survive.
               "persistent": bool((os.environ.get("VIDYAERP_DB") or "").strip())},
        "engine": "llm" if cfg.enabled else "rule-engine",
        "llm": {"configured": cfg.enabled, "provider": cfg.provider, "model": cfg.model},
        "operator_endpoints": "token-protected" if ADMIN_TOKEN else "open",
    }, status_code=200 if ok else 503)


@app.get("/", response_class=HTMLResponse)
def index():
    # encoding is explicit on purpose: the console contains typographic quotes and
    # '·' separators, and Windows defaults open() to cp1252, which cannot decode
    # them - the whole page 500s on a byte the file has always carried.
    with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.post("/api/chat")
def chat(payload: dict = Body(...)):
    """Routes to the LLM agent when a key is configured, else the deterministic rule engine."""
    text = (payload.get("text") or "").strip()
    sid = payload.get("session", "default")
    role = payload.get("role", "admin")
    if not text:
        return {"blocks": [], "trace": []}
    want = payload.get("engine")                      # 'llm' | 'rule' | None (auto)
    use_llm = llm.CFG.reload().enabled if want in (None, "auto") else (want == "llm")
    if use_llm and llm.CFG.enabled:
        return llm_agent.handle_llm(con, text, sid, role)
    out = orch.handle(con, text, sid, role)
    out["engine"] = "rule-engine"
    return out


@app.get("/api/mode")
def mode():
    return llm.CFG.reload().describe()


@app.post("/api/mode/test")
def mode_test():
    return llm.ping(llm.CFG.reload())


@app.post("/api/reset")
def reset(payload: dict = Body(default={})):
    orch.SESS.pop(payload.get("session", "default"), None)
    return {"ok": True}


@app.post("/api/mcp/approval")
def mcp_approval(request: Request, payload: dict = Body(default={})):
    """Mint a one-time code that lets an MCP client commit ONE guarded write.

    This is the human half of the write gate. In the browser the admin's own
    message is the approval and the model cannot forge it; over MCP there is no
    such message, so the admin mints a code here and reads it out. A model can
    relay a code, never mint one - which is why this endpoint exists on the
    console and not on the MCP surface.

    Operator endpoint: minting write approvals from an open URL would hand the
    gate to whoever found it, so VIDYAERP_ADMIN_TOKEN protects it in any
    deployment. Note also that the MCP server is stdio and reads the database
    beside it, so a code minted here is only useful to an MCP client running
    against this same instance - see the MCP section of the README.
    """
    return _denied(request) or mcp_server.mint_approval(con, payload.get("actor", "admin@vidyatech"))


@app.get("/api/kpis")
def kpis():
    k = AnalyticsAgent().kpis(con)
    k["overrides"] = one(con, "SELECT COUNT(*) c FROM overrides WHERE status='Applied'")["c"]
    k["today"] = TODAY.isoformat()
    k["leaves_today"] = rows(con, """SELECT f.name, f.dept, l.kind, l.from_date, l.to_date
                                     FROM leaves l JOIN faculty f ON f.id=l.faculty
                                     WHERE l.status='Approved' AND l.from_date<=? AND l.to_date>=?""",
                             (TODAY.isoformat(), TODAY.isoformat()))
    k["depts"] = rows(con, """SELECT dept, COUNT(*) n, ROUND(AVG(attendance),1) att,
                              SUM(CASE WHEN attendance<75 THEN 1 ELSE 0 END) short FROM students GROUP BY dept""")
    return k


@app.get("/api/timetable")
def timetable(dept: str = "CSE", sem: int = 5, section: str = "A", date: str = ""):
    return TimetableAgent().class_grid(con, dept, sem, section, date or TODAY.isoformat())


@app.get("/api/classes")
def classes():
    return rows(con, "SELECT DISTINCT dept, sem, section FROM timetable ORDER BY dept, sem, section")


# ------------------------------------------------------ timetable generation
# The stream is a PREVIEW and writes nothing; applying is a separate POST. The
# two agree because the solver is deterministic on its seed - asserted by
# tests/solver_test.py ("same seed gives an identical timetable"), so the client
# never has to ship a whole timetable back to commit what it just watched.
def _gen_args(p):
    scope = "all" if str(p.get("scope", "class")).lower().startswith("all") else "class"
    return {"scope": scope,
            "dept": (p.get("dept") or "CSE").upper() if scope == "class" else None,
            "sem": int(p.get("sem") or 5) if scope == "class" else None,
            "section": (p.get("section") or "A").upper() if scope == "class" else None,
            "seed": int(p.get("seed") or 7)}


@app.get("/api/timetable/generate/stream")
def generate_stream(scope: str = "class", dept: str = "CSE", sem: int = 5,
                    section: str = "A", seed: int = 7):
    a = _gen_args({"scope": scope, "dept": dept, "sem": sem, "section": section, "seed": seed})

    def events():
        inp = solver.build_input(con, **a)
        gen = solver.solve(inp)
        try:
            while True:
                yield "data: " + json.dumps(next(gen)) + "\n\n"
        except StopIteration as stop:
            res = stop.value
        yield "data: " + json.dumps({
            "t": "result", **{k: res[k] for k in
                              ("ok", "placed", "total", "steps", "backtracks", "conflicts",
                               "restarts", "score", "ms", "activity_slots", "timed_out",
                               "unplaced", "capacity_relaxed", "classes")},
            "bookings": [{"cls": "-".join(str(x) for x in b["cls"]), "day": b["day"],
                          "period": b["period"], "subject": b["subject"],
                          "faculty": b["faculty"], "room": b["room"], "kind": b["kind"]}
                         for b in res["bookings"]],
            "args": a}) + "\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/timetable/generate/apply")
def generate_apply(payload: dict = Body(default={})):
    """Commit a generated timetable. Re-solves with the same seed, then verifies."""
    a = _gen_args(payload)
    inp = solver.build_input(con, **a)
    res = solver.run(inp)
    solver.apply(con, res, scope=a["scope"], dept=a["dept"], sem=a["sem"], section=a["section"])
    problems = solver.verify(con, scope=a["scope"], dept=a["dept"], sem=a["sem"],
                             section=a["section"])
    target = "the whole college" if a["scope"] == "all" else f"{a['dept']}-{a['sem']}{a['section']}"
    Auditor().log(con, payload.get("actor", "admin"), "TimetableAgent", "timetable.generate",
                  {**a, "placed": res["placed"], "total": res["total"], "ms": res["ms"]},
                  "Applied" if not problems else f"Applied with {len(problems)} problem(s)")
    return {"ok": not problems, "target": target, "placed": res["placed"], "total": res["total"],
            "periods_written": len(res["bookings"]), "activity_slots": res["activity_slots"],
            "ms": res["ms"], "backtracks": res["backtracks"],
            "unplaced": res["unplaced"], "capacity_relaxed": res["capacity_relaxed"],
            "problems": problems}


@app.get("/api/faculty")
def faculty(dept: str = ""):
    q = """SELECT f.*, (SELECT COUNT(*) FROM timetable t WHERE t.faculty=f.id) load FROM faculty f"""
    if dept:
        q += " WHERE f.dept=?"
    r = rows(con, q, (dept,) if dept else ())
    for x in r:
        x["util"] = round(100 * x["load"] / x["max_load"])
        x["on_leave"] = bool(one(con, """SELECT 1 FROM leaves WHERE faculty=? AND status='Approved'
                                         AND from_date<=? AND to_date>=?""",
                                 (x["id"], TODAY.isoformat(), TODAY.isoformat())))
    return sorted(r, key=lambda x: -x["util"])


@app.get("/api/students")
def students(dept: str = "", sem: int = 0, q: str = "", risk: int = 0, limit: int = 60):
    sql = "SELECT * FROM students WHERE 1=1"
    a = []
    if dept: sql += " AND dept=?"; a.append(dept)
    if sem:  sql += " AND sem=?";  a.append(sem)
    if risk: sql += " AND attendance<75"
    if q:    sql += " AND (name LIKE ? OR usn LIKE ?)"; a += [f"%{q}%", f"%{q.upper()}%"]
    r = rows(con, sql + " ORDER BY attendance LIMIT ?", tuple(a) + (limit,))
    for x in r:
        x["mentor_name"] = fac_name(con, x["mentor"])
    return r


@app.get("/api/requests")
def requests_(status: str = ""):
    r = rows(con, "SELECT * FROM requests" + (" WHERE status=?" if status else "") +
             " ORDER BY CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, id",
             (status,) if status else ())
    for x in r:
        x["age"] = (TODAY - dt.date.fromisoformat(x["created_at"])).days
    return r


@app.post("/api/requests/{rid}/{decision}")
def decide(rid: int, decision: str):
    RequestAgent().decide(con, rid, "Approved" if decision == "approve" else "Rejected")
    return {"ok": True}


@app.get("/api/overrides")
def overrides():
    r = rows(con, """SELECT o.*, t.dept, t.sem, t.section, t.day, t.period, t.subject, t.faculty orig
                     FROM overrides o LEFT JOIN timetable t ON t.id=o.slot_id ORDER BY o.id DESC LIMIT 50""")
    for x in r:
        x["orig_name"] = fac_name(con, x["orig"]) if x["orig"] else "—"
        x["new_name"] = fac_name(con, x["new_faculty"]) if x["new_faculty"] else "—"
    return r


@app.get("/api/notifications")
def notifications():
    return rows(con, "SELECT * FROM notifications ORDER BY id DESC LIMIT 40")


@app.get("/api/audit")
def audit():
    return rows(con, "SELECT * FROM audit ORDER BY id DESC LIMIT 60")


@app.get("/api/leaves")
def leaves():
    r = rows(con, """SELECT l.*, f.name, f.dept FROM leaves l JOIN faculty f ON f.id=l.faculty
                     ORDER BY l.from_date DESC""")
    return r


@app.get("/api/exams")
def exams():
    return rows(con, """SELECT e.*, s.name sname FROM exams e LEFT JOIN subjects s ON s.code=e.subject
                        ORDER BY e.date LIMIT 40""")


@app.post("/api/seed/reset")
def reseed(request: Request):
    """Operator endpoint: drops every table and rebuilds the synthetic college."""
    d = _denied(request)
    if d:
        return d
    global con
    con.close()
    db.seed(force=True)
    con = db.connect()
    orch.SESS.clear()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
