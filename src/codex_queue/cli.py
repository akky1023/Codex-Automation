import argparse
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import subprocess

from .actions import queue_control, task_action, wake
from .arguments import ArgumentParser, parser_candidates
from .language import LANGUAGES, language, status_label, tr
from .planner import plan_idea, revise_task
from .runtime import register_schedule, setup
from .status import OUTCOMES, queue_status
from .store import DEFAULTS, get_setting, open_queue, saved_language, settings, update, write
from .text_layout import print_wrapped
from .worker import latest_attempt, run_worker


BRANCH_MODES = ("per-task", "current")
MODEL_SETTINGS = ("planner_model", "implementation_model", "allowed_models")


class QueueError(Exception):
    pass


def git_root(path):
    folder = Path(path).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=folder, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode:
        raise QueueError(tr("No Git repository at {path}", "{path} に Git リポジトリがありません", path=folder))
    return Path(result.stdout.strip()).resolve()


def changes(args, *keys):
    return {key: getattr(args, key) for key in keys if getattr(args, key) is not None}


def by_id(db, table, row_id):
    active = " AND retired = 0" if table == "projects" else ""
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?{active}", (row_id,)).fetchone()
    if row is None:
        entity = {"projects": tr("project", "プロジェクト"), "tasks": tr("task", "タスク"),
                  "ideas": tr("idea", "アイデア")}[table]
        raise QueueError(tr("Unknown {entity} ID {id}", "{entity} ID {id} が見つかりません", entity=entity, id=row_id))
    return row


def show_projects(rows):
    for row in rows:
        print(f"{row['id']}\t{row['name']}\t{row['path']}")


def active_projects(db):
    return db.execute("SELECT * FROM projects WHERE retired = 0 ORDER BY id").fetchall()


def path_project(db, root, create=False):
    key = os.path.normcase(str(root))
    row = db.execute("SELECT * FROM projects WHERE path_key = ?", (key,)).fetchone()
    if row is not None and not row["retired"]:
        return row
    if not create:
        raise QueueError(tr("Project is not registered: {path}", "未登録のプロジェクト：{path}", path=root))
    if row is None:
        cursor = db.execute(
            "INSERT INTO projects(path, path_key, name) VALUES (?, ?, ?)",
            (str(root), key, root.name),
        )
        return by_id(db, "projects", cursor.lastrowid)
    db.execute(
        """UPDATE projects SET retired = 0, name = ?, prompt = '',
                  branch = NULL, base = NULL, planner_model = NULL,
                  implementation_model = NULL, allowed_models = NULL WHERE id = ?""",
        (root.name, row["id"]),
    )
    return by_id(db, "projects", row["id"])


def project_for_idea(db, args):
    if args.project_id is not None:
        return by_id(db, "projects", args.project_id)
    if args.project is not None:
        candidates = [row for row in active_projects(db) if row["name"] == args.project]
        if len(candidates) == 1:
            return candidates[0]
        show_projects(candidates or active_projects(db))
        raise QueueError(tr("Choose a registered project with --project-id ID.", "--project-id ID で登録済みのプロジェクトを選んでください。"))
    try:
        root = git_root(".")
    except QueueError:
        show_projects(active_projects(db))
        raise QueueError(tr("Use --project NAME or --project-id ID outside a Git repository.", "Git リポジトリの外では --project NAME または --project-id ID を指定してください。")) from None
    return path_project(db, root, create=True)


def project_options(args):
    values = changes(args, "name", "prompt", "branch", "base", *MODEL_SETTINGS)
    for key in MODEL_SETTINGS:
        if values.get(key) == "inherit":
            values[key] = None
    if args.branch == "":
        values["branch"] = None
    if args.base in ("inherit", "unset"):
        values["base"] = None if args.base == "inherit" else ""
    return values


