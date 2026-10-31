# SPDX-License-Identifier: AGPL-3.0-or-later
"""What keeps the run at zero tokens, off the network and away from live files.

* ``check_environment`` stops the run when a variable looks like a key, a token, a
  proxy or a base URL. ``clean_environment`` builds the environment of a child from
  nothing: ``PATH``, ``HOME``, ``LANG``, ``PYTHONPATH``.
* ``scan_for_claude`` reads the program with ``ast`` and reports any call that could
  start ``claude``. ``path_has_claude`` checks a ``PATH`` for the binary.
* ``SocketGuard`` refuses every connect that is not loopback and counts the tries.
* ``OpenGuard`` refuses credential files and every write under a live path.
* ``claude_processes`` counts processes named ``claude`` under a given process.
* ``render_units`` returns the text of the two systemd user units. It installs
  nothing.
"""

from __future__ import annotations

import ast
import builtins
import io
import ipaddress
import os
import re
import socket
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .config import PARAMS

DENY_SUFFIXES = ("_API_KEY", "_TOKEN", "_PROXY", "_BASE_URL", "_AUTH_TOKEN", "_SECRET_KEY")
DENY_EXACT = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)
KEEP = ("PATH", "HOME", "LANG", "PYTHONPATH")
CREDENTIAL_NAMES = re.compile(
    r"(^|/)(token|\.credentials\.json|\.netrc|\.git-credentials|\.npmrc|\.pypirc|"
    r"id_rsa|id_ed25519|id_ecdsa|\.env|\.env\.[^/]+|credentials|[^/]*\.pem|[^/]*\.key)$",
    re.I,
)
SPAWNERS = {
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
    "system",
    "popen",
    "spawn",
    "spawnv",
    "spawnvp",
    "execv",
    "execvp",
    "execve",
    "create_subprocess_exec",
    "create_subprocess_shell",
}


def check_environment(environ: Mapping[str, str]) -> list[str]:
    """The names that stop the run. A name only, never a value."""
    bad = []
    for name in sorted(environ):
        upper = name.upper()
        if upper in DENY_EXACT or upper.endswith(DENY_SUFFIXES):
            bad.append(name)
    return bad


def clean_environment(environ: Mapping[str, str], **extra: str) -> dict[str, str]:
    """An environment from nothing: the four allowed names, then ``extra``."""
    env = {k: environ[k] for k in KEEP if k in environ}
    env.update(extra)
    return env


def path_has_claude(path: str) -> list[str]:
    return [d for d in path.split(os.pathsep) if d and (Path(d) / "claude").exists()]


def scan_for_claude(paths: Iterable[Path]) -> list[str]:
    """Calls that could start ``claude``: a spawn call whose arguments hold the
    string ``claude`` in a literal, and any literal that is exactly ``claude``
    inside a list or tuple that a spawn call takes. Returns ``file:line`` items."""
    found: list[str] = []
    for path in paths:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in SPAWNERS:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    words = re.split(r"[\s/]+", sub.value.strip())
                    if "claude" in words:
                        found.append(f"{path}:{node.lineno}")
                        break
    return found


# -- the socket guard -----------------------------------------------------------------


def is_loopback(address: Any) -> bool:
    if isinstance(address, tuple) and address:
        host = address[0]
    else:
        return True  # a unix socket path or an unknown form has no network
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a name: it would need a resolver, so it is not loopback


class SocketGuard:
    """Refuse every connect that is not loopback; count the tries. ``install`` is
    for the core unit only. The refused call raises ``ConnectionRefusedError``."""

    def __init__(self) -> None:
        self.refused = 0
        self.allowed = 0
        self.targets: list[str] = []
        self._orig: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _check(self, address: Any) -> None:
        if is_loopback(address):
            self.allowed += 1
            return
        with self._lock:
            self.refused += 1
            self.targets.append(str(address[0]) if isinstance(address, tuple) else "?")
        raise ConnectionRefusedError("long-run test: non-loopback connect refused")

    def install(self) -> SocketGuard:
        guard = self
        orig_connect = socket.socket.connect
        orig_ex = socket.socket.connect_ex
        orig_resolve = socket.getaddrinfo
        self._orig = {"connect": orig_connect, "connect_ex": orig_ex, "getaddrinfo": orig_resolve}

        def connect(sock, address):  # type: ignore[no-untyped-def]
            guard._check(address)
            return orig_connect(sock, address)

        def connect_ex(sock, address):  # type: ignore[no-untyped-def]
            guard._check(address)
            return orig_ex(sock, address)

        def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
            guard._check((host,))
            return orig_resolve(host, *args, **kwargs)

        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
        return self

    def uninstall(self) -> None:
        if self._orig:
            socket.socket.connect = self._orig["connect"]  # type: ignore[method-assign]
            socket.socket.connect_ex = self._orig["connect_ex"]  # type: ignore[method-assign]
            socket.getaddrinfo = self._orig["getaddrinfo"]  # type: ignore[assignment]
            self._orig = {}

    def __enter__(self) -> SocketGuard:
        return self.install()

    def __exit__(self, *exc: object) -> None:
        self.uninstall()


