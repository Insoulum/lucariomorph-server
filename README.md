# Lucario Morph Sync — the morph sync server

A tiny HTTP+WebSocket service for playing
[Lucario Morph](https://github.com/Insoulum/lucario-morph) on servers where
**the mod itself is not installed**. Clients of the mod write "my UUID → my morph"
here and read the morphs of other players; the same service also carries
**real-time emote animations** (dances, the emote menu, the T-pose) between players.
The data is cosmetic: UUID, variant and update time. The service stores nothing personal.

The service is a single `service.py` file on the pure Python standard library
(no dependencies — the RFC 6455 WebSocket endpoint is implemented by hand),
packaged in Docker. It consumes pennies of RAM/CPU (a load probe with 200 live
WS clients + 20 HTTP pollers: 23 MiB RSS, p50 push latency 11 ms).

## Quick start on a VPS

```bash
# 1. Copy the repository to the server, e.g.:
git clone https://github.com/Insoulum/lucariomorph-server.git /opt/lucario-sync
# (or scp -r this folder if you don't need git)

# 2. On the server:
cd /opt/lucario-sync
docker compose up -d --build

# 3. Check:
curl http://127.0.0.1:8080/health
# -> {"ok": true, "version": "1.5.7", "players": 0, "mods": 0, "emotes": 0, "live": 0}

# 4. Open the port externally (if a firewall is enabled):
ufw allow 8080/tcp        # Debian/Ubuntu
# firewall-cmd --add-port=8080/tcp --permanent && firewall-cmd --reload   # RHEL/CentOS
```

Checking from outside, from your PC:

```bash
curl http://193.164.155.75:8080/health
```

After that, mod clients (1.4.0+) will see the service — add the address to the
`serviceUrls` of the mod config (`config/lucariomorph.json`).

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness check + service `version` + `players` / `mods` (presence markers) / `emotes` / `live` (WS sockets) counts |
| GET | `/v1/morphs?uuids=A,B,C` | morphs of the listed players (max 100 at a time) |
| PUT | `/v1/morphs/<uuid>` | set the morph, body `{"variant": "lucariocaptain_shiny"}`; `""` — clear. A protected UUID requires the `X-Lucario-Token` header |
| DELETE | `/v1/morphs/<uuid>` | clear the morph (also tokened if the UUID is protected) |
| GET | `/v1/anims?uuids=A,B,C` | current emote states (snapshot; HTTP fallback of the emote channel) |
| PUT | `/v1/anims/<uuid>` | publish an emote event, body `{"anim": "dance", "state": "start"\|"stop", "ts": <epoch ms>}`; same token protection as morphs |
| GET | `/ws` | RFC 6455 WebSocket — the real-time emote channel (same port!) |
| POST | `/v1/auth/challenge` | authorization step 1: `{"uuid","name"}` → `{"serverId"}` |
| POST | `/v1/auth/verify` | authorization step 3: `{"uuid","name","serverId"}` → `{"token"}` (after checking with Mojang) |

Examples:

```bash
# set a morph
curl -X PUT http://127.0.0.1:8080/v1/morphs/069a79f4-44e9-4726-a5be-fca90e38aaf5 \
     -H 'Content-Type: application/json' -d '{"variant": "lucariocaptain_shiny"}'

# query
curl 'http://127.0.0.1:8080/v1/morphs?uuids=069a79f4-44e9-4726-a5be-fca90e38aaf5'

# clear
curl -X DELETE http://127.0.0.1:8080/v1/morphs/069a79f4-44e9-4726-a5be-fca90e38aaf5
```

Only variants from the mod's whitelist are accepted (16 base ones + `_shiny`
versions), otherwise — `422`. Emote endpoints additionally validate the animation
name against `^[a-z0-9_]{1,40}$` (mod clients 1.6.x and older simply never call
them — nothing breaks). Everything else a hostile request could try is bounded —
see [Hardening](#hardening--rate-limits-and-flood-protection).

## How sync works

### The morph record and the client "pulse"

A record is `uuid → variant` plus an update timestamp. The mod client (1.4.2+)
refreshes it by itself — on world join, on morph change, and with a silent
"pulse" every `heartbeatMinutes` minutes (default 15, client config) — so a
record expires (`TTL_DAYS`, default 30) only when its player is really gone,
and housekeeping removes it within `TTL + HOUSEKEEPING_SECONDS`. The
recommended pairing is `TTL_HOURS=6` + `HOUSEKEEPING_SECONDS=600` (set in
`docker-compose.yml`): the "ghost" of an uninstalled mod disappears in 6–7
hours instead of 30 days; for very short TTLs (under an hour) reduce the
housekeeping interval too, so cleanup doesn't lag behind expiration. Clients
1.4.1 and older are safe as well: they re-publish their morph every ~30
seconds while in the world.

### Presence marker

A client with **no morph chosen** publishes the special variant `__present__`
(mod 1.7.3+) instead of leaving no record. The store distinguishes three states:

| record | meaning |
|---|---|
| absent | the player has never published — most likely no mod |
| `__present__` | the mod IS installed, default (vanilla) skin is worn |
| `lucariochef`, … | the mod is installed and a morph is worn |

The marker is a normal record (heartbeated, TTL, storage cap, `morph` events,
snapshots, GET polls — like any variant). `/health` counts markers in `mods`
(`players` counts ALL records). Mod clients never render it: the variant
whitelist drops it on arrival, treating it exactly like "morph removed". Old
watchers (1.4.x–1.7.1) may keep the last seen morph of a player who switched
to the default skin — updating the watcher to 1.7.3 resolves it.

### Version visibility

`/health` reports the deployed version in the `version` field (also in the
`Server:` header of every response — both come from one constant, always in
sync). A server answering **without** the field runs a build older than 1.5.2
— a reliable "you need to redeploy" marker. The field is purely informational:
clients ignore unknown JSON keys.

### Storage and durability

* Data is written to disk in batches (every `FLUSH_SECONDS` when there are
  changes) and on container shutdown (`SIGTERM` is handled — `docker stop`
  loses nothing). Files: `./data/morphs.json` and `./data/tokens.json` on the
  host — they survive container restarts and are easy to back up.
* UUIDs are canonicalized, so `urn:uuid:...` and `{...}` forms land in the same
  record as the plain form.
* "Slow" connections are closed after 30 seconds — slowloris thread exhaustion
  doesn't get through.
* If port 8080 on the server is busy — change the **left** side of the mapping
  (`"9090:8080"`) and add the new address as a mirror in the mod config.

## Real-time channel — WebSocket on the same port

`GET /ws` upgrades to an RFC 6455 WebSocket (JSON text frames) that carries BOTH
real-time features: **emote animations** (mod 1.7.0+) and **morph changes**
(mod 1.7.2+). Publishing stays plain HTTP: an emote start/stop is a tiny
`PUT /v1/anims/<uuid>` to every configured mirror, fired the moment the player
toggles the emote and re-sent every 3 s until accepted; an active emote
re-publishes itself every 10 s. Morph publishes are the ordinary
`PUT/DELETE /v1/morphs/<uuid>`.

```
client                                        service
  │ {"type":"hello","uuid":"..."}              │
  │────────────────────────────────────────────>│ ack {"type":"hello","ok":true}
  │ {"type":"watch","uuids":[...≤100]}          │   ← «кого я вижу» (tracked players)
  │────────────────────────────────────────────>│ snapshot {"type":"state",
  │<────────────────────────────────────────────│   "anims":{...}, "morphs":{...}}
  │            ... any watched uuid changes ...  │
  │ {"type":"anim","uuid":...,"anim":"dance",   │
  │<──────────── "active":true,"ts":...}─────────│   ← pushed instantly (p50 ≈ 11 ms + RTT)
  │ {"type":"morph","uuid":...,"variant":"..."}  │
  │<─────────────────────────────────────────────│   ← same channel, same addressing
```

* **only nearby**: the watch list is the client's tracked players — players
  outside your view distance never generate traffic for you;
* **targeted pushes, no broadcast**: an anim or morph change is addressed through
  a reverse index and delivered only to the sockets watching that uuid;
  `"variant": ""` means the record was cleared; a heartbeat refresh of the SAME
  variant updates only the record's `ts` and pushes nothing;
* **the snapshot already contains morphs**: a newly joined player's morph
  arrives with the first `state` message, no extra round-trip;
* **guaranteed stop**: an explicit stop is pushed instantly; if the player's
  game crashes mid-emote, the record expires after `ANIM_TTL_SECONDS` (30 s —
  three missed 10 s refreshes) and the expiry is **pushed as a normal stop
  event** to every watcher;
* **fallback**: while the socket is down (or an old mirror answers 404 to `/ws`)
  the client polls `/v1/anims` every 2 s — emotes keep working, just with a
  little more delay;
* **transient**: emote records live in memory only and are never written to
  disk — a service restart simply clears them (morph records, in contrast, are
  durable);
* **backwards compatible**: the plain HTTP channels are unchanged — older mod
  clients (1.4.x–1.7.1) keep polling them exactly as before, and unknown WS
  message types are ignored by older clients;
* socket hygiene: silent sockets are closed (see Hardening), per-socket event
  queues are capped (drop-oldest), live sockets capped at `MAX_WS_CLIENTS`.

## Authorization and tokens (Mojang session servers)

Anyone who knows someone else's UUID could write to an open store (the maximum
harm — someone else's cosmetic morph). To give players protection, the mod
client proves account ownership the same way a vanilla server verifies players:

```
client                          this service                       Mojang
  │ POST /v1/auth/challenge        │                                 │
  │───────────────────────────────>│ generates a serverId (one-shot, │
  │<──────── {"serverId": …} ──────│ lives 5 min, bound to IP+UUID)  │
  │ POST sessionserver.mojang.com/session/minecraft/join             │
  │──────────────────────────────────────────────────────────────────>│
  │            {accessToken, selectedProfile, serverId}  (HTTPS!)    │
  │<──────────────────────────────────────────── 204 ────────────────│
  │ POST /v1/auth/verify            │                                │
  │───────────────────────────────>│ GET hasJoined?username&serverId│
  │                                │───────────────────────────────>│
  │                                │<────── {"id","name"} / 204 ────│
  │<───────── {"token": …} ─────────│ id matched → issue a FRESH token│
```

From then on, PUT/DELETE for that UUID require the `X-Lucario-Token: <token>`
header (otherwise `401`). Since 1.5.4 the service runs in **strict mode**
(`REQUIRE_AUTH=1`, the default): EVERY write needs the token, and the mod
client (1.7.x) reacts to a 401 **automatically** — it runs the claim chain,
stores the token in its config and retries, so the player just keeps playing
(first join after the upgrade shows one "record protected" chat message).
Reading stays public — that is what the mod needs to work. `REQUIRE_AUTH=0`
restores the legacy opt-in behavior (unclaimed UUIDs are written freely;
useful for testing). Squatting, overwriting and deleting other people's
records all fail with 401 — a token exists only where the owner proved
account ownership via Mojang at least once.

Key properties:

* **rotation (1.5.7)**: every verify issues a NEW token — the previous one stops
  working. The mod saves the fresh token automatically, so re-authorization is
  invisible to players; after deleting the config the client simply repeats the
  authorization and gets a new one (no orphaned records);
* **tokens expire (1.5.7)**: a token is accepted only for `TOKEN_TTL_DAYS` days
  (default 30, `0` = never) — a leaked token dies on its own instead of living
  forever, while legitimate clients re-authorize silently on the next 401;
* **the service never sees the accessToken**: it is sent only directly to Mojang
  over HTTPS;
* verify cannot be spoofed: hasJoined returns the profile of whoever actually
  performed the join with that serverId; the challenge is one-shot and bound to
  IP+UUID+name;
* works with licensed clients only (for offline sessions Mojang rejects the
  join);
* the Mojang call has an 8 s timeout and runs **outside all locks** — a
  slow/down session server cannot wedge the service itself (verified with a
  load test).

Tokens are stored in `./data/tokens.json` as **SHA-256 hashes only** (uuid →
hash, name, issue time) since 1.5.7 — the file holds no usable secrets, so a
leaked backup or volume snapshot cannot be replayed against the service;
pre-1.5.7 plaintext entries are hashed transparently on the first start and
the file is rewritten. Back the file up with `morphs.json` (even though a
leak no longer exposes usable tokens). If it is lost, protection falls off
and players restore it automatically at their next authorization.

Manually running the chain with curl (as the service sees it):

```bash
# step 1: challenge
SID=$(curl -s -X POST http://127.0.0.1:8080/v1/auth/challenge \
     -H 'Content-Type: application/json' \
     -d '{"uuid":"069a79f4-44e9-4726-a5be-fca90e38aaf5","name":"Notch"}' | jq -r .serverId)
# step 2 is performed by THE MOD CLIENT on the player's side (join on sessionserver.mojang.com)
# step 3: verify (returns 403 if the join was not made by the account owner)
curl -s -X POST http://127.0.0.1:8080/v1/auth/verify \
     -H 'Content-Type: application/json' \
     -d "{\"uuid\":\"069a79f4-44e9-4726-a5be-fca90e38aaf5\",\"name\":\"Notch\",\"serverId\":\"$SID\"}"
```

## Hardening — rate limits and flood protection

The service is a single Python process on a public port — everything a hostile
client could do is bounded, in-process (no fail2ban, no iptables needed). All
knobs are env-tunable with sane defaults (see [Settings](#settings)) and none
of it changes the protocol: a legit client never receives a 429 and never
notices a guard. The stories live in the CHANGELOG (1.5.4–1.5.6); the summary:

* **Request rate limit.** `RATE_PER_MIN=120` requests per minute per IP → 429.
* **Penalty box (1.5.6).** EVERY 429 counts against the source IP:
  `PENALTY_HITS=30` rejections inside one 60 s window ban the IP for
  `PENALTY_MINUTES=10` minutes, with connections dropped at ACCEPT level — no
  thread, no response, no log line. The box holds at most `PENALTY_MAX=10000`
  entries, live bans are never evicted by newer floods, `PENALTY_HITS=0`
  disables it.
* **Sampled 429 logging (1.5.6).** Only the first rejection of an IP's window
  logs normally; the rest bump a suppressed counter (one summary line per
  minute) — a flood cannot write the disk through the log.
* **Per-IP WebSocket cap (1.5.4).** `MAX_WS_PER_IP=5` live sockets per client IP
  (`0` = off) — one mod client holds exactly one, so five is generous even
  behind a shared VPN exit; flood upgrades get 429 before the upgrade.
* **Frame budget (1.5.5).** Every incoming frame except `close` costs one
  token: the bucket starts at `WS_BURST=4`, refills one per `WS_REFILL_MS=250`;
  an empty bucket silently drops the frame, and `WS_DROP_KILL=32` consecutive
  drops close the session — a ~57k msg/s flood burns its budget in under a
  millisecond instead of pinning a core, legit bursts never come close.
* **Payload caps (1.5.0/1.5.5).** Frames and fully assembled fragmented messages
  are capped at `WS_MAX_PAYLOAD` (8 KiB — a watch list of 100 UUIDs fits); a
  fragmented message unfinished after `WS_ASSEMBLY_DEADLINE=10` seconds is
  dropped with the socket — the "one tiny frame every 30 s keeps partial
  memory alive forever" slow-poison vector is gone.
* **Scanner hygiene (1.5.5).** A socket silent for `HELLO_DEADLINE=10` seconds
  after the upgrade is a scanner, not a client — its per-IP slot is freed
  (auto-ponging ghost sockets included; the mod says hello within
  milliseconds). Sockets ignoring pings for 35 s (`WS_PING_SECONDS=20`
  cadence) are closed as idle.
* **Input hard bounds.** Body ≤ 4 KB, valid UUIDs only, non-finite
  (NaN/Infinity) timestamps fall back to the server clock, and both stores
  answer `507` at their ceilings (`MAX_RECORDS`, `MAX_ANIM_RECORDS`).
* **Pressure visibility.** `/health` reports `rejected` (429 total), `banned`,
  `banned_dropped` (accept-level drops) and `conns` (open HTTP/WS connections)
  — flood pressure is one curl away. The shipped `docker-compose.yml` also
  caps the json-file log at 3 × 10 MB.
* **Verified.** `tests/guard155_smoke.py` (24 checks) and
  `tests/penalty_smoke.py` (19 checks): floods die, paced pings survive,
  slots are reused, counters move (see [Tests](#tests)).

## Settings

Set via the environment in `docker-compose.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | port inside the container |
| `RATE_PER_MIN` | `120` | requests per minute from one IP |
| `TTL_HOURS` | `0` | after how many hours a stale record is removed; `0` — use `TTL_DAYS` |
| `TTL_DAYS` | `30` | the same in days, used when `TTL_HOURS=0` |
| `HOUSEKEEPING_SECONDS` | `3600` | how often expired records and challenges are cleaned up (minimum 1) |
| `MAX_RECORDS` | `100000` | morph store ceiling; PUT answers `507` when full |
| `FLUSH_SECONDS` | `5` | how often changes are flushed to disk (batched writes) |
| `MOJANG_SESSION` | `https://sessionserver.mojang.com` | address of the Mojang session server (replaced by a stub in tests) |
| `CHALLENGE_TTL` | `300` | lifetime of a one-shot challenge, seconds |
| `MAX_PENDING` | `10000` | maximum unfinished challenges (memory protection) |
| `REQUIRE_AUTH` | `1` | 1.5.4: strict mode — every record write needs the Mojang token; `0` = legacy opt-in |
| `TOKEN_TTL_DAYS` | `30` | 1.5.7: a write token expires after this many days; `0` = never. Clients re-authorize silently on the next 401 — expiry is invisible to players, a leaked token dies on its own |
| `ANIM_TTL_SECONDS` | `30` | an active emote expires after this without a refresh (clients refresh every 10 s); the expiry is pushed as a stop |
| `ANIM_SWEEP_SECONDS` | `5` | how often the emote sweeper runs (expiry + dead-record cleanup), minimum 1 |
| `MAX_ANIM_RECORDS` | `50000` | emote store ceiling; PUT answers `507` when full |
| `MAX_WS_CLIENTS` | `2000` | maximum live WebSocket sockets |
| `MAX_WS_PER_IP` | `5` | 1.5.4: live WebSocket sockets per client IP (0 = off); one player needs one |
| `WS_PING_SECONDS` | `20` | server ping cadence; 35 s of client silence closes the socket |
| `WS_BURST` | `4` | 1.5.5: frame-token bucket size per socket (every frame except `close` costs one) |
| `WS_REFILL_MS` | `250` | 1.5.5: one frame token back per this many milliseconds |
| `WS_DROP_KILL` | `32` | 1.5.5: consecutive dropped frames (without a paid one in between) that close the session |
| `WS_ASSEMBLY_DEADLINE` | `10` | 1.5.5: seconds a fragmented message may take to assemble before being dropped with the socket |
| `HELLO_DEADLINE` | `10` | 1.5.5: seconds of post-upgrade silence before the socket counts as a scanner |
| `PENALTY_HITS` | `30` | 1.5.6: 429s within one 60 s window that put an IP into the penalty box (0 = off) |
| `PENALTY_MINUTES` | `10` | 1.5.6: how long a banned IP stays banned |
| `PENALTY_MAX` | `10000` | 1.5.6: ceiling of tracked IPs (live bans are never evicted) |

(`WS_MAX_PAYLOAD`, the 8 KiB per-message cap, is a constant in `service.py`,
not a knob.)

## Mirrors in the mod config

The client reads `config/lucariomorph.json`:

```json
{
  "syncEnabled": true,
  "rememberVariant": true,
  "autoAuth": true,
  "syncIntervalSeconds": 5,
  "serviceUrls": [
    "http://193.164.155.75:8080",
    "http://my-mirror.example.org:8080"
  ],
  "lastVariant": "lucariocaptain_shiny"
}
```

* publishing your own morph goes to **all** mirrors of the list;
* reading — from the first one that responds (the previously successful one has
  priority);
* for players to see each other, their mirror lists must share a common mirror —
  the simplest is to keep the lists identical;
* your own mirror is set up the same way: this repository on any machine with
  Docker, and the IP/domain is added to `serviceUrls`.

## HTTPS (recommended since 1.4.0)

Ordinary sync data is cosmetic and plain HTTP used to be enough for it. But
since 1.4.0 the **write token** also travels over the wire — intercepting it
lets anyone write for a protected foreign UUID. For serious protection, put
nginx/caddy with a certificate in front of the container (e.g. a
`sync.insoulum.ru` subdomain — Caddy obtains a Let's Encrypt certificate by
itself) and add `https://sync.insoulum.ru` to the clients' `serviceUrls` — the
container doesn't need changes (tokens for the HTTPS address are separate:
players will need to authorize once for it as well — the mod does it by itself
on the first 401).

**WebSocket behind a proxy**: the emote channel upgrades `GET /ws` on the SAME
port. Caddy proxies it with zero configuration. For nginx add the standard
upgrade headers (`proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";`) to the location — otherwise the mod
clients silently fall back to the 2-second HTTP polling of `/v1/anims`
(everything works, just a bit slower).

## Tests

All suites start the service locally (without Docker) plus a stub of the Mojang
session servers (`tests/mojang_stub.py`) and run automatically in GitHub
Actions on every push.

```bash
bash tests/test_auth_flow.sh     # TOTAL: PASS=38 FAIL=0
python3 tests/strict_smoke.py    # STRICT SMOKE: PASS=28 FAIL=0
python3 tests/guard155_smoke.py  # PASS=24 FAIL=0
python3 tests/penalty_smoke.py   # PASS=19 FAIL=0
```

* `test_auth_flow.sh` — the main suite: API validation, UUID
  canonicalization, the claim chain (challenge → join → verify), token
  protection, token rotation, legacy `tokens.json` migration, rate limiting,
  storage overflow; the emote/WS part (`tests/test_ws.py`,
  `tests/ws_probe.py`): push delivery, watch filtering, snapshots, TTL expiry
  (arrives as a pushed stop), input validation.
* `strict_smoke.py` — strict mode, two service instances: tokenless writes →
  401, the claim chain → token, token writes → 200, cross-UUID and wrong
  tokens → 401, re-claim → rotated token (old → 401, new → 200), the per-IP
  WS cap, the legacy instance still accepting tokenless writes.

## Privacy

* Stored on disk: player UUID, morph variant, last update time; for authorized
  players — the write token and name. That's all. Emote records are memory-only
  and die with the process.
* IPs appear only in container logs (for rate limiting) and in challenge memory
  — they are never written to the data files.
* The player's `accessToken` is **never received by the service**: the join is
  sent by the client directly to Mojang over HTTPS.
* Without authorization, any player can "draw" any valid variant onto a foreign
  UUID — maximum harm: a different cosmetic morph or a wrong emote animation.
  The cure is client-side auto-authorization (see "Authorization and tokens").
