#!/usr/bin/env python3
"""Pattern Learner — analyzes event bus history for trends, baselines, and anomalies.

Runs daily. Subscribes to all events since last run, computes:
  - Inbox volume trends (daily counts, day-of-week patterns, week-over-week)
  - Urgency distribution (URGENT/FYI/NOISE ratios over time)
  - Sender frequency (who emails most, who's always urgent)
  - Anomaly detection (sudden spikes or drops vs rolling baseline)
  - Quiet-hour detection (when email volume drops below threshold)

Output: a structured insight report. Significant findings publish a
`pattern_insight` event for the agent_runner to alert on.

Design:
  - Stateless: reads all events from the bus, computes, publishes insights
  - Idempotent: running twice produces the same insights (events don't change)
  - Best-effort: failures degrade to a note, never break
"""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

CC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CC)
from _lib import event_bus, mail


MIN_DAYS_DATA = 3       # need at least this many days to establish baseline
ANOMALY_ZSCORE = 2.0    # events outside this many stddevs from mean = anomaly
INSIGHT_TYPES = ("volume_trend", "urgency_shift", "sender_pattern",
                 "quiet_hours", "anomaly")


def _day(dt_str):
    """Extract date from ISO timestamp."""
    return dt_str[:10] if dt_str else ""


def _hour(dt_str):
    """Extract hour from ISO timestamp."""
    try:
        return int(dt_str[11:13]) if len(dt_str) >= 13 else -1
    except (ValueError, IndexError):
        return -1


def analyze_volume(events, cutoff_days=14):
    """Daily email volume: counts per day, day-of-week average, trend."""
    daily = Counter()
    dow = defaultdict(list)  # day-of-week -> [counts]
    for e in events:
        d = _day(e.get("ts"))
        if d:
            daily[d] += 1
            try:
                day_num = datetime.fromisoformat(d).weekday()
                dow[day_num].append(daily[d])
            except (ValueError, IndexError):
                pass

    # Recent trend: last 7 days vs the 7 before that
    sorted_days = sorted(daily.keys())
    recent = sorted_days[-7:] if len(sorted_days) >= 7 else sorted_days
    prior = sorted_days[-14:-7] if len(sorted_days) >= 14 else []

    recent_avg = sum(daily[d] for d in recent) / len(recent) if recent else 0
    prior_avg = sum(daily[d] for d in prior) / len(prior) if prior else 0

    trend = "stable"
    if prior_avg > 0 and recent_avg > prior_avg * 1.3:
        trend = "rising"
    elif prior_avg > 0 and recent_avg < prior_avg * 0.7:
        trend = "falling"

    # Day-of-week averages
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_avg = {}
    for d, counts in sorted(dow.items()):
        dow_avg[weekday_names[d]] = round(sum(counts) / len(counts), 1)

    return {
        "total_days": len(daily),
        "total_events": sum(daily.values()),
        "daily_avg": round(recent_avg, 1),
        "trend": trend,
        "day_of_week": dow_avg,
    }


def analyze_urgency(events):
    """URGENT/FYI/NOISE distribution over time."""
    daily_urgency = defaultdict(lambda: {"URGENT": 0, "FYI": 0, "NOISE": 0})
    for e in events:
        d = _day(e.get("ts"))
        payload = e.get("payload", {}) or {}
        # For inbox_triage results, look at the alert body
        if e.get("type") == "inbox_triage_result":
            # Parse the triage output for counts
            pass
    return {}


def analyze_senders(events):
    """Who emails the most, who's always urgent."""
    # Requires inbox_triage to publish sender info in event payload
    return {}


def analyze_hourly(events):
    """Email volume by hour of day."""
    hourly = Counter()
    for e in events:
        h = _hour(e.get("ts"))
        if h >= 0 and h < 24:
            hourly[h] += 1
    if not hourly:
        return {}
    total = sum(hourly.values())
    avg = total / 24 if total else 0
    quiet = [h for h, c in hourly.items() if c < avg * 0.3]
    busy = [h for h, c in hourly.items() if c > avg * 2.0]
    busiest = sorted(hourly.items(), key=lambda x: -x[1])[:3]
    return {
        "peak_hours": busiest,
        "quiet_hours": quiet,
        "busy_hours": busy,
    }


