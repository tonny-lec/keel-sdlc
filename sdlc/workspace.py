"""Content identity, contained paths, and durable local artifacts."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import platform
import shutil
import stat
import tempfile
from pathlib import Path

from .schema import HarnessError, relative_path


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def contained(root: Path, relative: str, *, internal: bool = False) -> Path:
    relative_path(relative, allow_internal=internal)
    path = root / relative
    current = root
    for component in Path(relative).parts:
        current = current / component
        if current.is_symlink():
            raise HarnessError(f"Symlink is not allowed in a managed path: {relative}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise HarnessError(f"Path escapes workspace: {relative}")
    return path


def file_hash(path: Path) -> str:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise HarnessError(f"Expected a regular file: {path}")
    hasher = hashlib.sha256()
    # O_NOFOLLOW closes the final-component symlink race on POSIX.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise HarnessError(f"File changed while opening: {path}")
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
        after = os.fstat(source.fileno())
    final = path.lstat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
    if identity(before) != identity(after) or identity(after) != identity(final):
        raise HarnessError(f"File changed while hashing: {path}; retry on a stable workspace")
    return hasher.hexdigest()


def snapshot(root: Path, config: dict, manifest: dict) -> dict:
    files = {}
    ignored = {".git", ".sdlc", *config["exclude_dirs"]}

    def add(path: Path) -> None:
        key = path.relative_to(root).as_posix()
        files[key] = {"sha256": file_hash(path), "executable": bool(path.stat().st_mode & 0o111)}

    def walk(path: Path) -> None:
        if path.is_symlink():
            raise HarnessError(f"Source symlink requires an explicit materialized input: {path}")
        if path.is_dir():
            for child in sorted(path.iterdir()):
                if child.name in {".git", ".sdlc"} or (child.name in ignored and child.is_dir()):
                    continue
                walk(child)
        elif path.is_file():
            add(path)
        else:
            raise HarnessError(f"Source input is missing or not a regular file: {path}")

    for entry in config["source_roots"]:
        walk(contained(root, entry))
    artifacts = {}
    for entry in manifest["release"]["artifacts"]:
        path = contained(root, entry)
        artifacts[entry] = file_hash(path) if path.exists() else None
    environment = {key: os.environ.get(key) for key in config["env_allowlist"]}
    executables = {}
    for check in config["checks"].values():
        command = check["argv"][0]
        cwd = contained(root, check["cwd"])
        if "/" in command:
            path = (cwd / command).resolve()
        else:
            search_path = check["env"].get("PATH", environment.get("PATH"))
            search_path = os.defpath if search_path is None else search_path
            absolute_search = os.pathsep.join(str((cwd / entry).resolve()) for entry in search_path.split(os.pathsep))
            found = shutil.which(command, path=absolute_search)
            path = Path(found).resolve() if found else None
        key = str(path) if path else command
        if key not in executables:
            executables[key] = {"sha256": file_hash(path), "executable": bool(path.stat().st_mode & 0o111)} if path and path.is_file() else None
    runtime = digest({"environment": environment, "executables": executables,
                      "platform": platform.platform(), "python": platform.python_version(),
                      "harness": {entry.name: hashlib.sha256(entry.read_bytes()).hexdigest()
                                  for entry in importlib.resources.files("sdlc").iterdir()
                                  if entry.name.endswith(".py") and entry.is_file()}})
    return {"digest": digest({"files": files, "artifacts": artifacts}), "runtime": runtime,
            "files": files, "artifacts": artifacts}


def atomic_write(path: Path, content: str | bytes, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    if exclusive:
        with path.open("xb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
    else:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    if os.name == "posix":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def write_json(path: Path, value, *, exclusive: bool = False) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", exclusive=exclusive)
