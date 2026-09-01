"""Environment loading.

Reads a `.env` file at the repo root into `os.environ` without pulling in a
dependency, so `recon/` stays standard-library only and a reviewer can clone
the repo and paste credentials into a file rather than exporting shell
variables. Real environment variables always win over the file.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

_loaded = False


def load_env(path: Path | None = None) -> dict[str, str]:
    """Load `.env` once. Returns what was read, for diagnostics."""
    global _loaded
    target = path or ENV_FILE
    found: dict[str, str] = {}
    if not target.exists():
        _loaded = True
        return found

    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        found[key] = value
        # An exported variable is a deliberate override; never clobber it.
        os.environ.setdefault(key, value)

    _loaded = True
    return found


def ensure_loaded() -> None:
    if not _loaded:
        load_env()
