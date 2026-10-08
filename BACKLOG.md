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
- [ ] Upstream to gdbstub-mcp: (1) ship a `py.typed` marker (we work around it with
      mypy `follow_untyped_imports`); (2) a public resume-with-packet API so `bs`/`bc`
      don't need RSPClient internals (`reverse_resume` in replay.py); (3) caller frames
      in `Session.backtrace` should look up `return_address - 1` (we re-render them in
      `_call_stack`).
