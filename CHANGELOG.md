# Changelog

## 0.3.0 - 2026-11-11

- Turns left uses the average turn-to-turn growth over the last 8 turns. Measured on 617 real compactions
  (`scripts/forecast_bench.py`), it lands within 2 turns 49% of the time against 40% for the median of 0.2.0.
- `--max-fill F` budget gate: exit 1 when any session's context fill is at or above F.
- GitHub Action (`uses: maumoya/token-meter@v0.3.0`) that runs the gate on an agent run's logs, with a CI
  self-test of a passing and a failing run.
- `scripts/forecast_bench.py`: replay your own compactions and score the forecast; aggregate output only.
- Security workflows: CodeQL, OpenSSF Scorecard, Dependabot for actions, SHA-pinned actions.
- README: header, measured result card, animated demo, CI and Results sections.

## 0.2.0 - 2026-11-11

- Turns left uses the median turn-to-turn growth over the last 8 turns, so one big file read no longer
  swings the forecast.
- Each session shows how many times it was compacted and how many subagent runs it started.
- Still read only, offline and process-free: no hooks, no settings edits, no stored history, no telemetry.

## 0.1.0 - 2026-11-11

- First release: live token meter per running Claude Code session (context, fill, turns left, baseline,
  cache share, cold mark, main and subagent tokens, optional cost, models, tools, MCP servers, skills) and
  the never-called list of MCP servers and skills over 7 days.
