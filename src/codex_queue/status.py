from datetime import datetime
import json
import os

from .language import status_label, tr
from .store import get_setting
from .worker import attempt_folder, latest_attempt


OUTCOMES = ("DONE", "ABORTED", "WAITING_FOR_INPUT")
RECENT_DONE_LIMIT = 15


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


def archive(lines, heading, items, expanded, key):
    if not items:
        return
    if expanded is None:
        lines.extend([f"{heading} ({len(items)})", *items])
        return
    is_open = key in expanded
    lines.append(f"{'▼' if is_open else '▶'} {heading} ({len(items)})  [{key.upper()}]")
    if is_open:
        lines.extend(items)


def queue_status(db, expanded=None):
    lines = ["Codex Queue", tr("Automatic execution enabled", "自動実行は有効です") if get_setting(db, "enabled") == "1" else tr("Automatic execution paused", "自動実行は一時停止中です")]
    rows = db.execute(
        """SELECT t.*, p.name AS project FROM tasks t JOIN projects p ON p.id=t.project_id
           WHERE t.status NOT IN ('REMOVED', 'SUPERSEDED') ORDER BY t.priority, t.id"""
    )
    running = []
    tasks = []
    results = {status: [] for status in OUTCOMES}
    counts = dict.fromkeys(OUTCOMES, 0)
    completed_ideas = {}
    done = []
    for row in rows:
        status = row["status"]
        label = f"{row['id']}  {row['project']}  {row['name']}"
        idea_id = row["idea_id"]
        if idea_id is not None:
            completed_ideas[idea_id] = completed_ideas.get(idea_id, True) and status == "DONE"
        if status == "DONE":
            attempt = latest_attempt(db, row["id"])
            if attempt and attempt["merge_status"] in ("PENDING", "CONFLICT", "BLOCKED", "FAILED"):
                tasks.append(f"{label}  {tr('Merge', 'マージ')}  {attempt['merge_status']}")
            done.append((row["completed_at"] or "", row["id"], label))
            continue
        if status == "RUNNING":
            running.append(f"{status_label(status)}  {label}")
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
        if status in results:
            counts[status] += 1
            destination = results[status]
            destination.append(label)
        else:
            destination = tasks
            destination.append(f"{status_label(status)}  {label}")
        if status == "QUEUED":
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
    ideas = []
    planned = []
    implemented = []
    for row in db.execute(
        """SELECT i.*, p.name AS project FROM ideas i JOIN projects p ON p.id=i.project_id
           ORDER BY i.id"""
    ):
        label = f"{row['id']}  {row['project']}  {row['title']}"
        if row["planned_at"]:
            (implemented if completed_ideas.get(row["id"]) else planned).append(label)
        else:
            ideas.append(label)
            if row["planning_error"]:
                ideas.append(f"  {row['planning_error']}")
            elif row["draft_plan"]:
                ideas.append("  " + tr("Awaiting plan approval", "計画の承認待ち") + f"  cq idea show {row['id']}")
    for status, heading in (("WAITING_FOR_INPUT", "Waiting response"), ("ABORTED", "Abort")):
        if counts[status]:
            lines.extend(["", f"{heading} ({counts[status]})", *results[status]])
    lines.extend(["", "Idea", *ideas])
    if not (ideas or planned or implemented):
        lines.append(tr("No ideas", "Idea はありません"))
    if planned:
        lines.extend(["Planned", *planned])
    archive(lines, tr("Implemented Ideas", "実装済みのIdea"), implemented, expanded, "f2")
    todos = running + tasks
    lines.extend(["", "ToDo", *(todos or [tr("No ToDos", "ToDo はありません")])])
    if done:
        done_labels = [label for _, _, label in sorted(done, reverse=True)]
        lines.extend(["", "Done", *(done_labels if expanded is None else done_labels[:RECENT_DONE_LIMIT])])
        if expanded is not None:
            archive(lines, tr("Earlier Done", "以前の Done"), done_labels[RECENT_DONE_LIMIT:], expanded, "f3")
    if error := get_setting(db, "nextcloud_error"):
        lines.append(error)
    return "\n".join(lines)
