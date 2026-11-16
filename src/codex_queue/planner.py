import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from .processes import process_lock
from .runtime import codex_command, codex_env
from .store import queue_folder, settings, write


def planning_config(db, project):
    defaults = settings(db)
    return {
        key: defaults[key] if project[key] is None else project[key]
        for key in ("planner_model", "implementation_model", "allowed_models")
    }


def idea_record(db, idea_id):
    row = db.execute(
        """SELECT i.*, p.path, p.name AS project, p.prompt AS project_prompt, p.branch AS project_branch,
                  p.planner_model, p.implementation_model, p.allowed_models
           FROM ideas i JOIN projects p ON p.id = i.project_id
           WHERE i.id = ? AND p.retired = 0""",
        (idea_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Unknown idea ID {idea_id}.")
    return row


def planning_prompt(idea, models, default_model):
    routing = (
        "For each ToDo, consider implementation models in the given lowest-to-highest order. "
        "Select the first model capable of implementing and verifying the plan. "
        f"Allowed models in order: {json.dumps(models)}."
        if models else f"Use the configured implementation model {json.dumps(default_model)}."
    )
    return (
        "Inspect the repository to turn this idea into one or more independently executable ToDos. "
        "Keep a sufficiently specific idea as one ToDo unless it contains separable work. "
        "Inspect relevant code, constraints and tests. Produce a short commit-style title, "
        "a human-readable abstract, and a complete implementation plan covering changes "
        "and verification. Use the language of the idea. Only plan the work. "
        "When a material requirement is missing, return a concrete question and an empty todos list. "
        "Otherwise return an empty question and at least one ToDo. "
        "Order related ToDos so prerequisite work comes first.\n"
        f"{routing}\n\n"
        f"# Project instructions\n{idea['project_prompt']}\n\n"
        f"# Idea\n{idea['title']}\n\n{idea['detail']}"
    )


def generate_plan(db, path, prompt, config, models, *, review_branch=False):
    options = models or [config["implementation_model"]]
    fields = {key: {"type": "string"} for key in ("title", "abstract", "plan")}
    fields["model"] = {"type": "string", "enum": options}
    schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "todos": {
                "type": "array",
                "items": {
                    "type": "object", "properties": fields,
                    "required": list(fields), "additionalProperties": False,
                },
            },
        },
        "required": ["question", "todos"],
        "additionalProperties": False,
    }
    if review_branch:
        prompt += (
            "\n\nRecommend per-idea when the ToDos belong in one review or need each other's changes. "
            "Recommend per-task when the ToDos can be reviewed and integrated separately. "
            "Explain the recommendation briefly in the language of the Idea."
        )
        schema["properties"].update({
            "branch_mode": {"type": "string", "enum": ["per-idea", "per-task"]},
            "branch_reason": {"type": "string"},
        })
        schema["required"].extend(("branch_mode", "branch_reason"))
    command = [*codex_command(db), "-a", "never", "exec", "-s", "read-only"]
    if config["planner_model"]:
        command += ["-m", config["planner_model"]]
    with TemporaryDirectory(prefix="plan-", dir=queue_folder()) as directory:
        schema_path = Path(directory) / "schema.json"
        output = Path(directory) / "plan.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        result = subprocess.run(
            [*command, "--output-schema", str(schema_path), "-o", str(output), "-"],
            input=prompt, cwd=path, env=codex_env(db), encoding="utf-8", errors="replace",
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"Planner exited with code {result.returncode}.")
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
            question, todos = report["question"].strip(), report["todos"]
            if not isinstance(todos, list) or bool(question) == bool(todos):
                raise ValueError
            for task in todos:
                if task["model"] not in options or any(
                    not task[key].strip() for key in ("title", "abstract", "plan")
                ) or len(task["title"].strip().splitlines()) != 1:
                    raise ValueError
            if review_branch:
                mode, reason = report["branch_mode"], report["branch_reason"].strip()
                if mode not in ("per-idea", "per-task") or (todos and not reason):
                    raise ValueError
                return question, todos, mode, reason
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Invalid planner response.") from error
    return question, todos


