"""An OS-owned lease for the single-client FireTuner endpoint."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class ConnectionBusyError(ConnectionError):
    """Another local MCP process owns the tuner; restarting the game cannot help."""


class TunerLease:
    def __init__(self, host: str, port: int):
        endpoint = f"{host}:{port}"
        key = hashlib.sha256(endpoint.encode()).hexdigest()[:24]
        self.path = Path(tempfile.gettempdir()) / f"civ6-tuner-{key}.lock"
        self.endpoint = endpoint
        self._file = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        file = self.path.open("a+b")
        # Windows locks a byte range; ensure the byte exists first.
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            file.close()
            raise ConnectionBusyError(
                f"TUNER_BUSY: another civ6-mcp connection owns {self.endpoint}. "
                "Use one shared HTTP server or close the other client; "
                "do not restart the game or kill unrelated Python processes."
            ) from exc
        self._file = file

    def release(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            # Closing releases the kernel lock, including after process exit.
            file.close()
