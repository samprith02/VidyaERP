"""Mock OpenAI-compatible endpoint — lets us exercise the full tool-calling loop with no API key."""
import json
from fastapi import FastAPI, Body
app = FastAPI()

@app.post("/v1/chat/completions")
def cc(payload: dict = Body(...)):
    msgs = payload.get("messages", [])
    user = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), "")
    already = [m for m in msgs if m.get("role") == "tool"]
    names = [m.get("name") for m in already]

    def wrap(msg):
        return {"choices": [{"message": msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 900 + 300*len(already), "completion_tokens": 120}}

    low = user.lower()
    # hop 1 : pick a tool
    if not already:
        if "absent" in low or "coverage" in low:
            call = ("plan_absence_coverage", {"faculty_name": user.split(" is ")[0], "date": "next monday"})
        elif "brief" in low or "status" in low:
            call = ("institution_overview", {})
        elif "defaulter" in low:
            call = ("attendance_defaulters", {"dept": "CSE", "sem": 5, "cutoff": 75})
        else:
            call = ("institution_overview", {})
        return wrap({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": call[0], "arguments": json.dumps(call[1])}}]})
    # hop 2 : chain a second tool once, to prove multi-hop
    if len(already) == 1 and "plan_absence_coverage" in names:
        return wrap({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "list_leaves", "arguments": "{}"}}]})
    # hop 3 : write the answer
    data = json.loads(already[0]["content"])
    if "plans" in data:
        best = data["plans"][0]
        txt = (f"**{data['faculty']}** has {len(data['impacted_blocks'])} teaching blocks on "
               f"{data['day']}, {data['date']} — about **{data['student_hours_at_stake']} student-hours**.\n\n"
               f"I'd go with **Plan {best['code']} ({best['title']})** at {best['confidence']}% confidence. "
               f"{best['summary']}\n\nNothing is written yet — say the word and I'll publish it.\n"
               f"SUGGEST: Apply plan {best['code']} | Show alternatives | Who else is on leave that day?")
    else:
        txt = ("Checked the live records — everything below is current.\n"
               "SUGGEST: Pending approvals | Attendance defaulters | Faculty workload")
    return wrap({"role": "assistant", "content": txt})

# --- rogue-agent test hook: model tries to WRITE without admin approval -------
@app.post("/rogue/v1/chat/completions")
def rogue(payload: dict = Body(...)):
    msgs = payload.get("messages", [])
    already = [m for m in msgs if m.get("role") == "tool"]
    def wrap(msg):
        return {"choices": [{"message": msg}], "usage": {"prompt_tokens": 800, "completion_tokens": 60}}
    if not already:
        return wrap({"role": "assistant", "content": None, "tool_calls": [
            {"id": "r1", "type": "function",
             "function": {"name": "apply_coverage_plan", "arguments": '{"plan_code":"A"}'}}]})
    import json as _j
    got = _j.loads(already[-1]["content"])
    return wrap({"role": "assistant",
                 "content": f"Guard response was: {list(got.keys())}\nSUGGEST: ok"})
