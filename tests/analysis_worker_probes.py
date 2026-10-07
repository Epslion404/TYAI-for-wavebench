"""Spawn targets with no numerical or pytest imports, for worker evidence tests."""
import os


def allocation_probe(*, output):
    # Persist probe-specific evidence before attempting the forbidden allocation.
    from pathlib import Path
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'probe_started').write_text('512 MiB bytearray', encoding='utf-8')
    bytearray(512 * 1024**2)
    (output / 'allocation_succeeded').write_text('limit not enforced', encoding='utf-8')
    raise RuntimeError('allocation unexpectedly exceeded the hard limit')


def native_crash():
    os._exit(7)


class CrashDuringBootstrap:
    def __reduce__(self):
        return native_crash, ()
