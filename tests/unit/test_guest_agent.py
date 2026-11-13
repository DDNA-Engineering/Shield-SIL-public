"""The Python guest agent (image/overlay/common/usr/lib/shield-sil/hwd.py), run on the host
against a FIFO standing in for the virtio-serial port."""

import json
import os
import subprocess
import sys
import time

import pytest

from shield_sil.profile import REPO_ROOT

AGENT = REPO_ROOT / "image" / "overlay" / "common" / "usr" / "lib" / "shield-sil" / "hwd.py"


@pytest.fixture
def agent(tmp_path):
    port = tmp_path / "port"
    os.mkfifo(port)
    state = tmp_path / "state"
    proc = subprocess.Popen([sys.executable, str(AGENT), str(port)], env={**os.environ, "SHIELD_STATE_DIR": str(state)})
    w = open(port, "wb", buffering=0)
    yield w, state
    w.close()
    proc.kill()
    proc.wait()


def wait_file(path, pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(path.read_text())
            if pred(data):
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.02)
    raise AssertionError(f"{path} never matched")


def frame(seq, **devs):
    lines = [f"{d}\t{json.dumps(v)}" for d, v in devs.items()] + [f"bus\t{json.dumps({'seq': seq})}"]
    return ("\n".join(lines) + "\n").encode()


def test_records_become_device_files(agent):
    w, state = agent
    w.write(frame(1, bms={"soc_pct": 80}, pd={"attached": True}))
    wait_file(state / "bus.json", lambda d: d["seq"] == 1)
    assert json.loads((state / "bms.json").read_text()) == {"soc_pct": 80}
    assert json.loads((state / "pd.json").read_text()) == {"attached": True}


def test_backlog_is_coalesced_to_the_newest_reading(agent):
    w, state = agent
    w.write(b"".join(frame(i, bms={"soc_pct": i}) for i in range(1, 500)))
    wait_file(state / "bus.json", lambda d: d["seq"] == 499)
    assert json.loads((state / "bms.json").read_text()) == {"soc_pct": 499}


def test_bad_names_are_ignored(agent):
    w, state = agent
    w.write(b"../etc/passwd\t{}\nBMS\t{}\n" + frame(2, fan={"rpm": 1}))
    wait_file(state / "bus.json", lambda d: d["seq"] == 2)
    assert sorted(p.name for p in state.iterdir()) == ["bus.json", "fan.json"]
