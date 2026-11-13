"""Control socket for a running SIL (``shield-sil up``).

Lets another terminal act on the hardware of a running emulated Shield, e.g.
press the front-panel button: ``shield-sil ctl button 0.5``. One JSON request
per connection, one JSON reply. The socket is
``build/runs/i<instance>.control.sock``, owned by root.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .profile import REPO_ROOT

if TYPE_CHECKING:
    from .sil import ShieldSIL


def socket_path(instance: int = 0) -> Path:
    return REPO_ROOT / "build" / "runs" / f"i{instance}.control.sock"


class ControlServer:
    def __init__(self, sil: ShieldSIL, path: Path) -> None:
        self.sil = sil
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        os.chmod(path, 0o600)
        self._sock.listen(4)
        self._sock.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sil-control", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                try:
                    req = json.loads(conn.makefile().readline() or "{}")
                    reply: dict[str, Any] = {"ok": True, **(self.handle(req) or {})}
                except Exception as e:  # noqa: BLE001 - report any failure to the caller
                    reply = {"ok": False, "error": str(e)}
                conn.sendall((json.dumps(reply) + "\n").encode())

    def handle(self, req: dict[str, Any]) -> dict[str, Any] | None:
        cmd = req.get("cmd")
        if cmd == "button":
            seconds = float(req.get("seconds", 0.3))
            if not 0 < seconds <= 600:
                raise ValueError("seconds must be in (0, 600]")
            self.sil.press_button(seconds)
            return {"pressed_s": seconds}
        if cmd == "link":
            self.sil.set_link(str(req["port"]), bool(req["up"]))
            return None
        raise ValueError(f"unknown command {cmd!r}")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()
        try:
            self.path.unlink()
        except OSError:
            pass


def request(req: dict[str, Any], instance: int = 0, timeout: float = 10) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(socket_path(instance)))
        s.sendall((json.dumps(req) + "\n").encode())
        return json.loads(s.makefile().readline())
