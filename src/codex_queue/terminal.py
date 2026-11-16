import argparse
import codecs
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from collections import deque
from functools import cache, partial
from io import StringIO
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from typing import NamedTuple
import unicodedata

from .arguments import ArgumentParser, parser_candidates
from .cli import BRANCH_MODES, MODEL_SETTINGS, QueueError, allowed_models, git_root, main, make_parser, model_arguments
from .editor import edit_text
from .language import language, status_label, tr
from .runtime import codex_models
from .status import queue_status
from .store import DEFAULTS, get_setting, open_queue, saved_language, settings
from .text_layout import fit, print_wrapped, terminal_width, wrap_rows
from .windows_console import ConsoleInput
from .worker import latest_attempt


class PosixInput:
    def __init__(self):
        import sys

        self.decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")(errors="replace")

    def read(self):
        import sys
        import termios
        import tty
        from select import select

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd, termios.TCSANOW)
            mode = termios.tcgetattr(fd)
            mode[0] &= ~termios.IXON
            termios.tcsetattr(fd, termios.TCSANOW, mode)
            if not select([fd], [], [], 0.03)[0]:
                return None
            byte = os.read(fd, 1)
            if not byte:
                raise EOFError
            if byte == b"\x1b":
                suffix = b""
                while len(suffix) < 8 and select([fd], [], [], 0.08)[0]:
                    following = os.read(fd, 1)
                    if not following:
                        break
                    suffix += following
                    if suffix[0] != ord("[") or (len(suffix) > 1 and 0x40 <= suffix[-1] <= 0x7E):
                        break
                return {
                    b"": "\x1b", b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left",
                    b"[H": "home", b"[F": "end", b"[3~": "delete",
                    b"[5~": "pageup", b"[6~": "pagedown",
                    b"[13;5u": "ctrl+enter", b"[27;5;13~": "ctrl+enter",
                }.get(suffix, "")
            return self.decoder.decode(byte) or None
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, saved)


def prompt_input(parser, message, **kwargs):
    if os.name == "nt":
        return windows_input(parser, message, **kwargs)
    return read_prompt(parser, message, True, console=PosixInput(), **kwargs)


def clear():
    if os.name == "nt":
        os.system("cls")
    else:
        print("\033[2J\033[H", end="")


def choose(title, options, default=0, *, ordered=None):
    if not options:
        print(tr("No available choices.", "選択できる項目がありません。"))
        return None
    output = virtual_terminal() if os.name == "nt" else nullcontext(True)
    with output as vt:
        return choose_options(title, options, default, vt, ordered=ordered)


