import argparse

from .language import language


class JapaneseHelpFormatter(argparse.HelpFormatter):
    def _format_usage(self, usage, actions, groups, prefix=None):
        if prefix is None:
            prefix = "使い方: "
        return super()._format_usage(usage, actions, groups, prefix)


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        japanese = language.get() == "ja"
        if japanese:
            kwargs.setdefault("formatter_class", JapaneseHelpFormatter)
        super().__init__(*args, **kwargs)
        if japanese:
            self._positionals.title = "位置引数"
            self._optionals.title = "オプション"
            if self.add_help:
                self._option_string_actions["--help"].help = "このヘルプを表示して終了"
