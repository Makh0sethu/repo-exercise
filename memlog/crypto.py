"""Encryption at rest for the memory file, standard library only.

Construction (all primitives from ``hashlib``/``hmac``/``secrets``):

* key derivation: scrypt(passphrase, salt, n=2**15, r=8, p=1) -> 64 bytes,
  split into a 32-byte encryption key and a 32-byte MAC key;
* encryption: HMAC-SHA256 in counter mode as a keystream
  (block_i = HMAC(enc_key, nonce || i)), XORed with zlib-compressed plaintext;
* integrity: HMAC-SHA256(mac_key, header || nonce || ciphertext), verified in
  constant time before anything is decrypted (encrypt-then-MAC).

A fresh random salt and nonce are used on every write. A wrong passphrase or
a modified file fails the tag check and raises ``DecryptionError``; no
partial plaintext is ever returned.

File layout: MAGIC(8) | salt(16) | nonce(16) | ciphertext | tag(32)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import tempfile
import zlib
from pathlib import Path

MAGIC = b"MEMLOG1\x00"
SALT_LEN = 16
NONCE_LEN = 16
TAG_LEN = 32
_BLOCK = 32
_SCRYPT = dict(n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)


class DecryptionError(Exception):
    """Wrong passphrase, or the file was modified or truncated."""


def is_encrypted_file(path: str | os.PathLike[str]) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except FileNotFoundError:
        return False


def derive_keys(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    km = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, dklen=64, **_SCRYPT)
    return km[:32], km[32:]


def _keystream_xor(key: bytes, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    for i in range(0, len(data), _BLOCK):
        block = hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        chunk = data[i : i + _BLOCK]
        out[i : i + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, block))
    return bytes(out)


def encrypt_bytes(plaintext: bytes, passphrase: str) -> bytes:
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    enc_key, mac_key = derive_keys(passphrase, salt)
    body = _keystream_xor(enc_key, nonce, zlib.compress(plaintext, 6))
    header = MAGIC + salt + nonce
    tag = hmac.new(mac_key, header + body, hashlib.sha256).digest()
    return header + body + tag


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    if len(blob) < len(MAGIC) + SALT_LEN + NONCE_LEN + TAG_LEN or not blob.startswith(MAGIC):
        raise DecryptionError("not a memlog encrypted file")
    off = len(MAGIC)
    salt = blob[off : off + SALT_LEN]
    off += SALT_LEN
    nonce = blob[off : off + NONCE_LEN]
    off += NONCE_LEN
    body, tag = blob[off:-TAG_LEN], blob[-TAG_LEN:]
    enc_key, mac_key = derive_keys(passphrase, salt)
    expected = hmac.new(mac_key, blob[: off] + body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise DecryptionError("wrong passphrase, or the file has been modified")
    try:
        return zlib.decompress(_keystream_xor(enc_key, nonce, body))
    except zlib.error as exc:  # cannot happen after a valid tag, but never return garbage
        raise DecryptionError("corrupt payload") from exc


def write_private(path: str | os.PathLike[str], data: bytes) -> None:
    """Atomically write ``data`` to ``path`` with owner-only permissions (0600)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def shred(path: str | os.PathLike[str]) -> bool:
    """Overwrite a file with random bytes, then delete it.

    Best effort: on SSDs and copy-on-write filesystems the old blocks may
    survive the overwrite, which is exactly why encryption at rest matters.
    """
    path = Path(path)
    if not path.exists():
        return False
    size = path.stat().st_size
    try:
        with open(path, "r+b") as fh:
            fh.write(secrets.token_bytes(size))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    path.unlink()
    return True
