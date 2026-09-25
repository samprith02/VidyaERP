"""
VidyaERP :: LLM agent loop
The supervisor is now an actual language model with the specialists bolted on as tools.

    user turn
       │
       ├─ PolicyGuard (RBAC, scope)                       [deterministic]
       ├─ LLM plans ──▶ tool_calls ──▶ specialists run    [model + code]
       │        ▲                          │
       │        └──────── results ─────────┘   (up to MAX_HOPS reasoning cycles)
       └─ LLM writes the answer ──▶ UI blocks from the tools + narrative from the model

If the model is unreachable or no key is configured, this module degrades to the
deterministic rule engine in orchestrator.py rather than failing.
"""
import json, re, time, datetime as dt
import llm, tools, nlu, orchestrator
from nlu import TODAY
from agents import Auditor, B_text, rows, one

MAX_HOPS = 6
HISTORY_TURNS = 4


def system_prompt(con):
    k = one(con, "SELECT COUNT(*) c FROM students")["c"]
    f = one(con, "SELECT COUNT(*) c FROM faculty")["c"]
    pend = one(con, "SELECT COUNT(*) c FROM requests WHERE status='Pending'")["c"]
    return f"""You are the VidyaERP Copilot for Vidyatech Institute of Engineering, an autonomous \
engineering college in coastal Karnataka (VTU scheme). {f} faculty, {k} students, departments \
CSE/ISE/ECE/MECH/CIVIL, semesters 3/5/7 running. Today is {TODAY.strftime('%A %d %B %Y')}. You are \
talking to the Registrar (admin, full authority); {pend} approvals are pending in their inbox.

CONTEXT: Mon-Sat. 55-min periods: P1 09:00, P2 09:55, break, P3 11:10, P4 12:05, lunch, P5 13:45, \
P6 14:40, P7 15:35. Sem 3 ends P7, sem 5 P6, sem 7 P5. Days have no free holes. Labs run 3 \
consecutive periods. VTU needs 75% attendance for SEE. Money in lakh/crore.

RULES
1. Use tools for every fact. Never invent a name, number, USN or date.
2. Chain tools when needed, but stop as soon as you can answer.
3. Propose before writing. Coverage plans, approvals, requests and broadcasts commit only after the \
admin says yes in that message. PolicyGuard blocks you otherwise - that is expected, not an error.
4. NEVER output a markdown table or re-list rows. The UI already renders every table, timetable and \
plan card returned by the tools, right under your text. Duplicating it looks broken.
5. Write 2-4 sentences of judgement instead: what it means, what is unusual, what you would do. \
Name 2-3 key people or figures inline at most.
6. plan_absence_coverage returns ranked plans - recommend the top one unless you can say in one line \
why it is wrong here. All three numbers are measured per plan, but only confidence and continuity \
rank them, at 0.8/0.2 - a policy choice, so say so if asked. Coverage is a feasibility floor, not a \
ranking axis. Never call a plan with incomplete=true a full solution. If a card says another plan \
would commit the same changes, say they are the same option rather than offering both.
7. Ambiguous faculty name -> ask which, never guess. Tool error -> say so plainly.
8. Campus services (library, hostel, bus, gate passes, placement, certificates, faculty leave) use \
ONE tool per write: called before the admin approves, it returns BLOCKED with a proposal and writes \
nothing - summarise the proposal and ask. When the admin says yes, call the SAME tool with the SAME \
arguments. Never call a write tool twice in one turn. "What needs my attention" -> ops_radar.

STYLE: brief, decisive, professional Indian English, like a chief of staff. Under 90 words normally, \
130 for a coverage recommendation. **Bold** the key figure or name. No JSON. Do not say "tool", \
"function" or "database" - say "the timetable shows" / "the leave ledger".

Finish with exactly one line:
SUGGEST: <2-4 follow-ups phrased exactly as the admin would type them, e.g. \
"Apply the recommended plan | Who else is free that day?">
Never put tool or function names in SUGGEST - they are buttons a human clicks."""


def _trim(msgs):
    """Keep the system prompt + the last N conversational turns (tool traffic is dropped)."""
    head = msgs[:1]
    tail = [m for m in msgs[1:] if m.get("role") in ("user", "assistant") and m.get("content")]
    return head + tail[-HISTORY_TURNS * 2:]


