"""Kannada and Hindi input (#31): read as English before the NLU, in both
scripts and romanised, without ever widening the write gate. No server, no
API cost.

    python tests/language_test.py
"""
import datetime as dt
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import db                                                          # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), "vidyaerp_language_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
db.DB_PATH = _TMP
db.seed(force=True)

import auth                                                        # noqa: E402
import guard                                                       # noqa: E402
import llm_agent                                                   # noqa: E402
import nlu                                                         # noqa: E402
import orchestrator as O                                           # noqa: E402
import tools                                                       # noqa: E402
from agents import one, rows                                       # noqa: E402

FAILED, PASSED = [], 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


con = db.connect()
FRI = dt.date(2026, 9, 4)
en = lambda t: nlu.normalize(t)[0]                                  # noqa: E731

# ------------------------------------------------------------------ 1 · the lexicon
print("\n1 · the words, in both scripts and romanised")
print("-" * 78)
check("1a English passes through untouched", nlu.normalize("Show CSE 5th sem A timetable")
      == ("Show CSE 5th sem A timetable", []))
check("1b 'tomorrow' in Kannada script, romanised Kannada and Hindi",
      all(nlu.parse_date(en(w))[0] == FRI + dt.timedelta(days=1) for w in ("ನಾಳೆ", "naale", "कल", "kal")))
check("1c a Kannada case ending is absorbed: ನಾಳೆಗೆ ('for tomorrow')",
      nlu.parse_date(en("ನಾಳೆಗೆ ಪರೀಕ್ಷೆ"))[0] == FRI + dt.timedelta(days=1), en("ನಾಳೆಗೆ ಪರೀಕ್ಷೆ"))
days = {"ಸೋಮವಾರ": 7, "मंगलवार": 8, "budhvar": 9, "ಗುರುವಾರ": 10, "शुक्रवार": 11, "shanivar": 5}
check("1d weekdays in both languages resolve to the next occurrence",
      all(nlu.parse_date(en(w))[0] == dt.date(2026, 9, d) for w, d in days.items()),
      str({w: nlu.parse_date(en(w))[0] for w in days}))
# "kal" is also yesterday in Hindi. It is read forward on purpose - the only
# reading a plan can act on - and the date is echoed before anything commits.
check("1e Hindi 'kal' is read forward, and the date is echoed back", en("kal") == "tomorrow")
check("1f a name is never translated: 'Kalpana', 'Indu' stay as they are",
      en("Kalpana is absent") == "Kalpana is absent" and en("Dr. Indu Rao") == "Dr. Indu Rao")
# The invariant that matters: the lexicon cannot manufacture an approval.
made = [e for e, _f in nlu.LEXICON if guard.APPROVAL_RX.search(e)]
check("1g no translation is an approval word", not made, str(made))
check("1h ...and no translation contains a confirmation either", not any(nlu.is_yes(e) for e, _f in nlu.LEXICON))

# ------------------------------------------------------------------ 2 · the rule engine
print("\n2 · the rule engine answers them")
print("-" * 78)


def ask(q, sid, role="admin", user=None):
    if user:
        O.sess(sid)["user"] = auth.principal(con, user)
    return O.handle(con, q, sid, role, user or "registrar")


out = ask("ನಾಳೆ CSE 5A ವೇಳಾಪಟ್ಟಿ ತೋರಿಸಿ", "k1")
check("2a Kannada: 'tomorrow CSE 5A timetable show' is the timetable",
      out["intent"] == "timetable.view" and "CSE · Sem 5 · Sec A" in str(out["blocks"]), out["intent"])
check("2b the trace says what was translated", any(t["action"] == "translate" for t in out["trace"]))
out = ask("कल ECE sem 3 की समय सारणी दिखाओ", "k2")
check("2c Hindi, the same", out["intent"] == "timetable.view" and "ECE · Sem 3" in str(out["blocks"]), out["intent"])
fac = one(con, """SELECT f.name FROM faculty f JOIN timetable t ON t.faculty=f.id WHERE t.day='Sat'
                  GROUP BY f.id ORDER BY COUNT(*) DESC LIMIT 1""")["name"]
before = one(con, "SELECT COUNT(*) c FROM overrides")["c"]
out = ask(f"{fac} naale barolla", "k3")
check("2d 'X naale barolla' (won't come tomorrow) plans cover for Saturday 05 Sep",
      out["intent"] == "absence.cover" and "05 Sep" in str(out["blocks"])
      and one(con, "SELECT COUNT(*) c FROM overrides")["c"] == before, str(out["blocks"])[:160])
out = ask("ಹೌದು", "k3")
check("2e a Kannada 'yes' commits nothing and says why",
      one(con, "SELECT COUNT(*) c FROM overrides")["c"] == before and "in English" in str(out["blocks"])
      and any(t["agent"] == "PolicyGuard" and t["status"] == "warn" for t in out["trace"]), str(out["blocks"])[:160])
out = ask("haan", "k3")
check("2f ...nor a romanised Hindi one", one(con, "SELECT COUNT(*) c FROM overrides")["c"] == before)
out = ask("yes", "k3")
check("2g the proposal is still there: 'yes' in English commits it",
      one(con, "SELECT COUNT(*) c FROM overrides")["c"] > before, str(out["blocks"])[:160])
aud = one(con, "SELECT payload FROM audit WHERE payload LIKE '%barolla%' ORDER BY id DESC LIMIT 1")
check("2h the ledger keeps the words as said", aud is not None)
out = ask("ನನ್ನ ವೇಳಾಪಟ್ಟಿ", "s1", "student", "4VP24CS009")
check("2i a student's 'my timetable' in Kannada is their own class", "CSE · Sem 5 · Sec A" in str(out["blocks"]),
      str(out["blocks"])[:120])

# ------------------------------------------------------------------ 3 · the LLM path
print("\n3 · the LLM path is told, and narrowed on the English reading")
print("-" * 78)
_s, names = tools.select_tools("ಗ್ರಂಥಾಲಯ ಪುಸ್ತಕ machine learning", {})
# (a library request: its tools are not in the fallback menu, so this cannot
# pass by the router failing to classify and offering CORE)
check("3a a Kannada library request is offered the library tools", "library_search" in names
      and "library_search" not in tools.CORE, str(names))
check("3b both prompts say: answer in their language, confirm only with yes in English",
      all("Kannada or Hindi" in p and "yes" in p for p in (
          llm_agent.system_prompt(con), llm_agent.persona_prompt(auth.principal(con, "4VP24CS009")))))

print("-" * 78)
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  ✘", f)
con.close()
sys.exit(1 if FAILED else 0)