def show_project(db, row):
    defaults = settings(db)
    inherited = tr("(inherited)", "（継承）")
    unset = tr("unset", "未設定")
    branch = row["branch"] or f"{defaults['branch']} {inherited}"
    base = row["base"]
    if base is None:
        base = f"{defaults['base'] or unset} {inherited}"
    show_projects([row])
    print(tr("Branch: {branch}\nBase: {base}\nPrompt: {prompt}",
             "ブランチ運用：{branch}\n開始元：{base}\n指示：{prompt}",
             branch=branch, base=base or unset, prompt=row["prompt"]))
    for key in MODEL_SETTINGS:
        value = (row[key] or unset) if row[key] is not None else f"{defaults[key] or unset} {inherited}"
        print(f"{key.replace('_', '-')}  {value}")


def manage_project(db, args):
    action = args.project_action
    if action in ("ls", "list"):
        show_projects(active_projects(db))
        return
    with write(db):
        if action in ("rm", "remove"):
            row = by_id(db, "projects", args.id)
            if db.execute("SELECT 1 FROM ideas WHERE project_id = ? AND planned_at IS NULL",
                          (row["id"],)).fetchone():
                raise QueueError(tr("Plan or remove outstanding ideas first.", "未計画の Idea を計画するか削除してください。"))
            if db.execute(
                "SELECT 1 FROM tasks WHERE project_id = ? AND status NOT IN ('DONE', 'ABORTED', 'REMOVED', 'SUPERSEDED')",
                (row["id"],),
            ).fetchone():
                raise QueueError(tr("Resolve or remove unfinished tasks before removing this project.", "プロジェクトを削除する前に、未完了のタスクを解決するか削除してください。"))
            db.execute("DELETE FROM remote_links WHERE project_id = ?", (row["id"],))
            update(db, "projects", row["id"], {"retired": 1})
            return
        values = project_options(args)
        if action == "edit":
            row = by_id(db, "projects", args.id)
            if args.path is not None:
                if not args.path:
                    raise QueueError(tr("Repository path is required.", "リポジトリのパスは必須です。"))
                root = git_root(args.path)
                if os.path.normcase(str(root)) != row["path_key"]:
                    if db.execute(
                        """SELECT 1 FROM tasks WHERE project_id = ? AND status IN
                           ('RUNNING', 'INTERRUPTED', 'WAITING_FOR_INPUT', 'NEEDS_REVIEW',
                            'RESUME_QUEUED', 'RATE_LIMITED', 'AUTH_REQUIRED', 'RETRY_WAIT')""",
                        (row["id"],),
                    ).fetchone():
                        raise QueueError(tr("Resolve active or suspended tasks before changing the path.", "パスを変更する前に、実行中または中断中のタスクを解決してください。"))
                    values.update(path=str(root), path_key=os.path.normcase(str(root)))
        else:
            row = path_project(db, git_root(args.path or "."), create=bool(values) or action == "add")
        update(db, "projects", row["id"], values)
        row = by_id(db, "projects", row["id"])
    show_project(db, row)


