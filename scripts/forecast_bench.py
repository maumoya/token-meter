#!/usr/bin/env python3
"""Forecast benchmark: how close is "turns left" to the real number of turns before each compaction?

Replays every compacted session in a Claude Code projects folder, read only. At each turn before a
compaction it forecasts the turns left from the last 8 turns, once with the average growth (what the meter
uses since v0.3.0) and once with the median growth (v0.2.0), aimed at the context the session really
compacted at, and compares both with the turns that really followed. Prints
aggregate percentages only: no session ids, names, paths or text.

    python3 scripts/forecast_bench.py [--root ~/.claude/projects]
"""
import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "token-meter" / "scripts"))
import token_meter as tm  # noqa: E402

CLOSE = 2   # a forecast within this many turns counts as close


def segments(path: Path) -> list:
    """Context per user turn, split at each compaction. Only segments that ended in a compaction."""
    done, turns, seen = [], [], set()
    for row in tm._rows(path):
        kind = row.get("type")
        if kind == "user" and row.get("isCompactSummary"):
            done.append([c for c in turns if c])
            turns = []
        elif kind == "user" and tm._is_user_turn(row):
            turns.append(None)
        elif kind == "assistant" and turns:
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
            key = message.get("id")
            if usage is None or key in seen:
                continue
            seen.add(key)
            turns[-1] = sum(tm._num(usage, k) for k in
                            ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return [s for s in done if len(s) > tm.MIN_FORECAST_TURNS]


def forecast(history: list, peak: int, grow) -> int:
    recent = history[-tm.FORECAST_TURNS:]
    steps = [b - a for a, b in zip(recent, recent[1:])]
    growth = grow(steps) if steps else 0
    return None if growth <= 0 else max(0, int((peak - history[-1]) // growth))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=tm.CLAUDE_HOME / "projects")
    args = parser.parse_args()
    errors = {"average": [], "median": []}   # average = the meter since v0.3.0, median = v0.2.0
    runs = 0
    for path in sorted(args.root.glob("*/*.jsonl")):
        for seg in segments(path):
            runs += 1
            peak = max(seg)   # the context the session reached right before it compacted
            for t in range(tm.MIN_FORECAST_TURNS, len(seg)):
                actual = len(seg) - t
                hist = seg[:t]
                guesses = {name: forecast(hist, peak, grow)
                           for name, grow in (("average", statistics.fmean), ("median", statistics.median))}
                if None in guesses.values():
                    continue   # compare both only where both can forecast
                for name, guess in guesses.items():
                    errors[name].append(abs(guess - actual))
    if not errors["average"]:
        print("no compacted sessions found")
        return 1
    print(f"compactions replayed: {runs} · forecasts: {len(errors['average'])}")
    for name, errs in errors.items():
        close = sum(e <= CLOSE for e in errs) / len(errs)
        print(f"{name:7} growth: within {CLOSE} turns {close:.0%} · median error {statistics.median(errs):.0f} turns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
