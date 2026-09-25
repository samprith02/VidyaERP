"""Deterministic regression test of the rule-engine agent mesh (server on :8000).

Pinned to engine="rule" on purpose: this suite asserts fixed routing/plan behaviour,
so it must not depend on a model's wording — and rapid-firing it at a metered LLM
just burns the token budget. For the LLM path run tests/live_llm.py instead.
"""
import json, os, urllib.request, sys
from http.cookiejar import CookieJar
# Prints → ✔ ✘ on the SUCCESS path, so a default Windows console (cp1252) threw
# UnicodeEncodeError on every run, not only a failing one (issue #16). Same fix
# the no-server suites got in c1e23dd.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://localhost:8000"

# Every route but sign-in needs a session since logins (#27); without one this
# suite died on its first request with a 401 (#52). Sign in once as the
# Registrar and carry the cookie on every call.
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

def sign_in():
    pw = os.environ.get("VIDYAERP_ADMIN_PASSWORD") or "registrar"
    OPENER.open(urllib.request.Request(BASE + "/api/auth/login",
        data=json.dumps({"username": "registrar", "password": pw}).encode(),
        headers={"Content-Type": "application/json"}))

def chat(q, s="smoke"):
    r = OPENER.open(urllib.request.Request(BASE + "/api/chat",
        data=json.dumps({"text": q, "session": s, "engine": "rule"}).encode(),
        headers={"Content-Type": "application/json"}))
    return json.load(r)

def show(q, r, nb=3):
    print(f"\n>>> {q}\n    routed → {r['intent']} ({r['confidence']}%)  ·  {len(r['trace'])} agent hops")
    for t in r['trace']:
        print(f"      · {t['agent']:<20}{t['action']:<26}{t['detail'][:70]}")
    for b in r['blocks'][:nb]:
        t = b.get('md') or b.get('title') or str(b.get('columns', ''))
        print("    ", b['type'], "::", t.replace("\n", " ")[:140])
        if b['type'] == 'plans':
            for p in b['plans']:
                print(f"       plan {p['code']}: conf {p['confidence']}% cov {p['coverage']}% "
                      f"cont {p['continuity']} · {len(p['legs'])} legs")

if __name__ == "__main__":
    sign_in()
    fac = json.load(OPENER.open(BASE + "/api/faculty"))
    busy = [f for f in fac if f['load'] >= 10][0]['name']
    script = [f"{busy} is absent next monday, arrange coverage",
              "show alternatives for period 3",
              "bulk approve everything under 50000",
              "apply plan A",
              "show me the updated timetable",
              "who is on leave next week",
              "undo the last change",
              "Dr. Ramesh is absent tomorrow"]
    for q in script:
        show(q, chat(q))
