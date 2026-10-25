#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lucario Morph Sync — integration tests of STRICT mode (1.5.4) + the per-IP WS cap.

Starts a Mojang stub (mojang_stub.py) and TWO service.py instances:
  S1) strict:   REQUIRE_AUTH is NOT set — the 1.5.4 default applies (=1);
                MAX_WS_PER_IP=2 (small on purpose: the cap must bite in a test)
  S2) legacy:   REQUIRE_AUTH=0 — the pre-1.5.4 opt-in behavior must still work

Covers: writes without a token -> 401 (morphs, anims, DELETE), reading stays
public, the full claim chain via the stub (challenge -> join -> verify -> token),
writes with the token -> 200, cross-UUID tokens are rejected, re-claim returns
the SAME token, verify without a join -> 403, the per-IP WebSocket cap (2 live
sockets from one IP, the 3rd upgrade -> 429, a freed slot is reusable), /health
live counts, and the legacy instance accepting tokenless writes.

Run:  python3 tests/strict_smoke.py
"""
import json
import os
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


def http(method, url, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Lucario-Token"] = token
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception:  # noqa: BLE001 — refused connection etc.
        return 0, {}


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def claim(base, stub_port, uuid_str, name="TestPlayer", do_join=True):
    """challenge -> (join) -> verify. Returns the token or None."""
    st, ch = http("POST", base + "/v1/auth/challenge",
                  {"uuid": uuid_str, "name": name})
    if st != 200 or not ch.get("serverId"):
        return None
    if do_join:
        http("POST", f"http://127.0.0.1:{stub_port}/session/minecraft/join",
             {"accessToken": "stub-token", "selectedProfile": uuid_str.replace("-", ""),
              "serverId": ch["serverId"]})
    st, v = http("POST", base + "/v1/auth/verify",
                 {"uuid": uuid_str, "name": name, "serverId": ch["serverId"]})
    if st != 200:
        return None
    return v.get("token")


def main():
    global PASS, FAIL
    stub_port, s1_port, s2_port = free_port(), free_port(), free_port()
    work = tempfile.mkdtemp(prefix="lucario-strict-")
    stub = subprocess.Popen([sys.executable, os.path.join(HERE, "mojang_stub.py"),
                             str(stub_port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = dict(os.environ, PORT=str(s1_port),
               DATA_DIR=os.path.join(work, "s1"),
               RATE_PER_MIN="1000", MAX_WS_PER_IP="2",
               MOJANG_SESSION=f"http://127.0.0.1:{stub_port}")
    # NOTE: REQUIRE_AUTH is deliberately NOT set here — the 1.5.4 default (=1) must hold.
    s1 = subprocess.Popen([sys.executable, os.path.join(ROOT, "service.py")], env=env,
                          stdout=open(os.path.join(work, "s1.log"), "w"),
                          stderr=subprocess.STDOUT)
    env2 = dict(os.environ, PORT=str(s2_port),
                DATA_DIR=os.path.join(work, "s2"),
                RATE_PER_MIN="1000", REQUIRE_AUTH="0",
                MOJANG_SESSION=f"http://127.0.0.1:{stub_port}")
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
        check(1, "both services started (strict default on, legacy opt-in on)",
              up1 and up2)
        S1 = f"http://127.0.0.1:{s1_port}"
        S2 = f"http://127.0.0.1:{s2_port}"

        # --- strict mode: reads public, writes 401 --------------------------------
        st, h = http("GET", S1 + "/health")
        check(2, "strict service /health -> 200, version 1.5.6",
              st == 200 and h.get("version") == "1.5.6")
        st, body = http("GET", S1 + f"/v1/morphs?uuids={U1}")
        check(3, "GET /v1/morphs stays public -> 200 {}", st == 200 and body == {})
        st, _ = http("PUT", S1 + f"/v1/morphs/{U1}", {"variant": "lucario"})
        check(4, "strict: PUT morph without a token -> 401", st == 401)
        st, _ = http("PUT", S1 + f"/v1/anims/{U1}",
                     {"anim": "dance", "state": "start", "ts": 123})
        check(5, "strict: PUT anim without a token -> 401", st == 401)
        st, _ = http("DELETE", S1 + f"/v1/morphs/{U1}")
        check(6, "strict: DELETE without a token -> 401", st == 401)
        st, body = http("GET", S1 + f"/v1/morphs?uuids={U1}")
        check(7, "the 401s wrote NOTHING (the store is still empty)", st == 200 and body == {})

        # --- the claim chain through the stub --------------------------------------
        t1 = claim(S1, stub_port, U1)
        check(8, "challenge + join + verify -> a write token is issued",
              bool(t1) and len(t1) >= 32)
        st, body = http("GET", S1 + f"/v1/morphs?uuids={U1}")
        t2_bad = claim(S1, stub_port, U3, do_join=False)
        check(9, "verify WITHOUT a prior join -> no token (Mojang did not confirm)",
              t2_bad is None)

        st, _ = http("PUT", S1 + f"/v1/morphs/{U1}", {"variant": "lucariocaptain"}, token=t1)
        check(10, "strict: PUT morph WITH the token -> 200", st == 200)
        st, body = http("GET", S1 + f"/v1/morphs?uuids={U1}")
        check(11, "the stored variant reads back", body.get(U1) == "lucariocaptain")
        st, _ = http("PUT", S1 + f"/v1/anims/{U1}",
                     {"anim": "dance", "state": "start", "ts": 456}, token=t1)
        check(12, "strict: PUT anim WITH the token -> 200", st == 200)
        st, _ = http("PUT", S1 + f"/v1/morphs/{U2}", {"variant": "riolu"}, token=t1)
        check(13, "a token of ANOTHER uuid does not unlock a stranger's record -> 401",
              st == 401)
        st, _ = http("PUT", S1 + f"/v1/morphs/{U1}",
                     {"variant": "lucario"}, token="not-the-token")
        check(14, "a WRONG token -> 401", st == 401)
        st, _ = http("DELETE", S1 + f"/v1/morphs/{U1}", token=t1)
        check(15, "strict: DELETE WITH the token -> 200", st == 200)
        st, body = http("GET", S1 + f"/v1/morphs?uuids={U1}")
        check(16, "the record is gone after the authorized DELETE", body == {})
        t1b = claim(S1, stub_port, U1)
        check(17, "re-authorization returns the SAME token", t1b == t1)

        # --- per-IP WebSocket cap (MAX_WS_PER_IP=2 on the strict instance) ---------
        w1 = WsClient(s1_port)
        w1.send({"type": "hello", "uuid": U1})
        ack = w1.recv_until(lambda m: m.get("type") == "hello", timeout=3)
        check(18, "ws slot 1: 101 + hello ack (reading over WS stays public)",
              ack is not None and ack.get("ok") is True)
        w2 = WsClient(s1_port)
        check(19, "ws slot 2: 101 (the cap is 2)", True)
        st, _ = http("GET", S1 + "/health")
        check(20, "/health counts the two live sockets (live: 2)",
              st == 200 and http("GET", S1 + "/health")[1].get("live") == 2)
        third_refused = False
        try:
            WsClient(s1_port)
        except ConnectionError as e:
            third_refused = " 429 " in str(e)
        check(21, "ws slot 3 from the same IP -> upgrade refused with 429", third_refused)
        w1.close()
        time.sleep(0.5)
        w3 = None
        err = None
        try:
            w3 = WsClient(s1_port)
        except ConnectionError as e:
            err = e
        check(22, "a freed slot is reusable (per-IP release works)", w3 is not None)
        w2.close()
        if w3:
            w3.close()
        time.sleep(0.5)

        # --- legacy instance: the old opt-in behavior is intact --------------------
        st, _ = http("PUT", S2 + f"/v1/morphs/{U1}", {"variant": "lucario"})
        check(23, "legacy (REQUIRE_AUTH=0): PUT without a token -> 200", st == 200)
        st, _ = http("DELETE", S2 + f"/v1/morphs/{U1}")
        check(24, "legacy: DELETE without a token -> 200", st == 200)

        st, h = http("GET", S1 + "/health")
        check(25, "the strict service is alive at the end (nothing crashed)",
              st == 200 and h.get("ok") is True)

        log1 = open(os.path.join(work, "s1.log")).read()
        check(26, "startup log says: strict auth on", "strict auth: on" in log1)
    finally:
        for p in (s1, s2, stub):
            p.kill()
        for p in (s1, s2, stub):
            p.wait()

    print(flush=True)
    print("=" * 50)
    print(f"STRICT SMOKE: PASS={PASS} FAIL={FAIL}")
    print("=" * 50)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
