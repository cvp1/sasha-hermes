#!/usr/bin/env python3
"""Inbox Triage Agent — watch Gmail + Proton for urgent emails between daily briefs.

Runs every 30min: checks Gmail and Proton for new email since last check,
classifies each by urgency, and alerts if anything needs same-day attention.

Design:
  - Gmail through `google_connector` (imap -> oauth behind one resolver)
  - Proton through `proton_connector` (the local Bridge, one transport home)
  - Classifies with local gemma4:e4b on .21 — sensitive data stays local
  - Tracks state in a JSON file (last_check timestamp, seen IDs)
  - Alerts via Proton email only when something actionable appears
  - Per-account degradation: a dead mailbox is LOUD on stderr and does not
    take the other account with it; neither ever reports empty for unreadable

Usage:
    python3 inbox_triage.py [--dry-run] [--alert-email craig.vandeputte@proton.me]
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
GAPI = os.path.join(HOME, ".hermes/skills/productivity/google-workspace/scripts/google_api.py")
STATE_FILE = os.path.join(os.path.expanduser("~"), ".local", "state", "cc", "sasha", "inbox_triage_state.json")

OLLAMA_URL = "http://192.168.86.21:11434/api/chat"
OLLAMA_MODEL = "gemma4:e4b"
DEFAULT_ALERT = "craig.vandeputte@proton.me"

# Mail this pipeline sends to itself — the Work Brief (Gmail->Gmail,
# cognizant_brief.py) and this script's own "Inbox Triage" alert (->Proton).
# Both land back in an inbox this script polls; never classify or alert on
# them, or a stray URGENT hit re-alerts on its own alert forever (found
# 2026-07-21: 6 straight 30-min cycles alerting on nothing but the prior
# alert's subject literally matching the `urgent` pattern).
SELF_ADDRESSES = {"craig.vandeputte@gmail.com", "craig.vandeputte@proton.me"}

# Proton Mail is read through `proton_connector`, which owns the Bridge
# transport, the vault read and the OTP guard. There are no IMAP constants
# here any more — a second copy of them is a second thing to get wrong, and
# port 1143 is Sheridan's account.

URGENT_PATTERNS = [
    # \bgs\b (not gs\b): must be a standalone "GS" (the Goldman Sachs
    # shorthand), not a substring match on any word ending in "gs" —
    # earnings/savings/bookings/meetings/listings/flags all matched
    # gs\b and false-positived (found 2026-07-22, PayPal "earnings" email).
    r"goldman.?sachs", r"\bgs\b", r"cognizant", r"urgent", r"action required",
    r"deadline", r"asap", r"today", r"meeting.*change", r"schedule.*conflict",
    r"client.*call", r"review.*by",
]
TOP_PEOPLE = [
    "sheridan", "craig vandeputte",
]


def _guard_otp(subject, snippet):
    """Blank one-time codes in a fetched message before anything else sees it.

    Both fetchers now come through a connector, whose dispatcher runs this same
    guard in its sanitize step — one guard per path, and neither path is
    unguarded. This stays as the belt to that braces: it is the guard any future
    fetcher added to this file inherits, and the cost of calling it twice is
    nothing next to the cost of a code reaching the classifier prompt.

    Each field is the other's context: the wording that identifies an auth code
    usually sits in the subject while the digits sit in the body.
    """
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
    """Recent inbox messages, through the google-connector's one path.

    Was a direct ``google_auth.service("gmail", "v1")`` call, which died whole
    when the OAuth grant was revoked on 2026-09-16. The connector resolves
    ``imap -> oauth`` behind one resolver, so this survives an OAuth death.

    The OTP guard that used to run here now runs in the DISPATCHER's sanitize
    step (``google-connector/sanitize.py``), and the Proton fetcher below now
    comes through ``proton_connector`` for the same reason — one guard per path,
    and neither path is unguarded.

    Returns the same dict shape as before, so nothing downstream changes.
    """
    from google_connector import Caller, dispatch

    query = "in:inbox"
    if since_iso:
        d = dt.datetime.fromisoformat(since_iso)
        query += " after:%s" % d.strftime("%Y/%m/%d")

    env = dispatch("google_mail_search",
                   {"query": query, "count": min(max_results, 40)},
                   Caller("inbox_triage"))
    if env.status == "unavailable":
        # Loud and specific. Returning [] here would read as "no new mail",
        # which is the failure mode the envelope exists to prevent.
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
    """Is this message's own Date at or after ``since_iso``?

    Gmail's `after:` (and IMAP's `SINCE`) are CALENDAR-DAY granular, so a job
    that runs every 30 minutes and asks for "since 14:00" gets everything since
    midnight and re-triages the whole day (Grok G15, 2026-09-16). The server
    query stays as the cheap coarse filter; the exact bound is applied here,
    against the message's own parsed Date.

    An unparseable Date is KEPT: dropping a message because its header is
    malformed would be a silent loss, and the coarse window already bounds it.
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
    """Recent Proton INBOX messages, through the proton-connector's one path.

    Was raw ``imaplib`` in this file — its own socket, its own
    ``open(~/.key/proton_cvp)``, its own OTP guard — and every failure path
    ``return []``. An empty Proton inbox that is not empty is the exact failure
    the connector's envelope exists to prevent, and it read the vault outside
    ``_lib.secrets`` while doing it (Grok G18, 2026-09-16).

    Raises on ``unavailable``, exactly as the Gmail fetcher does; the caller
    isolates the two accounts so one dead mailbox does not take the other.
    """
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
    """Classify an email using local gemma4:e4b on .21. Returns (category, urgency)."""
    prompt = (
        "Classify this email into exactly one category. Reply with exactly one word.\n\n"
        "URGENT — a human is waiting on Craig for a same-day reply, a real deadline "
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
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False, "think": False,
        "options": {"num_predict": 10, "temperature": 0},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
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
    # Degrade toward safety: an unparseable verdict must never bury mail as NOISE.
    print("inbox_triage: unparseable verdict %r — defaulting to FYI" % out[:40],
          file=sys.stderr)
    return "FYI"


def _is_self_sent(email):
    """True if `from` is one of Craig's own monitored addresses. These are
    always either an already-read digest or this tool's own prior alert —
    never new mail needing triage."""
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

    # Fetch from both Gmail and Proton (Gmail uses date-based since, Proton
    # uses UNSEEN). One account being down must not take the other with it —
    # that shared-fate coupling is exactly what the connector cutover removes,
    # so it would be perverse to reintroduce it here. A Gmail outage is LOUD on
    # stderr and the Proton half still runs.
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

    # Classify each new email
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

    # Update state — keep IDs from both sources
    all_ids = set(e["id"] for e in all_emails)
    state["seen_ids"] = sorted(all_ids)[-200:]  # keep last 200
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
        # Alert
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
