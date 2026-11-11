import ast
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "token-meter" / "scripts" / "token_meter.py"
sys.path.insert(0, str(SCRIPT.parent))
import token_meter as tm  # noqa: E402

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
SECRET = "MY-PRIVATE-PROMPT /Users/someone/secret.txt"


def user(text=SECRET, **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def reply(mid, ctx, *, out=100, at=NOW, model="claude-test-1", tools=(), long_cache=False):
    content = [{"type": "text", "text": SECRET}]
    for name, given in tools:
        content.append({"type": "tool_use", "name": name, "input": given})
    usage = {"input_tokens": 10, "cache_read_input_tokens": ctx - 1010, "cache_creation_input_tokens": 1000,
             "output_tokens": out}
    if long_cache:
        usage["cache_creation"] = {"ephemeral_1h_input_tokens": 1000}
    return {"type": "assistant", "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "message": {"id": mid, "model": model, "content": content, "usage": usage}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.root = self.home / "projects"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, rows, age_s=0):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
        stamp = (NOW - timedelta(seconds=age_s)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def build(self, **kw):
        kw.setdefault("window", 200_000)
        return tm.build(root=self.root, now=NOW, claude_home=self.home, claude_json=self.home / ".claude.json", **kw)


class LiveSessions(Fixture):
    def growing_session(self):
        rows = []
        for i, ctx in enumerate((50_000, 60_000, 70_000, 80_000)):
            rows += [user(), reply(f"m{i}", ctx, tools=[("Bash", {"command": SECRET}),
                                                        ("mcp__github__create_issue", {"title": SECRET}),
                                                        ("Skill", {"skill": "my-skill"})])]
        return rows

    def test_numbers_for_a_live_session(self):
        self.write("-Users-x-proj/abcdef1234.jsonl", self.growing_session())
        s = self.build()["sessions"][0]
        self.assertEqual(s["id"], "abcdef12")
        self.assertEqual(s["ctx_now"], 80_000)
        self.assertEqual(s["fill"], 0.4)
        self.assertEqual(s["baseline"], 50_000)
        self.assertEqual(s["turns"], 4)
        self.assertEqual(s["turns_left"], 12)            # 120k left / 10k per turn
        self.assertEqual(s["calls"], {"main": 4, "subagents": 0})
        self.assertEqual(s["tools"], [{"name": "Bash", "count": 4}])
        self.assertEqual(s["mcp"], [{"name": "github", "count": 4}])
        self.assertEqual(s["skills"], [{"name": "my-skill", "count": 4}])

    def test_no_prompt_path_or_text_in_any_output(self):
        self.write("-Users-x-proj/abcdef1234.jsonl", self.growing_session())
        body = self.build(with_unused=True)
        text = json.dumps(body) + tm.render(body)
        for leak in ("MY-PRIVATE-PROMPT", "secret.txt", "/Users", "-Users-x-proj", "create_issue"):
            self.assertNotIn(leak, text)

    def test_odd_names_become_other(self):
        self.write("p/s1.jsonl", [user(), reply("m", 5000, tools=[("Skill", {"skill": "a b/../c"}),
                                                                   ("We ird", {})])])
        s = self.build()["sessions"][0]
        self.assertEqual(s["skills"], [{"name": "other", "count": 1}])
        self.assertEqual(s["tools"], [{"name": "other", "count": 1}])

    def test_repeated_message_id_counts_once(self):
        self.write("p/s1.jsonl", [user(), reply("same", 5000), reply("same", 5000)])
        self.assertEqual(self.build()["sessions"][0]["calls"]["main"], 1)

    def test_subagents_counted_apart(self):
        self.write("p/s1.jsonl", [user(), reply("a", 5000)])
        self.write("p/s1/subagents/agent-1.jsonl", [reply("b", 3000, out=0)])
        s = self.build()["sessions"][0]
        self.assertEqual(s["tokens"], {"main": 5100, "subagents": 3000})
        self.assertEqual(s["ctx_now"], 5000)              # subagent context never counts as the main one
        self.assertEqual(s["subagent_runs"], 1)
        self.assertIn("1 run)", tm.render(self.build()))

    def test_big_reads_count_in_the_forecast(self):
        rows = []
        for i, ctx in enumerate((50_000, 60_000, 130_000, 140_000, 150_000)):   # one 70k jump (a big file read)
            rows += [user(), reply(f"m{i}", ctx)]
        self.write("p/s1.jsonl", rows)
        s = self.build()["sessions"][0]
        self.assertEqual(s["turns_left"], 2)             # 50k left / 25k a turn on average

    def test_mean_slope(self):
        self.assertEqual(tm.mean_slope([10, 20, 30]), 10)
        self.assertEqual(tm.mean_slope([10, 20, 60]), 25)
        self.assertEqual(tm.mean_slope([5]), 0.0)

    def test_old_sessions_are_not_live(self):
        self.write("p/old.jsonl", [user(), reply("a", 5000)], age_s=3600)
        self.assertEqual(self.build()["sessions"], [])
        self.assertEqual(len(self.build(minutes=120)["sessions"]), 1)

    def test_cold_after_cache_life(self):
        self.write("p/short.jsonl", [user(), reply("a", 5000, at=NOW - timedelta(seconds=400))])
        self.write("p/long.jsonl", [user(), reply("b", 5000, at=NOW - timedelta(seconds=400), long_cache=True)])
        cold = {s["id"]: s["cold"] for s in self.build()["sessions"]}
        self.assertEqual(cold, {"short": True, "long": False})

    def test_compaction_resets_the_forecast(self):
        rows = self.growing_session() + [user(isCompactSummary=True), user(), reply("z", 30_000)]
        self.write("p/s1.jsonl", rows)
        s = self.build()["sessions"][0]
        self.assertEqual(s["ctx_now"], 30_000)
        self.assertIsNone(s["turns_left"])
        self.assertEqual(s["compactions"], 1)

    def test_cost_only_with_prices(self):
        self.write("p/s1.jsonl", [user(), reply("a", 2010, out=1000)])
        self.assertEqual(self.build()["sessions"][0]["cost_usd"]["main"], None)
        prices = {"claude-test-1": {"input": 1.0, "output": 10.0, "cache_read": 0.1, "cache_write": 2.0}}
        cost = self.build(prices=prices)["sessions"][0]["cost_usd"]["main"]
        self.assertAlmostEqual(cost, (10 * 1 + 1000 * 10 + 1000 * 0.1 + 1000 * 2) / 1e6)

    def test_window_from_settings(self):
        (self.home / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000"}}))
        self.assertEqual(tm.compact_window(self.home), (150_000, "settings env"))
        body = tm.build(root=self.root, now=NOW, claude_home=self.home, claude_json=self.home / "x.json")
        self.assertEqual(body["window"], 150_000)


class Unused(Fixture):
    def test_lists_connected_but_never_called(self):
        (self.home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"github": {"env": {"TOKEN": "s3cr3t"}}, "notion": {}}}))
        for name in ("my-skill", "idle-skill"):
            (self.home / "skills" / name).mkdir(parents=True)
            (self.home / "skills" / name / "SKILL.md").write_text("---\n---\n")
        self.write("p/s1.jsonl", [user(), reply("a", 5000, tools=[("mcp__github__x", {}), ("Skill", {"skill": "my-skill"})])])
        body = self.build(with_unused=True)
        self.assertEqual(body["unused"], {"days": 7, "mcp": ["notion"], "skills": ["idle-skill"]})
        self.assertNotIn("s3cr3t", json.dumps(body))


class Safety(unittest.TestCase):
    def test_imports_nothing_that_talks_to_the_network_or_runs_programs(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(names & {"socket", "urllib", "http", "requests", "subprocess", "ftplib", "smtplib", "ssl"})

    def test_never_opens_a_file_for_writing(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for needle in ("write_text", "write_bytes", "\"w\"", "'w'", "\"a\"", "unlink", "rmtree", "os.remove"):
            self.assertNotIn(needle, source)

    def test_long_text_is_refused(self):
        with self.assertRaises(ValueError):
            tm.check_content_free({"x": ["y" * 200]})

    def test_cli_prints_json_on_an_empty_folder(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(tm.main(["--root", tmp, "--json", "--window", "1000"]), 0)
        self.assertEqual(json.loads(out.getvalue())["sessions"], [])

    def test_max_fill_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p" / "s1.jsonl"
            path.parent.mkdir()
            path.write_text(json.dumps(user()) + "\n" + json.dumps(reply("a", 1500)) + "\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(tm.main(["--root", tmp, "--window", "2000", "--max-fill", "0.8"]), 0)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(tm.main(["--root", tmp, "--window", "2000", "--max-fill", "0.75"]), 1)
            self.assertIn("s1", err.getvalue())
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                tm.main(["--max-fill", "1.5"])

    def test_bad_price_file_is_an_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write('{"m": {"input": "cheap"}}')
        try:
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                tm.main(["--prices", handle.name])
        finally:
            os.unlink(handle.name)


if __name__ == "__main__":
    unittest.main()
