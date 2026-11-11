#!/usr/bin/env python3
"""token-meter: what each running Claude Code session is spending, right now.

Reads the Claude Code session logs (~/.claude/projects/**/*.jsonl) READ ONLY and prints, per live session:
context now and how full the auto-compact window is, turns left before compaction, the starting baseline,
cache share, a "cold" mark when the prompt cache has expired, tokens for the main thread and for subagents,
cost when you give a price file, models, and calls per tool, MCP server and skill. With --unused it also lists
the connected MCP servers and personal skills that no session called in the last 7 days.

Privacy: no prompt, reply, file path or file text ever reaches the output. Only numbers, model ids, short
session ids and tool, MCP server and skill names; a name that is not a plain identifier prints as "other".
No network, no writes, no hooks, no background process. Python 3.10+ standard library only.

    python3 token_meter.py [--json] [--minutes 15] [--window N] [--prices FILE] [--unused] [--root DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

__version__ = "0.3.0"

CLAUDE_HOME = Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"
DEFAULT_WINDOW = 200_000      # context window when settings name no auto-compact window
LIVE_MINUTES = 15             # a session is live when its log changed this recently
TOP = 8                       # names shown per list
FORECAST_TURNS = 8            # turns the "turns left" forecast looks back over
MIN_FORECAST_TURNS = 3
CACHE_LIFE_LONG_S = 3600      # 1-hour prompt cache
CACHE_LIFE_SHORT_S = 300      # 5-minute prompt cache
CACHE_WRITE_MULTIPLIER = 1.25 # cache writes bill at 1.25x input unless the price file says otherwise
UNUSED_DAYS = 7
OTHER = "other"
MAX_TEXT = 80

_NAME = re.compile(r"[A-Za-z0-9_.:\-]{1,80}")
_MCP_USE = re.compile(r'"name":\s*"mcp__([^"]{1,200}?)__[^"]*"')
_SKILL_USE = re.compile(r'"skill":\s*"([^"]{1,200})"')


def clean_name(value: object) -> str:
    """A plain identifier as it is; anything else (spaces, slashes, long text) becomes "other"."""
    return value if isinstance(value, str) and _NAME.fullmatch(value) else OTHER


def _stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _num(usage: dict, key: str) -> int:
    value = usage.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def compact_window(claude_home: Path) -> tuple:
    """The auto-compact window from ~/.claude/settings.json, and where it came from. Read only."""
    try:
        settings = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_WINDOW, "default"
    if not isinstance(settings, dict):
        return DEFAULT_WINDOW, "default"
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    for value, source in ((env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"), "settings env"),
                          (settings.get("autoCompactWindow"), "settings")):
        try:
            window = int(value)
        except (TypeError, ValueError):
            continue
        if window > 0:
            return window, source
    return DEFAULT_WINDOW, "default"


def load_prices(path: Optional[Path]) -> dict:
    """{model id: {"input", "output", "cache_read"?, "cache_write"?}} in USD per million tokens. No file, no cost."""
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("price file must be a JSON object keyed by model id")
    prices = {}
    for model, spec in data.items():
        if model.startswith("_"):
            continue
        if not isinstance(spec, dict) or not all(isinstance(spec.get(k), (int, float)) for k in ("input", "output")):
            raise ValueError(f"price for {str(model)[:40]!r} needs numeric input and output")
        prices[model] = spec
    return prices


def cost_usd(model: str, usage: dict, prices: dict) -> Optional[float]:
    price = prices.get(model)
    if price is None:
        return None
    cache_read = price.get("cache_read", price["input"] * 0.1)
    cache_write = price.get("cache_write", price["input"] * CACHE_WRITE_MULTIPLIER)
    return (_num(usage, "input_tokens") * price["input"] + _num(usage, "output_tokens") * price["output"]
            + _num(usage, "cache_read_input_tokens") * cache_read
            + _num(usage, "cache_creation_input_tokens") * cache_write) / 1e6


def _rows(path: Path):
    """Parsed log lines; unreadable files and broken lines are skipped."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _is_user_turn(row: dict) -> bool:
    if row.get("isMeta") or row.get("isSidechain"):
        return False
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return isinstance(content, str)


