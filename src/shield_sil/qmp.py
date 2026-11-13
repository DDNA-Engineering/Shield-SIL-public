"""Minimal QEMU Machine Protocol client."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path


class QMPError(RuntimeError):
    pass


class QMP:
    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                self.sock.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        self.sock.settimeout(timeout)
        self._f = self.sock.makefile("rwb")
        self._read()  # greeting
        self.cmd("qmp_capabilities")

    def _read(self) -> dict:
        while True:
            line = self._f.readline()
            if not line:
                raise QMPError("QMP connection closed")
            msg = json.loads(line)
            if "event" in msg:
                continue
            return msg

    def cmd(self, command: str, **args) -> object:
        req = {"execute": command}
        if args:
            req["arguments"] = args
        self._f.write(json.dumps(req).encode() + b"\n")
        self._f.flush()
        resp = self._read()
        if "error" in resp:
            raise QMPError(f"{command}: {resp['error']}")
        return resp.get("return")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
