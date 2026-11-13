"""Minimal USB/IP client, playing the guest's vhci_hcd in unit tests."""

from __future__ import annotations

import socket
import struct
import threading

VERSION = 0x0111
HEADER = struct.Struct(">IIIII")
SUBMIT = struct.Struct(">IiiiI8s")
RET = struct.Struct(">iiiii8x")


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def devlist(port):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(struct.pack(">HHI", VERSION, 0x8005, 0))
        _v, code, status, n = struct.unpack(">HHII", recv_exact(s, 12))
        assert code == 0x0005 and status == 0
        out = []
        for _ in range(n):
            blk = recv_exact(s, 0x138)
            busid = blk[256:288].split(b"\0")[0].decode()
            vid, pid = struct.unpack(">HH", blk[0x12C:0x130])
            nif = blk[-1]
            ifs = [tuple(recv_exact(s, 4)[:3]) for _ in range(nif)]
            out.append({"busid": busid, "id": f"{vid:04x}:{pid:04x}", "interfaces": ifs})
        return out


class Attached:
    """An imported device: submit URBs synchronously or asynchronously."""

    def __init__(self, port, busid):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.sendall(struct.pack(">HHI", VERSION, 0x8003, 0) + busid.encode().ljust(32, b"\0"))
        _v, _code, status = struct.unpack(">HHI", recv_exact(self.sock, 8))
        if status != 0:
            self.sock.close()
            raise ConnectionRefusedError(f"import of {busid} refused")
        self.block = recv_exact(self.sock, 0x138)
        self.seq = 0
        self.lock = threading.Lock()
        self.replies: dict[int, tuple[int, bytes]] = {}
        self.cv = threading.Condition()
        self.closed = False
        self._is_in: dict[int, bool] = {}
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                cmd, seq, _d, _dir, _ep = HEADER.unpack(recv_exact(self.sock, 20))
                if cmd == 3:
                    status, actual, *_ = RET.unpack(recv_exact(self.sock, 28))
                    # Data follows only for IN transfers; the test knows which it sent.
                    data = recv_exact(self.sock, actual) if self._is_in.get(seq) and status == 0 else b""
                else:
                    status = struct.unpack(">i24x", recv_exact(self.sock, 28))[0]
                    data = b""
                with self.cv:
                    self.replies[seq] = (status, data)
                    self.cv.notify_all()
        except (ConnectionError, OSError):
            with self.cv:
                self.closed = True
                self.cv.notify_all()

    def submit(self, ep, direction_in, length=0, data=b"", setup=b"\0" * 8):
        with self.lock:
            self.seq += 1
            seq = self.seq
            self._is_in[seq] = direction_in
            pkt = HEADER.pack(1, seq, 0x10002, 1 if direction_in else 0, ep)
            pkt += SUBMIT.pack(0, length if direction_in else len(data), 0, -1, 0, setup) + (b"" if direction_in else data)
            self.sock.sendall(pkt)
        return seq

    def unlink(self, victim):
        with self.lock:
            self.seq += 1
            seq = self.seq
            self.sock.sendall(HEADER.pack(2, seq, 0x10002, 0, 0) + struct.pack(">I24x", victim))
        return seq

    def wait(self, seq, timeout=5.0):
        with self.cv:
            ok = self.cv.wait_for(lambda: seq in self.replies or self.closed, timeout)
            if seq in self.replies:
                return self.replies.pop(seq)
            raise TimeoutError(f"no reply to {seq}" if not ok else "connection closed")

    def control(self, request_type, request, value=0, index=0, length=0, data=b""):
        setup = struct.pack("<BBHHH", request_type, request, value, index, length or len(data))
        is_in = bool(request_type & 0x80)
        return self.wait(self.submit(0, is_in, length, data, setup))

    def close(self):
        self.sock.close()
