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
from codex_queue.terminal import Suggestion, command_text, completion, draw_prompt, edit_field, field, fill_project, fill_task, prompt_frame, queue_view, screen, tokens, windows_input
from codex_queue.text_layout import text_width, wrap_rows


class SuggestionTest(unittest.TestCase):
    def setUp(self):
        self.parser = make_parser()

    def test_help_respects_terminal_columns_in_both_languages(self):
        for locale in ("ja", "en"):
            token = language.set(locale)
            try:
                for columns in (24, 34, 80):
                    with self.subTest(locale=locale, columns=columns), patch.dict(os.environ, {"COLUMNS": str(columns)}):
                        parser = make_parser()
                        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exit:
                            parser.parse_args(["config", "--help"])
                        self.assertEqual(0, exit.exception.code)
                        rows = output.getvalue().splitlines()
                        self.assertTrue(all(text_width(row) < columns for row in rows), rows)
                        self.assertTrue(any("[--priority 1-9]" in row for row in rows))
                        self.assertTrue(any("--language" in row for row in rows))
            finally:
                language.reset(token)

    def test_display_wrap_keeps_words_and_counts_wide_glyphs(self):
        self.assertEqual(["  Task", "  日本語", "  Status"], list(wrap_rows("  Task 日本語 Status", 10)))

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

    def test_completes_option_choices_after_a_space_or_equals(self):
        for line, expected in (
            ("config --language j", "ja"),
            ("project edit 3 --branch=per", "per-task"),
            ("task edit 42 --priority 3", "3"),
            ("project --base in", "inherit"),
        ):
            with self.subTest(line=line):
                start, choices = completion(self.parser, line)
                self.assertEqual([Suggestion(expected)], choices)
                self.assertTrue((line[:start] + choices[0].name).endswith(expected))
        self.assertEqual([Suggestion("auto"), Suggestion("manual")], completion(self.parser, "config --sync ")[1])

    def test_completes_quoted_paths_and_continues_into_subfolders(self):
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            directory = Path(folder) / "Project with spaces"
            nested = directory / "Nested"
            nested.mkdir(parents=True)
            file = Path(folder) / "Project instructions.txt"
            file.touch()
            line = f'/project add --path="{Path(folder) / "Project "}'
            start, choices = completion(self.parser, line)
            self.assertEqual([directory.name + os.sep], [choice.name for choice in choices])
            completed = line[:start] + choices[0].insertion
            self.assertEqual(["project", "add", "--path=" + str(directory) + os.sep], tokens(command_text(completed)))
            self.assertEqual([nested.name + os.sep], [choice.name for choice in completion(self.parser, completed)[1]])
            files = completion(self.parser, f'add example --prompt-file "{Path(folder) / "Project i"}')[1]
            self.assertEqual([file.name], [choice.name for choice in files])
            self.assertTrue(files[0].insertion.endswith(" "))

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_project_add_form_completes_path_and_branch_fields(self):
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            directory = Path(folder) / "Project folder"
            directory.mkdir()
            replies = iter([(str(Path(folder) / "Project "), str(directory) + os.sep),
                            ("per", "per-task"), ("un", "unset")])

            def complete_field(parser, message, *, show_queue, complete):
                prefix, expected = next(replies)
                start, choices = complete(prefix)
                self.assertEqual(0, start)
                self.assertEqual([expected], [choice.insertion for choice in choices])
                self.assertFalse(show_queue)
                return choices[0].insertion

            with patch("codex_queue.terminal.windows_input", side_effect=complete_field), patch(
                "codex_queue.terminal.repo_here", return_value=None,
            ), patch("codex_queue.terminal.field", return_value=""), patch(
                "codex_queue.terminal.multiline", return_value="",
            ), patch("codex_queue.terminal.confirmed", side_effect=lambda command, prompted: command):
                command = fill_project(None, ["project", "add"])
            self.assertIn(f"--path={directory}{os.sep}", command)
            self.assertIn("--branch=per-task", command)
            self.assertIn("--base=unset", command)

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_tab_on_keep_unchanged_accepts_current_path(self):
        with patch("msvcrt.kbhit", return_value=True), patch(
            "codex_queue.terminal.keys", return_value="\t",
        ) as keys, patch("codex_queue.terminal.draw_prompt", return_value=(8, 0, 0)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual("old folder", edit_field("path", "Repository path", "old folder", field))
        keys.assert_called_once_with()

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_project_and_task_edits_omit_unchanged_options(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects(path, path_key, name, prompt) VALUES ('old folder', 'old', 'Project', 'Keep me')")
            db.execute("INSERT INTO tasks(project_id, name, prompt, priority) VALUES (1, 'Task', 'Keep me too', 5)")

            def complete_field(parser, message, *, show_queue, complete):
                _, candidates = complete("")
                self.assertEqual(Suggestion("Keep unchanged", insertion="", submit=True), candidates[0])
                if message.startswith("Project name"):
                    return "New project name"
                if message.startswith("Priority"):
                    self.assertEqual("1", candidates[1].insertion)
                    return "3"
                return candidates[0].insertion

            with patch("codex_queue.terminal.windows_input", side_effect=complete_field), patch(
                "codex_queue.terminal.confirmed", side_effect=lambda command, prompted: command,
            ):
                self.assertEqual(["project", "edit", "1", "--name=New project name"], fill_project(db, ["project", "edit", "1"]))
                self.assertEqual(["edit", "1", "--priority=3"], fill_task(db, ["edit", "1"]))

    def test_help_completes_nested_topics(self):
        self.assertEqual([Suggestion("help", "Show command help")], completion(self.parser, "/he")[1])
        self.assertEqual([Suggestion("add", "Register a Git repository")], completion(self.parser, "/help project a")[1])

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_screen_shows_slash_help_above_the_input(self):
        token = language.set("en")
        try:
            with patch("codex_queue.terminal.saved_language", return_value="en"), patch(
                "codex_queue.cli.saved_language", return_value="en",
            ), patch("codex_queue.terminal.windows_input", side_effect=["/help project add", "q"]) as prompt:
                self.assertEqual(0, screen())
            self.assertIn("--path", prompt.call_args.args[1])
            self.assertFalse(prompt.call_args.kwargs["show_queue"])
        finally:
            language.reset(token)

    def test_ctrl_c_exits_the_screen_without_running_a_command(self):
        token = language.set("en")
        try:
            with patch("codex_queue.terminal.saved_language", return_value="en"), patch(
                "codex_queue.terminal.windows_input", side_effect=KeyboardInterrupt,
            ), patch("builtins.input", side_effect=KeyboardInterrupt), patch(
                "codex_queue.terminal.clear",
            ), patch("codex_queue.terminal.queue_view", return_value="Queue"), patch(
                "codex_queue.terminal.main",
            ) as main, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, screen())
            main.assert_not_called()
        finally:
            language.reset(token)

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_help_is_formatted_again_when_terminal_width_changes(self):
        with patch("msvcrt.kbhit", side_effect=[False, True]), patch(
            "codex_queue.terminal.keys", return_value="\r",
        ), patch("codex_queue.terminal.time.monotonic", side_effect=[0.0, 1.0]), patch(
            "codex_queue.terminal.time.sleep",
        ), patch("codex_queue.terminal.shutil.get_terminal_size", side_effect=[
            os.terminal_size((80, 24)), os.terminal_size((34, 24)),
        ]), patch("codex_queue.terminal.command_output", return_value="Narrow help") as output, patch(
            "codex_queue.terminal.draw_prompt", return_value=(8, 0, 0),
        ) as draw, contextlib.redirect_stdout(io.StringIO()):
            windows_input(self.parser, "Wide help", show_queue=False, help_argv=["help", "config"])
        output.assert_called_once_with(["help", "config"])
        self.assertEqual("Narrow help", draw.call_args.args[1])
        self.assertEqual((34, 24), draw.call_args.args[5])

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
                "codex_queue.status.attempt_folder", return_value=Path(folder),
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
            env = dict(os.environ, CQ_WORKER_DATA_DIR=folder, PYTHONPATH=str(source), PYTHONIOENCODING="utf-8", COLUMNS="80")

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
            all_commands = cq("help")
            self.assertIn("コマンド一覧", all_commands)
            self.assertIn("add: タスクを追加", all_commands)
            self.assertIn("task list: タスクを一覧表示", all_commands)
            self.assertIn("nextcloud resolve:", all_commands)
            self.assertNotIn("_worker", all_commands)
            self.assertIn("--language", cq("help", "settings"))
            self.assertIn("--keep-remote", cq("help", "nextcloud", "resolve"))
            cq("help", "_worker", code=2)
            cq("config", "--language", "de", code=2)
            self.assertIn("language: ja", cq("config"))
            self.assertIn("language: en", cq("config", "--language", "en"))
            self.assertIn("Automatic execution paused.", cq("ls"))
            help_en = cq("config", "--help")
            self.assertIn("usage: cq config", help_en)
            self.assertIn("show this help message and exit", help_en)
