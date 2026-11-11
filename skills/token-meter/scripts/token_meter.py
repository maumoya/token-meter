#!/usr/bin/env python3
"""token-meter: what each running Claude Code session is spending, right now.

Reads the Claude Code session logs (~/.claude/projects/**/*.jsonl) READ ONLY and prints, per live session:
context now and how full the auto-compact window is, turns left before compaction, the starting baseline,
cache share, a "cold" mark when the prompt cache has expired, tokens for the main thread and for subagents,
cost when you give a price file, models, and calls per tool, MCP server and skill. With --unused it also lists
the connected MCP servers and personal skills that no session called in the last 7 days. --costs estimates
what CLAUDE.md files, rules, the MEMORY.md index and skill headers add to every call (characters / 4), and
--statusline prints one line for the Claude Code status line from the JSON it passes on stdin.

Privacy: no prompt, reply, file path or file text ever reaches the output (--costs reads those files only to count them). Only numbers, model ids, short
session ids and tool, MCP server and skill names; a name that is not a plain identifier prints as "other".
No network, no writes, no hooks, no background process. Python 3.10+ standard library only.

    python3 token_meter.py [--json] [--minutes 15] [--window N] [--prices FILE] [--unused] [--advise]
                           [--costs] [--project DIR] [--max-fill F] [--root DIR] [--statusline]
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

__version__ = "0.5.0"

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
# --advise thresholds: each rule fires only on a measured number
FILL_HIGH = 0.8               # compact when the context is this full
FEW_TURNS = 3                 # ... or this few turns are left
COLD_LARGE = 50_000           # a cold session this large is cheaper to restart than to resume
MANY_COMPACTIONS = 2          # compacted this often: the session holds several topics
BASELINE_HIGH = 0.15          # first call above this share of the window: heavy setup
SUB_HEAVY = 0.5               # subagents used this share of all tokens
CACHE_LOW = 0.5               # cache share below this after ...
CACHE_TURNS = 10              # ... this many turns
SPLIT_TURNS = 10              # a top-model-only session this long could split plan and build
TOP_MODELS = ("opus", "fable")
LOOKUP_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "LS"}
GENERIC_TOOLS = LOOKUP_TOOLS | {"Bash", "Edit", "Write", "MultiEdit", "Agent", "Task", "TodoWrite", "ToolSearch"}
LOOKUP_SHARE = 0.6            # lookups this share of tool calls ...
LOOKUP_CALLS = 20             # ... over at least this many calls
REPEAT_SHARE = 0.4            # one tool this share of tool calls ...
REPEAT_CALLS = 30             # ... and this many calls: worth a skill or script
# --costs: what each always-loaded source adds to every call (an estimate, see estimate_tokens)
CHARS_PER_TOKEN = 4           # about 4 characters per token for English text and code
SOURCE_HEAVY = 2_000          # a source this large is worth a trim
CLAUDE_MD_LINES = 300         # an instruction file longer than this is hard to follow
MEMORY_LINES = 200            # Claude Code loads only the first 200 lines of MEMORY.md
MAX_STDIN = 1_000_000         # --statusline reads at most this much JSON
BAR = 10                      # --statusline bar width
OTHER = "other"
MAX_TEXT = 80

_NAME = re.compile(r"[A-Za-z0-9_.:\-]{1,80}")
_SLUG = re.compile(r"[^A-Za-z0-9]")
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


def estimate_tokens(text: str) -> int:
    """Tokens a text adds to the context, estimated as characters / 4 (rounded up). Offline: no tokenizer."""
    return -(-len(text) // CHARS_PER_TOKEN)


def _text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    except OSError:
        return None


def _frontmatter(text: str) -> str:
    """The SKILL.md header (name and description): the only part loaded before the skill is used."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            return text[3:end]
    return ""


