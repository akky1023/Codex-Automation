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
        """SELECT i.*, p.path, p.name AS project, p.prompt AS project_prompt,
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


def generate_plan(db, idea, config, models):
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
    command = [*codex_command(db), "-a", "never", "exec", "-s", "read-only"]
    if config["planner_model"]:
        command += ["-m", config["planner_model"]]
    with TemporaryDirectory(prefix="plan-", dir=queue_folder()) as directory:
        schema_path = Path(directory) / "schema.json"
        output = Path(directory) / "plan.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        result = subprocess.run(
            [*command, "--output-schema", str(schema_path), "-o", str(output), "-"],
            input=planning_prompt(idea, models, config["implementation_model"]),
            cwd=idea["path"], env=codex_env(db), encoding="utf-8", errors="replace",
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
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Invalid planner response.") from error
    return question, todos


def plan_idea(db, idea_id):
    with process_lock(f"idea-{idea_id}.lock") as acquired:
        if not acquired:
            raise RuntimeError(f"Idea {idea_id} is already being planned.")
        return save_plan(db, idea_id)


def save_plan(db, idea_id):
    idea = idea_record(db, idea_id)
    if idea["planned_at"]:
        raise RuntimeError(f"Idea {idea_id} already has a plan.")
    config = planning_config(db, idea)
    models = config["allowed_models"].split(",") if config["allowed_models"] else []
    question, todos = generate_plan(db, idea, config, models)
    if question:
        raise RuntimeError(f"{question}\nUse cq idea edit {idea_id} to clarify the idea, then plan again.")
    with write(db):
        current = idea_record(db, idea_id)
        if current != idea or planning_config(db, current) != config:
            raise RuntimeError(f"Idea {idea_id} or its project settings changed during planning.")
        priority = int(settings(db)["priority"])
        task_ids = []
        for task in todos:
            cursor = db.execute(
                """INSERT INTO tasks(project_id, idea_id, name, abstract, prompt, priority, implementation_model)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (idea["project_id"], idea_id, task["title"].strip(), task["abstract"].strip(),
                 task["plan"].strip(), priority, task["model"] if models else None),
            )
            task_ids.append(cursor.lastrowid)
        db.execute(
            "UPDATE ideas SET planned_at = CURRENT_TIMESTAMP, planning_error = NULL WHERE id = ?",
            (idea_id,),
        )
    return task_ids


def plan_next_idea(db):
    if settings(db)["idea_planning"] != "auto":
        return False
    idea = db.execute(
        """SELECT i.id, i.title, i.detail FROM ideas i JOIN projects p ON p.id = i.project_id
           WHERE i.planned_at IS NULL AND i.planning_error IS NULL AND p.retired = 0
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