def choose_options(title, options, default, vt, *, ordered=None):
    index = default
    console = ConsoleInput() if os.name == "nt" else PosixInput()
    hint = tr("Up/Down select  Enter accept  Esc cancel", "上下キーで選択  Enter で確定  Esc で戻る")
    if ordered is not None:
        hint = tr("Up/Down move  Enter toggle  Ctrl+Enter confirm  Esc cancel",
                  "上下キーで移動  Enter で選択・解除  Ctrl+Enter で決定  Esc で戻る")
    while True:
        size = shutil.get_terminal_size()
        title_rows = len(list(wrap_rows(title, max(1, size.columns - 1))))
        capacity = max(1, size.lines - title_rows - 4)
        first = min(max(0, index - capacity // 2), max(0, len(options) - capacity))
        priorities = {value: i for i, value in enumerate(ordered or (), 1)}
        visible = []
        for i, (label, value) in enumerate(options[first:first + capacity], first):
            if priorities and value in priorities:
                label = f"{priorities[value]} {label}"
            visible.append(fit(("> " if i == index else "  ") + label, max(1, size.columns - 1)))
        draw_prompt(title, "\n".join(visible), options[index][0], [], 0, size, 0, vt, hint=hint)
        while (key := console.read()) is None:
            time.sleep(0.03)
            if shutil.get_terminal_size() != size:
                break
        if key == "up":
            index = (index - 1) % len(options)
        elif key == "down":
            index = (index + 1) % len(options)
        elif key == "ctrl+enter" and ordered is not None:
            return ordered
        elif key in ("\n", "\r"):
            value = options[index][1]
            if ordered is None or value is FREE_ENTRY:
                return value
            if value is APPLY_SELECTION:
                return ordered
            if value in ordered:
                ordered.remove(value)
            elif value is INHERIT_MODELS:
                ordered[:] = [value]
            else:
                if INHERIT_MODELS in ordered:
                    ordered.remove(INHERIT_MODELS)
                ordered.append(value)
        elif key in ("\x1b", "\x04"):
            return None
        elif key == "\x03":
            raise KeyboardInterrupt
        elif key == "\x1a":
            raise EOFError


def tokens(line):
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if os.name == "nt":
        lexer.escape = ""
    return list(lexer)


def command_text(line):
    return line.lstrip().removeprefix("/")


class Suggestion(NamedTuple):
    name: str
    help: str = ""
    insertion: str | None = None


COMPLETION_WORD = re.compile(r"""(?:[^\s'"]+|"[^"]*(?:"|$)|'[^']*(?:'|$))+""")
PROJECT_BASE_VALUES = ("inherit", "unset")


def path_suggestions(prefix, directories_only, quote=True):
    folder, name = os.path.split(prefix)
    try:
        with os.scandir(folder or ".") as entries:
            matches = []
            for entry in entries:
                if not os.path.normcase(entry.name).startswith(os.path.normcase(name)):
                    continue
                directory = entry.is_dir()
                if directory or (not directories_only and entry.is_file()):
                    matches.append((entry.name, directory))
    except OSError:
        return []
    candidates = []
    for name, directory in sorted(matches, key=lambda match: match[0].casefold()):
        display = name + (os.sep if directory else "")
        path = os.path.join(folder, display)
        if not quote:
            insertion = path
        elif os.name == "nt":
            insertion = f'"{path}"' if any(char.isspace() or char == "'" for char in path) else path
        else:
            insertion = shlex.quote(path)
        label = tr("Directory", "フォルダー") if directory else tr("File", "ファイル")
        if quote and not directory:
            insertion += " "
        candidates.append(Suggestion(display, label, insertion))
    return candidates


def value_suggestions(action, prefix, project):
    try:
        words = tokens(prefix)
    except ValueError:
        # A quoted value can still be in the middle of being typed.
        words = [prefix[1:]] if prefix.startswith(("'", '"')) else []
    value = words[0] if words else prefix
    if action.dest in ("path", "context_file"):
        return path_suggestions(value, directories_only=action.dest == "path")
    choices = action.choices
    if action.dest == "base":
        choices = PROJECT_BASE_VALUES if project else ("unset",)
    return [
        Suggestion(str(choice)) for choice in choices or ()
        if str(choice).startswith(value)
    ]


def completion(parser, line):
    """Return the replaceable suffix and its candidates for the current input."""
    if not line.strip():
        return len(line), []
    match = next((word for word in COMPLETION_WORD.finditer(line) if word.end() == len(line)), None)
    start = match.start() if match else len(line)
    prefix = line[start:]
    if not line[:start].strip() and prefix.startswith("/"):
        start += 1
        prefix = prefix[1:]
    try:
        words = tokens(command_text(line[:start]))
    except ValueError:
        return start, []
    help_topic = words[:1] == ["help"] and not prefix.startswith("-")
    if help_topic:
        words = words[1:]
    positional = False
    value_action = None
    for word in words:
        if value_action is not None:
            value_action = None
            continue
        if word == "--":
            return start, []
        commands, options = parser_candidates(parser)
        flag, equals, _ = word.partition("=")
        if flag in options:
            if not equals and options[flag].nargs != 0:
                value_action = options[flag]
        elif not positional and word in commands:
            parser, _ = commands[word]
        else:
            positional = True

    commands, options = parser_candidates(parser)
    if not help_topic:
        project = words[:1] == ["project"]
        if value_action is not None:
            return start, value_suggestions(value_action, prefix, project)
        flag, equals, value = prefix.partition("=")
        if equals and flag in options and options[flag].nargs != 0:
            return start + len(flag) + 1, value_suggestions(options[flag], value, project)
    if any(char in prefix for char in ("'", '"')):
        return start, []
    if prefix.startswith("-"):
        choices = {name: action.help for name, action in options.items()}
    else:
        choices = {} if positional else {name: help_text for name, (_, help_text) in commands.items()}
        if not words and not help_topic:
            choices["q"] = tr("Close the interactive screen", "対話画面を閉じる")
    return start, [
        Suggestion(name, help_text or "") for name, help_text in choices.items() if name.startswith(prefix)
    ]


def field(label, default=""):
    return edit_field("", label, default)


def form_suggestions(key, prefix):
    if not prefix:
        return []
    if key in ("path", "context_file"):
        return path_suggestions(prefix, directories_only=key == "path", quote=False)
    choices = {
        "branch": BRANCH_MODES,
        "base": PROJECT_BASE_VALUES,
    }.get(key, ())
    return [Suggestion(value, insertion=value) for value in choices if value.startswith(prefix)]


MULTILINE_FIELDS = {"prompt", "detail", "answer", "instruction", "note"}


def edit_field(key, label, current):
    def complete(prefix):
        return 0, form_suggestions(key, prefix)

    heading = label + "\n" + tr("Ctrl+U clear", "Ctrl+U で全消去")
    kwargs = {"show_queue": False, "complete": complete, "initial": current}
    if key in MULTILINE_FIELDS:
        heading = label + "\n" + tr(
            "Ctrl+Enter newline  |  Ctrl+E open editor  |  Ctrl+U clear",
            "Ctrl+Enter で改行  |  Ctrl+E でエディタ  |  Ctrl+U で全消去",
        )
        kwargs["editor"] = label.splitlines()[-1]
    return prompt_input(None, heading, **kwargs)


def choose_yes_no(label, default=False):
    return choose(label, [(tr("No", "いいえ"), False), (tr("Yes", "はい"), True)], int(default))


FREE_ENTRY = object()
APPLY_SELECTION = object()
INHERIT_MODELS = object()


def branch_label(value):
    return {
        "per-idea": tr("One branch per Idea", "Idea ごとにまとめる"),
        "per-task": tr("Separate branches per task", "タスクごとに分割"),
        "auto": tr("Review Codex recommendation", "Codex の提案を確認する"),
        "current": tr("Use current branch", "そのまま使う"),
    }[value]


def inherited_label():
    return tr("Inherit global setting", "全体設定を継承")


def unset_label():
    return tr("Unset", "未設定")


def free_entry_label():
    return tr("Enter manually", "自由記述")


def branch_mode(label, value, project):
    options = [(branch_label(mode), mode) for mode in BRANCH_MODES]
    if project:
        options.insert(0, (inherited_label(), ""))
    return choose_value(label, options, value)


def branch_names(repository):
    try:
        result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:lstrip=2)%09%(symref)", "refs/heads"],
            cwd=repository, capture_output=True, encoding="utf-8", timeout=3,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    return [name for line in result.stdout.splitlines() if "\t" in line
            for name, target in [line.split("\t", 1)] if name and not target]


def base_branch(label, value, repository, project):
    unset = "unset" if project else ""
    value = unset if project and value == "" else value
    options = [(unset_label(), unset)]
    if project:
        options.insert(0, (inherited_label(), "inherit"))
    known = dict.fromkeys([*branch_names(repository), value])
    reserved = {None, "", "unset", "inherit"} if project else {None, ""}
    options.extend((name, name) for name in known if name not in reserved)
    options.append((free_entry_label(), FREE_ENTRY))
    selection = choose_value(label, options, value)
    if selection is FREE_ENTRY:
        return edit_field("base", label, value if value not in reserved else "")
    return selection


def model_setting(key, label, value, available, project):
    models = dict(available)
    if key != "allowed_models":
        options = [(unset_label(), "")]
        if project:
            options.insert(0, (inherited_label(), "inherit"))
        if value and (value != "inherit" or not project) and value not in models:
            models[value] = value
        options.extend((name if model == name else f"{name} ({model})", model) for model, name in models.items())
        options.append((free_entry_label(), FREE_ENTRY))
        selection = choose_value(label, options, value)
        if selection is FREE_ENTRY:
            return edit_field(key, label, "" if value in (None, "inherit") else value)
        return selection

    order = value.split(",") if value else []
    if project and value == "inherit":
        order = [INHERIT_MODELS]
    while True:
        for model in order:
            if model is not INHERIT_MODELS:
                models.setdefault(model, model)
        options = [(inherited_label(), INHERIT_MODELS)] if project else []
        options.extend((name if name == model else f"{name} ({model})", model) for model, name in models.items())
        options.extend([(tr("Confirm", "決定"), APPLY_SELECTION), (free_entry_label(), FREE_ENTRY)])
        selection = choose(label, options, ordered=order)
        if selection is None:
            return None
        if selection is not FREE_ENTRY:
            return "inherit" if selection == [INHERIT_MODELS] else ",".join(selection)
        added = validated_input(label, "", lambda title, initial: edit_field(key, title, initial), allowed_models)
        if added:
            if added == "inherit" and project:
                order[:] = [INHERIT_MODELS]
            else:
                order[:] = list(dict.fromkeys([*(model for model in order if model is not INHERIT_MODELS), *added.split(",")]))


def confirmed(argv, prompted):
    if prompted and not choose_yes_no(tr("Save changes?", "変更を保存しますか？")):
        return None
    return argv


def screen_input(prompt):
    rows = list(wrap_rows(prompt, max(1, terminal_width() - 1)))
    if not rows:
        return input()
    if len(rows) > 1:
        print("\n".join(rows[:-1]))
    return input(rows[-1] + " ")


def option(command, flag, value):
    return [*command, f"{flag}={value}"]


def choose_value(title, options, supplied=None, default=0):
    if supplied is not None:
        for index, (_, value) in enumerate(options):
            if value == supplied:
                return choose(title, options, index)
        options = [(str(supplied), supplied), *options]
        default = 0
    return choose(title, options, default)


def option_preview(key, value):
    if value is None:
        return tr("default", "既定値")
    if key == "branch" and value in BRANCH_MODES:
        return branch_label(value)
    if (key in ("base", *MODEL_SETTINGS) and value == "inherit") or (key == "branch" and value == ""):
        return inherited_label()
    if (key == "base" and value in ("unset", "")) or (key in MODEL_SETTINGS and value == ""):
        return unset_label()
    return " ".join(str(value).split()) or "—"


def edit_options(fields, values, edit, *, defaults=None, current=None):
    """Keep supplied values and leave untouched options to the command defaults."""
    if not fields:
        return values
    defaults = defaults or {}
    current = defaults if current is None else current
    while True:
        options = [(tr("Continue", "次へ"), "")]
        for key, label in fields:
            value = values.get(key)
            default = defaults.get(key)
            if value is None:
                value = current.get(key)
            preview = option_preview(key, value)
            if default is not None and str(value) == str(default):
                preview += f" ({tr('default', '既定値')})"
            options.append((f"{label}: {preview}", key))
        key = choose(tr("Choose an option to edit", "変更するオプションを選択"), options)
        if key is None:
            return None
        if key == "":
            return values
        value = edit(key, dict(fields)[key], values.get(key))
        if value is not None:
            values[key] = value


def validated_input(label, initial, read, validate):
    heading = label
    while True:
        value = read(heading, initial)
        if value is None:
            return None
        try:
            return validate(value)
        except (QueueError, OSError, ValueError, argparse.ArgumentTypeError) as error:
            initial = value
            heading = f"{label}\n{error}"


def single_line(value, message):
    if not value.strip() or len(value.strip().splitlines()) != 1:
        raise ValueError(message)
    return value.strip()


def repository_path(value):
    if not value.strip():
        raise ValueError(tr("Repository path is required.", "リポジトリのパスは必須です。"))
    if not Path(value).is_dir():
        raise ValueError(tr("Directory does not exist: {path}", "フォルダーが存在しません：{path}", path=value))
    return git_root(value)


def task_options(db, statuses=None, excluded=("RUNNING", "REMOVED", "SUPERSEDED")):
    return [
        (f"{row['id']}  {row['project']}  {row['name']}  {status_label(row['status'])}", row["id"])
        for row in db.execute(
            """SELECT t.*, p.name AS project FROM tasks t JOIN projects p ON p.id=t.project_id
               ORDER BY t.priority,t.id"""
        )
        if row["status"] not in excluded and (statuses is None or row["status"] in statuses)
    ]


def project_options(db):
    return [(f"{r['name']}  {r['path']}", r["id"]) for r in db.execute(
        "SELECT * FROM projects WHERE retired=0 ORDER BY id"
    )]


def repo_here():
    try:
        return git_root(".")
    except (QueueError, OSError, ValueError):
        return None


def fill_add(db, argv):
    parser = ArgumentParser(add_help=False)
    parser.add_argument("title", nargs="?")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--project")
    target.add_argument("--project-id", type=int)
    parser.add_argument("--detail")
    args = parser.parse_args(argv[1:])
    title = validated_input(
        tr("Idea title", "Idea のタイトル"), args.title or "", field,
        lambda value: single_line(value, tr("Enter a single-line title.", "タイトルを1行で入力してください。")),
    )
    if title is None:
        return None
    command = ["add"]
    options = project_options(db)
    root = repo_here()
    default = 0
    if root:
        current = db.execute(
            "SELECT id FROM projects WHERE path_key=? AND retired=0", (os.path.normcase(str(root)),),
        ).fetchone()
        if current:
            default = next(i for i, (_, key) in enumerate(options) if key == current["id"])
        else:
            options.insert(0, (f"{root.name}  {root} " + tr("(register)", "（新規登録）"), "current"))
    supplied = args.project_id
    label = tr("Project", "プロジェクト")
    if args.project is not None:
        matches = [row["id"] for row in db.execute(
            "SELECT id FROM projects WHERE retired=0 AND name=?", (args.project,),
        )]
        supplied = matches[0] if len(matches) == 1 else None
        if supplied is None:
            label += "\n" + tr("Choose a registered project for {name}.", "{name} に対応する登録済みプロジェクトを選んでください。", name=args.project)
    if supplied is not None and not any(key == supplied for _, key in options):
        label += "\n" + tr("Unknown project ID {id}.", "プロジェクト ID {id} が見つかりません。", id=supplied)
        supplied = None
    selected = choose_value(label, options, supplied, default)
    if selected is None:
        return None
    if selected != "current":
        command = option(command, "--project-id", selected)
    values = edit_options(
        [("detail", tr("Idea details", "Idea の詳細"))], {"detail": args.detail},
        lambda key, label, value: edit_field(key, label, value or ""),
        defaults={"detail": ""},
    )
    if values is None:
        return None
    if values["detail"] is not None:
        command = option(command, "--detail", values["detail"])
    return confirmed(command + ["--", title], True)


def idea_options(db, include_planned=False):
    return [
        (f"{row['id']}  {row['project']}  {row['title']}", row["id"])
        for row in db.execute(
            """SELECT i.*, p.name AS project FROM ideas i JOIN projects p ON p.id = i.project_id
               WHERE ? OR i.planned_at IS NULL ORDER BY i.id""",
            (include_planned,),
        )
    ]


def fill_idea(db, argv):
    action = argv[0]
    parser = ArgumentParser(prog=f"cq {action}", add_help=False)
    parser.add_argument("id", nargs="?", type=int)
    if action == "edit":
        parser.add_argument("--title")
        parser.add_argument("--detail")
    args = parser.parse_args(argv[1:])
    options = idea_options(db)
    target = choose_value("Idea", options, args.id)
    if target is None:
        return None
    command = [action, str(target)]
    if action != "edit":
        return command
    idea = db.execute("SELECT * FROM ideas WHERE id = ?", (target,)).fetchone()
    if idea is None or idea["planned_at"]:
        return command
    fields = [("title", tr("Idea title", "Idea のタイトル")), ("detail", tr("Idea details", "Idea の詳細"))]
    values = {key: idea[key] if getattr(args, key) is None else getattr(args, key) for key, _ in fields}

    def edit(key, label, value):
        if key == "title":
            return validated_input(label, value, field, lambda text: single_line(
                text, tr("Enter a single-line title.", "タイトルを1行で入力してください。"),
            ))
        return edit_field(key, label, value)

    values = edit_options(fields, values, edit, defaults={"detail": ""})
    if values is None:
        return None
    for key, value in values.items():
        if value != idea[key]:
            command = option(command, f"--{key}", value)
    return confirmed(command, True) if len(command) > 2 else None


def fill_reply(db, argv):
    parser = ArgumentParser(prog="cq reply", add_help=False)
    parser.add_argument("id", nargs="?", type=int)
    parser.add_argument("answer", nargs="?")
    args = parser.parse_args(argv[1:])
    target = choose_value(tr("Task", "タスク"), task_options(db, {"WAITING_FOR_INPUT"}), args.id)
    if target is None:
        return None
    answer = args.answer
    attempt = latest_attempt(db, target)
    task = db.execute("SELECT status FROM tasks WHERE id = ?", (target,)).fetchone()
    if not (attempt and task and task["status"] == "WAITING_FOR_INPUT"):
        return ["reply", str(target), "--", answer or ""]
    label = "\n".join(filter(None, (attempt["reason"], tr("Answer", "回答"))))
    answer = edit_field("answer", label, answer or "")
    if answer is None:
        return None
    return confirmed(["reply", str(target), "--", answer], True)


def fill_project(db, argv):
    action = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else None
    command = argv[:2] if action else argv[:1]
    parser = ArgumentParser(prog="cq " + " ".join(command), add_help=False)
    if action in ("edit", "rm", "remove"):
        parser.add_argument("id", nargs="?", type=int)
    fields = (
        ("path", tr("Repository path", "リポジトリのパス")),
        ("name", tr("Project name", "プロジェクト名")),
        ("prompt", tr("Project instructions", "プロジェクトの共通指示")),
        ("branch", tr("Branch mode", "ブランチ運用")),
        ("base", tr("Base branch", "開始元ブランチ")),
    )
    if action in (None, "add", "edit"):
        for key, _ in fields:
            parser.add_argument(f"--{key}")
        model_arguments(parser, project=True)
        _, options = parser_candidates(parser)
        field_keys = {key for key, _ in fields}
        fields += tuple((argument.dest, argument.help) for argument in options.values()
                        if argument.dest not in field_keys)
        parser.add_argument("--nextcloud-sync", choices=("on", "off"))
        fields += (("nextcloud_sync", tr("Nextcloud board sync (on/off)", "Nextcloud ボード同期（on/off）")),)
    args = parser.parse_args(argv[len(command):])
    row = None
    if action in ("edit", "rm", "remove"):
        target = choose_value(tr("Project", "プロジェクト"), project_options(db), args.id)
        if target is None:
            return None
        command += [str(target)]
        if action in ("rm", "remove"):
            return command
        row = db.execute("SELECT * FROM projects WHERE id=? AND retired=0", (target,)).fetchone()
        if row is None:
            return command
    defaults = {"prompt": "", "branch": "", "base": "inherit", "nextcloud_sync": "on",
                **dict.fromkeys(MODEL_SETTINGS, "inherit")}
    current = dict(row) if row else {"path": ".", "name": "", **defaults}
    current["branch"] = current["branch"] or ""
    current.setdefault("nextcloud_sync", defaults["nextcloud_sync"])
    for key in ("base", *MODEL_SETTINGS):
        if current.get(key) is None:
            current[key] = "inherit"
    if current["base"] == "":
        current["base"] = "unset"
    labels = dict(fields)
    if action == "add":
        path = validated_input(
            labels["path"], args.path if args.path is not None else str(repo_here() or "."),
            lambda label, value: edit_field("path", label, value), repository_path,
        )
        if path is None:
            return None
        name = validated_input(
            labels["name"], args.name if args.name is not None else path.name,
            lambda label, value: edit_field("name", label, value), lambda value: single_line(
                value, tr("Enter a single-line project name.", "プロジェクト名を1行で入力してください。"),
            ),
        )
        if name is None:
            return None
        command = option(option(command, "--path", path), "--name", name)
        fields = [(key, label) for key, label in fields if key not in ("path", "name")]
    values = {key: getattr(args, key) if getattr(args, key) is not None else current[key] for key, _ in fields}
    models = cache(lambda: codex_models(db))

    def edit(key, label, value):
        if key == "branch":
            return branch_mode(label, value, project=True)
        if key == "base":
            return base_branch(label, value, path if action == "add" else values["path"], project=True)
        if key in MODEL_SETTINGS:
            return model_setting(key, label, value, models(), project=True)
        if key == "nextcloud_sync":
            return choose_value(label, [("on", "on"), ("off", "off")], value)
        if key == "path":
            return validated_input(label, value, lambda heading, initial: edit_field(key, heading, initial),
                                   lambda text: str(repository_path(text)))
        return edit_field(key, label, value or "")

    values = edit_options(fields, values, edit, defaults=defaults)
    if values is None:
        return None
    for key, value in values.items():
        if key == "branch" and value == "" and action != "edit":
            continue
        if value != current[key] or (action != "edit" and getattr(args, key) is not None):
            command = option(command, f"--{key.replace('_', '-')}", value)
    if action == "edit" and len(command) == 3:
        return None
    return confirmed(command, len(command) > 1)


TASK_STATUS_CHOICES = {
    "retry": {"NEEDS_REVIEW", "INTERRUPTED", "AUTH_REQUIRED", "RETRY_WAIT", "ABORTED"},
    "abort": {"NEEDS_REVIEW", "WAITING_FOR_INPUT"},
}
POSITIONAL_LABELS = {
    "instruction": ("Revision request", "修正指示"),
}


def argument_choices(db, command, argument):
    if argument.dest == "project_id":
        return tr("Project", "プロジェクト"), project_options(db)
    if argument.dest != "id":
        return None
    if command[0] == "idea":
        return "Idea", idea_options(db, include_planned=command[-1] == "show")
    action = command[-1]
    excluded = () if action in ("show", "logs", "resolve") else ("RUNNING", "REMOVED", "SUPERSEDED")
    if action == "done":
        excluded += ("DONE", "QUEUED")
    if action == "revise":
        excluded += ("RESUME_QUEUED",)
    options = task_options(db, TASK_STATUS_CHOICES.get(action), excluded)
    if action == "resolve":
        conflicts = {row["task_id"] for row in db.execute(
            "SELECT task_id FROM remote_tasks WHERE connection = ? AND writeback = 'conflict'",
            (get_setting(db, "nextcloud_connection"),),
        )}
        options = [(label, value) for label, value in options if value in conflicts]
    return tr("Task", "タスク"), options


def argument_label(argument):
    if argument.help and argument.help != argparse.SUPPRESS:
        return argument.help
    return tr(*POSITIONAL_LABELS.get(argument.dest, (argument.dest, argument.dest)))


def prompt_argument(db, command, argument, supplied, current=None, heading=None):
    target = argument_choices(db, command, argument)
    if target is not None:
        label, choices = target
        return choose_value(heading or label, choices, supplied)
    label = heading or argument_label(argument)
    initial = supplied if supplied is not None else current
    if argument.dest == "topic":
        value = field(label, " ".join(initial or []))
        return None if value is None else tokens(value)
    if isinstance(argument, argparse._StoreTrueAction):
        return choose_yes_no(label, bool(initial))
    if command == ["idea", "plan"] and argument.dest == "approve":
        return choose_value(label, [
            (tr("Use the recommendation", "提案どおりに進める"), "recommended"),
            *((branch_label(mode), mode) for mode in ("per-idea", "per-task")),
        ], initial)
    if argument.choices is not None:
        return choose_value(label, [(str(choice), str(choice)) for choice in argument.choices], initial)
    if argument.dest in MULTILINE_FIELDS or argument.dest == "context_file" or initial is not None:
        return edit_field(argument.dest, label, "" if initial is None else str(initial))
    return field(label, "" if initial is None else str(initial))


def fill_arguments(db, parser, command, argv):
    # This parser belongs to this prompt only. The normal CLI parser validates
    # the completed command, including required arguments and choices.
    arguments = [argument for argument in parser._actions if not isinstance(
        argument, (argparse._HelpAction, argparse._SubParsersAction)
    )]
    groups = parser._mutually_exclusive_groups
    group_members = {argument for group in groups for argument in group._group_actions}
    required = {argument for argument in arguments if argument.required or (
        command == ["nextcloud", "connect"] and argument.dest == "server"
    )}
    validation = {argument: (argument.type, argument.choices) for argument in required}
    for argument in arguments:
        argument.required = False
        if not argument.option_strings and argument.nargs is None:
            argument.nargs = "?"
        if argument in required:
            argument.type = argument.choices = None
    required_groups = {group for group in groups if group.required}
    for group in groups:
        group.required = False
    supplied = parser.parse_args(argv)
    for argument, (convert, choices) in validation.items():
        argument.type, argument.choices = convert, choices
    current = settings(db) if command[0] in ("config", "settings") else {}
    options, positionals = [], []
    optional = {}
    for argument in arguments:
        if argument in group_members:
            continue
        provided = getattr(supplied, argument.dest)
        if argument.option_strings and argument not in required:
            optional[argument.dest] = argument
            continue
        target = argument_choices(db, command, argument)

        def validate(value):
            if argument in required and isinstance(value, str) and not value.strip():
                raise ValueError(tr("A value is required.", "値を入力してください。"))
            if argument.type and isinstance(value, str):
                value = argument.type(value)
            if target is not None and all(value != key for _, key in target[1]):
                raise ValueError(tr("Select an available item.", "一覧にある項目を選んでください。"))
            return value

        value = validated_input(
            target[0] if target else argument_label(argument),
            provided,
            lambda label, initial: prompt_argument(
                db, command, argument,
                int(initial) if target and str(initial).isdecimal() else initial,
                heading=label,
            ),
            validate,
        )
        if value is None:
            return None
        if not argument.option_strings:
            positionals.extend(value if isinstance(value, list) else [str(value)])
        else:
            options.append(f"{argument.option_strings[0]}={value}")
    for group in groups:
        choices = [(argument.help, argument.option_strings[0]) for argument in group._group_actions]
        if group not in required_groups:
            choices.insert(0, (tr("Skip", "指定しない"), ""))
        default = next((argument.option_strings[0] for argument in group._group_actions
                        if getattr(supplied, argument.dest)), None)
        selected = choose_value(tr("Choose an action", "操作を選択"), choices, default)
        if selected is None:
            return None
        if selected:
            options.append(selected)
    values = {key: getattr(supplied, key) for key in optional}
    fields = [(key, argument_label(argument)) for key, argument in optional.items()
              if argument.help != argparse.SUPPRESS]
    models = cache(lambda: codex_models(db))

    def edit(key, label, value):
        argument = optional[key]
        initial = current.get(key) if value is None else value
        if command[0] in ("config", "settings"):
            if key == "branch":
                return branch_mode(label, initial, project=False)
            if key == "base":
                return base_branch(label, initial, ".", project=False)
            if key in MODEL_SETTINGS:
                return model_setting(key, label, initial, models(), project=False)
        if isinstance(argument, argparse._AppendAction):
            return validated_input(
                label + "\n" + tr("Separate values with spaces.", "値はスペースで区切ってください。"),
                " ".join(map(str, value or ())), field,
                lambda text: [(argument.type or str)(item) for item in tokens(text)],
            )
        return prompt_argument(db, command, argument, value, current.get(key))

    values = edit_options(fields, values, edit, defaults=DEFAULTS if current else None, current=current)
    if values is None:
        return None
    for key, value in values.items():
        argument = optional[key]
        flag = argument.option_strings[0]
        if isinstance(argument, argparse._StoreTrueAction):
            if value:
                options.append(flag)
        elif isinstance(argument, argparse._AppendAction):
            options.extend(f"{flag}={item}" for item in value or ())
        elif value is not None and value != current.get(key) and (
            value != "" or key in current or getattr(supplied, key) == ""
        ):
            options.append(f"{flag}={value}")
    separator = ["--"] if positionals and command[0] != "help" else []
    result = [*command, *options, *separator, *positionals]
    return confirmed(result, bool(current and options))


def prepare(argv):
    if not argv:
        return argv
    args_before_separator = argv[:argv.index("--")] if "--" in argv else argv
    if "-h" in args_before_separator or "--help" in args_before_separator:
        return argv
    parser = make_parser()
    command = []
    remaining = list(argv)
    while children := next((action for action in parser._actions
                            if isinstance(action, argparse._SubParsersAction)), None):
        if remaining and remaining[0] in children.choices:
            name = remaining.pop(0)
        elif command and not remaining and children.required:
            candidates, _ = parser_candidates(parser)
            name = choose(tr("Command", "コマンド"), [
                (f"{key}  {description}", key) for key, (_, description) in candidates.items()
            ])
            if name is None:
                return None
        else:
            break
        command.append(name)
        parser = children.choices[name]
    if not command:
        return argv
    if command[0] == "project" and len(command) == 1 and remaining and not remaining[0].startswith("-"):
        return argv
    action = command[-1]
    if command == ["help"]:
        return fill_arguments(None, parser, command, remaining)
    if not remaining and all(isinstance(argument, argparse._HelpAction) for argument in parser._actions):
        return command
    with open_queue() as db:
        if command[0] in ("add", "edit", "rm", "remove", "idea"):
            if action == "add":
                return fill_add(db, [action, *remaining])
            if action in ("edit", "rm", "remove"):
                return fill_idea(db, [action, *remaining])
        if command[0] == "reply":
            return fill_reply(db, [*command, *remaining])
        if command[0] == "project" and action not in ("ls", "list"):
            return fill_project(db, [*command, *remaining])
        return fill_arguments(db, parser, command, remaining)


def queue_view():
    with open_queue() as db:
        return queue_status(db)


def command_output(argv):
    output = StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        try:
            main(argv, interactive=False)
        except SystemExit:
            pass
    return output.getvalue().strip()


def screen():
    if os.name == "nt":
        with virtual_terminal(alternate=True):
            return run_screen()
    return run_screen()


def run_screen():
    message = ""
    showing_output = False
    help_argv = None
    try:
        while True:
            language.set(saved_language())
            line = prompt_input(make_parser(), message, show_queue=not showing_output, help_argv=help_argv)
            line = command_text(line)
            if line.strip() in ("q", "quit", "exit"):
                return 0
            try:
                argv = prepare(tokens(line))
                showing_output = bool(argv and argv[0] in ("help", "status"))
                help_argv = argv if argv and argv[0] == "help" else None
                if not argv:
                    message = ""
                    continue
                board = argv[:2] == ["nextcloud", "sync"] or argv[0] == "nextcloud-board"
                live = (
                    argv[:2] == ["nextcloud", "connect"]
                    or argv[0] in ("setup", "pause", "stop")
                    or (board and ("--watch" in argv or any(arg.startswith("--dev-port=") for arg in argv)))
                )
                if live:
                    try:
                        main(argv, interactive=False)
                    except SystemExit:
                        pass
                    screen_input(tr("Press Enter to return to the queue.", "Enter でキューに戻ります。"))
                    message = ""
                else:
                    message = command_output(argv)
            except (ValueError, OSError, RuntimeError, SystemExit) as error:
                message = str(error)
    except KeyboardInterrupt:
        print()
        return 0
    except EOFError:
        return 0


@contextmanager
def virtual_terminal(alternate=False):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32")
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel.GetStdHandle(-11)
    mode = wintypes.DWORD()
    enabled = bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))
                   and kernel.SetConsoleMode(handle, mode.value | 0x0004))
    try:
        if enabled and alternate:
            print("\033[?1049h", end="", flush=True)
        yield enabled
    finally:
        if enabled:
            try:
                if alternate:
                    print("\033[?1049l", end="", flush=True)
            finally:
                kernel.SetConsoleMode(handle, mode.value)


