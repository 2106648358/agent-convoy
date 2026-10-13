from __future__ import annotations

import argparse
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import agent_convoy as ac
import agent_convoy_web as web


class AgentConvoyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = ac.Paths(
            self.root / "codex",
            self.root / "claude",
            self.root / "workbuddy",
            self.root / "classic",
            self.root / "state",
            None,
            None,
        )
        for path in (self.paths.codex_home, self.paths.claude_home, self.paths.workbuddy_home, self.paths.workbuddy_classic_root):
            path.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def sample(self, source: str = "claude") -> ac.Conversation:
        return ac.Conversation(
            "source-1",
            source,
            "Example migration",
            str(self.root / "repo"),
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:01:00Z",
            [
                ac.Message("user", "Please inspect the parser", "2026-01-01T00:00:00Z"),
                ac.Message("assistant", "The parser is ready.", "2026-01-01T00:01:00Z"),
            ],
            "/source/transcript.jsonl",
        )

    def seed_claude(self) -> str:
        convo = self.sample("claude")
        result = ac.JsonlTarget("claude", self.paths.claude_home).import_native(convo)
        return result.target_id

    def seed_workbuddy_modern(self) -> str:
        convo = self.sample("workbuddy")
        result = ac.JsonlTarget("workbuddy", self.paths.workbuddy_home).import_native(convo)
        return result.target_id

    def seed_codex(self) -> str:
        session_id = "11111111-1111-4111-8111-111111111111"
        path = self.paths.codex_home / "sessions/2026/01/01" / f"rollout-2026-01-01T00-00-00-{session_id}.jsonl"
        path.parent.mkdir(parents=True)
        records = [
            {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": session_id, "cwd": str(self.root / "repo")}},
            {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}},
            {"timestamp": "2026-01-01T00:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi"}]}},
        ]
        path.write_text("\n".join(json.dumps(x) for x in records) + "\n")
        return session_id

    def seed_classic_workbuddy(self) -> str:
        history = self.paths.workbuddy_classic_root / "acct/CodeBuddyIDE/acct/history/workspace/conversation"
        messages = history / "messages"
        messages.mkdir(parents=True)
        (history.parent / "index.json").write_text(json.dumps({"current": "conversation", "conversations": [{"id": "conversation", "name": "Classic chat", "createdAt": "2026-01-01T00:00:00Z", "lastMessageAt": "2026-01-01T00:01:00Z", "type": "craft"}]}))
        (history / "index.json").write_text(json.dumps({"messages": [{"id": "m1", "role": "user", "type": "text", "isComplete": True}, {"id": "m2", "role": "assistant", "type": "text", "isComplete": True}], "requests": []}))
        (messages / "m1.json").write_text(json.dumps({"id": "m1", "role": "user", "message": "Question", "extra": "{}", "references": []}))
        (messages / "m2.json").write_text(json.dumps({"id": "m2", "role": "assistant", "message": "Answer", "extra": "{}"}))
        adapter = ac.WorkBuddyAdapter(self.paths.workbuddy_home, self.paths.workbuddy_classic_root)
        return next(x.id for x in adapter.list_sessions() if x.adapter == "workbuddy-classic")

    def test_claude_round_trip(self) -> None:
        session_id = self.seed_claude()
        loaded = ac.ClaudeAdapter(self.paths.claude_home).load(session_id)
        self.assertEqual([m.role for m in loaded.messages], ["user", "assistant"])
        self.assertEqual(loaded.title, "Example migration")

    def test_workbuddy_modern_round_trip(self) -> None:
        session_id = self.seed_workbuddy_modern()
        loaded = ac.WorkBuddyAdapter(self.paths.workbuddy_home, self.paths.workbuddy_classic_root).load(session_id)
        self.assertEqual(loaded.source, "workbuddy")
        self.assertEqual(len(loaded.messages), 2)

    def test_workbuddy_classic_reader(self) -> None:
        session_id = self.seed_classic_workbuddy()
        loaded = ac.WorkBuddyAdapter(self.paths.workbuddy_home, self.paths.workbuddy_classic_root).load(session_id)
        self.assertEqual(loaded.metadata["storage"], "workbuddy-classic")
        self.assertEqual([m.content for m in loaded.messages], ["Question", "Answer"])

    def test_codex_reader(self) -> None:
        session_id = self.seed_codex()
        loaded = ac.CodexAdapter(self.paths.codex_home, None, self.paths.state_home).load(session_id)
        self.assertEqual([m.content for m in loaded.messages], ["Hello", "Hi"])

    def test_all_six_directions_dry_run(self) -> None:
        ids = {"claude": self.seed_claude(), "workbuddy": self.seed_workbuddy_modern(), "codex": self.seed_codex()}
        for source in ac.SUPPORTED:
            for target in ac.SUPPORTED:
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    result = ac.perform_import(self.paths, source, target, ids[source], apply=False, title=None, cwd=None)
                    self.assertEqual(result["mode"], "dry-run")
                    self.assertEqual(result["target"], target)

    def test_apply_duplicate_and_rollback_for_file_target(self) -> None:
        session_id = self.seed_codex()
        result = ac.perform_import(self.paths, "codex", "claude", session_id, apply=True, title=None, cwd=None)
        target_path = Path(result["created_paths"][0])
        self.assertTrue(target_path.is_file())
        with self.assertRaises(ac.ConvoyError):
            ac.perform_import(self.paths, "codex", "claude", session_id, apply=True, title=None, cwd=None)
        plan = ac.rollback(self.paths, result["import_id"], apply=False)
        self.assertEqual(plan["mode"], "dry-run")
        ac.rollback(self.paths, result["import_id"], apply=True)
        self.assertFalse(target_path.exists())

    def test_path_escape_is_rejected(self) -> None:
        with self.assertRaises(ac.ConvoyError):
            ac.ensure_beneath(self.root / "elsewhere", self.root / "safe")

    def test_markdown_export_all_three_sources_and_deduplicate(self) -> None:
        ids = {"claude": self.seed_claude(), "workbuddy": self.seed_workbuddy_modern(), "codex": self.seed_codex()}
        output = self.root / "exports"
        output.mkdir()
        for source, session_id in ids.items():
            with self.subTest(source=source):
                result = ac.export_conversation(self.paths, source, session_id, output)
                path = Path(result["path"])
                self.assertTrue(path.is_file())
                rendered = path.read_text()
                self.assertIn("schema: \"agent-convoy/markdown-v1\"", rendered)
                self.assertIn("## 1. 用户", rendered)
                self.assertIn("## 2. 助手", rendered)
                duplicate = ac.export_conversation(self.paths, source, session_id, output)
                self.assertTrue(duplicate["reused"])
                self.assertEqual(duplicate["path"], result["path"])
        ledger = ac.Ledger(self.paths.state_home).read()
        self.assertEqual(ledger["version"], 2)
        self.assertEqual(len(ledger["exports"]), 3)

    def test_markdown_omits_thinking_and_uses_collision_suffix(self) -> None:
        convo = self.sample()
        convo.messages.insert(1, ac.Message("assistant", "[Thinking]\nprivate chain of thought"))
        convo.messages.insert(2, ac.Message("assistant", "[Tool call: shell]\n{\"api_key\":\"secret\"}"))
        convo.messages.append(ac.Message("tool", "authorization: abc123\nDone"))
        rendered = ac.render_markdown(convo)
        self.assertNotIn("private chain of thought", rendered)
        self.assertNotIn("Tool call", rendered)
        self.assertNotIn("abc123", rendered)
        self.assertIn("authorization: <redacted>", rendered)
        self.assertIn("The parser is ready.", rendered)
        session_id = self.seed_claude()
        output = self.root / "exports"
        output.mkdir()
        loaded = ac.ClaudeAdapter(self.paths.claude_home).load(session_id)
        collision = output / ac.export_filename(loaded)
        collision.write_text("unrelated")
        result = ac.export_conversation(self.paths, "claude", session_id, output)
        self.assertTrue(Path(result["path"]).name.endswith("-2.md"))
        self.assertEqual(collision.read_text(), "unrelated")

    def test_ledger_v1_is_compatible_and_upgrades_on_write(self) -> None:
        self.paths.state_home.mkdir()
        ledger_path = self.paths.state_home / "ledger.json"
        ledger_path.write_text(json.dumps({"version": 1, "imports": []}))
        ledger = ac.Ledger(self.paths.state_home)
        value = ledger.read()
        self.assertEqual(value["version"], 2)
        self.assertEqual(value["exports"], [])
        ledger.add_export({"export_id": "e1", "status": "created", "path": str(self.root / "x.md")})
        saved = json.loads(ledger_path.read_text())
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["exports"][0]["export_id"], "e1")

    def test_reveal_requires_registered_owned_export(self) -> None:
        session_id = self.seed_claude()
        output = self.root / "exports"
        output.mkdir()
        exported = ac.export_conversation(self.paths, "claude", session_id, output)
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("agent_convoy.subprocess.run", return_value=completed) as run:
            result = ac.reveal_export(self.paths, exported["export_id"])
        self.assertTrue(result["revealed"])
        run.assert_called_once_with(["/usr/bin/open", "-R", exported["path"]], text=True, capture_output=True, timeout=15, check=False)
        with self.assertRaises(ac.ConvoyError):
            ac.reveal_export(self.paths, "not-registered")

    def _http(self, origin: str, token: str, path: str, *, method: str = "GET", body=None, use_token: bool = True, use_origin: bool = True):
        headers = {}
        if use_token:
            headers["X-Agent-Convoy-Token"] = token
        if use_origin and method != "GET":
            headers["Origin"] = origin
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(origin + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_http_security_export_and_two_phase_migration(self) -> None:
        session_id = self.seed_codex()
        output = self.root / "exports"
        output.mkdir()
        server, state = web.create_server(self.paths, port=0, token="test-capability")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                self._http(state.origin, state.token, "/api/doctor", use_token=False)
            self.assertEqual(unauthorized.exception.code, 401)
            with self.assertRaises(urllib.error.HTTPError) as forbidden:
                self._http(
                    state.origin,
                    state.token,
                    "/api/export",
                    method="POST",
                    body={"source": "codex", "session_id": session_id, "output_dir": str(output)},
                    use_origin=False,
                )
            self.assertEqual(forbidden.exception.code, 403)
            status, exported = self._http(
                state.origin,
                state.token,
                "/api/export",
                method="POST",
                body={"source": "codex", "session_id": session_id, "output_dir": str(output)},
            )
            self.assertEqual(status, 200)
            self.assertTrue(Path(exported["path"]).is_file())
            with mock.patch("agent_convoy_web.choose_directory", return_value=str(output)):
                _, chosen = self._http(state.origin, state.token, "/api/choose-directory", method="POST", body={})
            self.assertEqual(chosen["path"], str(output))
            with mock.patch("agent_convoy_web.ac.reveal_export", return_value={"revealed": True}) as reveal:
                _, revealed = self._http(
                    state.origin,
                    state.token,
                    "/api/reveal",
                    method="POST",
                    body={"export_id": exported["export_id"]},
                )
            self.assertTrue(revealed["revealed"])
            reveal.assert_called_once_with(self.paths, exported["export_id"])
            _, plan = self._http(
                state.origin,
                state.token,
                "/api/migrate/plan",
                method="POST",
                body={"source": "codex", "target": "claude", "session_id": session_id},
            )
            with self.assertRaises(urllib.error.HTTPError):
                self._http(
                    state.origin,
                    state.token,
                    "/api/migrate/apply",
                    method="POST",
                    body={"plan_id": plan["plan_id"], "confirm": False},
                )
            _, applied = self._http(
                state.origin,
                state.token,
                "/api/migrate/apply",
                method="POST",
                body={"plan_id": plan["plan_id"], "confirm": True},
            )
            self.assertEqual(applied["status"], "applied")
            _, rollback_plan = self._http(
                state.origin,
                state.token,
                "/api/rollback/plan",
                method="POST",
                body={"import_id": applied["import_id"]},
            )
            _, rolled_back = self._http(
                state.origin,
                state.token,
                "/api/rollback/apply",
                method="POST",
                body={"plan_id": rollback_plan["plan_id"], "confirm": True},
            )
            self.assertEqual(rolled_back["mode"], "apply")
            request = urllib.request.Request(
                state.origin + "/api/doctor",
                data=b"{}",
                headers={"X-Agent-Convoy-Token": state.token, "Origin": state.origin, "Content-Type": "application/json"},
                method="PUT",
            )
            with self.assertRaises(urllib.error.HTTPError) as method_error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(method_error.exception.code, 405)
            oversized = urllib.request.Request(
                state.origin + "/api/export",
                data=b"{" + b"x" * (web.MAX_BODY + 1) + b"}",
                headers={"X-Agent-Convoy-Token": state.token, "Origin": state.origin, "Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as size_error:
                urllib.request.urlopen(oversized, timeout=5)
            self.assertEqual(size_error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_ui_uses_safe_text_rendering(self) -> None:
        self.assertIn("textContent", web.APP_JS)
        self.assertNotIn("innerHTML", web.APP_JS)


if __name__ == "__main__":
    unittest.main()

