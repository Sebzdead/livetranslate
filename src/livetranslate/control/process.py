"""Run the livetranslate pipeline as a child process; capture logs; stop gracefully.

Graceful stop sends SIGINT (CTRL_BREAK_EVENT on Windows) so runner.run_live
drains and closes the session store; kill() is the timeout fallback.
"""
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path


class PipelineProcess:
    def __init__(self, project_root, config_path="config.toml", cmd=None):
        self.project_root = Path(project_root)
        self.config_path = config_path
        self.cmd = cmd or [sys.executable, "-u", "-m", "livetranslate",
                           "--config", config_path]
        self.proc = None
        self.started_at = None
        self.last_exit = None
        self._log = deque(maxlen=2000)
        self._log_seq = 0            # lines ever appended (ring may have dropped early ones)
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, extra_env: dict) -> None:
        if self.running():
            raise RuntimeError("pipeline already running")
        env = {**os.environ, **extra_env}
        kwargs = {}
        if sys.platform == "win32":
            # New process group so CTRL_BREAK_EVENT reaches only the child.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            self.cmd, cwd=str(self.project_root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, **kwargs)
        self.started_at = time.time()
        self.last_exit = None
        threading.Thread(target=self._pump, name="pipeline-log-pump",
                         daemon=True).start()

    def _pump(self) -> None:
        proc = self.proc
        for line in proc.stdout:
            with self._lock:
                self._log.append(line.rstrip("\n"))
                self._log_seq += 1
        code = proc.wait()
        with self._lock:
            self.last_exit = code
            self._log.append(f"--- pipeline exited with code {code} ---")
            self._log_seq += 1

    def logs_since(self, after: int):
        """Return (new_lines, cursor). Poll with the returned cursor."""
        with self._lock:
            dropped = self._log_seq - len(self._log)
            start = max(after - dropped, 0)
            return list(self._log)[start:], self._log_seq

    def stop(self, grace_s: float = 10.0) -> None:
        if not self.running():
            return
        if sys.platform == "win32":
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
