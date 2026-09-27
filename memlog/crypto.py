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

Two file layouts share the same cipher:

* self-contained (``encrypt_bytes``): MAGIC1 | salt(16) | nonce(16) | ct | tag(32)
  Each file carries its own salt, so each open costs one scrypt.
* keyed (``seal``): MAGIC2 | nonce(16) | ct | tag(32)
  The salt lives once in the vault's config; keys are derived once per
  session and reused for every file in the vault.
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
MAGIC2 = b"MEMLOG2\x00"
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
            return is_encrypted(fh.read(len(MAGIC)))
    except FileNotFoundError:
        return False


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC) or blob.startswith(MAGIC2)


class Keys:
    """An encryption key and a MAC key derived from one passphrase."""

    __slots__ = ("enc", "mac")

    def __init__(self, enc: bytes, mac: bytes):
        self.enc, self.mac = enc, mac

    def verifier(self) -> bytes:
        """A value that proves the passphrase without revealing the keys."""
        return hmac.new(self.mac, b"memlog-vault-check", hashlib.sha256).digest()

    def __repr__(self) -> str:  # never print key material
        return "Keys(<redacted>)"


def derive_keys(passphrase: str, salt: bytes) -> Keys:
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    km = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, dklen=64, **_SCRYPT)
    return Keys(km[:32], km[32:])


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_LEN)


def _keystream_xor(key: bytes, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    for i in range(0, len(data), _BLOCK):
        block = hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        chunk = data[i : i + _BLOCK]
        out[i : i + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, block))
    return bytes(out)


def _seal_with(keys: Keys, header: bytes, nonce: bytes, plaintext: bytes) -> bytes:
    body = _keystream_xor(keys.enc, nonce, zlib.compress(plaintext, 6))
    tag = hmac.new(keys.mac, header + body, hashlib.sha256).digest()
    return header + body + tag


def _open_with(keys: Keys, header_len: int, nonce: bytes, blob: bytes) -> bytes:
    body, tag = blob[header_len:-TAG_LEN], blob[-TAG_LEN:]
    expected = hmac.new(keys.mac, blob[:header_len] + body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise DecryptionError("wrong passphrase, or the file has been modified")
    try:
        return zlib.decompress(_keystream_xor(keys.enc, nonce, body))
    except zlib.error as exc:  # cannot happen after a valid tag, but never return garbage
        raise DecryptionError("corrupt payload") from exc


def encrypt_bytes(plaintext: bytes, passphrase: str) -> bytes:
    """Self-contained format: the salt travels with the file."""
    salt = new_salt()
    nonce = secrets.token_bytes(NONCE_LEN)
    return _seal_with(derive_keys(passphrase, salt), MAGIC + salt + nonce, nonce, plaintext)


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    if len(blob) < len(MAGIC) + SALT_LEN + NONCE_LEN + TAG_LEN or not blob.startswith(MAGIC):
        raise DecryptionError("not a memlog encrypted file")
    off = len(MAGIC)
    salt = blob[off : off + SALT_LEN]
    nonce = blob[off + SALT_LEN : off + SALT_LEN + NONCE_LEN]
    return _open_with(derive_keys(passphrase, salt), off + SALT_LEN + NONCE_LEN, nonce, blob)


def seal(plaintext: bytes, keys: Keys) -> bytes:
    """Keyed format: no salt in the file, keys come from the vault."""
    nonce = secrets.token_bytes(NONCE_LEN)
    return _seal_with(keys, MAGIC2 + nonce, nonce, plaintext)


def unseal(blob: bytes, keys: Keys) -> bytes:
    if len(blob) < len(MAGIC2) + NONCE_LEN + TAG_LEN or not blob.startswith(MAGIC2):
        raise DecryptionError("not a memlog sealed file")
    off = len(MAGIC2)
    nonce = blob[off : off + NONCE_LEN]
    return _open_with(keys, off + NONCE_LEN, nonce, blob)


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
