"""Unit tests - no QEMU needed."""

import json
import socket
import threading

import pytest

from qemu_replay_mcp import server as S
from qemu_replay_mcp.qemu import QMP, QemuError, find_qemu
from qemu_replay_mcp.recording import Recording, machine_args


def _rec(tmp_path, **kw):
    base = dict(name="r", directory=str(tmp_path), kernel="k.elf", arch="i386", memory_mb=64,
                append=None, icount_shift=7, end_icount=1234, ended_by="timeout", duration_s=1.0)
    base.update(kw)
    return Recording(**base)


def test_recording_roundtrip(tmp_path):
    rec = _rec(tmp_path, ended_by="guest-crash")
    rec.save()
    loaded = Recording.load(str(tmp_path))
    assert loaded == rec
    assert loaded.rr_file.endswith("rec.bin") and loaded.disk.endswith("disk.qcow2")


def test_load_missing_recording_is_clear(tmp_path):
    with pytest.raises(FileNotFoundError, match="meta.json missing"):
        Recording.load(str(tmp_path))


def test_machine_args_make_replay_deterministic():
    args = machine_args("qemu", "k.elf", 64, "console=ttyS0", r"C:\x\disk.qcow2", r"C:\x\s.log", 4444)
    joined = " ".join(args)
    # Crash pauses instead of exiting, so the recording ends exactly at it.
    assert "-no-reboot" in args and "-no-shutdown" in args
    # Snapshots need qcow2 behind blkreplay; nothing nondeterministic attached.
    assert "file=C:/x/disk.qcow2,if=none,id=rr-disk" in joined
    assert "driver=blkreplay" in joined and "-net none" in joined and "-smp 1" in joined
    assert "file:C:/x/s.log" in joined
    assert args[-2:] == ["-append", "console=ttyS0"]


@pytest.mark.parametrize("bad", ["", "a/b", "a\\b", ".", ".."])
def test_names_are_validated(bad):
    with pytest.raises(ValueError, match="invalid name"):
        S._check_name(bad)


def test_find_qemu_rejects_path_like_arch():
    with pytest.raises(ValueError):
        find_qemu("../evil")


def test_default_dir_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("QEMU_REPLAY_DIR", str(tmp_path))
    assert S._default_dir("x") == str(tmp_path / "x")


def test_unknown_replay_error_is_instructive():
    with pytest.raises(Exception, match="call replay_start first"):
        S._get("nope")


def _fake_qmp(replies):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        f = conn.makefile("rwb")
        f.write(b'{"QMP": {}}\n')
        f.flush()
        for reply in replies:
            if not f.readline():
                return
            for msg in reply:
                f.write(json.dumps(msg).encode() + b"\n")
            f.flush()
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def test_qmp_skips_and_records_events():
    port = _fake_qmp([
        [{"return": {}}],                                            # qmp_capabilities
        [{"event": "STOP"}, {"return": {"icount": 42}}],            # query-replay
        [{"error": {"desc": "replay-seek: not in replay mode"}}],   # replay-seek
    ])
    q = QMP(port)
    assert q.command("query-replay") == {"icount": 42}
    assert [e["event"] for e in q.events] == ["STOP"]
    with pytest.raises(QemuError, match="not in replay mode"):
        q.command("replay-seek", icount=1)
    q.close()
