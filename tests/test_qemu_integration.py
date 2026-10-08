"""End-to-end time travel against real QEMU (skipped when it's not installed).

tests/fixtures/corrupt is a kernel with a genuine off-by-one: record_sample()
writes history[8], one past the end, which is `st.magic`. kmain checks magic
only every 1000 iterations, then triple faults - far from the bad write.
These tests find the culprit from the crash by going backwards.
"""

import os
import shutil

import pytest

from qemu_replay_mcp import server as S
from qemu_replay_mcp.qemu import QemuError, find_qemu

HERE = os.path.dirname(__file__)
CORRUPT = os.path.join(HERE, "fixtures", "corrupt", "kernel.elf")
LOOP = os.path.join(HERE, "fixtures", "kernel", "kernel.elf")


def _have_qemu():
    try:
        find_qemu("i386")
        return True
    except QemuError:
        return False


pytestmark = pytest.mark.skipif(not _have_qemu(), reason="qemu-system-i386 not installed")


@pytest.fixture
def rec_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("QEMU_REPLAY_DIR", str(tmp_path))
    yield tmp_path
    for name in list(S._replays):
        S._replays.pop(name).kill()
    S._recordings.clear()
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_record_until_crash_then_find_the_corrupting_write(rec_dir):
    out = S.replay_record("bug", CORRUPT, timeout_s=20)
    assert "TRIPLE FAULTED" in out
    rec = S._recordings["bug"]
    assert rec.ended_by == "guest-crash" and rec.end_icount > 0
    assert os.path.isfile(os.path.join(rec.directory, "int.log"))

    assert "Halted at the start" in S.replay_start("bug")
    S.replay_break("bug", "crash")
    stop = S.replay_continue("bug")
    assert "breakpoint #1 (crash)" in stop

    # The killer feature: from the crash, who wrote st.magic?
    found = S.replay_last_write("bug", "st+0x20")
    assert "<record_sample+0x17> at kernel.c:24" in found  # st.history[slot] = value
    assert "instruction: mov dword ptr" in found
    assert "<kmain+0x3e> at kernel.c:42" in found.split("call stack:")[1]  # the call line
    first_ic = S._replays["bug"].icount()

    # ...and the write before that, one step further back in time.
    again = S.replay_last_write("bug", "st+0x20")
    assert "kernel.c:24" in again
    assert S._replays["bug"].icount() < first_ic


def test_memory_is_time_travelled(rec_dir):
    S.replay_record("bug", CORRUPT, timeout_s=20)
    S.replay_start("bug")
    S.replay_break("bug", "crash")
    S.replay_continue("bug")
    at_crash = S.replay_memory("bug", "st+0x20", 4).splitlines()[0]
    S.replay_last_write("bug", "st+0x20")
    before_write = S.replay_memory("bug", "st+0x20", 4).splitlines()[0]
    assert at_crash != before_write


def test_no_write_since_boot(rec_dir):
    S.replay_record("bug", CORRUPT, timeout_s=20)
    S.replay_start("bug")
    S.replay_break("bug", "kmain", kind="hw")
    S.replay_continue("bug")
    # magic is initialised in the ELF's .data and not yet corrupted.
    out = S.replay_last_write("bug", "st+0x20")
    assert "No write to st+0x20" in out and "0xc0ffee" in out


def test_end_of_recording_is_fenced_and_reverse_step_works(rec_dir):
    S.replay_record("bug", CORRUPT, timeout_s=20)
    S.replay_start("bug")
    assert "End of recording reached" in S.replay_continue("bug")
    assert "already at the end" in S.replay_continue("bug")
    end_ic = S._replays["bug"].icount()
    S.replay_reverse_step("bug", 2)
    assert S._replays["bug"].icount() == end_ic - 2
    S.replay_step("bug", 1)
    assert S._replays["bug"].icount() == end_ic - 1


def test_goto_is_exact(rec_dir):
    S.replay_record("bug", CORRUPT, timeout_s=20)
    S.replay_start("bug")
    assert "icount 5,000 of" in S.replay_goto("bug", 5000)
    with pytest.raises(ValueError):
        S.replay_goto("bug", 10**12)


def test_record_until_serial_marker(rec_dir):
    out = S.replay_record("boot", LOOP, timeout_s=20, until_serial="loop ready")
    assert "serial marker 'loop ready' appeared" in out
    rec = S._recordings["boot"]
    with open(os.path.join(rec.directory, "serial.log")) as fh:
        assert "loop ready" in fh.read()


def test_record_timeout_and_replay_past_a_quit(rec_dir):
    # The loop kernel never stops: recording ends by timeout + quit. Without
    # the end fence, replaying past the recorded quit would exit QEMU.
    out = S.replay_record("loop", LOOP, timeout_s=1.5)
    assert "timeout after 1.5s" in out
    S.replay_start("loop")
    assert "End of recording reached" in S.replay_continue("loop", timeout_s=60)
    assert "halted" in S.replay_status("loop")
    S.replay_break("loop", "counter", kind="write")
    back = S.replay_reverse_continue("loop")
    assert "counter" in back or "kmain" in back
