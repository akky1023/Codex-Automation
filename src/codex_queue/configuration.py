"""Editable queue settings shared by CLI and the board bridge."""
import json

from .autonomy import APPROVAL_MODES, MERGE_MODES, parse_test_commands
from .store import DEFAULTS, settings, update

PROJECT_FIELDS = ('name', 'prompt', 'branch', 'base', 'planner_model',
                  'implementation_model', 'allowed_models', 'merge_mode', 'test_commands', 'nextcloud_sync')
INHERITABLE = set(PROJECT_FIELDS) - {'name', 'prompt', 'nextcloud_sync'}
CHOICES = {
    'branch': ('per-idea', 'per-task', 'auto', 'current'),
    'on_abort': ('pause', 'continue', 'next-idea'), 'completion_approval': APPROVAL_MODES,
    'merge_mode': MERGE_MODES, 'on_conflict': ('resolve', 'hold'),
    'sync': ('auto', 'manual'), 'language': ('en', 'ja'),
    'idea_planning': ('auto', 'manual'), 'nextcloud_sync': ('on', 'off'),
}


def normalize(values, *, project=False):
    if not isinstance(values, dict) or not values.keys() <= set(PROJECT_FIELDS if project else DEFAULTS):
        raise RuntimeError('未知の設定項目です。')
    result = {}
    for key, value in values.items():
        if value is None and project and key in INHERITABLE:
            result[key] = None
            continue
        if not isinstance(value, str) and not (type(value) is int and key in ('parallel', 'priority', 'test_retries')):
            raise RuntimeError(f'{key}: 値が不正です。')
        value = str(value)
        if '\0' in value or len(value) > 12000:
            raise RuntimeError(f'{key}: 値が不正です。')
        if key in CHOICES and value not in CHOICES[key]:
            raise RuntimeError(f'{key}: 選択肢が不正です。')
        if key in ('parallel', 'priority', 'test_retries'):
            if not value.isascii() or not value.isdecimal():
                raise RuntimeError(f'{key}: 整数を指定してください。')
            try:
                number = int(value)
            except ValueError as error:
                raise RuntimeError(f'{key}: 整数を指定してください。') from error
            if number < (0 if key == 'test_retries' else 1) or (key == 'priority' and number > 9):
                raise RuntimeError(f'{key}: 範囲外です。')
            value = str(number)
        if key == 'allowed_models':
            value = value.strip()
        if key == 'allowed_models' and value:
            models = [model.strip() for model in value.split(',')]
            if not all(models) or len(set(models)) != len(models):
                raise RuntimeError('allowed_models: 異なるモデル名をカンマ区切りで指定してください。')
            value = ','.join(models)
        if key == 'test_commands' and value:
            try:
                value = json.dumps(parse_test_commands(value), ensure_ascii=False)
            except ValueError as error:
                raise RuntimeError(f'test_commands: {error}') from error
        result[key] = value
    return result


def current_values(db, project_id=0):
    if not project_id:
        return settings(db)
    row = db.execute('SELECT * FROM projects WHERE id = ? AND retired = 0', (project_id,)).fetchone()
    if row is None:
        raise RuntimeError('同期対象の Project が見つかりません。')
    return {key: row[key] for key in PROJECT_FIELDS}


def save_values(db, values, project_id=0):
    values = normalize(values, project=bool(project_id))
    if project_id:
        update(db, 'projects', project_id, values)
    else:
        db.executemany('INSERT INTO settings(key,value) VALUES (?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', values.items())
    return values
