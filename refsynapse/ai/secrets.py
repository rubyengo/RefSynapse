"""Windows DPAPI storage for the assistant API key (stdlib ctypes only).

The key never sits in settings.json as plaintext: it is wrapped with
CryptProtectData under the current Windows user (plus app-specific entropy),
stored as "dpapi:<base64>", and unwrapped again at load time.

Known trade-off: DPAPI ties the blob to this Windows user on this machine.
Copying settings.json to another PC or user will not decrypt - the key must
be re-entered there. On non-Windows platforms storage falls back to plaintext.

Self-test:
    python -m refsynapse.ai.secrets
"""

import base64
import ctypes
import os
import sys
from ctypes import wintypes

PREFIX = "dpapi:"
ENTROPY = b"RefSynapse/ai/api-key/v1"
DESCRIPTION = "RefSynapse API key"


class SecretError(Exception):
    """DPAPI refused to wrap or unwrap the secret."""


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def available():
    return os.name == "nt"


def storage_note():
    if available():
        return "Key is stored encrypted with Windows DPAPI (this user, this PC)."
    return "DPAPI is unavailable on this platform; the key is stored as plaintext."


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _functions():
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    return crypt32


def _take(out):
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(text):
    """Plaintext -> "dpapi:<base64>" (empty stays empty)."""
    if not text:
        return ""
    if not available():
        return text
    crypt32 = _functions()
    in_blob, ent_blob, out = _blob(text.encode("utf-8")), _blob(ENTROPY), _DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(in_blob), DESCRIPTION,
                                    ctypes.byref(ent_blob), None, None, 0,
                                    ctypes.byref(out)):
        raise SecretError("CryptProtectData failed (winerror %d)" % ctypes.GetLastError())
    return PREFIX + base64.b64encode(_take(out)).decode("ascii")


def unprotect(stored):
    """"dpapi:<base64>" -> plaintext; legacy plaintext passes through."""
    if not stored:
        return ""
    if not is_encrypted(stored):
        return stored
    if not available():
        raise SecretError("DPAPI blob cannot be decrypted on this platform")
    try:
        raw = base64.b64decode(stored[len(PREFIX):])
    except ValueError:
        raise SecretError("Stored key blob is not valid base64") from None
    crypt32 = _functions()
    in_blob, ent_blob, out = _blob(raw), _blob(ENTROPY), _DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None,
                                      ctypes.byref(ent_blob), None, None, 0,
                                      ctypes.byref(out)):
        raise SecretError("Could not decrypt the stored key (winerror %d). "
                          "Different Windows user or corrupted blob?" % ctypes.GetLastError())
    return _take(out).decode("utf-8", "replace")


def _selftest():
    """python -m refsynapse.ai.secrets"""
    print("DPAPI available:", available())
    secret = "sk-test-12345-привет"
    wrapped = protect(secret)
    print("wrapped:", wrapped[:44] + ("…" if len(wrapped) > 44 else ""))
    print("is_encrypted:", is_encrypted(wrapped))
    print("round-trip ok:", unprotect(wrapped) == secret)
    print("legacy passthrough:", unprotect("plain-key") == "plain-key")
    if available():
        try:
            unprotect(PREFIX + base64.b64encode(b"garbage").decode("ascii"))
            print("tamper: NOT detected (bad)")
        except SecretError:
            print("tamper: detected (good)")
    print("DONE")


if __name__ == "__main__":
    _selftest()