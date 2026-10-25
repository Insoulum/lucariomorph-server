#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lucario Morph Sync — a lightweight morph synchronization service.

The Lucario Morph mod (Fabric, client-side at heart) cannot pass player morphs
through the Minecraft server itself on servers WITHOUT the mod. This service is a
shared "player UUID → morph variant" store where clients publish their own morph
and read the morphs of others.

Protocol (v1 — morphs):
  GET    /health                 -> {"ok": true, "version": "...", "players": N,
                                             "mods": P, "emotes": M, "live": K,
                                             "rejected": R, "banned": B,
                                             "banned_dropped": D, "conns": C}
  GET    /v1/morphs?uuids=A,B,C  -> {"<uuid>": "<variant>", ...}   (known ones only)
  PUT    /v1/morphs/<uuid>       body {"variant": "lucariocaptain_shiny"} or {"variant": ""} (clear)
  DELETE /v1/morphs/<uuid>       -> clear the morph
  POST   /v1/auth/challenge      {"uuid","name"} -> {"serverId"}          (record authorization, step 1)
  POST   /v1/auth/verify         {"uuid","name","serverId"} -> {"token"}  (step 3, verified via Mojang)

Protocol (v1.5 — emote animations, since 1.5.0):
  GET    /v1/anims?uuids=A,B,C   -> {"anims": {"<uuid>": {"anim","active","ts","age"}}, "cursor": N}
  PUT    /v1/anims/<uuid>        body {"anim": "dance", "state": "start"|"stop", "ts": <epoch ms>}
  GET    /ws                     -> RFC 6455 WebSocket (JSON text frames), the real-time channel:
                                  C: {"type":"hello","uuid":...}          (first message, required)
                                  C: {"type":"watch","uuids":[...]}       (replace the interest set, <= 100)
                                  S: {"type":"state","anims":{...},"cursor":N}  (a snapshot of the watched set)
                                  S: {"type":"anim","uuid":...,"anim":...,"active":...,"ts":...}
                                  Emote records live ONLY in memory (they are transient by nature): an
                                  active record expires after ANIM_TTL_SECONDS without a refresh, and the
                                  expiry is PUSHED to the watching sockets like a normal stop.

Protocol (v1.5.1 — morphs over the same WebSocket):
                                  S: {"type":"morph","uuid":...,"variant":...}  (push on PUT /v1/morphs,
                                                                 addressed via the reverse index —
                                                                 only to sockets watching that uuid;
                                                                 "variant": "" = the record was cleared)
                                  S: {"type":"state",...,"morphs":{uuid:variant,...}} — the watch snapshot
                                  now also carries the morphs (and "" = absent) of the watched set.
                                  Heartbeat re-publication of the SAME variant pushes NOTHING.

Protocol (v1.5.2 — version visibility):
  /health includes the service version as the "version" field, so a deployment can be
  verified with plain curl without inspecting HTTP headers (the Server: header still
  carries the same string, LucarioSync/<version>).

Protocol (v1.5.3 — presence marker):
  The special variant "__present__" means "the mod is installed but NO morph is
  chosen" — a permanent registration so the store can tell "mod, default skin"
  apart from "no mod at all". It is stored/pushed/heartbeated like any morph and
  appears in snapshots and GET polls; mod clients treat it as "no morph" (the
  variant whitelist rejects it before rendering). /health counts marker records
  in the "mods" field ("players" still counts ALL records).

Protocol (v1.5.4 — strict authorization by default, per-IP WS cap):
  REQUIRE_AUTH=1 (the default) turns record protection from opt-in into mandatory:
  EVERY PUT/DELETE /v1/morphs/<uuid> and PUT /v1/anims/<uuid> requires the
  X-Lucario-Token header; a UUID without a token answers 401 and the mod client
  claims it AUTOMATICALLY via Mojang (challenge -> join -> verify — the flow was
  already built in, it just used to trigger only for already-claimed records).
  Reading (GET, /ws watch) stays public — it is what the mod needs to work.
  REQUIRE_AUTH=0 restores the legacy opt-in behavior. Additionally, WebSocket
  upgrades from a single IP are capped at MAX_WS_PER_IP live sockets (0 = off):
  one player needs exactly one socket, so a burst of sockets from one address is
  a flood, not a client.

Protocol (v1.5.5 — per-socket hardening, red-team findings closed):
  * frame budget (token bucket): EVERY incoming frame except close pays one token
    (hello, watch, ping 0x9, pong 0xA, text/binary, continuation, empty frames).
    The bucket starts at WS_BURST and refills one token per WS_REFILL_MS; an empty
    bucket silently drops the frame, and WS_DROP_KILL consecutive drops without a
    single paid frame in between close the session (hello/watch spam at ~57k msg/s
    burns the budget in under a millisecond; legit bursts of 2-3 messages never do);
  * a fragmented (continuation) message obeys the same WS_MAX_PAYLOAD cap as a
    single frame and must finish assembling within WS_ASSEMBLY_DEADLINE seconds —
    “many small frames = one unlimited message” and “dribble forever holding
    partial-message memory” were real DoS vectors;
  * hello deadline: the mod says hello right after the upgrade; a socket that
    stayed silent for HELLO_DEADLINE seconds is a scanner — auto-ponging keeps
    such sockets alive forever, so they are dropped and the per-IP slot freed;
  * a non-finite emote ts (NaN/Infinity survive json.loads and nan <= 0 is False)
    now falls back to the server clock instead of being relayed as invalid JSON.

