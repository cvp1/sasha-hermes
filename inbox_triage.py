#!/usr/bin/env python3
"""Inbox triage: check Gmail and Proton for new mail, classify urgency, alert on urgent items.

Classification runs on a local Ollama model. One unreadable account is reported
on stderr and does not stop the other.

Usage:
    python3 inbox_triage.py [--dry-run] [--alert-email ADDRESS]
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from email.utils import parseaddr

HOME = os.path.expanduser("~")
CC = os.path.join(HOME, "Github", "CC")

sys.path.insert(0, CC)
from _lib.otp_guard import redact_field  # noqa: E402
from _lib import control_tokens  # noqa: E402
GAPI = os.path.join(HOME, ".hermes/skills/productivity/google-workspace/scripts/google_api.py")
STATE_FILE = os.path.join(os.path.expanduser("~"), ".local", "state", "cc", "sasha", "inbox_triage_state.json")

OLLAMA_URL = "http://192.168.86.21:11434/api/chat"
OLLAMA_MODEL = "gemma4:e4b"
DEFAULT_ALERT = os.environ.get("INBOX_TRIAGE_ALERT_EMAIL", "you@example.com")


def _env_list(name):
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


# Addresses this pipeline sends from; skipped so its own alerts never re-trigger.
SELF_ADDRESSES = {a.lower() for a in _env_list("INBOX_TRIAGE_SELF_ADDRESSES")}

URGENT_PATTERNS = [
    r"urgent", r"action required",
    r"deadline", r"asap", r"today", r"meeting.*change", r"schedule.*conflict",
    r"client.*call", r"review.*by",
] + _env_list("INBOX_TRIAGE_URGENT_PATTERNS")
TOP_PEOPLE = [p.lower() for p in _env_list("INBOX_TRIAGE_TOP_PEOPLE")]


def _guard_otp(subject, snippet):
    """Blank one-time codes in subject and snippet, each using the other as context."""
    return (redact_field(subject, context=snippet),
            redact_field(snippet, context=subject))


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _load_state():
    try:
        return json.loads(open(STATE_FILE).read())
    except (OSError, ValueError):
        return {"last_check": None, "seen_ids": [], "last_alert": None}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


def _fetch_emails(since_iso, max_results=15):
    """Fetch recent Gmail inbox messages via google_connector; raise if unavailable."""
    from google_connector import Caller, dispatch

    query = "in:inbox"
    if since_iso:
        d = dt.datetime.fromisoformat(since_iso)
        query += " after:%s" % d.strftime("%Y/%m/%d")

    env = dispatch("google_mail_search",
                   {"query": query, "count": min(max_results, 40)},
                   Caller("inbox_triage"))
    if env.status == "unavailable":
        # Raise rather than return [], which would read as "no new mail".
        raise RuntimeError("gmail fetch unavailable: %s — %s"
                           % ((env.error or {}).get("code"),
                              (env.error or {}).get("recovery") or "no recovery given"))
    for w in env.warnings:
        print("inbox_triage: %s" % w, file=sys.stderr)

    msgs = []
    for row in env.data or []:
        if not _at_or_after(row.get("date", ""), since_iso):
            continue
        msgs.append({
            "id": "gmail_%s" % row["id"],
            "from": row.get("from", "?"),
            "subject": row.get("subject") or "(no subject)",
            "snippet": "",   # the connector's list tools return headers only
            "date": row.get("date", ""),
            "internal_date": "0",
            "source": "gmail",
        })
    return msgs


def _at_or_after(date_header, since_iso):
    """True if the message Date is at or after since_iso (the server query is day-granular).

    Unparseable dates are kept rather than silently dropped.
    """
    if not since_iso:
        return True
    try:
        since = dt.datetime.fromisoformat(since_iso)
    except ValueError:
        return True
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return True
    if when is None:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=dt.timezone.utc)
    return when >= since


def _fetch_proton_emails(max_results=15):
    """Fetch recent Proton inbox messages via proton_connector; raise if unavailable."""
    from proton_connector import Caller, dispatch

    env = dispatch("proton_mail_recent", {"count": min(max_results, 40)},
                   Caller("inbox_triage"))
    if env.status == "unavailable":
        raise RuntimeError("proton fetch unavailable: %s — %s"
                           % ((env.error or {}).get("code"),
                              (env.error or {}).get("recovery") or "no recovery given"))
    for w in env.warnings:
        print("inbox_triage: %s" % w, file=sys.stderr)
    msgs = []
    for row in env.data or []:
        msgs.append({
            "id": "proton_%s" % row["id"],
            "from": row.get("from", "?"),
            "subject": row.get("subject") or "(no subject)",
            "snippet": "",   # the connector's list tools return headers only
            "date": row.get("date", ""),
            "source": "proton",
        })
    return msgs


def _classify_local(email_text):
    """Classify an email as URGENT, FYI or NOISE with the local model; FYI on any failure."""
    prompt = (
        "Classify this email into exactly one category. Reply with exactly one word.\n\n"
        "URGENT — a human is waiting on the user for a same-day reply, a real deadline "
        "lands today or tomorrow, or it is a client/work escalation. Do NOT mark it "
        "urgent just because it mentions money, a due date, or the word "
        "statement/payment — routine bills and account statements are never urgent "
        "unless they say overdue, suspended, fraud, or dispute.\n"
        "FYI — worth knowing, no reply needed today (routine account/billing "
        "notices, calendar-adjacent updates, personal correspondence with no "
        "deadline).\n"
        "NOISE — newsletter, marketing, automated notification, low-priority.\n\n"
        "Email:\n%s\n\nCategory:" % email_text[:1000]
    )
    try:
        # Neutralize control tokens so email text cannot forge chat turns.
        messages = control_tokens.neutralize_messages(
            [{"role": "user", "content": prompt}],
            OLLAMA_URL.rsplit("/api/", 1)[0], OLLAMA_MODEL)
        body = json.dumps({
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False, "think": False,
            "options": {"num_predict": 10, "temperature": 0},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        out = resp.get("message", {}).get("content", "").strip().upper()
    except Exception as e:
        print("inbox_triage: classifier call failed (%s) — defaulting to FYI" % e,
              file=sys.stderr)
        return "FYI"
    for cat in ("URGENT", "FYI", "NOISE"):
        if cat in out:
            return cat
    # An unparseable verdict must never bury mail as NOISE.
    print("inbox_triage: unparseable verdict %r — defaulting to FYI" % out[:40],
          file=sys.stderr)
    return "FYI"


def _is_self_sent(email):
    """True if the sender is one of SELF_ADDRESSES."""
    return parseaddr(email.get("from", ""))[1].lower() in SELF_ADDRESSES


def _is_urgent_by_pattern(email):
    """Check sender/subject for known urgent patterns."""
    text = (email.get("from", "") + " " + email.get("subject", "")).lower()
    for pat in URGENT_PATTERNS:
        if re.search(pat, text):
            return True
    return False


def _format_alert(emails):
    """Build a brief alert email body."""
    lines = ["## Inbox Triage — Urgent Items", "",
             "%d new email(s) since last check." % len(emails), ""]
    urgent = [e for e in emails if e.get("triage") in ("URGENT", "FYI")]
    for e in urgent[:8]:
        lines.append("**%s** — %s" % (e.get("from", "?"), e.get("subject", "")))
        lines.append("> %s" % e.get("snippet", ""))
        lines.append("")
    if not urgent:
        lines.append("No urgent items — inbox is quiet.")
    return "\n".join(lines)


def _json_output(new_emails):
    """Build rich JSON output with all email details for the dashboard."""
    urgent = [e for e in new_emails if e.get("triage") == "URGENT"]
    fyi = [e for e in new_emails if e.get("triage") == "FYI"]
    noise = [e for e in new_emails if e.get("triage") == "NOISE"]
    return json.dumps({
        "summary": "Triage: %d new · %d urgent · %d FYI · %d noise" % (
            len(new_emails), len(urgent), len(fyi), len(noise)),
        "count": len(new_emails),
        "urgent": len(urgent),
        "fyi": len(fyi),
        "noise": len(noise),
        "emails": [{
            "from": e.get("from",""),
            "subject": e.get("subject",""),
            "snippet": e.get("snippet","")[:150],
            "source": e.get("source",""),
            "triage": e.get("triage","?"),
            "urgent_pattern": e.get("urgent_pattern", False),
        } for e in new_emails],
    }, ensure_ascii=False)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Inbox triage agent")
    ap.add_argument("--dry-run", action="store_true", help="check but don't alert")
    ap.add_argument("--alert-email", default=DEFAULT_ALERT, help="where to send alerts")
    args = ap.parse_args()

    state = _load_state()
    since = state.get("last_check")
    seen = set(state.get("seen_ids", []))

    print("Inbox triage — checking since %s" % (since or "start of day"),
          file=sys.stderr)

    # One unavailable account must not stop the other.
    try:
        gmail_emails = _fetch_emails(since)
    except Exception as e:  # noqa: BLE001 — degrade one account, not the run
        print("  Gmail: UNAVAILABLE — %s" % e, file=sys.stderr)
        gmail_emails = []
    try:
        proton_emails = _fetch_proton_emails()
    except Exception as e:  # noqa: BLE001 — degrade one account, not the run
        print("  Proton: UNAVAILABLE — %s" % e, file=sys.stderr)
        proton_emails = []

    all_emails = gmail_emails + proton_emails
    new_emails = [e for e in all_emails if e["id"] not in seen]

    gmail_new = sum(1 for e in new_emails if e["source"] == "gmail")
    proton_new = sum(1 for e in new_emails if e["source"] == "proton")
    print("  Gmail: %d new · %d total   Proton: %d new · %d total"
          % (gmail_new, len(gmail_emails), proton_new, len(proton_emails)),
          file=sys.stderr)

    self_sent = [e for e in new_emails if _is_self_sent(e)]
    new_emails = [e for e in new_emails if not _is_self_sent(e)]
    if self_sent:
        print("  Skipped %d self-sent item(s) (digest/own alert, never triage-worthy): %s"
              % (len(self_sent), ", ".join(e["subject"][:40] for e in self_sent)),
              file=sys.stderr)

    if not new_emails:
        print("  Nothing new.", file=sys.stderr)
        print(json.dumps({"summary":"Triage: no new emails","count":0,"emails":[]}))
        return 0

    urgent = []
    for e in new_emails:
        tag = "[G]" if e["source"] == "gmail" else "[P]"
        text = "From: %s\nSubject: %s\n%s" % (e["from"], e["subject"], e["snippet"])
        cat = _classify_local(text)
        e["triage"] = cat
        if _is_urgent_by_pattern(e):
            e["urgent_pattern"] = True
            cat = "URGENT"
            e["triage"] = cat
        if cat == "URGENT":
            urgent.append(e)
        label = {"URGENT": "⚠", "FYI": "→", "NOISE": "·"}
        flag = label.get(cat, "?")
        print("  %s %s %s — %s" % (tag, flag, e["subject"][:60], cat), file=sys.stderr)

    all_ids = set(e["id"] for e in all_emails)
    state["seen_ids"] = sorted(all_ids)[-200:]
    state["last_check"] = _now_iso()

    if args.dry_run:
        print("\nDry run: %d urgent, %d FYI, %d noise" %
              (len([e for e in new_emails if e.get("triage") == "URGENT"]),
               len([e for e in new_emails if e.get("triage") == "FYI"]),
               len([e for e in new_emails if e.get("triage") == "NOISE"])),
              file=sys.stderr)
        _save_state(state)
        print(_json_output(new_emails))
        return 0

    if urgent:
        body = _format_alert(new_emails)
        try:
            sys.path.insert(0, CC)
            from _lib import mail
            mail.send("Inbox Triage — %d urgent" % len(urgent), body, html=None)
            state["last_alert"] = _now_iso()
            print("  Alert sent for %d urgent item(s)" % len(urgent), file=sys.stderr)
        except Exception as e:
            print("  Alert failed: %s" % e, file=sys.stderr)
    else:
        print("  No urgent items — no alert sent.", file=sys.stderr)

    _save_state(state)
    print(_json_output(new_emails))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
