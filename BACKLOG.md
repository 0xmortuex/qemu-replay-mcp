# Backlog

Real, finishable improvements - pick ONE, ship it end-to-end with tests.

## Features
- [ ] Periodic snapshots during replay (`snapshot-save` every N instructions) so reverse
      execution reloads a nearby snapshot instead of replaying from icount 0. The
      dominant cost today: ~23s for a reverse-continue near the end of a 744M-instruction
      recording.
- [ ] ISO and disk boots: put every user block device behind its own `blkreplay` node.
- [ ] `replay_first_write` / `replay_writes` (forward scan): list every write to an
      address across the whole recording, with icounts - a "history of this variable".
- [ ] `replay_diff`: compare registers + a memory range between two icounts.
- [ ] Combine with qemu-mcp: `qemu_screenshot` at an icount (screendump works in replay).
- [ ] Real-mode pc at icount 0: show CS.base + EIP (same backlog item as gdbstub-mcp).

## Dependencies
- [ ] Switch `gdbstub-mcp @ git+https://...` to a PyPI version pin once gdbstub-mcp is
      published. PyPI rejects packages with direct URL dependencies, so this must be
      done BEFORE publishing qemu-replay-mcp to PyPI.
- [x] Upstream to gdbstub-mcp (done in gdbstub-mcp fda9444): `py.typed`, public
      `resume(reverse=True)` (now used by `reverse_resume`), and caller frames looked up
      at `return_address - 1`.
- [ ] Drop `_call_stack`'s own frame re-rendering now that `Session.backtrace` gets
      caller lines right.

## Findings
- Periodic `savevm` snapshots during recording (tried 2026-10-08, QEMU 11.0.50): snapshots
  are valid (each carries its ICOUNT in `qemu-img snapshot -l`), but reverse-continue to a
  write watchpoint on a hot variable got *slower* (4s loop recording: 57s with only the
  init snapshot, >120s with one every 0.5s). Likely the per-hit watchpoint cost dominates.
  Before retrying: measure with a cold variable, and read QEMU's
  replay_reverse_continue to see how it walks snapshots.