class Session:
    """Running totals for one session: its main log plus its subagent logs."""

    def __init__(self) -> None:
        self.seen: set = set()
        self.last_at: Optional[datetime] = None
        self.long_cache = False
        self.calls = {"main": 0, "subagents": 0}
        self.tokens = {"main": 0, "subagents": 0}
        self.cost = {"main": None, "subagents": None}
        self.unpriced = 0
        self.ctx_total = 0
        self.cache_read = 0
        self.ctx_now = 0
        self.baseline = 0
        self.turns = 0
        self.turn_ctx: list = []
        self.compactions = 0
        self.subagent_runs = 0
        self.models: Counter = Counter()
        self.tools: Counter = Counter()
        self.mcp: Counter = Counter()
        self.skills: Counter = Counter()

    def read(self, path: Path, lane: str, prices: dict) -> None:
        if lane == "subagents":
            self.subagent_runs += 1
        for row in _rows(path):
            kind = row.get("type")
            if kind == "user" and lane == "main":
                self._user(row)
            elif kind == "assistant":
                self._assistant(row, lane, prices)

    def _user(self, row: dict) -> None:
        if row.get("isCompactSummary"):
            self.compactions += 1
            self.turn_ctx = []
        elif _is_user_turn(row):
            self.turns += 1
            self.turn_ctx.append(None)

    def _assistant(self, row: dict, lane: str, prices: dict) -> None:
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
        self._count_names(message.get("content"))
        key = message.get("id") or row.get("uuid")
        if usage is None or (key is not None and key in self.seen):
            return   # one API reply is logged once per content block; count it once
        self.seen.add(key)
        ctx = _num(usage, "input_tokens") + _num(usage, "cache_creation_input_tokens") + _num(usage, "cache_read_input_tokens")
        self.calls[lane] += 1
        self.tokens[lane] += ctx + _num(usage, "output_tokens")
        self.ctx_total += ctx
        self.cache_read += _num(usage, "cache_read_input_tokens")
        model = str(message.get("model") or "")
        self.models[clean_name(model)] += 1
        cost = cost_usd(model, usage, prices)
        if cost is None:
            self.unpriced += 1
        else:
            self.cost[lane] = (self.cost[lane] or 0.0) + cost
        creation = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        if _num(creation, "ephemeral_1h_input_tokens"):
            self.long_cache = True
        at = _parse_time(row.get("timestamp"))
        if at and (self.last_at is None or at > self.last_at):
            self.last_at = at
        if lane == "main" and ctx:
            self.baseline = self.baseline or ctx
            self.ctx_now = ctx
            if self.turn_ctx:
                self.turn_ctx[-1] = ctx

    def _count_names(self, content: object) -> None:
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name") if isinstance(block.get("name"), str) else ""
            if name.startswith("mcp__"):
                self.mcp[clean_name(name[5:].rsplit("__", 1)[0])] += 1
            elif name == "Skill":
                given = block.get("input") if isinstance(block.get("input"), dict) else {}
                self.skills[clean_name(given.get("skill"))] += 1
            else:
                self.tools[clean_name(name)] += 1

    def turns_left(self, window: int) -> Optional[int]:
        recent = [c for c in self.turn_ctx if c][-FORECAST_TURNS:]
        if len(recent) < MIN_FORECAST_TURNS:
            return None
        growth = mean_slope(recent)
        return None if growth <= 0 else max(0, int((window - self.ctx_now) // growth))


def mean_slope(values: list) -> float:
    """Growth per turn: the average turn-to-turn growth. Replayed on 617 real compactions
    (scripts/forecast_bench.py), it lands within 2 turns more often than the median (49% vs 40%):
    big file reads keep happening, so ignoring them made the forecast too hopeful."""
    if len(values) < 2:
        return 0.0
    return (values[-1] - values[0]) / (len(values) - 1)


def _top(counter: Counter) -> list:
    return [{"name": n, "count": c} for n, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]]


def session_files(root: Path) -> dict:
    """{(project folder, session id): [(path, lane, mtime)]} for main logs and subagent logs."""
    found: dict = {}
    for pattern, lane in (("*/*.jsonl", "main"), ("*/*/subagents/*.jsonl", "subagents")):
        for path in root.glob(pattern):
            parts = path.relative_to(root).parts
            session = path.stem if lane == "main" else parts[1]
            try:
                found.setdefault((parts[0], session), []).append((path, lane, path.stat().st_mtime))
            except OSError:
                continue
    return found


def session_body(key: tuple, files: list, *, now: datetime, window: int, prices: dict) -> Optional[dict]:
    state = Session()
    for path, lane, _ in sorted(files, key=lambda f: (f[1] != "main", str(f[0]))):
        state.read(path, lane, prices)
    total_calls = sum(state.calls.values())
    if not total_calls:
        return None
    last_at = state.last_at or datetime.fromtimestamp(max(f[2] for f in files), timezone.utc)
    idle = max(0, int((now - last_at).total_seconds()))
    return {
        "id": clean_name(key[1][:8]), "last_at": _stamp(last_at), "idle_s": idle,
        "cold": idle > (CACHE_LIFE_LONG_S if state.long_cache else CACHE_LIFE_SHORT_S),
        "ctx_now": state.ctx_now, "fill": round(state.ctx_now / window, 3), "turns": state.turns,
        "compactions": state.compactions, "subagent_runs": state.subagent_runs,
        "turns_left": state.turns_left(window), "baseline": state.baseline,
        "cache_share": round(state.cache_read / state.ctx_total, 3) if state.ctx_total else 0.0,
        "calls": state.calls, "tokens": state.tokens,
        "cost_usd": {k: None if v is None else round(v, 6) for k, v in state.cost.items()},
        "unpriced": state.unpriced,
        "models": [{"name": n, "share": round(c / total_calls, 3)} for n, c in state.models.most_common(TOP)],
        "tools": _top(state.tools), "mcp": _top(state.mcp), "skills": _top(state.skills),
    }


def live_sessions(root: Path, *, now: datetime, window: int, prices: dict, minutes: int = LIVE_MINUTES) -> list:
    since = (now - timedelta(minutes=minutes)).timestamp()
    bodies = [session_body(key, files, now=now, window=window, prices=prices)
              for key, files in session_files(root).items() if max(f[2] for f in files) >= since]
    return sorted((b for b in bodies if b), key=lambda b: (b["idle_s"], b["id"]))


def installed(claude_home: Path, claude_json: Path) -> tuple:
    """Names only: MCP server keys in ~/.claude.json (values never read out) and personal skill folders."""
    try:
        servers = json.loads(claude_json.read_text(encoding="utf-8")).get("mcpServers", {})
    except (OSError, ValueError, AttributeError):
        servers = {}
    mcp = {k for k in (servers if isinstance(servers, dict) else {}) if clean_name(k) != OTHER}
    skills = {p.parent.name for p in (claude_home / "skills").glob("*/SKILL.md") if clean_name(p.parent.name) != OTHER}
    return mcp, skills


def unused(root: Path, *, now: datetime, claude_home: Path, claude_json: Path, days: int = UNUSED_DAYS) -> dict:
    """Connected MCP servers and personal skills that no log of the last `days` called."""
    since = (now - timedelta(days=days)).timestamp()
    used_mcp: set = set()
    used_skills: set = set()
    for files in session_files(root).values():
        for path, _, mtime in files:
            if mtime < since:
                continue
            try:
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if '"tool_use"' in line:
                            used_mcp.update(_MCP_USE.findall(line))
                            used_skills.update(_SKILL_USE.findall(line))
            except OSError:
                continue
    mcp, skills = installed(claude_home, claude_json)
    return {"days": days, "mcp": sorted(mcp - used_mcp), "skills": sorted(skills - used_skills)}


def check_content_free(body: object) -> None:
    """ValueError when the output could carry free text: any string longer than MAX_TEXT, at any depth."""
    if isinstance(body, dict):
        for key, value in body.items():
            check_content_free(key)
            check_content_free(value)
    elif isinstance(body, list):
        for item in body:
            check_content_free(item)
    elif isinstance(body, str) and len(body) > MAX_TEXT:
        raise ValueError(f"output field is longer than {MAX_TEXT} characters")


def build(*, root: Path, now: Optional[datetime] = None, window: Optional[int] = None, prices: Optional[dict] = None,
          minutes: int = LIVE_MINUTES, claude_home: Path = CLAUDE_HOME, claude_json: Path = CLAUDE_JSON,
          with_unused: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    size, source = (window, "given") if window else compact_window(claude_home)
    body = {
        "generated_at": _stamp(now), "version": __version__, "window": size, "window_source": source,
        "live_minutes": minutes,
        "sessions": live_sessions(root, now=now, window=size, prices=prices or {}, minutes=minutes),
        "unused": unused(root, now=now, claude_home=claude_home, claude_json=claude_json) if with_unused else None,
    }
    check_content_free(body)
    return body


def short(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def render(body: dict) -> str:
    lines = [f"{len(body['sessions'])} live session(s) in the last {body['live_minutes']} min · "
             f"auto-compact window {short(body['window'])} ({body['window_source']})"]
    for s in body["sessions"]:
        left = "-" if s["turns_left"] is None else str(s["turns_left"])
        priced = [v for v in s["cost_usd"].values() if v is not None]
        cost = f"${sum(priced):,.2f}" if priced else "no price"
        model = s["models"][0]["name"] if s["models"] else "-"
        lines.append(f"  {s['id']}  ctx {short(s['ctx_now']):>5} {s['fill']:>4.0%} · turns left {left:>3} · "
                     f"cache {s['cache_share']:.0%}{' COLD' if s['cold'] else ''} · base {short(s['baseline'])} · "
                     f"spent {short(s['tokens']['main'] + s['tokens']['subagents'])} "
                     f"(sub {short(s['tokens']['subagents'])}, {s['subagent_runs']} run{'' if s['subagent_runs'] == 1 else 's'}) · "
                     f"compacted {s['compactions']}x · {cost} · {model}")
        uses = [f"{label} " + ", ".join(f"{u['name']} {u['count']}" for u in s[key][:4])
                for label, key in (("tools", "tools"), ("MCP", "mcp"), ("skills", "skills")) if s[key]]
        if uses:
            lines.append("      " + " · ".join(uses))
    if body.get("unused"):
        for label, key in (("MCP servers", "mcp"), ("skills", "skills")):
            names = body["unused"][key]
            lines.append(f"  never called in {body['unused']['days']} days, {label}: {', '.join(names) if names else '-'}")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="token_meter.py", description="Live token use per running Claude Code session.")
    parser.add_argument("--root", type=Path, default=CLAUDE_HOME / "projects", help="Claude Code projects log folder")
    parser.add_argument("--minutes", type=int, default=LIVE_MINUTES, help="a session is live if its log changed this recently")
    parser.add_argument("--window", type=int, help="context window to measure against (default: from settings, else 200000)")
    parser.add_argument("--prices", type=Path, help="JSON price file, USD per million tokens by model id")
    parser.add_argument("--unused", action="store_true", help="also list MCP servers and skills not called in 7 days")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the summary")
    parser.add_argument("--max-fill", type=float, help="exit 1 when any session's context fill is at or above this (0-1)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.minutes <= 0 or (args.window is not None and args.window <= 0):
        parser.error("--minutes and --window must be positive")
    if args.max_fill is not None and not 0 < args.max_fill <= 1:
        parser.error("--max-fill must be above 0 and at most 1")
    try:
        prices = load_prices(args.prices)
    except (OSError, ValueError) as err:
        parser.error(f"cannot read the price file: {err}")
    body = build(root=args.root, window=args.window, prices=prices, minutes=args.minutes, with_unused=args.unused)
    print(json.dumps(body, indent=2) if args.json else render(body))
    if args.max_fill is None:
        return 0
    over = [s for s in body["sessions"] if s["fill"] >= args.max_fill]
    for s in over:
        print(f"over budget: session {s['id']} at {s['fill']:.0%} of the window (max {args.max_fill:.0%})", file=sys.stderr)
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