# -- the open guard -------------------------------------------------------------------

_WRITE_MODES = set("wax+")


class OpenGuard:
    """Wrap ``open``: refuse credential files, and refuse a write, anywhere under a
    live path. Every refusal is counted and recorded by name (never by content).
    A refused call raises ``PermissionError``."""

    def __init__(self, live_paths: Iterable[Path], allow_write: Iterable[Path] = ()) -> None:
        self.live = [Path(p).expanduser().resolve() for p in live_paths]
        self.allow = [Path(p).expanduser().resolve() for p in allow_write]
        self.refused_reads = 0
        self.refused_writes = 0
        self.events: list[str] = []
        self._orig: Any = None
        self._orig_io: Any = None

    def _under(self, path: Path, roots: list[Path]) -> bool:
        return any(path == r or r in path.parents for r in roots)

    def check(self, file: Any, mode: str = "r") -> None:
        if isinstance(file, int) or not isinstance(file, (str, bytes, os.PathLike)):
            return
        raw = os.fsdecode(file)
        path = Path(raw).expanduser()
        try:
            path = path.resolve()
        except OSError:
            return
        writing = bool(_WRITE_MODES & set(mode))
        if CREDENTIAL_NAMES.search(str(path)):
            self.refused_reads += 1
            self.events.append(f"credential:{path.name}")
            raise PermissionError(f"long-run test: {path.name} is a credential file")
        if writing and self._under(path, self.live) and not self._under(path, self.allow):
            self.refused_writes += 1
            self.events.append(f"write:{path.name}")
            raise PermissionError("long-run test: write under a live path refused")

    def install(self) -> OpenGuard:
        guard = self
        orig = builtins.open
        orig_io = io.open
        self._orig, self._orig_io = orig, orig_io

        def guarded(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
            guard.check(file, mode if isinstance(mode, str) else "r")
            return orig(file, mode, *args, **kwargs)

        builtins.open = guarded  # type: ignore[assignment]
        io.open = guarded  # type: ignore[assignment]
        return self

    def uninstall(self) -> None:
        if self._orig is not None:
            builtins.open = self._orig
            io.open = self._orig_io
            self._orig = None

    def __enter__(self) -> OpenGuard:
        return self.install()

    def __exit__(self, *exc: object) -> None:
        self.uninstall()


# -- processes ------------------------------------------------------------------------


def descendants(root_pid: int, proc: Path = Path("/proc")) -> list[int]:
    """The pids below ``root_pid`` (children, grandchildren), from ``/proc``."""
    parent_of: dict[int, int] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            # The name sits in parentheses and may hold spaces: split after the last one.
            parent_of[int(entry.name)] = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
    found, frontier = [], {root_pid}
    while frontier:
        nxt = {pid for pid, ppid in parent_of.items() if ppid in frontier and pid not in found}
        found.extend(sorted(nxt))
        frontier = nxt
    return found


def claude_processes(root_pid: int, proc: Path = Path("/proc")) -> int:
    """Processes named ``claude`` at or below ``root_pid``."""
    count = 0
    for pid in [root_pid, *descendants(root_pid, proc)]:
        try:
            if (proc / str(pid) / "comm").read_text().strip() == "claude":
                count += 1
        except OSError:
            continue
    return count


# -- the start check ------------------------------------------------------------------


def start_check(
    environ: Mapping[str, str], program: Iterable[Path], pid: int | None = None
) -> dict[str, Any]:
    """Everything the run checks before it starts. ``ok`` is false on any finding."""
    program = list(program)
    findings = {
        "env_names": check_environment(environ),
        "claude_on_path": path_has_claude(environ.get("PATH", "")),
        "claude_calls": scan_for_claude(program),
        "claude_processes": claude_processes(pid if pid is not None else os.getpid()),
    }
    findings["ok"] = not any(findings[k] for k in list(findings))
    return findings


# -- the units ------------------------------------------------------------------------


BIN_TOOLS = ("git", "sh", "env")


def make_bin(folder: Path) -> Path:
    """A ``PATH`` folder of symlinks to the few tools the harness needs. The system
    ``PATH`` can hold a ``claude`` binary (it does on this kind of host), so the unit
    gets this folder alone: it holds no ``claude``."""
    import shutil

    folder = Path(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in BIN_TOOLS:
        found = shutil.which(name, path="/usr/bin:/bin")
        link = folder / name
        if found and not link.exists():
            link.symlink_to(found)
    return folder


def render_units(
    run_dir: Path, python: str, tools_dir: Path, params=PARAMS, private_network: bool = True
) -> dict[str, str]:
    """The text of the two systemd user units. Nothing is written or started.
    ``private_network=False`` leaves ``PrivateNetwork`` out for a host whose user
    manager cannot set up a network namespace; the socket guard that the units put on
    ``PYTHONPATH`` (``write_guard_site``) then keeps the core unit off the network."""
    unset = " ".join(DENY_EXACT[:4] + ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"))
    common = f"""[Service]
Environment=PATH={run_dir}/bin
Environment=HOME=%h
Environment=LANG=C.UTF-8
Environment=PYTHONPATH={run_dir}/private/guard-site:{tools_dir}
UnsetEnvironment={unset}
UMask=0077
Nice=19
CPUWeight=10
CPUQuota={params.cpu_quota}
IOSchedulingClass=idle
MemoryMax={params.memory_max}
TasksMax=64
WorkingDirectory={run_dir}
"""
    net = "PrivateNetwork=yes\n" if private_network else ""
    core = f"""[Unit]
Description=NOBLIVION long-run test, core (no network)

{common}Type=simple
Restart=on-failure
RuntimeMaxSec=7d 2h
{net}ExecStart={python} -m longtest.cli run --run-dir {run_dir}

[Install]
WantedBy=default.target
"""
    local = f"""[Unit]
Description=NOBLIVION long-run test, localhost work only

{common}Type=simple
Restart=on-failure
RuntimeMaxSec=7d 2h
IPAddressDeny=any
IPAddressAllow=127.0.0.0/8
ExecStart={python} -m longtest.cli localnet --run-dir {run_dir}

[Install]
WantedBy=default.target
"""
    return {"nob-longtest.service": core, "nob-longtest-localnet.service": local}


# -- the guard for child processes ----------------------------------------------------

SITECUSTOMIZE = """\
# Written by the long-run test. Loaded at interpreter start through PYTHONPATH: it
# refuses every connect that is not loopback and appends one line per try to the log.
import ipaddress
import os
import socket

_LOG = {log!r}
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_resolve = socket.getaddrinfo


def _loopback(address):
    if not (isinstance(address, tuple) and address):
        return True
    host = address[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _refuse(address):
    try:
        fd = os.open(_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, (str(os.getpid()) + " refused\\n").encode())
        os.close(fd)
    except OSError:
        pass
    raise ConnectionRefusedError("long-run test: non-loopback connect refused")


def connect(sock, address):
    if not _loopback(address):
        _refuse(address)
    return _connect(sock, address)


def connect_ex(sock, address):
    if not _loopback(address):
        _refuse(address)
    return _connect_ex(sock, address)


def getaddrinfo(host, *args, **kwargs):
    if not _loopback((host,)):
        _refuse((host,))
    return _resolve(host, *args, **kwargs)


socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = (
    connect, connect_ex, getaddrinfo)
"""


def write_guard_site(folder: Path, log: Path) -> Path:
    """A folder with a ``sitecustomize.py`` that guards the sockets of any child
    python that has the folder on ``PYTHONPATH``. Returns the folder."""
    folder = Path(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (folder / "sitecustomize.py").write_text(SITECUSTOMIZE.format(log=str(log)), encoding="utf-8")
    return folder


def count_refused(log: Path) -> int:
    try:
        return len(Path(log).read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0
