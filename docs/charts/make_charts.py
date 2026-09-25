"""Measure the system and draw the README's charts. Standard library only.

    python docs/charts/make_charts.py            # measure + draw (about a minute)
    python docs/charts/make_charts.py --tests    # also run the ten no-server suites

Every number in docs/charts/*.svg comes from running the code in this repo
against the seeded institution; nothing is typed in by hand. The measurements
are written to docs/charts/results.json next to the SVGs, so a chart can be
checked against its data, and re-running this script reproduces both.

It works on a COPY of college.db in the system temp directory (like
tests/ranking_test.py), so it can never change the shipped database.

The SVGs carry their own light and dark palettes (prefers-color-scheme), drawn
from a colour-blind-validated categorical set: blue / orange / aqua.
"""
import collections
import datetime as dt
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_charts.db")
db.seed()
shutil.copy(db.DB_PATH, _TMP)
db.DB_PATH = _TMP

import agents                                                      # noqa: E402
import solver                                                      # noqa: E402
import tools                                                       # noqa: E402
from nlu import TODAY, day_of                                      # noqa: E402

con = db.connect()
R = {"generated_by": "docs/charts/make_charts.py", "today": TODAY.isoformat()}


# =================================================================== SVG kit
W = 760
STYLE = """
<style>
svg{--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--base:#c3c2b7;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--wash:rgba(42,120,214,.10);--band:rgba(11,11,11,.035)}
@media (prefers-color-scheme: dark){svg{--surface:#1a1a19;--ink:#ffffff;--ink2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--base:#383835;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--wash:rgba(57,135,229,.14);--band:rgba(255,255,255,.04)}}
text{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;fill:var(--ink2);font-size:12px}
.bg{fill:var(--surface)} .h{fill:var(--ink);font-size:16px;font-weight:600} .sub{fill:var(--ink2);font-size:12.5px}
.t1{fill:var(--ink)} .mut{fill:var(--muted);font-size:11px} .num{font-variant-numeric:tabular-nums}
.grid{stroke:var(--grid);stroke-width:1} .base{stroke:var(--base);stroke-width:1}
.s1{fill:var(--s1)} .s2{fill:var(--s2)} .s3{fill:var(--s3)}
.l1{stroke:var(--s1);stroke-width:2;fill:none;stroke-linejoin:round;stroke-linecap:round}
.wash{fill:var(--wash)} .band{fill:var(--band)} .ring{stroke:var(--surface);stroke-width:2}
.rule{stroke:var(--ink2);stroke-width:1}
</style>"""


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Svg:
    def __init__(self, title, sub, h):
        self.h, self.parts = h, []
        self.parts.append(f'<rect class="bg" x="0" y="0" width="{W}" height="{h}" rx="10"/>')
        self.text(24, 34, title, "h")
        assert len(sub) <= 112, f"subtitle too long ({len(sub)}): {sub}"
        self.text(24, 54, sub, "sub")
        self.title = title

    def text(self, x, y, s, cls="", anchor="start", extra=""):
        self.parts.append(f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" text-anchor="{anchor}" {extra}>{esc(s)}</text>')

    def line(self, x1, y1, x2, y2, cls):
        self.parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" class="{cls}"/>')

    def raw(self, s):
        self.parts.append(s)

    def hbar(self, x, y, w, t, cls):
        """Horizontal bar: square at the baseline (left), 4px rounded data end."""
        if w <= 0:
            return
        r = min(4, w, t / 2)
        self.raw(f'<path class="{cls}" d="M{x:.1f},{y:.1f} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} '
                 f'v{t - 2 * r:.1f} a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z"/>')

    def vbar(self, x, ybase, w, h, cls):
        """Column: square at the baseline (bottom), 4px rounded data end (top)."""
        if h <= 0:
            return
        r = min(4, h, w / 2)
        y = ybase - h
        self.raw(f'<path class="{cls}" d="M{x:.1f},{ybase:.1f} v-{h - r:.1f} a{r},{r} 0 0 1 {r},-{r} '
                 f'h{w - 2 * r:.1f} a{r},{r} 0 0 1 {r},{r} v{h - r:.1f} z"/>')

    def legend(self, x, y, items):
        for label, cls in items:
            self.raw(f'<rect x="{x}" y="{y - 9}" width="10" height="10" rx="2" class="{cls}"/>')
            self.text(x + 16, y, label, "t1")
            x += 30 + 7 * len(label)

    def save(self, name, source):
        self.text(24, self.h - 14, source, "mut")
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {self.h}" width="{W}" '
               f'height="{self.h}" role="img" aria-label="{esc(self.title)}">{STYLE}'
               + "".join(self.parts) + "</svg>\n")
        with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
            f.write(svg)
        print(f"  wrote docs/charts/{name}")


