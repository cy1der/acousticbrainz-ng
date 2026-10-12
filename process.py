import os
import subprocess
import threading
from typing import List

from .constants import ENV


class AnalysisCancelled(Exception):
    pass


class ProcessRunner:
    """Runs external programs, and kills all running ones when cancelled"""

    def __init__(self, log):
        self.log = log
        self._cancelled = threading.Event()
        self._processes: set = set()
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            for proc in self._processes:
                try:
                    proc.kill()
                except Exception:
                    pass

    def check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise AnalysisCancelled()

    def run(self, args: List[str]) -> subprocess.CompletedProcess:
        self.check_cancelled()
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=ENV,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        with self._lock:
            self._processes.add(proc)
        try:
            stdout, stderr = proc.communicate()
        finally:
            with self._lock:
                self._processes.discard(proc)
        self.check_cancelled()
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)

    def log_failure(self, description: str, proc: subprocess.CompletedProcess) -> None:
        self.log.error(f"{description} with exit code {proc.returncode}")
        if proc.stdout:
            self.log.error(f"stdout: {proc.stdout}")
        if proc.stderr:
            self.log.error(f"stderr: {proc.stderr}")
