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
tools) → `tools.py` (22 tool schemas, PolicyGuard-wrapped) → `agents.py` (the specialists).

| Piece | Where | Note |
|---|---|---|
| PolicyGuard | `agents.py:84` | RBAC, scope, rupee ceilings, HITL gate. **Outside the model** |
| Substitution | `agents.py:103` | the flagship: absence → 3 ranked coverage plans |
| Timetable / Faculty / Student / Finance / Exam / Request / Notify / Analytics | `agents.py:367+` | specialists, all return structured dicts |
| Auditor | `agents.py:561` | immutable ledger of who asked, which agent acted, what changed |
| MCP surface | `mcp_server.py` | stdio JSON-RPC; `call_as` delegates to `tools.execute`. No SDK |
| Solver | `solver.py` | timetable generation from scratch (below) |
| NLU | `nlu.py` | intent scoring + regex priority rules + Indian date parsing |

**Two engines, one interface.** The rule engine is the default and costs nothing; the LLM agent
is opt-in. The rule engine drives `agents.py` directly through its own HITL state machine
(`orchestrator.resolve_pending`); the LLM agent goes through `tools.py`. The parity that is
actually enforced is between the **LLM agent and MCP** — both reach `tools.execute` and cannot
differ, which is what `tests/mcp_parity.py` asserts.
`nlu.select_tools()` narrows 22 tools to ~4–8 per utterance (−64% payload) — free tiers are
stingy and this is what keeps multi-hop turns inside the budget.

---

## Rules that matter here

- **Writes are guarded, and the guard is in code, not in the prompt.** *Five* write tools —
  `tools.GATED_WRITES`: `apply_coverage_plan`, `apply_timetable_generation`, `decide_request`,
  `create_request`, `broadcast_notice` — refuse to commit without explicit admin approval *in the
  current turn* (`tools.approved_this_turn`). `tests/mock_llm.py` has a rogue-agent endpoint that
  tries to skip the gate and is blocked. **Never** add a write path that bypasses this, and never
  move the check into a system prompt. Adding a sixth write tool means adding it to
  `GATED_WRITES`; `mcp_parity.py:15c` fails if you forget.
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
- **Propose, then commit.** Every write tool has a `plan_*` partner that writes nothing and
  stashes into `S["pending"]`. Keep that shape for new write tools.
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
- **The console has no authentication, and that is the security model.** No login, and
  `/api/chat` can drive every write. Anyone who can reach the URL is the Registrar — correct for a
  laptop or a college LAN, and it makes any public deployment a demo over synthetic data, not a
  system of record. Say this plainly rather than implying otherwise; `tests/deploy_test.py:4j`
  asserts the docs still say it. `VIDYAERP_ADMIN_TOKEN` closes only the two *operator* endpoints
  (`/api/seed/reset`, `/api/mcp/approval`) — an open approval endpoint would hand the MCP write
  gate to whoever found it, which would contradict a claim this project makes about itself.
- **The real environment beats `.env`.** `llm.py`'s config reads `os.environ` first and `.env`
  second. It used to be the other way round, which meant a stale `.env` inside an image would
  silently shadow a platform dashboard with nothing in the UI to show why
  (`tests/deploy_test.py:2a`). A deployment configures this app through environment variables and
  has no `.env` to edit.
- **`static/index.html` loads nothing external** — no CDN, no fonts, no images. Keep it that way;
  it is why the console works offline and on a locked-down college network.

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

- **Always pass `encoding="utf-8"` to `open()`.** Windows defaults to cp1252 and the console
  contains typographic quotes and `·` separators. `app.py` served a 500 on the whole page for
  exactly this reason.
- **Stop the server before deleting or restoring `college.db`.** A running server holds the file
  and the delete fails — on Windows `git checkout` reports `unable to unlink old ... Invalid
  argument`. Kill the uvicorn process first.
- **Storage is ephemeral on a free host, and `/health` says so.** `db.seed()` rebuilds the whole
  institution on a cold start (~0.07 s), so a restart silently discards every applied override.
  `VIDYAERP_DB` points the database at a mounted disk; `/health` reports `persistent: false` when
  it is not set.
- **MCP cannot reach a remote deployment.** `mcp_server.py` is stdio and opens the database beside
  it, so an approval code minted on the Render box is useless to an MCP client on a laptop. Do not
  describe the deployed console and the MCP surface as one system — they are two instances.
- **`college.db` is gitignored on purpose.** `db.seed()` reproduces the seeded data exactly
  (verified table-by-table; `db.py` pins `random.seed(20)`), but the *file* is not byte-identical
  — SQLite page layout is not reproducible, and a tracked DB accumulates runtime rows. The copy
  this replaced had five stray `audit` entries committed by accident. Do not commit it.
- **`tests/smoke.py` hardcodes port 8000** and needs the server already running.
- Set `PYTHONIOENCODING=utf-8` when a test prints the trace, or the Windows console throws on the
  `→` characters.

---

## Tests

```bash
python tests/solver_test.py    # 44 assertions, no server, no API cost
python tests/mcp_parity.py     # 155 assertions, no server, no API cost
python tests/ranking_test.py   # 43 assertions, no server, no API cost
python tests/deploy_test.py    # 38 assertions, no server, no API cost
python tests/smoke.py          # rule-engine regression — needs the server on :8000, no API cost
python tests/live_llm.py       # 6 real-model queries; costs tokens
```

`ranking_test.py` is the one to run after touching `SubstitutionAgent`. Four of its assertions
encode real defects and say so in their comments — leave them labelled: a swap must not change the
partner's teaching hours, a swap must never split a lab block, `find_makeup` must be able to
succeed at all, and the rank shown on the card must be the rank used to sort.

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
  `0.6 / 0.25 / 0.15` (`RANK_WEIGHTS`) and the continuity sub-weights are **policy coefficients,
  not findings** — they are declared in one place and printed on every plan card, which is the
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
- **The `attendance` table is created but never populated** (0 rows). Per-subject attendance does
  not exist; `students.attendance` is the only figure in the system. Anything claiming
  subject-level attendance would be reading an empty table.

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