def plan_idea(db, idea_id, branch_choice=None):
    with process_lock(f"idea-{idea_id}.lock") as acquired:
        if not acquired:
            raise RuntimeError(f"Idea {idea_id} is already being planned.")
        return save_plan(db, idea_id, branch_choice)


def plan_context(db, idea):
    return {
        "idea": {key: idea[key] for key in idea.keys() if key not in ("draft_plan", "planning_error")},
        "config": planning_config(db, idea),
        "mode": idea["project_branch"] or settings(db)["branch"],
    }


def save_plan(db, idea_id, branch_choice=None):
    idea = idea_record(db, idea_id)
    if idea["planned_at"]:
        raise RuntimeError(f"Idea {idea_id} already has a plan.")
    context = plan_context(db, idea)
    config, mode = context["config"], context["mode"]
    models = config["allowed_models"].split(",") if config["allowed_models"] else []
    draft = json.loads(idea["draft_plan"]) if idea["draft_plan"] else None
    waiting_for_review = mode == "auto" and draft and draft["context"] == context
    if branch_choice and not waiting_for_review:
        raise RuntimeError(f"No current proposal to approve. Run cq idea plan {idea_id} first.")
    if waiting_for_review:
        if not branch_choice:
            return []
        mode = draft["branch_mode"] if branch_choice == "recommended" else branch_choice
        todos = draft["todos"]
    else:
        prompt = planning_prompt(idea, models, config["implementation_model"])
        if mode == "auto":
            question, todos, recommendation, reason = generate_plan(
                db, idea["path"], prompt, config, models, review_branch=True,
            )
        else:
            question, todos = generate_plan(db, idea["path"], prompt, config, models)
        if question:
            raise RuntimeError(f"{question}\nUse cq idea edit {idea_id} to clarify the idea, then plan again.")
    with write(db):
        current = idea_record(db, idea_id)
        if current != idea or plan_context(db, current) != context:
            raise RuntimeError(f"Idea {idea_id} or its project settings changed during planning.")
        if mode == "auto":
            draft = {"context": context, "todos": todos, "branch_mode": recommendation, "reason": reason}
            db.execute("UPDATE ideas SET draft_plan = ?, planning_error = NULL WHERE id = ?",
                       (json.dumps(draft, ensure_ascii=False), idea_id))
            return []
        priority = int(settings(db)["priority"])
        task_ids = []
        for position, task in enumerate(todos, 1):
            cursor = db.execute(
                """INSERT INTO tasks(project_id, idea_id, name, abstract, prompt, priority, implementation_model, idea_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (idea["project_id"], idea_id, task["title"].strip(), task["abstract"].strip(),
                 task["plan"].strip(), priority, task["model"] if models else None, position),
            )
            task_ids.append(cursor.lastrowid)
        db.execute(
            """UPDATE ideas SET planned_at = CURRENT_TIMESTAMP, planning_error = NULL,
               branch_mode = ?, draft_plan = NULL WHERE id = ?""",
            (mode, idea_id),
        )
    return task_ids


def revise_task(db, task_id, instruction):
    task = db.execute(
        """SELECT t.*, p.path, p.prompt AS project_prompt, p.planner_model,
                  p.implementation_model AS project_model, p.allowed_models,
                  i.title AS idea_title, i.detail AS idea_detail, i.branch_mode AS idea_branch_mode
           FROM tasks t JOIN projects p ON p.id = t.project_id
           LEFT JOIN ideas i ON i.id = t.idea_id
           WHERE t.id = ? AND t.status NOT IN ('REMOVED', 'SUPERSEDED') AND p.retired = 0""",
        (task_id,),
    ).fetchone()
    if task is None or not instruction.strip():
        raise RuntimeError("Specify an existing ToDo and a revision request.")
    if task["status"] in ("RUNNING", "RESUME_QUEUED"):
        raise RuntimeError("Wait for the current execution to stop before requesting a revision.")
    config = planning_config(db, {**dict(task), "implementation_model": task["project_model"]})
    models = config["allowed_models"].split(",") if config["allowed_models"] else []
    attempts = [dict(row) for row in db.execute(
        """SELECT number, phase, branch, start_commit, end_commit, reason, report, answer
           FROM attempts WHERE task_id = ? ORDER BY number""", (task_id,),
    )]
    history = [dict(row) for row in db.execute(
        "SELECT action, note FROM task_actions WHERE task_id = ? ORDER BY rowid", (task_id,),
    )]
    context = {
        "title": task["name"], "abstract": task["abstract"], "plan": task["prompt"],
        "status": task["status"], "idea": [task["idea_title"], task["idea_detail"]],
        "attempts": attempts, "actions": history,
    }
    prompt = planning_prompt(
        {"title": instruction, "detail": json.dumps(context, ensure_ascii=False),
         "project_prompt": task["project_prompt"]}, models, config["implementation_model"],
    )
    prompt += (
        f"\n\nThis request revises ToDo {task_id}. Use its original plan and execution history. "
        "Return exactly one replacement plan, or ask for specific missing history or clarification. "
        "If prior commits are needed, identify the branch or commit in the plan."
    )
    question, todos = generate_plan(db, task["path"], prompt, config, models)
    if question:
        return None, question
    if len(todos) != 1:
        raise RuntimeError("A ToDo revision must produce one replacement plan.")
    revision = todos[0]
    with write(db):
        current = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        project = db.execute("SELECT * FROM projects WHERE id = ?", (task["project_id"],)).fetchone()
        if (current is None or any(current[key] != task[key] for key in current.keys())
                or project["retired"] or project["path"] != task["path"]
                or project["prompt"] != task["project_prompt"] or planning_config(db, project) != config):
            raise RuntimeError("The ToDo or its project changed during planning. Request the revision again.")
        remote = db.execute("SELECT 1 FROM remote_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task["status"] == "QUEUED" and not remote:
            db.execute(
                "UPDATE tasks SET name = ?, abstract = ?, prompt = ?, implementation_model = ? WHERE id = ?",
                (revision["title"].strip(), revision["abstract"].strip(), revision["plan"].strip(),
                 revision["model"] if models else None, task_id),
            )
            return task_id, ""
        replaces_position = task["status"] != "DONE" and (
            task["status"] != "ABORTED" or task["idea_branch_mode"] == "per-idea"
        )
        cursor = db.execute(
            """INSERT INTO tasks(project_id, idea_id, name, abstract, prompt, priority, implementation_model, revision_of, idea_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task["project_id"], task["idea_id"], revision["title"].strip(), revision["abstract"].strip(),
             revision["plan"].strip(), task["priority"], revision["model"] if models else None, task_id,
             (task["idea_order"] or task_id) if replaces_position else None),
        )
        if replaces_position:
            db.execute("UPDATE tasks SET status = 'SUPERSEDED' WHERE id = ?", (task_id,))
            db.execute("INSERT INTO task_actions(task_id, action, note) VALUES (?, 'revise', ?)",
                       (task_id, f"Superseded by ToDo {cursor.lastrowid}"))
    return cursor.lastrowid, ""


def plan_next_idea(db):
    if settings(db)["idea_planning"] != "auto":
        return False
    idea = db.execute(
        """SELECT i.id, i.title, i.detail FROM ideas i JOIN projects p ON p.id = i.project_id
           WHERE i.planned_at IS NULL AND i.planning_error IS NULL AND i.draft_plan IS NULL AND p.retired = 0
           ORDER BY i.id LIMIT 1"""
    ).fetchone()
    if idea is None:
        return False
    with process_lock(f"idea-{idea['id']}.lock") as acquired:
        if not acquired:
            return False
        try:
            save_plan(db, idea["id"])
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            with write(db):
                db.execute(
                    """UPDATE ideas SET planning_error = ? WHERE id = ? AND planned_at IS NULL
                       AND title = ? AND detail = ?""",
                    (str(error), idea["id"], idea["title"], idea["detail"]),
                )
    return True