def manage_task(db, args):
    action = args.command
    if action == "revise":
        instruction = args.instruction
        if args.context_file:
            instruction += "\n\n# Additional history\n" + args.context_file.read_text(encoding="utf-8-sig")
        task_id, question = revise_task(db, args.id, instruction)
        if question:
            print(question)
            print(tr("Run the same request again with --context-file or include the answer in the instruction.",
                     "回答を指示に追記するか、--context-file で補足を渡して再実行してください。"))
        else:
            print(tr("Revision queued as ToDo {id}.", "修正計画を ToDo {id} として登録しました。", id=task_id))
            wake(db)
        return
    if action == "show":
        row = db.execute(
            """SELECT t.*, p.name AS project, i.title AS idea_title FROM tasks t
               JOIN projects p ON p.id = t.project_id LEFT JOIN ideas i ON i.id = t.idea_id
               WHERE t.id = ?""", (args.id,),
        ).fetchone()
        if row is None:
            raise QueueError(tr("Unknown task ID {id}", "タスク ID {id} が見つかりません", id=args.id))
        print(f"{row['id']}  {status_label(row['status'])}  {row['name']}\nProject  {row['project']}")
        if row["idea_id"] is not None:
            print(f"Original idea  {row['idea_id']}  {row['idea_title']}\n\nAbstract\n{row['abstract']}")
        print(f"\nPlan\n{row['prompt']}")
        if row["implementation_model"]:
            print(f"\nImplementation model  {row['implementation_model']}")
        if row["revision_of"] is not None:
            print(f"\nRevises ToDo  {row['revision_of']}")
        return
    if action in ("ls", "list"):
        if get_setting(db, "enabled") != "1":
            print(tr("Automatic execution paused.", "自動実行は一時停止中です。"))
        if error := get_setting(db, "nextcloud_error"):
            print(error)
        section = "ToDo"
        print(section)
        for row in db.execute(
            """SELECT t.*, p.name AS project_name, r.writeback, r.blocked FROM tasks t
               JOIN projects p ON p.id = t.project_id LEFT JOIN remote_tasks r ON r.task_id = t.id
               WHERE t.status NOT IN ('REMOVED', 'SUPERSEDED')
               ORDER BY (t.status IN ('DONE', 'ABORTED', 'WAITING_FOR_INPUT')), t.priority, t.id"""
        ):
            if row["status"] in OUTCOMES and section == "ToDo":
                section = "Done / Abort / Waiting response"
                print(section)
            print(f"{row['id']}\t{row['project_name']}\t{status_label(row['status'])}\t{row['name']}")
            if row["status"] not in ("DONE", "QUEUED"):
                attempt = latest_attempt(db, row["id"])
                if attempt and attempt["reason"]:
                    print(f"  {attempt['reason']}")
                if attempt and attempt["retry_at"]:
                    retry = datetime.fromtimestamp(attempt["retry_at"]).astimezone().isoformat()
                    print("  " + tr("Retry after {time}", "再試行予定 {time}", time=retry))
                if row["status"] == "AUTH_REQUIRED":
                    print("  " + tr("Run codex login.", "codex login を実行してください。"))
            if row["writeback"] == "conflict":
                print("  " + tr("Nextcloud completion conflict. Run cq nextcloud resolve {id}.",
                                "Nextcloud で完了状態が競合しています。cq nextcloud resolve {id} を実行してください。",
                                id=row["id"]))
            if row["blocked"] and row["status"] in ("QUEUED", "RESUME_QUEUED"):
                print("  " + tr("Waiting for Nextcloud to confirm the task location.", "Nextcloud でタスクの所在を確認中です。"))
        return

def manage_idea(db, args):
    action = args.idea_action
    if action == "plan":
        ids = plan_idea(db, args.id)
        print(tr("Added ToDo {ids}.", "ToDo {ids} を追加しました。", ids=", ".join(map(str, ids))))
        wake(db)
        return
    if action in ("ls", "list", "show"):
        if action == "show":
            by_id(db, "ideas", args.id)
        planned = getattr(args, "planned", False)
        for row in db.execute(
            """SELECT i.*, p.name AS project FROM ideas i JOIN projects p ON p.id = i.project_id
               WHERE CASE WHEN ? IS NULL THEN (i.planned_at IS NOT NULL) = ? ELSE i.id = ? END
               ORDER BY i.id""",
            (getattr(args, "id", None), planned, getattr(args, "id", None)),
        ):
            state = tr("Planned", "計画済み") if row["planned_at"] else tr("Unplanned", "未計画")
            print(f"{row['id']}\t{row['project']}\t{state}\t{row['title']}")
            if action == "show":
                print(f"Planned  {'yes' if row['planned_at'] else 'no'}")
                if row["planned_at"]:
                    print(f"Planned at  {row['planned_at']} UTC")
                if row["detail"]:
                    print(row["detail"])
                if row["planning_error"]:
                    print(row["planning_error"])
                for task in db.execute("SELECT id, name FROM tasks WHERE idea_id = ? AND status NOT IN ('REMOVED', 'SUPERSEDED') ORDER BY id", (row["id"],)):
                    print(f"ToDo {task['id']}  {task['name']}")
        return
    with write(db):
        values = changes(args, "title", "detail") if action in ("add", "edit") else {}
        if "title" in values:
            values["title"] = values["title"].strip()
            if not values["title"] or len(values["title"].splitlines()) != 1:
                raise QueueError(tr("Enter a single-line title.", "タイトルを1行で入力してください。"))
        if action == "add":
            project = project_for_idea(db, args)
            cursor = db.execute("INSERT INTO ideas(project_id, title, detail) VALUES (?, ?, ?)",
                                (project["id"], values["title"], values.get("detail", "")))
            idea_id = cursor.lastrowid
        else:
            idea_id = args.id
            if by_id(db, "ideas", idea_id)["planned_at"]:
                raise QueueError(tr("This idea already has ToDos.", "この Idea にはすでに ToDo があります。"))
            if action == "edit":
                update(db, "ideas", idea_id, {**values, "planning_error": None})
            else:
                db.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))
    print(f"Idea {idea_id}")
    if action in ("add", "edit") and settings(db)["idea_planning"] == "auto":
        wake(db)


