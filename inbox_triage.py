#!/usr/bin/env python3
"""Inbox Triage Agent — watch Gmail + Proton for urgent emails between daily briefs.

Runs every 30min: checks Gmail (via Google API) and Proton (via Bridge IMAP
port 1144) for new email since last check, classifies each by urgency, and
alerts if anything needs same-day attention.

Design:
  - Gmail via Google API (already OAuth-authenticated) — no Claude dependency
  - Proton via local Bridge IMAP (127.0.0.1:1144, self-signed cert)
  - Classifies with local gemma4:e4b on .21 — sensitive data stays local
  - Tracks state in a JSON file (last_check timestamp, seen IDs)
  - Alerts via Proton email only when something actionable appears
  - Best-effort: failures degrade silently (the morning brief is the source of truth)

Usage:
    python3 inbox_triage.py [--dry-run] [--alert-email craig.vandeputte@proton.me]
"""
import datetime as dt
import email as eml
import imaplib
import json
import os
import re
import ssl
import sys
import urllib.request
from email.header import decode_header
from email.utils import parseaddr

HOME = os.path.expanduser("~")
CC = os.path.join(HOME, "Github", "CC")
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

# Proton Mail Bridge IMAP
PROTON_HOST = "127.0.0.1"
PROTON_PORT = 1144
PROTON_USER = "craig.vandeputte@proton.me"
PROTON_PW_FILE = "~/.key/proton_cvp"

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
    """Fetch recent inbox messages via Google API. Returns list of dicts."""
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    tok = os.path.join(HOME, ".hermes", "google_token.json")
    creds = Credentials.from_authorized_user_file(tok)
    service = build("gmail", "v1", credentials=creds)

    query = "in:inbox"
    if since_iso:
        # Use date-based query for simplicity
        d = dt.datetime.fromisoformat(since_iso)
        query += " after:%s" % d.strftime("%Y/%m/%d")

    results = service.users().messages().list(
        userId="me", maxResults=max_results, q=query,
    ).execute()

    msgs = []
    for m in results.get("messages", []):
        meta = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date", "X-Priority"]
        ).execute()
        hdrs = {h["name"]: h["value"] for h in meta.get("payload", {}).get("headers", [])}
        msg_id = m["id"]
        fr = hdrs.get("From", "?")
        subj = hdrs.get("Subject", "(no subject)")
        snippet = (meta.get("snippet") or "")[:200]
        date_str = hdrs.get("Date", "")
        msgs.append({
            "id": "gmail_%s" % msg_id,
            "from": fr,
            "subject": subj,
            "snippet": snippet,
            "date": date_str,
            "internal_date": meta.get("internalDate", "0"),
            "source": "gmail",
        })
    return msgs


def _fetch_proton_emails(max_results=15):
    """Fetch unseen inbox messages via Proton Mail Bridge IMAP.

    Returns same dict format as _fetch_emails() with a ``source``: ``"proton"``
    key so we can distinguish sources later. Best-effort — failures return [].
    """
    pw_file = os.path.expanduser(PROTON_PW_FILE)
    if not os.path.isfile(pw_file):
        print("  Proton: no password file at %s" % pw_file, file=sys.stderr)
        return []
    pw = open(pw_file).read().strip()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        M = imaplib.IMAP4(PROTON_HOST, PROTON_PORT, timeout=10)
        M.starttls(ctx)
        M.login(PROTON_USER, pw)
    except Exception as e:
        print("  Proton: connection/login failed — %s" % e, file=sys.stderr)
        return []

    try:
        typ, data = M.select("INBOX")
        if typ != "OK":
            print("  Proton: select INBOX failed — %s" % data, file=sys.stderr)
            return []

        typ, data = M.search(None, "UNSEEN")
        unseen_ids = data[0].split() if data[0] else []

        msgs = []
        for uid in unseen_ids[:max_results]:
            typ, fetch_data = M.fetch(uid, "(BODY.PEEK[] INTERNALDATE)")
            if typ != "OK" or not fetch_data or fetch_data[0] is None:
                continue

            raw = fetch_data[0]
            raw_bytes = raw[1] if isinstance(raw, tuple) and len(raw) > 1 else None
            if raw_bytes is None:
                continue

            msg = eml.message_from_bytes(raw_bytes)
            fr = str(decode_header(msg.get("From", "?"))[0][0], "utf-8", "replace") \
                if isinstance(decode_header(msg.get("From", "?"))[0][0], bytes) \
                else decode_header(msg.get("From", "?"))[0][0]
            subj = str(decode_header(msg.get("Subject", "(no subject)"))[0][0], "utf-8", "replace") \
                if isinstance(decode_header(msg.get("Subject", "(no subject)"))[0][0], bytes) \
                else decode_header(msg.get("Subject", "(no subject)"))[0][0]

            # Get plain-text snippet
            snippet = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            snippet = payload.decode(
                                part.get_content_charset() or "utf-8", "replace"
                            )[:200].replace("\n", " ")
                        break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    snippet = payload.decode(
                        msg.get_content_charset() or "utf-8", "replace"
                    )[:200].replace("\n", " ")

            msgs.append({
                "id": "proton_%s" % uid.decode(),
                "from": str(fr),
                "subject": str(subj),
                "snippet": snippet,
                "date": msg.get("Date", ""),
                "source": "proton",
            })

        return msgs
    finally:
        try:
            M.logout()
        except Exception:
            pass


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
    except Exception:
        out = ""
    for cat in ("URGENT", "FYI", "NOISE"):
        if cat in out:
            return cat
    return "NOISE"


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

    # Fetch from both Gmail and Proton (Gmail uses date-based since, Proton uses UNSEEN)
    gmail_emails = _fetch_emails(since)
    proton_emails = _fetch_proton_emails()

    all_emails = gmail_emails + proton_emails
    new_emails = [e for e in all_emails if e["id"] not in seen]

    gmail_new = sum(1 for e in new_emails if e["source"] == "gmail")
    proton_new = sum(1 for e in new_emails if e["source"] == "proton")
    print("  Gmail: %d new · %d total   Proton: %d unseen"
          % (gmail_new, len(gmail_emails), len(proton_emails)),
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
