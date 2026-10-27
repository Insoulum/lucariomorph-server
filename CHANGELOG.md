# Changelog

## 1.5.7 — rotating hashed write tokens

The write token stops being a permanent secret and becomes a short-lived,
hash-only credential. Zero changes required on the mod side: clients already
re-authorize automatically on any 401 and overwrite the stored token.

* **Rotation.** `/v1/auth/verify` issues a FRESH random token on EVERY call —
  there is no "the same token returned forever" anymore. The mod client saves
  the new token automatically (`serviceTokens.putAll`), so rotation is
  invisible to players; after deleting the client config a re-authorization
  hands out a new token the same way.
* **Hash-only storage.** `tokens.json` now stores `uuid → {"hash": <SHA-256 of
  the token>, "name", "ts"}`. A leaked backup or volume snapshot can no longer
  be replayed against the service — the plaintext token exists exactly once,
  in the verify response. The constant-time comparison
  (`secrets.compare_digest`) is kept, now over digests.
* **Transparent migration.** A pre-1.5.7 `tokens.json` (plaintext `"token"`
  fields) is hashed on load and rewritten on the next flush; every existing
  token keeps working until it expires or the client re-authorizes. The startup
  log reports `N migrated from plaintext`.
* **Expiry.** New env knob `TOKEN_TTL_DAYS` (default `30`, `0` = never): a
  token older than that answers 401 like a wrong one. The record itself stays
  protected (deleting the record would open the UUID for writing in the legacy
  `REQUIRE_AUTH=0` mode), and the mod client re-authorizes silently — expiry is
  invisible to legitimate players, while a stolen token dies on its own instead
  of living forever.
* **Tests.** Both suites now assert rotation instead of the same-token behavior
  (a re-claim issues a fresh token; the old one answers 401, the new one works),
  plus a legacy-file migration scenario seeded into the main suite (an old
  plaintext token keeps working and `tokens.json` is rewritten hashed).

## 1.5.6 — HTTP flood protection (the in-process penalty box)

The HTTP side gets the same treatment the WebSocket side got in 1.5.5: a flood
must not be able to harm the service or its disk in any way. No fail2ban, no
iptables — the server already sees every client IP, so it does the job itself.

* **Penalty box.** EVERY 429 (the `RATE_PER_MIN` limiter AND the `MAX_WS_PER_IP`
  WebSocket cap) counts against the source IP. `PENALTY_HITS=30` rejections
  inside one 60 s window put the IP into the box for `PENALTY_MINUTES=10`
  minutes. A banned IP is dropped at ACCEPT level — no thread, no response, no
  log line — so flooding becomes free for the attacker and free for the server.
  Ban and release log exactly one line each (`[penalty] <ip> banned/released`).
  Env knobs: `PENALTY_HITS` (0 = off), `PENALTY_MINUTES`, `PENALTY_MAX=10000`
  (the box never grows unbounded; live bans are never evicted by newer floods).
* **Sampled 429 logging.** The first rejection of an IP's 60 s window logs
  normally; the rest only bump a suppressed counter, surfaced as one summary
  line per minute per flooding IP (`[rate] <ip>: 429 xN this window (M
  suppressed)`). Rejected requests no longer hold keep-alive slots, and a
  client resetting its socket right after a 429 no longer writes a ~15-line
  traceback (`handle_error` filters client-side resets; the server closes the
  connection cleanly after every 429). A request flood cannot write the disk
  through the log anymore — previously the ONE realistic single-IP DoS vector.
* **Pressure counters in `/health`.** New fields: `rejected` (429 total),
  `banned` (IPs in the box right now), `banned_dropped` (connections closed at
  the door), `conns` (currently open HTTP/WS connections — WebSocket had
  `MAX_WS_CLIENTS`, plain HTTP had no visibility at all). All previous fields
  are unchanged.
* **Log rotation in docker-compose.** The `logging:` block caps the json-file
  log at 3 × 10 MB — a hard ceiling regardless of anything else.
