# qemu-replay-mcp

**Time-travel debugging for kernels: record one run, then let an AI agent step backwards through it.**

<!-- mcp-name: io.github.0xmortuex/qemu-replay-mcp -->

Memory corruption is the worst bug in kernel development. Something writes a bad
value, and thousands of instructions later a different function trips over it and
the machine triple-faults. The crash site tells you nothing about the cause. With
an ordinary debugger you guess, add a watchpoint, reboot, and hope the bug
reproduces.

qemu-replay-mcp records the run once with QEMU's deterministic record/replay.
Then it replays that exact run under a debugger that can go **backwards**. From
the crash, the agent asks "who wrote this variable?" and gets the instruction,
the source line and the call stack.

I couldn't find any other MCP server that does this for kernels. The existing
time-travel MCPs wrap `rr`, which only works for Linux userland processes.

## What it looks like

This is real output. `tests/fixtures/corrupt` is a ~50-line kernel with an
off-by-one. `record_sample()` accepts `slot == 8`, which writes one element past
`history[8]` and into `st.magic`. `kmain` checks `magic` only every 1000
iterations, then crashes.

```
> replay_record bug kernel.elf
Recorded 'bug': 6,201,039 instructions in 0.61s; stopped because the guest
TRIPLE FAULTED - replay ends at the crash

> replay_start bug
> replay_break crash
> replay_continue
Stopped: SIGTRAP (breakpoint/step) - breakpoint #1 (crash)
pc = 0x100040 <crash> at kernel.c:27
position: icount 6,201,035 of 6,201,039 (100.0%)

> replay_memory st+0x20 4
0x001001d8: 88 0b 00 00          ← magic should be 0xC0FFEE. Who wrote 0xb88?

> replay_last_write st+0x20
Last write to st+0x20 (0x1001d8, 4 bytes) before icount 6,201,035:
  at 0x100037 <record_sample+0x17> at kernel.c:24
  instruction: mov dword ptr [eax*4 + 0x1001b8], ecx
  value: 0xb58 -> 0xb88
  position: icount 6,200,572 of 6,201,039 (you are ON the writing instruction)
  call stack:
    #0  0x100037 <record_sample+0x17> at kernel.c:24
    #1  0x10008e <kmain+0x3e> at kernel.c:42
    #2  0x100016 <_start+0xa> at boot.s:21
Call replay_last_write again to find the write before this one.
```

`kernel.c:24` is `st.history[slot] = value;`, the bug, 463 instructions and two
function calls before the crash. Calling it again walks back through earlier
writes (`0xb28 -> 0xb58`, ...). Every replay is bit-for-bit identical, so
`icount 6,200,572` names the same machine state every time you or the agent
go there.

## How it works

1. **`replay_record`** boots the kernel with
   `-icount shift=7,rr=record,rrsnapshot=init`. QEMU logs every
   non-deterministic input (interrupts, timers, I/O) by instruction count
   (icount). It also takes a VM snapshot at icount 0, stored in a qcow2 image
   behind `blkreplay`. With `-no-reboot -no-shutdown`, a triple fault pauses the
   VM instead of exiting, so the recording ends exactly at the crash. Recording
   also stops on a serial-console marker or a timeout.
2. **`replay_start`** boots the same machine with `rr=replay` and a gdbstub. It
   checks that the stub advertises `ReverseStep+` and `ReverseContinue+`.
3. **Going backwards** uses the GDB remote protocol's `bs`/`bc` packets. QEMU
   reloads the snapshot and replays forward to the previous hit. Registers,
   memory and breakpoints all follow the timeline.
4. **`replay_last_write`** sets a write watchpoint, runs backwards to the most
   recent hit, then steps back one more instruction. On x86 a data watchpoint
   fires *after* the store, so that extra step lands on the instruction that
   wrote. It reads memory before and after the write to show the old and new
   values.

