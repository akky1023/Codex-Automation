import contextlib
import ctypes
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from codex_queue.editor import edit_text
from codex_queue.cli import QueueError, make_parser, manage_project, manage_idea
from codex_queue.language import language
from codex_queue.store import SCHEMA
from codex_queue.terminal import APPLY_SELECTION, Suggestion, choose_options, command_text, completion, draw_prompt, edit_field, fill_project, fill_idea, prepare, prompt_frame, queue_view, screen, tokens, windows_input
from codex_queue.text_layout import text_width, wrap_rows
from codex_queue.windows_console import ConsoleInput, InputRecord, KeyEvent


class SuggestionTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(language.reset, language.set("en"))
        self.parser = make_parser()

    def test_idea_plan_accepts_grilling_mode(self):
        args = self.parser.parse_args(["idea", "plan", "12", "--grilling"])
        self.assertEqual(("plan", 12, True), (args.idea_action, args.id, args.grilling))

    def test_project_list_aligns_paths_and_shows_sync_columns_when_connected(self):
        output = io.StringIO()
        args = self.parser.parse_args(["project", "ls"])
        locale = language.set("en")
        self.addCleanup(language.reset, locale)
        with contextlib.closing(sqlite3.connect(":memory:")) as db, contextlib.redirect_stdout(output):
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects(path,path_key,name) VALUES ('/first','first','A')")
            db.execute("INSERT INTO projects(path,path_key,name,nextcloud_sync) VALUES ('/second','second','日本語','off')")
            db.execute("INSERT INTO remote_links VALUES ('inbox',1)")
            manage_project(db, args)
            self.assertNotIn("Deck", output.getvalue())
            output.seek(0)
            output.truncate()
            db.execute("INSERT INTO settings VALUES ('nextcloud_credential','test')")
            manage_project(db, args)
        header, first, second = output.getvalue().splitlines()
        self.assertIn("Nextcloud App", header)
        self.assertIn("Deck", header)
        self.assertRegex(first, r"On\s+Linked")
        self.assertRegex(second, r"Off\s+Unlinked")
        self.assertEqual(text_width(header.split("Path")[0]), text_width(first.split("/first")[0]))
        self.assertEqual(text_width(first.split("/first")[0]), text_width(second.split("/second")[0]))

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

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_console_keeps_repeats_after_switching_from_japanese_input(self):
        events = [KeyEvent(True, 1, 0xE7, 0, ord(char), 0) for char in "あいうえお"]
        events.extend([
            KeyEvent(True, 1, 0x19, 41, 0, 0),
            KeyEvent(False, 1, 0x19, 41, 0, 0),
            KeyEvent(True, 5, 0x41, 30, ord("a"), 0),
            KeyEvent(True, 1, 8, 14, 8, 0),
            KeyEvent(True, 1, 13, 28, 13, 0),
            KeyEvent(True, 2, 13, 28, 13, 0x0008),
            KeyEvent(True, 1, 13, 28, 10, 0x0004),
        ])
        records = iter(InputRecord(1, event) for event in events)

        def read_event(handle, output, limit, count):
            record = next(records)
            ctypes.memmove(output, ctypes.byref(record), ctypes.sizeof(record))
            return True

        def count_events(handle, count):
            ctypes.cast(count, ctypes.POINTER(ctypes.c_ulong)).contents.value = 1
            return True

        with patch("codex_queue.windows_console.ctypes.WinDLL") as load:
            kernel = load.return_value
            kernel.GetNumberOfConsoleInputEvents.side_effect = count_events
            kernel.ReadConsoleInputW.side_effect = read_event
            console = ConsoleInput()
            self.assertEqual(
                list("あいうえおaaaaa\b\r") + ["ctrl+enter"] * 3,
                [console.read() for _ in range(15)],
            )

    def test_slash_command_replaces_only_prefix(self):
        line = "  /proj"
        start, choices = completion(self.parser, line)
        self.assertEqual([Suggestion("project", "Manage projects")], choices)
        self.assertEqual("project", command_text(line[:start] + choices[0].name))

    def test_subcommands_and_options_follow_parser(self):
        self.assertEqual([], completion(self.parser, "task e")[1])
        self.assertEqual([Suggestion("--detail")], completion(self.parser, "idea edit 42 --de")[1])
        self.assertEqual([Suggestion("resolve", "Resolve a completion conflict")], completion(self.parser, "/deck re")[1])
        self.assertNotIn("_worker", [candidate.name for candidate in completion(self.parser, "/")[1]])

    def test_completes_option_choices_after_a_space_or_equals(self):
        for line, expected in (
            ("config --language j", "ja"),
            ("project edit 3 --branch=per-t", "per-task"),
            ("config --priority 3", "3"),
            ("project --base in", "inherit"),
        ):
            with self.subTest(line=line):
                start, choices = completion(self.parser, line)
                self.assertEqual([Suggestion(expected)], choices)
                self.assertTrue((line[:start] + choices[0].name).endswith(expected))
        self.assertEqual([Suggestion("auto"), Suggestion("manual")], completion(self.parser, "config --sync ")[1])
        self.assertEqual([Suggestion("per-idea"), Suggestion("per-task")],
                         completion(self.parser, "project edit 3 --branch=per")[1])

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
            files = completion(self.parser, f'todo revise 1 request --context-file "{Path(folder) / "Project i"}')[1]
            self.assertEqual([file.name], [choice.name for choice in files])
            self.assertTrue(files[0].insertion.endswith(" "))

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_project_add_form_completes_path_and_selects_branches(self):
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            directory = Path(folder) / "Project folder"
            directory.mkdir()
            def complete_field(parser, message, *, show_queue, complete, initial, editor=None):
                self.assertEqual((0, []), complete(""))
                if not message.startswith("Repository path"):
                    return initial
                start, choices = complete(str(Path(folder) / "Project "))
                self.assertEqual(0, start)
                self.assertEqual([str(directory) + os.sep], [choice.insertion for choice in choices])
                self.assertFalse(show_queue)
                return choices[0].insertion

            with patch("codex_queue.terminal.windows_input", side_effect=complete_field), patch(
                "codex_queue.terminal.repo_here", return_value=None,
            ), patch("codex_queue.terminal.git_root", return_value=directory), patch(
                "codex_queue.terminal.branch_names", return_value=["main"],
            ), patch(
                "codex_queue.terminal.choose", side_effect=["branch", "per-task", "base", "unset", ""],
            ), patch("codex_queue.terminal.confirmed", side_effect=lambda command, prompted: command):
                command = fill_project(None, ["project", "add"])
            self.assertIn(f"--path={directory}", command)
            self.assertIn("--name=Project folder", command)
            self.assertIn("--branch=per-task", command)
            self.assertIn("--base=unset", command)

    def test_project_add_retries_invalid_required_values_before_showing_options(self):
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            plain = Path(folder) / "plain"
            plain.mkdir()
            repo = Path(folder) / "repo"
            repo.mkdir()
            missing = str(Path(folder) / "missing")
            calls = []

            def edit(key, heading, initial):
                calls.append((key, heading, initial))
                return next(replies)

            def git_root(value):
                if value == str(plain):
                    raise QueueError("No Git repository")
                self.assertEqual(str(repo), value)
                return repo

            def choose(label, options, default=0):
                self.assertEqual(["path", "path", "path", "name", "name"], [key for key, _, _ in calls])
                return options[default][1]

            replies = iter([missing, str(plain), str(repo), "", "New project"])
            with patch("codex_queue.terminal.git_root", side_effect=git_root), patch(
                "codex_queue.terminal.edit_field", side_effect=edit,
            ), patch("codex_queue.terminal.choose", side_effect=choose), patch(
                "codex_queue.terminal.confirmed", side_effect=lambda command, prompted: command,
            ):
                command = fill_project(None, ["project", "add", "--path=."])
            self.assertEqual(["project", "add", f"--path={repo}", "--name=New project"], command)
            self.assertEqual([".", missing, str(plain), "repo", ""], [initial for _, _, initial in calls])
            self.assertIn("does not exist", calls[1][1])
            self.assertIn("No Git repository", calls[2][1])

    def test_idea_add_retries_title_and_keeps_supplied_detail_without_opening_editor(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects(path, path_key, name) VALUES ('repo', 'repo', 'Notes')")
            bad_title = "Two\nlines"
            with patch("codex_queue.terminal.open_queue", return_value=contextlib.nullcontext(db)), patch(
                "codex_queue.terminal.repo_here", return_value=None,
            ), patch("codex_queue.terminal.field", side_effect=[bad_title, "Title"]) as field, patch(
                "codex_queue.terminal.choose", side_effect=lambda label, options, default=0: options[default][1],
            ), patch("codex_queue.terminal.edit_field") as editor, patch(
                "codex_queue.terminal.confirmed", side_effect=lambda command, prompted: command,
            ):
                command = prepare(["idea", "add", "--detail=Already supplied"])
            self.assertEqual(["add", "--project-id=1", "--detail=Already supplied", "--", "Title"], command)
            self.assertIn("single-line", field.call_args.args[0])
            self.assertEqual(bad_title, field.call_args.args[1])
            editor.assert_not_called()

    def test_project_edit_validates_path_only_when_selected(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects(path, path_key, name) VALUES ('old', 'old', 'Project')")
            with TemporaryDirectory(dir=Path.cwd()) as folder:
                missing = str(Path(folder) / "missing")
                with patch("codex_queue.terminal.choose", side_effect=[1, "path", ""]), patch(
                    "codex_queue.terminal.git_root", return_value=Path(folder),
                ) as git_root, patch(
                    "codex_queue.terminal.edit_field", side_effect=[missing, folder],
                ) as editor, patch(
                    "codex_queue.terminal.confirmed", side_effect=lambda command, prompted: command,
                ):
                    command = fill_project(db, ["project", "edit", "1"])
                self.assertEqual(["project", "edit", "1", f"--path={folder}"], command)
                self.assertEqual("old", editor.call_args_list[0].args[2])
                self.assertEqual(missing, editor.call_args.args[2])
                git_root.assert_called_once_with(folder)

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_edit_starts_with_current_value_and_can_clear_it(self):
        current = "First line\nSecond line"
        for keys, expected in (
            (["\t", "\r"], current),
            (["\x15", "\r"], ""),
            (["\x15", "a", "\n", "b", "\r"], "a\nb"),
            (["ctrl+enter", "続", "\r"], current + "\n続"),
        ):
            with self.subTest(keys=keys), patch(
                "codex_queue.terminal.ConsoleInput.read", side_effect=keys,
            ), patch(
                "codex_queue.terminal.draw_prompt", return_value=(8, 0, 0),
            ) as draw, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(expected, edit_field("prompt", "Instructions", current))
            self.assertEqual((current, []), draw.call_args_list[0].args[2:4])

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_multiline_editor_applies_or_discards_changes_to_blue_field(self):
        for exit_key, expected in (("\x13", "日本\n語"), ("\x1b", "日本")):
            with self.subTest(exit_key=exit_key), patch(
                "codex_queue.terminal.ConsoleInput.read",
                side_effect=["\x05", "\r", "語", exit_key, "\r"],
            ), patch("codex_queue.terminal.virtual_terminal", return_value=contextlib.nullcontext(True)), (
                contextlib.redirect_stdout(io.StringIO())
            ) as output:
                self.assertEqual(expected, edit_field("detail", "Details", "日本"))
            self.assertIn("\x1b[97;44mcq> 日本", output.getvalue())
            self.assertIn("Ctrl+S", output.getvalue())

    def test_editor_can_edit_existing_lines_at_the_cursor(self):
        console = Mock()
        console.read.side_effect = ["up", "home", "delete", "新", "down", "home", "\b", "\x13"]
        with patch("codex_queue.editor.draw_editor", return_value=0):
            self.assertEqual("新行次", edit_text(console, "Details", "前行\n次", True))

    def test_choice_shows_selected_value_in_blue_field(self):
        with patch("codex_queue.terminal.ConsoleInput.read", side_effect=["down", "\r"]), patch(
            "codex_queue.terminal.PosixInput.read", side_effect=["down", "\n"],
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(2, choose_options("Task", [("First", 1), ("Second", 2)], 0, True))
        self.assertIn("\x1b[97;44mcq> First", output.getvalue())
        self.assertIn("\x1b[97;44mcq> Second", output.getvalue())

    def test_ordered_choices_toggle_and_confirm_in_selection_order(self):
        options = [("First", "first"), ("Second", "second"), ("Confirm", APPLY_SELECTION)]
        toggle = ["down", "\r", "up", "\r", "down", "\r", "\r"]
        for finish in (["ctrl+enter"], ["down", "\r"]):
            with self.subTest(finish=finish), patch(
                "codex_queue.terminal.ConsoleInput.read", side_effect=toggle + finish,
            ), patch("codex_queue.terminal.PosixInput.read", side_effect=toggle + finish), patch(
                "codex_queue.terminal.draw_prompt",
            ) as render:
                self.assertEqual(
                    ["first", "second"], choose_options("Models", options, 0, True, ordered=[]),
                )
            final_frame = render.call_args.args[1]
            self.assertIn("1 First", final_frame)
            self.assertIn("2 Second", final_frame)

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_backspace_removes_japanese_characters_in_edit_field(self):
        for backspace in ("\b", "\x7f"):
            with self.subTest(backspace=backspace), patch(
                "codex_queue.terminal.ConsoleInput.read", side_effect=["本", "か", "\u3099", backspace, "ctrl+enter", "\r"],
            ), patch(
                "codex_queue.terminal.draw_prompt", return_value=(8, 0, 0),
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual("日本", edit_field("name", "Task title", "日"))

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_project_and_idea_edits_omit_unchanged_options(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("""INSERT INTO projects(path, path_key, name, prompt, branch, base)
                          VALUES ('old folder', 'old', 'Project', 'Keep me', 'current', 'main')""")
            db.execute("INSERT INTO ideas(project_id, title, detail) VALUES (1, 'Idea', 'Keep me too')")
            db.commit()

            def complete_field(parser, message, *, show_queue, complete, initial, editor=None):
                self.assertEqual((0, []), complete(""))
                if message.startswith("Project name"):
                    return "New project name"
                if "instructions" in message or "Idea details" in message:
                    return ""
                return initial

            with patch("codex_queue.terminal.choose", side_effect=[1, "name", "prompt", "", 1, "detail", ""]), patch(
                "codex_queue.terminal.windows_input", side_effect=complete_field,
            ), patch(
                "codex_queue.terminal.confirmed", side_effect=lambda command, prompted: command,
            ):
                self.assertEqual(["project", "edit", "1", "--name=New project name", "--prompt="], fill_project(db, ["project", "edit", "1"]))
                self.assertEqual(["edit", "1", "--detail="], fill_idea(db, ["edit", "1"]))
            with patch("codex_queue.terminal.choose", side_effect=[1, "", 1, ""]), patch(
                "codex_queue.terminal.windows_input", side_effect=lambda *args, initial, **kwargs: initial,
            ):
                self.assertIsNone(fill_project(db, ["project", "edit", "1"]))
                self.assertIsNone(fill_idea(db, ["edit", "1"]))
            with patch("codex_queue.terminal.choose", side_effect=[1, "planner_model", "custom", "", True, 1, "", True]) as menu, patch(
                "codex_queue.terminal.codex_models", return_value=[("known", "Known")],
            ):
                command = fill_project(db, ["project", "edit", "1", "--planner-model=custom"])
                self.assertEqual("custom", self.parser.parse_args(command).planner_model)
                self.assertIn(("custom", "custom"), menu.call_args_list[2].args[1])
                command = fill_project(db, ["project", "edit", "1", "--nextcloud-sync=off"])
                self.assertEqual("off", self.parser.parse_args(command).nextcloud_sync)

            def clear_field(parser, message, *, show_queue, complete, initial, editor=None):
                return initial if message.startswith(("Repository path", "Idea title")) else ""

            with patch("codex_queue.terminal.choose", side_effect=[1, "name", "prompt", "branch", "", "base", "unset", "", True, 1, "detail", "", True]), patch(
                "codex_queue.terminal.windows_input", side_effect=clear_field,
            ), patch("codex_queue.cli.wake"), contextlib.redirect_stdout(io.StringIO()):
                project = fill_project(db, ["project", "edit", "1"])
                idea = fill_idea(db, ["edit", "1"])
                manage_project(db, self.parser.parse_args(project))
                manage_idea(db, self.parser.parse_args(idea))
            self.assertEqual(("", "", None, ""), tuple(db.execute("SELECT name, prompt, branch, base FROM projects").fetchone()))
            self.assertEqual(("Idea", ""), tuple(db.execute("SELECT title, detail FROM ideas").fetchone()))

    def test_option_menus_show_values_and_defaults(self):
        locale = language.set("ja")
        self.addCleanup(language.reset, locale)
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO settings VALUES ('parallel', '3')")
            db.execute("INSERT INTO projects(path, path_key, name) VALUES ('repo', 'repo', 'Saved project')")
            db.execute("INSERT INTO ideas(project_id, title, detail) VALUES (1, 'Saved idea', 'Saved detail')")
            with patch("codex_queue.terminal.open_queue", return_value=contextlib.nullcontext(db)), patch(
                "codex_queue.terminal.choose", side_effect=["", "", 1, "", 1, ""],
            ) as menu, patch("codex_queue.terminal.confirmed", side_effect=lambda command, prompted: command):
                self.assertEqual(["config"], prepare(["config"]))
                self.assertEqual(["config", "--parallel=4"], prepare(["config", "--parallel=4"]))
                self.assertEqual(["project", "edit", "1", "--name=New project"],
                                 fill_project(db, ["project", "edit", "1", "--name=New project"]))
                self.assertEqual(["edit", "1", "--detail=New detail"],
                                 fill_idea(db, ["edit", "1", "--detail=New detail"]))

            config_options = {key: label for label, key in menu.call_args_list[0].args[1]}
            self.assertEqual("同時に実行するタスク数の上限: 3", config_options["parallel"])
            self.assertEqual("タスクの既定の優先度: 5 (既定値)", config_options["priority"])
            self.assertIn(("同時に実行するタスク数の上限: 4", "parallel"), menu.call_args_list[1].args[1])
            project_options = menu.call_args_list[3].args[1]
            self.assertIn(("プロジェクト名: New project", "name"), project_options)
            self.assertIn(("リポジトリのパス: repo", "path"), project_options)
            self.assertIn(("Nextcloud ボード同期（on/off）: on (既定値)", "nextcloud_sync"), project_options)
            idea_options = menu.call_args_list[5].args[1]
            self.assertIn(("Idea の詳細: New detail", "detail"), idea_options)
            self.assertIn(("Idea のタイトル: Saved idea", "title"), idea_options)

    def test_workflow_choices_show_japanese_and_keep_cli_values(self):
        locale = language.set("ja")
        self.addCleanup(language.reset, locale)
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects(path, path_key, name) VALUES ('repo', 'repo', 'Project')")
            with patch("codex_queue.terminal.open_queue", return_value=contextlib.nullcontext(db)), patch(
                "codex_queue.terminal.choose",
                side_effect=["on_abort", "next-idea", "", 1, "merge_mode", "approval", ""],
            ) as menu, patch("codex_queue.terminal.confirmed", side_effect=lambda command, prompted: command):
                self.assertEqual(["config", "--on-abort=next-idea"], prepare(["config"]))
                self.assertEqual(["project", "edit", "1", "--merge-mode=approval"],
                                 fill_project(db, ["project", "edit", "1"]))
            self.assertIn(("失敗した Idea を保留し、別の Idea へ進む", "next-idea"),
                          menu.call_args_list[1].args[1])
            self.assertIn("失敗した Idea を保留し、別の Idea へ進む",
                          {key: label for label, key in menu.call_args_list[2].args[1]}["on_abort"])
            self.assertIn(("全体設定を継承", "inherit"), menu.call_args_list[5].args[1])
            self.assertIn(("マージは人の承認待ちにして次の ToDo へ進む", "approval"),
                          menu.call_args_list[5].args[1])

    def test_command_forms_supply_missing_arguments_and_keep_explicit_defaults(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects(path, path_key, name) VALUES ('repo', 'repo', 'Notes')")
            db.execute("INSERT INTO ideas(project_id, title, detail) VALUES (1, 'Idea', '')")
            db.execute("INSERT INTO tasks(project_id, name, priority, status) VALUES (1, 'Task', 5, 'ABORTED')")
            db.execute("INSERT INTO settings(key, value) VALUES ('nextcloud_connection', 'server')")
            db.execute(
                "INSERT INTO remote_tasks(connection, uid, task_id, list, url, writeback) "
                "VALUES ('server', 'item', 1, 'list', 'url', 'conflict')"
            )
            with patch("codex_queue.terminal.open_queue", side_effect=lambda: contextlib.nullcontext(db)), patch(
                "codex_queue.terminal.choose", side_effect=lambda label, options, default=0: (
                    True if label == "Save changes?" else options[default][1]
                ),
            ), patch("codex_queue.terminal.field", side_effect=lambda label, default="": (
                default or "15"
            )), patch("codex_queue.terminal.edit_field", side_effect=lambda key, label, initial: (
                initial or {"instruction": "revise it", "note": "15"}.get(key, "")
            )):
                def parsed(*argv):
                    return self.parser.parse_args(prepare(list(argv)))

                self.assertEqual(15, parsed("schedule").every_minutes)
                self.assertEqual(30, parsed("schedule", "--every-minutes=30").every_minutes)
                self.assertEqual(1, parsed("idea", "show").id)
                self.assertEqual(1, parsed("todo", "show").id)
                self.assertEqual("revise it", parsed("task", "revise").instruction)
                self.assertIsNone(parsed("task", "revise").context_file)
                self.assertEqual("history.txt", str(parsed(
                    "task", "revise", "1", "adjust plan", "--context-file=history.txt",
                ).context_file))
                self.assertIsNone(parsed("done", "1").note)
                self.assertFalse(parsed("retry").fresh)
                self.assertTrue(parsed("retry", "--fresh").fresh)
                self.assertTrue(parsed("idea", "list", "--planned").planned)
                self.assertEqual("ja", parsed("settings", "--language=ja").language)
                self.assertEqual("model", parsed("project", "--planner-model=model").planner_model)
                self.assertEqual("https://cloud.example", parsed(
                    "nextcloud", "connect", "--server=https://cloud.example",
                ).server)
                linked = parsed("deck", "link")
                self.assertEqual(("15", 1), (linked.list, linked.project_id))
                self.assertTrue(parsed("deck", "resolve").complete)
                resolved = parsed("deck", "resolve", "--keep-remote")
                self.assertTrue(resolved.keep_remote)
                self.assertFalse(resolved.complete)

    def test_help_completes_nested_topics(self):
        self.assertEqual([Suggestion("help", "Show command help")], completion(self.parser, "/he")[1])
        self.assertEqual([Suggestion("add", "Register a Git repository")], completion(self.parser, "/help project a")[1])

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_screen_shows_slash_help_above_the_input(self):
        token = language.set("en")
        try:
            with patch("codex_queue.terminal.saved_language", return_value="en"), patch(
                "codex_queue.cli.saved_language", return_value="en",
            ), patch("codex_queue.terminal.field", side_effect=lambda label, default: default), patch(
                "codex_queue.terminal.windows_input", side_effect=["/help project add", "q"],
            ) as prompt:
                self.assertEqual(0, screen())
            self.assertIn("--path", prompt.call_args.args[1])
            self.assertFalse(prompt.call_args.kwargs["show_queue"])
        finally:
            language.reset(token)

    def test_ctrl_c_exits_the_screen_without_running_a_command(self):
        token = language.set("en")
        try:
            with patch("codex_queue.terminal.saved_language", return_value="en"), patch(
                "codex_queue.terminal.prompt_input", side_effect=KeyboardInterrupt,
            ), patch(
                "codex_queue.terminal.clear",
            ), patch("codex_queue.terminal.queue_view", return_value="Queue"), patch(
                "codex_queue.terminal.main",
            ) as main, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, screen())
            main.assert_not_called()
        finally:
            language.reset(token)

    def test_grilling_runs_with_live_terminal_output(self):
        argv = ["idea", "plan", "9", "--grilling"]
        output = io.StringIO()

        def run_command(*args, **kwargs):
            self.assertIs(sys.stdout, output)
            self.assertIn("Grilling", output.getvalue())
            print("Grilling question")

        with patch("codex_queue.terminal.prompt_input", side_effect=["idea plan 9 --grilling", "exit"]), patch(
            "codex_queue.terminal.prepare", return_value=argv,
        ), patch("codex_queue.terminal.main", side_effect=run_command) as main, patch(
            "codex_queue.terminal.screen_input",
        ), contextlib.redirect_stdout(output):
            self.assertEqual(0, screen())

        main.assert_called_once_with(argv, interactive=False)
        self.assertIn("Grilling question", output.getvalue())

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_help_is_formatted_again_when_terminal_width_changes(self):
        with patch(
            "codex_queue.terminal.ConsoleInput.read", side_effect=[None, "\r"],
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
            self.assertIn("> add: Idea を登録", frame)
            self.assertIn(Suggestion("remove", "未計画の Idea を削除"), completion(parser, "/rem")[1])
        finally:
            language.reset(token)

    def test_no_completion_in_argument_values(self):
        for line in ('add "title --pr', 'add x --detail --pr', 'add -- --pr'):
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

    def test_virtual_terminal_positions_each_row_without_scrolling(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            draw_prompt("Queue", "", "日本", [], 0, (80, 8), 0, vt=True)
            draw_prompt("Queue", "", "日", [], 0, (40, 4), 0, vt=True)
        self.assertNotIn("\n", output.getvalue())
        self.assertTrue(output.getvalue().endswith("\033[4;1H\033[2K\033[97;44mcq> 日\033[K\033[0m"))

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_arrow_selects_tab_completes_and_enter_submits(self):
        sequence = ["/", "r", "down", "\t", "\r"]
        with patch(
            "codex_queue.terminal.ConsoleInput.read", side_effect=sequence,
        ), patch("codex_queue.terminal.queue_view", return_value="Queue"), patch(
            "codex_queue.terminal.draw_prompt", return_value=(8, 0, 0),
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual("/remove ", windows_input(self.parser, ""))


class QueueViewTest(unittest.TestCase):
    def test_queue_folds_implemented_ideas_and_older_done_tasks(self):
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects(path, path_key, name) VALUES ('test', 'test', 'Notes')")
            db.executemany("INSERT INTO ideas(project_id, title, planned_at) VALUES (1, ?, 'now')",
                           [("Finished idea",), ("Aborted idea",), ("Empty plan",)])
            db.executemany(
                "INSERT INTO tasks(project_id, idea_id, name, status, completed_at, priority) VALUES (1, ?, ?, ?, ?, 1)",
                [(1, "Newest task", "DONE", "2026-11-17")]
                + [(1, f"Task {day:02}", "DONE", f"2026-11-{day:02}") for day in range(1, 17)]
                + [(2, "Aborted task", "ABORTED", None), (1, "Replaced task", "SUPERSEDED", None)],
            )
            with patch("codex_queue.terminal.open_queue", return_value=contextlib.nullcontext(db)):
                collapsed = queue_view()
                self.assertIn("[F2]", collapsed)
                self.assertIn("[F3]", collapsed)
                self.assertNotIn("Finished idea", collapsed)
                self.assertIn("Aborted idea", collapsed)
                self.assertIn("Empty plan", collapsed)
                self.assertLess(collapsed.index("Newest task"), collapsed.index("Task 16"))
                self.assertIn("Task 03", collapsed)
                self.assertNotIn("Task 02", collapsed)
                expanded = queue_view({"f2", "f3"})
                self.assertIn("Finished idea", expanded)
                self.assertIn("Task 02", expanded)
                db.execute("UPDATE tasks SET status = 'QUEUED' WHERE name = 'Newest task'")
                self.assertIn("Finished idea", queue_view())
                self.assertNotIn("[F2]", queue_view())

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
            self.assertLess(view.index("\nToDo\n"), view.index("Index notes"))
            self.assertLess(view.index("Index notes"), view.index("Fix search"))
            self.assertIn("Running: git status", view)
            self.assertLess(view.index("Fix search"), view.index("Update docs"))
            self.assertLess(view.index("Update docs"), view.index("First release"))
            self.assertNotIn("Discarded task", view)
            self.assertNotIn("タスク一覧", japanese_view)
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
            self.assertIn("自動実行は一時停止中です。", cq("task", "ls"))
            help_ja = cq("config", "--help")
            for label in ("使い方: cq config", "オプション:", "このヘルプを表示して終了", "表示言語"):
                self.assertIn(label, help_ja)
            self.assertIn("位置引数:", cq("--help"))
            self.assertNotIn("show this help message", help_ja)
            all_commands = cq("help")
            self.assertIn("コマンド一覧", all_commands)
            self.assertIn("add: Idea を登録", all_commands)
            self.assertIn("task list: タスクを一覧表示", all_commands)
            self.assertIn("nextcloud sync:", all_commands)
            self.assertIn("deck resolve:", all_commands)
            self.assertNotIn("_worker", all_commands)
            self.assertIn("--language", cq("help", "settings"))
            self.assertIn("--keep-remote", cq("help", "deck", "resolve"))
            cq("help", "_worker", code=2)
            cq("config", "--language", "de", code=2)
            self.assertIn("language: ja", cq("config"))
            self.assertIn("language: en", cq("config", "--language", "en"))
            self.assertIn("Automatic execution paused.", cq("task", "ls"))
            help_en = cq("config", "--help")
            self.assertIn("usage: cq config", help_en)
            self.assertIn("show this help message and exit", help_en)
