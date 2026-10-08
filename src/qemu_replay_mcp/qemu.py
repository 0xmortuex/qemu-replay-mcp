"""Locating QEMU binaries and talking QMP.

Record/replay needs two binaries: `qemu-system-<arch>` and `qemu-img`
(snapshots live in a qcow2 image, so every recording gets one).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from typing import Any

_WINDOWS_DIRS = (r"C:\Program Files\qemu", r"C:\Program Files (x86)\qemu")


class QemuError(RuntimeError):
    pass


def _search_dirs() -> list[str]:
    dirs = []
    if os.environ.get("QEMU_DIR"):
        dirs.append(os.environ["QEMU_DIR"])
    if sys.platform == "win32":
        dirs.extend(_WINDOWS_DIRS)
    return dirs


def find_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in _search_dirs():
        candidate = os.path.join(d, exe)
        if os.path.isfile(candidate):
            return candidate
    raise QemuError(
        f"{name} not found. Install QEMU and put it on PATH"
        + (" (or set QEMU_DIR)." if sys.platform == "win32" else ".")
    )


def find_qemu(arch: str) -> str:
    if not arch or any(c in arch for c in "/\\") or arch in (".", ".."):
        raise ValueError(f"invalid arch {arch!r}")
    return find_binary(f"qemu-system-{arch}")


def find_qemu_img(qemu: str) -> str:
    # Prefer the qemu-img shipped next to the emulator, so versions match.
    sibling = os.path.join(os.path.dirname(qemu), "qemu-img" + (".exe" if sys.platform == "win32" else ""))
    if os.path.isfile(sibling):
        return sibling
    return find_binary("qemu-img")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


class QMP:
    """Minimal synchronous QMP client that remembers the events it skips."""

    def __init__(self, port: int, connect_timeout: float = 20.0, read_timeout: float = 30.0):
        deadline = time.monotonic() + connect_timeout
        last: OSError | None = None
        sock: socket.socket | None = None
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError as e:
                last = e
                time.sleep(0.1)
        if sock is None:
            raise QemuError(f"could not connect to QEMU's QMP socket on port {port}: {last}")
        sock.settimeout(read_timeout)
        self.sock = sock
        self._file = sock.makefile("rwb")
        self.events: list[dict[str, Any]] = []
        self._read()  # greeting
        self.command("qmp_capabilities")

    def _read(self) -> dict[str, Any]:
        try:
            line = self._file.readline()
        except OSError as e:
            raise QemuError(f"QMP connection error: {e}") from None
        if not line:
            raise QemuError("QMP connection closed - QEMU exited")
        msg: dict[str, Any] = json.loads(line)
        return msg

    def command(self, name: str, **arguments: Any) -> Any:
        msg: dict[str, Any] = {"execute": name}
        if arguments:
            msg["arguments"] = arguments
        try:
            self._file.write(json.dumps(msg).encode() + b"\n")
            self._file.flush()
        except OSError as e:
            raise QemuError(f"QMP connection error: {e}") from None
        while True:
            resp = self._read()
            if "event" in resp:
                self.events.append(resp)
                continue
            if "error" in resp:
                raise QemuError(f"{name}: {resp['error'].get('desc', resp['error'])}")
            return resp.get("return")

    def quit(self, proc: subprocess.Popen[bytes], timeout: float = 30.0) -> None:
        """Ask QEMU to exit and wait for it.

        QEMU may close the QMP socket before its reply arrives (8.2 does; 11.0
        replies first), so a dropped connection after `quit` is the expected
        outcome, not an error. What counts is that the process exits.
        """
        try:
            self.command("quit")
        except QemuError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise QemuError(f"QEMU did not exit within {timeout}s of `quit`") from None

    def close(self) -> None:
        try:
            self._file.close()
            self.sock.close()
        except OSError:
            pass
