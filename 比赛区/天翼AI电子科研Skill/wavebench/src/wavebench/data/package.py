from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

def safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", label.strip())
    return cleaned.strip("_") or "capture"

def new_package_dir(base: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Keep the label suffix used to locate failed captures, even at frozen time.
    return base / f"{stamp}_{uuid4().hex}_{safe_label(label)}"


def write_new_file(path: Path, content: bytes) -> None:
    """Publish a complete file without replacing existing evidence.

    A same-directory temporary file is flushed and closed before its hard link
    is published. Linking fails closed on unsupported filesystems or an existing
    destination; there is no fallback to a partially visible exclusive write.
    Ordinary failures remove the temporary file. An abrupt process termination
    can leave a hidden .tmp file, which is never a published artifact.
    """
    temporary: Path | None = None
    failure: BaseException | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                if failure is not None:
                    failure.add_note(f"temporary artifact cleanup failed: {temporary}: {exc}")
                else:
                    # Publication already succeeded. Report leftover staging
                    # evidence without telling the caller to repeat the write.
                    logging.getLogger(__name__).warning(
                        "artifact published but temporary cleanup failed: %s: %s",
                        temporary, exc,
                    )
