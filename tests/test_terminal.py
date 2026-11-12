import contextlib
import io
import os
import unittest
from unittest.mock import patch

from codex_queue.cli import make_parser
from codex_queue.terminal import command_text, completion, windows_input


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
        self.assertNotIn("_worker", completion(self.parser, "")[1])

    def test_no_completion_in_argument_values(self):
        for line in ('add "title --pr', 'add x --prompt --pr', 'add -- --pr'):
            with self.subTest(line=line):
                self.assertEqual([], completion(self.parser, line)[1])

    @unittest.skipUnless(os.name == "nt", "Windows console input")
    def test_arrow_selects_tab_completes_and_enter_submits(self):
        sequence = ["/", "r", "down", "\t", "\r"]
        with patch("msvcrt.kbhit", return_value=True), patch(
            "codex_queue.terminal.keys", side_effect=sequence,
        ), patch("codex_queue.terminal.queue_view", return_value="Queue"), patch(
            "codex_queue.terminal.draw_prompt",
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual("/remove ", windows_input(self.parser, ""))
