"""VidyaERP :: FastAPI application

Deployment note, stated plainly because it decides how this may be used:
**everyone signs in (#27).** The Registrar gets the console; students, faculty
and HODs get the self-service portal, and what each may see or change is
decided per tool by access.py, enforced in tools.execute. Every route passes
auth.gate - one pure function, tested without a server.

**In DEMO MODE (the default) every password is published on the login page** -
each account's password is its own ID, the Registrar's is "registrar" - so a
public deployment in demo mode is still a public demo over synthetic data,
nothing more. VIDYAERP_DEMO_LOGINS=0 turns that off; then only passwords that
were actually set, or VIDYAERP_ADMIN_PASSWORD for the Registrar, work.

VIDYAERP_ADMIN_TOKEN is an alternative to a Registrar session for the two
operator endpoints: /api/seed/reset (wipes and rebuilds the database) and
/api/mcp/approval (mints the human half of the MCP write gate).
"""
import os, json, datetime as dt
from fastapi import FastAPI, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import db, orchestrator as orch, llm, llm_agent, solver, mcp_server, tools, campus, auth
from agents import (rows, one, fac_name, PERIOD_TIME, TimetableAgent, AnalyticsAgent,
                    RequestAgent, Auditor)
from nlu import TODAY, DAYS, day_of

HERE = os.path.dirname(os.path.abspath(__file__))
db.seed()
con = db.connect()
auth.ensure(con)

app = FastAPI(title="VidyaERP", docs_url="/api/docs")

ADMIN_TOKEN = (os.environ.get("VIDYAERP_ADMIN_TOKEN") or "").strip()


@app.middleware("http")
async def signed_in(request: Request, call_next):
    """Attach the signed-in person, then let auth.gate decide. Pages redirect
    to /login; API calls get a JSON 401/403."""
    user = auth.session_user(con, request.cookies.get(auth.COOKIE))
    request.state.user = user
    verdict = auth.gate(request.url.path, user, dict(request.headers), ADMIN_TOKEN)
    if verdict:
        status, reason = verdict
        if not request.url.path.startswith("/api/") and status == 401:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": reason}, status_code=status)
    return await call_next(request)


