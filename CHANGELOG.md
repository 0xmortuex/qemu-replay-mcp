# Changelog

## 0.1.0 - 2026-10-08

First release.

- `replay_record`: record a `-kernel` boot with QEMU's deterministic record/replay
  until the guest crashes or shuts down, a serial marker appears, or a timeout passes.
  Saves the replay log, an icount-0 snapshot (qcow2 behind blkreplay), the serial log
  and QEMU's interrupt log.
- `replay_start` replays a recording under a gdbstub and checks for ReverseStep and
  ReverseContinue support.
- Moving in time: `replay_continue`, `replay_reverse_continue`, `replay_step`,
  `replay_reverse_step`, `replay_goto` (exact icount), `replay_wait`, `replay_interrupt`.
- `replay_last_write`: from any point, find the instruction that last wrote a
  variable/address, with old and new values, source line and call stack.
- Handles QEMU replay quirks: steps over the current breakpoint before a forward
  continue, fences the end of a quit-terminated recording with `replay-break`, and
  detects fast crashes from the interrupt log.
- Caller frames in call stacks name the call line (return address - 1).
