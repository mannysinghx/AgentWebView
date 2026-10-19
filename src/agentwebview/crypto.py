"""Provider keys are encrypted at rest (AES-256-GCM) with a key file that never leaves the data folder.

Each ciphertext is bound to its row (the key record's id and provider), so a value copied onto another
row does not decrypt. Losing ``secret.key`` makes the stored provider keys unreadable; they can be added
again on the Providers screen.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"


class KeyBox:
    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self._aead = AESGCM(self._load_or_create())

    def _load_or_create(self) -> bytes:
        if self.key_path.exists():
            data = self.key_path.read_bytes()
            if len(data) != 32:
                raise RuntimeError(f"{self.key_path} is not a 32-byte key; restore it or move it away")
            return data
        key = secrets.token_bytes(32)
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key

    def encrypt(self, plaintext: str, aad: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return _VERSION + nonce + self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))

    def decrypt(self, blob: bytes, aad: str) -> str:
        if not blob or blob[:1] != _VERSION:
            raise ValueError("unknown ciphertext format")
        nonce, ct = blob[1:13], blob[13:]
        return self._aead.decrypt(nonce, ct, aad.encode("utf-8")).decode("utf-8")
