from argparse import Namespace
from getpass import getpass
import json
import sqlite3
from subprocess import SubprocessError
import time
from urllib.request import ProxyHandler, build_opener

from .actions import task_action, wake
from .nextcloud import Client, RejectRedirects, connection
from .planner import plan_idea, revise_task
from .processes import process_lock
from .store import write
from .worker import latest_attempt


class LocalBoardClient(Client):
    """Allow HTTP only for the explicitly selected local Docker instance."""

    def __init__(self, port, auth):
        self.server = f"http://127.0.0.1:{port}"
        self.auth = auth
        self.opener = build_opener(ProxyHandler({}), RejectRedirects())

    def href(self, value, base=None):
        if not value.startswith(self.server + "/"):
            raise RuntimeError("Local board request is outside the development instance.")
        return value


def add_board_command(commands):
    parser = commands.add_parser("nextcloud-board", help="Synchronize a Nextcloud Codex Queue board")
    add_board_options(parser)


def add_board_options(parser):
    parser.add_argument("--project-id", type=int, action="append",
                        help="Limit synchronization to these projects (default: all enabled projects)")
    parser.add_argument("--watch", action="store_true", help="Keep checking for board requests")
    parser.add_argument("--dev-port", type=int, help="Use a local HTTP development instance")


def ocs(client, method, data=None):
    url = client.server + "/ocs/v2.php/apps/codex_queue/api/v1/bridge?format=json"
    _, _, body = client.request(
        method, url, None if data is None else json.dumps(data).encode("utf-8"),
        {"OCS-APIRequest": "true", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        response = json.loads(body)["ocs"]
        if response["meta"]["statuscode"] not in (100, 200):
            raise ValueError("invalid OCS status")
        return response["data"]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError("Unexpected response from the Nextcloud board.") from error


def board_snapshot(db, projects):
    marks = ", ".join("?" for _ in projects)
    ideas = [dict(row) for row in db.execute(
        f"""SELECT id, project_id, title, detail, planned_at, planning_error FROM ideas
            WHERE project_id IN ({marks}) ORDER BY id DESC""", tuple(projects),
    )]
    tasks = []
    for row in db.execute(
        f"""SELECT id, project_id, idea_id, name, abstract, prompt, status, completed_at FROM tasks
            WHERE project_id IN ({marks}) AND status NOT IN ('REMOVED', 'SUPERSEDED')
            ORDER BY priority, id DESC""", tuple(projects),
    ):
        task = dict(row)
        attempt = latest_attempt(db, row["id"])
        if attempt:
            task["reason"] = attempt["reason"]
            task["report"] = attempt["report"]
        tasks.append(task)
    return {"projects": projects, "ideas": ideas, "tasks": tasks}


def request_action(db, request, projects, key):
    kind, target = request["kind"], int(request["target_id"])
    title, body = request["title"], request["body"]
    if kind == "sync":
        return "同期しました。"
    if kind == "add":
        if target not in projects:
            raise RuntimeError("同期対象のプロジェクトを選んでください。")
        with write(db):
            db.execute("INSERT INTO ideas(project_id, title, detail) VALUES (?, ?, ?)", (target, title, body))
            save_outcome(db, key, "Idea を追加しました。")
        return "Idea を追加しました。"
    if kind in ("edit", "remove", "plan"):
        with write(db):
            idea = db.execute("SELECT * FROM ideas WHERE id = ?", (target,)).fetchone()
            if idea is None or idea["project_id"] not in projects or idea["planned_at"]:
                raise RuntimeError("未計画の Idea が見つかりません。")
            if kind == "edit":
                db.execute("UPDATE ideas SET title = ?, detail = ?, planning_error = NULL, draft_plan = NULL WHERE id = ?",
                           (title, body, target))
                save_outcome(db, key, "Idea を更新しました。")
                return "Idea を更新しました。"
            if kind == "remove":
                db.execute("DELETE FROM ideas WHERE id = ?", (target,))
                save_outcome(db, key, "Idea を削除しました。")
                return "Idea を削除しました。"
        ids = plan_idea(db, target)
        if not ids:
            return f"ブランチ運用の承認待ちです。cq idea show {target} で提案を確認できます。"
        return "To Do " + ", ".join(map(str, ids)) + " を作成しました。"
    task = db.execute("SELECT project_id FROM tasks WHERE id = ?", (target,)).fetchone()
    if task is None or task["project_id"] not in projects:
        raise RuntimeError("同期対象の To Do が見つかりません。")
    if kind == "revise":
        task_id, question = revise_task(db, target, body)
        return question or f"To Do {task_id} を更新しました。"
    if kind in ("reply", "retry"):
        task_action(db, Namespace(command=kind, id=target, answer=body, fresh=False))
        return "依頼を処理しました。"
    if kind in ("done", "abort"):
        task_action(db, Namespace(command=kind, id=target, note=body or None), expected_status="NEEDS_REVIEW")
        return f"To Do {target} を{'完了' if kind == 'done' else '中止'}にしました。"
    raise RuntimeError("この操作には対応していません。")


def save_outcome(db, key, outcome):
    db.execute("UPDATE board_receipts SET outcome = ? WHERE board = ? AND request_id = ?", (outcome, *key))


def exchange(db, client, projects):
    incoming = ocs(client, "GET")
    board = json.dumps([incoming["instance"], incoming["owner"]], ensure_ascii=False)
    results = []
    for request in incoming["requests"]:
        request_id = int(request["id"])
        key = (board, request_id)
        with write(db):
            reserved = db.execute(
                "INSERT OR IGNORE INTO board_receipts VALUES (?, ?, ?)",
                (*key, "処理が中断されました。ボードの状態を確認してから、必要なら再度依頼してください。"),
            ).rowcount
        if reserved:
            try:
                outcome = request_action(db, request, projects, key)
            except (OSError, RuntimeError, sqlite3.Error, SubprocessError) as error:
                outcome = f"処理できませんでした。{error}"
            with write(db):
                save_outcome(db, key, outcome[:1800])
        outcome = db.execute("SELECT outcome FROM board_receipts WHERE board = ? AND request_id = ?", key).fetchone()[0]
        results.append({"id": request_id, "outcome": outcome})
    ocs(client, "POST", {"snapshot": board_snapshot(db, projects), "results": results})
    if any(request["kind"] in ("add", "plan", "edit", "revise") for request in incoming["requests"]):
        wake(db)


def run_board(db, args):
    project_ids = set(args.project_id) if args.project_id else None
    if project_ids and not project_ids <= {row[0] for row in db.execute("SELECT id FROM projects WHERE retired = 0")}:
        raise RuntimeError("Select active projects with --project-id.")
    local_client = None
    if args.dev_port is not None:
        if not 1 <= args.dev_port <= 65535:
            raise RuntimeError("Invalid development port.")
        username = input("Nextcloud user: ").strip()
        local_client = LocalBoardClient(args.dev_port, (username, getpass("Nextcloud app password: ")))
    with process_lock("nextcloud-board.lock") as acquired:
        if not acquired:
            raise RuntimeError("The Nextcloud board is already synchronizing.")
        while True:
            try:
                client = local_client if local_client is not None else connection(db)[0]
                exchange(db, client, available_projects(db, project_ids))
            except (OSError, RuntimeError) as error:
                if not args.watch:
                    raise
                print(f"Nextcloud board: {error}", flush=True)
            if not args.watch:
                return
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                return


def available_projects(db, project_ids=None):
    return {
        row["id"]: row["name"] for row in db.execute(
            "SELECT id, name FROM projects WHERE retired = 0 AND nextcloud_sync = 'on' ORDER BY id"
        )
        if project_ids is None or row["id"] in project_ids
    }
