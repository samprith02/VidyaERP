"""
VidyaERP :: authentication and route gating (#27)

Who is asking, proven - standard library only, no new dependency.

  Accounts are the institution itself, not a separate users table:
      registrar            -> the Registrar (role admin)
      F012                 -> a faculty member; role hod if faculty.is_hod
      4VP24CS017           -> a student
  so there is nothing to seed and nothing to drift out of sync.

  Passwords are PBKDF2-HMAC-SHA256, stored only once someone SETS one.

  DEMO MODE (the default) publishes every account's password on the login
  page: it is the account's own ID, lower-cased, and the Registrar's is
  "registrar". That keeps `git clone && uvicorn` working and keeps the public
  deployment what it has always been - a demo over synthetic data. Say so; do
  not imply otherwise. VIDYAERP_DEMO_LOGINS=0 turns it off, and then only
  passwords that were actually set (or VIDYAERP_ADMIN_PASSWORD) work.

  Sessions are server-side. The browser holds a random token in an HttpOnly,
  SameSite=Lax cookie; the database holds only its SHA-256, so a copied
  database does not hand out live sessions.

Authorisation of TOOLS is not here - it is in access.py and enforced by
tools.execute, the one dispatch point. This module only proves identity and
decides which HTTP routes a role may reach (`gate`).
"""
import datetime as dt
import hashlib
import hmac
import os
import secrets
import time

from agents import one

COOKIE = "vidya_session"
SESSION_HOURS = 12
PBKDF2_ROUNDS = 200_000
LOCK_AFTER, LOCK_SECONDS = 5, 60

DDL = """
CREATE TABLE IF NOT EXISTS credentials(
  username TEXT PRIMARY KEY, salt TEXT, hash TEXT, rounds INT, updated TEXT, temporary INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, username TEXT, created TEXT, expires TEXT, last_seen TEXT);
"""


def demo_mode():
    return (os.environ.get("VIDYAERP_DEMO_LOGINS") or "1").strip().lower() not in ("0", "false", "no", "off")


def ensure(con):
    con.executescript(DDL)
    con.commit()


# ------------------------------------------------------------------ identity
def principal(con, username):
    """The account behind a username, or None. Role comes from the data."""
    u = (username or "").strip()
    if u.lower() == "registrar":
        return {"id": "registrar", "role": "admin", "name": "Registrar", "dept": None}
    if u.upper().startswith("F"):
        f = one(con, "SELECT * FROM faculty WHERE id=?", (u.upper(),))
        if f:
            return {"id": f["id"], "role": "hod" if f["is_hod"] else "faculty", "name": f["name"],
                    "dept": f["dept"], "fid": f["id"], "designation": f["designation"]}
    s = one(con, "SELECT * FROM students WHERE usn=?", (u.upper(),))
    if s:
        return {"id": s["usn"], "role": "student", "name": s["name"], "dept": s["dept"], "usn": s["usn"],
                "sem": s["sem"], "section": s["section"]}
    return None


# ----------------------------------------------------------------- passwords
def _hash(password, salt, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), rounds).hex()


def set_password(con, username, password, temporary=False):
    ensure(con)
    salt = secrets.token_hex(16)
    con.execute("INSERT OR REPLACE INTO credentials VALUES(?,?,?,?,?,?)",
                (username, salt, _hash(password, salt), PBKDF2_ROUNDS,
                 dt.datetime.now().isoformat(timespec="seconds"), 1 if temporary else 0))
    con.commit()


def demo_password(username):
    return (username or "").strip().lower()


def verify_password(con, p, password):
    """A set password always wins; the published demo one works only in demo
    mode and only until the account sets its own."""
    ensure(con)
    row = one(con, "SELECT * FROM credentials WHERE username=?", (p["id"],))
    if row:
        return hmac.compare_digest(_hash(password or "", row["salt"], row["rounds"]), row["hash"])
    if p["role"] == "admin":
        env = (os.environ.get("VIDYAERP_ADMIN_PASSWORD") or "").strip()
        if env:
            return hmac.compare_digest((password or "").encode(), env.encode())
    return demo_mode() and hmac.compare_digest((password or "").encode(), demo_password(p["id"]).encode())


# Lockout is in-process on purpose: one worker (render.yaml pins it), and a
# restart clearing it is an acceptable failure mode for a demo-grade gate.
_FAILS = {}


def locked(key):
    n, until = _FAILS.get(key, (0, 0))
    return until > time.time()


def note_failure(key):
    n, _u = _FAILS.get(key, (0, 0))
    n += 1
    _FAILS[key] = (0, time.time() + LOCK_SECONDS) if n >= LOCK_AFTER else (n, 0)


def login(con, username, password, key=None):
    """-> (principal, token) or (None, reason). Same message for an unknown
    account and a wrong password, so the form does not enumerate accounts."""
    key = key or (username or "").lower()
    if locked(key):
        return None, f"Too many attempts - try again in {LOCK_SECONDS} seconds."
    p = principal(con, username)
    if not p or not verify_password(con, p, password):
        note_failure(key)
        return None, "Wrong username or password."
    _FAILS.pop(key, None)
    token = secrets.token_urlsafe(32)
    now = dt.datetime.now()
    con.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), p["id"], now.isoformat(timespec="seconds"),
                 (now + dt.timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds")))
    con.execute("DELETE FROM sessions WHERE expires < ?", (now.isoformat(timespec="seconds"),))
    con.commit()
    return p, token


def session_user(con, token):
    if not token:
        return None
    ensure(con)
    row = one(con, "SELECT * FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    if not row or row["expires"] < dt.datetime.now().isoformat(timespec="seconds"):
        return None
    return principal(con, row["username"])


def logout(con, token):
    if token:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        con.commit()


def temporary_password(con, username):
    """Registrar-issued reset. Returned ONCE to the Registrar's screen; never
    logged, never sent to a model."""
    p = principal(con, username)
    if not p:
        return None
    pw = secrets.token_urlsafe(9)
    set_password(con, p["id"], pw, temporary=True)
    con.execute("DELETE FROM sessions WHERE username=?", (p["id"],))
    con.commit()
    return pw


# ------------------------------------------------------------------ routes
PUBLIC = {"/health", "/login", "/api/auth/login", "/api/auth/demo", "/favicon.ico"}
PUBLIC_PREFIX = ("/static/",)
ANY_USER = {"/", "/portal", "/api/chat", "/api/reset", "/api/mode", "/api/auth/me", "/api/auth/logout",
            "/api/auth/password", "/api/me/home"}
OPERATOR = {"/api/seed/reset", "/api/mcp/approval"}


def gate(path, user, headers=None, admin_token=""):
    """The whole HTTP access rule, as one pure function: None to let the
    request through, else (status, reason). Kept free of the framework so the
    suite can test every branch without a server or an HTTP client."""
    if path in PUBLIC or path.startswith(PUBLIC_PREFIX):
        return None
    if path in OPERATOR:
        if user and user["role"] == "admin":
            return None
        if admin_token and (headers or {}).get("X-Admin-Token", "") == admin_token:
            return None
        return 401, "operator endpoint - sign in as the Registrar or send the X-Admin-Token header"
    if not user:
        return 401, "sign in first"
    if path in ANY_USER:
        return None
    if user["role"] == "admin":
        return None
    return 403, "the Registrar's console only"
