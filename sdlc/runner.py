"""Bounded POSIX check execution. This is supervision, not a sandbox."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from .schema import HarnessError
from .workspace import contained


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def execute(root: Path, check: dict, config: dict, timeout: float, log: Path, on_started) -> dict:
    if os.name != "posix":
        raise HarnessError("Check execution requires Linux/macOS/WSL (POSIX process groups)")
    cwd = contained(root, check["cwd"])
    if not cwd.is_dir():
        raise HarnessError(f"Check cwd is not a directory: {check['cwd']}")
    environment = {key: os.environ[key] for key in config["env_allowlist"] if key in os.environ}
    environment.update(check["env"])
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    process = None
    status, reason, total = None, "", 0
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("xb") as output:
        try:
            process = subprocess.Popen(check["argv"], cwd=cwd, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       shell=False, close_fds=True)
            on_started(process.pid)
            assert process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                pipe_open = True
                while pipe_open:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        status, reason = "timeout", f"Exceeded {timeout:.3f}s wall-clock budget"
                        break
                    events = selector.select(min(0.05, remaining))
                    for key, _ in events:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            pipe_open = False
                            break
                        remaining_bytes = config["max_log_bytes"] - total
                        output.write(chunk[:remaining_bytes])
                        total += min(len(chunk), remaining_bytes)
                        if len(chunk) > remaining_bytes:
                            status, reason = "output_limit", "Check exceeded the configured log byte limit"
                            pipe_open = False
                            break
                    # A finished check must not leave children holding its pipe open.
                    if process.poll() is not None:
                        kill_group(process)
                if status is None:
                    remaining = max(0.001, timeout - (time.monotonic() - started))
                    try:
                        process.wait(timeout=remaining)
                        status = "pass" if process.returncode == 0 else "fail"
                        reason = f"Exited with code {process.returncode}"
                    except subprocess.TimeoutExpired:
                        status, reason = "timeout", f"Exceeded {timeout:.3f}s wall-clock budget"
        except FileNotFoundError as exc:
            status, reason = "error", f"Executable or cwd not found: {exc.filename}"
        except KeyboardInterrupt:
            status, reason = "interrupted", "Interrupted by operator; no success evidence recorded"
        finally:
            if process is not None:
                kill_group(process)
                process.wait()
                if process.stdout:
                    process.stdout.close()
            output.flush()
            os.fsync(output.fileno())
    return {"status": status, "reason": reason, "returncode": process.returncode if process else None,
            "duration_seconds": time.monotonic() - started, "log_bytes": total}
