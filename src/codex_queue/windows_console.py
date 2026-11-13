import ctypes
from ctypes import wintypes


class KeyEvent(ctypes.Structure):
    _fields_ = [
        ("down", wintypes.BOOL),
        ("repeat", wintypes.WORD),
        ("virtual_key", wintypes.WORD),
        ("scan_code", wintypes.WORD),
        ("character", wintypes.WORD),
        ("control_state", wintypes.DWORD),
    ]


class InputRecord(ctypes.Structure):
    # KEY_EVENT_RECORD supplies the size and alignment of the Win32 event union.
    _fields_ = [("type", wintypes.WORD), ("key", KeyEvent)]


NAVIGATION_KEYS = {0x21: "pageup", 0x22: "pagedown", 0x26: "up", 0x28: "down"}


class ConsoleInput:
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        self.handle = kernel.GetStdHandle(-10)
        self.count_events = kernel.GetNumberOfConsoleInputEvents
        self.count_events.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.count_events.restype = wintypes.BOOL
        self.read_event = kernel.ReadConsoleInputW
        self.read_event.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(InputRecord), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        self.read_event.restype = wintypes.BOOL
        self.repeated_key = None
        self.remaining = 0
        self.high_surrogate = None

    def read(self):
        if self.remaining:
            self.remaining -= 1
            return self.repeated_key

        count = wintypes.DWORD()
        record = InputRecord()
        while True:
            if not self.count_events(self.handle, ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not count.value:
                return None
            if not self.read_event(self.handle, ctypes.byref(record), 1, ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            if record.type != 1 or not record.key.down:
                continue

            event = record.key
            code = event.character
            if 0xD800 <= code <= 0xDBFF:
                self.high_surrogate = code
                continue
            if 0xDC00 <= code <= 0xDFFF:
                if self.high_surrogate is None:
                    continue
                code = 0x10000 + ((self.high_surrogate - 0xD800) << 10) + (code - 0xDC00)
            key = chr(code) if code else NAVIGATION_KEYS.get(event.virtual_key)
            if key is not None:
                self.high_surrogate = None
                self.repeated_key = key
                self.remaining = max(0, event.repeat - 1)
                return key
