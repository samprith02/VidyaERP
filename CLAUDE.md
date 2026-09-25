# VidyaERP — working notes for Claude

**This is the product. All work happens here.** Open Claude in this folder, not in MAWOS.

A SMART agentic ERP for an Indian engineering college (autonomous / VTU-affiliated model),
admin-first. The differentiator is not the CRUD — it is the agent mesh over the institutional
database that an administrator talks to and delegates work to.

Public, Apache 2.0: <https://github.com/samprith02/VidyaERP>

```bash
pip install -r requirements.txt
python -m uvicorn app:app --port 8000        # → http://localhost:8000
python mcp_server.py --tools                 # the same tools, over MCP (stdio)
```

Deploys from `render.yaml` as-is: Blueprint → pick the repo → paste the Groq key when prompted
(`sync: false`, so it never enters git). Health check is `/health`, which answers from the
database and 503s when it cannot.

No database server, no migrations, no API key needed. `db.seed()` builds a full synthetic
institution into SQLite on first import and the rule engine answers everything offline. Drop a
key into `.env` (gitignored) and the LLM agent takes over — same guardrails either way.

---

## Architecture in one pass

`app.py` routes → `orchestrator.py` (rule engine) **or** `llm_agent.py` (model plans and calls
tools) → `tools.py` (56 tool schemas, PolicyGuard-wrapped, role policy enforced) → `agents.py` + `campus.py`
+ `portal.py` (the specialists). Everyone signs in first (`auth.py`).

| Piece | Where | Note |
|---|---|---|
| PolicyGuard | `agents.py:84` | RBAC, scope, rupee ceilings, HITL gate. **Outside the model** |
| Substitution | `agents.py:103` | the flagship: absence → 3 ranked coverage plans |
| Timetable / Faculty / Student / Finance / Exam / Request / Notify / Analytics | `agents.py:367+` | specialists, all return structured dicts |
| Auditor | `agents.py` (`class Auditor`) | immutable ledger of who asked, which agent acted, what changed |
| Campus services | `campus.py` | library, hostel, transport, gate pass, placement, documents, leave review, Student 360, Ops radar — tools + `rule_route` |
| Campus data | `campus_data.py` | their tables + seed, own `Random`; `ensure()` is additive to an old DB |
| Sign-in | `auth.py` | principals from the data, PBKDF2, hashed session tokens, `gate()` for every route |
| Role policy | `access.py` | default-deny per role; rules that NARROW arguments or refuse |
| Self-service | `portal.py` | student / faculty / HOD tools + rule routing; acts only for `S["user"]` |
| Write gate | `guard.py` | `approved_this_turn` lives here so every tool module can import it; `tools` re-exports it |
| MCP surface | `mcp_server.py` | stdio JSON-RPC; `call_as` delegates to `tools.execute`. No SDK |
| Solver | `solver.py` | timetable generation from scratch (below) |
| NLU | `nlu.py` | intent scoring + regex priority rules + Indian date parsing |

