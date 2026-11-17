import io
import threading
import unittest
from unittest.mock import patch

from codex_queue import progress
from codex_queue.language import language


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ProgressTest(unittest.TestCase):
    def test_animation_updates_before_completion_and_joins_on_every_exit(self):
        for error in (None, RuntimeError, KeyboardInterrupt, SystemExit):
            with self.subTest(error=error):
                stream = Terminal()
                updated = threading.Event()
                original = progress._draw
                calls = []

                def draw():
                    original()
                    calls.append(threading.get_ident())
                    if len(calls) > 1:
                        updated.set()

                with progress.output_to(stream), patch.object(progress, '_draw', side_effect=draw):
                    try:
                        with progress.waiting('Planning', '計画中'):
                            self.assertTrue(updated.wait(2), 'No update during the wait')
                            self.assertNotEqual(calls[0], calls[1])
                            self.assertIn('Planning', stream.getvalue())
                            if error:
                                raise error()
                    except BaseException as caught:
                        self.assertIs(type(caught), error)
                self.assertFalse(progress._active)
                self.assertFalse(any(t.name == 'cq-progress' for t in threading.enumerate()))
                self.assertTrue(stream.getvalue().endswith('\r'))

    def test_redirected_output_has_only_translated_start_and_end(self):
        stream = io.StringIO()
        token = language.set('ja')
        try:
            with progress.output_to(stream), progress.waiting('Planning', '計画中'):
                with progress.waiting('Request', '通信中'):
                    pass
        finally:
            language.reset(token)
        self.assertEqual('計画中...\n計画中: 完了\n', stream.getvalue())

    def test_nested_and_concurrent_operations_restore_visible_wait(self):
        stream = Terminal()
        entered, release = threading.Event(), threading.Event()

        def parallel():
            with progress.output_to(stream), progress.waiting('Parallel', '並行'):
                entered.set()
                release.wait(2)

        with progress.output_to(stream), progress.waiting('Outer', '外側'):
            with progress.waiting('Inner', '内側'):
                self.assertEqual(2, len(progress._active))
                self.assertEqual('Inner', progress._active[-1][1])
            self.assertEqual('Outer', progress._active[-1][1])
            thread = threading.Thread(target=parallel)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(2, len(progress._active))
            finally:
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual('Outer', progress._active[-1][1])
        self.assertFalse(progress._active)

    def test_prompts_suspend_all_drawing_and_resume_after_exception(self):
        stream = Terminal()
        with progress.output_to(stream), progress.waiting('Planning', '計画中'):
            with self.assertRaises(ValueError), progress.suspended():
                before = stream.getvalue()
                with progress.suspended():
                    with progress._lock:
                        progress._draw()
                    self.assertEqual(before, stream.getvalue())
                raise ValueError()
            self.assertEqual(0, progress._paused)
            self.assertGreater(len(stream.getvalue()), len(before))

    def test_closed_or_absent_stderr_does_not_break_work(self):
        stream = io.StringIO()
        stream.close()
        with progress.output_to(stream), progress.waiting('Work', '処理中'):
            pass
        with patch.object(progress.sys, 'stderr', None), progress.waiting('Work', '処理中'):
            pass

    def test_spinner_uses_monotonic_time(self):
        self.assertEqual('|', progress.spinner(0))
        self.assertEqual('/', progress.spinner(progress.INTERVAL))
