/* VidyaERP :: Agent Mesh
   ---------------------------------------------------------------------------
   A live 3D instrument over the agent mesh. Hand-rolled perspective projection
   on a plain 2D canvas: no three.js, no CDN, because the console loads nothing
   external and has to keep working offline on a locked-down college LAN.

   Truthfulness rules, the reason this file is shaped the way it is:
     * Every packet is one step of a REAL execution trace returned by the
       server, replayed in order. There is no decorative traffic. The only
       motion that is not a trace step is the Supervisor breathing while a
       request is in flight - and the camera.
     * A fault is drawn only where the server said "error": a failed tool call
       (pinned on the agent that owns the tool), a failed post-write
       verification, an LLM transport failure. A write held by PolicyGuard is
       the guard doing its job and is drawn as a shield, never as a fault.
     * Statistics are counted once, when a trace arrives. Replays are visual.

   Model: nodes/edges/runs/incidents + a playback clock (pause, step, speed).
   View:  camera, picking, drawing - one for the chat rail, one for the stage.
*/
(function () {
'use strict';

// Light mode only. Group colours are kept clear of the four status colours:
// red is a fault, amber a warning, violet a write held for approval, green a
// write that landed and was verified.
const GROUPS = {
  core: {c: '#2342A8', label: 'Supervisor and routing'},
  gov:  {c: '#161922', label: 'Guard and audit'},
  acad: {c: '#0E7490', label: 'Academic'},
  stud: {c: '#BE185D', label: 'Students and finance'},
  camp: {c: '#8A6D1F', label: 'Campus services'},
  ops:  {c: '#64748B', label: 'Operations'},
};
const RED = '#D93A2B', AMBER = '#D97706', GREEN = '#1C7A4B', VIOLET = '#6A3DB8';

// Every agent that can appear in a trace. tests/campus_test.py checks that the
// server's tool -> agent map never names an agent missing from this catalog.
const CATALOG = {
  'Supervisor':        {g: 'core', role: 'Receives every request, routes it through the mesh and composes the answer.', ask: 'What needs my attention today?'},
  'Router':            {g: 'core', role: 'Rule-engine intent classifier: keyword scoring plus regex priority rules.'},
  'ToolRouter':        {g: 'core', role: 'Narrows 47 tools to the handful an utterance can need, and picks one on the rule engine.'},
  'EntityResolver':    {g: 'core', role: 'Resolves faculty names, USNs, departments, semesters and Indian date phrases.'},
  'LLM Planner':       {g: 'core', role: 'The language model planning tool calls (LLM engine only).'},
  'ToolBus':           {g: 'core', role: 'Carries each tool call from the planner to the agent that owns it.'},
  'MCP':               {g: 'core', role: 'External MCP clients reaching the same tools through the same gate.'},
  'PolicyGuard':       {g: 'gov',  role: 'Holds every write until the admin approves in their own words. A hold is the guard working, not a fault.'},
  'Auditor':           {g: 'gov',  role: 'Immutable ledger: who asked, which agent acted, what changed.'},
  'TimetableAgent':    {g: 'acad', role: 'Timetables, free faculty and rooms, and from-scratch generation with post-write verification.', ask: 'Show CSE 5th sem A timetable'},
  'SubstitutionAgent': {g: 'acad', role: 'Turns a faculty absence into three measured, ranked coverage plans.'},
  'WorkloadBalancer':  {g: 'acad', role: 'Checks how many extra hours a coverage plan puts on each substitute.'},
  'PlanRanker':        {g: 'acad', role: 'Ranks plans by 0.8·confidence + 0.2·continuity; coverage is a floor.'},
  'FacultyAgent':      {g: 'acad', role: 'Faculty directory, workload and subject allocation.', ask: 'Faculty workload above 90%'},
  'ExamAgent':         {g: 'acad', role: 'SEE calendar, eligibility and invigilation.', ask: 'SEE eligibility check'},
  'HRAgent':           {g: 'acad', role: 'Leave ledger; prices each pending leave by running the substitution planner.', ask: 'Review pending leave applications'},
  'StudentAgent':      {g: 'stud', role: 'Student records, attendance defaulters and academic risk.', ask: 'Attendance defaulters in CSE sem 5'},
  'AttendanceAgent':   {g: 'stud', role: 'Subject-wise attendance, reconciled to each student\'s headline figure.', ask: 'Everything about 4VP24CS017'},
  'RiskAgent':         {g: 'stud', role: 'Buckets attendance risk by severity.'},
  'FinanceAgent':      {g: 'stud', role: 'Fee dues and collections; the accounts ledger in no-dues checks.', ask: 'Fee dues by department'},
  'LibraryAgent':      {g: 'camp', role: 'Catalogue, circulation rules, overdue fines and reminder runs.', ask: 'Library status'},
  'HostelAgent':       {g: 'camp', role: 'Occupancy, waitlist allotment and complaint dispatch.', ask: 'Hostel status'},
  'TransportAgent':    {g: 'camp', role: 'Seat loads, rebalancing at shared stops, breakdown cover.', ask: 'Transport status'},
  'GatePassAgent':     {g: 'camp', role: 'Screens every gate pass against written policy.', ask: 'Pending gate passes'},
  'PlacementAgent':    {g: 'camp', role: 'Drive eligibility and shortlists.', ask: 'Placement status'},
  'DocumentAgent':     {g: 'camp', role: 'No-dues clearance and certificates with register serials.', ask: 'Certificate register'},
  'RequestAgent':      {g: 'ops',  role: 'Approvals inbox: raise, route and decide requests.', ask: 'Pending approvals in my inbox'},
  'SLAMonitor':        {g: 'ops',  role: 'Flags requests past their service level.'},
  'NotifyAgent':       {g: 'ops',  role: 'Drafts and dispatches notices. Recorded, not delivered - there is no gateway.'},
  'AnalyticsAgent':    {g: 'ops',  role: 'Institution KPIs and the daily brief.', ask: 'Brief me on today\'s institution status'},
  'OpsRadar':          {g: 'ops',  role: 'Asks every agent what needs a human today and ranks the answers.', ask: 'What needs my attention today?'},
};
const LAYOUTS = ['sphere', 'rings', 'flow', 'clusters'];
const STORE = 'vidyaerp.mesh.v2';
const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const $ = id => document.getElementById(id);

/* =============================================================== model */
const M = {
  nodes: {}, edges: {}, runs: [], incidents: [], queue: [], cur: null,
  packets: [], fx: [], clock: 0, speed: 1, playing: true, flight: false,
  layout: 'sphere', hidden: new Set(), heat: false, labels: true, sel: null, hover: null, query: '',
  ctr: {runs: 0, hops: 0, tools: 0, held: 0, committed: 0, verified: 0, faults: 0, warnings: 0},
  caption: null, seq: 0,
};

function node(name) {
  if (M.nodes[name]) return M.nodes[name];
  const c = CATALOG[name] || {g: 'ops', role: 'An agent this console has not catalogued yet.'};
  const n = M.nodes[name] = {name, g: c.g, role: c.role, ask: c.ask || null, p: [0, 0, 0], t: [0, 0, 0],
    heat: 0, ok: 0, warn: 0, err: 0, held: 0, runs: new Set(), recent: [], lat: 0, latN: 0,
    act: -1e9, fault: 0, faultAt: -1e9, warnAt: -1e9, shieldAt: -1e9, okAt: -1e9, col: null};
  layout(M.layout);              // the others glide to their new places; this one appears in place
  n.p = n.t.slice();
  return n;
}

function classify(step) {
  const a = String(step.action || '');
  if (step.status === 'error') return 'error';
  if (step.status === 'warn') return 'warn';
  if (a.includes('write_blocked')) return 'held';
  if (a.includes('write_authorisation') || (a === 'verify' && step.status !== 'error')) return 'commit';
  return 'ok';
}

// Count once, on arrival from the server. Replays never pass through here.
function ingest(trace, label, summary, opts) {
  opts = opts || {};
  if (!trace || !trace.length) return null;
  const steps = trace.map(t => ({agent: t.agent || 'Unknown', action: t.action || '', detail: t.detail || '',
    status: t.status || 'ok', ms: +t.ms || 0}));
  steps.forEach(s => s.kind = classify(s));
  const run = {id: ++M.seq, label: label || 'request', summary: summary || '', at: opts.at || Date.now(), steps,
    faults: steps.filter(s => s.kind === 'error').length, warns: steps.filter(s => s.kind === 'warn').length};
  M.runs.push(run);
  if (M.runs.length > 40) M.runs.shift();
  M.ctr.runs++;
  let prev = 'Supervisor';
  steps.forEach((s, i) => {
    const n = node(s.agent);
    n.heat++; n.runs.add(run.id);
    n[s.kind === 'error' ? 'err' : s.kind === 'warn' ? 'warn' : s.kind === 'held' ? 'held' : 'ok']++;
    const next = steps[i + 1];
    if (next && next.ms >= s.ms) { n.lat += next.ms - s.ms; n.latN++; }
    n.recent.unshift({action: s.action, detail: s.detail, kind: s.kind, run: run.id});
    if (n.recent.length > 14) n.recent.pop();
    if (prev !== s.agent) {
      const k = [prev, s.agent].sort().join('|');
      const e = M.edges[k] || (M.edges[k] = {a: prev < s.agent ? prev : s.agent, b: prev < s.agent ? s.agent : prev, n: 0});
      e.n++;
    }
    M.ctr.hops++;
    if (s.agent === 'ToolBus' || (s.agent === 'ToolRouter' && s.action === 'select')) M.ctr.tools++;
    if (s.kind === 'held') M.ctr.held++;
    if (s.action.includes('write_authorisation')) M.ctr.committed++;
    if (s.action === 'verify' && s.kind !== 'error') M.ctr.verified++;
    if (s.kind === 'error' || s.kind === 'warn') {
      M.ctr[s.kind === 'error' ? 'faults' : 'warnings']++;
      M.incidents.unshift({id: M.incidents.length + 1, kind: s.kind, agent: s.agent, action: s.action,
        detail: s.detail, run: run.id, label: run.label, at: run.at, ack: !!opts.restored});
    }
    prev = s.agent;
  });
  if (M.incidents.length > 60) M.incidents.length = 60;
  if (!opts.restored) { M.queue.push(run); save(); }
  UI.dirty = true;
  return run;
}

function save() {
  try {
    sessionStorage.setItem(STORE, JSON.stringify(M.runs.slice(-30).map(r => ({label: r.label, summary: r.summary,
      at: r.at, steps: r.steps.map(s => ({agent: s.agent, action: s.action, detail: s.detail.slice(0, 200),
        status: s.status, ms: s.ms}))}))));
  } catch (e) { /* private window or quota: history is a convenience */ }
}
function restore() {
  try {
    const rs = JSON.parse(sessionStorage.getItem(STORE) || '[]');
    rs.forEach(r => ingest(r.steps, r.label, r.summary, {restored: true, at: r.at}));
  } catch (e) { /* ignore a corrupt store */ }
}

/* ------------------------------------------------------------ playback */
function gapFor(run) { return clamp(2600 / run.steps.length, 120, 330); }

function startNext() {
  const run = M.queue.shift();
  if (!run) { M.cur = null; return; }
  const gap = gapFor(run);
  M.cur = {run, i: 0, t0: M.clock, gap, travel: gap * 1.55, prev: 'Supervisor', arrived: 0};
  UI.dirty = true;
}
function replay(run) {
  M.queue = [run]; M.cur = null; M.packets = []; M.playing = true; startNext();
}
function tick(dt) {
  if (M.playing) M.clock += dt * M.speed;
  if (!M.cur && M.queue.length) startNext();
  const c = M.cur;
  if (c) {
    while (c.i < c.run.steps.length && M.clock >= c.t0 + c.i * c.gap) {
      const s = c.run.steps[c.i];
      const a = node(c.prev), b = node(s.agent);
      const start = c.t0 + c.i * c.gap;
      if (a !== b) M.packets.push({a, b, start, dur: c.travel, s, run: c.run, idx: c.i});
      else arrive(b, s, c.run, c.i, start);
      c.prev = s.agent; c.i++;
    }
  }
  M.packets = M.packets.filter(p => {
    if (M.clock >= p.start + p.dur) { arrive(p.b, p.s, p.run, p.idx, p.start + p.dur); return false; }
    return true;
  });
  if (c && c.i >= c.run.steps.length && !M.packets.length) {
    M.cur = null; UI.dirty = true;
    if (M.queue.length) startNext();
  }
  M.fx = M.fx.filter(f => M.clock - f.t < f.life);
}
function arrive(n, s, run, idx, t) {
  n.act = t; n.col = s.kind;
  if (s.kind === 'error') { n.fault++; n.faultAt = t; M.fx.push({n, t, life: 1600, kind: 'error'}); }
  else if (s.kind === 'warn') { n.warnAt = t; M.fx.push({n, t, life: 1200, kind: 'warn'}); }
  else if (s.kind === 'held') { n.shieldAt = t; M.fx.push({n, t, life: 1500, kind: 'held'}); }
  else if (s.kind === 'commit') { n.okAt = t; M.fx.push({n, t, life: 1300, kind: 'commit'}); }
  else M.fx.push({n, t, life: 900, kind: 'ok'});
  if (M.cur && M.cur.run === run) M.cur.arrived = Math.max(M.cur.arrived, idx + 1);
  M.caption = {s, run, idx};
  UI.caption();
}
// Paused single-step: advance exactly to the next arrival.
function stepOnce() {
  M.playing = false;
  if (!M.cur && M.queue.length) startNext();
  const c = M.cur;
  if (!c) return;
  if (!M.packets.length && c.i < c.run.steps.length) { M.clock = Math.max(M.clock, c.t0 + c.i * c.gap); tick(0); }
  if (M.packets.length) { M.clock = Math.max(M.clock, Math.min(...M.packets.map(p => p.start + p.dur))); tick(0); }
  UI.dirty = true;
}
function acknowledge(name) {
  M.incidents.forEach(i => { if (!name || i.agent === name) i.ack = true; });
  Object.values(M.nodes).forEach(n => { if (!name || n.name === name) n.fault = 0; });
  UI.dirty = true;
}
const faulted = () => Object.values(M.nodes).filter(n => n.fault > 0);

/* -------------------------------------------------------------- layouts */
function layout(kind, instant) {
  M.layout = kind;
  const all = Object.values(M.nodes);
  const byG = g => all.filter(n => n.g === g && n.name !== 'Supervisor');
  const set = (n, v) => { n.t = v; if (instant) n.p = v.slice(); };
  const sup = M.nodes.Supervisor;
  const ring = (list, r, y, tilt, phase) => list.forEach((n, i) => {
    const a = (i / Math.max(1, list.length)) * Math.PI * 2 + (phase || 0);
    set(n, [Math.cos(a) * r, y + Math.sin(a * 2) * (tilt || 0), Math.sin(a) * r]);
  });
  if (kind === 'sphere') {
    if (sup) set(sup, [0, 0, 0]);
    ring([...byG('core'), ...byG('gov')], .46, 0, .08);
    const outer = [...byG('acad'), ...byG('stud'), ...byG('camp'), ...byG('ops')];
    const N = outer.length, ga = Math.PI * (3 - Math.sqrt(5));
    outer.forEach((n, i) => { const y = 1 - (i + .5) / N * 2, r = Math.sqrt(1 - y * y), t = ga * i;
      set(n, [Math.cos(t) * r, y * .92, Math.sin(t) * r]); });
  } else if (kind === 'rings') {
    if (sup) set(sup, [0, 1.02, 0]);
    const tiers = {core: [.62, .45], gov: [.32, .26], acad: [0, 1.0], stud: [-.32, .76], camp: [-.62, .96], ops: [-.9, .62]};
    Object.entries(tiers).forEach(([g, [y, r]], k) => ring(byG(g), r, y, 0, k * .4));
  } else if (kind === 'flow') {
    if (sup) set(sup, [-1.3, 0, 0]);
    const lane = (list, x, r) => list.forEach((n, i) => { const a = (i / Math.max(1, list.length)) * Math.PI * 2;
      set(n, [x, Math.cos(a) * r, Math.sin(a) * r]); });
    lane(byG('core'), -.78, .38); lane(byG('gov'), -.3, .2);
    lane([...byG('acad'), ...byG('stud'), ...byG('camp')], .42, .92); lane(byG('ops'), 1.18, .38);
  } else {
    if (sup) set(sup, [0, 0, 0]);
    const centres = {core: [0, .55, 0], gov: [0, -.55, 0], acad: [.95, .1, 0], stud: [-.95, .1, 0], camp: [0, .1, .95], ops: [0, .1, -.95]};
    Object.entries(centres).forEach(([g, c]) => {
      const list = byG(g), N = list.length, ga = Math.PI * (3 - Math.sqrt(5)), r = g === 'core' || g === 'gov' ? .2 : .32;
      list.forEach((n, i) => { const y = 1 - (i + .5) / Math.max(1, N) * 2, rr = Math.sqrt(Math.max(0, 1 - y * y)), t = ga * i;
        set(n, [c[0] + Math.cos(t) * rr * r, c[1] + y * r, c[2] + Math.sin(t) * rr * r]); });
    });
  }
  UI.dirty = true;
}

function neighbours(name) {
  const s = new Set([name]);
  Object.values(M.edges).forEach(e => { if (e.a === name) s.add(e.b); if (e.b === name) s.add(e.a); });
  return s;
}

/* ================================================================ view */
class View {
  constructor(canvas, big) {
    this.cv = canvas; this.ctx = canvas.getContext('2d'); this.big = big;
    this.yaw = .6; this.pitch = -.3; this.zoom = big ? 1 : 1; this.px = 0; this.py = 0;
    this.goal = null; this.auto = !RM; this.lastInput = 0; this.proj = {};
    new ResizeObserver(() => this.fit()).observe(canvas.parentElement);
    this.fit();
  }
  fit() {
    const d = devicePixelRatio || 1, el = this.cv.parentElement, w = el.clientWidth, h = el.clientHeight;
    if (!w || !h) return;
    this.cv.width = w * d; this.cv.height = h * d; this.W = w; this.H = h; this.ctx.setTransform(d, 0, 0, d, 0, 0);
  }
  visible() { return this.W && this.cv.offsetParent !== null; }
  project(p) {
    const cy = Math.cos(this.yaw), sy = Math.sin(this.yaw), cp = Math.cos(this.pitch), sp = Math.sin(this.pitch);
    const x1 = p[0] * cy + p[2] * sy, z1 = -p[0] * sy + p[2] * cy, y2 = p[1] * cp - z1 * sp, z2 = p[1] * sp + z1 * cp;
    const F = 3.2, s = F / Math.max(.4, F + z2), R = Math.min(this.W, this.H) * (this.big ? .33 : .31) * this.zoom;
    return {x: this.W / 2 + this.px + x1 * R * s, y: this.H / 2 + this.py + (this.big ? 6 : 10) - y2 * R * s, s, z: z2};
  }
  focus(n) {
    const [x, y, z] = n.t, r = Math.hypot(x, z) || 1e-6;
    let yaw = Math.atan2(x, -z);
    while (yaw - this.yaw > Math.PI) yaw -= 2 * Math.PI;
    while (this.yaw - yaw > Math.PI) yaw += 2 * Math.PI;
    this.goal = {yaw: n.name === 'Supervisor' ? this.yaw : yaw, pitch: clamp(Math.atan2(-y, r), -1.1, 1.1) * .8,
      zoom: Math.max(this.zoom, 1.25), px: 0, py: 0};
    this.lastInput = performance.now();
  }
  reset() { this.goal = {yaw: .6, pitch: -.3, zoom: 1, px: 0, py: 0}; this.lastInput = performance.now(); }
  pick(mx, my) {
    let best = null, bd = 16;
    for (const n of Object.values(M.nodes)) {
      if (M.hidden.has(n.g)) continue;
      const q = this.proj[n.name]; if (!q) continue;
      const d = Math.hypot(q.x - mx, q.y - my) - (q.r || 0) * .6;
      if (d < bd) { bd = d; best = n; }
    }
    return best;
  }
  step(dt, now) {
    if (this.goal) {
      const k = 1 - Math.exp(-dt / 180);
      for (const f of ['yaw', 'pitch', 'zoom', 'px', 'py']) this[f] += (this.goal[f] - this[f]) * k;
      if (Math.abs(this.goal.yaw - this.yaw) < .002 && Math.abs(this.goal.zoom - this.zoom) < .002) this.goal = null;
    } else if (this.auto && now - this.lastInput > 2500 && !(this.big && M.sel)) {
      this.yaw += (M.flight ? .0075 : .0022) * (dt / 16.7);
    }
  }
  draw(now) {
    if (!this.visible()) return;
    const c = this.ctx, W = this.W, H = this.H, T = M.clock;
    // background painted into the canvas so an exported PNG looks like the screen
    c.fillStyle = '#FFFFFF'; c.fillRect(0, 0, W, H);
    c.fillStyle = '#E3E7EE';                      // a quiet dot grid: depth comes from the scene, not the backdrop
    const g0 = 24, ox = (this.px % g0 + g0) % g0, oy = (this.py % g0 + g0) % g0;
    for (let x = ox; x < W; x += g0) for (let y = oy; y < H; y += g0) c.fillRect(x, y, 1.2, 1.2);
    this.guides(c);
    const P = {}; for (const n of Object.values(M.nodes)) P[n.name] = this.project(n.p);
    this.proj = P;
    const focusSet = M.hover ? neighbours(M.hover.name) : M.sel ? neighbours(M.sel.name) : null;
    const q = M.query.trim().toLowerCase();
    const dim = n => (M.hidden.has(n.g) ? 0 : (q && !n.name.toLowerCase().includes(q)) ? .16 :
      (focusSet && !focusSet.has(n.name)) ? .22 : 1);
    const fog = z => clamp(.55 + .45 * (1 - (z + 1.2) / 2.4), .3, 1);
    // hub spokes, faint
    const sup = P.Supervisor;
    if (sup) for (const n of Object.values(M.nodes)) {
      if (n.name === 'Supervisor' || M.hidden.has(n.g)) continue;
      const p = P[n.name]; c.strokeStyle = `rgba(94,100,114,${.07 * dim(n)})`; c.lineWidth = 1;
      c.beginPath(); c.moveTo(sup.x, sup.y); c.lineTo(p.x, p.y); c.stroke();
    }
    // learned edges: curved, thickness by traffic
    const maxE = Math.max(1, ...Object.values(M.edges).map(e => e.n));
    for (const e of Object.values(M.edges)) {
      const A = M.nodes[e.a], B = M.nodes[e.b]; if (!A || !B || M.hidden.has(A.g) || M.hidden.has(B.g)) continue;
      const lit = focusSet ? (focusSet.has(e.a) && focusSet.has(e.b) && (e.a === (M.hover || M.sel).name || e.b === (M.hover || M.sel).name)) : true;
      const w = e.n / maxE, alpha = (lit ? .12 + .45 * w : .04) * Math.min(dim(A), dim(B)) ;
      c.strokeStyle = M.heat ? `rgba(217,${Math.round(140 - 80 * w)},${Math.round(40 - 20 * w)},${alpha + .08})` : `rgba(35,66,168,${alpha})`;
      c.lineWidth = (M.heat ? .8 + 4 * w : .7 + 2.2 * w) * (this.big ? 1 : .7);
      this.curve(c, A.p, B.p, 18);
    }
    c.lineWidth = 1;
    // packets
    for (const p of M.packets) {
      if (T < p.start) continue;
      const u = clamp((T - p.start) / p.dur, 0, 1), e = u < .5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2;
      const col = p.s.kind === 'error' ? RED : p.s.kind === 'warn' ? AMBER : p.s.kind === 'held' ? VIOLET : p.s.kind === 'commit' ? GREEN : GROUPS[p.b.g] ? GROUPS[p.b.g].c : '#2342A8';
      for (let j = 12; j >= 0; j--) {
        const uu = Math.max(0, e - j * .03), qp = this.project(this.arc(p.a.p, p.b.p, uu));
        c.globalAlpha = (1 - j / 13) * .85; c.fillStyle = col;
        c.beginPath(); c.arc(qp.x, qp.y, (j ? 1.7 : 3.6) * qp.s * (this.big ? 1.15 : .9), 0, 6.2832); c.fill();
      }
    }
    c.globalAlpha = 1;
    // nodes, far to near
    const order = Object.values(M.nodes).filter(n => !M.hidden.has(n.g)).sort((a, b) => P[b.name].z - P[a.name].z);
    const maxHeat = Math.max(1, ...order.map(n => n.heat));
    for (const n of order) {
      const pp = P[n.name], d = dim(n), f = fog(pp.z);
      const flash = clamp(1 - (T - n.act) / 1100, 0, 1);
      const faulty = n.fault > 0, blink = faulty ? .5 + .5 * Math.sin(now / 95) : 0;
      const warnBlink = T - n.warnAt < 3200 ? .5 + .5 * Math.sin(now / 110) : 0;
      let base = n.name === 'Supervisor' ? 8 : 4.8;
      base += M.heat ? Math.sqrt(n.heat / maxHeat) * 9 : Math.min(3.2, n.heat * .22);
      let r = base * pp.s * (this.big ? 1.3 : .92);
      if (faulty) r *= 1.75 + .35 * blink;
      else if (warnBlink) r *= 1.25 + .15 * warnBlink;
      pp.r = r;
      let col = M.heat ? heatColor(n.heat / maxHeat) : (GROUPS[n.g] || GROUPS.ops).c;
      if (faulty) col = blink > .5 ? RED : '#ff9aa5';
      else if (warnBlink) col = AMBER;
      else if (flash > .02 && n.col === 'commit') col = GREEN;
      else if (flash > .02 && n.col === 'held') col = VIOLET;
      const breathe = n.name === 'Supervisor' ? (M.flight ? .55 + .45 * Math.sin(now / 130) : .12 + .08 * Math.sin(now / 900)) : 0;
      // halo
      const hr = r * (3 + flash * 2.8 + (faulty ? 1.6 * blink : 0) + breathe * 1.5);
      const g = c.createRadialGradient(pp.x, pp.y, 0, pp.x, pp.y, hr);
      g.addColorStop(0, hexA(col, .28)); g.addColorStop(1, hexA(col, 0));
      c.globalAlpha = d * f * (.25 + .6 * flash + (faulty ? .6 : 0) + breathe * .5);
      c.fillStyle = g; c.beginPath(); c.arc(pp.x, pp.y, hr, 0, 6.2832); c.fill();
      // shaded ball
      const sh = c.createRadialGradient(pp.x - r * .35, pp.y - r * .4, r * .1, pp.x, pp.y, r);
      sh.addColorStop(0, hexA(col, .55)); sh.addColorStop(.35, col); sh.addColorStop(1, shade(col, .72));
      c.globalAlpha = d * f; c.fillStyle = sh; c.beginPath(); c.arc(pp.x, pp.y, r, 0, 6.2832); c.fill();
      c.strokeStyle = '#FFFFFF'; c.lineWidth = Math.max(1, r * .22); c.stroke(); c.lineWidth = 1;
      if (faulty) {                                   // pulsing alarm rings + badge
        for (let k = 0; k < 2; k++) {
          const u = ((now / 900) + k / 2) % 1;
          c.strokeStyle = RED; c.globalAlpha = d * (1 - u) * .9; c.lineWidth = 2;
          c.beginPath(); c.arc(pp.x, pp.y, r * (1.3 + u * 2.6), 0, 6.2832); c.stroke();
        }
        c.globalAlpha = d; c.fillStyle = RED; c.beginPath(); c.arc(pp.x + r * .85, pp.y - r * .85, Math.max(6, r * .45), 0, 6.2832); c.fill();
        c.fillStyle = '#fff'; c.font = `700 ${Math.max(8, r * .55)}px "Segoe UI",sans-serif`; c.textAlign = 'center'; c.textBaseline = 'middle';
        c.fillText(n.fault > 9 ? '9+' : String(n.fault), pp.x + r * .85, pp.y - r * .82); c.textAlign = 'left'; c.textBaseline = 'alphabetic';
      }
      if (M.sel === n) {                              // rotating selection reticle
        c.globalAlpha = 1; c.strokeStyle = '#161922'; c.lineWidth = 1.4; c.setLineDash([4, 5]); c.lineDashOffset = -now / 40;
        c.beginPath(); c.arc(pp.x, pp.y, r + 7, 0, 6.2832); c.stroke(); c.setLineDash([]);
      }
      c.globalAlpha = 1; c.lineWidth = 1;
    }
    // effects: shields for held writes, shockwaves for commits, rings for arrivals
    for (const fx of M.fx) {
      if (M.hidden.has(fx.n.g)) continue;
      const pp = P[fx.n.name], u = (T - fx.t) / fx.life; if (u < 0) continue;
      const r0 = (pp.r || 6);
      if (fx.kind === 'held') {
        c.strokeStyle = VIOLET; c.globalAlpha = (1 - u) * .95; c.lineWidth = 2;
        hexagon(c, pp.x, pp.y, r0 * 2.2 + u * 6); c.globalAlpha = (1 - u) * .18; c.fillStyle = VIOLET; c.fill();
      } else if (fx.kind === 'commit') {
        c.strokeStyle = GREEN; c.lineWidth = 2.2;
        for (let k = 0; k < 2; k++) { const uu = clamp(u - k * .18, 0, 1); c.globalAlpha = (1 - uu) * .9;
          c.beginPath(); c.arc(pp.x, pp.y, r0 + uu * 42, 0, 6.2832); c.stroke(); }
      } else if (fx.kind === 'error') {
        c.strokeStyle = RED; c.lineWidth = 3; c.globalAlpha = (1 - u);
        c.beginPath(); c.arc(pp.x, pp.y, r0 + u * 60, 0, 6.2832); c.stroke();
      } else {
        c.strokeStyle = fx.kind === 'warn' ? AMBER : (GROUPS[fx.n.g] || GROUPS.ops).c; c.lineWidth = 1.5; c.globalAlpha = (1 - u) * .8;
        c.beginPath(); c.arc(pp.x, pp.y, r0 + u * 24, 0, 6.2832); c.stroke();
      }
    }
    c.globalAlpha = 1; c.lineWidth = 1;
    // labels: a fault's label is drawn last so nothing sits on top of it
    for (const n of [...order].sort((a, b) => (a.fault > 0) - (b.fault > 0))) {
      const pp = P[n.name], d = dim(n);
      const flash = T - n.act < 1100, faulty = n.fault > 0;
      const show = faulty || M.sel === n || M.hover === n || (q && d === 1) ||
        (this.big ? (M.labels && (pp.s > .9 || n.heat > 0 || n.name === 'Supervisor') && d > .2) || flash
                  : flash || n.name === 'Supervisor');
      if (!show) continue;
      c.font = `${faulty || M.sel === n ? 650 : 500} ${this.big ? 12 : 10}px "Segoe UI Variable Text","Segoe UI",-apple-system,sans-serif`;
      const txt = faulty ? `${n.name}, fault` : n.name;
      const x = pp.x + (pp.r || 5) + 6, y = pp.y + 4;
      const w = c.measureText(txt).width;
      c.globalAlpha = faulty ? 1 : Math.max(.55, d); c.fillStyle = faulty ? '#FFFFFF' : 'rgba(255,255,255,.88)'; c.fillRect(x - 4, y - 12, w + 8, 16);
      c.fillStyle = faulty ? RED : '#161922';
      c.fillText(txt, x, y); c.globalAlpha = 1;
    }
  }
  guides(c) {
    c.lineWidth = 1; c.strokeStyle = 'rgba(35,66,168,.09)';
    const poly = pts => { c.beginPath(); pts.forEach((p, i) => { const q = this.project(p); i ? c.lineTo(q.x, q.y) : c.moveTo(q.x, q.y); }); c.stroke(); };
    const circ = (fn) => poly(Array.from({length: 65}, (_, i) => fn(i / 64 * 6.2832)));
    if (M.layout === 'sphere') [-.55, 0, .55].forEach(y => { const r = Math.sqrt(1 - y * y); circ(a => [Math.cos(a) * r, y * .92, Math.sin(a) * r]); });
    else if (M.layout === 'rings') [[.62, .45], [0, 1], [-.32, .76], [-.62, .96]].forEach(([y, r]) => circ(a => [Math.cos(a) * r, y, Math.sin(a) * r]));
    else if (M.layout === 'flow') [[-.78, .38], [.42, .92], [1.18, .38]].forEach(([x, r]) => circ(a => [x, Math.cos(a) * r, Math.sin(a) * r]));
    else circ(a => [Math.cos(a) * .95, .1, Math.sin(a) * .95]);
  }
  arc(A, B, u) {
    const m = [(A[0] + B[0]) / 2, (A[1] + B[1]) / 2 + .16, (A[2] + B[2]) / 2], L = Math.hypot(...m) || 1, k = Math.max(1, .62 / L) * 1.12;
    const C = m.map(v => v * k), w = 1 - u;
    return [0, 1, 2].map(i => w * w * A[i] + 2 * w * u * C[i] + u * u * B[i]);
  }
  curve(c, A, B, n) {
    c.beginPath();
    for (let i = 0; i <= n; i++) { const q = this.project(this.arc(A, B, i / n)); i ? c.lineTo(q.x, q.y) : c.moveTo(q.x, q.y); }
    c.stroke();
  }
}
function hexagon(c, x, y, r) { c.beginPath(); for (let i = 0; i <= 6; i++) { const a = i / 6 * 6.2832 + Math.PI / 6; i ? c.lineTo(x + Math.cos(a) * r, y + Math.sin(a) * r) : c.moveTo(x + Math.cos(a) * r, y + Math.sin(a) * r); } c.stroke(); }
function hexA(h, a) { const n = parseInt(h.slice(1), 16); return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`; }
function shade(h, k) { const n = parseInt(h.slice(1), 16); return `rgb(${Math.round((n >> 16 & 255) * k)},${Math.round((n >> 8 & 255) * k)},${Math.round((n & 255) * k)})`; }
function heatColor(t) {                                   // cool blue -> hot orange
  const a = [111, 137, 221], b = [255, 140, 70], m = a.map((v, i) => Math.round(v + (b[i] - v) * clamp(t, 0, 1)));
  return '#' + m.map(v => v.toString(16).padStart(2, '0')).join('');
}

/* ================================================================== UI */
// Only touch the DOM when the markup actually changed: rebuilding a panel under
// the pointer every frame would swallow the click the admin is making.
function setHTML(el, html) { if (el && el._h !== html) { el._h = html; el.innerHTML = html; } }
const UI = {dirty: true, mini: null, big: null, host: null, toastT: 0,
  caption() {
    const el = $('mCaption'); if (!el || !M.caption) return;
    const {s, run, idx} = M.caption, cls = {error: 'e', warn: 'w', commit: 'g', held: 'h'}[s.kind] || '';
    el.innerHTML = `<span class="${cls}">${idx + 1}/${run.steps.length}</span> · <b>${esc(s.agent)}</b> ${esc(s.action)} — ${esc(s.detail).slice(0, 140)}`;
  },
};

function inspector() {
  const el = $('mRight'); if (!el) return;
  const n = M.sel;
  if (!n) {
    const top = Object.values(M.nodes).filter(x => x.heat).sort((a, b) => b.heat - a.heat).slice(0, 10), mx = top.length ? top[0].heat : 1;
    setHTML(el, `<div class="glass"><div class="ph"><span>Most active agents</span></div>${top.length ? top.map(x =>
      `<div class="bar2" data-sel="${esc(x.name)}"><span>${esc(x.name)}</span><span class="t"><i style="width:${100 * x.heat / mx}%;background:${x.fault ? RED : (GROUPS[x.g] || GROUPS.ops).c}"></i></span><span>${x.heat}</span></div>`).join('')
      : '<div class="empty">No activity yet. Give the agents a job below, or run the probe.</div>'}</div>
      <div class="glass"><div class="ph"><span>How to read it</span></div><div class="empty">Click an agent to inspect it and fly the camera to it. Hover to light up who it talks to.
      <b style="color:var(--bad)">Blinking red and enlarged</b> = a real failure the server reported; it stays until acknowledged. <b style="color:var(--warn)">Amber</b> = a warning.
      <b style="color:var(--violet)">Violet shield</b> = PolicyGuard holding a write for your approval — the guard working. <b style="color:var(--ok)">Green wave</b> = an approved write landed and was verified.
      <br><br>Keys: <b>Space</b> play/pause · <b>N</b> step · <b>L</b> layout · <b>H</b> heat · <b>R</b> reset view · <b>F</b> full screen · <b>A</b> acknowledge · <b>Esc</b> deselect · <b>Shift/right-drag</b> pan · <b>double-click</b> reset.</div></div>`);
    return;
  }
  const nb = Object.values(M.edges).filter(e => e.a === n.name || e.b === n.name)
    .map(e => ({name: e.a === n.name ? e.b : e.a, n: e.n})).sort((a, b) => b.n - a.n);
  const state = n.fault ? `<span class="health err">● ${n.fault} unacknowledged fault${n.fault > 1 ? 's' : ''}</span>`
    : n.err ? `<span class="health warn">● ${n.err} fault${n.err > 1 ? 's' : ''}, acknowledged</span>`
    : n.warn ? `<span class="health warn">● ${n.warn} warning${n.warn > 1 ? 's' : ''}</span>`
    : n.heat ? '<span class="health ok">● healthy</span>' : '<span class="health idle">● idle this session</span>';
  const kind = {error: 'e', warn: 'w', held: 'h', commit: 'g'};
  setHTML(el, `<div class="glass">
    <div class="ag-h"><i style="background:${(GROUPS[n.g] || GROUPS.ops).c};box-shadow:0 0 10px ${(GROUPS[n.g] || GROUPS.ops).c}"></i><b>${esc(n.name)}</b><button data-m="desel" title="Close (Esc)">✕</button></div>
    <div class="ag-g">${esc((GROUPS[n.g] || GROUPS.ops).label)}</div>
    <div class="ag-role">${esc(n.role)}</div>${state}
    <div class="kv"><div><div class="l">Activations</div><div class="v">${n.heat}</div></div>
      <div><div class="l">Runs</div><div class="v">${n.runs.size}</div></div>
      <div><div class="l">Avg step</div><div class="v">${n.latN ? Math.round(n.lat / n.latN) + '<small style="font-size:10px"> ms</small>' : '—'}</div></div>
      <div><div class="l">Faults / warn</div><div class="v ${n.err ? 'r' : n.warn ? 'a' : ''}">${n.err}/${n.warn}</div></div>
      ${n.name === 'PolicyGuard' ? `<div><div class="l">Writes held</div><div class="v p">${n.held}</div></div>` : ''}</div>
    ${n.fault ? '<div style="margin-top:9px"><button class="tb red" data-m="ackone">Acknowledge faults</button></div>' : ''}
    ${n.ask ? `<div style="margin-top:9px"><button class="tb" data-ask="${esc(n.ask)}">▶ Ask: “${esc(n.ask)}”</button></div>` : ''}
    <div class="sub-h">Talks to</div><div class="chipz">${nb.length ? nb.map(x => `<button data-sel="${esc(x.name)}">${esc(x.name)} · ${x.n}</button>`).join('') : '<span class="empty">—</span>'}</div>
    <div class="sub-h">Recent actions</div><div class="acts-l">${n.recent.length ? n.recent.map(r => `<div class="${kind[r.kind] || ''}"><b>${esc(r.action)}</b> ${esc(r.detail).slice(0, 130)}</div>`).join('') : '<span class="empty">Nothing yet.</span>'}</div>
  </div>`);
}

function leftPanel() {
  const el = $('mLeft'); if (!el) return;
  const c = M.ctr, open = M.incidents.filter(i => !i.ack);
  setHTML(el, `<div class="glass"><div class="ph"><span>Session</span></div><div class="kv">
      <div><div class="l">Runs</div><div class="v">${c.runs}</div></div><div><div class="l">Agent hops</div><div class="v">${c.hops}</div></div>
      <div><div class="l">Tool calls</div><div class="v">${c.tools}</div></div><div><div class="l">Writes held</div><div class="v p">${c.held}</div></div>
      <div><div class="l">Committed</div><div class="v g">${c.committed}</div></div><div><div class="l">Verified</div><div class="v g">${c.verified}</div></div>
      <div><div class="l">Faults</div><div class="v ${c.faults ? 'r' : ''}">${c.faults}</div></div><div><div class="l">Warnings</div><div class="v ${c.warnings ? 'a' : ''}">${c.warnings}</div></div></div></div>
    <div class="glass"><div class="ph"><span>Incidents${open.length ? ` <b style="color:var(--bad)">${open.length} open</b>` : ''}</span>${open.length ? '<button data-m="ackall">Acknowledge all</button>' : ''}</div>
      ${M.incidents.length ? M.incidents.slice(0, 25).map(i => `<div class="inc ${i.kind === 'warn' ? 'warn' : ''} ${i.ack ? 'ack' : ''}" data-sel="${esc(i.agent)}">
        <b>${i.kind === 'error' ? '✕' : '!'} ${esc(i.agent)} · ${esc(i.action)}</b><small>${esc(i.detail).slice(0, 150)}</small>
        <small>run “${esc(i.label).slice(0, 40)}” · ${new Date(i.at).toLocaleTimeString()}</small></div>`).join('')
      : '<div class="empty">No failures or warnings reported this session. Run the probe to see how one looks — it sends one request designed to fail.</div>'}</div>`);
}

function runsStrip() {
  const el = $('mRuns'); if (!el) return;
  setHTML(el, M.runs.length ? M.runs.slice().reverse().map(r => `<button class="run ${M.cur && M.cur.run === r ? 'on' : ''}" data-run="${r.id}" title="${esc(r.label)} — ${r.steps.length} steps. Click to replay.">
      <span class="d ${r.faults ? 'e' : r.warns ? 'w' : ''}"></span>${esc(r.label)} · ${r.steps.length}</button>`).join('')
    : '<span class="empty">Runs appear here — click one to replay it.</span>');
}

function status() {
  const busy = M.flight || M.cur, f = faulted().length;
  const txt = M.flight ? 'thinking…' : M.cur ? `replaying ${Math.min(M.cur.arrived, M.cur.run.steps.length)}/${M.cur.run.steps.length}` :
    M.ctr.runs ? `${M.ctr.runs} run${M.ctr.runs > 1 ? 's' : ''} · ${M.ctr.hops} hops` : 'idle';
  const live = $('mLive'); if (live) { live.textContent = f ? `${f} fault${f > 1 ? 's' : ''}` : busy ? 'Live' : 'Ready'; live.className = 'live' + (f ? ' fault' : busy ? ' busy' : ''); }
  const st = $('mStat'); if (st) st.textContent = txt;
  const mini = $('meshMiniStat'); if (mini) mini.textContent = txt;
  const box = $('meshMini'); if (box) box.classList.toggle('has-fault', f > 0);
  const mf = $('meshMiniFault'); if (mf) mf.textContent = `● ${f} fault${f > 1 ? 's' : ''}`;
  const pr = $('mProg'); if (pr) pr.style.width = M.cur ? (100 * M.cur.arrived / M.cur.run.steps.length) + '%' : '0%';
  const pb = $('mPlay'); if (pb) pb.textContent = M.playing ? '❚❚' : '▶';
}
function panels() { inspector(); leftPanel(); runsStrip(); legend(); status(); }

function legend() {
  const el = $('mLegend'); if (!el) return;
  setHTML(el, Object.entries(GROUPS).map(([k, g]) => `<button data-grp="${k}" class="${M.hidden.has(k) ? 'off' : ''}" title="Show / hide"><i style="background:${g.c}"></i>${g.label}</button>`).join('') +
    `<span class="sep"></span><span class="k"><i style="background:${RED}"></i>fault</span><span class="k"><i style="background:${AMBER}"></i>warning</span>
     <span class="k"><i style="background:${VIOLET}"></i>held for approval</span><span class="k"><i style="background:${GREEN}"></i>committed / verified</span>`);
}

function toast(html) {
  const el = $('mToast'); if (!el) return;
  el.innerHTML = html; el.classList.add('show'); clearTimeout(UI.toastT);
  UI.toastT = setTimeout(() => el.classList.remove('show'), 9000);
}

function select(n, fly) {
  M.sel = n || null;
  if (n && fly !== false && UI.big) UI.big.focus(n);
  UI.dirty = true;
}

/* ------------------------------------------------------------- input */
function bindCanvas(view, isMini, onExpand) {
  const cv = view.cv; let down = null, moved = 0;
  cv.addEventListener('pointerdown', e => {
    down = {x: e.clientX, y: e.clientY, pan: e.button === 2 || e.shiftKey}; moved = 0; cv.setPointerCapture(e.pointerId);
    view.lastInput = performance.now(); view.goal = null;
  });
  cv.addEventListener('pointermove', e => {
    const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    if (down) {
      const dx = e.clientX - down.x, dy = e.clientY - down.y; moved += Math.abs(dx) + Math.abs(dy);
      if (!isMini) cv.classList.add('dragging');
      if (down.pan && !isMini) { view.px += dx; view.py += dy; }
      else { view.yaw += dx * .008; view.pitch = clamp(view.pitch + dy * .006, -1.35, 1.35); }
      down.x = e.clientX; down.y = e.clientY; view.lastInput = performance.now();
      return;
    }
    if (isMini) return;
    const n = view.pick(mx, my);
    if (n !== M.hover) { M.hover = n; }
    cv.classList.toggle('pointing', !!n);
    const tip = cv.parentElement.querySelector('.mesh-tip');
    if (!n) { tip.style.display = 'none'; return; }
    tip.style.display = 'block'; tip.style.left = mx + 'px'; tip.style.top = my + 'px';
    tip.innerHTML = `<b>${esc(n.name)}</b>${n.fault ? ' <span class="e">⚠ ' + n.fault + ' fault' + (n.fault > 1 ? 's' : '') + '</span>' : ''}
      <small>${esc((GROUPS[n.g] || GROUPS.ops).label)} · ${n.heat} activation${n.heat === 1 ? '' : 's'}${n.recent[0] ? ' · last: ' + esc(n.recent[0].action) : ''}</small><small>click to inspect</small>`;
  });
  cv.addEventListener('pointerup', e => {
    cv.classList.remove('dragging');
    const wasClick = down && moved < 5; down = null; view.lastInput = performance.now();
    if (!wasClick) return;
    if (isMini) { onExpand && onExpand(); return; }
    const r = cv.getBoundingClientRect(), n = view.pick(e.clientX - r.left, e.clientY - r.top);
    select(n === M.sel ? null : n);
  });
  cv.addEventListener('pointerleave', () => { if (!isMini) { M.hover = null; const t = cv.parentElement.querySelector('.mesh-tip'); if (t) t.style.display = 'none'; } });
  cv.addEventListener('contextmenu', e => e.preventDefault());
  cv.addEventListener('dblclick', () => { if (!isMini) { view.reset(); select(null, false); } });
  if (!isMini) cv.addEventListener('wheel', e => { e.preventDefault(); view.goal = null; view.lastInput = performance.now();
    view.zoom = clamp(view.zoom * (1 - e.deltaY * .0012), .5, 3); }, {passive: false});
}

function fullscreen(on) {
  const st = UI.host;
  if (!st) return;
  const isOn = document.fullscreenElement === st;
  if (on === undefined) on = !isOn;
  if (on && !isOn && st.requestFullscreen) st.requestFullscreen().catch(() => {});
  if (!on && isOn) document.exitFullscreen();
}

function bindStage(opts) {
  const st = UI.host;
  st.addEventListener('click', e => {
    const t = e.target.closest('[data-m],[data-sel],[data-grp],[data-run],[data-ask]'); if (!t) return;
    e.stopPropagation();          // the page's own [data-ask] handler would switch views
    if (t.dataset.sel) { select(M.nodes[t.dataset.sel]); return; }
    if (t.dataset.grp) { const g = t.dataset.grp; M.hidden.has(g) ? M.hidden.delete(g) : M.hidden.add(g);
      if (M.sel && M.hidden.has(M.sel.g)) select(null, false); UI.dirty = true; return; }
    if (t.dataset.run) { const r = M.runs.find(x => x.id === +t.dataset.run); if (r) replay(r); return; }
    if (t.dataset.ask) { opts.ask(t.dataset.ask); return; }
    const m = t.dataset.m;
    if (m === 'desel') select(null, false);
    else if (m === 'ackall') acknowledge();
    else if (m === 'ackone' && M.sel) acknowledge(M.sel.name);
    else if (m === 'heat') { M.heat = !M.heat; t.classList.toggle('on', M.heat); }
    else if (m === 'labels') { M.labels = !M.labels; t.classList.toggle('on', M.labels); }
    else if (m === 'orbit') { UI.big.auto = !UI.big.auto; t.classList.toggle('on', UI.big.auto); }
    else if (m === 'reset') { UI.big.reset(); select(null, false); }
    else if (m === 'full') fullscreen();
    else if (m === 'shot') shot();
    else if (m === 'probe') probe(opts, t);
    else if (m === 'play') { M.playing = !M.playing; }
    else if (m === 'step') stepOnce();
    else if (m === 'restart') { const r = (M.cur && M.cur.run) || M.runs[M.runs.length - 1]; if (r) replay(r); }
    UI.dirty = true;
  });
  $('mLayout').onchange = e => layout(e.target.value);
  $('mSpeed').onchange = e => { M.speed = +e.target.value; };
  $('mSearch').oninput = e => { M.query = e.target.value; const q = M.query.trim().toLowerCase();
    const hit = q && Object.values(M.nodes).filter(n => n.name.toLowerCase().includes(q));
    if (hit && hit.length === 1) select(hit[0]); };
  const go = () => { const v = $('mCmd').value.trim(); if (!v) return; $('mCmd').value = ''; opts.ask(v); };
  $('mGo').onclick = go; $('mCmd').onkeydown = e => { if (e.key === 'Enter') go(); };
  $('mToast').onclick = () => $('mToast').classList.remove('show');
  document.addEventListener('fullscreenchange', () => {
    const on = document.fullscreenElement === st, b = st.querySelector('[data-m="full"]');
    if (b) b.innerHTML = on ? 'Exit full screen <kbd>F</kbd>' : 'Full screen <kbd>F</kbd>';
    setTimeout(() => { UI.big.fit(); UI.mini.fit(); }, 60);
  });
  document.addEventListener('keydown', e => {
    const inStage = document.fullscreenElement === st || (st.offsetParent !== null);
    if (!inStage || /INPUT|TEXTAREA|SELECT/.test((e.target.tagName || ''))) return;
    const k = e.key.toLowerCase(), v = UI.big;
    if (k === ' ') { M.playing = !M.playing; e.preventDefault(); }
    else if (k === 'n') stepOnce();
    else if (k === 'f') fullscreen();
    else if (k === 'r') { v.reset(); select(null, false); }
    else if (k === 'l') { const i = (LAYOUTS.indexOf(M.layout) + 1) % LAYOUTS.length; layout(LAYOUTS[i]); $('mLayout').value = LAYOUTS[i]; }
    else if (k === 'h') { M.heat = !M.heat; const b = st.querySelector('[data-m="heat"]'); if (b) b.classList.toggle('on', M.heat); }
    else if (k === 'a') acknowledge();
    else if (k === 'escape') select(null, false);
    else if (k === '+' || k === '=') v.zoom = clamp(v.zoom * 1.12, .5, 3);
    else if (k === '-') v.zoom = clamp(v.zoom / 1.12, .5, 3);
    else if (k === 'arrowleft') v.yaw -= .12;
    else if (k === 'arrowright') v.yaw += .12;
    else if (k === 'arrowup') v.pitch = clamp(v.pitch - .1, -1.35, 1.35);
    else if (k === 'arrowdown') v.pitch = clamp(v.pitch + .1, -1.35, 1.35);
    else return;
    v.lastInput = performance.now(); UI.dirty = true;
  });
}

function shot() {
  const a = document.createElement('a');
  a.download = `vidyaerp-agent-mesh-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.png`;
  a.href = UI.big.cv.toDataURL('image/png'); a.click();
}

// Three real, read-only requests on the free deterministic engine, the last
// one deliberately asking for a student who does not exist - so fault handling
// can be seen end to end without anything being faked.
async function probe(opts, btn) {
  const qs = ['What needs my attention today?', 'Everything about 4VP24CS017', 'No dues status of 4VP99ZZ999'];
  btn.disabled = true; const sid = 'mesh-probe-' + Math.random().toString(36).slice(2, 7);
  toast('<b>Probe running</b> — 3 real read-only requests on the rule engine; the last asks for a student who does not exist.');
  try {
    for (const q of qs) {
      M.flight = true; UI.dirty = true;
      const r = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: q, session: sid, engine: 'rule'})}).then(x => x.json());
      M.flight = false;
      ingest(r.trace || [], 'probe · ' + q, summarise(r));
    }
    toast('<b>Probe finished.</b> Watch the replay: the third request fails at <b>DocumentAgent</b>, which blinks red until you acknowledge it.');
  } catch (e) { M.flight = false; toast('<b>Probe could not reach the server.</b> Nothing was sent.'); }
  btn.disabled = false;
}

function summarise(r) {
  const b = (r && r.blocks) || [];
  // the answer, not the "LLM unreachable, rule engine answered" notice above it
  const t = b.find(x => x.type === 'text' && x.md && !/Nothing has been written yet/.test(x.md) && !/^_Answered by the rule engine/.test(x.md));
  if (t) return t.md.replace(/\*\*/g, '').replace(/_/g, '').slice(0, 220);
  const titled = b.find(x => x.title);
  return titled ? titled.title : '';
}

/* ------------------------------------------------------------- loop */
// One step of model + drawing. Driven by animation frames; a timer takes over
// whenever frames stall (headless renderers, some throttled windows), so a
// queued run can never sit unplayed just because the compositor went quiet.
// ONE time source. A rAF timestamp is the frame's start and can be EARLIER than
// the timer's last performance.now(); mixing the two once drove dt negative and
// ran the playback clock backwards until it froze. dt is also clamped at 0.
function frame() { step(performance.now()); requestAnimationFrame(frame); }
setInterval(() => { const now = performance.now(); if (now - (step.t || 0) > 180) step(now); }, 100);
function step(now) {
  const dt = clamp(now - (step.t || now), 0, 64); step.t = Math.max(step.t || 0, now);
  tick(dt);
  const k = 1 - Math.exp(-dt / 240);
  for (const n of Object.values(M.nodes)) for (let i = 0; i < 3; i++) n.p[i] += (n.t[i] - n.p[i]) * k;
  for (const v of [UI.mini, UI.big]) if (v) { v.step(dt, now); v.draw(now); }
  if (UI.dirty || (M.cur && now - (step.p || 0) > 250)) { step.p = now; UI.dirty = false; panels(); }
}

/* --------------------------------------------------------------- API */
window.AgentMesh = {
  CATALOG, GROUPS,
  init(opts) {
    Object.keys(CATALOG).forEach(node);
    layout('sphere', true);
    UI.host = $('meshStage');
    UI.mini = new View($('meshMini').querySelector('canvas'), false);
    UI.big = new View($('meshCanvas'), true);
    // opts.show() un-hides the stage synchronously, so full screen can be requested
    // inside the same click - browsers refuse it outside a user gesture
    bindCanvas(UI.mini, true, () => { opts.show(); fullscreen(true); });
    bindCanvas(UI.big, false);
    bindStage(opts);
    restore();
    requestAnimationFrame(frame);
  },
  ingest(trace, label, summary) {
    const run = ingest(trace, label, summary);
    if (run && summary && UI.host && UI.host.offsetParent !== null)
      toast(`<b>${esc(label).slice(0, 80)}</b><br>${esc(summary)}${run.faults ? `<small style="color:var(--bad)">${run.faults} fault${run.faults > 1 ? 's' : ''} reported — see Incidents</small>` : ''}<small>click to dismiss</small>`);
    return run;
  },
  inflight(v) { M.flight = !!v; UI.dirty = true; },
  show() { if (UI.big) { UI.big.fit(); } UI.dirty = true; },
  fullscreen, summarise,
  layout(kind) { if (LAYOUTS.includes(kind)) { layout(kind); const el = $('mLayout'); if (el) el.value = kind; } },
  select(name) { const n = M.nodes[name]; if (n) select(n); return !!n; },
  _state: M,
};
})();
