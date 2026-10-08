"""qemu-replay-mcp: time-travel debugging of kernels with QEMU record/replay."""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import Any, TypeVar

from gdbstub_mcp.rsp import RSPError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import recording as recmod
from .qemu import QemuError
from .recording import Recording
from .replay import ReplaySession

mcp = MCPServer(
    "qemu-replay",
    instructions=(
        "Time-travel debugging for kernels in QEMU. replay_record runs a kernel once "
        "under deterministic recording (until it crashes, prints a marker, or a timeout). "
        "replay_start replays that exact run under a debugger, and you can move BACKWARDS: "
        "replay_reverse_step, replay_reverse_continue (to the previous breakpoint/watchpoint "
        "hit), replay_goto (any instruction count). The key tool is replay_last_write: from "
        "wherever you are (e.g. the crash), it finds the instruction that last wrote a "
        "variable or address - the usual way to find memory corruption. Typical loop: "
        "replay_record -> replay_start -> replay_break crash/panic symbol -> replay_continue "
        "-> replay_last_write <corrupted variable>. Every replay is identical, so findings "
        "are reproducible. Addresses accept 0x1234, symbol, symbol+0x10, file.c:42, $esp+8."
    ),
)

_replays: dict[str, ReplaySession] = {}
_recordings: dict[str, Recording] = {}

F = TypeVar("F", bound=Callable[..., Any])

# Failures an agent can act on. MCPServer only forwards a ToolError's message
# to the client - anything else arrives as a bare "Error executing tool X" -
# so these are re-raised as ToolError with their text intact.
_EXPECTED = (RSPError, QemuError, KeyError, ValueError, FileNotFoundError, OSError)


