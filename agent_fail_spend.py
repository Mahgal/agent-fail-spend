#!/usr/bin/env python3
"""agent-fail-spend: where did your coding-agent money actually go?

Reads coding-agent session transcripts (or generic JSONL run logs) and
reports spend split by run outcome: passed / failed / timeout / unknown,
with the dollars wasted on runs that never passed.

Why it exists: Runta's FrontierHarness Eval v1.1 (2026-11-17) ran 27,280
coding-agent runs and found 64.5% of list-price cost went to trials that
did not pass (52.7% failures + 11.8% timeouts). Most people only ever see
one monthly total. This tool splits that total by outcome, per session,
so the waste is visible instead of averaged away.

Inputs
------
Claude Code transcripts (default): ``~/.claude/projects/*/*.jsonl``.
Parses ``result`` messages (subtype success/error, total_cost_usd,
duration_ms) and assistant messages carrying ``usage`` token counts and
a model name. Embedded cost fields win whenever they are present.

Generic JSONL run logs (``--format generic``): one object per line with
fields like ``{"id": ..., "status": ..., "cost_usd": ...}`` or
``{"id": ..., "outcome": ..., "tokens": ..., "model": ...}``. Status
synonyms accepted:

    passed : pass, passed, success, succeeded, ok
    failed : fail, failed, failure, error, errored
    timeout: timeout, timed_out, timed-out, timedout

Pricing: token -> dollars ONLY comes from a user-supplied ``--prices``
JSON file. Model prices are never hard-coded here, because list prices
change and unverifiable numbers would be invented dollars. If neither an
embedded cost nor a prices file is available, tokens are reported and
cost is marked unknown.

Prices file format (per 1M tokens, USD)::

    {
      "claude-sonnet-4-5": {
        "input": 3.00, "output": 15.00,
        "cache_read": 0.30, "cache_creation": 3.75
      }
    }

Exit codes: 0 when a report is produced (even when failed spend is
greater than zero -- the report IS the product), 2 on a bad input path
or unreadable files. Malformed lines are skipped with a warning count,
never a crash.

Stdlib only. No network, no API calls, reads local files only.
"""

import argparse
import json
import os
import sys

VERSION = "1.0.0"

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_UNKNOWN = "unknown"
OUTCOMES = (OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_TIMEOUT, OUTCOME_UNKNOWN)

STATUS_SYNONYMS = {
    "pass": OUTCOME_PASSED, "passed": OUTCOME_PASSED,
    "success": OUTCOME_PASSED, "succeeded": OUTCOME_PASSED,
    "ok": OUTCOME_PASSED,
    "fail": OUTCOME_FAILED, "failed": OUTCOME_FAILED,
    "failure": OUTCOME_FAILED, "error": OUTCOME_FAILED,
    "errored": OUTCOME_FAILED,
    "timeout": OUTCOME_TIMEOUT, "timed_out": OUTCOME_TIMEOUT,
    "timed-out": OUTCOME_TIMEOUT, "timedout": OUTCOME_TIMEOUT,
}

TOKEN_FIELDS = ("input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens")


def classify_status(raw):
    """Map a free-form status string to passed/failed/timeout/unknown."""
    if raw is None:
        return OUTCOME_UNKNOWN
    key = str(raw).strip().lower().replace(" ", "_")
    return STATUS_SYNONYMS.get(key, OUTCOME_UNKNOWN)


def _is_timeout_error(result_msg):
    """True when a result message says the run timed out."""
    subtype = str(result_msg.get("subtype", "")).lower()
    if "timeout" in subtype or "timed_out" in subtype:
        return True
    err = result_msg.get("error")
    if isinstance(err, str) and "tim" in err.lower() and "out" in err.lower():
        return True
    if isinstance(err, dict):
        text = str(err.get("message", "")).lower()
        if "timed out" in text or "timeout" in text:
            return True
    return False


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SessionResult:
    """One parsed session (one transcript file) or one generic run."""

    def __init__(self, name):
        self.name = name
        self.outcome = OUTCOME_UNKNOWN
        self.cost_usd = None          # None means unknown
        self.tokens = 0
        self.duration_ms = None
        self.model = None
        self._result_seen = False

    def cost_display(self):
        return "$%.2f" % self.cost_usd if self.cost_usd is not None else "unknown"


