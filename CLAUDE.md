# VidyaERP — working notes for Claude

**This is the product. All work happens here.** Open Claude in this folder, not in MAWOS.

A SMART agentic ERP for an Indian engineering college (autonomous / VTU-affiliated model),
admin-first. The differentiator is not the CRUD — it is the agent mesh over the institutional
database that an administrator talks to and delegates work to.

Public, Apache 2.0: <https://github.com/samprith02/VidyaERP>

```bash
pip install -r requirements.txt
python -m uvicorn app:app --port 8000        # → http://localhost:8000
```

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
| Solver | `solver.py` | timetable generation from scratch (below) |
| NLU | `nlu.py` | intent scoring + regex priority rules + Indian date parsing |

**Two engines, one interface.** The rule engine is the default and costs nothing; the LLM agent
is opt-in. Both go through the *same* `tools.py`, so guardrails cannot differ between them.
`nlu.select_tools()` narrows 22 tools to ~4–8 per utterance (−64% payload) — free tiers are
stingy and this is what keeps multi-hop turns inside the budget.

---

## Rules that matter here

- **Writes are guarded, and the guard is in code, not in the prompt.** Four write tools —
  `apply_coverage_plan`, `apply_timetable_generation`, `decide_request`, `broadcast_notice` —
  refuse to commit without explicit admin approval *in the current turn*
  (`tools.approved_this_turn`). `tests/mock_llm.py` has a rogue-agent endpoint that tries to skip
  the gate and is blocked. **Never** add a write path that bypasses this, and never move the check
  into a system prompt.
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
python tests/smoke.py          # rule-engine regression — needs the server on :8000, no API cost
python tests/live_llm.py       # 6 real-model queries; costs tokens
```

`solver_test.py` is the one to run after any solver change. Two of its assertions encode real
defects that were found by measurement, and their comments say so — leave them labelled:
scoped regeneration must not promise a teacher hours their *pinned* bookings already consume,
and room scarcity must degrade rather than collapse.

---

## Known weak points (say these before an examiner does)

- **The coverage ranking is not fully computed.** `agents.py:326` ranks plans by
  `0.6·confidence + 0.25·coverage + 0.15·continuity`. Coverage is genuinely measured and plan A's
  confidence is computed from real candidate scores (`agents.py:245`), but **`continuity` is a
  constant per strategy** (74 / 92 / 88 at lines 300/309/318) and **plan B and C confidence are
  formulaic** (`71 + min(20, swaps*6)` and a flat `66`). Defensible as a demo, and it is the
  first question an examiner will ask. Computing them properly is the highest-value next
  improvement — syllabus hours preserved, whether the batch's own teacher still delivers, how far
  a make-up slips.
- **The weights `0.6 / 0.25 / 0.15` are a policy choice, not a finding.** Surface them in the UI
  rather than hiding them.
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

1. **The MCP server port** — `backend/app/mcp_server.py` plus its parity suite is the one thing
   MAWOS has that VidyaERP does not. Porting it is §4 of
   `../MAWOS/docs/superpowers/specs/2026-09-14-mawos-v6-product-aim.md`: expose these same tools
   over MCP so an admin can drive the ERP from Claude Desktop, with the MCP surface a thin
   delegation to the *same* functions behind the *same* PolicyGuard — no second authorisation
   path, no MCP-only tool. The parity test replays adversarial items through both entry points
   and asserts identical verdicts.
2. **Research material** — `../MAWOS/evaluation/` if a paper is ever wanted.

**Do not copy MAWOS's data, documents or dependencies into this repo.** It is public and
unlicensed and carries third-party personal data and a third-party dataset; see its
`docs/OPEN_SOURCE_AUDIT.md`. Nothing from `ml/data/`, `MAWOS_Review1.*` or `psycopg` belongs here.
