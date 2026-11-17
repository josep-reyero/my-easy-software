"""Which build of mes is installed: ``__version__`` is the same for every commit, so the root
script of ``mes server plan`` also compares a content hash of the installed mes package.

Public API
----------
``build_id() -> str``  16 hex characters: SHA-256 of every file of the installed ``mes`` package
    (names and contents, sorted; bytecode left out).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def build_id() -> str:
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith((".pyc", ".pyo")):
                continue
            full = Path(dirpath) / name
            if not full.is_file() or full.is_symlink():
                continue
            rel = full.relative_to(root).as_posix()
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0" + full.read_bytes() + b"\0")
    return h.hexdigest()[:16]