def prompt_frame(view, message, line, candidates, selected, size, offset, form=False, hint=None):
    columns, rows = size
    width = max(0, columns - 1)
    body = list(wrap_rows(view, width))
    if message:
        if body:
            body.append("")
        body.extend(wrap_rows(message, width))
    footer = []
    if rows >= 4:
        if candidates:
            footer.append(tr("Up/Down select  Tab complete  Esc hide", "上下キーで選択  Tab で補完  Esc で閉じる"))
            page = min(5, rows - 4)
            first = max(0, selected - page + 1)
            for index, candidate in enumerate(candidates[first:first + page], first):
                marker = ">" if index == selected else " "
                description = f": {candidate.help}" if candidate.help else ""
                footer.append(f"{marker} {candidate.name}{description}")
        elif hint:
            footer.append(hint)
        elif form:
            footer.append(tr("Enter accept  Tab complete", "Enter で確定  Tab で補完"))
        else:
            footer.append(tr("Enter run  / commands  PgUp/PgDn scroll", "Enter で実行  / で候補  PgUp/PgDn でスクロール"))
    separator = ["-" * width] if rows >= 4 else []
    available = max(0, rows - len(footer) - len(separator) - 1)
    max_offset = max(0, len(body) - available)
    offset = min(offset, max_offset)
    visible = body[offset:offset + available]
    if max_offset and footer:
        footer[0] += "  |  PgUp/PgDn" if candidates else f"  |  {offset + 1}-{offset + len(visible)}/{len(body)}"
    prompt = "cq> "
    preview = line.replace("\r\n", "\n").replace("\n", "↵").expandtabs(4)
    preview = "".join(char for char in preview if unicodedata.category(char) != "Cc")
    visible_input = fit(preview, max(0, width - len(prompt)), tail=True)
    lines = [*visible, *([""] * (available - len(visible))), *footer, *separator, prompt + visible_input]
    return "\n".join(fit(row, width) for row in lines), available, offset, max_offset


