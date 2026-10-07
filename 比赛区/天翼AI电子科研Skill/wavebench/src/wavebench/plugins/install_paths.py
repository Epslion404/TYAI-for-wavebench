from __future__ import annotations

from os import path as _path


def installation_path_key(path: str) -> str:
    """Compare wheel/RECORD paths using the local target platform's path rules.

    Windows folds case and separators; POSIX preserves case. Keep the original
    path in metadata and integrity hashes, using this key only for conflicts.
    """
    return _path.normcase(_path.normpath(path))