def _git_head(root):
    """(commit, branch) read straight out of .git, with no subprocess.

    Only for local development — a deployment gets the commit from the platform
    instead. Resolved once at import, never per request.
    """
    g = os.path.join(root, ".git")
    try:
        with open(os.path.join(g, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
    except OSError:
        return None, None
    if not head.startswith("ref:"):
        return head, None                                   # detached HEAD
    ref = head.split(" ", 1)[1].strip()
    branch = ref.rsplit("/", 1)[-1]
    try:
        with open(os.path.join(g, ref), encoding="utf-8") as f:
            return f.read().strip(), branch
    except OSError:
        pass
    try:                                                    # a packed ref
        with open(os.path.join(g, "packed-refs"), encoding="utf-8") as f:
            for line in f:
                if line.rstrip().endswith(" " + ref):
                    return line.split()[0], branch
    except OSError:
        pass
    return None, branch


# Which code is actually running. Render sets RENDER_GIT_COMMIT on every deploy.
#
# This exists because a deployment silently stopped tracking the repository:
# auto-deploy was configured and enabled, but the webhook never reached the
# platform, so no deploy was created at all — no failure, no error, just a
# stale box behind a green /health for six days. The only way to notice was to
# spot an old ranking weight in an API response. A build identifier turns
# "which code is live" into a question you can answer directly.
def resolve_build(env=None, root=None):
    """(commit, branch, source), platform variable first.

    The platform wins over `.git` deliberately: a deployed image may carry no
    .git at all, and if it does, the checkout is not necessarily what is
    running. Takes `env` so the precedence can be tested without re-importing
    this module — asserting on the module constant alone would pass even if the
    environment were never read, which is the half that matters in production.
    """
    env = os.environ if env is None else env
    sha = (env.get("RENDER_GIT_COMMIT") or "").strip()
    branch = (env.get("RENDER_GIT_BRANCH") or "").strip()
    if sha:
        return sha, branch, "platform"
    sha, b = _git_head(root or HERE)
    return sha, branch or b or "", ("git" if sha else "unavailable")


COMMIT, BRANCH, COMMIT_SOURCE = resolve_build()


def _denied(request):
    """None if the caller may use an operator endpoint, else a 401 response.

    The middleware already applied auth.gate; this repeats the same rule at
    the handler so the endpoint is safe even when called directly (as the
    suite does). A Registrar session or the admin token - nothing else. Before
    logins existed (#27) an unset token left these open; with logins, an
    anonymous caller being able to wipe the database would be indefensible."""
    user = getattr(getattr(request, "state", None), "user", None)
    if auth.gate("/api/seed/reset", user, dict(request.headers), ADMIN_TOKEN) is None:
        return None
    return JSONResponse({"error": "operator endpoint — sign in as the Registrar or send the "
                                  "X-Admin-Token header"}, status_code=401)


@app.get("/health")
def health():
    """Liveness and readiness, for the platform health check and for a human.

    It answers from the database, not from the fact that the process is up: a
    health check that passes while the data layer is unreachable is worse than
    no health check, because it keeps a broken instance in rotation.

    It also reports the commit it is running, so "is the deployment current?"
    is answerable by comparing one string against `git rev-parse HEAD` instead
    of by probing the app's behaviour and inferring.

    Nothing secret is returned. Whether a key is configured, never what it is;
    the commit is a public repository's SHA.
    """
    build = {"commit": COMMIT or "unknown", "short": (COMMIT or "unknown")[:7],
             "branch": BRANCH or "unknown", "source": COMMIT_SOURCE}
    try:
        counts = {t: one(con, f"SELECT COUNT(*) c FROM {t}")["c"]
                  for t in ("students", "faculty", "timetable")}
        ok = all(counts.values())
    except Exception as e:
        return JSONResponse({"status": "unhealthy", "service": "vidyaerp", "build": build,
                             "db": {"ok": False, "error": f"{type(e).__name__}: {e}"}},
                            status_code=503)
    cfg = llm.CFG.reload()
    return JSONResponse({
        "status": "ok" if ok else "unhealthy",
        "service": "vidyaerp",
        "build": build,
        "today": TODAY.isoformat(),
        "db": {"ok": ok, **counts,
               "path": os.path.basename(db.DB_PATH),
               # A default filesystem is ephemeral on most platforms: the data
               # re-seeds on restart and applied overrides do not survive.
               "persistent": bool((os.environ.get("VIDYAERP_DB") or "").strip())},
        "engine": "llm" if cfg.enabled else "rule-engine",
        "llm": {"configured": cfg.enabled, "provider": cfg.provider, "model": cfg.model},
        "operator_endpoints": "registrar-session-or-token" if ADMIN_TOKEN else "registrar-session",
        # Demo mode publishes every password on the login page. Say so here too:
        # a deployment's security posture should be readable, not inferred.
        "auth": {"logins": True, "mode": "demo" if auth.demo_mode() else "strict",
                 "passwords_published": auth.demo_mode()},
    }, status_code=200 if ok else 503)


def _page(name):
    # encoding is explicit on purpose: the pages contain typographic quotes and
    # '·' separators, and Windows defaults open() to cp1252, which cannot decode
    # them - the whole page 500s on a byte the file has always carried.
    with open(os.path.join(HERE, "static", name), encoding="utf-8") as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """The Registrar gets the console; everyone else the self-service portal."""
    user = getattr(request.state, "user", None)
    return _page("index.html" if user and user["role"] == "admin" else "portal.html")


@app.get("/portal", response_class=HTMLResponse)
def portal_page():
    return _page("portal.html")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _page("login.html")


# ------------------------------------------------------------------ sign-in
def _cookie(resp, request, token, age):
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(auth.COOKIE, token, max_age=age, httponly=True, samesite="lax", secure=secure, path="/")


def _public(p):
    return {k: p[k] for k in ("id", "role", "name", "dept", "usn", "sem", "section", "fid", "designation") if k in p}


@app.post("/api/auth/login")
def login(request: Request, payload: dict = Body(default={})):
    ip = request.client.host if request.client else "?"
    p, token = auth.login(con, payload.get("username", ""), payload.get("password", ""),
                          key=f"{(payload.get('username') or '').lower()}@{ip}")
    if not p:
        return JSONResponse({"error": token}, status_code=401)
    Auditor().log(con, p["id"], "Auth", "login", {"role": p["role"]}, "ok")
    resp = JSONResponse({"user": _public(p), "home": "/"})
    _cookie(resp, request, token, auth.SESSION_HOURS * 3600)
    return resp


@app.post("/api/auth/logout")
def logout(request: Request):
    auth.logout(con, request.cookies.get(auth.COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def me(request: Request):
    return {"user": _public(request.state.user), "demo": auth.demo_mode()}


@app.get("/api/auth/demo")
def demo_accounts():
    """Sample accounts for the login page - only while demo mode publishes
    passwords anyway. In strict mode this says nothing."""
    if not auth.demo_mode():
        return {"demo": False, "accounts": []}
    hod = one(con, "SELECT id, name, dept FROM faculty WHERE is_hod=1 ORDER BY id LIMIT 1")
    fac = one(con, "SELECT f.id, f.name, f.dept FROM faculty f WHERE is_hod=0 AND EXISTS "
                   "(SELECT 1 FROM students s WHERE s.mentor=f.id) ORDER BY f.id LIMIT 1")
    stu = one(con, "SELECT usn, name, dept, sem, section FROM students WHERE hostel=1 AND sem=5 ORDER BY usn LIMIT 1")
    fin = one(con, "SELECT usn, name, dept FROM students WHERE sem=7 AND backlogs=0 AND cgpa>=7.5 ORDER BY usn LIMIT 1")
    acc = [{"role": "Registrar", "username": "registrar", "who": "Institution administrator"},
           {"role": "HOD", "username": hod["id"], "who": f"{hod['name']} · {hod['dept']}"},
           {"role": "Faculty", "username": fac["id"], "who": f"{fac['name']} · {fac['dept']}"},
           {"role": "Student", "username": stu["usn"], "who": f"{stu['name']} · {stu['dept']}-{stu['sem']}{stu['section']}"},
           {"role": "Final-year student", "username": fin["usn"], "who": f"{fin['name']} · {fin['dept']}-7"}]
    for a in acc:
        a["password"] = auth.demo_password(a["username"])
    return {"demo": True, "accounts": acc,
            "note": "Demo mode: every account's password is its own ID in lower case. Synthetic data."}


@app.post("/api/auth/password")
def change_password(request: Request, payload: dict = Body(default={})):
    user = request.state.user
    new = payload.get("new") or ""
    if not auth.verify_password(con, user, payload.get("current") or ""):
        return JSONResponse({"error": "Current password is wrong."}, status_code=400)
    if len(new) < 8 or new.lower() == auth.demo_password(user["id"]):
        return JSONResponse({"error": "Use at least 8 characters, and not your ID."}, status_code=400)
    auth.set_password(con, user["id"], new)
    Auditor().log(con, user["id"], "Auth", "password.change", {}, "ok")
    return {"ok": True}


@app.post("/api/auth/reset")
def reset_password(payload: dict = Body(default={})):
    """Registrar only (auth.gate). The temporary password is returned to the
    Registrar's screen once - never logged, never shown to a model."""
    pw = auth.temporary_password(con, payload.get("username", ""))
    if not pw:
        return JSONResponse({"error": "No such account."}, status_code=404)
    Auditor().log(con, "registrar", "Auth", "password.reset", {"username": payload.get("username")}, "issued")
    return {"username": payload.get("username"), "temporary_password": pw,
            "note": "Shown once. Their existing sessions were signed out."}


@app.get("/api/me/home")
def my_home(request: Request):
    S = {"pending": None, "history": [], "ctx": {}, "user": request.state.user}
    r = tools.execute(con, S, "", "my_home", {})
    return {"blocks": r.get("blocks", []), "chips": r.get("chips", []), "data": r.get("data")}


def _sid(user, sid):
    """The chat session belongs to the signed-in person. Keying it by the
    client's id alone would let anyone answer "yes" to someone else's pending
    write by sending their session id."""
    return f"{user['id']}::{sid or 'default'}"


@app.post("/api/chat")
def chat(request: Request, payload: dict = Body(...)):
    """Routes to the LLM agent when a key is configured, else the deterministic rule engine.
    The role is the SESSION's, never a field the client sends."""
    user = request.state.user
    text = (payload.get("text") or "").strip()
    sid = _sid(user, payload.get("session"))
    if not text:
        return {"blocks": [], "trace": []}
    S = orch.sess(sid)
    S["user"] = None if user["role"] == "admin" else user   # admin: unrestricted, as before
    want = payload.get("engine")                      # 'llm' | 'rule' | None (auto)
    use_llm = llm.CFG.reload().enabled if want in (None, "auto") else (want == "llm")
    if use_llm and llm.CFG.enabled:
        return llm_agent.handle_llm(con, text, sid, user["role"], actor=user["id"])
    out = orch.handle(con, text, sid, user["role"], actor=user["id"])
    out["engine"] = "rule-engine"
    return out


@app.get("/api/mode")
def mode():
    return llm.CFG.reload().describe()


@app.post("/api/mode/test")
def mode_test():
    return llm.ping(llm.CFG.reload())


@app.post("/api/reset")
def reset(request: Request, payload: dict = Body(default={})):
    orch.SESS.pop(_sid(request.state.user, payload.get("session")), None)
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
    k["campus"] = campus.kpis(con)
    return k


# ------------------------------------------------------------ campus panels
# The module views render exactly what the agents' READ tools return, through
# the same tools.execute every other caller uses. Only reads are listed, and U
# is empty, so even a listed name could not commit anything: an empty U is the
# refusal every gated write already returns. tests/campus_test.py asserts no
# gated write is ever in this set.
PANELS = {"ops_radar", "library_overview", "library_overdue", "library_search", "hostel_status",
          "hostel_complaints", "transport_status", "gate_pass_queue", "placement_overview",
          "drive_eligibility", "certificate_register", "no_dues_status", "student_360",
          "review_pending_leaves"}


@app.get("/api/panel/{name}")
def panel(name: str, request: Request):
    if name not in PANELS:
        return JSONResponse({"error": f"'{name}' is not a readable panel"}, status_code=404)
    args = {k: v for k, v in request.query_params.items() if v}
    r = tools.execute(con, {"pending": None, "history": [], "ctx": {}}, "", name, args)
    return {"blocks": r.get("blocks", []), "data": r.get("data"), "chips": r.get("chips", []),
            "trace": [{"agent": t[0], "action": t[1], "detail": t[2] if len(t) > 2 else "",
                       "status": "ok", "ms": 0} for t in r.get("trace", [])]}


@app.get("/api/badges")
def badges():
    c = campus.kpis(con)
    return {"requests": one(con, "SELECT COUNT(*) c FROM requests WHERE status='Pending'")["c"],
            "gatepasses": c["gatepass_pending"], "leaves": c["leaves_pending"],
            "complaints": c["hostel_complaints"], "overdue": c["library_overdue"]}


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
