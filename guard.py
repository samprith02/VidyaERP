"""
VidyaERP :: the write gate

One function decides whether a guarded write may commit: did the ADMIN approve
in the current turn? It lives in its own module so every tool module can call it
without importing tools.py (which imports them) - tools.py re-exports both names
unchanged, so `tools.approved_this_turn` is still the gate the suites test.

`U` is always the human's own words for this turn. Over MCP it is produced only
by redeeming a one-time code the admin minted (mcp_server.approval_text), never
from anything the model wrote. Nothing in this file may ever read tool
arguments.
"""
import re

APPROVAL_RX = re.compile(
    r"\b(yes|yeah|yep|ok|okay|sure|confirm|confirmed|approve[d]?|apply|applied|go ahead|do it|"
    r"proceed|send it|publish|commit|accept|make it (?:so|happen)|plan\s*[abc])\b", re.I)


def approved_this_turn(user_text):
    return bool(APPROVAL_RX.search(user_text or ""))
