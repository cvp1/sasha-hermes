#!/usr/bin/env python3
"""Wire hermes to the me/ passport (~/ai-os/me/).

Injects an idempotent marked block into ~/.hermes/SOUL.md — the identity file
hermes composes into EVERY session's system prompt (agent/system_prompt.py:
"stable — identity (SOUL.md ...)"; operational-context.md is NOT on the
/api/ws gateway path — learned by probing a fresh gateway) — telling the agent
to read, honor, and maintain the same WHOAMI.md / HOW-I-WORK.md files that
Sasha-on-Claude-Code writes. This is the graduation guarantee: two separate
products, one identity, nothing lost moving between them.

Run AS THE TARGET USER (no root):  python3 me_bridge.py [me_dir]
Idempotent: re-running replaces the marked block, never duplicates it.
"""
import os, re, sys

ME_DIR = sys.argv[1] if len(sys.argv) > 1 else "~/ai-os/me"
CTX = os.path.expanduser("~/.hermes/SOUL.md")
START, END = "<!-- SASHA-ME-BRIDGE start -->", "<!-- SASHA-ME-BRIDGE end -->"

BLOCK = f"""{START}
## My person's files — the me/ passport (read these FIRST, every session)

Standing identity lives in {ME_DIR}/ — read BOTH files at the start of every
session and honor them:
- {ME_DIR}/WHOAMI.md — who they are: their world, key people, projects.
- {ME_DIR}/HOW-I-WORK.md — how they like things, plus hard "never without
  asking" rules. Treat every rule in it as BINDING.

Also read {ME_DIR}/CAPABILITIES.md — what they have wired up on EACH Sasha
surface (skills, connectors). Use it to route: if they ask for something you
can't do here but their other Sasha can, point them to it by name ("on your
Claude Code Sasha, type /prep") instead of just declining.

Keep the files true:
- When they correct how you work ("shorter", "warmer", "never do X without
  asking"), UPDATE HOW-I-WORK.md right then — that is where the correction
  lives, so it sticks everywhere.
- When you learn something durable about their world (a person, a project, a
  change), propose adding it to WHOAMI.md.

These same files are read and written by their other Sasha (on Claude Code).
Same person, same passport — never assume you are the only writer: re-read a
file before you edit it, and merge rather than overwrite.
{END}"""


def main():
    os.makedirs(os.path.dirname(CTX), exist_ok=True)
    try:
        txt = open(CTX).read()
    except FileNotFoundError:
        txt = "# SOUL\n"
    if START in txt and END in txt:
        txt = re.sub(re.escape(START) + r".*?" + re.escape(END), BLOCK, txt, flags=re.S)
        action = "updated"
    else:
        txt = txt.rstrip() + "\n\n" + BLOCK + "\n"
        action = "added"
    open(CTX, "w").write(txt)
    print(f"me-bridge {action} in {CTX} (me_dir={ME_DIR})")


if __name__ == "__main__":
    main()
