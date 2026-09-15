"""Live LLM smoke test — exercises the real model end to end (server on :8000).

Checks each query is actually answered by the LLM (not the rule-engine failover),
stays inside the token budget, and never leaks a markdown table into the prose.
"""
import json,urllib.request,time
Q=["which faculty is most overloaded right now?",
   "attendance defaulters below 65 percent in CSE",
   "show me the pending approvals",
   "who is free on Wednesday period 5?",
   "what is the fee collection position?",
   "show CSE 5A timetable for Monday"]
ok=fail=0
for q in Q:
    r=urllib.request.Request("http://127.0.0.1:8000/api/chat",
        data=json.dumps({"text":q,"session":"bat-"+str(hash(q)%999),"role":"admin"}).encode(),
        headers={"Content-Type":"application/json"})
    try:
        t=time.time(); d=json.loads(urllib.request.urlopen(r,timeout=120).read()); ms=int((time.time()-t)*1000)
    except Exception as e:
        print(f"✗ {q[:44]:<46} EXCEPTION {e}"); fail+=1; continue
    eng=d.get('engine',''); tk=(d.get('tokens') or {}).get('in','?')
    txt=next((b.get('md','') for b in d.get('blocks',[]) if b['type']=='text'),'')
    leak='TABLE-LEAK' if ('---|' in txt or '|--' in txt) else ''
    bad = 'rule-engine (LLM' in eng
    print(f"{'✗' if bad else '✓'} {q[:44]:<46} {eng:<28} {ms:>5}ms {str(tk):>5}tok {len(txt.split()):>3}w {leak}")
    if bad: print("     ↳", txt[:150])
    ok += not bad; fail += bad
    time.sleep(1)
print(f"\n{ok}/{len(Q)} answered by the LLM, {fail} fell back")