* **Tests.** `tests/penalty_smoke.py` (19 checks, two instances, three loopback
  source addresses): three flood stages (200s → 429s → silent drops), the box
  fed by both 429 sources, accept-level drops, `/health` counters, sampled
  logging, one-line ban/release events, TTL release, re-ban, and "2 hits do
  not ban". Full regression re-run of every suite: green.

Protocol impact: none. Old mod versions keep working exactly as before — legit
clients never receive a 429, so they never meet the box.

## 1.5.5 — Per-socket WebSocket guards (red-team findings closed)

Everything a single socket could do wrong, however exotic, is now bounded.
Protocol unchanged; mod 1.7.x never notices any of the guards.

* **Frame budget (token bucket).** Every incoming frame except `close` costs
  one token (hello, watch, ping 0x9, pong 0xA, text/binary, continuation, even
  empty frames). The bucket starts at `WS_BURST=4` and refills one token per
  `WS_REFILL_MS=250`; an empty bucket silently drops the frame, and
  `WS_DROP_KILL=32` consecutive drops without a single paid frame in between
  close the session. A hello/watch flood at ~57k msg/s (the last red-team
  finding) burns its drop budget in under a millisecond instead of pinning a
  core; legit bursts never come close.
* **Fragmented messages obey the 8 KiB cap.** The per-frame payload cap existed
  since 1.5.0, but a message split into continuation frames was assembled
  WITHOUT a limit — a proven 32 MiB memory spike per socket. The assembled
  total is now capped at `WS_MAX_PAYLOAD`.
* **Assembly deadline.** A fragmented message that has not finished assembling
  within `WS_ASSEMBLY_DEADLINE=10` seconds is dropped with the socket — the
  "one tiny frame every 30 s keeps partial-message memory alive forever"
  slow-poison vector (OOM over hours) is gone.
* **Hello deadline.** A socket that says nothing for `HELLO_DEADLINE=10`
  seconds is a scanner, not a client: it is dropped and its per-IP slot freed.
  Auto-ponging keeps such sockets alive forever (the recv timeout alone can
  never reap them) — the real-world case was the leaked orphan sockets of the
  mod 1.7.0 reconnect bug. Honest caveat: the deadline does NOT catch ghosts
  that already said hello (that is the client's bug — fixed in mod 1.7.1).
* **Non-finite emote timestamps.** `NaN`/`Infinity` survive `json.loads` and
  `nan <= 0` is `False`, so a bad `ts` used to be relayed to watchers as
  invalid JSON; non-finite `ts` now falls back to the server clock.
* **Internals.** `Session` uses `__slots__` (+ born/helloed/tokens/tlast/drops);
  the startup log lists every guard; `docker-compose.yml` documents all new
  env knobs (commented, defaults are sane).
* **Tests.** `tests/guard155_smoke.py` — 24 checks (floods die, paced pings
  survive, fragmented messages both work and die past cap/deadline, slots are
  reused after every kill, NaN/Infinity never reach watchers).

## Older releases (short)

* **1.5.4** — strict authorization by default (`REQUIRE_AUTH=1`: every record
  write needs the Mojang-verified token; the mod claims itself automatically on
  401) + per-IP WebSocket cap (`MAX_WS_PER_IP=5`).
* **1.5.3** — presence marker `__present__`: "mod installed, default skin" is
  distinguishable from "no mod"; the `mods` counter in `/health`.
* **1.5.2** — `/health` carries the service `version` (deployment verifiable
  with one curl).
* **1.5.1** — morph changes pushed over the same WebSocket (`morph` events +
  morphs in the `state` snapshot); heartbeats push nothing.
* **1.5.0** — emote animations: in-memory store, real-time push over
  hand-rolled RFC 6455 WebSocket on the same port, HTTP fallback, TTL expiry
  pushed like a stop.
* **1.4.x** — write tokens, Mojang session-server verification
  (challenge → join → verify), multi-mirror support, heartbeat.
* **1.3.0** — the mod no longer requires installation on the game server:
  morphs sync through this external HTTP service.