def tool(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except _EXPECTED as e:
            msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            raise ToolError(str(msg)) from e

    mcp.tool()(wrapper)
    return fn


def _default_dir(name: str) -> str:
    root = os.environ.get("QEMU_REPLAY_DIR") or os.path.join(
        os.path.expanduser("~"), ".qemu-replay-mcp", "recordings")
    return os.path.join(root, name)


def _check_name(name: str) -> None:
    if not name or any(c in name for c in "/\\") or name in (".", ".."):
        raise ValueError(f"invalid name {name!r}: must be non-empty with no path separators")


def _get(name: str) -> ReplaySession:
    r = _replays.get(name)
    if r is None:
        known = ", ".join(sorted(_replays)) or "none"
        raise RSPError(f"no replay named {name!r} (active: {known}) - call replay_start first")
    if r.proc.poll() is not None:
        del _replays[name]
        raise QemuError(f"the replay QEMU for {name!r} has exited - call replay_start again")
    return r


def _load(name: str, directory: str | None) -> Recording:
    if directory is None and name in _recordings:
        return _recordings[name]
    rec = Recording.load(directory or _default_dir(name))
    _recordings[rec.name] = rec
    return rec


def _call_stack(r: ReplaySession, max_frames: int) -> tuple[list[str], str]:
    """gdbstub-mcp's backtrace, with caller frames attributed to the CALL line.

    A caller frame's address is a return address - the instruction after the
    call - so looking it up directly names the line after the call. Debuggers
    look up return_address - 1 instead; gdbstub-mcp 0.1.0 doesn't yet.
    """
    s = r.session
    frames, method = s.backtrace(max_frames, "auto")
    if s.syms is None:
        return frames, method
    out = []
    for i, frame in enumerate(frames):
        addr = int(frame.split()[1], 16)
        if i == 0:
            out.append(frame)
            continue
        sym = s.syms.symbolize(addr)
        text = f"#{i}  {addr:#x}" + ("" if sym.startswith("0x") else f" <{sym}>")
        line = s.syms.line_for(addr - 1)
        if line is not None:
            text += f" at {os.path.basename(line.file)}:{line.line}"
        out.append(text)
    return out, method


def _stopped(r: ReplaySession, reply: bytes | None, timeout_s: float, note: str = "") -> str:
    if reply is None:
        return (f"Still running after {timeout_s}s. Call replay_wait to keep waiting or "
                f"replay_interrupt to halt.")
    text = reply.decode(errors="replace")
    ic = r.icount()
    end = r.recording.end_icount
    if end and ic >= end - 1:
        head = f"End of recording reached ({r.position()}). Go backwards from here."
        return head + "\n" + f"pc = {r.session.where(r.pc())}"
    desc = r.session.describe_stop(reply)
    if ic == 0 and text.startswith(("T05", "S05")):
        desc = "Reached the START of the recording (icount 0) without hitting a breakpoint.\n" + desc
    out = f"{desc}\nposition: {r.position()}"
    return out + (f"\n({note})" if note else "")


# -- recording -----------------------------------------------------------------


@tool
def replay_record(
    name: str,
    kernel: str,
    timeout_s: float = 10.0,
    until_serial: str | None = None,
    arch: str = "i386",
    memory_mb: int = 128,
    append: str | None = None,
    directory: str | None = None,
    icount_shift: int = 7,
) -> str:
    """Run a kernel once under QEMU's deterministic recording.

    Recording stops when the guest crashes (triple fault) or shuts down, when
    `until_serial` text appears on the serial console, or after timeout_s.
    The kernel must boot with -kernel (multiboot/bzImage); recording is
    single-CPU with no network. Files go to `directory` (default
    ~/.qemu-replay-mcp/recordings/<name>, or $QEMU_REPLAY_DIR/<name>),
    including int.log - the QEMU interrupt log, which gdbstub-mcp's
    debug_explain_fault can decode. Then call replay_start.
    """
    _check_name(name)
    rec = recmod.record(name, directory or _default_dir(name), kernel, arch, memory_mb, append,
                        timeout_s, until_serial, icount_shift)
    _recordings[name] = rec
    how = {
        "guest-crash": ("the guest TRIPLE FAULTED - replay ends at the crash (int.log has the "
                        "exception chain for gdbstub-mcp's debug_explain_fault)"),
        "guest-shutdown": "the guest shut down",
        "timeout": f"timeout after {timeout_s}s",
        "serial-marker": f"serial marker {until_serial!r} appeared",
    }[rec.ended_by]
    return (f"Recorded {name!r}: {rec.end_icount:,} instructions in {rec.duration_s}s; "
            f"stopped because {how}.\nFiles: {rec.directory}\n"
            f"Next: replay_start name={name!r}")


@tool
def replay_recordings(directory: str | None = None) -> str:
    """List saved recordings (in the default directory, or `directory`)."""
    root = directory or os.path.dirname(_default_dir("x"))
    if not os.path.isdir(root):
        return f"No recordings yet ({root} does not exist)."
    rows = []
    for entry in sorted(os.listdir(root)):
        try:
            rec = Recording.load(os.path.join(root, entry))
        except (FileNotFoundError, ValueError, TypeError):
            continue
        active = " [replaying]" if rec.name in _replays else ""
        rows.append(f"{rec.name}: {rec.end_icount:,} instructions, ended by {rec.ended_by}, "
                    f"kernel {os.path.basename(rec.kernel)}{active}")
    return "\n".join(rows) if rows else f"No recordings in {root}."


# -- replay session -------------------------------------------------------------


@tool
def replay_start(name: str, elf: str | None = None, directory: str | None = None,
                 timeout_s: float = 20.0) -> str:
    """Start replaying a recording, halted at its first instruction (icount 0).

    elf is the ELF with symbols (defaults to the recorded kernel). One replay
    per recording at a time.
    """
    _check_name(name)
    if name in _replays:
        raise RSPError(f"{name!r} is already being replayed - replay_stop it first")
    rec = _load(name, directory)
    r = ReplaySession(rec, elf, timeout_s)
    _replays[name] = r
    syms = r.session.syms
    sym_note = (f"symbols: {len(syms.symbols)}, line entries: {len(syms.lines)}"
                if syms is not None else "no symbols")
    return (f"Replaying {name!r} ({rec.end_icount:,} instructions recorded, {sym_note}).\n"
            f"Halted at the start: {r.position()}, pc = {r.session.where(r.pc())}\n"
            f"Set breakpoints with replay_break, then replay_continue.")


@tool
def replay_status(name: str) -> str:
    """Where the replay is: icount position, pc, breakpoints."""
    r = _get(name)
    if r.session.rsp.running:
        return f"{name}: running ({r.position()})"
    bps = ", ".join(f"#{b.id} {b.kind} {b.label}" for b in r.session.breakpoints.values()) or "none"
    return f"{name}: halted at {r.session.where(r.pc())}\nposition: {r.position()}\nbreakpoints: {bps}"


@tool
def replay_stop(name: str) -> str:
    """Stop a replay (the recording stays on disk and can be replayed again)."""
    r = _get(name)
    r.kill()
    del _replays[name]
    return f"Stopped replaying {name!r}."


# -- moving through time ----------------------------------------------------------


@tool
def replay_continue(name: str, timeout_s: float = 30.0) -> str:
    """Run FORWARD until a breakpoint/watchpoint hits or the recording ends."""
    r = _get(name)
    reply, note = r.forward(timeout_s)
    if reply is None and note:
        return note
    return _stopped(r, reply, timeout_s)


@tool
def replay_reverse_continue(name: str, timeout_s: float = 120.0) -> str:
    """Run BACKWARDS to the previous breakpoint/watchpoint hit (or the start).

    QEMU re-executes from the start of the recording to find it, so this
    takes longer the further into the recording you are.
    """
    r = _get(name)
    return _stopped(r, r.reverse(False, timeout_s), timeout_s)


@tool
def replay_step(name: str, count: int = 1) -> str:
    """Single-step `count` instructions forward (1..1000)."""
    if not 1 <= count <= 1000:
        raise ValueError("count must be between 1 and 1000")
    r = _get(name)
    return _stopped(r, r.step(count), 0)


@tool
def replay_reverse_step(name: str, count: int = 1, timeout_s: float = 60.0) -> str:
    """Single-step `count` instructions BACKWARDS (1..100)."""
    if not 1 <= count <= 100:
        raise ValueError("count must be between 1 and 100")
    r = _get(name)
    return _stopped(r, r.reverse_step(count, timeout_s), timeout_s)


@tool
def replay_goto(name: str, icount: int) -> str:
    """Jump to an instruction count (0 = start). Positions are exact and
    reproducible: the same icount is always the same machine state."""
    r = _get(name)
    r.goto(icount)
    return f"At {r.position()}, pc = {r.session.where(r.pc())}"


@tool
def replay_wait(name: str, timeout_s: float = 30.0) -> str:
    """Keep waiting for a running replay to stop."""
    r = _get(name)
    return _stopped(r, r.session.rsp.wait_stop(timeout_s), timeout_s)


@tool
def replay_interrupt(name: str) -> str:
    """Halt a running replay where it is."""
    r = _get(name)
    return _stopped(r, r.session.rsp.interrupt(), 0)


# -- breakpoints and inspection -----------------------------------------------------


@tool
def replay_break(name: str, location: str, kind: str = "sw", length: int | None = None) -> str:
    """Set a breakpoint ("sw"/"hw") or watchpoint ("write"/"read"/"access").
    Breakpoints apply in both directions of time."""
    r = _get(name)
    bp, note = r.session.add_breakpoint(location, kind, length)
    return f"#{bp.id} {kind} at {r.session.where(bp.address)}" + (f"\n({note})" if note else "")


@tool
def replay_delete(name: str, id: int) -> str:
    """Remove a breakpoint/watchpoint by id."""
    r = _get(name)
    bp = r.session.remove_breakpoint(id)
    return f"Removed #{bp.id} ({bp.label})."


@tool
def replay_registers(name: str, registers: str | None = None) -> str:
    """Read registers at the current point in time (comma-separated list, or
    the general set)."""
    r = _get(name)
    s = r.session
    names = [x.strip() for x in registers.split(",")] if registers else list(s.read_regs())
    lines = []
    for n in names:
        v = s.read_reg(n)
        reg = s.layout.by_name(n)
        line = f"{reg.name:>8} = {v:#0{reg.bitsize // 4 + 2}x}"
        where = s.where(v)
        if where != f"{v:#x}":
            line += "  " + where[len(f"{v:#x}"):].strip()
        lines.append(line)
    return "\n".join(lines) + f"\nposition: {r.position()}"


@tool
def replay_memory(name: str, address: str, length: int = 32) -> str:
    """Read memory at the current point in time (hex dump, up to 1024 bytes).
    Go back in time and read again to see what it held earlier."""
    if not 0 < length <= 1024:
        raise ValueError("length must be between 1 and 1024")
    r = _get(name)
    addr, _ = r.session.resolve(address)
    data = r.session.rsp.read_memory(addr, length)
    rows = [f"{addr + o:#010x}: " + " ".join(f"{b:02x}" for b in data[o:o + 16])
            for o in range(0, len(data), 16)]
    return "\n".join(rows) + f"\nposition: {r.position()}"


@tool
def replay_backtrace(name: str, max_frames: int = 16) -> str:
    """Call stack at the current point in time."""
    r = _get(name)
    frames, method = _call_stack(r, max_frames)
    return "\n".join(frames) + f"\n(method: {method})"


@tool
def replay_last_write(name: str, location: str, length: int | None = None,
                      timeout_s: float = 180.0) -> str:
    """Find the instruction that last WROTE a variable/address before now.

    The standard way to debug memory corruption: stop where the bad value is
    noticed (a crash, a failed check), then ask who wrote it. Runs backwards
    to the most recent write and leaves you on the writing instruction, with
    the old and new values, source line and call stack. Call it again from
    there to find the write before that. location: symbol, symbol+off,
    0x1234 or $reg+off; length defaults to one machine word.
    """
    r = _get(name)
    s = r.session
    addr, note = s.resolve(location)
    n = length or s.word
    if not 0 < n <= 8:
        raise ValueError("length must be between 1 and 8 bytes (a CPU watchpoint)")
    order = s.layout.byteorder
    start_ic = r.icount()
    value_now = int.from_bytes(s.rsp.read_memory(addr, n), order)
    bp, _ = s.add_breakpoint(f"{addr:#x}", "write", n)
    try:
        reply = r.reverse(False, timeout_s)
        if reply is None:
            s.rsp.interrupt(30)
            raise RSPError(
                f"no write found within {timeout_s}s (reverse execution re-runs the recording "
                f"from the start; raise timeout_s). Stopped at {r.position()}.")
    finally:
        if not s.rsp.running:
            s.remove_breakpoint(bp.id)
    if r.icount() == 0:
        return (f"No write to {location} ({addr:#x}) between the start of the recording and "
                f"icount {start_ic:,}: it has held {value_now:#x} since boot (or was written "
                f"before the first instruction, e.g. loaded from the ELF image).\n"
                f"Now at the start of the recording.")
    after = int.from_bytes(s.rsp.read_memory(addr, n), order)
    # A write watchpoint fires after the store; one step back is the store.
    stepped = r.reverse(True, 60)
    if stepped is None:
        raise RSPError("reverse step onto the writing instruction did not complete")
    before = int.from_bytes(s.rsp.read_memory(addr, n), order)
    pc = r.pc()
    insn = s.disassemble(pc, 1)[0].split(":", 1)[1].strip()
    frames, _ = _call_stack(r, 8)
    lines = [
        f"Last write to {location} ({addr:#x}, {n} bytes) before icount {start_ic:,}:",
        f"  at {s.where(pc)}",
        f"  instruction: {insn}",
        f"  value: {before:#x} -> {after:#x}" + ("" if after == value_now else
                                                 f" (later changed again; now {value_now:#x})"),
        f"  position: {r.position()} (you are ON the writing instruction)",
        "  call stack:",
        *["    " + f for f in frames],
        "Call replay_last_write again to find the write before this one.",
    ]
    if note:
        lines.append(f"({note})")
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
