import contextlib
import io
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from codex_queue.cli import make_parser
from codex_queue.store import SCHEMA
from codex_queue.terminal import command_text, completion, draw_prompt, prompt_frame, queue_view, windows_input


class SuggestionTest(unittest.TestCase):
    def setUp(self):
        self.parser = make_parser()

    def test_slash_command_replaces_only_prefix(self):
        line = "  /proj"
        start, choices = completion(self.parser, line)
        self.assertEqual(["project"], choices)
        self.assertEqual("project", command_text(line[:start] + choices[0]))

    def test_subcommands_and_options_follow_parser(self):
        self.assertEqual(["edit"], completion(self.parser, "task e")[1])
        self.assertEqual(["--priority"], completion(self.parser, "task edit 42 --pri")[1])
        self.assertEqual(["resolve"], completion(self.parser, "/nextcloud re")[1])
        self.assertNotIn("_worker", completion(self.parser, "/")[1])

    def test_blank_input_has_no_candidates_but_slash_opens_them(self):
        for line in ("", "   ", "\t"):
            self.assertEqual([], completion(self.parser, line)[1])
        self.assertIn("project", completion(self.parser, "/")[1])

    def test_no_completion_in_argument_values(self):
        for line in ('add "title --pr', 'add x --prompt --pr', 'add -- --pr'):
            with self.subTest(line=line):
                self.assertEqual([], completion(self.parser, line)[1])

    def test_input_stays_on_last_row_with_or_without_suggestions(self):
        for candidates in ([], ["project", "pause"]):
            frame, _, _, _ = prompt_frame("Queue", "Done", "/p", candidates, 0, (80, 12), 0)
            rows = frame.split("\n")
            self.assertEqual(12, len(rows))
            self.assertEqual("cq> /p", rows[-1])
            self.assertEqual("-" * 79, rows[-2])
            self.assertEqual("Queue", rows[0])
            if candidates:
                self.assertIn("> project", rows[:-1])

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
            self.assertLess(view.index("Index notes"), view.index("Tasks\n"))
            self.assertIn("Running: git status", view)
            self.assertLess(view.index("Fix search"), view.index("Update docs"))
            self.assertLess(view.index("Update docs"), view.index("First release"))
            self.assertNotIn("Discarded task", view)
