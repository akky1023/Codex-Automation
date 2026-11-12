import contextlib
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from codex_queue.cli import make_parser
from codex_queue.language import language
from codex_queue.store import SCHEMA
from codex_queue.terminal import Suggestion, command_text, completion, draw_prompt, prompt_frame, queue_view, windows_input


class SuggestionTest(unittest.TestCase):
    def setUp(self):
        self.parser = make_parser()

    def test_slash_command_replaces_only_prefix(self):
        line = "  /proj"
        start, choices = completion(self.parser, line)
        self.assertEqual([Suggestion("project", "Manage projects")], choices)
        self.assertEqual("project", command_text(line[:start] + choices[0].name))

    def test_subcommands_and_options_follow_parser(self):
        self.assertEqual([Suggestion("edit", "Edit a task")], completion(self.parser, "task e")[1])
        self.assertEqual([Suggestion("--priority", "Task priority")], completion(self.parser, "task edit 42 --pri")[1])
        self.assertEqual([Suggestion("resolve", "Resolve a completion conflict")], completion(self.parser, "/nextcloud re")[1])
        self.assertNotIn("_worker", [candidate.name for candidate in completion(self.parser, "/")[1]])

    def test_blank_input_has_no_candidates_but_slash_opens_them(self):
        for line in ("", "   ", "\t"):
            self.assertEqual([], completion(self.parser, line)[1])
        self.assertIn(Suggestion("project", "Manage projects"), completion(self.parser, "/")[1])

    def test_localized_command_and_alias_descriptions_are_shown(self):
        token = language.set("ja")
        try:
            parser = make_parser()
            _, candidates = completion(parser, "/a")
            frame, _, _, _ = prompt_frame("Queue", "", "/a", candidates, 0, (80, 12), 0)
            self.assertIn("> add: タスクを追加", frame)
            self.assertIn(Suggestion("remove", "タスクを削除"), completion(parser, "/rem")[1])
        finally:
            language.reset(token)

    def test_no_completion_in_argument_values(self):
        for line in ('add "title --pr', 'add x --prompt --pr', 'add -- --pr'):
            with self.subTest(line=line):
                self.assertEqual([], completion(self.parser, line)[1])

    def test_input_stays_on_last_row_with_or_without_suggestions(self):
        for candidates in ([], [Suggestion("project", "Manage projects"), Suggestion("pause", "Pause the queue")]):
            frame, _, _, _ = prompt_frame("Queue", "Done", "/p", candidates, 0, (80, 12), 0)
            rows = frame.split("\n")
            self.assertEqual(12, len(rows))
            self.assertEqual("cq> /p", rows[-1])
            self.assertEqual("-" * 79, rows[-2])
            self.assertEqual("Queue", rows[0])
            if candidates:
                self.assertIn("> project: Manage projects", rows[:-1])

    def test_short_console_scrolls_output_and_shows_end_of_wide_input(self):
        frame, page, offset, maximum = prompt_frame(
            "Queue", "\n".join(f"task {n}" for n in range(12)),
            "\u3042" * 15 + "end", [], 0, (14, 5), 100,
        )
        self.assertEqual(2, page)
        self.assertEqual(maximum, offset)
        self.assertIn("task 11", frame)
        self.assertTrue(frame.endswith("end"))
        self.assertEqual(5, len(frame.split("\n")))

    def test_virtual_terminal_highlights_only_input_row(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            draw_prompt("Queue", "", "", [], 0, (80, 8), 0, vt=True)
        self.assertTrue(output.getvalue().endswith("\033[97;44mcq> \033[K\033[0m"))

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_arrow_selects_tab_completes_and_enter_submits(self):
        sequence = ["/", "r", "down", "\t", "\r"]
        with patch("msvcrt.kbhit", return_value=True), patch(
            "codex_queue.terminal.keys", side_effect=sequence,
        ), patch("codex_queue.terminal.queue_view", return_value="Queue"), patch(
            "codex_queue.terminal.draw_prompt", return_value=(8, 0, 0),
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual("/remove ", windows_input(self.parser, ""))


class QueueViewTest(unittest.TestCase):
    def test_dashboard_lists_tasks_and_refreshes_progress_from_events(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db, TemporaryDirectory(dir=Path.cwd()) as folder:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects(path, path_key, name) VALUES ('test', 'test', 'Notes')")
            db.executemany(
                "INSERT INTO tasks(project_id, name, priority, status) VALUES (1, ?, ?, ?)",
                [("Index notes", 5, "RUNNING"), ("Fix search", 3, "QUEUED"),
                 ("Update docs", 4, "QUEUED"), ("First release", 1, "DONE"),
                 ("Discarded task", 1, "REMOVED")],
            )
            db.execute(
                "INSERT INTO attempts(task_id, number, path, mode, prompt, phase) "
                "VALUES (1, 1, 'test', 'per-task', '', 'EXECUTING')",
            )
            run = Path(folder) / "1"
            run.mkdir()
            with patch("codex_queue.terminal.open_queue", return_value=contextlib.nullcontext(db)), patch(
                "codex_queue.terminal.attempt_folder", return_value=Path(folder),
            ):
                self.assertIn("Waiting for Codex output", queue_view())
                (run / "events.jsonl").write_text(
                    '{"item":{"command":"git status","status":"in_progress"}}\n', encoding="utf-8",
                )
                view = queue_view()
                token = language.set("ja")
                try:
                    japanese_view = queue_view()
                finally:
                    language.reset(token)
            self.assertLess(view.index("Index notes"), view.index("Tasks\n"))
            self.assertIn("Running: git status", view)
            self.assertLess(view.index("Fix search"), view.index("Update docs"))
            self.assertLess(view.index("Update docs"), view.index("First release"))
            self.assertNotIn("Discarded task", view)
            self.assertIn("タスク一覧", japanese_view)
            self.assertIn("待機中  2  Notes  Fix search", japanese_view)
            self.assertIn("実行中: git status", japanese_view)

    def test_language_switch_is_persisted_for_new_cli_processes(self):
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            source = Path(__file__).resolve().parents[1] / "src"
            env = dict(os.environ, CQ_WORKER_DATA_DIR=folder, PYTHONPATH=str(source), PYTHONIOENCODING="utf-8")

            def cq(*args, code=0):
                result = subprocess.run(
                    [sys.executable, "-m", "codex_queue.cli", *args],
                    env=env, capture_output=True, encoding="utf-8", timeout=10,
                )
                self.assertEqual(code, result.returncode, result.stderr)
                return result.stdout

            self.assertIn("language: en", cq("config"))
            self.assertIn("language: ja", cq("config", "--language", "ja"))
            self.assertIn("自動実行は一時停止中です。", cq("ls"))
            help_ja = cq("config", "--help")
            for label in ("使い方: cq config", "オプション:", "このヘルプを表示して終了", "表示言語"):
                self.assertIn(label, help_ja)
            self.assertIn("位置引数:", cq("--help"))
            self.assertNotIn("show this help message", help_ja)
            cq("config", "--language", "de", code=2)
            self.assertIn("language: ja", cq("config"))
            self.assertIn("language: en", cq("config", "--language", "en"))
            self.assertIn("Automatic execution paused.", cq("ls"))
            help_en = cq("config", "--help")
            self.assertIn("usage: cq config", help_en)
            self.assertIn("show this help message and exit", help_en)
