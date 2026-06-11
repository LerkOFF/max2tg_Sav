from __future__ import annotations

import os
import sys
from pathlib import Path


def _candidate_paths() -> list[Path]:
    root = Path(__file__).resolve().parent
    env_path = os.getenv("MAXAPI_REPO_PATH")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(
        [
            root / "vendor" / "MaxAPI",
            root / "external" / "MaxAPI",
            root.parent / "MaxAPI",
            root.parent / "maxapi",
        ]
    )
    return candidates


def bootstrap_maxapi() -> Path | None:
    try:
        import max_proto  # noqa: F401
        return None
    except ImportError:
        pass

    for candidate in _candidate_paths():
        if (candidate / "max_proto" / "__init__.py").exists():
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                sys.path.insert(0, candidate_str)
            return candidate

    searched = ", ".join(str(path) for path in _candidate_paths())
    raise RuntimeError(
        "MaxAPI checkout not found. "
        "Set MAXAPI_REPO_PATH or place a clone in one of: "
        f"{searched}"
    )
