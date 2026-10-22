#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lucario Morph Sync — a lightweight morph synchronization service.

The Lucario Morph mod (Fabric, client-side at heart) cannot pass player morphs
through the Minecraft server itself on servers WITHOUT the mod. This service is a
shared "player UUID → morph variant" store where clients publish their own morph
and read the morphs of others.

Protocol (v1):
  GET    /health                 -> {"ok": true, "players": N}
  GET    /v1/morphs?uuids=A,B,C  -> {"<uuid>": "<variant>", ...}   (known ones only)
  PUT    /v1/morphs/<uuid>       body {"variant": "lucariocaptain_shiny"} or {"variant": ""} (clear)
  DELETE /v1/morphs/<uuid>       -> clear the morph
  POST   /v1/auth/challenge      {"uuid","name"} -> {"serverId"}          (record authorization, step 1)
  POST   /v1/auth/verify         {"uuid","name","serverId"} -> {"token"}  (step 3, verified via Mojang)

Safety rules:
  * only UUIDs are accepted (canonicalized) and only variants from the mod's
    whitelist;
  * per-IP request rate limit (RATE_PER_MIN);
  * request body no larger than 4 KB, at most 100 UUIDs per GET;
  * "slow" connections are closed after 30 seconds of inactivity (slowloris);
  * a UUID can be protected with a write token: the client proves account ownership
    via the Mojang session servers (challenge -> join -> verify), after which PUT/DELETE
    for that UUID require the X-Lucario-Token header. Opt-in protection: a UUID without
    a token is written freely (maximum harm — someone else's cosmetic morph);
  * the challenge is one-shot, lives CHALLENGE_TTL seconds and is bound to IP, UUID and name;
  * the Mojang call happens OUTSIDE all locks and with a hard timeout (resilience);
  * no more than MAX_RECORDS records (507 when full);
  * records older than TTL are removed (TTL_HOURS, or TTL_DAYS when zero): mod clients
    1.4.2+ refresh their own record every heartbeatMinutes minutes and on every world
    join, so a record without updates is abandoned (the mod was uninstalled).

Storage — JSON files (DATA_DIR/morphs.json and DATA_DIR/tokens.json). Disk writes are
batched (every 5 seconds when there are changes) and happen on container shutdown
(SIGTERM/SIGINT).
No dependencies: Python 3.10+ standard library only.
"""
import hashlib
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
import uuid as uuidlib
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

# --- Variant whitelist (must match the mod's Variants.java) ---
BASES = [
    "lucario", "lucariocafe", "lucariocaptain", "lucariochef", "lucarioconcert",
    "lucariocostumeparty", "lucarioholiday", "lucariomartialarts", "lucariomega",
    "lucariopmd", "lucariopsmd", "lucarioruins", "lucariospace",
    "riolu", "riolupmd", "riolupsmd",
]
ALLOWED = set(BASES) | {b + "_shiny" for b in BASES}

# --- Settings (via environment) ---
PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DATA_FILE = os.path.join(DATA_DIR, "morphs.json")
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "120"))
TTL_DAYS = float(os.environ.get("TTL_DAYS", "30"))
# TTL_HOURS (> 0) overrides TTL_DAYS — handy for short TTLs (1..6 hours):
# a heartbeat client refreshes its record every few minutes, so a short TTL is
# safe, while abandoned records (mod uninstalled) expire on their own within TTL + HOUSEKEEPING.
TTL_HOURS = float(os.environ.get("TTL_HOURS", "0"))
TTL_SECONDS = int(TTL_HOURS * 3600) if TTL_HOURS > 0 else int(TTL_DAYS * 86400)
HOUSEKEEPING_SECONDS = max(1.0, float(os.environ.get("HOUSEKEEPING_SECONDS", "3600")))
MAX_RECORDS = int(os.environ.get("MAX_RECORDS", "100000"))
FLUSH_SECONDS = float(os.environ.get("FLUSH_SECONDS", "5"))
MAX_BODY = 4096
MAX_UUIDS = 100

# --- Record authorization (Mojang session servers) ---
MOJANG_SESSION = os.environ.get("MOJANG_SESSION", "https://sessionserver.mojang.com").rstrip("/")
CHALLENGE_TTL = float(os.environ.get("CHALLENGE_TTL", "300"))
MAX_PENDING = int(os.environ.get("MAX_PENDING", "10000"))
TOK_FILE = os.path.join(DATA_DIR, "tokens.json")
NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
SERVER_ID_RE = re.compile(r"^[0-9a-f]{40}$")

STORE = {}                     # uuid (canonical) -> {"variant": str, "ts": float}
TOKENS = {}                    # uuid (canonical) -> {"token", "name", "ts"}  (write tokens)
PENDING = {}                   # serverId -> {"uuid", "name", "ip", "ts"}  (live challenges)
DIRTY = False                  # unsaved morphs.json changes exist (under LOCK)
TOK_DIRTY = False              # unsaved tokens.json changes exist (under LOCK)
LOCK = threading.Lock()        # STORE/TOKENS/DIRTY/TOK_DIRTY; leaf lock: no network I/O or other locks under it
PENDING_LOCK = threading.Lock()  # PENDING; a leaf lock too
RATE = defaultdict(deque)
RATE_LOCK = threading.Lock()


def load() -> None:
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        now = time.time()
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, str) or v not in ALLOWED:
                continue
            try:
                canonical = str(uuidlib.UUID(k))
            except ValueError:
                continue
            STORE[canonical] = {"variant": v, "ts": now}  # ts unknown — treat as fresh
        print(f"loaded {len(STORE)} records from {DATA_FILE}", flush=True)
    except Exception as e:  # noqa: BLE001 — the service must not crash on a malformed file
        print(f"cannot load {DATA_FILE}: {e!r}", flush=True)


def save_locked() -> None:
    """Atomically write the whole store. Call only while holding LOCK."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: rec["variant"] for k, rec in STORE.items()}, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def save_tokens_locked() -> None:
    """Atomically write the tokens. Call only while holding LOCK."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TOK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(TOKENS, f, ensure_ascii=False)
    os.replace(tmp, TOK_FILE)


def set_token(uuid_str: str, name: str) -> str:
    """Creates or returns the existing write token of a UUID.

    An already issued token is returned as is — this is how the client regains
    access after deleting its config: identity is re-proven via Mojang,
    and the service hands out the same token.
    """
    global TOK_DIRTY
    with LOCK:
        rec = TOKENS.get(uuid_str)
        if rec is not None:
            return rec["token"]
        tok = secrets.token_urlsafe(32)
        TOKENS[uuid_str] = {"token": tok, "name": name, "ts": time.time()}
        TOK_DIRTY = True
    return tok


def flush_now() -> None:
    """Flushes pending changes to disk, if any."""
    global DIRTY, TOK_DIRTY
    with LOCK:
        m_dirty, DIRTY = DIRTY, False
        t_dirty, TOK_DIRTY = TOK_DIRTY, False
        if m_dirty:
            save_locked()
        if t_dirty:
            save_tokens_locked()


def set_morph(uuid_str: str, variant: str) -> bool:
    """Upserts a record. Returns False when the store is full."""
    global DIRTY
    with LOCK:
        if variant:
            if uuid_str not in STORE and len(STORE) >= MAX_RECORDS:
                return False
            STORE[uuid_str] = {"variant": variant, "ts": time.time()}
        else:
            STORE.pop(uuid_str, None)
        DIRTY = True
    return True


def flusher() -> None:
    """Batched disk flush: at most once per FLUSH_SECONDS."""
    while True:
        time.sleep(FLUSH_SECONDS)
        flush_now()


def housekeeping() -> None:
    """Every HOUSEKEEPING_SECONDS, drops expired records and trims rate-limit counters."""
    while True:
        time.sleep(HOUSEKEEPING_SECONDS)
        global DIRTY
        now = time.time()
        with PENDING_LOCK:
            stale_ch = [k for k, v in PENDING.items() if now - v["ts"] > CHALLENGE_TTL]
            for k in stale_ch:
                del PENDING[k]
        stale = []
        cutoff = now - TTL_SECONDS
        with LOCK:
            stale = [k for k, rec in STORE.items() if rec["ts"] < cutoff]
            for k in stale:
                del STORE[k]
            if stale:
                DIRTY = True  # the flusher will persist it
        with RATE_LOCK:
            now = time.time()
            for ip in list(RATE.keys()):
                q = RATE[ip]
                while q and now - q[0] > 60:
                    q.popleft()
                if not q:
                    del RATE[ip]
        if stale:
            print(f"housekeeping: dropped {len(stale)} stale records", flush=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


class Handler(BaseHTTPRequestHandler):
    server_version = "LucarioSync/1.4.2"
    protocol_version = "HTTP/1.1"
    # Close "slow" connections: protection against slowloris thread exhaustion.
    timeout = 30

    def log_message(self, fmt, *args):  # noqa: A003 — standard hook
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.client_address[0]} {fmt % args}", flush=True)

    # ------------------------------------------------------------- helpers
    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rate_ok(self) -> bool:
        ip = self.client_address[0]
        now = time.time()
        with RATE_LOCK:
            q = RATE[ip]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= RATE_PER_MIN:
                return False
            q.append(now)
            return True

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > MAX_BODY:
            return None
        return self.rfile.read(n)

    @staticmethod
    def _canonical_uuid(raw: str):
        """Canonical UUID form or None. Accepts 'urn:uuid:...', {...} and other equivalent spellings."""
        try:
            return str(uuidlib.UUID(raw))
        except (ValueError, AttributeError, TypeError):
            return None

    def _authorized(self, uuid_str: str) -> bool:
        """Verifies the write token. A UUID without a token is open for writing (opt-in model)."""
        with LOCK:
            rec = TOKENS.get(uuid_str)
        if rec is None:
            return True
        supplied = (self.headers.get("X-Lucario-Token") or "").strip()
        try:
            if supplied and secrets.compare_digest(supplied.encode("utf-8"), rec["token"].encode("utf-8")):
                return True
        except Exception:  # noqa: BLE001 — malformed header = no access
            pass
        self._send(401, {"error": "morph record is protected; the client authorizes itself via Mojang and sends X-Lucario-Token"})
        return False

    # ------------------------------------------------------------- GET
    def do_GET(self):  # noqa: N802 — name required by BaseHTTPRequestHandler
        if not self._rate_ok():
            return self._send(429, {"error": "rate limit, slow down"})
        parts = urlsplit(self.path)
        if parts.path == "/health":
            with LOCK:
                return self._send(200, {"ok": True, "players": len(STORE)})
        if parts.path == "/v1/morphs":
            raw = (parse_qs(parts.query).get("uuids") or [""])[0]
            ids = [s.strip() for s in raw.split(",") if s.strip()][:MAX_UUIDS]
            out = {}
            with LOCK:
                for u in ids:
                    canonical = self._canonical_uuid(u)
                    if canonical is None:
                        continue
                    rec = STORE.get(canonical)
                    if rec:
                        out[canonical] = rec["variant"]
            return self._send(200, out)
        return self._send(404, {"error": "not found"})

    # ------------------------------------------------------------- POST (record authorization)
    def do_POST(self):  # noqa: N802
        if not self._rate_ok():
            return self._send(429, {"error": "rate limit, slow down"})
        parts = urlsplit(self.path)
        body = self._read_body()
        if body is None:
            return self._send(400, {"error": "missing or too large body"})
        try:
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "bad json"})
        if parts.path == "/v1/auth/challenge":
            return self._auth_challenge(data)
        if parts.path == "/v1/auth/verify":
            return self._auth_verify(data)
        return self._send(404, {"error": "not found"})

    def _auth_challenge(self, data) -> None:
        """Step 1: issues a one-shot serverId bound to IP + UUID + name.
        The response is sent OUTSIDE PENDING_LOCK (no I/O under the lock)."""
        uuid_str = self._canonical_uuid(str(data.get("uuid") or ""))
        name = str(data.get("name") or "")
        if uuid_str is None or not NAME_RE.match(name):
            return self._send(400, {"error": "bad uuid or name"})
        server_id = hashlib.sha1(secrets.token_bytes(32)).hexdigest()
        with PENDING_LOCK:
            now = time.time()
            if len(PENDING) >= MAX_PENDING:
                for k in [x for x, v in PENDING.items() if now - v["ts"] > CHALLENGE_TTL]:
                    del PENDING[k]
            full = len(PENDING) >= MAX_PENDING
            if not full:
                PENDING[server_id] = {"uuid": uuid_str, "name": name,
                                      "ip": self.client_address[0], "ts": now}
        if full:
            return self._send(503, {"error": "too many pending challenges"})
        return self._send(200, {"serverId": server_id, "ttl": int(CHALLENGE_TTL)})

    def _auth_verify(self, data) -> None:
        """Step 3: checks the serverId with Mojang and issues/returns the write token."""
        uuid_str = self._canonical_uuid(str(data.get("uuid") or ""))
        name = str(data.get("name") or "")
        server_id = str(data.get("serverId") or "")
        if uuid_str is None or not NAME_RE.match(name) or not SERVER_ID_RE.match(server_id):
            return self._send(400, {"error": "bad uuid, name or serverId"})
        with PENDING_LOCK:
            challenge = PENDING.pop(server_id, None)  # one-shot: popped — it burns
        if (challenge is None or time.time() - challenge["ts"] > CHALLENGE_TTL
                or challenge["uuid"] != uuid_str or challenge["name"] != name
                or challenge["ip"] != self.client_address[0]):
            return self._send(400, {"error": "unknown or expired challenge; start over"})
        ok, profile_uuid = self._mojang_has_joined(name, server_id)
        if not ok or profile_uuid != uuid_str:
            return self._send(403, {"error": "Mojang verification failed (licensed account required)"})
        token = set_token(uuid_str, name)
        return self._send(200, {"ok": True, "token": token})

    @staticmethod
    def _mojang_has_joined(name: str, server_id: str):
        """Asks Mojang: did the player with this name perform a join with this serverId?

        Returns (ok, canonical_uuid | None). A network call with a hard 8 s timeout;
        NEVER call under LOCK/PENDING_LOCK — otherwise a slow Mojang would block
        the whole service.
        """
        url = (MOJANG_SESSION + "/session/minecraft/hasJoined?username="
               + quote(name) + "&serverId=" + server_id)
        try:
            req = Request(url, headers={"User-Agent": "LucarioMorphSync/1.4.2"})
            with urlopen(req, timeout=8) as resp:
                if resp.status != 200:
                    return False, None
                prof = json.loads(resp.read(4096).decode("utf-8"))
        except HTTPError:
            return False, None  # 204 and other non-2xx — no confirmation
        except Exception:  # noqa: BLE001 — Mojang unreachable / garbage in the response
            return False, None
        raw = prof.get("id") if isinstance(prof, dict) else None
        if not isinstance(raw, str):
            return False, None
        try:
            return True, str(uuidlib.UUID(raw))
        except ValueError:
            return False, None

    # ------------------------------------------------------------- PUT
    def do_PUT(self):  # noqa: N802
        if not self._rate_ok():
            return self._send(429, {"error": "rate limit, slow down"})
        parts = urlsplit(self.path)
        if not parts.path.startswith("/v1/morphs/"):
            return self._send(404, {"error": "not found"})
        uuid_str = self._canonical_uuid(parts.path[len("/v1/morphs/"):])
        if uuid_str is None:
            return self._send(400, {"error": "bad uuid"})
        body = self._read_body()
        if body is None:
            return self._send(400, {"error": "missing or too large body"})
        if not self._authorized(uuid_str):
            return
        try:
            data = json.loads(body.decode("utf-8"))
            variant = data.get("variant")
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "bad json"})
        if not isinstance(variant, str):
            return self._send(400, {"error": "variant must be a string"})
        variant = variant.strip().lower()
        if variant and variant not in ALLOWED:
            return self._send(422, {"error": "unknown variant"})
        if not set_morph(uuid_str, variant):
            return self._send(507, {"error": "storage full"})
        return self._send(200, {"ok": True})

    # ------------------------------------------------------------- DELETE
    def do_DELETE(self):  # noqa: N802
        if not self._rate_ok():
            return self._send(429, {"error": "rate limit, slow down"})
        parts = urlsplit(self.path)
        if not parts.path.startswith("/v1/morphs/"):
            return self._send(404, {"error": "not found"})
        uuid_str = self._canonical_uuid(parts.path[len("/v1/morphs/"):])
        if uuid_str is None:
            return self._send(400, {"error": "bad uuid"})
        if not self._authorized(uuid_str):
            return
        if not set_morph(uuid_str, ""):
            return self._send(507, {"error": "storage full"})
        return self._send(200, {"ok": True})


def load_tokens() -> None:
    if not os.path.exists(TOK_FILE):
        return
    try:
        with open(TOK_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            tok = v.get("token")
            if not isinstance(tok, str) or not tok:
                continue
            try:
                canonical = str(uuidlib.UUID(k))
            except ValueError:
                continue
            TOKENS[canonical] = {"token": tok,
                                 "name": str(v.get("name") or ""),
                                 "ts": float(v.get("ts") or time.time())}
        print(f"loaded {len(TOKENS)} write-tokens from {TOK_FILE}", flush=True)
    except Exception as e:  # noqa: BLE001 — the service must not crash on a malformed file
        print(f"cannot load {TOK_FILE}: {e!r}", flush=True)


def main() -> None:
    load()
    load_tokens()
    threading.Thread(target=flusher, daemon=True).start()
    threading.Thread(target=housekeeping, daemon=True).start()
    # SIGTERM from docker stop: exit cleanly; the finally block saves the data.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = Server(("0.0.0.0", PORT), Handler)
    ttl_desc = f"{TTL_HOURS:g} hours" if TTL_HOURS > 0 else f"{TTL_DAYS:g} days"
    print(f"Lucario Morph Sync listening on 0.0.0.0:{PORT} "
          f"(variants: {len(ALLOWED)}, ttl: {ttl_desc}, housekeeping every {HOUSEKEEPING_SECONDS:g}s, "
          f"max records: {MAX_RECORDS}, flush every {FLUSH_SECONDS}s, auth via {MOJANG_SESSION}, "
          f"write-tokens: {len(TOKENS)})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("bye", flush=True)
    finally:
        flush_now()


if __name__ == "__main__":
    main()
