"""Recording a kernel run with QEMU's deterministic record/replay.

A recording is a directory:

    rec.bin       QEMU's replay log (every non-deterministic input, by icount)
    disk.qcow2    an empty qcow2 that holds the VM snapshot taken at icount 0
                  (`rrsnapshot=init`) - reverse execution restarts from it
    serial.log    the guest's serial output while recording
    int.log       QEMU `-d int,cpu_reset` log (feed it to gdbstub-mcp's
                  debug_explain_fault)
    meta.json     how to replay it: kernel, arch, icount shift, final icount

Deterministic replay requires the exact same machine on both sides, so the
QEMU command line is built in one place (machine_args) and reused verbatim.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass

from .qemu import QMP, QemuError, find_qemu, find_qemu_img, free_port


@dataclass
class Recording:
    name: str
    directory: str
    kernel: str
    arch: str
    memory_mb: int
    append: str | None
    icount_shift: int
    end_icount: int
    ended_by: str  # "guest-crash" | "guest-shutdown" | "timeout" | "serial-marker"
    duration_s: float

    @property
    def rr_file(self) -> str:
        return os.path.join(self.directory, "rec.bin")

    @property
    def disk(self) -> str:
        return os.path.join(self.directory, "disk.qcow2")

    def save(self) -> None:
        with open(os.path.join(self.directory, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)

    @classmethod
    def load(cls, directory: str) -> Recording:
        path = os.path.join(directory, "meta.json")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no recording in {directory} (meta.json missing)")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(**data)


def machine_args(qemu: str, kernel: str, memory_mb: int, append: str | None,
                 disk: str, serial_log: str, qmp_port: int) -> list[str]:
    """The VM definition shared by record and replay."""
    disk_path = disk.replace("\\", "/")
    args = [
        qemu, "-display", "none", "-m", str(memory_mb), "-smp", "1",
        "-kernel", kernel,
        # Record/replay supports only what it can log; no NIC keeps it simple.
        "-net", "none",
        # A triple fault becomes a shutdown (-no-reboot), and the shutdown
        # pauses the VM instead of exiting (-no-shutdown): the recording ends
        # exactly at the crash and QEMU stays alive to report its icount.
        "-no-reboot", "-no-shutdown",
        # Snapshots need a qcow2 image, and every block device must sit
        # behind blkreplay so disk I/O is replayed deterministically.
        "-drive", f"file={disk_path},if=none,id=rr-disk",
        "-drive", "driver=blkreplay,if=none,image=rr-disk,id=rr-blkreplay",
        "-device", "ide-hd,drive=rr-blkreplay",
        "-serial", "file:" + serial_log.replace("\\", "/"),
        "-qmp", f"tcp:127.0.0.1:{qmp_port},server=on,wait=off",
    ]
    if append:
        args += ["-append", append]
    return args


def record(name: str, directory: str, kernel: str, arch: str = "i386", memory_mb: int = 128,
           append: str | None = None, timeout_s: float = 10.0, until_serial: str | None = None,
           icount_shift: int = 7) -> Recording:
    if not os.path.isfile(kernel):
        raise FileNotFoundError(f"kernel not found: {kernel}")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if not 0 <= icount_shift <= 10:
        raise ValueError("icount_shift must be between 0 and 10")
    qemu = find_qemu(arch)
    if os.path.exists(os.path.join(directory, "meta.json")):
        raise QemuError(f"{directory} already holds a recording - pick another name or directory")
    os.makedirs(directory, exist_ok=True)
    rec = Recording(name, os.path.abspath(directory), os.path.abspath(kernel), arch, memory_mb,
                    append, icount_shift, 0, "", 0.0)
    subprocess.run([find_qemu_img(qemu), "create", "-q", "-f", "qcow2", rec.disk, "16M"],
                   check=True, capture_output=True)
    serial_log = os.path.join(rec.directory, "serial.log")
    qmp_port = free_port()
    rr = rec.rr_file.replace("\\", "/")
    cmd = machine_args(qemu, rec.kernel, memory_mb, append, rec.disk, serial_log, qmp_port) + [
        "-icount", f"shift={icount_shift},rr=record,rrfile={rr},rrsnapshot=init",
        "-d", "int,cpu_reset", "-D", os.path.join(rec.directory, "int.log").replace("\\", "/"),
    ]
    stderr_path = os.path.join(rec.directory, "qemu-record.stderr")
    with open(stderr_path, "wb") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
    start = time.monotonic()
    try:
        try:
            qmp = QMP(qmp_port)
        except QemuError:
            proc.kill()
            proc.wait()
            raise QemuError(f"QEMU failed to start for recording: {_tail(stderr_path)}") from None
        ended_by = "timeout"
        while time.monotonic() - start < timeout_s:
            status = qmp.command("query-status")
            if not status["running"]:
                # A fast crash can happen before QMP connects, so its SHUTDOWN
                # event may be lost; QEMU's own interrupt log is authoritative.
                crashed = "Triple fault" in _read(os.path.join(rec.directory, "int.log"))
                ended_by = "guest-crash" if crashed else "guest-shutdown"
                break
            if until_serial and until_serial in _read(serial_log):
                ended_by = "serial-marker"
                break
            time.sleep(0.05)
        rec.end_icount = int(qmp.command("query-replay")["icount"])
        rec.ended_by = ended_by
        rec.duration_s = round(time.monotonic() - start, 2)
        qmp.quit(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if not os.path.isfile(rec.rr_file) or os.path.getsize(rec.rr_file) == 0:
        raise QemuError(f"QEMU wrote no replay log: {_tail(stderr_path)}")
    rec.save()
    return rec


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def _tail(path: str, n: int = 800) -> str:
    text = _read(path).strip()
    return text[-n:] if text else "(no output)"
