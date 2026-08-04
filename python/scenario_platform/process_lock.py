"""Cross-process lock adapter with a dependency-free POSIX fallback.

Packaged applications install ``filelock``.  The small fallback keeps the
domain-neutral source and GI-only CI environment importable on POSIX without
silently weakening cross-process ownership.  Non-POSIX packaged targets must
provide the audited dependency.
"""

from __future__ import annotations

import errno
import os
import stat
import time
from pathlib import Path
from typing import Any

try:  # pragma: no cover - normal packaged path
    from filelock import FileLock
    from filelock import Timeout as FileLockTimeout
except ImportError:  # pragma: no cover - exercised in the lean GI CI environment
    if os.name == "posix":
        import fcntl

        class FileLockTimeout(TimeoutError):
            """The trusted lock remained owned past the bounded wait."""

        class FileLock:
            """Minimal flock-backed API compatible with the calls used by the worker."""

            def __init__(self, lock_file: str, *, thread_local: bool = False) -> None:
                del thread_local
                self.lock_file = Path(lock_file)
                self._descriptor: int | None = None

            def acquire(self, *, timeout: float = -1) -> FileLock:
                if self._descriptor is not None:
                    return self
                flags = os.O_RDWR | os.O_CREAT
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.lock_file, flags, 0o600)
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or (
                    hasattr(os, "getuid") and metadata.st_uid != os.getuid()
                ):
                    os.close(descriptor)
                    raise OSError("process lock must be a user-owned regular file")
                deadline = None if timeout < 0 else time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self._descriptor = descriptor
                        return self
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                            os.close(descriptor)
                            raise
                        if deadline is not None and time.monotonic() >= deadline:
                            os.close(descriptor)
                            raise FileLockTimeout(str(self.lock_file)) from exc
                        time.sleep(0.02)

            def release(self, *_args: Any, **_kwargs: Any) -> None:
                descriptor = self._descriptor
                if descriptor is None:
                    return
                self._descriptor = None
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    else:

        class FileLockTimeout(TimeoutError):
            pass

        class FileLock:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError(
                    "the ecocorridor filelock dependency is required on non-POSIX targets"
                )
