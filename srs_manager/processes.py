"""Own only processes created by this application; never kill by executable name."""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import subprocess
import threading
import time


class ProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._children: set[subprocess.Popen] = set()
        self._closing = False
        atexit.register(self.close)

    def spawn(self, command: list[str], *, cwd: Path | None = None,
              stdin: bool = False) -> subprocess.Popen:
        with self._lock:
            if self._closing:
                raise RuntimeError("程序正在关闭")
            process = subprocess.Popen(
                command, cwd=cwd, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self._children.add(process)
            return process

    def release(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            self.stop(process)
        with self._lock:
            self._children.discard(process)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe:
                pipe.close()

    @staticmethod
    def stop(process: subprocess.Popen, *, graceful: bool = False, timeout: float = 1.5) -> None:
        if process.poll() is not None:
            return
        if graceful and process.stdin:
            try:
                process.stdin.write(b"q\n")
                process.stdin.flush()
                process.wait(timeout=timeout)
                return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        with self._lock:
            self._closing = True
            children = list(self._children)
        # Stop in parallel so two blocked decoders cannot lengthen GUI shutdown.
        threads = [threading.Thread(target=self.stop, args=(p,), daemon=True) for p in children]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 4
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))

    @property
    def active_pids(self) -> list[int]:
        with self._lock:
            return [p.pid for p in self._children if p.poll() is None]
