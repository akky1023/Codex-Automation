import sqlite3
import unittest

from codex_queue.nextcloud import Client, apply_snapshot, completed_body, todo_fields, writeback
from codex_queue.store import SCHEMA


BODY = (
    'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:item\r\n'
    'DESCRIPTION;ALTREP="https://example.test/details":line one\\nline\r\n two\r\n'
    'STATUS:NEEDS-ACTION\r\nBEGIN:VALARM\r\nDESCRIPTION:alarm\r\nEND:VALARM\r\n'
    'END:VTODO\r\nEND:VCALENDAR\r\n'
)


class CalDAVTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        with self.db:
            self.db.execute("INSERT INTO projects(path,path_key,name) VALUES ('test','test','test')")
            self.db.execute("INSERT INTO remote_links VALUES ('inbox',1)")
            self.links = {"inbox": 1}
            self.snapshot = {"item": [("inbox", "https://example.test/item", '"original"', todo_fields(BODY))]}
            apply_snapshot(self.db, "test", self.links, self.snapshot, False)

    def test_failure_and_duplicate_do_not_withdraw_task(self):
        with self.db:
            apply_snapshot(self.db, "test", self.links, {}, True)
            self.assertEqual(1, self.db.execute("SELECT blocked FROM remote_tasks").fetchone()[0])
            apply_snapshot(self.db, "test", self.links, {"item": self.snapshot["item"] * 2}, False)
            self.assertEqual("QUEUED", self.db.execute("SELECT status FROM tasks").fetchone()[0])
            apply_snapshot(self.db, "test", self.links, {}, False)
            self.assertEqual("ABORTED", self.db.execute("SELECT status FROM tasks").fetchone()[0])

    def test_conflict_and_explicit_completion(self):
        with self.db:
            self.db.execute("UPDATE tasks SET status='DONE', completed_at='2026-11-12 00:00:00'")
        row = self.db.execute(
            "SELECT r.*, t.completed_at FROM remote_tasks r JOIN tasks t ON t.id=r.task_id"
        ).fetchone()

        class MockClient:
            def __init__(self):
                self.updates = []

            def request(self, method, url, body=None, headers=None, acceptable=()):
                if method == "GET":
                    return 200, {"ETag": '"changed"'}, BODY.encode()
                self.updates.append((body, headers))
                return 204, {}, b""

        client = MockClient()
        self.assertEqual("conflict", writeback(self.db, client, row))
        self.assertEqual([], client.updates)
        self.assertEqual("synced", writeback(self.db, client, row, force=True))
        payload, headers = client.updates[0]
        self.assertEqual('"changed"', headers["If-Match"])
        self.assertEqual("COMPLETED", todo_fields(payload.decode())["STATUS"])
        self.assertEqual("DONE", self.db.execute("SELECT status FROM tasks").fetchone()[0])

    def test_done_task_is_not_requeued_and_remote_url_can_follow_a_move(self):
        with self.db:
            self.db.execute("UPDATE tasks SET status='DONE'")
            apply_snapshot(self.db, "test", self.links, {"item": [
                ("inbox", "https://example.test/moved", '"new"', todo_fields(BODY)),
            ]}, False)
        self.assertEqual([("DONE",)], [tuple(row) for row in self.db.execute("SELECT status FROM tasks")])
        self.assertTrue(self.db.execute("SELECT url FROM remote_tasks").fetchone()[0].endswith("/moved"))

    def test_completion_preserves_description_folding_and_alarm(self):
        result = completed_body(BODY, "20261112T000000Z")
        self.assertIn('line\r\n two', result)
        self.assertIn("BEGIN:VALARM\r\nDESCRIPTION:alarm\r\nEND:VALARM", result)
        self.assertEqual(todo_fields(BODY)["DESCRIPTION"], todo_fields(result)["DESCRIPTION"])

    def test_credentials_cannot_follow_cross_origin_links(self):
        client = Client("https://example.test")
        with self.assertRaises(RuntimeError):
            client.href("https://other.test/calendar")
        with self.assertRaises(RuntimeError):
            client.href("http://example.test/calendar")