def nice_max(v):
    """Smallest round axis maximum >= v whose quarter ticks are whole, round numbers."""
    if v <= 0:
        return 4
    mag = 10 ** max(len(str(int(v))) - 2, 0)
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100):
        top = 4 * step * mag
        if top >= v and float(step * mag).is_integer():
            return int(top)
    return int(4 * 100 * mag)


def tick(v):
    return f"{int(v):,}" if float(v).is_integer() else f"{v:g}"


def hbar_chart(name, title, sub, items, source, cls="s1", fmt=tick, label_w=170, extra=None):
    """items: [(label, value)] in display order. One series, value at the tip."""
    rowh, t = 30, 18
    top = 80
    h = top + rowh * len(items) + 50
    s = Svg(title, sub, h)
    x0, x1 = 24 + label_w, W - 70
    vmax = nice_max(max(v for _l, v in items))
    for k in range(5):
        gx = x0 + (x1 - x0) * k / 4
        s.line(gx, top - 6, gx, top + rowh * len(items) - 6, "grid")
        s.text(gx, top + rowh * len(items) + 10, fmt(vmax * k / 4), "mut num", "middle")
    for i, (label, v) in enumerate(items):
        y = top + i * rowh
        s.text(x0 - 10, y + t - 4, label, "t1", "end")
        wv = (x1 - x0) * v / vmax
        s.hbar(x0, y, wv, t, cls if isinstance(cls, str) else cls[i])
        s.text(x0 + wv + 6, y + t - 4, fmt(v), "t1 num")
    s.line(x0, top - 6, x0, top + rowh * len(items) - 6, "base")
    if extra:
        extra(s)
    s.save(name, source)


def column_chart(name, title, sub, items, source, cls="s1", fmt=tick, xlabel="", h=330,
                 marker=None, tick_every=1):
    """items: [(label, value)]. One series, columns, value on the cap when it fits."""
    top, bottom = 80, h - 72
    s = Svg(title, sub, h)
    x0, x1 = 60, W - 24
    n = len(items)
    band = (x1 - x0) / n
    bw = min(24, band - 2)
    vmax = nice_max(max(v for _l, v in items))
    for k in range(5):
        gy = bottom - (bottom - top) * k / 4
        s.line(x0, gy, x1, gy, "grid" if k else "base")
        s.text(x0 - 8, gy + 4, fmt(vmax * k / 4), "mut num", "end")
    for i, (label, v) in enumerate(items):
        cx = x0 + band * i + band / 2
        hv = (bottom - top) * v / vmax
        s.vbar(cx - bw / 2, bottom, bw, hv, cls if isinstance(cls, str) else cls[i])
        if v and (n <= 16 or v == max(x for _l, x in items)):
            s.text(cx, bottom - hv - 6, fmt(v), "t1 num", "middle")
        if i % tick_every == 0:
            s.text(cx, bottom + 16, label, "mut num", "middle")
    if xlabel:
        s.text((x0 + x1) / 2, bottom + 34, xlabel, "mut", "middle")
    if marker:
        marker(s, x0, x1, top, bottom, band)
    s.save(name, source)


# ======================================================== 1. coverage sweep
print("\n1 · coverage plans across every absence the timetable can produce")
SUB = agents.SubstitutionAgent()


class Tr:
    def add(self, *a, **k):
        pass


week = [TODAY + dt.timedelta(days=i) for i in range(1, 8) if day_of(TODAY + dt.timedelta(days=i)) != "Sun"]
faculty = agents.rows(con, "SELECT * FROM faculty ORDER BY id")
sweep, t0 = [], time.perf_counter()
for date in week:
    for f in faculty:
        plans, affected = SUB.build_plans(con, f, date, Tr())
        if not plans:
            continue
        sweep.append({"faculty": f["id"], "date": date.isoformat(), "blocks": len(affected),
                      "plans": [{k: p[k] for k in ("code", "rank", "confidence", "coverage", "continuity")}
                                for p in plans]})
