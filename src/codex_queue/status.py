from datetime import datetime
import json
import os

from .language import status_label, tr
from .store import get_setting
from .worker import attempt_folder, latest_attempt


def latest_progress(attempt):
    directory = attempt_folder(attempt)
    try:
        runs = [p for p in directory.iterdir() if p.is_dir() and p.name.isdecimal()]
        if not runs:
            return ""
        file = max(runs, key=lambda p: int(p.name)) / "events.jsonl"
        with file.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 65536))
            for line in reversed(stream.readlines()):
                try:
                    item = json.loads(line).get("item", {})
                except (ValueError, AttributeError):
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    command = item.get("command")
                    if isinstance(command, str) and command.strip():
                        action = {
                            "in_progress": tr("Running", "実行中"),
                            "completed": tr("Finished", "実行済み"),
                            "failed": tr("Failed", "失敗"),
                        }.get(item.get("status"), tr("Command", "コマンド"))
                        text = f"{action}: {command}"
                    if isinstance(text, str) and text.strip():
                        return " ".join(text.split())[:160]
    except OSError:
        pass
    return ""


def queue_status(db):
    lines = ["Codex Queue", tr("Automatic execution enabled", "自動実行は有効です") if get_setting(db, "enabled") == "1" else tr("Automatic execution paused", "自動実行は一時停止中です")]
    rows = db.execute(
        """SELECT t.*, p.name AS project FROM tasks t JOIN projects p ON p.id=t.project_id
           WHERE t.status <> 'REMOVED' ORDER BY (t.status = 'DONE'), t.priority, t.id"""
    )
    running = []
    tasks = []
    replies = []
    reply_tasks = []
    for row in rows:
        status = row["status"]
        label = f"{row['id']}  {row['project']}  {row['name']}"
        if status == "RUNNING":
            running.append(label)
            attempt = latest_attempt(db, row["id"])
            if attempt:
                progress = latest_progress(attempt)
                if not progress:
                    progress = (
                        tr("Preparing workspace", "作業フォルダを準備中") if attempt["phase"] in ("PREPARING", "CHECKOUT", "READY")
                        else tr("Waiting for Codex output", "Codex の出力を待機中")
                    )
                running.append(f"  {attempt['started_at']} UTC  {progress}")
            else:
                running.append(tr("  Preparing workspace", "  作業フォルダを準備中"))
            continue
        waiting = status == "WAITING_FOR_INPUT"
        destination = reply_tasks if waiting else tasks
        destination.append(f"{status_label(status)}  {label}")
        if waiting:
            replies.append(str(row["id"]))
        if status in ("QUEUED", "DONE", "ABORTED"):
            continue
        attempt = latest_attempt(db, row["id"])
        if attempt:
            if attempt["reason"]:
                destination.append(f"  {attempt['reason']}")
            if attempt["retry_at"]:
                retry = datetime.fromtimestamp(attempt["retry_at"]).astimezone()
                destination.append("  " + tr("Retry after {time}", "再試行予定 {time}", time=retry.isoformat()))
        if waiting:
            destination.append(f'  cq reply {row["id"]} "..."')
    if replies:
        lines.append(tr("Replies needed ({count}): {ids}", "返信待ち（{count}件）：{ids}",
                        count=len(replies), ids=", ".join(replies)))
        lines.extend(reply_tasks)
    else:
        lines.append(tr("No replies needed", "返信待ちのタスクはありません"))
    lines.extend(["", tr("In progress", "実行中"), *(running or [tr("No tasks running", "実行中のタスクはありません")])])
    lines.extend(["", tr("Tasks", "タスク一覧"), *(tasks or [tr("No other tasks", "ほかのタスクはありません")])])
    if error := get_setting(db, "nextcloud_error"):
        lines.append(error)
    return "\n".join(lines)
