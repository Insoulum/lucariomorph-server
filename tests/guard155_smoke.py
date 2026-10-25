#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lucario Morph Sync — integration tests of the 1.5.5 per-socket guards.

Starts TWO service.py instances:
  S1) guards:    fast timers (HELLO_DEADLINE=1.5s, WS_ASSEMBLY_DEADLINE=1.5s),
                 default budget (WS_BURST=4, WS_REFILL_MS=250, WS_DROP_KILL=32)
  S2) legacy:    REQUIRE_AUTH=0 (tokenless writes for the anim-ts / push checks)

Covers: token bucket (legit burst survives, ping/hello floods kill), hello
deadline (silent scanner sockets die, a hello at any point saves), fragmented
messages (payload cap, assembly deadline, a small fragmented message works),
empty frames, oversize single frame (regression), slot reuse after every kill,
and the NaN/Infinity ts fallback (no invalid JSON is ever relayed to watchers).
Also re-covers the 1.5.1 morph push and 1.5.0 anim push cycles end-to-end.

Run:  python3 tests/guard155_smoke.py
"""
import json
import math
import os
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from ws_probe import WsClient  # noqa: E402

U1 = "069a79f4-44e9-4726-a5be-fca90e38aaf5"

PASS = 0
FAIL = 0


def check(n, desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {n:2}  {desc}", flush=True)
    else:
        FAIL += 1
        print(f"FAIL {n:2}  {desc}", flush=True)


def http(method, url, body=None, token=None, raw_body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Lucario-Token"] = token
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:  # noqa: BLE001 — refused connection etc.
        return 0, b""


def strict_json(b):
    """json.loads that REJECTS NaN/Infinity literals (invalid JSON per RFC 8259)."""
    def boom(_):
        raise ValueError("non-finite JSON constant")
    return json.loads(b.decode("utf-8"), parse_constant=boom)


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def send_fragmented(w, chunks, opcode=0x1):
    """Sends one message as several masked fragments (FIN split across frames)."""
    mask = secrets.token_bytes(4)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        op = opcode if i == 0 else 0x0
        b1 = (0x80 if last else 0x00) | op
        header = bytearray([b1])
        n = len(chunk)
        if n < 126:
            header.append(0x80 | n)
        elif n <= 0xFFFF:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[j % 4] for j, b in enumerate(chunk))
        w.sock.sendall(bytes(header) + masked)


def socket_dead(w, timeout=4.0):
    """True when the SERVER kills the socket (EOF/RST/close frame) within timeout.
    Reads raw bytes: pongs/snapshots are skipped, silence means still alive."""
    deadline = time.time() + timeout
    w.sock.settimeout(0.25)
    while time.time() < deadline:
        try:
            data = w.sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            return True                      # reset / broken pipe
        if not data:
            return True                      # clean EOF = the server closed
        if data[0] & 0x0F == 0x8:            # a WS close frame from the server
            return True
        # anything else (pong, hello ack, snapshot) — keep waiting for the kill
    return False


def socket_alive(w, timeout=0.6):
    """True when the socket stays silent (no close/EOF) for `timeout` seconds."""
    return not socket_dead(w, timeout)


def main():
    global PASS, FAIL
    s1_port, s2_port = free_port(), free_port()
    work = tempfile.mkdtemp(prefix="lucario-guard155-")
    env1 = dict(os.environ, PORT=str(s1_port),
                DATA_DIR=os.path.join(work, "s1"),
                RATE_PER_MIN="100000",
                HELLO_DEADLINE="1.5", WS_ASSEMBLY_DEADLINE="1.5",
                WS_PING_SECONDS="5")
    s1 = subprocess.Popen([sys.executable, os.path.join(ROOT, "service.py")], env=env1,
                          stdout=open(os.path.join(work, "s1.log"), "w"),
                          stderr=subprocess.STDOUT)
    env2 = dict(os.environ, PORT=str(s2_port),
                DATA_DIR=os.path.join(work, "s2"),
                RATE_PER_MIN="100000", REQUIRE_AUTH="0")
    s2 = subprocess.Popen([sys.executable, os.path.join(ROOT, "service.py")], env=env2,
                          stdout=open(os.path.join(work, "s2.log"), "w"),
                          stderr=subprocess.STDOUT)

    def wait_up(port):
        for _ in range(60):
            if http("GET", f"http://127.0.0.1:{port}/health")[0] == 200:
                return True
            time.sleep(0.1)
        return False

    try:
        up1, up2 = wait_up(s1_port), wait_up(s2_port)
        check(1, "both services started (guard instance + legacy instance)", up1 and up2)
        S1 = f"http://127.0.0.1:{s1_port}"
        S2 = f"http://127.0.0.1:{s2_port}"
        st, body = http("GET", S1 + "/health")
        h = strict_json(body)
        check(2, "/health -> 200 and version 1.5.5 (strict JSON parse)",
              st == 200 and h.get("version") == "1.5.5")

        # --- budget: the legit cadence must survive --------------------------------
        w = WsClient(s1_port)
        w.send({"type": "hello", "uuid": U1})
        ack = w.recv_until(lambda m: m.get("type") == "hello", timeout=3)
        check(3, "hello -> ack (the budget pays normal frames)", ack is not None)
        w.send({"type": "watch", "uuids": []})
        snap = w.recv_until(lambda m: m.get("type") == "state", timeout=3)
        check(4, "watch -> state snapshot", snap is not None)
        for _ in range(3):                     # a 3-watch resync burst, one token each
            w.send({"type": "watch", "uuids": []})
        time.sleep(0.35)                       # refill ~1.4 tokens
        w.send({"type": "watch", "uuids": [U1]})
        snap = w.recv_until(lambda m: m.get("type") == "state", timeout=3)
        check(5, "a legit watch burst survives; the last watch yields a snapshot",
              snap is not None and snap.get("morphs") is not None)
        for _ in range(6):                     # 6 pings at 300 ms = 3.3/s < refill 4/s
            w._send_frame(0x9, b"pi")
            time.sleep(0.3)
        check(6, "6 paced client pings keep the session alive (refill keeps up)",
              socket_alive(w, 0.4))
        w.close()
        time.sleep(0.3)

        # --- floods must die --------------------------------------------------------
        w2 = WsClient(s1_port)
        w2.send({"type": "hello", "uuid": U1})
        time.sleep(0.2)
        killed = False
        try:
            for _ in range(100):
                w2._send_frame(0x9, b"")       # 100 instant pings: 4 paid, then drops
            killed = socket_dead(w2, 3)
        except OSError:
            killed = True
        check(7, "ping flood (100 in a burst) -> the session is killed", killed)
        w2.close()
        time.sleep(0.3)

        w3 = WsClient(s1_port)
        w3.send({"type": "hello", "uuid": U1})
        time.sleep(0.2)
        killed = False
        try:
            for _ in range(2000):
                w3.send({"type": "hello", "uuid": U1})   # ~57k msg/s scaled down
            killed = socket_dead(w3, 3)
        except OSError:
            killed = True
        check(8, "hello flood (2000 in a burst) -> the session is killed", killed)
        w3.close()
        time.sleep(0.3)

        w4 = WsClient(s1_port)                 # a freed slot is reusable after each kill
        w4.send({"type": "hello", "uuid": U1})
        check(9, "reconnect after kills works (slots released)", w4.recv_until(
            lambda m: m.get("type") == "hello", timeout=3) is not None)
        w4.close()
        time.sleep(0.3)

        # --- hello deadline ---------------------------------------------------------
        w5 = WsClient(s1_port)
        alive_then = True
        try:
            t0 = time.time()
            for _ in range(8):                 # chatter (pings) but NEVER a hello
                w5._send_frame(0x9, b"hi")
                if socket_dead(w5, 0.3):
                    alive_then = False
                    break
        except OSError:
            alive_then = False
        took = time.time() - t0
        check(10, "a socket that never says hello is dropped by the deadline "
                  "(even while pinging)", not alive_then and took < 4)
        w5.close()

        w6 = WsClient(s1_port)
        time.sleep(1.0)                        # 1.0 s of silence (deadline is 1.5 s)
        w6.send({"type": "hello", "uuid": U1})
        ok6 = w6.recv_until(lambda m: m.get("type") == "hello", timeout=3) is not None
        time.sleep(1.5)                        # well past the deadline now
        check(11, "a hello before the deadline saves the socket", ok6 and socket_alive(w6, 0.5))
        w6.close()
        time.sleep(0.3)

        # --- fragmented messages ----------------------------------------------------
        w7 = WsClient(s1_port)
        w7.send({"type": "hello", "uuid": U1})
        w7.recv_until(lambda m: m.get("type") == "hello", timeout=3)
        try:
            send_fragmented(w7, [b"x" * 4000, b"x" * 2500, b"x" * 2500])  # 9000 > 8192
            over = socket_dead(w7, 3)
        except OSError:
            over = True
        check(12, "a fragmented message over the payload cap -> killed", over)
        w7.close()
        time.sleep(0.3)

        w8 = WsClient(s1_port)
        w8.send({"type": "hello", "uuid": U1})
        w8.recv_until(lambda m: m.get("type") == "hello", timeout=3)
        try:
            send_fragmented(w8, [json.dumps({"type": "watch", "uuids": []}).encode()[:20]])
            time.sleep(2.5)                    # longer than WS_ASSEMBLY_DEADLINE=1.5
            send_fragmented(w8, [b'"uuids":[]}]'], opcode=0x0)
            slow = socket_dead(w8, 3)
        except (OSError, IndexError):
            slow = True
        check(13, "a fragmented message dribbled past the assembly deadline -> killed", slow)
        w8.close()
        time.sleep(0.3)

        w9 = WsClient(s1_port)
        w9.send({"type": "hello", "uuid": U1})
        w9.recv_until(lambda m: m.get("type") == "hello", timeout=3)
        msg = json.dumps({"type": "watch", "uuids": []}).encode()
        third = len(msg) // 3
        send_fragmented(w9, [msg[:third], msg[third:2 * third], msg[2 * third:]])
        snap = w9.recv_until(lambda m: m.get("type") == "state", timeout=3)
        check(14, "a small fragmented watch still works (assembled and paid)",
              snap is not None)
        w9._send_frame(0x1, b"")               # an empty data frame: pays, no crash
        time.sleep(0.3)
        w9.send({"type": "watch", "uuids": []})
        check(15, "an empty frame is harmless; the session keeps working",
              w9.recv_until(lambda m: m.get("type") == "state", timeout=3) is not None)
        try:
            w9._send_frame(0x1, b"y" * 8193)   # single-frame oversize (regression)
            over1 = socket_dead(w9, 3)
        except OSError:
            over1 = True
        check(16, "a single frame over WS_MAX_PAYLOAD -> killed (as before)", over1)
        w9.close()
        time.sleep(0.3)

        w10 = WsClient(s1_port)
        w10.send({"type": "hello", "uuid": U1})
        acked = w10.recv_until(lambda m: m.get("type") == "hello", timeout=3) is not None
        w10.close()                            # a clean client-side close
        time.sleep(0.4)
        st, h2 = http("GET", S1 + "/health")
        check(17, "clean close + every killed socket released: live back to 0",
              acked and st == 200 and strict_json(h2).get("live") == 0)

        # --- legacy instance: NaN ts fallback + push regression ---------------------
        w11 = WsClient(s2_port)
        w11.send({"type": "hello", "uuid": U1})
        w11.recv_until(lambda m: m.get("type") == "hello", timeout=3)
        w11.send({"type": "watch", "uuids": [U1]})
        w11.recv_until(lambda m: m.get("type") == "state", timeout=3)
        st, _ = http("PUT", S2 + f"/v1/morphs/{U1}", {"variant": "lucario"})
        ev = w11.recv_until(lambda m: m.get("type") == "morph", timeout=3)
        check(18, "morph push still works end-to-end (1.5.1 regression)",
              st == 200 and ev is not None and ev.get("variant") == "lucario")
        st, body = http("PUT", S2 + f"/v1/anims/{U1}",
                        raw_body=b'{"anim":"dance","state":"start","ts":NaN}')
        st2, body2 = http("GET", S2 + f"/v1/anims?uuids={U1}")
        snap2 = strict_json(body2)
        rec = snap2.get("anims", {}).get(U1, {})
        check(19, "PUT anim with ts=NaN -> accepted, falls back to the server clock",
              st == 200 and st2 == 200 and rec.get("active") is True
              and math.isfinite(rec.get("ts", float("nan"))))
        ev = w11.recv_until(lambda m: m.get("type") == "anim", timeout=3)
        check(20, "the pushed anim event is valid JSON with a finite ts (no NaN relay)",
              ev is not None and ev.get("active") is True
              and math.isfinite(ev.get("ts", float("nan"))))
        st, body = http("PUT", S2 + f"/v1/anims/{U1}",
                        raw_body=b'{"anim":"dance","state":"start","ts":Infinity}')
        st2, body2 = http("GET", S2 + f"/v1/anims?uuids={U1}")
        rec = strict_json(body2).get("anims", {}).get(U1, {})
        check(21, "ts=Infinity falls back the same way (strict parse of the store)",
              st == 200 and st2 == 200 and math.isfinite(rec.get("ts", float("nan"))))
        st, _ = http("PUT", S2 + f"/v1/anims/{U1}",
                     {"anim": "dance", "state": "stop", "ts": 123})
        ev = w11.recv_until(lambda m: m.get("type") == "anim" and m.get("active") is False,
                            timeout=3)
        check(22, "anim stop push works (finite ts everywhere)", ev is not None)
        w11.close()

        st, h3 = http("GET", S2 + "/health")
        check(23, "both services alive at the end (nothing crashed)",
              st == 200 and strict_json(h3).get("ok") is True)

        log1 = open(os.path.join(work, "s1.log")).read()
        check(24, "startup log lists the 1.5.5 guards",
              "frame budget 4+1/250ms kill 32" in log1
              and "assembly cap 8192B/1.5s" in log1 and "hello deadline 1.5s" in log1)
    finally:
        for p in (s1, s2):
            p.kill()
        for p in (s1, s2):
            p.wait()

    print(flush=True)
    print("=" * 50)
    print(f"GUARD 1.5.5 SMOKE: PASS={PASS} FAIL={FAIL}")
    print("=" * 50)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