sweep_ms = (time.perf_counter() - t0) * 1000
n_abs = len(sweep)
top = [a["plans"][0] for a in sweep]
rec = collections.Counter(p["code"] for p in top)
folded = sum(1 for a in sweep if len(a["plans"]) < 3)
full = sum(1 for p in top if p["coverage"] == 100)
R["coverage"] = {"absences": n_abs, "dates": [d.isoformat() for d in week],
                 "recommended": dict(sorted(rec.items())), "folded_duplicates": folded,
                 "top_plan_fully_covered": full, "ms_total": round(sweep_ms),
                 "ms_per_absence": round(sweep_ms / max(n_abs, 1), 1),
                 "top_rank": {"min": min(p["rank"] for p in top), "median": statistics.median(p["rank"] for p in top),
                              "max": max(p["rank"] for p in top)}}
print(f"  {n_abs} absences, {sweep_ms / max(n_abs, 1):.1f} ms each; recommended {dict(rec)}")

PLAN_CLS = {"A": "s1", "B": "s2", "C": "s3"}
PLAN_NAME = {"A": "A · substitute", "B": "B · swap forward", "C": "C · release + make-up"}
hbar_chart("plans_recommended.svg", "Which plan the ranking recommends",
           f"Each teacher absent on each teaching day of a week: {n_abs} absences. "
           f"The top plan fully covers {full}.",
           [(PLAN_NAME[c], rec.get(c, 0)) for c in "ABC"],
           "Measured by docs/charts/make_charts.py · SubstitutionAgent.build_plans over the seeded institution",
           cls=[PLAN_CLS[c] for c in "ABC"], label_w=150)

# scatter: confidence vs continuity for every plan offered
h = 470
s = Svg("Every plan offered, by the two axes it is ranked on",
        "One dot per plan card. rank = 0.8·confidence + 0.2·continuity; coverage is a floor, not an axis.", h)