**Two engines, one interface.** The rule engine is the default and costs nothing; the LLM agent
is opt-in. The rule engine drives `agents.py` directly through its own HITL state machine
(`orchestrator.resolve_pending`); the LLM agent goes through `tools.py`. The campus services are
the exception on the rule side: `orchestrator.h_campus` picks a tool with `campus.rule_route` and
runs it through `tools.execute`, so for them all three callers share one path. The parity that is
actually enforced is between the **LLM agent and MCP** — both reach `tools.execute` and cannot
differ, which is what `tests/mcp_parity.py` asserts.
`tools.select_tools()` narrows 56 tools to ~4–9 per utterance (a non-admin is offered exactly their role's menu) (−64% payload) — free tiers are
stingy and this is what keeps multi-hop turns inside the budget.

---

## Rules that matter here

- **Writes are guarded, and the guard is in code, not in the prompt.** *Nineteen* write tools —
  `tools.GATED_WRITES`: the five core ones (`apply_coverage_plan`, `apply_timetable_generation`,
  `decide_request`, `create_request`, `broadcast_notice`), the eleven in `campus.GATED_WRITES` and the
  three in `portal.GATED_WRITES`
  — refuse to commit without explicit admin approval *in the current turn*
  (`guard.approved_this_turn`, re-exported as `tools.approved_this_turn`). `tests/mock_llm.py` has a
  rogue-agent endpoint that tries to skip the gate and is blocked. **Never** add a write path that
  bypasses this, and never move the check into a system prompt. A new write tool must call
  `approved_this_turn(U)` **directly in its own body** (15c derives the gated set from each tool's
  bytecode — a helper that did the check would hide the tool) and be listed in `GATED_WRITES`;
  a campus write also needs an entry in `mcp_parity.py`'s `CAMPUS_ARGS` (16a fails otherwise).
- **`tools.execute` is the only dispatch point.** `llm_agent.py` and `mcp_server.call_as` both
  arrive there. A new caller that reaches `tools.FUNCS` directly re-creates the second path this
  design exists to prevent.
- **Over MCP, approval never comes from the arguments.** The gate reads `U`, the admin's own
  message. Over MCP the caller *is* the model, so there is no such message and a string in the
  arguments would be the model approving itself. `mcp_server.approval_text` ignores everything but
  a one-time code the admin minted out of band (`--approve`, or `POST /api/mcp/approval`):
  six digits, 120 s, single use. **Never** widen this to read an approval string from `args` —
  that is the one change that makes the whole parity claim false, and `mcp_parity.py:02` is the
  mutation test that proves it.
- **Propose, then commit.** Two shapes, both write nothing until approved. The core writes have a
  `plan_*` partner that stashes into `S["pending"]`. The campus writes are ONE tool each: called
  without approval it computes the change and stages `{"kind": "tool_commit", "tool", "args"}`
  (`campus._propose`); the same call with approval commits. The rule engine's `resolve_pending`
  commits a `tool_commit` by calling `tools.execute` again with the admin's "yes" as `U` — the same
  gate, never a shortcut. After committing, a campus tool re-reads what it wrote before reporting.
- **Module pages are read-only by construction.** `/api/panel/{name}` serves only names in
  `app.PANELS` (reads) and passes `U=""`. `campus_test.py:10a` fails if a gated write is ever listed.
- **Radar commands must never contain approval words.** An Ops-radar button sends its sentence as
  the admin's turn; if it said "approve", one click would commit. `campus_test.py:9e` checks every
  one. Same reason `#ask=` deep links carrying approval words are only pre-filled (`10d` keeps the
  page's copy of the regex identical to `guard.APPROVAL_RX`).
- **Never report a write as clean on the writer's own word.** `apply_timetable_generation` calls
  `solver.verify()` — an independent re-read of the committed rows — before saying it worked.
- **All data is synthetic and must stay that way.** No real institution, person, USN, email or
  phone number ever enters this repo. This is public and Apache 2.0 licensed: anything committed
  here is granted to the world. The sibling MAWOS repo got this wrong and is documented as a
  cautionary case in its `docs/OPEN_SOURCE_AUDIT.md`.
- **Dependencies must be permissively licensed.** Currently two: FastAPI (MIT) and Uvicorn
  (BSD-3). No copyleft — this is intended for commercial use, and Indian colleges procure
  on-premise, which counts as distribution. There is deliberately **no LLM SDK**: `llm.py` speaks
  OpenAI-compatible HTTP over `urllib`, so any provider works and nothing needs upgrading when a
  vendor changes its client. Think hard before adding a third dependency.
- **Everyone signs in (#27, replacing the old no-login model, #22).** Registrar → console;
  student / faculty / HOD → portal. **Demo mode is the default and publishes every password on the
  login page** (each account's own ID; `registrar`), so a public deployment in demo mode is still
  a demo over synthetic data, not a system of record — say so plainly; `deploy_test.py:4k` and
  `/health`'s `auth.passwords_published` hold it down. `VIDYAERP_DEMO_LOGINS=0` = strict mode: only
  set passwords, and `VIDYAERP_ADMIN_PASSWORD` for the Registrar. No auth library: PBKDF2 from
  `hashlib`, a random token in an HttpOnly SameSite=Lax cookie, only its SHA-256 stored.
- **Who may do what is decided in `tools.execute`, by `access.py`, per call.** Default deny: a tool
  not listed for a role is refused for it. A rule may only NARROW (fill in "me" / "my department")
  or refuse — never widen. A refusal is `DENIED` with a `warn` trace step: the guard working, not a
  tool failure. **No principal on the session = an operator** (MCP over stdio, the suites, internal
  code) and is unrestricted; the web always sets one. The rule engine never runs its admin handlers
  for a non-admin (`orchestrator.portal_turn`), and the LLM is offered only the role's tools.
- **Every route passes `auth.gate(path, user, headers, token)`** — one pure function, so
  `auth_test.py` 2a–2h check it against every route the app actually registers. A new route is
  Registrar-only unless added to `PUBLIC` or `ANY_USER`. The operator endpoints accept a Registrar
  session or `VIDYAERP_ADMIN_TOKEN`; before logins existed they were open with no token, which
  with logins would let any student wipe the database (`deploy_test.py:4a`).
- **The chat session belongs to the signed-in person, and so does the role.** `app._sid` keys it
  by user id; `/api/chat` ignores any `role` in the body. Keying by the client's session id alone
  would let anyone answer "yes" to someone else's pending write (`auth_test.py:5b`).
- **A self-service write needs its proposal staged first** (`portal._staged`). People ask for
  leave with "apply for leave", and "apply" is an approval word — without this the request
  sentence confirmed itself and the leave was filed before its timetable impact was shown. Found
  by driving the portal in a browser. The Registrar's tools keep the older rule on purpose
  ("approve 3" is an instruction). Self-service tools read the person from `S["user"]`, never from
  an argument.
- **`VIDYAERP_ADMIN_TOKEN`** remains the non-session way into the two operator endpoints — an open
  approval endpoint would hand the MCP write gate to whoever found it.
- **The real environment beats `.env`.** `llm.py`'s config reads `os.environ` first and `.env`
  second. It used to be the other way round, which meant a stale `.env` inside an image would
  silently shadow a platform dashboard with nothing in the UI to show why
  (`tests/deploy_test.py:2a`). A deployment configures this app through environment variables and
  has no `.env` to edit.
- **`static/index.html` loads nothing external** — no CDN, no fonts, no images. Keep it that way;
  it is why the console works offline and on a locked-down college network. The 3D agent mesh is
  therefore a hand-rolled perspective projection on a 2D canvas (`static/mesh.js`, served locally),
  not three.js. Every packet it draws is a real trace step — do not add decorative "ambient"
  traffic; the only non-trace motion is the Supervisor breathing while a request is in flight.
- **The mesh blinks red only because the server said so.** A failed tool call must reach the trace
  as `status: "error"` on the agent that OWNS the tool: `tools.failure_of(result)` +
  `tools.agent_of(name)`, applied by `llm_agent`'s loop and the rule engine's `orchestrator._relay`.
  A BLOCKED write is not a failure (`failure_of` returns None) and must never be drawn as one.
  Every campus `verify` step states `"ok"`/`"error"` explicitly. A new tool needs an entry in
  `tools.TOOL_AGENT` (or `campus.AGENT_OF`), and a new agent name needs an entry in `mesh.js`'s
  `CATALOG` — `tests/mesh_test.py` 1b–1d fail otherwise.
- **The mesh's clock has one time source.** `step()` runs off `performance.now()` whether driven by
  `requestAnimationFrame` or the stall-fallback timer, and `dt` is clamped at 0. Mixing a rAF
  timestamp with `performance.now()` once ran the playback clock backwards until it froze.

---

## The timetable solver (`solver.py`)

Ported from Chronos (`samprith02/chronos-teacher-erp-timetable`, the owner's own repo — that is
what makes it relicensable) and adapted to Indian-college rules: 3-period lab blocks inside one
session, per-semester day lengths, contiguous days, scope pinning.

Five phases: **Staffing → Search → Repair → Polish → Fill.** `solve()` is a generator — the
browser streams every decision over SSE onto a live grid and a synchronous caller drains the same
generator. **One code path**, so what you watch is what gets written.

Measured: 456 curriculum periods across 21 sections in ~0.3 s, 0 clashes.

Three things that will bite you:

- **MRV staleness is fine; stuck-variable thrash is not.** `dom[]` is an ordering heuristic
  refreshed only for variables a placement can affect, because `build_cands()` re-derives the
  truth for the variable actually chosen. But a variable that wipes out repeatedly must be
  **parked** (`STUCK_LIMIT`, `solver.py:551`). The reference's `recovered` flag goes true when
  some *other* frame retries, so the culprit is never parked and the search oscillates. Measured
  before the fix: 3,205 placements against 3,183 undos, 24 of 456 periods surviving, 20 s budget
  burned. After: 288 ms. Do not raise `STUCK_LIMIT` to "try harder".
- **Backtracking is conflict-directed, not chronological.** Undoing another department's theory
  period cannot free a lab room, so irrelevant frames are frozen rather than unwound
  (`related()`, `solver.py:553`). This is why it degrades gracefully instead of collapsing.
- **Capacity relaxation is surfaced, never silent.** Lab rooms seat 36, a CSE section is ~60, and
  this dataset has no lab batch splitting. The solver relaxes and names every affected class in
  `capacity_relaxed`, shown as an amber warning above the Apply button. **Do not "fix" this by
  dropping the constraint** — the honest fix is to model lab batches.

The stream is a preview and writes nothing; `POST /api/timetable/generate/apply` re-solves with
the same seed (determinism is asserted by the suite) and commits. That is why the client never
ships a timetable back.

---

## Gotchas that have cost real time

- **Never write backslashes through a shell heredoc.** On 2026-09-25 heredocs three times turned
  `\b` into a literal U+0008 and `\n` into a real newline inside a Python string. One made a test
  vacuous without anyone noticing (#47). Write such edits with the file tools;
  `campus_test.py:10f` fails if any source file carries a stray backspace.
- **To see a signed-in page headlessly, drive Edge over DevTools** (Node 22 has a native
  `WebSocket`): sign in through the real form, then evaluate and click. A plain
  `--screenshot` cannot carry a session cookie, and headless Edge stops firing animation frames
  after ~100 — which is why `mesh.js` also steps from a timer when frames stall.

- **Always pass `encoding="utf-8"` to `open()`.** Windows defaults to cp1252 and the console
  contains typographic quotes and `·` separators. `app.py` served a 500 on the whole page for
  exactly this reason.
- **Stop the server before deleting or restoring `college.db`.** A running server holds the file
  and the delete fails — on Windows `git checkout` reports `unable to unlink old ... Invalid
  argument`. Kill the uvicorn process first.
- **A deployment can silently stop tracking the repo, so `/health` reports the commit.**
  `autoDeploy: yes` / `autoDeployTrigger: commit` were set correctly and the webhook still never
  reached Render: no deploy was *created* for two commits over six days — not a failed build,
  nothing at all, just a stale box behind a green health check. Compare
  `curl .../health | grep short` against `git rev-parse --short HEAD` rather than inferring from
  behaviour. `app.resolve_build()` takes the platform's `RENDER_GIT_COMMIT` first and falls back
  to reading `.git/HEAD` directly (no subprocess, resolved once at import);
  `deploy_test.py:3q-3t` exercise the env read itself, not just the reporting — asserting on the
  module constant alone passes even when the variable is never read.
  **Still unresolved as of 2026-09-25:** the likely cause is the Render GitHub App's access to
  `samprith02/VidyaERP`, which is fixed in the GitHub/Render dashboards, not in code. Until it is,
  **every push needs a manual deploy** (Render dashboard → Manual Deploy, or the API trigger), and
  then the `/health` check above. Verified working live on 2026-09-25: `short: fe6d846`,
  `source: platform`.
- **Storage is ephemeral on a free host, and `/health` says so.** `db.seed()` rebuilds the whole
  institution on a cold start (~0.25 s with the campus services), so a restart silently discards
  every applied override.
  `VIDYAERP_DB` points the database at a mounted disk; `/health` reports `persistent: false` when
  it is not set.
- **MCP cannot reach a remote deployment.** `mcp_server.py` is stdio and opens the database beside
  it, so an approval code minted on the Render box is useless to an MCP client on a laptop. Do not
  describe the deployed console and the MCP surface as one system — they are two instances.
- **`db.seed()` re-pins `random.seed(20)` on every seed — it used to only at import.** Found
  2026-09-25 by `campus_test.py:1b`: the first seed in a process was reproducible and the second
  was not, so `/api/seed/reset` on a running server rebuilt a *different* institution from the one
  it booted with. The core tables are fingerprinted in `campus_test.py` (`CORE_BEFORE`, `1a`); if
  you change the core generator on purpose, re-record them.
- **Campus data draws from its own `Random`, after the core seed.** Never let `campus_data.py`
  touch the global stream — `1a` is what notices if it does.
- **`college.db` is gitignored on purpose.** `db.seed()` reproduces the seeded data exactly
  (verified table-by-table; `db.py` pins `random.seed(20)`), but the *file* is not byte-identical
  — SQLite page layout is not reproducible, and a tracked DB accumulates runtime rows. The copy
  this replaced had five stray `audit` entries committed by accident. Do not commit it.
- **`tests/smoke.py` hardcodes port 8000** and needs the server already running.
- **The no-server suites set their own stdout encoding; the two server/LLM ones do not.**
  `mcp_parity`, `ranking_test` and `deploy_test` call
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` before they print anything, because
  Windows encodes a piped stdout as cp1252 and their failure path prints `✘` (U+2718) — which
  replaced the name of the failed assertion with a `UnicodeEncodeError` traceback at exactly the
  moment you needed it. Exit codes were always right, so CI never noticed; only the human
  debugging did. `solver_test` is ASCII-only and needs nothing.
  `smoke.py` and `live_llm.py` printed `→`, `✔`, `✘` on the *success* path and so threw on every
  run; they got the same one-line fix on 2026-09-25 (#16). Every suite that prints non-ASCII now
  self-configures; `solver_test` alone does not need to.

---

## Tests

```bash
python tests/solver_test.py    # 44 assertions, no server, no API cost
python tests/mcp_parity.py     # 254 assertions, no server, no API cost
python tests/campus_test.py    # 122 assertions, no server, no API cost
python tests/mesh_test.py      # 22 assertions, no server, no API cost
python tests/auth_test.py      # 77 assertions, no server, no API cost
python tests/ranking_test.py   # 57 assertions, no server, no API cost
python tests/deploy_test.py    # 68 assertions, no server, no API cost
python tests/nlu_test.py       # 15 assertions, no server, no API cost
python tests/smoke.py          # rule-engine regression — needs the server on :8000, no API cost
python tests/live_llm.py       # 6 real-model queries; costs tokens
```

`ranking_test.py` is the one to run after touching `SubstitutionAgent`. Four of its assertions
encode real defects and say so in their comments — leave them labelled: a swap must not change the
partner's teaching hours, a swap must never split a lab block, `find_makeup` must be able to
succeed at all, and the rank shown on the card must be the rank used to sort.

`campus_test.py` is the one to run after touching `campus.py`, `campus_data.py` or the NLU. Its
policy cases (gate passes, circulation, hostel, bus seats, eligibility, no-dues) are pinned one by
one and were mutation-tested: each rule, broken, fails a named assertion. Two cases are
*constructed* because the seed happens not to exercise them — `3b` (opens one bed in a batch-mate
room) and `4f` (every other bus already over-full; a negative slice would once have moved riders
onto them).

`solver_test.py` is the one to run after any solver change. Two of its assertions encode real
defects that were found by measurement, and their comments say so — leave them labelled:
scoped regeneration must not promise a teacher hours their *pinned* bookings already consume,
and room scarcity must degrade rather than collapse.

---

## Known weak points (say these before an examiner does)

- ~~**The coverage ranking is not fully computed.**~~ **Done 2026-09-15.** Every number on a plan
  card is now measured per instance by `SubstitutionAgent.score_plan` / `score_leg`. Continuity is
  three measured terms (syllabus hours preserved · delivered by the subject's own teacher · timing
  intact, via `timing_intact(slip) = 1/(1+slip)`), and B and C confidence come from
  `score_deliverer`, which is the same candidate-scoring lens Plan A always used. The weights
  `0.8 / 0.2` (`RANK_WEIGHTS`) and the continuity sub-weights are **policy coefficients, not
  findings** — they are declared in one place and printed on every plan card, which is the
  point. `tests/ranking_test.py` asserts each number varies across 30+ absences; a measurement
  that never varies is a constant wearing a function's clothes.
- **Three defects surfaced while computing them**, each by measurement, each with a labelled
  assertion in `tests/ranking_test.py`:
  1. `find_makeup` searched only periods `1..day_periods(sem,day)`, and `db.verify()` asserts a
     batch fills exactly those with no holes — so it could never succeed. Measured: 0 of 126
     class-days have a free in-day period. **Plan C reported 0% coverage for every absence ever
     put to it**, hidden by its flat confidence of 66. It now searches to the last period of the
     college day, which is where a make-up hour actually goes.
  2. Plan B was never a swap. It wrote **one** override on the vacated slot setting only
     `new_faculty`, leaving the partner's own period untouched — so the partner taught both and
     picked up an extra hour, while the card claimed "No extra workload on any faculty". It now
     writes **two** rows under one `plan_ref` (the partner's subject moves via `new_subject`; their
     original period is `VACATED` and the absent subject gets a make-up).
  3. Plan B's fallback substitutes were scored against **Plan A's** `used` accumulator, so B was
     penalised `-9` per assignment only A would have made. The plans are alternatives; B has its
     own accumulator now.
- **Coverage is a floor, not a ranking axis, because it was being counted twice.**
  `coverage = mean(hours)` (`agents.py:411`) and `continuity = 100·(0.45·hours + …)`
  (`:412`) — coverage IS the hours term, and continuity already contains 45% of it. The old
  formula therefore weighted hours at `0.25 + 0.15×0.45 = 0.3175` while the card told the
  administrator they were two separate axes worth 0.25 and 0.15. That double-count is the reason
  the weight is gone, and it holds for every instance. `ranking_test.py:7c/7e/7f` assert the
  relationship itself, so if continuity ever stops being built from hours the argument fails
  loudly. It is still measured and still shown as `incomplete`. **Do not put it back without a
  measurement showing it has become independent.**
  An earlier version of this note argued instead that the term "could not change an ordering" and
  that a discriminating case "cannot be built". That was too strong and wrong: it reasoned only
  from normal load and *total* scarcity, and missed **partial scarcity**, where some legs are
  coverable and some are not — 203 of 720 probe scenarios score below 100, across
  {0, 33, 50, 60, 67, 75, 80, 100}. Ordering survives nearly all of it, but that is supporting
  evidence, not the argument.
- **Two plans that would commit the same rows are folded into one.** With no swap available Plan B
  falls back to substitution and picks Plan A's candidates — 90 of 271 absences — which produced
  two identical cards at an exact rank tie and made "recommended" arbitrary. `commit_signature`
  compares what a plan would actually write, not how its card reads. A tie-break alone would have
  hidden this; there is also a documented one (rank → continuity → coverage → code) for genuine
  coincidences, of which the sweep found exactly one.
- **`this`, `next`, `coming` and `on <weekday>` all mean the next occurrence.** `next X` used to
  add a further week — on Fri 04 Sep "next monday" was the 14th, skipping the 7th. Changed because
  the distinction held for only 6 of 7 weekdays (when X is today's own weekday both readings land
  +7 anyway) and the failure costs are asymmetric: a week *late* leaves the real absence uncovered
  on the day it happens and nothing notices, while a week early is caught by the date echoed back
  before anything commits. `tests/nlu_test.py` pins all 7×7 reference-target pairs, so changing the
  convention again is a deliberate act with a failing test attached.
- **A swap must never split a lab.** `find_swap` refuses any partner whose subject is `kind='L'`
  or which sits in a contiguous same-subject block (`in_multi_period_block`). Labs run three
  consecutive periods; CIVIL-3A Mon P5-P7 is the live example. Removing that guard makes
  `ranking_test.py:4b` name the blocks it split.
- **Make-ups are advisory, not scheduled.** Both Plan B and Plan C record a make-up slot in the
  leg and the override reason, but nothing inserts a `timetable` row for it. The slot is verified
  free for both the batch and the teacher at proposal time; it is not held.
- Teacher unavailability is honoured by the solver as an input but nothing populates it yet —
  approved leave drives the *rescheduling* path, not generation.
- **Attendance is a single independent draw per student** — `random.gauss(80, 12)` clamped to
  [46, 99] (`db.py`, students insert). It carries no correlation with CGPA, backlogs, subject or
  semester, so any "attendance risk" analytics are structurally shallow.
- **Per-subject attendance is derived, not observed.** Since 2026-09-25 `campus_data` fills the
  `attendance` table, reconciled so each student's hours-weighted mean reproduces
  `students.attendance` (±0.5, `campus_test.py:1f`). It refines the headline; it inherits its
  weakness — one independent draw per student — and adds no signal of its own.
- **Hostel gender is inferred from the synthetic first-name pools** (`campus_data.gender_of`).
  Sound only because every name was generated from those pools; real data needs a real column.
- **The gate-pass, circulation and placement rules are policy constants** (`CURFEW`,
  `OUTING_CLASS_HOURS_BAR`, `LOAN_LIMIT`, `FINE_PER_DAY`, `DREAM_MULTIPLE`), stated in the console
  and pinned by tests. They are one college's plausible policy, not findings.
- **Campus notifications are recorded, not delivered.** Like every notice here, they land in the
  `notifications` table; there is no SMS/email gateway.
- **The fee-structure certificate uses synthetic fee heads** (`campus.FEE_HEADS`), and every
  printed document carries a footer saying it is a specimen from synthetic data.

---

## Where MAWOS fits now

`../MAWOS` is the archived B.E. research prototype this project superseded. You rarely need it.
Two reasons to open it:

1. ~~**The MCP server port**~~ — **done 2026-09-15**, as `mcp_server.py` +
   `tests/mcp_parity.py`. Nothing was copied: MAWOS needs the `mcp` SDK and gates on *role*
   against SQLAlchemy models, VidyaERP speaks raw JSON-RPC over stdlib and gates on *turn
   approval*. What was taken is the shape — `call_as` as a thin delegation, and a parity suite
   that replays adversarial items through every entry point. VidyaERP's suite adds the question
   MAWOS never had to ask: whether a model can manufacture its own approval when it is the only
   caller. See §4 of `../MAWOS/docs/superpowers/specs/2026-09-14-mawos-v6-product-aim.md`.
2. **Research material** — `../MAWOS/evaluation/` if a paper is ever wanted.

**Do not copy MAWOS's data, documents or dependencies into this repo.** MAWOS is public but
unlicensed by choice — this is the licensed, distributable project, and that one is a research
archive. Nothing from `ml/data/`, its Review-1 material, or `psycopg` (LGPL-3.0) belongs here.

Its `docs/OPEN_SOURCE_AUDIT.md` is worth reading once before adding *anything* third-party here,
less for its findings than for what it got wrong: it called a public research dataset and a set
of university roll numbers an "exposure" and repeated it until the word stopped meaning anything.
Both were real issues — CC BY 4.0 data cannot be sublicensed under Apache 2.0, and other people's
identifiers are not ours to publish — and neither was dangerous. **State severity as it actually
is.** Over-flagging trains the reader to discount the next finding, which is the expensive
failure. The same discipline the solver notes above demand of measurements applies to risks.