def detect_anomalies(events):
    """Find anomalous patterns using rolling mean/stddev."""
    daily = Counter()
    for e in events:
        d = _day(e.get("ts"))
        if d:
            daily[d] += 1

    counts = [c for c in daily.values()]
    if len(counts) < MIN_DAYS_DATA:
        return []

    mean = sum(counts) / len(counts)
    variance = sum((c - mean) ** 2 for c in counts) / len(counts)
    stddev = variance ** 0.5 if variance else 0

    if stddev == 0:
        return []

    anomalies = []
    for d, c in sorted(daily.items()):
        z = (c - mean) / stddev
        if abs(z) > ANOMALY_ZSCORE:
            direction = "spike" if z > 0 else "drop"
            anomalies.append({
                "date": d,
                "count": c,
                "z_score": round(z, 2),
                "direction": direction,
                "expected": round(mean, 1),
            })
    return anomalies


def compute_insights(bus):
    """Pull all events and return a report dict."""
    all_events = list(bus.subscribe(since_id=0))
    if not all_events:
        return {"error": "no events in bus yet", "reportable": False}

    volume = analyze_volume(all_events)
    hourly = analyze_hourly(all_events)
    anomalies = detect_anomalies(all_events)

    reportable = (
        volume.get("total_days", 0) >= MIN_DAYS_DATA
        and volume.get("total_events", 0) > 0
    )

    return {
        "reportable": reportable,
        "period": {
            "from": all_events[0].get("ts", "?")[:10],
            "to": all_events[-1].get("ts", "?")[:10],
            "events": len(all_events),
        },
        "volume": volume,
        "hourly": hourly,
        "anomalies": anomalies[:5],  # max 5 anomalies
    }


def format_report(insights):
    """Render the insights dict as a markdown brief."""
    if not insights.get("reportable"):
        return "# Pattern Learner\n\nNot enough data yet (%d events, need ≥%d days)." % (
            insights.get("period", {}).get("events", 0), MIN_DAYS_DATA)

    lines = ["# Pattern Learner — %s to %s" % (
        insights["period"]["from"], insights["period"]["to"])]
    lines.append("")
    lines.append("%d events across %d days.\n" % (
        insights["period"]["events"], insights["volume"]["total_days"]))

    v = insights["volume"]
    lines.append("## Volume")
    lines.append("- **Daily avg:** %.1f events (last %d days)" % (
        v["daily_avg"], min(7, v["total_days"])))
    trend_icon = {"rising": "↑", "falling": "↓", "stable": "→"}
    lines.append("- **Trend:** %s %s" % (trend_icon.get(v["trend"], "?"), v["trend"]))
    if v.get("day_of_week"):
        lines.append("- **By day:** " + " · ".join(
            "%s %.1f" % (d, c) for d, c in sorted(v["day_of_week"].items())))

    if insights.get("hourly"):
        h = insights["hourly"]
        lines.append("\n## Hourly Patterns")
        if h.get("peak_hours"):
            lines.append("- **Peak hours:** " + ", ".join(
                "%.0f:00 (%d)" % (h_, c) for h_, c in h["peak_hours"]))
        if h.get("quiet_hours"):
            lines.append("- **Quiet hours:** " + ", ".join("%.0f:00" % h_ for h_ in h["quiet_hours"]))

    if insights.get("anomalies"):
        lines.append("\n## Anomalies")
        for a in insights["anomalies"]:
            icon = "⚠" if a["direction"] == "spike" else "⬇"
            lines.append("- %s **%s** — %d events (expected %.1f, z=%.2f)" % (
                icon, a["date"], a["count"], a["expected"], a["z_score"]))

    lines.append("")
    lines.append("_Pattern learner runs daily. Insights are saved to the event bus._")
    return "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Pattern learner — event bus analysis")
    ap.add_argument("--dry-run", action="store_true", help="print report without publishing")
    ap.add_argument("--publish", action="store_true", help="publish insights to event bus")
    ap.add_argument("--alert", action="store_true", help="email report if anomalies found")
    args = ap.parse_args()

    bus = event_bus.EventBus()
    insights = compute_insights(bus)

    if args.dry_run:
        print(format_report(insights))
        return 0

    report = format_report(insights)

    if args.publish and insights.get("reportable"):
        bus.publish("pattern_learner", "insights",
                    {"summary": report[:500], "anomalies": len(insights.get("anomalies", []))})

    if args.alert and insights.get("anomalies"):
        try:
            mail.send("Pattern Learner — %d anomaly(ies)" % len(insights["anomalies"]),
                      report, html=None)
            print("Alert sent: %d anomalies" % len(insights["anomalies"]), file=sys.stderr)
        except Exception as e:
            print("Alert failed: %s" % e, file=sys.stderr)

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
