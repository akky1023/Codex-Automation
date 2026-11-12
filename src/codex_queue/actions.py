import json
import time

from .language import status_label, tr

from .runtime import request_worker
from .store import get_setting, set_setting, update, write
from .worker import HOLD, attempt_folder, latest_attempt


def wake(db):
    if get_setting(db, "enabled") != "1":
        return
    if db.execute("SELECT 1 FROM tasks WHERE status IN (?,?,?,?,?)", HOLD).fetchone():
        print(tr("Queue is waiting. Run cq ls for details.", "キューは待機中です。詳細は cq ls で確認できます。"))
        return
    try:
        requested = request_worker(db)
    except OSError:
        requested = False
    print(tr("Worker requested.", "ワーカーの起動を要求しました。") if requested else tr("Worker will check the queue on its next scheduled run.", "次回の定期起動でキューを確認します。"))


def queue_control(db, command):
    if command in ("start", "resume"):
        with write(db):
            if get_setting(db, "stop") == "1" and db.execute(
                "SELECT 1 FROM tasks WHERE status = 'RUNNING'"
            ).fetchone():
                raise RuntimeError(tr("Wait for the stop request to finish before resuming.", "停止処理が完了してから再開してください。"))
            set_setting(db, "stop", "0")
            set_setting(db, "enabled", "1")
        print(tr("Automatic execution enabled.", "自動実行を有効にしました。"))
        wake(db)
        return
    with write(db):
        set_setting(db, "enabled", "0")
        if command == "stop":
            set_setting(db, "stop", "1")
    while db.execute("SELECT 1 FROM tasks WHERE status = 'RUNNING'").fetchone():
        from .processes import process_lock
        from .worker import recover
        with process_lock("worker.lock") as acquired:
            if acquired:
                recover(db)
                break
        time.sleep(0.2)
    print(tr("Queue stopped.", "キューを停止しました。") if command == "stop" else tr("Queue paused.", "キューを一時停止しました。"))


def task_action(db, args):
    action = args.command
    if action == "logs":
        show_logs(db, args.id)
        return
    with write(db):
        task = db.execute("SELECT status FROM tasks WHERE id = ?", (args.id,)).fetchone()
        if task is None:
            raise RuntimeError(tr("Unknown task ID {id}", "タスク ID {id} が見つかりません", id=args.id))
        status = task["status"]
        attempt = latest_attempt(db, args.id)
        if status in ("RUNNING", "DONE", "REMOVED") or attempt is None:
            raise RuntimeError(tr("Cannot {action} task {id} in state {status}.",
                                  "状態が「{status}」のタスク {id} に {action} は実行できません。",
                                  action=action, id=args.id, status=status_label(status)))
        note = None
        if action == "reply":
            if status != "WAITING_FOR_INPUT" or not attempt["session"]:
                raise RuntimeError(tr("The task has no waiting Codex session.", "回答を待っている Codex セッションがありません。"))
            if not args.answer.strip():
                raise RuntimeError(tr("Enter an answer.", "回答を入力してください。"))
            note = args.answer
            update(db, "attempts", attempt["id"], {"answer": note})
            target = "RESUME_QUEUED"
        elif action == "retry":
            if status == "RATE_LIMITED":
                raise RuntimeError(tr("This task will retry on a scheduled run after the usage limit wait.", "利用上限が解除された後の定期起動で再試行します。"))
            if status not in ("NEEDS_REVIEW", "INTERRUPTED", "AUTH_REQUIRED", "RETRY_WAIT", "ABORTED"):
                raise RuntimeError(tr("Cannot retry a task in state {status}.", "状態が「{status}」のタスクは再試行できません。",
                                      status=status_label(status)))
            if args.fresh:
                target = "QUEUED"
            elif attempt["session"] or attempt["phase"] in ("PREPARING", "READY", "REPORTED", "RESTORE"):
                target = "RESUME_QUEUED"
                update(db, "attempts", attempt["id"], {"retry_at": None, "failures": 0})
            else:
                raise RuntimeError(tr("No resumable session. Check the logs and use cq retry --fresh.", "再開できるセッションがありません。ログを確認し、cq retry --fresh を使ってください。"))
        elif action == "abort":
            if status not in ("NEEDS_REVIEW", "WAITING_FOR_INPUT"):
                raise RuntimeError(tr("Only tasks needing review or input can be aborted.", "中止できるのは確認待ちまたは入力待ちのタスクです。"))
            if get_setting(db, "on_abort", "pause") == "pause":
                set_setting(db, "enabled", "0")
            target = "ABORTED"
        else:
            if status == "QUEUED":
                raise RuntimeError(tr("Only a stopped, started task can be marked done.", "完了にできるのは実行を開始した後で停止したタスクです。"))
            target = "DONE"
            note = args.note
        db.execute(
            """UPDATE tasks SET status = ?, completed_at =
               CASE WHEN ? = 'DONE' THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id = ?""",
            (target, target, args.id),
        )
        db.execute("INSERT INTO task_actions(task_id, action, note) VALUES (?, ?, ?)", (args.id, action, note))
    print(tr("Task {id}: {status}", "タスク {id}：{status}", id=args.id, status=status_label(target)))
    wake(db)


def show_logs(db, task_id):
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise RuntimeError(tr("Unknown task ID {id}", "タスク ID {id} が見つかりません", id=task_id))
    print(f"{task['id']}  {status_label(task['status'])}  {task['name']}")
    if task["completed_at"]:
        print(tr("Completed: {time} UTC", "完了日時：{time} UTC", time=task["completed_at"]))
    for attempt in db.execute("SELECT * FROM attempts WHERE task_id = ? ORDER BY number", (task_id,)):
        print(tr("Attempt {number}", "試行 {number}", number=attempt["number"]), attempt["started_at"], attempt["phase"], sep="  ")
        print("  " + tr("Repository: {path}", "リポジトリ：{path}", path=attempt["path"]))
        print("  " + tr("Base: {base}  Commit: {commit}", "開始元：{base}  コミット：{commit}",
                        base=attempt["base"], commit=attempt["start_commit"]))
        print("  " + tr("Branch: {branch}  Final commit: {commit}", "ブランチ：{branch}  最終コミット：{commit}",
                        branch=attempt["branch"], commit=attempt["end_commit"]))
        print("  " + tr("Session: {session}  Logs: {path}", "セッション：{session}  ログ：{path}",
                        session=attempt["session"], path=attempt_folder(attempt)))
        if attempt["reason"]:
            print(f"  {attempt['reason']}")
        if attempt["report"]:
            report = json.loads(attempt["report"])
            print(f"  {report['status']}: {report['reason']}")
            print("  " + tr("Verification: {details}", "検証結果：{details}", details=report["verification"]))
    for row in db.execute("SELECT * FROM task_actions WHERE task_id = ? ORDER BY rowid", (task_id,)):
        print(f"{row['created_at']}  {row['action']}  {row['note'] or ''}")
