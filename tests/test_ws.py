#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lucario Morph Sync — integration tests of the EMOTE channel (HTTP + WebSocket).

Starts its own service.py instance with a fast TTL (ANIM_TTL_SECONDS=2,
ANIM_SWEEP_SECONDS=1) and runs the full emote scenario through the stdlib WS
client (ws_probe.py): push delivery, watch filtering, snapshots, explicit stop,
TTL expiry and input validation. Exits non-zero on any failure.

Run standalone:  python3 tests/test_ws.py
Run from bash:   bash tests/test_auth_flow.sh  (calls this file)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from ws_probe import WsClient  # noqa: E402

U1 = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
U2 = "11111111-2222-3333-4444-555555555555"
U3 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

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


def http(method, url, body=None):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    global PASS, FAIL
    port = free_port()
    work = tempfile.mkdtemp(prefix="lucario-ws-")
    env = dict(os.environ)
    env.update({
        "PORT": str(port),
        "DATA_DIR": os.path.join(work, "data"),
        "RATE_PER_MIN": "1000",
        "ANIM_TTL_SECONDS": "2",
        "ANIM_SWEEP_SECONDS": "1",
        "WS_PING_SECONDS": "2",
    })
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "service.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                if http("GET", base + "/health")[0] == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        else:
            print("service did not start")
            return 1

        # 1. validation of the emote API ------------------------------------------------
        code, _ = http("PUT", f"{base}/v1/anims/{U1}", {"anim": "dance", "state": "start", "ts": 123})
        check(1, "PUT anim start -> 200", code == 200)
        code, _ = http("PUT", f"{base}/v1/anims/{U1}", {"anim": "not ok!", "state": "start"})
        check(2, "PUT anim with a bad name -> 422", code == 422)
        code, _ = http("PUT", f"{base}/v1/anims/{U1}", {"anim": "dance", "state": "jump"})
        check(3, "PUT anim with a bad state -> 400", code == 400)
        code, _ = http("PUT", f"{base}/v1/anims/not-a-uuid", {"anim": "dance", "state": "start"})
        check(4, "PUT anim with a malformed UUID -> 400", code == 400)

        code, body = http("GET", f"{base}/v1/anims?uuids={U1},{U2}")
        rec = body.get("anims", {}).get(U1, {})
        check(5, "GET /v1/anims returns the active record", code == 200
              and rec.get("anim") == "dance" and rec.get("active") is True)

        # 2. WebSocket: hello + snapshot-on-watch ---------------------------------------
        w1 = WsClient(port)
        w1.send({"type": "hello", "uuid": U3})
        msg = w1.recv_until(lambda m: m.get("type") == "hello", timeout=3)
        check(6, "hello -> ack", msg is not None and msg.get("ok") is True)

        w1.send({"type": "watch", "uuids": [U1]})
        msg = w1.recv_until(lambda m: m.get("type") == "state", timeout=3)
        check(7, "watch -> snapshot carries the current record", msg is not None
              and msg.get("anims", {}).get(U1, {}).get("anim") == "dance"
              and msg["anims"][U1]["active"] is True and "cursor" in msg)

        # 3. push delivery + interest filtering -----------------------------------------
        w2 = WsClient(port)
        w2.send({"type": "hello", "uuid": U2})
        w2.send({"type": "watch", "uuids": [U2]})   # watches someone ELSE
        w2.recv_until(lambda m: m.get("type") == "state", timeout=3)

        http("PUT", f"{base}/v1/anims/{U1}", {"anim": "specialist_full", "state": "start", "ts": 456})
        msg = w1.recv_until(lambda m: m.get("type") == "anim", timeout=3)
        check(8, "a watched start is pushed instantly", msg is not None
              and msg.get("uuid") == U1 and msg.get("anim") == "specialist_full"
              and msg.get("active") is True)
        noise = w2.recv_until(lambda m: m.get("type") == "anim", timeout=1.2)
        check(9, "an unrelated event is NOT pushed to other watchers", noise is None)

        http("PUT", f"{base}/v1/anims/{U1}", {"anim": "specialist_full", "state": "stop", "ts": 789})
        msg = w1.recv_until(lambda m: m.get("type") == "anim" and m.get("active") is False, timeout=3)
        check(10, "a watched stop is pushed instantly", msg is not None
              and msg.get("uuid") == U1 and msg.get("active") is False)

        # 4. TTL: a crashed player's emote expires and the expiry is PUSHED --------------
        http("PUT", f"{base}/v1/anims/{U1}", {"anim": "dance", "state": "start", "ts": 1000})
        w1.recv_until(lambda m: m.get("type") == "anim", timeout=2)  # drain the start
        msg = w1.recv_until(lambda m: m.get("type") == "anim" and m.get("active") is False,
                            timeout=6)
        check(11, "an unrefreshed emote expires by TTL and the expiry is pushed", msg is not None)

        code, body = http("GET", f"{base}/v1/anims?uuids={U1}")
        check(12, "after TTL the record reads as inactive", code == 200
              and body.get("anims", {}).get(U1, {}).get("active") is False)

        # 5. watch replacement + a dead record leaves the snapshot -----------------------
        w1.send({"type": "watch", "uuids": [U2]})
        msg = w1.recv_until(lambda m: m.get("type") == "state", timeout=3)
        check(13, "re-watch -> a fresh snapshot of the new set", msg is not None
              and U2 not in msg.get("anims", {}))

        code, body = http("GET", base + "/health")
        check(14, "/health counts live sockets", code == 200 and body.get("live", 0) >= 2)

        w1.close()
        w2.close()
        return 0 if FAIL == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)
        print(f"WS SUITE: PASS={PASS} FAIL={FAIL}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
