#!/usr/bin/env python3
"""Offline selftest for sasha-hermes' inbox triage.

Run: ``python3 sasha-hermes/selftest.py`` from ``~/Github/CC``.

No network, no vault, no mailbox: both fetchers are pointed at a fake
``dispatch`` and everything asserted is BEHAVIOUR — what the fetcher returns,
and what it does when a connector says ``unavailable``.

Written for the 2026-09-16 bug bash (rows 19 and 20), which found that the
Gmail fetcher's time window collapsed to calendar-day granularity and that the
Proton fetcher still spoke IMAP itself and returned ``[]`` on every failure.
There was no suite here before; there is one now, because a consumer with no
test is where a connector's guarantees quietly stop applying.
"""
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
sys.path.insert(0, CC)
sys.path.insert(0, HERE)

import inbox_triage as T                                   # noqa: E402

FAILS = []
CHECKS = []


def check(label, cond):
    CHECKS.append(bool(cond))
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


class FakeEnv:
    """An envelope shaped like the connector's."""

    def __init__(self, status="ok", data=None, code=None, recovery=""):
        self.status = status
        self.data = data
        self.warnings = []
        self.error = ({"code": code, "recovery": recovery} if code else None)


def _install_fake(module_name, env, seen):
    """Put a fake connector module in ``sys.modules`` so the fetcher imports it.

    The fetchers import their connector INSIDE the function, deliberately (the
    cron path must not pay for an import it may not use), which is also what
    makes this substitution possible without touching the code under test.
    """
    import types
    mod = types.ModuleType(module_name)

    def dispatch(name, args=None, caller=None):
        seen.append((name, dict(args or {})))
        return env

    mod.dispatch = dispatch
    mod.Caller = lambda *a, **k: None
    sys.modules[module_name] = mod
    return mod


def main():  # noqa: C901 — a flat list of assertions reads better than helpers
    # --- ROW 19: the since-window is exact, not calendar-day ---------------
    # Gmail's `after:` and IMAP's `SINCE` are day-granular, so a job that runs
    # every 30 minutes and asks for "since 14:00" was handed everything back to
    # midnight and re-triaged the whole day.
    since = "2026-09-16T14:00:00+00:00"
    rows = [
        {"id": "gm-1", "from": "a@x.io", "subject": "before the window",
         "date": "Wed, 16 Sep 2026 09:00:00 +0000"},
        {"id": "gm-2", "from": "b@x.io", "subject": "inside the window",
         "date": "Wed, 16 Sep 2026 15:30:00 +0000"},
        {"id": "gm-3", "from": "c@x.io", "subject": "exactly at the bound",
         "date": "Wed, 16 Sep 2026 14:00:00 +0000"},
        {"id": "gm-4", "from": "d@x.io", "subject": "unparseable date",
         "date": "not a date at all"},
    ]
    seen = []
    _install_fake("google_connector", FakeEnv("ok", rows), seen)
    got = T._fetch_emails(since)
    subjects = [m["subject"] for m in got]
    check("row 19: a message before the exact bound is dropped",
          "before the window" not in subjects)
    check("row 19: a message after the exact bound is kept",
          "inside the window" in subjects)
    check("row 19: a message exactly AT the bound is kept (>=, not >)",
          "exactly at the bound" in subjects)
    check("row 19: an unparseable Date is kept, never silently lost",
          "unparseable date" in subjects)
    check("row 19: the coarse server query is still sent",
          seen and "after:2026/09/16" in seen[0][1]["query"])
    check("row 19: no since means no client-side filtering",
          len(T._fetch_emails(None)) == len(rows))

    # The predicate itself, directly.
    check("row 19: _at_or_after is inclusive at the bound",
          T._at_or_after("Wed, 16 Sep 2026 14:00:00 +0000", since))
    check("row 19: _at_or_after refuses a second before the bound",
          not T._at_or_after("Wed, 16 Sep 2026 13:59:59 +0000", since))
    check("row 19: a garbage since_iso does not drop everything",
          T._at_or_after("Wed, 16 Sep 2026 09:00:00 +0000", "not-a-time"))

    # --- ROW 20: Proton goes through the connector, and a failure is LOUD --
    # It used to open its own IMAP socket, read ~/.key directly, and return []
    # on every failure — an empty Proton inbox that is not empty.
    proton_rows = [{"id": "uid-7", "from": "e@x.io", "subject": "hello",
                    "date": "Wed, 16 Sep 2026 15:00:00 +0000"}]
    pseen = []
    _install_fake("proton_connector", FakeEnv("ok", proton_rows), pseen)
    got = T._fetch_proton_emails()
    check("row 20: proton mail arrives through the connector's dispatch",
          pseen and pseen[0][0] == "proton_mail_recent")
    check("row 20: the rows keep the shape the rest of the file expects",
          len(got) == 1 and got[0]["id"] == "proton_uid-7"
          and got[0]["source"] == "proton")
    check("row 20: the count is bounded before it leaves",
          pseen[0][1].get("count") == 15)

    pseen2 = []
    _install_fake("proton_connector",
                  FakeEnv("unavailable", None, code="AUTH_RECONSENT_REQUIRED",
                          recovery="copy the bridge password from the UI"),
                  pseen2)
    try:
        T._fetch_proton_emails()
        check("row 20: an unreadable Proton mailbox is NOT an empty one", False)
    except RuntimeError as e:
        check("row 20: an unreadable Proton mailbox is NOT an empty one",
              "AUTH_RECONSENT_REQUIRED" in str(e))
        check("row 20: ...and the refusal names the human's next step",
              "bridge password" in str(e))

    # And the Gmail half still behaves the same way it already did.
    _install_fake("google_connector",
                  FakeEnv("unavailable", None, code="VAULT_LOCKED",
                          recovery="run keyvault/unlock.sh"), [])
    try:
        T._fetch_emails(since)
        check("an unreadable Gmail mailbox is NOT an empty one", False)
    except RuntimeError as e:
        check("an unreadable Gmail mailbox is NOT an empty one",
              "VAULT_LOCKED" in str(e))

    # --- the OTP guard is still on this path ------------------------------
    subj, snip = T._guard_otp("Your verification code is 483920",
                              "Enter this code to complete your sign-in.")
    check("a one-time code never survives the triage record",
          "483920" not in subj and "483920" not in snip)

    print("-" * 46)
    if FAILS:
        print("sasha-hermes selftest: FAIL %d/%d -> %s"
              % (len(FAILS), len(CHECKS), ", ".join(FAILS)))
        return 1
    print("sasha-hermes selftest: PASS %d/%d" % (len(CHECKS), len(CHECKS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
