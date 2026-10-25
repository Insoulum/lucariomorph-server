#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lucario Morph Sync — integration tests of the 1.5.6 HTTP penalty box.

Starts TWO service instances with fast/small knobs:
  S1) box via the WS cap:  RATE_PER_MIN=100000 (the rate limiter never fires),
                           MAX_WS_PER_IP=1 (every second upgrade is a 429),
                           PENALTY_HITS=10, PENALTY_MINUTES=0.05 (3 s),
                           HELLO_DEADLINE=60 (a held socket stays put)
  S2) box via the limiter: RATE_PER_MIN=40, PENALTY_HITS=10, PENALTY_MINUTES=3

The tests drive requests from DIFFERENT loopback source addresses (127.0.0.2 is
the flooding victim, 127.0.0.3 is the observer, 127.0.0.4 exercises the WS cap),
so the per-IP state of one test actor never leaks into another.

Covers: three flood stages (200s -> 429s -> silent accept-level drops), the
penalty box banning after PENALTY_HITS 429s (fed by BOTH the rate limiter and
the per-IP WS cap), /health pressure counters (rejected / banned /
banned_dropped / conns), sampled 429 logging, one-line ban/release events,
ban expiry by TTL (the IP works again) and a re-ban.

Run:  python3 tests/penalty_smoke.py
"""
import http.client as httpclient
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

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


def http(method, url, body=None, src=None, timeout=5):
    """HTTP request optionally bound to a specific source address."""
    data = json.dumps(body).encode() if body is not None else None
    p = urllib.parse.urlsplit(url)
    conn = httpclient.HTTPConnection(p.hostname, p.port, timeout=timeout)
    if src is not None:
        def connect(self, _src=src, _timeout=timeout):
            self.sock = socket.socket()
            self.sock.bind((_src, 0))
            self.sock.settimeout(_timeout)
            self.sock.connect((self.host, self.port))
        conn.connect = connect.__get__(conn)
    path = p.path + (f"?{p.query}" if p.query else "")
    try:
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        return r.status, r.read()
    except Exception:  # noqa: BLE001 — dropped/refused/reset
        return 0, b""
    finally:
        conn.close()


def raw_get(port, path="/health", src="127.0.0.2", host="127.0.0.1", timeout=4):
    """One plain request over a fresh socket bound to `src`.
    Returns the raw response bytes (b'' when the server drops us silently)."""
    s = socket.socket()
    s.bind((src, 0))
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode())
        buf = b""
        while True:
            try:
                chunk = s.recv(4096)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            buf += chunk
        return buf
    finally:
        s.close()


def ws_open(port, src):
    """One raw WS upgrade from `src`. Returns (status_str, socket|None)."""
    s = socket.socket()
    s.bind((src, 0))
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", port))
        s.sendall((f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        first = buf.split(b"\r\n")[0].decode(errors="replace")
        code = first.split(" ")[1] if " " in first else "0"
        if code == "101":
            return code, s          # the caller owns the session socket
        s.close()
        return code, None
    except OSError:
        s.close()
        return "drop", None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_instance(port, extra_env, work, name):
    env = dict(os.environ, PORT=str(port),
               DATA_DIR=os.path.join(work, name),
               PENALTY_HITS="10", PENALTY_MINUTES="0.05",
               REQUIRE_AUTH="0", HOUSEKEEPING_SECONDS="5", **extra_env)
    log_path = os.path.join(work, f"{name}.log")
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "service.py")], env=env,
                            stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    return proc, log_path


def main():
    global PASS, FAIL
    p1, p2 = free_port(), free_port()
    work = tempfile.mkdtemp(prefix="lucario-penalty156-")
    s1, log1 = start_instance(p1, dict(RATE_PER_MIN="100000", MAX_WS_PER_IP="1",
                                       HELLO_DEADLINE="60", WS_PING_SECONDS="60"),
                              work, "s1")
    s2, log2 = start_instance(p2, dict(RATE_PER_MIN="40", HELLO_DEADLINE="60",
                                       WS_PING_SECONDS="60"),
                              work, "s2")
    held = []   # WS session sockets to close at the end

    def health1(src="127.0.0.3"):
        st, body = http("GET", f"http://127.0.0.1:{p1}/health", src=src)
        return st, (json.loads(body) if st == 200 else {})

    def health2(src="127.0.0.3"):
        st, body = http("GET", f"http://127.0.0.1:{p2}/health", src=src)
        return st, (json.loads(body) if st == 200 else {})

    try:
        up = False
        for _ in range(60):
            if health1()[0] == 200 and health2()[0] == 200:
                up = True
                break
            time.sleep(0.1)
        check(1, "both services started", up)

        st, h = health1()
        check(2, "/health reports version 1.5.6 and the new counters",
              st == 200 and h.get("version") == "1.5.6"
              and h.get("rejected") == 0 and h.get("banned") == 0
              and "banned_dropped" in h and "conns" in h)

        # ================= S1: the box fed by the per-IP WS cap ====================
        ok_legit = all(raw_get(p1, src="127.0.0.2").startswith(b"HTTP/1.1 200")
                       for _ in range(5))
        check(3, "5 paced requests from 127.0.0.2 all pass (200)", ok_legit)

        code, sock = ws_open(p1, "127.0.0.2")
        if sock:
            held.append(sock)
        check(4, "the first WS upgrade from .2 passes (101), the slot is held",
              code == "101" and sock is not None)
        codes = {"101": 0, "429": 0, "drop": 0}
        for _ in range(30):
            code, s2sock = ws_open(p1, "127.0.0.2")
            codes[code] = codes.get(code, 0) + 1
        check(5, "the WS-cap flood: some 429s, then silent accept-level drops",
              codes["429"] > 0 and (codes["drop"] + codes.get("0", 0)) > 0)
        print(f"     ws-cap flood breakdown: {codes}")

        buf = raw_get(p1, src="127.0.0.2")
        check(6, "a banned IP gets NO http response at all (accept-level drop)",
              not buf.startswith(b"HTTP/1.1"))

        st, h = health1()
        check(7, "/health: banned=1 and banned_dropped>0 while the ban is live",
              h.get("banned") == 1 and h.get("banned_dropped", 0) >= 1)
        check(8, "/health: rejected counted every 429 (>= PENALTY_HITS)",
              h.get("rejected", 0) >= 10)

        log = open(log1).read()
        check(9, "the ban logged exactly one [penalty] line",
              log.count("[penalty] 127.0.0.2 banned") == 1)
        n429 = sum(1 for line in log.splitlines() if line.rstrip().endswith("429 -"))
        check(10, "429 log lines are SAMPLED (10+ rejections -> a couple of lines)",
              1 <= n429 <= 5 and codes["429"] >= 3)

        if held and held[0]:
            held[0].close()      # free the WS slot before the release check
        held = []
        deadline = time.time() + 8
        released = False
        while time.time() < deadline:
            time.sleep(0.5)
            if raw_get(p1, src="127.0.0.2").startswith(b"HTTP/1.1 200"):
                released = True
                break
        check(11, "after the ban TTL the IP works again (release path)", released)
        log = open(log1).read()
        check(12, "the release logged one [penalty] released line",
              log.count("[penalty] 127.0.0.2 released") == 1)

        # --- re-ban: a fresh storm re-bans the IP ---------------------------------
        code, sock = ws_open(p1, "127.0.0.2")
        if sock:
            held.append(sock)
        re_banned = False
        for _ in range(15):
            code, s2sock = ws_open(p1, "127.0.0.2")
            if code in ("drop", "0"):
                re_banned = True
                break
        check(13, "a fresh storm re-bans the IP", re_banned)
        for s in held:
            if s:
                s.close()
        held = []
        time.sleep(4.0)          # let the second ban expire for a clean WS-cap phase

        # --- WS-cap 429s from another IP: counted, but 2 hits do not ban ----------
        st_before, h_before = health1()
        code, sock = ws_open(p1, "127.0.0.4")
        if sock:
            held.append(sock)
        more = [ws_open(p1, "127.0.0.4")[0] for _ in range(2)]
        st_after, h_after = health1()
        check(14, "WS-cap 429s feed 'rejected'; 2 hits do not ban",
              code == "101" and more == ["429", "429"]
              and h_after.get("rejected", 0) >= h_before.get("rejected", 0) + 2
              and h_after.get("banned") == 0)

        st, h = health1()
        check(15, "conns counts the live WS session", h.get("conns", 0) >= 1)

        # ================= S2: the box fed by the rate limiter =====================
        ok_legit = all(raw_get(p2, src="127.0.0.2").startswith(b"HTTP/1.1 200")
                       for _ in range(5))
        check(16, "S2: 5 paced requests pass (200)", ok_legit)
        codes = {"200": 0, "429": 0, "drop": 0}
        for _ in range(80):
            buf = raw_get(p2, src="127.0.0.2")
            if buf.startswith(b"HTTP/1.1 200"):
                codes["200"] += 1
            elif buf.startswith(b"HTTP/1.1 429"):
                codes["429"] += 1
            else:
                codes["drop"] += 1
        check(17, "S2: the flood saw all three stages (200s -> 429s -> drops)",
              codes["200"] > 0 and codes["429"] >= 3 and codes["drop"] > 0)
        print(f"     http flood breakdown: {codes}")
        st, h = health2()
        check(18, "S2: banned=1, banned_dropped>0, rejected>=10",
              h.get("banned") == 1 and h.get("banned_dropped", 0) >= 1
              and h.get("rejected", 0) >= 10)
        log = open(log2).read()
        n429 = sum(1 for line in log.splitlines() if line.rstrip().endswith("429 -"))
        check(19, "S2: one [penalty] line, no release yet, sampled 429 lines",
              log.count("[penalty] 127.0.0.2 banned") == 1
              and "released" not in log and 1 <= n429 <= 5)

    finally:
        for s in held:
            try:
                if s:
                    s.close()
            except OSError:
                pass
        for proc in (s1, s2):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if FAIL:
            for name in ("s1", "s2"):
                path = os.path.join(work, f"{name}.log")
                print(f"---- {name} log (tail) ----")
                print("\n".join(open(path).read().splitlines()[-25:]))
        print(f"\nlogs: {work}")

    print(f"PENALTY 1.5.6 SMOKE: PASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
