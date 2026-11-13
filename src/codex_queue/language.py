from contextvars import ContextVar


LANGUAGES = ("en", "ja")
language = ContextVar("language", default="en")


def tr(english, japanese, **values):
    message = japanese if language.get() == "ja" else english
    return message.format(**values) if values else message


def status_label(status):
    if language.get() != "ja":
        return status
    return {
        "QUEUED": "待機中",
        "RUNNING": "実行中",
        "DONE": "完了",
        "ABORTED": "中止",
        "INTERRUPTED": "中断",
        "WAITING_FOR_INPUT": "入力待ち",
        "NEEDS_REVIEW": "確認待ち",
        "RESUME_QUEUED": "再開待ち",
        "RATE_LIMITED": "利用上限の解除待ち",
        "AUTH_REQUIRED": "認証待ち",
        "RETRY_WAIT": "再試行待ち",
        "REMOVED": "削除済み",
    }.get(status, status)
