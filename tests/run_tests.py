#!/usr/bin/env python3
"""Tests for agent-fail-spend. Stdlib only; exit 0 on success.

Hand-computed fixture totals (asserted, not re-derived from the tool):
  Claude demo fixtures (examples/claude/):
    passed : $0.42, tokens 1200+800+5000+200 = 7200
    failed : $1.87, tokens 23000+13000+30000 = 66000
    timeout: $0.95, tokens 20000+12000+40000+500 = 72500
    nocost : cost unknown, tokens 3000+1500 = 4500
    totals: known spend $3.24, failed $1.87, timeout $0.95,
            wasted 1.87/3.24 = 57.7%
  Generic fixture (examples/generic-runs.jsonl):
    total $3.65, failed $2.80, timeout $0.75, passed $0.10,
    wasted 2.80/3.65 = 76.7%
  Pricing math (prices-example.json, claude-sonnet-4-5):
    1,000,000 input @ $3.00 + 100,000 output @ $15.00
    + 2,000,000 cache_read @ $0.30 = $3.00 + $1.50 + $0.60 = $5.10
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import agent_fail_spend as afs  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def approx(a, b, tol=0.005):
    assert abs(a - b) <= tol, "%r != %r" % (a, b)


def run_main(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = afs.main(args)
    return code, out.getvalue(), err.getvalue()


@test
def test_result_message_parsing():
    s, bad = afs.parse_claude_transcript(
        os.path.join(ROOT, "examples", "claude", "claude-passed.jsonl"), None)
    assert bad == 0, bad
    assert s.outcome == "passed", s.outcome
    approx(s.cost_usd, 0.42)
    assert s.tokens == 7200, s.tokens
    assert s.duration_ms == 45000
    assert s.model == "claude-sonnet-4-5"


@test
def test_outcome_classification():
    s, _ = afs.parse_claude_transcript(
        os.path.join(ROOT, "examples", "claude", "claude-failed.jsonl"), None)
    assert s.outcome == "failed", s.outcome
    approx(s.cost_usd, 1.87)
    s, _ = afs.parse_claude_transcript(
        os.path.join(ROOT, "examples", "claude", "claude-timeout.jsonl"), None)
    assert s.outcome == "timeout", s.outcome
    approx(s.cost_usd, 0.95)
    assert s.tokens == 72500, s.tokens


@test
def test_usage_aggregation_across_messages():
    s, _ = afs.parse_claude_transcript(
        os.path.join(ROOT, "examples", "claude", "claude-failed.jsonl"), None)
    assert s.tokens == 66000, s.tokens  # two assistant messages summed


@test
def test_status_synonyms():
    for raw, want in [
        ("pass", "passed"), ("passed", "passed"), ("success", "passed"),
        ("succeeded", "passed"), ("ok", "passed"),
        ("fail", "failed"), ("failed", "failed"), ("failure", "failed"),
        ("error", "failed"), ("errored", "failed"),
        ("timeout", "timeout"), ("timed_out", "timeout"),
        ("timed-out", "timeout"), ("timedout", "timeout"),
        ("weird-status", "unknown"), (None, "unknown"),
    ]:
        assert afs.classify_status(raw) == want, (raw, want)


@test
def test_generic_log_totals():
    sessions, bad = afs.parse_generic_log(
        os.path.join(ROOT, "examples", "generic-runs.jsonl"))
    assert bad == 0
    assert len(sessions) == 4
    report = afs.build_report(sessions, 5)
    approx(report["total_cost_usd"], 3.65)
    approx(report["failed_cost_usd"], 2.80)
    approx(report["timeout_cost_usd"], 0.75)
    approx(report["passed_cost_usd"], 0.10)
    approx(report["wasted_pct"], 76.7, 0.05)
    assert [s.name for s in report["top_failed"]] == ["run-002", "run-004"]


@test
def test_demo_hand_computed_totals():
    code, out, _ = run_main(["--demo"])
    assert code == 0, code
    assert "total spend:   $3.24" in out, out
    assert "failed spend:  $1.87" in out, out
    assert "timeout spend: $0.95" in out, out
    assert "wasted on failed runs: 57.7%" in out, out
    assert "sessions with unknown cost: 1" in out, out
    assert "claude-failed.jsonl" in out


@test
def test_pricing_math_with_prices_file():
    prices = afs.load_prices(
        os.path.join(ROOT, "examples", "prices-example.json"))
    usage = {"input_tokens": 1000000, "output_tokens": 100000,
             "cache_read_input_tokens": 2000000,
             "cache_creation_input_tokens": 0}
    cost = afs.price_usage(usage, "claude-sonnet-4-5", prices)
    approx(cost, 5.10)
    # Unknown model: cost stays unknown, never invented.
    assert afs.price_usage(usage, "some-unlisted-model", prices) is None


@test
def test_missing_cost_is_unknown_not_zero():
    s, _ = afs.parse_claude_transcript(
        os.path.join(ROOT, "examples", "claude", "claude-nocost.jsonl"), None)
    assert s.outcome == "unknown", s.outcome
    assert s.cost_usd is None, s.cost_usd
    assert s.tokens == 4500, s.tokens
    # With a prices file, the same usage gets priced instead.
    prices = afs.load_prices(
        os.path.join(ROOT, "examples", "prices-example.json"))
    s, _ = afs.parse_claude_transcript(
        os.path.join(ROOT, "examples", "claude", "claude-nocost.jsonl"), prices)
    approx(s.cost_usd, 3000 * 3.00 / 1e6 + 1500 * 15.00 / 1e6)  # $0.0315


@test
def test_malformed_lines_tolerated():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "messy.jsonl")
        with open(path, "w") as fh:
            fh.write('{"type": "result", "subtype": "success", "total_cost_usd": 0.50}\n')
            fh.write('this is not json\n')
            fh.write('{"type": "result", "subtype": "success", "total_cost_usd": 0.25}\n')
            fh.write('["not", "an", "object"]\n')
            fh.write('\n')
        s, bad = afs.parse_claude_transcript(path, None)
        assert bad == 2, bad
        assert s.outcome == "passed"
        approx(s.cost_usd, 0.25)  # last result message wins


@test
def test_empty_dir_and_bad_path():
    with tempfile.TemporaryDirectory() as td:
        code, out, err = run_main(["--dir", td])
        assert code == 2, (code, out, err)
        assert "no .jsonl files" in err
    code, out, err = run_main(["--dir", "/nonexistent/path/xyz"])
    assert code == 2, (code, out, err)
    assert "no such file or directory" in err


@test
def test_demo_determinism():
    code1, out1, _ = run_main(["--demo"])
    code2, out2, _ = run_main(["--demo"])
    assert code1 == 0 and code2 == 0
    h1 = hashlib.sha256(out1.encode()).hexdigest()
    h2 = hashlib.sha256(out2.encode()).hexdigest()
    assert h1 == h2, (h1, h2)


@test
def test_json_output_shape():
    code, out, _ = run_main(["--demo", "--json"])
    assert code == 0
    data = json.loads(out)
    approx(data["total_cost_usd"], 3.24)
    approx(data["failed_cost_usd"], 1.87)
    approx(data["wasted_pct"], 57.7, 0.05)
    assert data["counts"] == {"passed": 1, "failed": 1, "timeout": 1,
                              "unknown": 1}
    assert len(data["sessions"]) == 4


@test
def test_top_n_flag():
    code, out, _ = run_main(["--demo", "--top", "1"])
    assert code == 0
    assert "claude-failed.jsonl" in out
    assert out.count("most expensive failed sessions (top 1)") == 1


def main():
    passed = failed = 0
    for fn in TESTS:
        try:
            fn()
            passed += 1
            print("PASS %s" % fn.__name__)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            failed += 1
            print("FAIL %s: %s" % (fn.__name__, exc))
    print("%d/%d tests passed" % (passed, passed + failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
