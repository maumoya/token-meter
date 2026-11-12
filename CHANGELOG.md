# Changelog

## Unreleased

- History: the 0.5.0 CI actions bump is now authored by the maintainer (same files); tag v0.5.0 re-signed and
  its release rebuilt on the new commit.

## 0.5.0 - 2026-11-11

- README shows the OpenSSF Best Practices badge (passing).
- CI actions bumped to their latest pinned commits.
- `--costs`: what every call pays before you type, per source: global and project CLAUDE.md files, rules,
  the MEMORY.md index (only the 200 lines that load) and skill headers, estimated as characters / 4, with
  the total share of the window. The text is read only to count it and never printed.
- `--advise` now says what each cut saves: `trim-source` rows for a heavy (2k tokens or more) or long
  (over 300 lines; MEMORY.md over 200) file, and the tokens per call each unused skill costs.
- `--statusline`: one line for the Claude Code status line (`ctx 166k ████████░░ 83% · 4 turns left ·
  cache 93%`, plus `COLD`), read from the JSON Claude Code passes on stdin. It reads only that session's
  log, and only under the logs folder.
- README shows the OpenSSF Best Practices badge (passing).
- CI actions bumped to their latest pinned commits.

## 0.4.0 - 2026-11-11

- `--advise`: proposals for what to cut and improve, drawn only from the numbers: compact, start fresh, trim
  the baseline, fewer subagents, a stable cache prefix, split plan and build models (`/model opusplan`), a
  local model for lookups (admitted first, never installed), a skill for a repeated step, and the MCP servers
  and skills to turn off. Proposals only: it changes nothing. Same content-free output rules as the meter.

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
