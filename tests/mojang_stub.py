#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Заглушка sessionserver.mojang.com для интеграционных тестов сервиса синхронизации.

Эмулирует два эндпоинта, которые участвуют в авторизации записи:

  POST /session/minecraft/join
       тело: {"accessToken": "...", "selectedProfile": "<uuid без дефисов>", "serverId": "<40 hex>"}
       ответ: 204 (пусто). Запоминает пару serverId -> selectedProfile.

  GET /session/minecraft/hasJoined?username=NAME&serverId=SID
       ответ: 200 {"id": "<uuid без дефисов>", "name": NAME}, если join с этим serverId был;
              204 (пусто) — если join не делали (как у настоящего Mojang).

Запуск: python3 mojang_stub.py <port>
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

JOINS = {}  # serverId -> {"id": "<uuid без дефисов>", "name": str}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 — тихая заглушка
        pass

    # ------------------------------------------------------------- POST /join
    def do_POST(self):  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path != "/session/minecraft/join":
            return self._send(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400)
        if n <= 0 or n > 65536:
            return self._send(400)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return self._send(400)
        server_id = body.get("serverId")
        profile = body.get("selectedProfile")
        if not isinstance(server_id, str) or not isinstance(profile, str) or not profile:
            return self._send(400)
        with LOCK:
            JOINS[server_id] = {"id": profile}
        return self._send(204)

    # ------------------------------------------------------------- GET /hasJoined
    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path != "/session/minecraft/hasJoined":
            return self._send(404)
        query = parse_qs(parts.query)
        username = (query.get("username") or [""])[0]
        server_id = (query.get("serverId") or [""])[0]
        with LOCK:
            rec = JOINS.get(server_id)
        if rec is None:
            return self._send(204)  # у настоящего Mojang «нет подтверждения» = пустой ответ
        return self._send(200, {"id": rec["id"], "name": username})

    # ------------------------------------------------------------- служебное
    def _send(self, code: int, obj=None) -> None:
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if obj is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18799
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mojang stub on 127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
