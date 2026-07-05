#!/usr/bin/env python3
"""Write the hermes surface's capability inventory into the shared
~/ai-os/me/CAPABILITIES.md — the passport's "what you've got wired up" file.

Each Sasha surface OWNS a marked section and reads the WHOLE file, so either
Sasha knows the full toolkit across surfaces and can route ("you don't have
that here — on your Claude Code Sasha, type /prep"). This writes only the
HERMES-CAPS block from live state (~/.hermes/skills/ + config.yaml
mcp_servers); the AIOS-CAPS block is owned by Sasha-on-Claude-Code.

Idempotent, best-effort — never raises into the caller (dashboard boot).
"""
import os, re

START, END = "<!-- HERMES-CAPS start -->", "<!-- HERMES-CAPS end -->"


def _hermes_skills(home):
    d = os.path.join(home, ".hermes", "skills")
    out = []
    try:
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if not os.path.isdir(p) or name.startswith("."):
                continue
            desc = ""
            for fn in ("DESCRIPTION.md", "SKILL.md"):
                fp = os.path.join(p, fn)
                if os.path.exists(fp):
                    txt = open(fp, encoding="utf-8", errors="replace").read()
                    m = re.search(r"(?m)^description:\s*(.+)$", txt)
                    if m:
                        desc = m.group(1).strip().strip('"').strip("'")
                    else:
                        for line in txt.splitlines():
                            line = line.strip()
                            if line and not line.startswith(("---", "#")):
                                desc = line
                                break
                    break
            out.append((name, desc[:90]))
    except OSError:
        pass
    return out


def _hermes_connectors(home):
    """Enabled mcp_servers from config.yaml — no yaml dep, tolerant line scan."""
    cfg = os.path.join(home, ".hermes", "config.yaml")
    servers = []
    try:
        lines = open(cfg, encoding="utf-8", errors="replace").read().splitlines()
        in_mcp = False
        cur = None
        for l in lines:
            if l.strip() == "mcp_servers:":
                in_mcp = True
                continue
            if in_mcp:
                if l and not l[0].isspace() and l.strip():
                    break  # left the block
                m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", l)
                if m:
                    cur = m.group(1)
                elif cur and "enabled: true" in l:
                    servers.append(cur)
                    cur = None
    except OSError:
        pass
    return servers


def build_block(home):
    skills = _hermes_skills(home)
    conns = _hermes_connectors(home)
    lines = [START, "## On hermes (the web chat — Sasha here)", ""]
    if conns:
        lines.append("Connected tools: " + ", ".join(conns))
    if skills:
        lines.append("")
        lines.append("Skills I can use here:")
        for name, desc in skills:
            lines.append(f"- **{name}**" + (f" — {desc}" if desc else ""))
    if not skills and not conns:
        lines.append("(No hermes skills or connectors detected yet.)")
    lines.append(END)
    return "\n".join(lines)


def update(me_dir=None, home=None):
    home = home or os.path.expanduser("~")
    me_dir = me_dir or os.path.join(home, "ai-os", "me")
    try:
        os.makedirs(me_dir, exist_ok=True)
        path = os.path.join(me_dir, "CAPABILITIES.md")
        block = build_block(home)
        try:
            txt = open(path, encoding="utf-8").read()
        except FileNotFoundError:
            txt = "# What Sasha can do for you (across your surfaces)\n\n"
        if START in txt and END in txt:
            txt = re.sub(re.escape(START) + r".*?" + re.escape(END), block, txt, flags=re.S)
        else:
            txt = txt.rstrip() + "\n\n" + block + "\n"
        open(path, "w", encoding="utf-8").write(txt)
        return path
    except OSError:
        return None


if __name__ == "__main__":
    import sys
    p = update(me_dir=(os.path.expanduser(sys.argv[1]) if len(sys.argv) > 1 else None))
    print(f"capabilities written to {p}" if p else "capabilities: nothing written")