def configure(db, args):
    values = changes(args, *DEFAULTS)
    if args.base == "unset":
        values["base"] = ""
    with write(db):
        db.executemany(
            """INSERT INTO settings(key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            values.items(),
        )
        current = settings(db)
    language.set(current["language"])
    for key, value in current.items():
        print(f"{key.replace('_', '-')}: {value or tr('unset', '未設定')}")
    if values.get("idea_planning") == "auto":
        wake(db)


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(tr("must be a positive integer", "正の整数を指定してください"))
    return number


def task_commands(subparsers):
    show = subparsers.add_parser("show", help=tr("Show a ToDo and its plan", "ToDo と実装計画を表示"))
    show.add_argument("id", type=positive)
    subparsers.add_parser("ls", aliases=["list"], help=tr("List tasks", "タスクを一覧表示"))
    revise = subparsers.add_parser("revise", help=tr("Ask the planning model to revise a ToDo", "計画モデルに ToDo の修正を依頼"))
    revise.add_argument("id", type=positive)
    revise.add_argument("instruction")
    revise.add_argument("--context-file", type=Path, help=tr("Read additional history from a UTF-8 file", "追加の履歴を UTF-8 ファイルから読み込む"))


def idea_commands(subparsers):
    add_idea = subparsers.add_parser("add", help=tr("Register an idea", "Idea を登録"))
    add_idea.set_defaults(idea_action="add")
    add_idea.add_argument("title")
    add_idea.add_argument("--detail")
    target = add_idea.add_mutually_exclusive_group()
    target.add_argument("--project")
    target.add_argument("--project-id", type=positive)
    list_ideas = subparsers.add_parser("ls", aliases=["list"], help=tr("List unplanned ideas", "未計画の Idea を一覧表示"))
    list_ideas.set_defaults(idea_action="ls")
    list_ideas.add_argument("--planned", action="store_true", help=tr("List planned ideas", "計画済みの Idea を一覧表示"))
    for action, description in (
        ("edit", tr("Edit an unplanned idea", "未計画の Idea を編集")),
        ("rm", tr("Remove an unplanned idea", "未計画の Idea を削除")),
    ):
        item = subparsers.add_parser(action, aliases=["remove"] if action == "rm" else [], help=description)
        item.set_defaults(idea_action=action)
        item.add_argument("id", type=positive)
        if action == "edit":
            item.add_argument("--title")
            item.add_argument("--detail")


def model_arguments(parser, project=False):
    suffix = tr(" (use inherit to use the global setting)", "（inherit で全体設定を継承）") if project else ""
    parser.add_argument("--planner-model", help=tr("Idea planning model", "Idea の計画モデル") + suffix)
    parser.add_argument("--implementation-model", help=tr("Default ToDo implementation model", "ToDo の既定の実装モデル") + suffix)
    parser.add_argument("--allowed-models", type=allowed_models,
                        help=tr("Planner-selectable models, comma-separated, lowest capability first",
                                "計画モデルが選べる実装モデルを性能の低い順にカンマ区切りで指定") + suffix)


def allowed_models(value):
    if not value.strip():
        return ""
    models = [model.strip() for model in value.split(",")]
    if not all(models) or len(set(models)) != len(models):
        raise argparse.ArgumentTypeError(tr("Enter distinct model names separated by commas.",
                                           "異なるモデル名をカンマ区切りで指定してください。"))
    return ",".join(models)


def project_arguments(parser, editing=False):
    model_arguments(parser, project=True)
    parser.add_argument("--name", help=tr("Project name", "プロジェクト名"))
    parser.add_argument("--path", help=tr("Repository path", "リポジトリのパス"))
    parser.add_argument("--prompt", help=tr("Project instructions", "プロジェクトの共通指示"))
    parser.add_argument("--branch", choices=(*BRANCH_MODES, "") if editing else BRANCH_MODES,
                        help=tr("Branch mode", "ブランチ運用"))
    parser.add_argument("--base", help=tr("Base branch", "開始元ブランチ"))


def make_parser():
    parser = ArgumentParser(prog="cq", description=tr("Manage the local Codex task queue.", "ローカルの Codex タスクキューを管理します。"))
    commands = parser.add_subparsers(dest="command")
    idea_commands(commands)
    commands.add_parser("status", help=tr("Show queue status and tasks awaiting a reply", "キューの状況と返信待ちのタスクを表示"))
    task = commands.add_parser("task", aliases=["todo"], help=tr("Manage ToDos", "ToDo を管理"))
    task_commands(task.add_subparsers(dest="command", required=True))
    idea = commands.add_parser("idea", help=tr("Register ideas and plan ToDos", "Idea の登録と ToDo の計画"))
    idea_actions = idea.add_subparsers(dest="idea_action", required=True)
    idea_commands(idea_actions)
    for action, description in (
        ("show", tr("Show an idea and its ToDos", "Idea と子 ToDo を表示")),
        ("plan", tr("Generate and queue ToDos for an idea", "Idea から ToDo を生成してキューに登録")),
    ):
        item = idea_actions.add_parser(action, help=description)
        item.add_argument("id", type=positive)
    project = commands.add_parser("project", help=tr("Manage projects", "プロジェクトを管理"))
    project_arguments(project)
    actions = project.add_subparsers(dest="project_action")
    project_arguments(actions.add_parser("add", help=tr("Register a Git repository", "Git リポジトリを登録")))
    actions.add_parser("ls", aliases=["list"], help=tr("List projects", "プロジェクトを一覧表示"))
    edit = actions.add_parser("edit", help=tr("Edit a project", "プロジェクトを編集"))
    edit.add_argument("id", type=positive)
    project_arguments(edit, editing=True)
    remove = actions.add_parser("rm", aliases=["remove"], help=tr("Remove a project", "プロジェクトを削除"))
    remove.add_argument("id", type=positive)

    config = commands.add_parser("config", aliases=["settings"], help=tr("Manage queue defaults", "キューの設定を管理"))
    model_arguments(config)
    config.add_argument("--idea-planning", choices=("auto", "manual"),
                        help=tr("When to plan new ideas", "Idea を自動で計画するか手動で計画するか"))
    config.add_argument("--parallel", type=positive, help=tr("Maximum concurrent tasks", "同時に実行するタスク数の上限"))
    config.add_argument("--priority", type=int, choices=range(1, 10), metavar="1-9", help=tr("Default task priority", "タスクの既定の優先度"))
    config.add_argument("--branch", choices=BRANCH_MODES, help=tr("Default branch mode", "既定のブランチ運用"))
    config.add_argument("--base", help=tr("Default base branch", "既定の開始元ブランチ"))
    config.add_argument("--on-abort", choices=["pause", "continue"], help=tr("Queue behavior after an abort", "タスク中止後のキューの動作"))
    config.add_argument("--sync", choices=["auto", "manual"], help=tr("Nextcloud sync mode", "Nextcloud の同期方式"))
    config.add_argument("--language", choices=LANGUAGES, help=tr(
        "Display language (en: English, ja: Japanese)",
        "表示言語（en: 英語、ja: 日本語）",
    ))
    for name, aliases, help_text in (
        ("start", ["resume"], tr("Enable automatic execution and request the worker", "自動実行を有効にしてワーカーを起動")),
        ("pause", [], tr("Pause the queue and wait for running tasks", "キューを一時停止し、実行中のタスクを待つ")),
        ("stop", [], tr("Stop the queue and interrupt running tasks", "キューを停止し、実行中のタスクを中断")),
        ("setup", [], tr("Authenticate Codex CLI and register the worker", "Codex CLI を認証し、ワーカーを登録")),
    ):
        commands.add_parser(name, aliases=aliases, help=help_text)
    schedule = commands.add_parser("schedule", help=tr("Register a Windows scheduled task", "Windows の定期タスクを登録"))
    schedule.add_argument("--every-minutes", type=positive, required=True, help=tr("Interval in minutes", "実行間隔（分）"))
    for name, help_text in (
        ("retry", tr("Retry a task", "タスクを再試行")),
        ("reply", tr("Answer a task's question", "タスクからの質問に回答")),
        ("abort", tr("Abort a task", "タスクを中止")),
        ("logs", tr("Show task logs", "タスクのログを表示")),
        ("done", tr("Mark a task as complete", "タスクを完了にする")),
    ):
        action = commands.add_parser(name, help=help_text)
        action.add_argument("id", type=positive)
        if name == "retry":
            action.add_argument("--fresh", action="store_true", help=tr("Start a new attempt", "新しい試行を開始"))
        elif name == "reply":
            action.add_argument("answer")
        elif name == "done":
            action.add_argument("--note", help=tr("Completion note", "完了メモ"))
    help_command = commands.add_parser("help", help=tr("Show command help", "コマンドのヘルプを表示"))
    help_command.add_argument("topic", nargs="*", metavar="COMMAND",
                              help=tr("Command or subcommand", "コマンドまたはサブコマンド"))
    worker = commands.add_parser("_worker")
    worker.add_argument("--scheduled", action="store_true")
    worker.add_argument("--data-dir", help=argparse.SUPPRESS)
    from .nextcloud import add_commands
    add_commands(commands)
    return parser


def command_help_rows(parser, prefix=""):
    commands, _ = parser_candidates(parser)
    for name, (child, description) in commands.items():
        path = prefix + name
        yield path, description or ""
        yield from command_help_rows(child, path + " ")


def show_command_help(parser, topic):
    if not topic:
        print(tr("Commands", "コマンド一覧"))
        for command, description in command_help_rows(parser):
            print_wrapped(f"  {command}: {description}")
        print_wrapped(tr("Use cq help COMMAND for options.", "オプションを確認するには cq help COMMAND を実行してください。"))
        return
    target = parser
    for name in topic:
        commands, _ = parser_candidates(target)
        if name not in commands:
            parser.error(tr("Unknown command: {command}", "不明なコマンド：{command}", command=" ".join(topic)))
        target, _ = commands[name]
    target.print_help()


def main(argv=None, interactive=True):
    token = language.set(saved_language())
    try:
        return run_cli(argv, interactive)
    finally:
        language.reset(token)


def run_cli(argv, interactive):
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command == "help":
        show_command_help(parser, args.topic)
        return 0
    if args.command is None:
        import sys
        if interactive and sys.stdin.isatty() and sys.stdout.isatty():
            from .terminal import screen
            return screen()
        parser.print_help()
        return 0
    try:
        if args.command == "_worker":
            if args.data_dir:
                os.environ["CQ_WORKER_DATA_DIR"] = args.data_dir
                language.set(saved_language())
            run_worker(args.scheduled)
            return 0
        with open_queue() as db:
            if args.command == "project":
                manage_project(db, args)
            elif getattr(args, "idea_action", None):
                manage_idea(db, args)
            elif args.command == "status":
                print_wrapped(queue_status(db))
            elif args.command in ("config", "settings"):
                configure(db, args)
            elif args.command in ("start", "resume", "pause", "stop"):
                queue_control(db, args.command)
            elif args.command == "setup":
                setup(db)
            elif args.command == "schedule":
                register_schedule(db, args.every_minutes)
            elif args.command in ("retry", "reply", "abort", "logs", "done"):
                task_action(db, args)
            elif args.command in ("nextcloud", "sync"):
                from .nextcloud import dispatch
                dispatch(db, args)
            else:
                manage_task(db, args)
    except sqlite3.IntegrityError as error:
        if "projects.path_key" in str(error):
            parser.exit(1, "cq: " + tr("That Git repository is already registered.", "その Git リポジトリは登録済みです。") + "\n")
        parser.exit(1, f"cq: {error}\n")
    except (QueueError, OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError, UnicodeError) as error:
        parser.exit(1, f"cq: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