Debugging uses [gdbstub-mcp](https://github.com/0xmortuex/gdbstub-mcp): its RSP
client, register discovery, ELF/DWARF symbols and backtraces.

## Install

```bash
pip install git+https://github.com/0xmortuex/qemu-replay-mcp
claude mcp add qemu-replay -- qemu-replay-mcp
```

You need QEMU (`qemu-system-<arch>` and `qemu-img`) on PATH. On Windows, the
default install dir and `QEMU_DIR` also work. Python 3.10+.

## Tools

| Tool | What it does |
|------|--------------|
| `replay_record` | Record a kernel run until it crashes or shuts down, a `until_serial` marker appears, or `timeout_s` passes. Saves `rec.bin`, a snapshot disk, `serial.log`, and `int.log` (for gdbstub-mcp's `debug_explain_fault`). |
| `replay_recordings` | List saved recordings. |
| `replay_start` / `replay_stop` | Start replaying a recording, halted at icount 0, or stop the replay. |
| `replay_status` | Current icount, pc, breakpoints. |
| `replay_continue` / `replay_reverse_continue` | Run forward or **backward** to the next breakpoint or watchpoint hit. |
| `replay_step` / `replay_reverse_step` | Single-step forward or **backward**. |
| `replay_goto` | Jump to any icount. Positions are exact and reproducible. |
| `replay_wait` / `replay_interrupt` | Keep waiting for a long run, or halt it. |
| `replay_break` / `replay_delete` | Set a breakpoint or a watchpoint (`write`/`read`/`access`) on a symbol, `file.c:line` or address, or remove one. |
| `replay_registers` / `replay_memory` / `replay_backtrace` | Inspect the machine *at the current point in time*. |
| `replay_last_write` | **Find the instruction that last wrote a variable or address**, with old and new values, line and call stack. |

## QEMU behaviour it handles

All of these were found on QEMU 11.0. Each one has an integration test.

- **A forward continue from a breakpoint doesn't advance in replay.** QEMU
  re-reports the same hit at the same icount. The server steps over the
  breakpoint first, like gdb does.
- **Replaying past a recording that ended with `quit` replays the quit**, and
  QEMU exits. The server stores the final icount and fences it with QMP
  `replay-break` before every forward run. It has to re-arm the fence each time,
  because reverse execution uses `replay-break` internally.
- **Fast crashes beat QMP.** The kernel can triple-fault before the QMP socket
  connects, so the `SHUTDOWN` event is lost. Crashes are detected from QEMU's
  interrupt log instead.

## Limits (honest ones)

- **Reverse execution gets slower the further into the recording you are.**
  QEMU replays forward from the only snapshot, at icount 0. `replay_last_write`
  took 1–8s near the end of a 6M-instruction recording, and a reverse-continue
  took ~23s near the end of a 744M-instruction one. Periodic snapshots would fix
  this; see [BACKLOG.md](BACKLOG.md).
- **`-kernel` boots only, single CPU, no network.** That's what record/replay
  supports without extra setup. ISO and disk boots need every block device
  behind `blkreplay`, which is planned.
- **Watchpoints are CPU watchpoints**: up to 8 bytes, and in practice only a few
  at once.
- At icount 0 the CPU is in real mode at the reset vector, so `pc = 0xfff0` shows
  without the CS base. Set a breakpoint and continue to get into your kernel.

## Tests

```bash
pip install -e ".[test]" "ruff==0.16.0" "mypy==2.3.0"
ruff check src tests && mypy --strict src
pytest tests
```

Unit and stdio tests need no QEMU. `tests/test_qemu_integration.py` records and
replays the two fixture kernels in real QEMU, and skips itself when QEMU is
missing. It checks: finding the corrupting write from the crash, walking to the
previous write, memory following the timeline, "no write since boot", the end
fence, reverse-step accuracy, exact `goto`, and serial-marker and timeout
recordings. CI installs `qemu-system-x86` and runs them on Linux.

## License

MIT
