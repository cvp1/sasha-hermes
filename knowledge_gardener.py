#!/usr/bin/env python3
"""Knowledge Gardener — daily scan for stale vault notes with related new signals.

Scans notes older than 30 days, cross-references them against recent signal scans
and frontier developments, and drops update proposals into the /ingest inbox.

Design:
  - Read-only on the vault (never edits a note directly)
  - Proposes updates as markdown files in _inbox/ for /ingest to process
  - Uses the existing knowledge index for semantic matching (nomic-embed-text on .21)
  - Best-effort: failures degrade to a note, never break the cron run

Usage:
    python3 knowledge_gardener.py [--dry-run] [--stale-days 45]
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request

HOME = os.path.expanduser("~")
CC = os.path.join(HOME, "Github", "CC")
VAULT = os.path.join(HOME, "notes")
SIGNALS_DIR = os.path.join(VAULT, "06 Logs", "Signals")
INBOX_DIR = os.path.join(VAULT, "_inbox")
INDEX_DIR = os.path.join(os.path.expanduser("~"), ".local", "state", "cc", "knowledge")
EMBED_URL = "http://192.168.86.21:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"

STALE_DAYS = 30
MAX_PROPOSALS = 5  # per run — don't overwhelm the inbox


def _embed(text):
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode()
    req = urllib.request.Request(EMBED_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    return resp.get("embedding", [])


def _cosine_sim(a, b):
    """Cosine similarity between two vectors."""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb + 1e-8)


def find_stale_notes(stale_days=STALE_DAYS):
    """Yield (path, rel_path, age_days, snippet) for notes older than stale_days."""
    now = dt.datetime.now().timestamp()
    cutoff = now - stale_days * 86400
    for root, dirs, files in os.walk(VAULT):
        # Skip hidden/generated dirs
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("_inbox", ".trash")]
        for f in files:
            if not f.endswith(".md"):
                continue
            path = os.path.join(root, f)
            mtime = os.path.getmtime(path)
            if mtime > cutoff:
                continue
            rel = os.path.relpath(path, VAULT)
            # Skip generated artifacts (signal scans are in 06 Logs/Signals — they're
            # generated daily and should not be "gardened")
            if rel.startswith("06 Logs/"):
                continue
            age_days = int((now - mtime) / 86400)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
                # Strip YAML frontmatter
                text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
                snippet = text.strip()[:300]
                if len(snippet) < 50:
                    continue
            except Exception:
                continue
            yield path, rel, age_days, snippet


def find_recent_signals(days_back=14):
    """Return recent signal scan texts as a list."""
    signals = []
    now = dt.date.today()
    for i in range(days_back):
        d = now - dt.timedelta(days=i)
        path = os.path.join(SIGNALS_DIR, "%s.md" % d.strftime("%Y-%m-%d"))
        if os.path.exists(path):
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
                # Strip frontmatter, keep the signal body
                text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
                signals.append({"date": d.strftime("%Y-%m-%d"), "text": text[:2000]})
            except Exception:
                pass
    return signals


def find_related_signals(note_text, signals, threshold=0.35):
    """Find signal passages semantically related to a note's content."""
    note_emb = _embed(note_text[:500])
    if not note_emb:
        return []
    related = []
    for sig in signals:
        sig_emb = _embed(sig["text"][:500])
        if not sig_emb:
            continue
        score = _cosine_sim(note_emb, sig_emb)
        if score >= threshold:
            related.append({"date": sig["date"], "score": round(score, 3),
                            "snippet": sig["text"][:200]})
    related.sort(key=lambda r: -r["score"])
    return related[:3]


def propose_update(note_rel, note_snippet, related_signals, age_days=30):
    """Write an update proposal into the /ingest inbox."""
    title = "gardener: %s" % note_rel.replace(".md", "").replace("/", " - ")
    slug = re.sub(r"[^a-z0-9]+", "-", note_rel.lower().replace(".md", "").replace("/", "-"))[:60]
    path = os.path.join(INBOX_DIR, "gardener-%s.md" % slug)
    if os.path.exists(path):
        return None  # already proposed

    lines = [
        "---",
        "source: knowledge-gardener",
        "date: %s" % dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "canonical: %s" % note_rel,
        "---",
        "",
        "# 🌱 Gardener Update: %s" % note_rel,
        "",
        "> This note is %d+ days old. Recent signals relate to its topic." % age_days,
        "",
        "## Current note (snippet)",
        "",
        "```",
        note_snippet[:500],
        "```",
        "",
    ]
    if related_signals:
        lines.append("## Related recent signals")
        lines.append("")
        for r in related_signals:
            lines.append("- **%.1f%%** match — %s" % (r["score"] * 100, r["date"]))
            lines.append("  %s" % r["snippet"])
            lines.append("")
    lines.append("---")
    lines.append("_Proposed by knowledge-gardener. Review and /ingest if useful._")
    os.makedirs(INBOX_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Knowledge Gardener — stale note proposals")
    ap.add_argument("--dry-run", action="store_true", help="plan only; no proposals")
    ap.add_argument("--stale-days", type=int, default=STALE_DAYS, help="stale threshold (default 30)")
    args = ap.parse_args()

    print("Knowledge Gardener — scanning for stale notes...", file=sys.stderr)

    stale = list(find_stale_notes(args.stale_days))
    print("  %d stale notes found (≥%d days without edit)" % (len(stale), args.stale_days),
          file=sys.stderr)

    if not stale:
        print("  Nothing to garden.", file=sys.stderr)
        print("Gardener: no stale notes found")
        return 0

    signals = find_recent_signals()
    print("  %d recent signal scans loaded" % len(signals), file=sys.stderr)

    proposed = 0
    for path, rel, age, snippet in stale[:MAX_PROPOSALS * 2]:  # scan up to 10
        if proposed >= MAX_PROPOSALS:
            break
        related = find_related_signals(snippet, signals)
        if not related:
            continue
        if args.dry_run:
            proposed += 1
            print("  [dry] %s (%d days) — %d related signals" % (rel, age, len(related)),
                  file=sys.stderr)
            continue
        prop_path = propose_update(rel, snippet, related, age)
        if prop_path:
            proposed += 1
            print("  [proposed] %s → %s" % (rel, os.path.basename(prop_path)),
                  file=sys.stderr)

    if args.dry_run:
        print("\nDry run: %d proposals would be created." % proposed, file=sys.stderr)
    else:
        print("  %d proposal(s) dropped in _inbox/. Review and /ingest." % proposed,
              file=sys.stderr)
    print("Gardener: %d stale · %d proposals" % (len(stale), proposed))
    print("Done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