def parse_claude_transcript(path, prices):
    """Parse one Claude Code session transcript (.jsonl)."""
    session = SessionResult(os.path.basename(path))
    usage = {f: 0 for f in TOKEN_FIELDS}
    usage_seen = False
    malformed = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(obj, dict):
                malformed += 1
                continue
            mtype = obj.get("type")
            if mtype == "result":
                session._result_seen = True
                subtype = str(obj.get("subtype", "")).lower()
                if _is_timeout_error(obj):
                    session.outcome = OUTCOME_TIMEOUT
                elif subtype == "success" and not obj.get("is_error"):
                    session.outcome = OUTCOME_PASSED
                elif subtype == "error" or obj.get("is_error"):
                    session.outcome = OUTCOME_FAILED
                cost = _num(obj.get("total_cost_usd", obj.get("costUSD")))
                if cost is not None:
                    session.cost_usd = cost
                dur = _num(obj.get("duration_ms"))
                if dur is not None:
                    session.duration_ms = int(dur)
                # Result messages sometimes carry usage too; use it only
                # when no assistant message supplied any.
                ru = obj.get("usage")
                if isinstance(ru, dict) and not usage_seen:
                    for f in TOKEN_FIELDS:
                        if isinstance(ru.get(f), (int, float)):
                            usage[f] += int(ru[f])
                            usage_seen = True
            elif mtype == "message":
                msg = obj.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                model = msg.get("model")
                if model and not session.model:
                    session.model = model
                u = msg.get("usage") or {}
                if isinstance(u, dict):
                    for f in TOKEN_FIELDS:
                        if isinstance(u.get(f), (int, float)):
                            usage[f] += int(u[f])
                            usage_seen = True
    session.tokens = sum(usage.values())
    if session.cost_usd is None and usage_seen and prices:
        session.cost_usd = price_usage(usage, session.model, prices)
    return session, malformed


def price_usage(usage, model, prices):
    """Cost a token-usage dict from a user-supplied prices table.

    Prices are per 1M tokens, keyed by model name, with keys
    input / output / cache_read / cache_creation. Returns None when the
    model has no entry (cost stays unknown rather than invented).
    """
    if not model or model not in prices:
        return None
    entry = prices[model]
    if not isinstance(entry, dict):
        return None
    rate = {
        "input_tokens": entry.get("input"),
        "output_tokens": entry.get("output"),
        "cache_read_input_tokens": entry.get("cache_read"),
        "cache_creation_input_tokens": entry.get("cache_creation"),
    }
    total = 0.0
    for field, per_million in rate.items():
        if per_million is None:
            continue
        try:
            total += usage.get(field, 0) * float(per_million) / 1_000_000
        except (TypeError, ValueError):
            continue
    return total


def parse_generic_log(path):
    """Parse one generic JSONL run log; one object per line."""
    sessions = []
    malformed = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(obj, dict):
                malformed += 1
                continue
            name = str(obj.get("id") or obj.get("run_id") or "run")
            s = SessionResult(name)
            raw = obj.get("status", obj.get("outcome"))
            s.outcome = classify_status(raw)
            cost = _num(obj.get("cost_usd", obj.get("costUSD", obj.get("cost"))))
            if cost is not None:
                s.cost_usd = cost
            tokens = obj.get("tokens")
            if isinstance(tokens, dict):
                s.tokens = sum(int(v) for v in tokens.values()
                               if isinstance(v, (int, float)))
            elif isinstance(tokens, (int, float)):
                s.tokens = int(tokens)
            else:
                s.tokens = sum(int(obj.get(f, 0)) for f in TOKEN_FIELDS
                               if isinstance(obj.get(f), (int, float)))
            dur = _num(obj.get("duration_ms", obj.get("duration_seconds")))
            if dur is not None:
                s.duration_ms = int(dur * 1000) if "duration_seconds" in obj \
                    else int(dur)
            s.model = obj.get("model")
            sessions.append(s)
    return sessions, malformed


def collect_paths(targets, fmt):
    """Expand --dir/--file targets into a sorted list of .jsonl files."""
    files = []
    for target in targets:
        if os.path.isfile(target):
            files.append(target)
        elif os.path.isdir(target):
            for root, _dirs, names in os.walk(target):
                for name in sorted(names):
                    if name.endswith(".jsonl"):
                        files.append(os.path.join(root, name))
        else:
            return None, "no such file or directory: %s" % target
    files.sort()
    return files, None