def _lines(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _project_folders(project: Path) -> list:
    """The project folder and its parents up to (not including) the home folder: where CLAUDE.md files load from."""
    home, folders = Path.home(), []
    for folder in [project, *project.parents]:
        if folder == home or folder == folder.parent:
            break
        folders.append(folder)
    return folders


def sources(claude_home: Path, project: Path) -> list:
    """What each always-loaded source costs per call: instructions, rules, memory index and skill headers.
    Each row is a kind, a name, estimated tokens and lines; the text itself never leaves this function."""
    rows: list = []

    def add(kind: str, name: str, text: Optional[str], loaded: Optional[str] = None) -> None:
        if text is not None:
            rows.append({"kind": kind, "name": clean_name(name), "lines": _lines(text),
                         "tokens": estimate_tokens(text if loaded is None else loaded)})

    add("instructions", "global:CLAUDE.md", _text(claude_home / "CLAUDE.md"))
    for path in sorted((claude_home / "rules").rglob("*.md")):
        add("rules", f"rules:{path.stem}", _text(path))
    folders = _project_folders(project)
    for depth, folder in enumerate(folders):
        where = "project" if depth == 0 else f"up{depth}"
        for rel in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"):
            add("instructions", f"{where}:{rel.replace('/', '-')}", _text(folder / rel))
        for path in sorted((folder / ".claude" / "rules").glob("*.md")):
            add("rules", f"{where}-rules:{path.stem}", _text(path))
    for folder in folders:
        memory = _text(claude_home / "projects" / _SLUG.sub("-", str(folder)) / "memory" / "MEMORY.md")
        if memory is not None:
            add("memory", "memory:MEMORY.md", memory, "".join(memory.splitlines(True)[:MEMORY_LINES]))
            break
    for path in sorted((claude_home / "skills").glob("*/SKILL.md")):
        text = _text(path)
        add("skill", path.parent.name, None if text is None else _frontmatter(text))
    return sorted(rows, key=lambda r: (-r["tokens"], r["name"]))


def costs(claude_home: Path, project: Path, window: int) -> dict:
    rows = sources(claude_home, project)
    total = sum(r["tokens"] for r in rows)
    by_kind: Counter = Counter()
    for r in rows:
        by_kind[r["kind"]] += r["tokens"]
    return {"method": "chars/4", "total": total, "window_share": round(total / window, 3) if window else 0.0,
            "by_kind": dict(sorted(by_kind.items())), "sources": rows}


def _row(kind: str, session: str, reason: str, do: str, **extra) -> dict:
    return {"kind": kind, "session": session, "reason": reason[:MAX_TEXT], "do": do[:MAX_TEXT], **extra}


def _session_advice(s: dict, window: int) -> list:
    sid, rows = s["id"], []
    if s["fill"] >= FILL_HIGH or (s["turns_left"] is not None and s["turns_left"] <= FEW_TURNS):
        left = "-" if s["turns_left"] is None else s["turns_left"]
        rows.append(_row("compact", sid, f"{s['fill']:.0%} full, {left} turns left",
                         "/compact at the next milestone, or /clear for a new topic"))
    if s["cold"] and s["ctx_now"] >= COLD_LARGE:
        rows.append(_row("fresh", sid, f"COLD with {short(s['ctx_now'])} context",
                         "start a fresh session instead of resuming, if the history is done"))
    if s["compactions"] >= MANY_COMPACTIONS:
        rows.append(_row("fresh", sid, f"compacted {s['compactions']}x",
                         "one session per topic: /clear when the topic changes"))
    if window and s["baseline"] / window >= BASELINE_HIGH:
        rows.append(_row("trim", sid, f"first call {short(s['baseline'])} ({s['baseline'] / window:.0%} of window)",
                         "trim CLAUDE.md and rules, turn off unused MCP servers and skills"))
    spent = s["tokens"]["main"] + s["tokens"]["subagents"]
    if spent and s["tokens"]["subagents"] / spent >= SUB_HEAVY:
        rows.append(_row("subagents", sid, f"subagents {s['tokens']['subagents'] / spent:.0%} of tokens",
                         "delegate only work that cannot fit one context"))
    if s["turns"] >= CACHE_TURNS and s["cache_share"] < CACHE_LOW:
        rows.append(_row("cache", sid, f"cache {s['cache_share']:.0%} after {s['turns']} turns",
                         "keep the prompt prefix stable; avoid edits to CLAUDE.md mid-session"))
    top = s["models"][0] if s["models"] else None
    if top and s["turns"] >= SPLIT_TURNS and top["share"] >= 0.9 and any(m in top["name"] for m in TOP_MODELS):
        rows.append(_row("model-split", sid, f"{top['name'][:30]} on {top['share']:.0%} of calls",
                         "/model opusplan: plan on the top model, build on a cheaper one"))
    calls = sum(t["count"] for t in s["tools"])
    lookups = sum(t["count"] for t in s["tools"] if t["name"] in LOOKUP_TOOLS)
    if calls >= LOOKUP_CALLS and lookups / calls >= LOOKUP_SHARE:
        rows.append(_row("local-model", sid, f"lookups {lookups / calls:.0%} of {calls} tool calls",
                         "propose a small local model for lookups; admit it first, never auto-install"))
    for t in [t for t in s["tools"] if t["name"] not in GENERIC_TOOLS][:1]:
        if calls and t["count"] >= REPEAT_CALLS and t["count"] / calls >= REPEAT_SHARE:
            rows.append(_row("new-skill", sid, f"{t['name'][:30]} {t['count']} calls ({t['count'] / calls:.0%})",
                             "propose a skill or script for the repeated step"))
    return rows


def advise(body: dict) -> list:
    """Proposals, never actions: each row is a kind, the session it came from, the measured reason and the step."""
    rows = [r for s in body["sessions"] for r in _session_advice(s, body["window"])]
    found = (body.get("costs") or {}).get("sources", [])
    skill_cost = {r["name"]: r["tokens"] for r in found if r["kind"] == "skill"}
    for r in found:
        if r["kind"] == "skill":
            continue
        why = []
        if r["tokens"] >= SOURCE_HEAVY:
            why.append(f"~{short(r['tokens'])} tokens on every call")
        if r["kind"] == "memory" and r["lines"] > MEMORY_LINES:
            why.append(f"{r['lines']} lines, only {MEMORY_LINES} load")
        elif r["kind"] != "memory" and r["lines"] > CLAUDE_MD_LINES:
            why.append(f"{r['lines']} lines")
        if why:
            rows.append(_row("trim-source", "-", f"{r['name'][:40]}: " + ", ".join(why),
                             "trim it, or move detail to a file read on demand",
                             name=r["name"], saves=r["tokens"]))
    gone = body.get("unused") or {}
    for label, key in (("MCP server", "mcp"), ("skill", "skills")):
        for name in gone.get(key, []):
            saves = skill_cost.get(name) if key == "skills" else None
            rows.append(_row("cut", "-", f"{label} never called in {gone['days']} days",
                             f"turn off {label} {name[:40]}" + (f": -{short(saves)}/call" if saves else ""),
                             name=name, label=label, saves=saves))
    return rows


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
          with_unused: bool = False, with_advice: bool = False, with_costs: bool = False,
          project: Optional[Path] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    with_unused = with_unused or with_advice
    with_costs = with_costs or with_advice
    size, source = (window, "given") if window else compact_window(claude_home)
    body = {
        "generated_at": _stamp(now), "version": __version__, "window": size, "window_source": source,
        "live_minutes": minutes,
        "sessions": live_sessions(root, now=now, window=size, prices=prices or {}, minutes=minutes),
        "unused": unused(root, now=now, claude_home=claude_home, claude_json=claude_json) if with_unused else None,
        "costs": costs(claude_home, project or Path.cwd(), size) if with_costs else None,
    }
    if with_advice:
        body["advice"] = advise(body)
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
    if body.get("costs"):
        c = body["costs"]
        kinds = ", ".join(f"{k} {short(v)}" for k, v in c["by_kind"].items())
        lines.append(f"  loaded on every call: ~{short(c['total'])} tokens ({c['window_share']:.0%} of window, "
                     f"{c['method']}) · {kinds or '-'}")
        top = [r for r in c["sources"] if r["kind"] != "skill"][:TOP]
        if top:
            lines.append("      largest: " + ", ".join(f"{r['name']} {short(r['tokens'])}" for r in top))
    if "advice" in body:
        lines.append(f"  advice ({len(body['advice'])}, proposals only, nothing is changed):")
        for row in body["advice"]:
            if row["kind"] != "cut":
                where = "" if row["session"] == "-" else f"{row['session']}: "
                lines.append(f"    [{row['kind']}] {where}{row['reason']} -> {row['do']}")
        for label in ("MCP server", "skill"):
            cut = [r for r in body["advice"] if r["kind"] == "cut" and r.get("label") == label]
            if cut:
                saves = sum(r["saves"] or 0 for r in cut)
                lines.append(f"    [cut] {len(cut)} {label}s never called: turn off "
                             + ", ".join(r["name"] for r in cut) + (f" (saves ~{short(saves)}/call)" if saves else ""))
    return "\n".join(lines)


def statusline(raw: str, *, root: Path, window: int, now: Optional[datetime] = None) -> str:
    """One line for the Claude Code status line, from the JSON it passes on stdin. Reads only that session's log,
    and only when it sits under the logs folder; anything else prints "ctx -"."""
    now = now or datetime.now(timezone.utc)
    try:
        given = json.loads(raw)
    except ValueError:
        return "ctx -"
    path = given.get("transcript_path") if isinstance(given, dict) else None
    if not isinstance(path, str) or not path.endswith(".jsonl"):
        return "ctx -"
    try:
        log = Path(path).resolve()
        log.relative_to(root.resolve())
    except (OSError, ValueError):
        return "ctx -"
    state = Session()
    state.read(log, "main", {})
    if not state.ctx_now:
        return "ctx -"
    fill = state.ctx_now / window
    filled = min(BAR, round(fill * BAR))
    left = state.turns_left(window)
    idle = (now - state.last_at).total_seconds() if state.last_at else 0
    cold = idle > (CACHE_LIFE_LONG_S if state.long_cache else CACHE_LIFE_SHORT_S)
    cache = state.cache_read / state.ctx_total if state.ctx_total else 0.0
    line = (f"ctx {short(state.ctx_now)} {'█' * filled}{'░' * (BAR - filled)} {fill:.0%} · "
            f"{'-' if left is None else left} turns left · cache {cache:.0%}{' · COLD' if cold else ''}")
    return line[:MAX_TEXT]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="token_meter.py", description="Live token use per running Claude Code session.")
    parser.add_argument("--root", type=Path, default=CLAUDE_HOME / "projects", help="Claude Code projects log folder")
    parser.add_argument("--minutes", type=int, default=LIVE_MINUTES, help="a session is live if its log changed this recently")
    parser.add_argument("--window", type=int, help="context window to measure against (default: from settings, else 200000)")
    parser.add_argument("--prices", type=Path, help="JSON price file, USD per million tokens by model id")
    parser.add_argument("--unused", action="store_true", help="also list MCP servers and skills not called in 7 days")
    parser.add_argument("--advise", action="store_true",
                        help="also propose cuts and improvements from the numbers (implies --unused); changes nothing")
    parser.add_argument("--costs", action="store_true",
                        help="also estimate what CLAUDE.md, rules, MEMORY.md and skill headers add to every call")
    parser.add_argument("--project", type=Path, help="project folder for --costs (default: the current folder)")
    parser.add_argument("--statusline", action="store_true",
                        help="print one status line from the JSON Claude Code passes on stdin, then exit")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the summary")
    parser.add_argument("--max-fill", type=float, help="exit 1 when any session's context fill is at or above this (0-1)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.minutes <= 0 or (args.window is not None and args.window <= 0):
        parser.error("--minutes and --window must be positive")
    if args.max_fill is not None and not 0 < args.max_fill <= 1:
        parser.error("--max-fill must be above 0 and at most 1")
    if args.statusline:
        window = args.window or compact_window(CLAUDE_HOME)[0]
        print(statusline(sys.stdin.read(MAX_STDIN), root=args.root, window=window))
        return 0
    try:
        prices = load_prices(args.prices)
    except (OSError, ValueError) as err:
        parser.error(f"cannot read the price file: {err}")
    body = build(root=args.root, window=args.window, prices=prices, minutes=args.minutes, with_unused=args.unused,
                 with_advice=args.advise, with_costs=args.costs, project=args.project)
    print(json.dumps(body, indent=2) if args.json else render(body))
    if args.max_fill is None:
        return 0
    over = [s for s in body["sessions"] if s["fill"] >= args.max_fill]
    for s in over:
        print(f"over budget: session {s['id']} at {s['fill']:.0%} of the window (max {args.max_fill:.0%})", file=sys.stderr)
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
