"""
VidyaERP :: who may call which tool, on whose data (#27)

Enforced by tools.execute - the one dispatch point the rule engine, the LLM
agent and MCP all reach - so no caller can route around it. Default DENY: a
tool that is not listed for a role is refused for that role, whatever it is.

A rule either lets a call through, rewrites its arguments into the caller's
own scope (a student's `student_360` is always their own record), or refuses
with a reason. Rewriting is only ever NARROWING: a rule may fill in "me" or
"my department", never widen a request.

The Registrar (and any caller with no principal - MCP over stdio, the test
suites, internal code) is unrestricted. The web always attaches a principal;
app.chat sets it from the session cookie on every request.
"""
import re
import nlu
from agents import rows, one

ALLOW = "allow"


def _fac(con, ref):
    fac = rows(con, "SELECT id,name,dept,designation,expertise,max_load FROM faculty")
    m = re.search(r"\bf0?(\d{2,3})\b", str(ref or ""), re.I)
    if m:
        return next((f for f in fac if f["id"] == f"F{int(m.group(1)):03d}"), None)
    f, sc = nlu.match_faculty(str(ref or ""), fac)
    return f if f and sc >= 0.8 else None


def _stu(con, ref):
    m = re.search(r"\b4vp\d{2}[a-z]{2}\d{3}\b", str(ref or ""), re.I)
    if m:
        return one(con, "SELECT * FROM students WHERE usn=?", (m.group(0).upper(),))
    q = str(ref or "").strip()
    return one(con, "SELECT * FROM students WHERE name LIKE ?", (f"%{q}%",)) if len(q) >= 3 else None


# ------------------------------------------------------------ scope rules
def self_usn(con, P, a, key="usn"):
    if a.get(key) and str(a[key]).strip().upper() != P["usn"]:
        return None, "You can only see your own record."
    a[key] = P["usn"]
    return a, None


def own_class(con, P, a):
    for k, v in (("dept", P["dept"]), ("sem", P["sem"]), ("section", P["section"])):
        if a.get(k) not in (None, "") and str(a[k]).upper() != str(v).upper():
            return None, "You can only see your own class timetable."
        a[k] = v
    return a, None


def own_dept_sem(con, P, a):
    if a.get("dept") and str(a["dept"]).upper() != P["dept"]:
        return None, "You can only see your own department's exams."
    a["dept"] = P["dept"]
    if P["role"] == "student":
        a["sem"] = P["sem"]
    return a, None


def own_dept(con, P, a, key="dept"):
    if a.get(key) and str(a[key]).upper() != P["dept"]:
        return None, f"You can only see {P['dept']}."
    a[key] = P["dept"]
    return a, None


def dept_class(con, P, a):
    if a.get("dept") and str(a["dept"]).upper() != P["dept"]:
        return None, f"You can only see {P['dept']} timetables."
    a["dept"] = P["dept"]
    return a, None


def self_fac(con, P, a, key="name"):
    f = _fac(con, a.get(key)) if a.get(key) else None
    if a.get(key) and (not f or f["id"] != P["fid"]):
        return None, "You can only see your own schedule and profile."
    a[key] = P["fid"]
    return a, None


def own_teaching(con, P, a):
    """A teacher's own make-up classes, whatever class they are for."""
    if a.get("faculty") and str(a["faculty"]).upper() != P["fid"]:
        return None, "You can only see the make-up classes you teach."
    a["faculty"] = P["fid"]
    return a, None


def dept_fac(con, P, a, key="name"):
    """HOD: any colleague in the department, themselves by default."""
    if not a.get(key):
        a[key] = P["fid"]
        return a, None
    f = _fac(con, a[key])
    if not f:
        return None, f"No faculty member matches '{a[key]}'."
    if f["dept"] != P["dept"]:
        return None, f"{f['name']} is not in {P['dept']}."
    a[key] = f["id"]
    return a, None


def dept_student(con, P, a, key):
    s = _stu(con, a.get(key))
    if not s:
        return None, "Name a student in your department by USN."
    if s["dept"] != P["dept"]:
        return None, f"{s['usn']} is not in {P['dept']}."
    a[key] = s["usn"]
    return a, None


def dept_leave(con, P, a):
    a["dept"] = P["dept"]
    if a.get("leave_id") not in (None, ""):
        lv = one(con, """SELECT l.id, f.dept FROM leaves l JOIN faculty f ON f.id=l.faculty WHERE l.id=?""",
                 (int(re.sub(r"\D", "", str(a["leave_id"])) or 0),))
        if not lv or lv["dept"] != P["dept"]:
            return None, f"Leave {a['leave_id']} is not a {P['dept']} application."
    return a, None


# ------------------------------------------------------------- the policy
STUDENT = {
    "my_home": ALLOW, "my_requests": ALLOW, "my_placement": ALLOW,
    "request_gate_pass": ALLOW, "request_certificate": ALLOW,
    "student_360": self_usn, "no_dues_status": self_usn,
    "get_timetable": own_class, "exam_schedule": own_dept_sem, "library_search": ALLOW,
    "makeup_schedule": own_class,
}
FACULTY = {
    "my_home": ALLOW, "my_requests": ALLOW, "my_mentees": ALLOW, "my_leaves": ALLOW, "apply_leave": ALLOW,
    "faculty_timetable": self_fac, "faculty_profile": self_fac,
    "get_timetable": dept_class, "exam_schedule": ALLOW, "library_search": ALLOW, "find_free_rooms": ALLOW,
    "makeup_schedule": own_teaching,
    "faculty_availability": lambda con, P, a: self_fac(con, P, a, "faculty"),
}
HOD = {
    **FACULTY,
    "dept_overview": ALLOW,
    "makeup_schedule": lambda con, P, a: own_dept(con, P, a),
    "faculty_timetable": dept_fac, "faculty_profile": dept_fac,
    "faculty_workload": own_dept, "attendance_defaulters": own_dept, "exam_eligibility": own_dept,
    "find_free_faculty": own_dept,
    "faculty_availability": lambda con, P, a: own_dept(con, P, a),
    "student_360": lambda con, P, a: dept_student(con, P, a, "usn"),
    "student_lookup": lambda con, P, a: dept_student(con, P, a, "query"),
    "review_pending_leaves": lambda con, P, a: own_dept(con, P, a),
    "decide_leave": dept_leave,
    "plan_absence_coverage": lambda con, P, a: dept_fac(con, P, a, "faculty_name"),
    "apply_coverage_plan": ALLOW,          # only ever commits the HOD's own staged plan
}
POLICY = {"student": STUDENT, "faculty": FACULTY, "hod": HOD}


def allowed_tools(P):
    """None means unrestricted."""
    if not P or P.get("role") == "admin":
        return None
    return sorted(POLICY.get(P["role"], {}))


def authorize(con, P, name, args):
    """-> (args, None) to proceed with possibly narrowed args, or (None, reason)."""
    if not P or P.get("role") == "admin":
        return args, None
    rule = POLICY.get(P["role"], {}).get(name)
    if rule is None:
        return None, (f"'{name}' is not available to a {P['role']} account — the Registrar's office "
                      f"handles that.")
    if rule == ALLOW:
        return args, None
    return rule(con, P, dict(args or {}))
