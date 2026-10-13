"""Seal what leaves the machine: compressed, encrypted, and checked on the way back.

The storage a session travels through (sync.py) must never read it: a
transcript holds code, paths, hosts, sometimes a secret. So the bytes are
sealed HERE, before they leave, with a passphrase both machines know and the
storage never sees.

    sealed = MAGIC | salt (16) | tag (32) | openssl ciphertext
    tag    = HMAC-SHA256(key, MAGIC | salt | ciphertext)
    key    = PBKDF2-SHA256(passphrase, salt, ITERATIONS)

- The standard library has no cipher: `openssl enc -aes-256-cbc -pbkdf2`
  encrypts (it salts and derives its own key), which keeps docpin without a
  Python dependency. `openssl` ships with Linux and macOS.
- CBC alone does not tell a wrong passphrase from damaged bytes, and decrypts
  either into garbage at times: the HMAC, checked BEFORE decrypting, refuses
  both with one clear message.
- The passphrase reaches openssl through the environment of that one child,
  never its command line (which `ps` shows to anyone).
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import os
import secrets
import shutil
import subprocess

MAGIC = b"DPS1"
SALT_SIZE = 16
TAG_SIZE = 32
ITERATIONS = 200_000
CIPHER = ("enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(ITERATIONS), "-salt",
          "-pass", "env:DOCPIN_PASSPHRASE")
TIMEOUT = 120


class VaultError(Exception):
    """The bytes cannot be sealed, or what came back cannot be opened."""


def _openssl() -> str:
    found = shutil.which("openssl")
    if not found:
        raise VaultError("openssl is not on the PATH: install it to sync sessions")
    return found


def _run(args: list[str], data: bytes, passphrase: str) -> bytes:
    env = {**os.environ, "DOCPIN_PASSPHRASE": passphrase}
    try:
        done = subprocess.run([_openssl(), *args], input=data, capture_output=True,
                              env=env, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise VaultError(f"openssl failed: {exc}") from exc
    if done.returncode != 0:
        raise VaultError(f"openssl exited {done.returncode}: "
                         f"{done.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return done.stdout


def _key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, ITERATIONS)


def _check(passphrase: str) -> None:
    if not passphrase:
        raise VaultError("no passphrase set: choose one, the same on both machines")


def seal(data: bytes, passphrase: str) -> bytes:
    _check(passphrase)
    ciphertext = _run(list(CIPHER), gzip.compress(data, compresslevel=6), passphrase)
    salt = secrets.token_bytes(SALT_SIZE)
    tag = hmac.new(_key(passphrase, salt), MAGIC + salt + ciphertext, "sha256").digest()
    return MAGIC + salt + tag + ciphertext


def unseal(sealed: bytes, passphrase: str) -> bytes:
    """The bytes `seal` was given, or VaultError: a wrong passphrase and
    damaged bytes are refused alike, before anything is decrypted."""
    _check(passphrase)
    head = len(MAGIC) + SALT_SIZE + TAG_SIZE
    if len(sealed) <= head or not sealed.startswith(MAGIC):
        raise VaultError("not something docpin sealed")
    salt = sealed[len(MAGIC):len(MAGIC) + SALT_SIZE]
    tag, ciphertext = sealed[len(MAGIC) + SALT_SIZE:head], sealed[head:]
    expected = hmac.new(_key(passphrase, salt), MAGIC + salt + ciphertext, "sha256").digest()
    if not hmac.compare_digest(tag, expected):
        raise VaultError("wrong passphrase, or the file was damaged: set the same "
                         "passphrase on both machines")
    try:
        return gzip.decompress(_run([*CIPHER, "-d"], ciphertext, passphrase))
    except (OSError, EOFError) as exc:
        raise VaultError(f"cannot decompress: {exc}") from exc