Protocol (v1.5.6 — HTTP flood protection, the in-process penalty box):
  * EVERY 429 (the rate limiter and the per-IP WS cap alike) counts against the
    client IP; PENALTY_HITS rejections inside one 60 s window put the IP into
    the penalty box for PENALTY_MINUTES minutes;
  * a banned IP is dropped at ACCEPT level — no thread, no response, no log
    line: a flood becomes free for the attacker and free for the server (the
    disk is no longer reachable through the request log);
  * 429 logging is SAMPLED: the first rejection of an IP's window logs normally,
    the rest only bump a suppressed counter, and one summary line per minute per
    flooding IP keeps the forensics ("429 xN (... M suppressed)");
  * /health reports the pressure: "rejected" (429 total), "banned" (IPs in the
    box right now), "banned_dropped" (connections closed at the door) and
    "conns" (currently open HTTP/WS connections — WebSocket had MAX_WS_CLIENTS,
    plain HTTP had no visibility at all);
  * ban and release log one line each; the box is bounded (PENALTY_MAX) and
    pruned by housekeeping, so even a many-IP flood cannot grow it unbounded.

Safety rules:
  * only UUIDs are accepted (canonicalized) and only variants from the mod's
    whitelist;
  * per-IP request rate limit (RATE_PER_MIN);
  * request body no larger than 4 KB, at most 100 UUIDs per GET;
  * "slow" connections are closed after 30 seconds of inactivity (slowloris);
  * a UUID can be protected with a write token: the client proves account ownership
    via the Mojang session servers (challenge -> join -> verify), after which PUT/DELETE
    for that UUID require the X-Lucario-Token header. 1.5.4: REQUIRE_AUTH=1 (default)
    makes the token MANDATORY for every record; REQUIRE_AUTH=0 restores the legacy
    opt-in behavior (a UUID without a token is written freely — maximum harm:
    someone else's cosmetic morph);
  * the challenge is one-shot, lives CHALLENGE_TTL seconds and is bound to IP, UUID and name;
  * the Mojang call happens OUTSIDE all locks and with a hard timeout (resilience);
  * no more than MAX_RECORDS records (507 when full);
  * emote records are EPHEMERAL and stay in memory only: an active record expires after
    ANIM_TTL_SECONDS without a refresh (the mod client re-sends it every 10 s), the expiry
    is pushed to the watching sockets like a normal stop, and dead records are dropped later;
  * WebSocket sockets are capped at MAX_WS_CLIENTS globally and at MAX_WS_PER_IP
    per client IP (1.5.4), their watch lists at 100 UUIDs and their event queues at
    WS_QUEUE_MAX entries (drop-oldest) — a slow/bad socket cannot grow memory;
  * a live socket answers pings every WS_PING_SECONDS; 35 s of silence = the socket is closed;
  * 1.5.5 per-socket guards: a frame budget (token bucket, close exempt), a payload cap
    AND an assembly deadline for fragmented messages, and a HELLO_DEADLINE for silent
    scanner sockets — a leaked/broken client cannot squat slots forever either way;
  * 1.5.6 HTTP guards: a per-IP penalty box (PENALTY_HITS 429s in a window ->
    PENALTY_MINUTES of silent accept-level drops), sampled 429 logging (a flood
    cannot fill the disk with rejected-request lines) and /health pressure
    counters (rejected / banned / banned_dropped / conns);
  * records older than TTL are removed (TTL_HOURS, or TTL_DAYS when zero): mod clients
    1.4.2+ refresh their own record every heartbeatMinutes minutes and on every world
    join, so a record without updates is abandoned (the mod was uninstalled).

Storage — JSON files (DATA_DIR/morphs.json and DATA_DIR/tokens.json). Disk writes are
batched (every 5 seconds when there are changes) and happen on container shutdown
(SIGTERM/SIGINT).
No dependencies: Python 3.10+ standard library only (WebSocket implemented by hand per
RFC 6455 — the same thread-per-connection model as the HTTP handlers).
"""
import base64
import hashlib
import json
import math
import os
import re
import secrets
import signal
import socket
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
# 1.5.3: the presence marker — "the mod is installed, no morph chosen". Not a real
# variant: mod clients filter it with their whitelist before rendering. It turns
# "record absent" (unambiguous only for non-mod players) into a three-state signal:
# absent = no mod, "__present__" = mod with default skin, anything else = mod + morph.
PRESENT = "__present__"
ALLOWED = set(BASES) | {b + "_shiny" for b in BASES} | {PRESENT}

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

# --- Emote animations (since 1.5.0): real-time push over WebSocket, HTTP fallback ---
ANIM_RE = re.compile(r"^[a-z0-9_]{1,40}$")   # short animation names + "__tpose__"
# An ACTIVE emote record expires after this many seconds without a refresh; mod clients
# re-send their active emote every 10 s, so 30 s = three missed refreshes (a crashed game).
# The expiry is pushed to the watching sockets exactly like an explicit stop.
ANIM_TTL_SECONDS = float(os.environ.get("ANIM_TTL_SECONDS", "30"))
ANIM_SWEEP_SECONDS = max(1.0, float(os.environ.get("ANIM_SWEEP_SECONDS", "5")))
MAX_ANIM_RECORDS = int(os.environ.get("MAX_ANIM_RECORDS", "50000"))
MAX_WS_CLIENTS = int(os.environ.get("MAX_WS_CLIENTS", "2000"))
# 1.5.4: live WebSocket sockets per client IP. One mod client holds exactly one
# socket, so 5 is a generous ceiling even behind a public VPN's shared NAT exit;
# a burst of sockets from one address is a flood. 0 disables the per-IP cap.
MAX_WS_PER_IP = int(os.environ.get("MAX_WS_PER_IP", "5"))
WS_PING_SECONDS = float(os.environ.get("WS_PING_SECONDS", "20"))
WS_RECV_TIMEOUT = 35.0        # a live client replies to pings — 35 s of silence = dead socket
WS_MAX_PAYLOAD = 8192         # a watch list of 100 UUIDs fits easily
MAX_WATCH = 100               # watched UUIDs per socket (matches the HTTP read batch)
WS_QUEUE_MAX = 64             # per-socket event queue ceiling (drop-oldest)
# 1.5.5: per-socket frame budget (token bucket). Every incoming frame except close
# costs one token; the bucket starts full at WS_BURST and refills one token per
# WS_REFILL_MS. Empty-bucket frames are silently dropped; WS_DROP_KILL drops in a
# row (without a single paid frame between) = a flood -> the session is closed.
WS_BURST = int(os.environ.get("WS_BURST", "4"))
WS_REFILL_MS = int(os.environ.get("WS_REFILL_MS", "250"))
WS_DROP_KILL = int(os.environ.get("WS_DROP_KILL", "32"))
# 1.5.5: a fragmented (continuation) message obeys the same payload cap as a single
# frame and must finish assembling within WS_ASSEMBLY_DEADLINE seconds (a dribbler
# holding partial-message memory forever was a real DoS vector).
WS_ASSEMBLY_DEADLINE = float(os.environ.get("WS_ASSEMBLY_DEADLINE", "10"))
# 1.5.5: the mod sends hello right after the upgrade; a socket that stays silent
# for HELLO_DEADLINE seconds is a scanner (auto-ponging keeps it alive forever,
# which the recv timeout alone cannot catch) -> it is dropped, its slot freed.
HELLO_DEADLINE = float(os.environ.get("HELLO_DEADLINE", "10"))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 1.5.6: the HTTP penalty box — the in-process fail2ban replacement. EVERY 429
# (the rate limiter AND the per-IP WS cap) counts against the source IP;
# PENALTY_HITS 429s inside one 60 s window put the IP into the box for
# PENALTY_MINUTES minutes. Connections from a banned IP are closed at accept
# level (no thread, no response, no log). PENALTY_HITS <= 0 disables the box.
PENALTY_HITS = int(os.environ.get("PENALTY_HITS", "30"))
PENALTY_MINUTES = float(os.environ.get("PENALTY_MINUTES", "10"))
PENALTY_MAX = int(os.environ.get("PENALTY_MAX", "10000"))

# --- Record authorization (Mojang session servers) ---
# 1.5.4: strict mode by default — every record write needs a token; a UUID without
# one answers 401 and the mod client claims it via Mojang automatically. Reading
# stays public. REQUIRE_AUTH=0 restores the legacy opt-in (unclaimed = open).
REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH", "1") == "1"
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
LOCK = threading.Lock()        # STORE/TOKENS/DIRTY/TOK_DIRTY/ANIMS/ANIM_SEQ; leaf lock: no network I/O or other locks under it
PENDING_LOCK = threading.Lock()  # PENDING; a leaf lock too
RATE = defaultdict(deque)
RATE_LOCK = threading.Lock()

# 1.5.6: the penalty box (PENALTY_LOCK is a leaf lock) + pressure counters for
# /health. PENALTY: ip -> {"hits", "suppressed", "last_log", "window", "until"}.
PENALTY = {}
PENALTY_LOCK = threading.Lock()
COUNTERS = {"rejected": 0, "banned_dropped": 0, "conns": 0}

ANIMS = {}                     # uuid (canonical) -> {"anim": str, "active": bool, "ts": float} — in-memory only
ANIM_SEQ = 0                   # global monotonic counter of anim changes (under LOCK; snapshot cursors)


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


def set_morph(uuid_str: str, variant: str):
    """Upserts a record. Returns (ok, changed): ok=False when the store is full;
    changed=True when the stored variant actually differs (a heartbeat refresh of the
    SAME variant updates only `ts` and must not spam watchers with no-op events)."""
    global DIRTY
    with LOCK:
        old = STORE.get(uuid_str)
        old_variant = old["variant"] if old else None
        if variant:
            if uuid_str not in STORE and len(STORE) >= MAX_RECORDS:
                return False, False
            STORE[uuid_str] = {"variant": variant, "ts": time.time()}
        else:
            STORE.pop(uuid_str, None)
        DIRTY = True
    return True, (old_variant or "") != variant


def flusher() -> None:
    """Batched disk flush: at most once per FLUSH_SECONDS."""
    while True:
        time.sleep(FLUSH_SECONDS)
        flush_now()


# ===========================================================================
# Emote animations: store + WebSocket hub (RFC 6455, hand-rolled, no deps)
# ===========================================================================

class Session:
    """One live WebSocket client. Fields are guarded by `cond` unless said otherwise."""
    __slots__ = ("uuid", "watched", "queue", "cond", "alive", "wlock", "last_watch", "ip",
                 "born", "helloed", "tokens", "tlast", "drops")

    def __init__(self):
        self.uuid = None            # canonical uuid of the client from "hello" (informational)
        self.ip = None              # 1.5.4: client IP — the per-IP slot is held while set
        self.watched = frozenset()  # canonical uuids this socket receives events for (HUB lock)
        self.queue = deque(maxlen=WS_QUEUE_MAX)  # outgoing JSON-ready events (cond)
        self.cond = threading.Condition()
        self.alive = True
        self.wlock = threading.Lock()  # socket write lock: the reader (pongs) and writer (frames) share it
        self.last_watch = 0.0       # flood guard for "watch" messages
        self.born = time.time()     # 1.5.5: reference point of the hello deadline
        self.helloed = False        # 1.5.5: hello seen (reader writes, writer reads)
        self.tokens = float(WS_BURST)  # 1.5.5: frame-budget tokens (reader thread only)
        self.tlast = self.born      # 1.5.5: last lazy refill timestamp (reader thread only)
        self.drops = 0              # 1.5.5: consecutive dropped frames (reader thread only)


class Hub:
    """Registry of WS sessions + reverse index uuid -> sessions. Own lock: a leaf lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = set()
        self.index = {}             # uuid -> set of Session
        self.per_ip = {}            # 1.5.4: client IP -> reserved WS slots

    def register(self, s: Session) -> bool:
        with self.lock:
            if len(self.sessions) >= MAX_WS_CLIENTS:
                return False
            self.sessions.add(s)
            return True

    def reserve_ip(self, ip: str) -> bool:
        """1.5.4: atomically reserves one WS slot for a client IP, BEFORE the 101
        response — concurrent upgrades from one address cannot slip past the cap.
        MAX_WS_PER_IP <= 0 disables the per-IP limit."""
        if MAX_WS_PER_IP <= 0:
            return True
        with self.lock:
            if self.per_ip.get(ip, 0) >= MAX_WS_PER_IP:
                return False
            self.per_ip[ip] = self.per_ip.get(ip, 0) + 1
            return True

    def release_ip(self, ip: str) -> None:
        """1.5.4: returns a reserved per-IP slot (idempotent)."""
        if not ip:
            return
        with self.lock:
            n = self.per_ip.get(ip, 0) - 1
            if n > 0:
                self.per_ip[ip] = n
            else:
                self.per_ip.pop(ip, None)

    def unregister(self, s: Session) -> None:
        with self.lock:
            self.sessions.discard(s)
            for uuid in s.watched:
                subs = self.index.get(uuid)
                if subs:
                    subs.discard(s)
                    if not subs:
                        del self.index[uuid]
            s.watched = frozenset()
            if s.ip:  # 1.5.4: the session's death frees its per-IP slot
                n = self.per_ip.get(s.ip, 0) - 1
                if n > 0:
                    self.per_ip[s.ip] = n
                else:
                    self.per_ip.pop(s.ip, None)
                s.ip = None

    def set_watch(self, s: Session, uuids: frozenset) -> None:
        """Replaces the session's interest set (and maintains the reverse index)."""
        with self.lock:
            for uuid in s.watched:
                subs = self.index.get(uuid)
                if subs:
                    subs.discard(s)
                    if not subs:
                        del self.index[uuid]
            s.watched = uuids
            for uuid in uuids:
                self.index.setdefault(uuid, set()).add(s)

    def deliver(self, uuid: str, event: dict) -> None:
        """Queues an event for every socket watching `uuid`. Never blocks, never does I/O:
        the sockets' writer threads drain their own queues."""
        with self.lock:
            subs = self.index.get(uuid)
            targets = list(subs) if subs else ()
        for s in targets:
            if not s.alive:
                continue
            with s.cond:
                if s.alive:
                    s.queue.append(event)
                    s.cond.notify_all()

    def live_count(self) -> int:
        with self.lock:
            return len(self.sessions)


HUB = Hub()


def enqueue(s: Session, event: dict) -> None:
    """Queues an event for ONE socket (replies go through the same writer as pushes)."""
    with s.cond:
        if s.alive:
            s.queue.append(event)
            s.cond.notify_all()


def set_anim(uuid_str: str, anim: str, start: bool, ts: float):
    """Upserts an emote record. `ts` is the SENDER's epoch ms (echoed back so viewers can
    order events off one clock); `at` is the server-side stamp (seconds) used for TTL and
    age. Returns the event dict to deliver (or None if nothing changed for the watchers)
    and an HTTP code. Call sites deliver the event OUTSIDE LOCK."""
    global ANIM_SEQ
    with LOCK:
        event = None
        if start:
            if uuid_str not in ANIMS and len(ANIMS) >= MAX_ANIM_RECORDS:
                return None, 507
            ANIMS[uuid_str] = {"anim": anim, "active": True, "ts": ts, "at": time.time()}
            ANIM_SEQ += 1
            event = {"type": "anim", "uuid": uuid_str, "anim": anim, "active": True, "ts": ts}
        else:
            rec = ANIMS.get(uuid_str)
            if rec is not None:
                rec["active"] = False
                rec["ts"] = ts
                rec["at"] = time.time()
                ANIM_SEQ += 1
                event = {"type": "anim", "uuid": uuid_str, "anim": rec["anim"],
                         "active": False, "ts": ts}
        return event, 200


def anim_sweeper() -> None:
    """Every ANIM_SWEEP_SECONDS: expires stale ACTIVE emotes (the player crashed / lost the
    network mid-emote) and drops long-dead records. Expiry is delivered like a normal stop,
    so watchers always learn about it in (near) real time. The expiry event's ts is one
    millisecond past the sender's last refresh — strictly newer on the SAME (sender) clock,
    so clients that order by ts always accept it."""
    while True:
        time.sleep(ANIM_SWEEP_SECONDS)
        now = time.time()
        expired = []
        dead = []
        global ANIM_SEQ
        with LOCK:
            for u, rec in ANIMS.items():
                if rec["active"]:
                    if now - rec["at"] > ANIM_TTL_SECONDS:
                        rec["active"] = False
                        rec["at"] = now
                        ANIM_SEQ += 1
                        expired.append((u, rec["anim"], rec["ts"]))
                elif now - rec["at"] > max(60.0, ANIM_TTL_SECONDS * 5):
                    dead.append(u)
            for u in dead:
                del ANIMS[u]
        for u, anim, last_ts in expired:
            HUB.deliver(u, {"type": "anim", "uuid": u, "anim": anim,
                            "active": False, "ts": last_ts + 1.0})


# ===========================================================================
# 1.5.6: HTTP flood protection — the penalty box (fail2ban without fail2ban)
# ===========================================================================

def _penalty_prune_locked(now: float) -> None:
    """Drops expired bans and stale tracking entries (no hits for a window).
    Called with PENALTY_LOCK held — from housekeeping and when the box is full.
    NEVER evicts a live ban: a many-IP flood must not buy out real bans."""
    stale = [ip for ip, rec in PENALTY.items()
             if rec["until"] <= now and now - rec["window"] > 60]
    for ip in stale:
        del PENALTY[ip]


def penalty_hit(ip: str) -> bool:
    """Counts one 429 for `ip` (the rate limiter or the per-IP WS cap called this).
    Returns True when THIS is the first rejection of the IP's current 60 s window
    — the only one that gets a per-request log line; the rest live in the
    suppressed counter and surface in a once-per-minute summary line. After
    PENALTY_HITS rejections inside one window the IP is banned.
    PENALTY_HITS <= 0: counters only, nobody is ever banned.
    Prints happen OUTSIDE PENALTY_LOCK: stdout must never stall the accept loop."""
    now = time.time()
    log_line = None
    with PENALTY_LOCK:
        COUNTERS["rejected"] += 1
        if PENALTY_HITS <= 0:
            return True
        rec = PENALTY.get(ip)
        if rec is None:
            if len(PENALTY) >= PENALTY_MAX:
                _penalty_prune_locked(now)
                if len(PENALTY) >= PENALTY_MAX:
                    return True    # box full (a many-IP flood): log normally, stay untracked
            rec = PENALTY[ip] = {"hits": 0, "suppressed": 0,
                                 "last_log": now, "window": now, "until": 0.0}
        if now - rec["window"] > 60:
            rec["window"] = now
            rec["hits"] = 0
            rec["last_log"] = now
        rec["hits"] += 1
        rec["suppressed"] += 1
        first = rec["hits"] == 1
        if rec["hits"] >= PENALTY_HITS:
            rec["until"] = now + PENALTY_MINUTES * 60
            log_line = f"[penalty] {ip} banned for {PENALTY_MINUTES:g}m (429 storm)"
            rec["hits"] = 0
            rec["suppressed"] = 0
            rec["last_log"] = now
        elif not first and now - rec["last_log"] >= 60:
            rec["last_log"] = now
            log_line = (f"[rate] {ip}: 429 x{rec['hits']} this window "
                        f"({rec['suppressed']} suppressed)")
    if log_line:
        print(log_line, flush=True)
    return first


def banned(ip: str) -> bool:
    """1.5.6: True only while `ip` has an ACTIVE ban (until > now). A mere tracking
    entry (until == 0.0) is NOT a ban. A freshly expired ban is removed by the
    first post-ban connection (which also logs the release). The release print
    happens OUTSIDE PENALTY_LOCK (this runs in the accept loop)."""
    now = time.time()
    released = False
    with PENALTY_LOCK:
        rec = PENALTY.get(ip)
        if rec is None:
            return False
        if rec["until"] > now:
            return True
        if rec["until"] > 0.0:          # an expired ban, not a tracker
            del PENALTY[ip]
            released = True
    if released:
        print(f"[penalty] {ip} released", flush=True)
    return False


# ----------------------------------------------------------------- WS frames (RFC 6455)

def frame_budget(s: Session) -> bool:
    """1.5.5: the per-socket frame budget — a token bucket. EVERY incoming frame
    except close costs one token (hello, watch, ping, pong, text/binary, empty
    frames); the bucket starts at WS_BURST and refills one token per WS_REFILL_MS
    lazily, by timestamp. A paid frame resets the consecutive-drop counter, a
    dropped one (empty bucket — silent) increments it; WS_DROP_KILL consecutive
    drops without a single paid frame in between is a flood and the caller closes
    the session. A legit burst (a couple of watch resyncs, pongs to server pings)
    never comes close, while 57k msg/s of hello spam burns its 32-drop budget in
    under a millisecond of server time. Reader-thread only — no locking."""
    now = time.time()
    if now > s.tlast:
        s.tokens = min(WS_BURST, s.tokens + (now - s.tlast) * (1000.0 / WS_REFILL_MS))
        s.tlast = now
    if s.tokens >= 1.0:
        s.tokens -= 1.0
        s.drops = 0
        return True
    return False


def drop_tick(s: Session) -> None:
    """Counts a silently dropped frame; WS_DROP_KILL in a row = a flood -> disconnect
    (the caller lets the ConnectionError tear the session down, freeing the slot)."""
    s.drops += 1
    if s.drops >= WS_DROP_KILL:
        raise ConnectionError("frame budget exhausted (flood)")


def ws_send(s: Session, sock, opcode: int, payload: bytes) -> None:
    """Sends one unmasked server frame. Serialized per socket (reader pongs + writer frames)."""
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n <= 0xFFFF:
        header.append(126)
        header += n.to_bytes(2, "big")
    else:
        header.append(127)
        header += n.to_bytes(8, "big")
    with s.wlock:
        sock.sendall(bytes(header) + payload)


def _read_exact(sock, n: int):
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (TimeoutError, socket.timeout):
            raise ConnectionError("recv timeout")
        if not chunk:
            if not buf:
                return None          # clean EOF between frames = the peer closed
            raise ConnectionError("eof in the middle of a frame")
        buf += chunk
    return bytes(buf)


def ws_read_message(s: Session, sock):
    """Reads one complete DATA message (assembling continuation frames), transparently
    answering pings. Returns (opcode, payload) with opcode 0x1/0x2, or (0x8, None) on close.
    Raises ConnectionError on protocol violations / dead sockets."""
    opcode = None
    payload = bytearray()
    total = 0        # 1.5.5: bytes of the message being assembled
    started = 0.0    # 1.5.5: when the assembly of the current message began
    while True:
        head = _read_exact(sock, 2)
        if head is None:
            return 0x8, None
        b1, b2 = head[0], head[1]
        fin = b1 & 0x80
        op = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if not masked:
            raise ConnectionError("client frames must be masked")
        if length == 126:
            ext = _read_exact(sock, 2)
            length = int.from_bytes(ext, "big")
        elif length == 127:
            ext = _read_exact(sock, 8)
            length = int.from_bytes(ext, "big")
        if length > WS_MAX_PAYLOAD:
            raise ConnectionError("frame too large")
        mask = _read_exact(sock, 4)
        data = _read_exact(sock, length) if length else b""
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op >= 0x8:                      # control frames may ride mid-message
            if op == 0x8:                  # close: terminal and FREE of charge — it ends the
                try:                       # session either way (1.5.5 budget exempt)
                    ws_send(s, sock, 0x8, data[:2])
                except Exception:  # noqa: BLE001 — the socket may already be gone
                    pass
                return 0x8, None
            if op not in (0x9, 0xA):
                raise ConnectionError("unknown control frame")
            if not frame_budget(s):        # 1.5.5: pings AND pongs pay; close does not
                drop_tick(s)               # raises on WS_DROP_KILL consecutive drops
                continue                   # silent drop: no pong echo, nothing remembered
            if op == 0x9:                  # ping -> pong
                ws_send(s, sock, 0xA, data)
            continue                       # 0xA: proof of life, nothing to answer
        if op == 0x0:                      # continuation
            if opcode is None:
                raise ConnectionError("unexpected continuation frame")
            total += len(data)
            payload += data
        else:                              # text/binary start
            if opcode is not None:
                raise ConnectionError("expected a continuation frame")
            opcode = op
            payload = bytearray(data)
            total = len(data)
            started = time.time()
        # 1.5.5: a fragmented message obeys the same cap as a single frame and cannot
        # be dribbled forever — every new fragment checks the assembly deadline (a
        # fully stalled assembler dies on the recv timeout holding at most the cap).
        if total > WS_MAX_PAYLOAD:
            raise ConnectionError("message exceeds the payload cap")
        if fin:
            return opcode, bytes(payload)
        if time.time() - started > WS_ASSEMBLY_DEADLINE:
            raise ConnectionError("message assembly deadline exceeded")


def ws_handle_message(s: Session, payload: bytes) -> None:
    """Client -> server messages: hello (identify) and watch (replace the interest set).
    Both are tiny; parsing errors are silently dropped (the protocol is fire-and-forget)."""
    try:
        msg = json.loads(payload.decode("utf-8"))
        if not isinstance(msg, dict):
            return
        typ = msg.get("type")
        if typ == "hello":
            s.helloed = True             # 1.5.5: stops the hello deadline (any hello counts)
            u = Handler._canonical_uuid(str(msg.get("uuid") or ""))
            if u is not None:
                s.uuid = u
            enqueue(s, {"type": "hello", "ok": True})
        elif typ == "watch":
            now = time.time()
            if now - s.last_watch < 0.25:  # flood guard: watch replacements at most 4/s
                return
            s.last_watch = now
            raw = msg.get("uuids")
            if not isinstance(raw, list):
                return
            ids = []
            for item in raw[:MAX_WATCH]:
                u = Handler._canonical_uuid(str(item or ""))
                if u is not None and u not in ids:
                    ids.append(u)
            HUB.set_watch(s, frozenset(ids))
            wanted = set(ids)
            with LOCK:
                out = {u: {"anim": rec["anim"], "active": rec["active"], "ts": rec["ts"]}
                       for u, rec in ANIMS.items() if u in wanted}
                cursor = ANIM_SEQ
                # 1.5.1: the snapshot now carries the morphs of the watched set too —
                # a new player's morph arrives with the same reply, no extra round-trip.
                morphs = {u: rec["variant"] for u, rec in STORE.items() if u in wanted}
            enqueue(s, {"type": "state", "anims": out, "cursor": cursor, "morphs": morphs})
    except Exception:  # noqa: BLE001 — a broken frame must not kill the session
        pass


def ws_reader(s: Session, sock) -> None:
    """Runs in its own thread: reads client frames until close/error."""
    try:
        while s.alive:
            opcode, payload = ws_read_message(s, sock)
            if opcode == 0x8:
                break
            if not frame_budget(s):    # 1.5.5: every assembled data message pays (empty too)
                drop_tick(s)
                continue               # silent drop — the drop counter decides the fate
            if payload:
                ws_handle_message(s, payload)
    except Exception:  # noqa: BLE001 — timeout/EOF/violation all mean: the session is over
        pass
    finally:
        s.alive = False
        with s.cond:
            s.cond.notify_all()


def ws_writer(s: Session, sock) -> None:
    """Runs in the HTTP request thread: drains the queue, keeps the ping cadence.
    1.5.5: also enforces the hello deadline — the mod says hello within milliseconds,
    so a socket silent for HELLO_DEADLINE seconds is a scanner (it may auto-pong
    forever, which is exactly why the recv timeout cannot catch it)."""
    deadline = s.born + HELLO_DEADLINE
    next_ping = s.born + WS_PING_SECONDS
    try:
        while s.alive:
            wake = next_ping
            if not s.helloed and deadline < wake:
                wake = deadline        # 1.5.5: wake up no later than the hello deadline
            timeout = max(0.05, wake - time.time())
            with s.cond:
                if not s.queue and s.alive:
                    s.cond.wait(timeout)
                events = list(s.queue)
                s.queue.clear()
            if not s.alive:
                break
            for event in events:
                ws_send(s, sock, 0x1, json.dumps(event, ensure_ascii=False,
                                                 separators=(",", ":")).encode("utf-8"))
            now = time.time()
            if not s.helloed and now > deadline:
                break                  # silent scanner socket — drop it, free the slot
            if now >= next_ping:
                ws_send(s, sock, 0x9, b"")
                next_ping = now + WS_PING_SECONDS
    except Exception:  # noqa: BLE001 — a failed send = a dead socket
        pass
    finally:
        s.alive = False
        with s.cond:
            s.cond.notify_all()


def run_ws_session(handler, ip: str) -> None:
    """The full life of one WebSocket connection. Occupies the request thread (like every
    handler) + one reader thread; both exit when the session dies."""
    sock = handler.connection
    s = Session()
    s.ip = ip   # the per-IP slot was reserved in _ws_upgrade; unregister frees it
    if not HUB.register(s):
        HUB.release_ip(ip)   # the global cap rejected the session — give the slot back
        try:
            ws_send(s, sock, 0x8, (1013).to_bytes(2, "big"))  # Try Again Later
        except Exception:  # noqa: BLE001
            pass
        return
    reader = threading.Thread(target=ws_reader, args=(s, sock), daemon=True)
    reader.start()
    try:
        ws_writer(s, sock)
    finally:
        s.alive = False
        with s.cond:
            s.cond.notify_all()
        HUB.unregister(s)
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
        reader.join(timeout=2)


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
        with PENALTY_LOCK:
            _penalty_prune_locked(now)
        if stale:
            print(f"housekeeping: dropped {len(stale)} stale records", flush=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def handle_error(self, request, client_address):
        # 1.5.6: a flooder closing its socket right after a 429 must not write
        # tracebacks into the log (a client-side reset is ~15 lines of noise per
        # request — exactly the disk-fill vector the sampling closes). Real bugs
        # still print the full traceback.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            TimeoutError, socket.timeout)):
            return
        super().handle_error(request, client_address)

    def process_request(self, request, client_address):
        # 1.5.6: the penalty box check runs in the accept loop BEFORE any thread,
        # parsing or logging is spent on the connection. A banned peer gets an
        # instant socket close: no response, no log line — nothing to write to disk.
        if PENALTY_HITS > 0 and banned(client_address[0]):
            with PENALTY_LOCK:
                COUNTERS["banned_dropped"] += 1
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass
            return
        with PENALTY_LOCK:
            COUNTERS["conns"] += 1
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with PENALTY_LOCK:
                COUNTERS["conns"] -= 1


VERSION = "1.5.6"   # single source of truth: Server: header + /health "version" field


class Handler(BaseHTTPRequestHandler):
    server_version = "LucarioSync/" + VERSION
    protocol_version = "HTTP/1.1"
    # Close "slow" connections: protection against slowloris thread exhaustion.
    timeout = 30

    def log_message(self, fmt, *args):  # noqa: A003 — standard hook
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.client_address[0]} {fmt % args}", flush=True)

    def log_request(self, code='-', size='-'):
        # 1.5.6: sampled 429 logging — _rejected() sets quiet_429 for the suppressed
        # rejections so a flood cannot write the disk through the request log. The
        # flag is consumed by the very next 429 line (send_response -> log_request).
        if getattr(self, "quiet_429", False) and code == 429:
            self.quiet_429 = False
            return
        super().log_request(code, size)

    def _rejected(self):
        """1.5.6: the single path for ALL HTTP 429s. Counts the rejection into the
        penalty box (PENALTY_HITS in one window -> a silent accept-level ban) and
        samples the log: the first rejection of the IP's window logs normally,
        the rest are suppressed (only the once-per-minute summary prints).
        The connection is closed after a 429: a rejected client must not hold a
        keep-alive slot, and the close stays clean (no reset-traceback noise)."""
        self.quiet_429 = not penalty_hit(self.client_address[0])
        self.close_connection = True
        return self._send(429, {"error": "rate limit, slow down"})

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
        """Verifies the write token. 1.5.4: with REQUIRE_AUTH=1 every record write
        needs a token — a UUID without one answers 401 and the mod client claims it
        via Mojang automatically. REQUIRE_AUTH=0 restores the legacy opt-in model:
        an unclaimed UUID is open for writing."""
        with LOCK:
            rec = TOKENS.get(uuid_str)
        if rec is None:
            if REQUIRE_AUTH:
                self._send(401, {"error": "strict mode: this record requires authorization; "
                                          "the client claims the UUID via Mojang "
                                          "(POST /v1/auth/challenge -> join -> /v1/auth/verify) "
                                          "and then sends the X-Lucario-Token header"})
                return False
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
            return self._rejected()
        parts = urlsplit(self.path)
        if parts.path == "/health":
            with LOCK:
                active = sum(1 for rec in ANIMS.values() if rec["active"])
                mods = sum(1 for rec in STORE.values() if rec["variant"] == PRESENT)
                players = len(STORE)
            now = time.time()
            with PENALTY_LOCK:
                banned_now = sum(1 for rec in PENALTY.values() if rec["until"] > now)
                rejected = COUNTERS["rejected"]
                banned_dropped = COUNTERS["banned_dropped"]
                conns = COUNTERS["conns"]
            return self._send(200, {"ok": True, "version": VERSION, "players": players,
                                    "mods": mods, "emotes": active, "live": HUB.live_count(),
                                    "rejected": rejected, "banned": banned_now,
                                    "banned_dropped": banned_dropped, "conns": conns})
        if parts.path == "/ws":
            return self._ws_upgrade()
        if parts.path == "/v1/anims":
            raw = (parse_qs(parts.query).get("uuids") or [""])[0]
            ids = [s.strip() for s in raw.split(",") if s.strip()][:MAX_UUIDS]
            wanted = set()
            for u in ids:
                canonical = self._canonical_uuid(u)
                if canonical is not None:
                    wanted.add(canonical)
            now = time.time()
            with LOCK:
                out = {u: {"anim": rec["anim"], "active": rec["active"], "ts": rec["ts"],
                           "age": round(now - rec["at"], 2)}
                       for u, rec in ANIMS.items() if u in wanted}
                cursor = ANIM_SEQ
            return self._send(200, {"anims": out, "cursor": cursor})
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

    # ------------------------------------------------------------- WebSocket upgrade
    def _ws_upgrade(self):
        """RFC 6455 handshake on the SAME port, then the session owns the thread until the
        socket dies. Live clients auto-reply to pings, so WS_RECV_TIMEOUT doubles as the
        liveness check for the reader."""
        if (self.headers.get("Upgrade") or "").lower() != "websocket" \
                or "upgrade" not in (self.headers.get("Connection") or "").lower():
            return self._send(400, {"error": "websocket upgrade required"})
        key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
        if not key:
            return self._send(400, {"error": "missing Sec-WebSocket-Key"})
        # 1.5.4: per-IP cap on live WebSocket sockets. The reservation happens BEFORE
        # the 101 response, so concurrent upgrades from one address cannot slip past
        # the cap; it is released on a failed handshake write, on a global-cap
        # rejection, or on the session's normal death.
        ip = self.client_address[0]
        if not HUB.reserve_ip(ip):
            # 1.5.6: a WS-cap rejection feeds the same penalty box and log sampling
            self.quiet_429 = not penalty_hit(ip)
            self.close_connection = True
            return self._send(429, {"error": "too many websocket connections from this ip"})
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        self.connection.settimeout(WS_RECV_TIMEOUT)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception:  # noqa: BLE001 — the peer vanished mid-handshake
            HUB.release_ip(ip)
            return
        self.close_connection = True   # after the session the socket is ours to close
        run_ws_session(self, ip)

    # ------------------------------------------------------------- POST (record authorization)
    def do_POST(self):  # noqa: N802
        if not self._rate_ok():
            return self._rejected()
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
            return self._rejected()
        parts = urlsplit(self.path)
        if parts.path.startswith("/v1/anims/"):
            return self._anim_put(parts.path[len("/v1/anims/"):])
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
        ok, changed = set_morph(uuid_str, variant)
        if not ok:
            return self._send(507, {"error": "storage full"})
        if changed:
            # 1.5.1: push the change to the sockets watching THIS uuid (the HUB reverse
            # index — never a broadcast). Outside LOCK: delivery only queues, never blocks.
            HUB.deliver(uuid_str, {"type": "morph", "uuid": uuid_str, "variant": variant})
        return self._send(200, {"ok": True})

    def _anim_put(self, raw_uuid: str):
        """Publishes an emote event: {"anim": "dance"|"__tpose__"|..., "state": "start"|"stop",
        "ts": <sender epoch ms>}. Same token protection as morphs. The event is PUSHED to
        every WS socket watching the uuid; everyone else sees it in the HTTP snapshot."""
        uuid_str = self._canonical_uuid(raw_uuid)
        if uuid_str is None:
            return self._send(400, {"error": "bad uuid"})
        if not self._authorized(uuid_str):
            return
        body = self._read_body()
        if body is None:
            return self._send(400, {"error": "missing or too large body"})
        try:
            data = json.loads(body.decode("utf-8"))
            anim = data.get("anim")
            state = data.get("state")
            ts = data.get("ts")
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "bad json"})
        if not isinstance(anim, str) or not isinstance(state, str):
            return self._send(400, {"error": "anim and state must be strings"})
        anim = anim.strip().lower()
        state = state.strip().lower()
        if state not in ("start", "stop"):
            return self._send(400, {"error": "state must be start or stop"})
        if state == "start" and not ANIM_RE.match(anim):
            return self._send(422, {"error": "unknown animation name"})
        if (not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts <= 0):
            ts = time.time() * 1000.0   # 1.5.5: NaN/Infinity fall back to the server clock
        event, code = set_anim(uuid_str, anim, state == "start", float(ts))
        if code == 507:
            return self._send(507, {"error": "storage full"})
        if event is not None:
            HUB.deliver(uuid_str, event)   # outside LOCK: delivery only queues, never blocks
        return self._send(200, {"ok": True})

    # ------------------------------------------------------------- DELETE
    def do_DELETE(self):  # noqa: N802
        if not self._rate_ok():
            return self._rejected()
        parts = urlsplit(self.path)
        if not parts.path.startswith("/v1/morphs/"):
            return self._send(404, {"error": "not found"})
        uuid_str = self._canonical_uuid(parts.path[len("/v1/morphs/"):])
        if uuid_str is None:
            return self._send(400, {"error": "bad uuid"})
        if not self._authorized(uuid_str):
            return
        ok, changed = set_morph(uuid_str, "")
        if not ok:
            return self._send(507, {"error": "storage full"})
        if changed:
            # 1.5.1: clearing is a change too — tell the watchers (addressed, not broadcast).
            HUB.deliver(uuid_str, {"type": "morph", "uuid": uuid_str, "variant": ""})
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
    threading.Thread(target=anim_sweeper, daemon=True).start()
    # SIGTERM from docker stop: exit cleanly; the finally block saves the data.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = Server(("0.0.0.0", PORT), Handler)
    ttl_desc = f"{TTL_HOURS:g} hours" if TTL_HOURS > 0 else f"{TTL_DAYS:g} days"
    print(f"Lucario Morph Sync listening on 0.0.0.0:{PORT} "
          f"(variants: {len(ALLOWED)}, ttl: {ttl_desc}, housekeeping every {HOUSEKEEPING_SECONDS:g}s, "
          f"max records: {MAX_RECORDS}, flush every {FLUSH_SECONDS}s, auth via {MOJANG_SESSION}, "
          f"write-tokens: {len(TOKENS)}; emotes: ttl {ANIM_TTL_SECONDS:g}s, sweep {ANIM_SWEEP_SECONDS:g}s, "
          f"ws ping {WS_PING_SECONDS:g}s, max live {MAX_WS_CLIENTS}, max per ip {MAX_WS_PER_IP}, "
          f"frame budget {WS_BURST}+1/{WS_REFILL_MS}ms kill {WS_DROP_KILL}, "
          f"assembly cap {WS_MAX_PAYLOAD}B/{WS_ASSEMBLY_DEADLINE:g}s, hello deadline {HELLO_DEADLINE:g}s, "
          f"penalty box: {PENALTY_HITS if PENALTY_HITS > 0 else 'off'} x 429/min -> "
          f"{PENALTY_MINUTES:g}m (max {PENALTY_MAX} ips), "
          f"strict auth: {'on' if REQUIRE_AUTH else 'off (legacy opt-in)'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("bye", flush=True)
    finally:
        flush_now()


if __name__ == "__main__":
    main()