def load_prices(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("prices file must be a JSON object keyed by model")
    return data


def build_report(sessions, top_n):
    """Aggregate sessions into totals + top failed sessions."""
    known = [s for s in sessions if s.cost_usd is not None]
    total = sum(s.cost_usd for s in known)
    failed = sum(s.cost_usd for s in known if s.outcome == OUTCOME_FAILED)
    timeout = sum(s.cost_usd for s in known if s.outcome == OUTCOME_TIMEOUT)
    passed = sum(s.cost_usd for s in known if s.outcome == OUTCOME_PASSED)
    unknown_cost = sum(s.cost_usd for s in known
                       if s.outcome == OUTCOME_UNKNOWN)
    wasted_pct = (failed / total * 100) if total > 0 else 0.0
    by_outcome = {o: sum(1 for s in sessions if s.outcome == o)
                  for o in OUTCOMES}
    failed_sessions = sorted(
        (s for s in known if s.outcome == OUTCOME_FAILED),
        key=lambda s: -s.cost_usd)[:top_n]
    return {
        "sessions": sessions,
        "total_cost_usd": total,
        "failed_cost_usd": failed,
        "timeout_cost_usd": timeout,
        "passed_cost_usd": passed,
        "unknown_outcome_cost_usd": unknown_cost,
        "wasted_pct": wasted_pct,
        "counts": by_outcome,
        "unknown_cost_sessions": sum(1 for s in sessions
                                     if s.cost_usd is None),
        "top_failed": failed_sessions,
    }


def _dur_str(ms):
    if ms is None:
        return "-"
    seconds = ms / 1000.0
    if seconds < 60:
        return "%.0fs" % seconds
    return "%.1fm" % (seconds / 60.0)


def render_text(report, top_n):
    lines = []
    lines.append("agent-fail-spend  --  spend by run outcome")
    lines.append("")
    header = "%-34s %-8s %10s %10s %8s" % (
        "session", "outcome", "cost", "tokens", "duration")
    lines.append(header)
    lines.append("-" * len(header))
    for s in report["sessions"]:
        lines.append("%-34s %-8s %10s %10d %8s" % (
            s.name[:34], s.outcome, s.cost_display(), s.tokens,
            _dur_str(s.duration_ms)))
    lines.append("-" * len(header))
    lines.append("sessions: %d  (passed %d, failed %d, timeout %d, unknown %d)"
                 % (len(report["sessions"]), report["counts"]["passed"],
                    report["counts"]["failed"], report["counts"]["timeout"],
                    report["counts"]["unknown"]))
    lines.append("total spend:   $%.2f" % report["total_cost_usd"])
    lines.append("passed spend:  $%.2f" % report["passed_cost_usd"])
    lines.append("failed spend:  $%.2f" % report["failed_cost_usd"])
    lines.append("timeout spend: $%.2f" % report["timeout_cost_usd"])
    if report["unknown_cost_sessions"]:
        lines.append("sessions with unknown cost: %d "
                     "(no embedded cost, no prices file)"
                     % report["unknown_cost_sessions"])
    lines.append("wasted on failed runs: %.1f%% of known spend"
                 % report["wasted_pct"])
    if report["top_failed"]:
        lines.append("")
        lines.append("most expensive failed sessions (top %d):" % top_n)
        for s in report["top_failed"]:
            lines.append("  $%.2f  %s" % (s.cost_usd, s.name))
    return "\n".join(lines) + "\n"


def render_json(report):
    def sess(s):
        return {"session": s.name, "outcome": s.outcome,
                "cost_usd": s.cost_usd, "tokens": s.tokens,
                "duration_ms": s.duration_ms, "model": s.model}
    return json.dumps({
        "total_cost_usd": round(report["total_cost_usd"], 2),
        "passed_cost_usd": round(report["passed_cost_usd"], 2),
        "failed_cost_usd": round(report["failed_cost_usd"], 2),
        "timeout_cost_usd": round(report["timeout_cost_usd"], 2),
        "unknown_outcome_cost_usd": round(report["unknown_outcome_cost_usd"], 2),
        "wasted_pct": round(report["wasted_pct"], 1),
        "counts": report["counts"],
        "unknown_cost_sessions": report["unknown_cost_sessions"],
        "top_failed": [sess(s) for s in report["top_failed"]],
        "sessions": [sess(s) for s in report["sessions"]],
    }, indent=2) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Report coding-agent spend split by run outcome.")
    ap.add_argument("--dir", action="append", default=[],
                    help="directory of .jsonl transcripts (repeatable)")
    ap.add_argument("--file", action="append", default=[],
                    help="single .jsonl transcript or run log (repeatable)")
    ap.add_argument("--format", choices=["claude", "generic"],
                    default="claude", help="transcript format (default claude)")
    ap.add_argument("--prices", help="JSON file of per-1M-token prices by model")
    ap.add_argument("--demo", action="store_true",
                    help="run the bundled synthetic fixtures in examples/")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--top", type=int, default=5,
                    help="how many failed sessions to list (default 5)")
    ap.add_argument("--version", action="version", version="agent-fail-spend " + VERSION)
    args = ap.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    prices = None
    if args.prices:
        try:
            prices = load_prices(args.prices)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print("error: cannot read prices file: %s" % exc, file=sys.stderr)
            return 2

    sessions = []
    malformed = 0
    if args.demo:
        demo_dir = os.path.join(here, "examples", "claude")
        files, err = collect_paths([demo_dir], "claude")
        if err:
            print("error: %s" % err, file=sys.stderr)
            return 2
        print("[demo] synthetic fixtures in examples/claude/ -- "
              "labelled test data, not real spend", file=sys.stderr)
        for f in files:
            s, bad = parse_claude_transcript(f, prices)
            sessions.append(s)
            malformed += bad
    else:
        targets = list(args.dir) + list(args.file)
        if not targets:
            targets = [os.path.expanduser("~/.claude/projects")]
        files, err = collect_paths(targets, args.format)
        if err:
            print("error: %s" % err, file=sys.stderr)
            return 2
        if not files:
            print("error: no .jsonl files found under: %s"
                  % ", ".join(targets), file=sys.stderr)
            return 2
        for f in files:
            if args.format == "generic":
                got, bad = parse_generic_log(f)
                sessions.extend(got)
                malformed += bad
            else:
                s, bad = parse_claude_transcript(f, prices)
                sessions.append(s)
                malformed += bad
    sessions.sort(key=lambda s: s.name)
    report = build_report(sessions, args.top)
    out = render_json(report) if args.json else render_text(report, args.top)
    sys.stdout.write(out)
    if malformed:
        print("warning: skipped %d malformed line(s)" % malformed,
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
