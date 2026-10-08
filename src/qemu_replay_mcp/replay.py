"""Replaying a recording under a gdbstub, with execution in both directions.

Behaviour of QEMU's replay mode that shapes this module (all observed on
QEMU 11.0 with the fixture kernels, see tests/test_qemu_integration.py):

* The gdbstub advertises ReverseStep+/ReverseContinue+ and accepts `bs`/`bc`.
  Reverse execution reloads the nearest snapshot before the current icount
  (here: `init`, at icount 0) and replays forward to find the target, so a
  reverse-continue costs time proportional to how far into the recording
  you are.
* A forward `c` while sitting on a breakpoint does NOT advance in replay
  mode - it re-reports the same hit at the same icount. We step over the
  breakpoint first, the way gdb does.
* A write watchpoint stops *after* the store; the pc points at the next
  instruction. One reverse-step lands on the instruction that wrote.
* Running past the end of a recording that ended with `quit` replays the
  quit and QEMU exits. We fence the end with QMP `replay-break` before
  every forward resume (reverse execution uses replay-break internally, so
  the fence can't be set once and left).
"""

from __future__ import annotations

import os
import subprocess
import time

from gdbstub_mcp.rsp import RSPError
from gdbstub_mcp.session import Session

from .qemu import QMP, QemuError, find_qemu, free_port
from .recording import Recording, _tail, machine_args


def reverse_resume(session: Session, step: bool) -> None:
    """Send `bs`/`bc`. gdbstub-mcp's RSPClient only exposes forward resume,
    so this mirrors RSPClient.resume() with the reverse packets."""
    rsp = session.rsp
    with rsp._lock:
        if rsp.running:
            raise RSPError("target is already running")
        rsp._send_packet(b"bs" if step else b"bc")
        rsp.running = True


class ReplaySession:
    def __init__(self, recording: Recording, elf: str | None, timeout_s: float = 20.0):
        self.recording = recording
        qemu = find_qemu(recording.arch)
        self.qmp_port = free_port()
        self.gdb_port = free_port()
        self.serial_log = os.path.join(recording.directory, "serial.replay.log")
        rr = recording.rr_file.replace("\\", "/")
        cmd = machine_args(qemu, recording.kernel, recording.memory_mb, recording.append,
                           recording.disk, self.serial_log, self.qmp_port) + [
            "-icount", f"shift={recording.icount_shift},rr=replay,rrfile={rr},rrsnapshot=init",
            "-gdb", f"tcp:127.0.0.1:{self.gdb_port}", "-S",
        ]
        self._stderr_path = os.path.join(recording.directory, "qemu-replay.stderr")
        with open(self._stderr_path, "wb") as err:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
        try:
            try:
                self.qmp = QMP(self.qmp_port, connect_timeout=timeout_s)
            except QemuError:
                self.proc.kill()
                self.proc.wait()
                raise QemuError(f"replay QEMU failed to start: {_tail(self._stderr_path)}") from None
            self.session = Session(recording.name, "127.0.0.1", self.gdb_port,
                                   elf or recording.kernel, 0, timeout_s)
            features = self.session.rsp.features
            features_ok = features.get("ReverseStep") == "+" and features.get("ReverseContinue") == "+"
            if not features_ok:
                raise QemuError("this QEMU's gdbstub does not advertise ReverseStep/ReverseContinue")
        except Exception:
            self.kill()
            raise

    # -- state -------------------------------------------------------------

    def icount(self) -> int:
        return int(self.qmp.command("query-replay")["icount"])

    def pc(self) -> int:
        return self.session.read_reg(self.session.layout.pc().name)

    def position(self) -> str:
        end = self.recording.end_icount
        ic = self.icount()
        pct = f" ({100 * ic / end:.1f}%)" if end else ""
        return f"icount {ic:,} of {end:,}{pct}"

    # -- execution -----------------------------------------------------------

    def _fence(self) -> None:
        end = self.recording.end_icount
        if end and self.icount() < end - 1:
            self.qmp.command("replay-break", icount=end - 1)

    def _at_end(self) -> bool:
        end = self.recording.end_icount
        return bool(end) and self.icount() >= end - 1

    def _step_over_breakpoint(self) -> bool:
        """Replay mode re-reports a breakpoint you're sitting on instead of
        moving past it; single-step with it removed, like gdb does.
        Returns True if a step was taken."""
        pc = self.pc()
        here = [b for b in self.session.breakpoints.values()
                if b.address == pc and b.kind in ("sw", "hw")]
        if not here:
            return False
        kinds = {"sw": 0, "hw": 1}
        for b in here:
            self.session.rsp.clear_breakpoint(kinds[b.kind], b.address, b.length)
        try:
            self.session.rsp.resume(step=True)
            if self.session.rsp.wait_stop(self.session.rsp.read_timeout) is None:
                raise RSPError("target did not stop after stepping over a breakpoint")
        finally:
            for b in here:
                self.session.rsp.set_breakpoint(kinds[b.kind], b.address, b.length)
        return True

    def forward(self, timeout_s: float) -> tuple[bytes | None, str]:
        """Continue forward. Returns (stop reply or None if still running, note)."""
        if self._at_end():
            return None, "already at the end of the recording - go backwards or replay_goto"
        self._step_over_breakpoint()
        self._fence()
        self.session.rsp.resume()
        return self.session.rsp.wait_stop(timeout_s), ""

    def reverse(self, step: bool, timeout_s: float) -> bytes | None:
        reverse_resume(self.session, step)
        return self.session.rsp.wait_stop(timeout_s)

    def step(self, count: int) -> bytes:
        reply = b""
        for _ in range(count):
            if self._at_end():
                break
            if self._step_over_breakpoint():
                reply = self.session.rsp.stop_reason()
                continue
            self.session.rsp.resume(step=True)
            r = self.session.rsp.wait_stop(self.session.rsp.read_timeout)
            if r is None:
                raise RSPError("target did not stop after a single step")
            reply = r
        return reply or self.session.rsp.stop_reason()

    def reverse_step(self, count: int, timeout_s: float) -> bytes:
        reply = b""
        for _ in range(count):
            if self.icount() == 0:
                break
            r = self.reverse(True, timeout_s)
            if r is None:
                raise RSPError(f"reverse step did not finish within {timeout_s}s")
            reply = r
        return reply or self.session.rsp.stop_reason()

    def goto(self, icount: int) -> None:
        end = self.recording.end_icount
        if icount < 0 or (end and icount >= end):
            raise ValueError(f"icount must be between 0 and {end - 1:,}" if end else "icount must be >= 0")
        if self.session.rsp.running:
            raise RSPError("target is running - call replay_interrupt first")
        self.qmp.command("replay-seek", icount=icount)
        # replay-seek is asynchronous; wait until QEMU reports the position.
        deadline = time.monotonic() + 120
        while self.icount() != icount:
            if time.monotonic() > deadline:
                raise QemuError(f"replay-seek to {icount} did not complete")
            time.sleep(0.05)

    # -- teardown ------------------------------------------------------------

    def kill(self) -> None:
        sess = getattr(self, "session", None)
        if sess is not None:
            try:
                sess.close()
            except OSError:
                pass
        q = getattr(self, "qmp", None)
        if q is not None:
            q.close()
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
