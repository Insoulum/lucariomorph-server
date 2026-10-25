# Lucario Morph Sync — the morph sync server

A tiny HTTP+WebSocket service for playing
[Lucario Morph](https://github.com/Insoulum/lucario-morph) on servers where
**the mod itself is not installed**. Clients of the mod write "my UUID → my morph"
here and read the morphs of other players; since 1.5.0 the same service also carries
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
# -> {"ok": true, "players": 0}

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
| GET | `/health` | liveness check + `players` / `emotes` / `live` (WS sockets) counts |
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

Only variants from the mod's whitelist are accepted (16 base ones + `_shiny` versions),
otherwise — `422`. Junk/overload is cut off: a limit of 120 requests per minute per IP
(`429`), body ≤ 4 KB, valid UUIDs only, a `MAX_RECORDS` storage ceiling (`507`),
an animation-name whitelist `^[a-z0-9_]{1,40}$` (mod clients 1.6.x and older simply
never call the emote endpoints — nothing breaks for them).

## Emote animations (since 1.5.0 — WebSocket, same port)

The mod (1.7.0+) plays emote animations of OTHER players. How it stays fast and reliable:

* **publish**: an emote start/stop is a tiny `PUT /v1/anims/<uuid>` to every configured
  mirror, fired the moment the player toggles the emote (and re-sent every 3 s until
  accepted); an active emote re-publishes itself every 10 s;
* **receive**: the client opens ONE WebSocket to `GET /ws` (after the HTTP upgrade the
  connection speaks JSON text frames):

```
client                                        service
  │ {"type":"hello","uuid":"..."}              │
  │────────────────────────────────────────────>│ ack {"type":"hello","ok":true}
  │ {"type":"watch","uuids":[...≤100]}          │   ← «кого я вижу» (tracked players)
  │────────────────────────────────────────────>│ snapshot {"type":"state","anims":{...}}
  │<────────────────────────────────────────────│
  │            ... any watched uuid changes ...  │
  │ {"type":"anim","uuid":...,"anim":"dance",   │
  │<──────────── "active":true,"ts":...}─────────│   ← pushed instantly (p50 ≈ 11 ms + RTT)
```

* **only nearby**: the watch list is the client's tracked players — players outside your
  view distance never generate traffic for you;
* **guaranteed stop**: an explicit stop is pushed instantly; if the player's game crashes
  mid-emote, the record expires after `ANIM_TTL_SECONDS` (30 s — three missed 10 s
  refreshes) and the expiry is **pushed as a normal stop event** to every watcher;
* **fallback**: while the socket is down (or an old mirror answers 404 to `/ws`) the client
  polls `/v1/anims` every 2 s — emotes keep working, just with a little more delay;
* **transient**: emote records live in memory only and are never written to disk — a
  service restart simply clears them;
* a socket that stays silent for 35 s (no pong replies) is closed; per-socket event queues
  are capped (drop-oldest); sockets are capped at `MAX_WS_CLIENTS`.

## Record authorization (Mojang session servers, since 1.4.0)

Anyone who knows someone else's UUID could write to an open store (the maximum harm —
someone else's cosmetic morph). To give players protection, the mod client proves
account ownership the same way a vanilla server verifies players:

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
  │<───────── {"token": …} ─────────│ id matched → issue/return token│
```

From then on, PUT/DELETE for that UUID require the `X-Lucario-Token: <token>` header
(otherwise `401`). Key properties:

* **opt-in protection**: a UUID without a token is written freely, as before;
* **the token is recoverable**: verify returns the already issued token — after deleting
  the config, the client repeats the authorization and gets the same one (no orphaned records);
* **the service never sees the accessToken**: it is sent only directly to Mojang over HTTPS;
* verify cannot be spoofed: hasJoined returns the profile of whoever actually performed
  the join with that serverId; the challenge is one-shot and bound to IP+UUID+name;
* works with licensed clients only (for offline sessions Mojang rejects the join);
* the Mojang call has an 8 s timeout and runs **outside all locks** — a slow/down session
  server cannot wedge the service itself (verified with a load test).

Tokens are stored in `./data/tokens.json` (uuid → token, name, issue time) and **do not
expire** — back them up together with `morphs.json`. If the file is lost, protection
falls off (records become open again) and players will restore it automatically at their
next authorization.

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

## Tests

The integration suite starts the service locally (without Docker) plus a stub of the
Mojang session servers (`tests/mojang_stub.py`), and runs the full cycle: API validation,
UUID canonicalization, clearing morphs, authorization (challenge → join → verify),
token protection, token re-issue, rate limiting and storage overflow.

```bash
bash tests/test_auth_flow.sh
# TOTAL: PASS=33 FAIL=0   (25 API/authorization checks + 8 emote/WS checks)
```

The emote/WS part (`tests/test_ws.py`, client `tests/ws_probe.py`) starts its own service
with a fast TTL and checks: push delivery, watch filtering, snapshots on watch, explicit
stop, TTL expiry (the expiry arrives as a pushed stop), input validation and `/health`.

The same suite runs automatically in GitHub Actions on every push.

## Settings

Set via the environment in `docker-compose.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | port inside the container |
| `RATE_PER_MIN` | `120` | requests per minute from one IP |
| `TTL_HOURS` | `0` | after how many hours a stale record is removed; `0` — use `TTL_DAYS` |
| `TTL_DAYS` | `30` | the same in days, used when `TTL_HOURS=0` |
| `HOUSEKEEPING_SECONDS` | `3600` | how often expired records and challenges are cleaned up (minimum 1) |
| `MAX_RECORDS` | `100000` | storage ceiling; PUT answers `507` when full |
| `FLUSH_SECONDS` | `5` | how often changes are flushed to disk (batched writes) |
| `MOJANG_SESSION` | `https://sessionserver.mojang.com` | address of the Mojang session server (replaced by a stub in tests) |
| `CHALLENGE_TTL` | `300` | lifetime of a one-shot challenge, seconds |
| `MAX_PENDING` | `10000` | maximum unfinished challenges (memory protection) |
| `ANIM_TTL_SECONDS` | `30` | an active emote expires after this without a refresh (clients refresh every 10 s); the expiry is pushed as a stop |
| `ANIM_SWEEP_SECONDS` | `5` | how often the emote sweeper runs (expiry + dead-record cleanup), minimum 1 |
| `MAX_ANIM_RECORDS` | `50000` | emote store ceiling; PUT answers `507` when full |
| `MAX_WS_CLIENTS` | `2000` | maximum live WebSocket sockets |
| `WS_PING_SECONDS` | `20` | server ping cadence; 35 s of client silence closes the socket |

### About TTL and the client "pulse"

A morph record lives for a limited time: the mod client (1.4.2+) refreshes its record
by itself — on world join, on morph change, and with a silent "pulse" every
`heartbeatMinutes` minutes (default 15, configured in the client config). A short TTL
is therefore safe: active players always have a fresh record, while abandoned ones
(mod uninstalled, player gone) are removed automatically within
`TTL + HOUSEKEEPING_SECONDS`.

The recommended pairing is `TTL_HOURS=6` + `HOUSEKEEPING_SECONDS=600` (set in
`docker-compose.yml`): the "ghost" of an uninstalled mod disappears in 6–7 hours instead
of 30 days. For very short TTLs (under an hour), reduce the housekeeping interval too,
so cleanup doesn't lag behind expiration. Mod clients 1.4.1 and older are safe as well:
they re-publish their morph every ~30 seconds while in the world.

Data is written to disk in batches (every `FLUSH_SECONDS` when there are changes) and on
container shutdown (`SIGTERM` is handled — `docker stop` loses nothing). "Slow"
connections are closed after 30 seconds — slowloris thread exhaustion doesn't get
through. UUIDs are canonicalized, so `urn:uuid:...` and `{...}` forms land in the same
record as the plain form.

The storage is `./data/morphs.json` (morphs) and `./data/tokens.json` (write tokens) on
the host: they survive container restarts and are easy to back up. If port 8080 on the
server is busy — change the **left** side of the mapping (`"9090:8080"`) and add the new
address as a mirror in the mod config.

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
* reading — from the first one that responds (the previously successful one has priority);
* for players to see each other, their mirror lists must share a common mirror —
  the simplest is to keep the lists identical;
* your own mirror is set up the same way: this repository on any machine with Docker,
  and the IP/domain is added to `serviceUrls`.

## HTTPS (recommended since 1.4.0)

Ordinary sync data is cosmetic and plain HTTP used to be enough for it. But since 1.4.0
the **write token** also travels over the wire — intercepting it lets anyone write for a
protected foreign UUID. For serious protection, put nginx/caddy with a certificate in
front of the container (e.g. a `sync.insoulum.ru` subdomain — Caddy obtains a Let's
Encrypt certificate by itself) and add `https://sync.insoulum.ru` to the clients'
`serviceUrls` — the container doesn't need changes (tokens for the HTTPS address are
separate: players will need to authorize once for it as well — the mod does it by
itself on the first 401).

**WebSocket behind a proxy**: the emote channel upgrades `GET /ws` on the SAME port.
Caddy proxies it with zero configuration. For nginx add the standard upgrade headers
(`proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";`) to the location — otherwise the mod clients
silently fall back to the 2-second HTTP polling of `/v1/anims` (everything works,
just a bit slower).

## Privacy

* Stored on disk: player UUID, morph variant, last update time; for authorized players —
  the write token and name. That's all. Emote records are memory-only and die with the
  process.
* IPs appear only in container logs (for rate limiting) and in challenge memory —
  they are never written to the data files.
* The player's `accessToken` is **never received by the service**: the join is sent by
  the client directly to Mojang over HTTPS.
* Without authorization, any player can "draw" any valid variant onto a foreign UUID —
  maximum harm: a different cosmetic morph or a wrong emote animation. The cure is
  client-side auto-authorization (see "Record authorization").
