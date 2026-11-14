import os
import shutil
import time
import unicodedata

from .language import tr
from .text_layout import fit, text_width


def display_text(text):
    return "".join(char for char in text.replace("\t", "    ") if unicodedata.category(char) != "Cc")


def draw_editor(label, lines, row, column, top, vt):
    columns, height = shutil.get_terminal_size()
    width = max(1, columns - 1)
    page = max(1, height - 2)
    top = min(max(top, row - page + 1), row)
    body = []
    cursor_column = 1
    for index, line in enumerate(lines[top:top + page], top):
        if index == row:
            before = fit(display_text(line[:column]), max(0, width - 1), tail=True)
            cursor_column = text_width(before) + 1
            line = before + display_text(line[column:])
        else:
            line = display_text(line)
        body.append(fit(line, width))
    hint = tr("Ctrl+S apply  Esc cancel  Enter newline", "Ctrl+S で反映  Esc で取消  Enter で改行")
    if vt:
        print("\033[2J\033[H" + fit(label, width), end="")
        for index, line in enumerate(body, 2):
            print(f"\033[{index};1H{line}", end="")
        if height >= 3:
            print(f"\033[{height};1H\033[97;44m{fit(hint, width)}\033[K\033[0m", end="")
        print(f"\033[{min(height, row - top + 2)};{cursor_column}H", end="", flush=True)
    else:
        os.system("cls")
        print(label, *body, hint, sep="\n", flush=True)
    return top


def edit_text(console, label, original, vt):
    lines = original.replace("\r\n", "\n").split("\n")
    row = len(lines) - 1
    column = len(lines[row])
    top = 0
    size = None
    redraw = True
    while True:
        current_size = shutil.get_terminal_size()
        if redraw or size != current_size:
            top = draw_editor(label, lines, row, column, top, vt)
            size = current_size
        key = console.read()
        redraw = True
        line = lines[row]
        if key == "\x13":
            return "\n".join(lines)
        if key == "\x1b":
            return original
        if key == "\x03":
            raise KeyboardInterrupt
        if key in ("\x1a", "\x04"):
            raise EOFError
        if key in ("up", "down"):
            row = max(0, min(len(lines) - 1, row + (1 if key == "down" else -1)))
            column = min(column, len(lines[row]))
        elif key == "left":
            if column:
                column -= 1
            elif row:
                row -= 1
                column = len(lines[row])
        elif key == "right":
            if column < len(line):
                column += 1
            elif row < len(lines) - 1:
                row += 1
                column = 0
        elif key in ("home", "end"):
            column = len(line) if key == "end" else 0
        elif key in ("\r", "\n"):
            lines[row:row + 1] = [line[:column], line[column:]]
            row += 1
            column = 0
        elif key in ("\b", "\x7f"):
            if column:
                lines[row] = line[:column - 1] + line[column:]
                column -= 1
            elif row:
                column = len(lines[row - 1])
                lines[row - 1:row + 1] = [lines[row - 1] + line]
                row -= 1
        elif key == "delete":
            if column < len(line):
                lines[row] = line[:column] + line[column + 1:]
            elif row < len(lines) - 1:
                lines[row:row + 2] = [line + lines[row + 1]]
        elif key and (key == "\t" or (len(key) == 1 and key.isprintable())):
            inserted = "    " if key == "\t" else key
            lines[row] = line[:column] + inserted + line[column:]
            column += len(inserted)
        else:
            redraw = False
            if key is None:
                time.sleep(0.03)
