"""Windows Credential Manager storage for the Nextcloud app password."""
import ctypes
from ctypes import wintypes
import json
import os


class Credential(ctypes.Structure):
    _fields_ = [
        ("flags", wintypes.DWORD), ("type", wintypes.DWORD),
        ("target", wintypes.LPWSTR), ("comment", wintypes.LPWSTR),
        ("last_written", wintypes.FILETIME), ("size", wintypes.DWORD),
        ("blob", ctypes.POINTER(ctypes.c_ubyte)), ("persist", wintypes.DWORD),
        ("attribute_count", wintypes.DWORD), ("attributes", ctypes.c_void_p),
        ("alias", wintypes.LPWSTR), ("username", wintypes.LPWSTR),
    ]


def api():
    if os.name != "nt":
        raise RuntimeError("Nextcloud credentials require Windows Credential Manager.")
    library = ctypes.WinDLL("Advapi32", use_last_error=True)
    library.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    library.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(Credential)),
    ]
    library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    library.CredFree.argtypes = [ctypes.c_void_p]
    library.CredFree.restype = None
    return library


def save(target, username, password):
    data = json.dumps([username, password]).encode("utf-8")
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    credential = Credential(type=1, target=target, size=len(data), blob=blob, persist=2)
    if not api().CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def load(target):
    library = api()
    pointer = ctypes.POINTER(Credential)()
    if not library.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return json.loads(ctypes.string_at(pointer.contents.blob, pointer.contents.size))
    finally:
        library.CredFree(pointer)


def delete(target):
    if not api().CredDeleteW(target, 1, 0):
        error = ctypes.get_last_error()
        if error != 1168:
            raise ctypes.WinError(error)
