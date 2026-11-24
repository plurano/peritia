# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Compute SHA-1 Git blob IDs as defined by the Git object format.

https://git-scm.com/docs/hash-function-transition#_object_names
"""
from __future__ import annotations

import hashlib
import io
from typing import BinaryIO

GIT_BLOB_CHUNK = 65_536


def git_blob_id(data: bytes) -> str:
    """Compute the Git blob object ID for bytes in memory."""
    return _hash_git_blob(io.BytesIO(data), len(data))


def _hash_git_blob(handle: BinaryIO, size: int) -> str:
    """Hash the Git object header and the declared number of content bytes."""
    digest = hashlib.sha1(f"blob {size}\0".encode(), usedforsecurity=False)
    for offset in range(0, size, GIT_BLOB_CHUNK):
        expected = min(GIT_BLOB_CHUNK, size - offset)
        chunk = handle.read(expected)
        if len(chunk) != expected:
            raise OSError("Git blob source was truncated while reading")
        digest.update(chunk)
    return digest.hexdigest()