def draw_prompt(view, message, line, candidates, selected, size, offset, vt, form=False, hint=None):
    frame, page, offset, max_offset = prompt_frame(view, message, line, candidates, selected, size, offset, form, hint)
    if vt:
        *body, prompt = frame.split("\n")
        # Advancing with newlines can scroll the terminal if it shrinks mid-frame.
        upper = "".join(f"\033[{row};1H\033[2K{text}" for row, text in enumerate(body, 1))
        print(f"{upper}\033[{len(body) + 1};1H\033[2K\033[97;44m{prompt}\033[K\033[0m", end="", flush=True)
    else:
        clear()
        print(frame, end="", flush=True)
    return page, offset, max_offset


def windows_input(parser, message, show_queue=True, help_argv=None, complete=None, initial=None, editor=None):
    with virtual_terminal() as vt:
        return read_prompt(parser, message, vt, show_queue, help_argv, complete, initial, editor)


def read_prompt(parser, message, vt, show_queue=True, help_argv=None, complete=None, initial=None,
                editor=None, console=None):
    if console is None:
        console = ConsoleInput()
    line = initial or ""
    form = complete is not None
    suggest = complete if form else partial(completion, parser)
    view = queue_view() if show_queue else ""
    selected = 0
    start, candidates = len(line), []
    size = shutil.get_terminal_size()
    page, offset, max_offset = draw_prompt(view, message, line, candidates, selected, size, 0, vt, form)
    refresh_at = time.monotonic() + 0.2
    while True:
        redraw = False
        key = console.read()
        if key is not None:
            if key == "\r" or (key == "\n" and (not editor or os.name != "nt")):
                print()
                return line
            if key == "\x03":
                raise KeyboardInterrupt
            if key in ("\x1a", "\x04"):
                raise EOFError
            if key == "\x05" and editor:
                line = edit_text(console, editor, line, vt)
                start, candidates = len(line), []
                selected = 0
                size = shutil.get_terminal_size()
                redraw = True
            elif key == "\x1b" and candidates:
                candidates = []
                redraw = True
            elif key == "\x1b" and form:
                return None
            elif key in ("up", "down") and candidates:
                selected = (selected + (1 if key == "down" else -1)) % len(candidates)
                redraw = True
            elif key in ("pageup", "pagedown"):
                direction = 1 if key == "pagedown" else -1
                new_offset = max(0, min(max_offset, offset + direction * max(1, page - 1)))
                redraw = offset != new_offset
                offset = new_offset
            else:
                previous = line
                if key == "\t" and candidates:
                    candidate = candidates[selected]
                    line = line[:start] + (candidate.insertion if candidate.insertion is not None else candidate.name + " ")
                elif key in ("\b", "\x7f") and line:
                    end = len(line) - 1
                    while end and unicodedata.category(line[end]).startswith("M"):
                        end -= 1
                    line = line[:end]
                elif key == "\x15" and initial is not None:
                    line = ""
                elif editor and key in ("ctrl+enter", "\n"):
                    line += "\n"
                elif len(key) == 1 and key.isprintable():
                    line += key
                if line != previous:
                    start, candidates = suggest(line)
                    selected = 0
                    redraw = True
        else:
            time.sleep(0.03)
        now = time.monotonic()
        if now >= refresh_at:
            refresh_at = now + 0.2
            current = queue_view() if show_queue else ""
            current_size = shutil.get_terminal_size()
            if current != view or current_size != size:
                if help_argv and current_size.columns != size.columns:
                    message = command_output(help_argv)
                view = current
                size = current_size
                redraw = True
        if redraw:
            page, offset, max_offset = draw_prompt(view, message, line, candidates, selected, size, offset, vt, form)
