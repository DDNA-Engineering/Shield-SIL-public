"""Run shell commands on the emulated Shield over its serial console."""

from __future__ import annotations

import re
import socket
import threading
import time
import uuid
from pathlib import Path


class ConsoleError(RuntimeError):
    pass


# Cursor position report: the query, and the answer of a 50x250 terminal (the
# guest moves the cursor to the far corner first, so this becomes its size).
CPR_QUERY = b"\x1b[6n"
CPR_REPLY = b"\x1b[50;250R"


class Console:
    """Serial console client.

    QEMU exposes ttyAMA0 on a unix socket. Output is captured continuously to
    ``log_path``. ``run()`` wraps a command in unique markers so its output and
    exit status can be recovered reliably from the byte stream.
    """

    def __init__(self, sock_path: Path, log_path: Path, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self.sock.connect(str(sock_path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                self.sock.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        self._buf = bytearray()
        self._cv = threading.Condition()
        self._log = open(log_path, "ab")
        self._closed = False
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, name="console", daemon=True).start()

    def _reader(self) -> None:
        while True:
            try:
                data = self.sock.recv(4096)
            except OSError:
                data = b""
            if not data:
                with self._cv:
                    self._closed = True
                    self._cv.notify_all()
                return
            self._log.write(data)
            self._log.flush()
            with self._cv:
                # A cursor position query can straddle two reads.
                queries = (self._buf[-3:] + data).count(CPR_QUERY)
                self._buf += data
                self._cv.notify_all()
            for _ in range(queries):
                self._answer(CPR_REPLY)

    def _answer(self, reply: bytes) -> None:
        """Answer a terminal query as a terminal would (systemd and agetty on
        Ubuntu 26.04 ask for the cursor position to size the console)."""
        try:
            # Not under self._lock: run() holds it while it waits for output.
            self.sock.sendall(reply)
        except OSError:
            pass

    def text(self) -> str:
        with self._cv:
            return self._buf.decode(errors="replace")

    def mark(self) -> int:
        """Position in the console output, for wait_for(start=...): only what comes after."""
        with self._cv:
            return len(self._buf)

    def text_since(self, mark: int) -> str:
        """Console output after a mark()."""
        with self._cv:
            return self._buf[mark:].decode(errors="replace")

    def wait_for(self, pattern: str, timeout: float, start: int = 0) -> re.Match:
        rx = re.compile(pattern.encode())
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                m = rx.search(self._buf, start)
                if m:
                    return m
                if self._closed:
                    raise ConsoleError(f"console closed while waiting for {pattern!r}")
                left = deadline - time.monotonic()
                if left <= 0:
                    tail = self._buf[-2000:].decode(errors="replace")
                    raise ConsoleError(f"timeout waiting for {pattern!r}; console tail:\n{tail}")
                self._cv.wait(left)

    def send(self, text: str) -> None:
        self.sock.sendall(text.encode())

    def run(self, cmd: str, timeout: float = 60.0, check: bool = True) -> tuple[int, str]:
        tag = uuid.uuid4().hex[:12]
        begin, end = f"__B{tag}__", f"__E{tag}__"
        with self._lock:
            mark = len(self._buf)
            # Markers are assembled by the shell so the echoed command line never matches.
            self.send(
                f"echo {begin[:6]}''{begin[6:]}; ( {cmd} ) 2>&1; echo {end[:6]}''{end[6:]}=$?\n"
            )
            m = self.wait_for(re.escape(end) + r"=(\d+)", timeout, start=mark)
            b = self.wait_for(re.escape(begin), 1, start=mark)
        out = self._buf[b.end(): m.start()].decode(errors="replace")
        out = out.replace("\r\n", "\n").strip("\n")
        rc = int(m.group(1))
        if check and rc != 0:
            raise ConsoleError(f"command failed ({rc}): {cmd}\n{out}")
        return rc, out

    def close(self) -> None:
        try:
            self.sock.close()
        finally:
            self._log.close()
