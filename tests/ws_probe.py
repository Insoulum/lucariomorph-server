#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A minimal RFC 6455 WebSocket client for the integration tests (stdlib only).

Speaks enough of the protocol for service.py: masked client frames, unmasked server
frames, ping/pong and close. Used by test_ws.py and can be driven by hand:

    python3 ws_probe.py 8080 '{"type":"hello","uuid":"..."}' '{"type":"watch","uuids":[]}'
"""
import base64
import hashlib
import json
import secrets
import socket
import struct
import sys
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WsClient:
    def __init__(self, port, host="127.0.0.1", timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = ("GET /ws HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: eof")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0].decode("latin-1")
        if " 101 " not in status:
            raise ConnectionError("handshake refused: " + status)
        expected = base64.b64encode(
            hashlib.sha1((key + GUID).encode("ascii")).digest()).decode()
        headers = {}
        for line in head.split(b"\r\n")[1:]:
            if b":" in line:
                k, _, v = line.partition(b":")
                headers[k.strip().lower()] = v.strip()
        if headers.get(b"sec-websocket-accept", b"") != expected.encode():
            raise ConnectionError("handshake: bad Sec-WebSocket-Accept")
        self.buf = bytearray(rest)

    # ---------------------------------------------------------------- frames
    def _read_exact(self, n, deadline):
        while len(self.buf) < n:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError("recv timeout")
            self.sock.settimeout(min(remain, 1.0))
            try:
                chunk = self.sock.recv(65536)
            except (TimeoutError, socket.timeout):
                continue
            if not chunk:
                raise ConnectionError("eof")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _send_frame(self, opcode, payload):
        mask = secrets.token_bytes(4)
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n <= 0xFFFF:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send(self, obj):
        self._send_frame(0x1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def recv(self, timeout=5.0):
        """Next DATA message as a dict. Answers pings, skips pongs.
        Returns None on a clean timeout (partial bytes stay buffered for the next call)."""
        deadline = time.time() + timeout
        while True:
            try:
                head = self._read_exact(2, deadline)
                b1, b2 = head[0], head[1]
                fin = b1 & 0x80
                op = b1 & 0x0F
                masked = b2 & 0x80
                ln = b2 & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", self._read_exact(2, deadline))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", self._read_exact(8, deadline))[0]
                # only CLIENT frames carry a 4-byte mask; server frames are unmasked
                mask = self._read_exact(4, deadline) if masked else b""
                data = self._read_exact(ln, deadline) if ln else b""
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            except (TimeoutError, socket.timeout):
                return None
            if op == 0x8:
                return None
            if op == 0x9:
                self._send_frame(0xA, data)   # pong (masked: client frames must be)
                continue
            if op == 0xA:
                continue
            if fin and op in (0x1, 0x2):
                return json.loads(data.decode("utf-8"))

    def recv_until(self, pred, timeout=5.0):
        """Reads messages until one satisfies pred(timeout); returns it or None."""
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return None
            msg = self.recv(timeout=remain)
            if msg is None:
                return None
            if pred(msg):
                return msg

    def close(self):
        try:
            self._send_frame(0x8, struct.pack(">H", 1000))
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


def main():
    port = int(sys.argv[1])
    c = WsClient(port)
    for arg in sys.argv[2:]:
        c.send(json.loads(arg))
    try:
        while True:
            print(json.dumps(c.recv(timeout=30), ensure_ascii=False), flush=True)
    except Exception:  # noqa: BLE001 — timeout/close ends the dump
        pass
    finally:
        c.close()


if __name__ == "__main__":
    main()