def handle_llm(con, text, sid="default", role="admin", actor="admin@vidyatech"):
    S = orchestrator.sess(sid)
    S.setdefault("msgs", [])
    t_start = time.time()
    trace = []
    add = lambda a, ac, de, st="ok": trace.append(
        {"agent": a, "action": ac, "detail": de, "status": st,
         "ms": int((time.time() - t_start) * 1000)})

    cfg = llm.CFG.reload()
    add("Supervisor", "receive", f'"{text[:90]}"')
    add("PolicyGuard", "rbac_check", f"role={role} · scope=institution · writes gated by turn-approval")

    if not S["msgs"]:
        S["msgs"] = [{"role": "system", "content": system_prompt(con)}]
    S["msgs"].append({"role": "user", "content": text})
    work = _trim(S["msgs"])

    blocks, refresh, used_tools = [], False, []
    total_in = total_out = 0

    try:
        for hop in range(MAX_HOPS):
            schemas, names = tools.select_tools(text, S)
            if hop == 0:
                add("ToolRouter", "narrow_toolset",
                    f"{len(names)} of {len(tools.SCHEMAS)} tools offered: {', '.join(names[:5])}"
                    + ("…" if len(names) > 5 else ""))
            msg, usage, ms, used_model, tier = llm.chat_resilient(work, schemas, cfg)
            if tier:
                add("LLM Planner", "model_failover",
                    f"primary rate-limited → answered on {used_model}", "warn")
            total_in += usage.get("prompt_tokens", 0)
            total_out += usage.get("completion_tokens", 0)
            calls = msg.get("tool_calls") or []
            add("LLM Planner", f"reason (hop {hop+1})",
                f"{used_model} · {usage.get('prompt_tokens',0)}→{usage.get('completion_tokens',0)} tok · {ms}ms"
                + (f" · calling {', '.join(c['function']['name'] for c in calls)}" if calls else " · composing answer"))

            if not calls:
                content = (msg.get("content") or "").strip()
                body, chips = _split_suggestions(content)
                S["msgs"].append({"role": "assistant", "content": content})
                add("Auditor", "ledger_write",
                    f"{len(used_tools)} tool call(s) · {total_in+total_out} tokens this turn")
                Auditor().log(con, actor, "LLM Supervisor", "llm_turn",
                              {"q": text, "tools": used_tools, "model": cfg.model}, "answered")
                return {"blocks": ([B_text(body)] if body else []) + blocks,
                        "trace": trace, "intent": ", ".join(used_tools) or "conversation",
                        "confidence": 100, "chips": _clean_chips(chips) or _fallback_chips(used_tools),
                        "refresh": refresh, "engine": "llm", "model": used_model,
                        "tokens": {"in": total_in, "out": total_out}}

            work.append(msg)
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                # `text` is the admin's own message, not anything the model wrote:
                # that is what the write gate reads. Dispatch goes through
                # tools.execute so an MCP client cannot reach a different one.
                t0 = time.time()
                result = tools.execute(con, S, text, name, args)
                add("ToolBus", name,
                    f"args={json.dumps(args, default=str)[:70]} · {int((time.time()-t0)*1000)}ms")
                used_tools.append(name)
                for tr in result.get("trace", []):
                    add(tr[0], tr[1], tr[2] if len(tr) > 2 else "")
                blocks += result.get("blocks", [])
                refresh = refresh or result.get("refresh", False)
                work.append({"role": "tool", "tool_call_id": call["id"], "name": name,
                             "content": json.dumps(result.get("data", {}), default=str)[:2800]})

        add("Supervisor", "hop_limit", f"stopped after {MAX_HOPS} reasoning cycles", "warn")
        return {"blocks": [B_text("I looped through several checks without landing on a final answer. "
                                  "Could you narrow the question a little?")] + blocks,
                "trace": trace, "intent": ", ".join(used_tools), "confidence": 60,
                "chips": _fallback_chips(used_tools), "refresh": refresh, "engine": "llm"}

    except llm.LLMError as e:
        add("LLM Planner", "transport_error", str(e)[:160], "warn")
        add("Supervisor", "failover", "switching to the deterministic rule engine", "warn")
        out = orchestrator.handle(con, text, sid, role, actor)
        out["trace"] = trace + out.get("trace", [])
        out["engine"] = "rule-engine (LLM unreachable)"
        friendly = ("the free-tier token budget for this minute is exhausted"
                    if getattr(e, "status", None) == 429 else str(e)[:110])
        out["blocks"] = [B_text(f"_⚠ {friendly} — answered with the built-in rule engine instead. "
                                f"The result below is fully accurate, just less conversational._")] \
                        + out.get("blocks", [])
        return out


def _split_suggestions(content):
    """Pull the SUGGEST: line out of the model's reply and turn it into chips."""
    chips = []
    m = re.search(r"^\s*SUGGEST\s*:\s*(.+)$", content, re.I | re.M)
    if m:
        chips = [c.strip(" -•*") for c in m.group(1).split("|") if c.strip()][:4]
        content = content[:m.start()].rstrip()
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content, chips


_TOOLNAMES = set()


def _clean_chips(chips):
    """Models sometimes echo tool names into SUGGEST. Those make useless buttons."""
    global _TOOLNAMES
    if not _TOOLNAMES:
        _TOOLNAMES = {sch["function"]["name"] for sch in tools.SCHEMAS}
    out = []
    for c in chips or []:
        c = c.strip().strip("`\"'")
        if not c or c in _TOOLNAMES:
            continue
        if "_" in c and " " not in c:          # snake_case leak
            continue
        out.append(c[:60])
    return out[:4]


def _fallback_chips(used):
    if any("absence" in u or "coverage" in u for u in used):
        return ["Apply the recommended plan", "Show me the other options",
                "Who else is on leave that day?"]
    if any("request" in u for u in used):
        return ["Approve the high-priority ones", "Show approved requests"]
    return ["What needs my attention today?", "Brief me on today's institution status",
            "Pending approvals in my inbox"]