x0, x1, y0, y1 = 70, W - 30, 90, h - 70
lo_x = min(p["confidence"] for a in sweep for p in a["plans"])
lo_y = min(p["continuity"] for a in sweep for p in a["plans"])
lo_x, lo_y = (int(lo_x) // 10) * 10, (int(lo_y) // 10) * 10
sx = lambda v: x0 + (x1 - x0) * (v - lo_x) / (100 - lo_x)
sy = lambda v: y1 - (y1 - y0) * (v - lo_y) / (100 - lo_y)
for k in range(lo_x, 101, 10):
    s.line(sx(k), y0, sx(k), y1, "grid")
    s.text(sx(k), y1 + 16, k, "mut num", "middle")
for k in range(lo_y, 101, 10):
    s.line(x0, sy(k), x1, sy(k), "grid")
    s.text(x0 - 8, sy(k) + 4, k, "mut num", "end")
s.line(x0, y1, x1, y1, "base")
s.text((x0 + x1) / 2, y1 + 36, "confidence (the deliverer's measured fit, 0–100)", "mut", "middle")
s.text(20, (y0 + y1) / 2, "continuity (0–100)", "mut", "middle", f'transform="rotate(-90 20 {(y0 + y1) / 2})"')
n_pts = collections.Counter()
for code in "CBA":                                        # A drawn last, on top
    for a in sweep:
        for p in a["plans"]:
            if p["code"] == code:
                n_pts[code] += 1
                s.raw(f'<circle cx="{sx(p["confidence"]):.1f}" cy="{sy(p["continuity"]):.1f}" r="4" '
                      f'class="{PLAN_CLS[code]} ring" fill-opacity=".75"/>')
s.legend(x0, 78, [(f"{PLAN_NAME[c]} ({n_pts[c]})", PLAN_CLS[c]) for c in "ABC"])
s.save("plans_scatter.svg", "Measured by docs/charts/make_charts.py · duplicate plans folded by committed effect")

bins = collections.Counter(int(p["rank"] // 5) * 5 for p in top)
lo = min(bins)
column_chart("plans_rank.svg", "How good is the best plan?",
             f"Rank of the recommended plan across {n_abs} absences "
             f"(median {R['coverage']['top_rank']['median']:.1f}).",
             [(f"{b}", bins.get(b, 0)) for b in range(lo, 100, 5)],
             "Measured by docs/charts/make_charts.py · bins of 5 rank points",
             xlabel="rank of the recommended plan (lower edge of each bin)")


# =========================================================== 2. the solver
print("\n2 · timetable solver")
inp = solver.build_input(con, scope="all", seed=7)
res = solver.run(inp)
ev = res["trace"]
kinds = collections.Counter(e["t"] for e in ev)
fails = collections.Counter(e.get("c") for e in ev if e["t"] == "fail")
R["solver"] = {k: res[k] for k in ("placed", "total", "backtracks", "conflicts", "ms", "activity_slots")}
R["solver"]["capacity_relaxed"] = len(res["capacity_relaxed"])
R["solver"]["events"] = dict(kinds)
R["solver"]["rejections"] = dict(fails)
times = []
for seed in range(1, 11):
    r = solver.run(solver.build_input(con, scope="all", seed=seed))
    times.append((seed, r["ms"], r["placed"], r["total"]))
R["solver"]["seeds"] = [{"seed": a, "ms": b, "placed": c, "total": d} for a, b, c, d in times]
print(f"  {res['placed']}/{res['total']} in {res['ms']} ms; 10 seeds: {[t[1] for t in times]} ms")

# the same instance, ten different seeds: every one places everything
column_chart("solver_seeds.svg", "Ten seeds, ten complete timetables",
             f"Solve time for the whole college ({res['total']} periods, {len(res['classes'])} sections). "
             f"Every seed placed {min(t[2] for t in times)} of {res['total']}.",
             [(f"seed {a}", b) for a, b, _c, _d in times],
             "Measured by docs/charts/make_charts.py · solver.run on this machine; the stream the browser draws is the same generator",
             fmt=lambda v: f"{int(v):,} ms")

FAIL_NAME = {"class": "class already busy", "teacher": "teacher already busy", "room": "no free room",
             "unavail": "teacher's standing window", "dayload": "teacher's daily cap"}
hbar_chart("solver_rejections.svg", "Why candidate slots were rejected",
           f"{sum(fails.values()):,} candidate slots refused by a hard constraint in one full solve (seed 7).",
           [(FAIL_NAME.get(k, str(k)), v) for k, v in fails.most_common()],
           "Measured by docs/charts/make_charts.py · 'fail' events in solver.run(seed=7)",
           fmt=lambda v: f"{int(v):,}", label_w=190)

scar = []
for nlabs in (6, 5, 4, 3, 2, 1):
    i4 = solver.build_input(con, scope="all", seed=7, time_budget_s=20)
    labs = [r for r in i4.rooms if i4.room_meta[r]["kind"] == "Lab"]
    if nlabs > len(labs):
        continue
    i4.rooms = [r for r in i4.rooms if r not in set(labs[nlabs:])]
    r4 = solver.run(i4)
    scar.append({"lab_rooms": nlabs, "placed": r4["placed"], "total": r4["total"], "ms": r4["ms"],
                 "backtracks": r4["backtracks"]})
n_labs_all = len([r for r in inp.rooms if inp.room_meta[r]["kind"] == "Lab"])
R["solver"]["scarcity"] = scar
R["solver"]["lab_rooms_total"] = n_labs_all
print("  scarcity:", [(x["lab_rooms"], x["placed"], x["ms"]) for x in scar])
column_chart("solver_scarcity.svg", "Take lab rooms away: it degrades, it does not collapse",
             f"Share of {scar[0]['total']} curriculum periods placed as lab rooms are removed "
             f"(the college has {n_labs_all}). Each solve < {max(x['ms'] for x in scar) / 1000:.1f} s.",
             [(f"{x['lab_rooms']} lab room{'s' if x['lab_rooms'] > 1 else ''}", round(100 * x["placed"] / x["total"], 1))
              for x in scar],
             "Measured by docs/charts/make_charts.py · same instance as tests/solver_test.py, seed 7",
             fmt=lambda v: f"{v:g}%")


# ============================================ 3. the tool surface and router
print("\n3 · tools and the router")
mod_of = {n: tools.FUNCS[n].__module__ for n in tools.FUNCS}
MOD = {"tools": "core (tools.py)", "campus": "campus.py", "portal": "portal.py", "academics": "academics.py"}
by_mod = collections.defaultdict(lambda: [0, 0])
for n, m in mod_of.items():
    by_mod[m][1 if n in tools.GATED_WRITES else 0] += 1
R["tools"] = {"total": len(tools.FUNCS), "gated": len(tools.GATED_WRITES),
              "by_module": {MOD.get(m, m): {"reads": r, "gated_writes": w} for m, (r, w) in by_mod.items()}}
order = sorted(by_mod.items(), key=lambda kv: -sum(kv[1]))
rowh, t, top_ = 34, 18, 96
h = top_ + rowh * len(order) + 44
s = Svg(f"{len(tools.FUNCS)} tools, {len(tools.GATED_WRITES)} of them writes, every write gated",
        "A gated write computes and stages its change, and commits only on explicit approval in the same turn.", h)
x0, x1 = 170, W - 80
vmax = nice_max(max(sum(v) for _m, v in order))
for k in range(5):
    gx = x0 + (x1 - x0) * k / 4
    s.line(gx, top_ - 6, gx, top_ + rowh * len(order) - 6, "grid")
    s.text(gx, top_ + rowh * len(order) + 10, tick(vmax * k / 4), "mut num", "middle")
for i, (m, (r, w)) in enumerate(order):
    y = top_ + i * rowh
    s.text(x0 - 10, y + t - 4, MOD.get(m, m), "t1", "end")
    wr = (x1 - x0) * r / vmax
    ww = (x1 - x0) * w / vmax
    s.raw(f'<rect class="s1" x="{x0}" y="{y}" width="{max(wr - 2, 0):.1f}" height="{t}"/>')   # 2px surface gap
    s.hbar(x0 + wr, y, ww, t, "s2")
    s.text(x0 + wr + ww + 6, y + t - 4, f"{r} read · {w} write", "t1 num")
s.line(x0, top_ - 6, x0, top_ + rowh * len(order) - 6, "base")
s.legend(x0, 80, [("read", "s1"), ("gated write", "s2")])
s.save("tools_by_module.svg", "Measured by docs/charts/make_charts.py · tools.FUNCS and tools.GATED_WRITES")

# the router: how many tools each utterance is offered, and what that saves
CORPUS = [
    "Which ECE faculty are free on Monday period 4?", "Free rooms at period 6",
    "Show CSE 5th sem A timetable", "Who is teaching at period 3?", "Faculty workload above 90%",
    "Who teaches Machine Learning?", "Attendance defaulters below 65% in CSE sem 5",
    "Student 4VP24CS017 details", "Fee dues by department", "SEE eligibility check for MECH",
    "Draft invigilation roster", "Pending approvals", "Approve 3", "Bulk approve under 50000",
    "Raise a purchase request to the Principal for 3 lakh, urgent",
    "Notify all CSE students that lab exams are postponed to 15 Sep",
    "Brief me on today's institution status", "Who is on leave this week?",
    "Dr. Rashmi Prabhu is absent next Monday, arrange coverage", "Show alternatives for period 3",
    "Undo the last change", "Generate the timetable from scratch", "What needs my attention today?",
    "Issue BK0012 to 4VP24CS017", "Send overdue library reminders", "Allocate hostel rooms to the waitlist",
    "Rebalance bus routes", "Bus on route 4 broke down", "Decide pending gate passes by policy",
    "Who is eligible for the Nethra Robotics drive?",
    "Issue a bonafide certificate for 4VP24CS017 for passport", "Review pending leave applications",
    "Everything about 4VP24CS017", "No dues status of 4VP24CS017", "Hostel complaints",
    "Library overdue", "Placement overview", "Prof. Nithin Mallya is not available every Friday afternoon",
    "Show the make-up schedule", "Faculty timetable of Dr. Sunitha Gowda",
]
full_bytes = len(json.dumps(tools.SCHEMAS))
offered, sizes = [], []
for u in CORPUS:
    sch, names = tools.select_tools(u, {})
    offered.append(len(names))
    sizes.append(len(json.dumps(sch)))
saving = 100 * (1 - statistics.mean(sizes) / full_bytes)
R["router"] = {"utterances": len(CORPUS), "offered": offered, "full_schema_bytes": full_bytes,
               "mean_offered_bytes": round(statistics.mean(sizes)), "payload_saving_pct": round(saving, 1)}
print(f"  {len(CORPUS)} utterances: offered {min(offered)}-{max(offered)} tools, payload -{saving:.0f}%")
cnt = collections.Counter(offered)
column_chart("router.svg", f"The tool router offers a short menu, not all {len(tools.FUNCS)}",
             f"Tools offered for {len(CORPUS)} example requests: mean schema payload "
             f"{statistics.mean(sizes) / 1024:.1f} KB, not {full_bytes / 1024:.1f} KB ({saving:.0f}% less).",
             [(str(k), cnt.get(k, 0)) for k in range(1, max(offered) + 1)],
             "Measured by docs/charts/make_charts.py · tools.select_tools on the README's example requests",
             xlabel="tools offered for one request")


# ============================================================ 4. the data
print("\n4 · the seeded institution")
dept_n = agents.rows(con, "SELECT dept, COUNT(*) n FROM students GROUP BY dept ORDER BY n DESC")
R["data"] = {"students_by_dept": {r["dept"]: r["n"] for r in dept_n}}
hbar_chart("students_by_dept.svg", "Students by department",
           f"{sum(r['n'] for r in dept_n):,} synthetic students across semesters 3, 5 and 7.",
           [(r["dept"], r["n"]) for r in dept_n],
           "Measured by docs/charts/make_charts.py · students table, generated by db.py", label_w=80)

att = [r["attendance"] for r in agents.rows(con, "SELECT attendance FROM students")]
below = sum(1 for a in att if a < 75)
R["data"]["attendance"] = {"n": len(att), "mean": round(statistics.mean(att), 1), "below_75": below}
bins = collections.Counter(int(a // 5) * 5 for a in att)


def cut75(s, x0, x1, top, bottom, band):
    lo_b = min(bins)
    x = x0 + band * ((75 - lo_b) / 5)
    s.line(x, top - 4, x, bottom, "rule")
    s.text(x - 6, top + 8, f"75% SEE bar · {below} below", "t1", "end")


column_chart("attendance.svg", "Attendance, one draw per student",
             f"Mean {statistics.mean(att):.1f}%. One gauss(80, 12) draw clamped to 46–99, uncorrelated with anything else.",
             [(f"{b}", bins.get(b, 0)) for b in range(min(bins), 100, 5)],
             "Measured by docs/charts/make_charts.py · students.attendance (lower edge of each 5-point bin)",
             xlabel="attendance %", marker=cut75)

mix = agents.rows(con, """SELECT sem, kind, COUNT(*) n, COUNT(DISTINCT dept||section) secs
                          FROM timetable GROUP BY sem, kind""")
secs = {r["sem"]: 0 for r in mix}
for r in agents.rows(con, "SELECT sem, COUNT(DISTINCT dept||section) c FROM timetable GROUP BY sem"):
    secs[r["sem"]] = r["c"]
per = collections.defaultdict(dict)
for r in mix:
    per[r["sem"]][r["kind"]] = r["n"] / secs[r["sem"]]
R["data"]["periods_per_section_week"] = {str(k): {kk: round(vv, 1) for kk, vv in v.items()} for k, v in per.items()}
R["data"]["timetable_rows"] = sum(r["n"] for r in mix)
KIND = [("T", "theory", "s1"), ("L", "lab", "s2"), ("A", "activity", "s3")]
rowh, t, top_ = 40, 20, 100
h = top_ + rowh * 3 + 44
s = Svg("A week for one class, by semester",
        "Periods per section per week. Seniors leave early for projects; gaps become activities, never blanks.", h)
x0, x1 = 110, W - 60
vmax = nice_max(max(sum(v.values()) for v in per.values()))
for k in range(5):
    gx = x0 + (x1 - x0) * k / 4
    s.line(gx, top_ - 6, gx, top_ + rowh * 3 - 6, "grid")
    s.text(gx, top_ + rowh * 3 + 10, tick(vmax * k / 4), "mut num", "middle")
for i, sem in enumerate(sorted(per)):
    y = top_ + i * rowh
    s.text(x0 - 10, y + t - 5, f"Semester {sem}", "t1", "end")
    x = x0
    for j, (k, nm, cls) in enumerate(KIND):
        v = per[sem].get(k, 0)
        wv = (x1 - x0) * v / vmax
        if j < 2:
            s.raw(f'<rect class="{cls}" x="{x:.1f}" y="{y}" width="{max(wv - 2, 0):.1f}" height="{t}"/>')
        else:
            s.hbar(x, y, wv, t, cls)
        if wv > 34:
            s.raw(f'<text x="{x + wv / 2 - 1:.1f}" y="{y + t - 6}" text-anchor="middle" '
                  f'style="fill:#fff;font-size:11.5px;font-weight:600" class="num">{v:.0f}</text>')
        x += wv
    s.text(x + 6, y + t - 5, f"{sum(per[sem].values()):.0f} h", "t1 num")
s.legend(x0, 84, [(nm, cls) for _k, nm, cls in KIND])
s.save("timetable_mix.svg", "Measured by docs/charts/make_charts.py · timetable rows ÷ sections, generated by db.py")

load = agents.rows(con, """SELECT f.id, f.max_load, COUNT(t.id) n FROM faculty f
                           LEFT JOIN timetable t ON t.faculty=f.id AND t.kind!='A' GROUP BY f.id""")
util = [100 * r["n"] / r["max_load"] for r in load if r["max_load"]]
R["data"]["faculty_utilisation"] = {"n": len(util), "mean": round(statistics.mean(util), 1),
                                    "max": round(max(util), 1)}
bins = collections.Counter(min(int(u // 10) * 10, 100) for u in util)
column_chart("faculty_load.svg", "How loaded is each teacher?",
             f"Weekly curriculum periods ÷ sanctioned load, {len(util)} faculty (mean {statistics.mean(util):.0f}%, "
             f"highest {max(util):.0f}%). Activities excluded.",
             [(f"{b}", bins.get(b, 0)) for b in range(min(bins), max(bins) + 10, 10)],
             "Measured by docs/charts/make_charts.py · curriculum timetable rows ÷ faculty.max_load",
             xlabel="% of sanctioned load (lower edge of each 10-point bin)")


# =========================================================== 5. the suites
SUITES = ["mcp_parity", "campus_test", "auth_test", "deploy_test", "ranking_test", "solver_test",
          "makeup_test", "mesh_test", "academics_test", "nlu_test"]
if "--tests" in sys.argv:
    print("\n5 · running the ten no-server suites")
    got = []
    for name in SUITES:
        t0 = time.perf_counter()
        p = subprocess.run([sys.executable, os.path.join(ROOT, "tests", name + ".py")], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        m = re.search(r"(\d+) passed, (\d+) failed", p.stdout)
        passed, failed = (int(m.group(1)), int(m.group(2))) if m else (0, -1)
        got.append({"suite": name, "passed": passed, "failed": failed, "exit": p.returncode,
                    "seconds": round(time.perf_counter() - t0, 1)})
        print(f"  {name:16s} {passed:4d} passed, {failed} failed, exit {p.returncode}")
    R["tests"] = got
else:
    try:
        with open(os.path.join(HERE, "results.json"), encoding="utf-8") as f:
            R["tests"] = json.load(f).get("tests", [])
    except (OSError, ValueError):
        R["tests"] = []
if R["tests"]:
    tot = sum(x["passed"] for x in R["tests"])
    bad = sum(max(x["failed"], 0) for x in R["tests"]) + sum(1 for x in R["tests"] if x["exit"])
    hbar_chart("tests.svg", f"{tot} assertions across {len(R['tests'])} suites"
               + (", all passing" if not bad else f", {bad} FAILING"),
               "No server, no API key, no cost. The server and LLM suites (smoke, live_llm) are not counted.",
               sorted(((x["suite"], x["passed"]) for x in R["tests"]), key=lambda kv: -kv[1]),
               "Measured by docs/charts/make_charts.py --tests · each suite's own 'N passed, M failed' line",
               label_w=130)

with open(os.path.join(HERE, "results.json"), "w", encoding="utf-8") as f:
    json.dump(R, f, indent=1)
print("\n  wrote docs/charts/results.json")
