"""Minimal in-loop ASGI WebSocket client for tests.

Runs the app's WebSocket handler as a task on the test's own event loop (the
async fixtures - aiosqlite engine, fakeredis, in-memory Qdrant - are bound to it),
exchanging raw ASGI messages through queues.
"""

from __future__ import annotations

import asyncio
import json
from types import TracebackType
from typing import Any

from fastapi import FastAPI

from tests.conftest import TEST_EXTENSION_ORIGIN

WS_PATH = "/api/v1/analytics/ws"


class WsClient:
    def __init__(
        self, app: FastAPI, *, origin: str | None = TEST_EXTENSION_ORIGIN, path: str = WS_PATH
    ) -> None:
        headers = [(b"origin", origin.encode())] if origin is not None else []
        self.scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "http_version": "1.1",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver"), *headers],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 50000),
            "subprotocols": [],
            "state": {},
        }
        self.app = app
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.accepted = False
        self.close_code: int | None = None

    async def __aenter__(self) -> WsClient:
        await self._to_app.put({"type": "websocket.connect"})
        self._task = asyncio.create_task(
            self.app(self.scope, self._to_app.get, self._from_app.put)  # type: ignore[arg-type]
        )
        first = await asyncio.wait_for(self._from_app.get(), 10)
        if first["type"] == "websocket.accept":
            self.accepted = True
        else:
            self.close_code = first.get("code", 1000)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.disconnect()

    async def disconnect(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self._task, 30)
        self._task = None

    async def send(self, frame: dict[str, Any] | str) -> None:
        text = frame if isinstance(frame, str) else json.dumps(frame)
        await self._to_app.put({"type": "websocket.receive", "text": text})

    async def recv(self, wait: float = 15) -> dict[str, Any]:
        """Next frame as JSON, or ``{"__close__": code}`` when the server closed."""
        message = await asyncio.wait_for(self._from_app.get(), wait)
        if message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
            return {"__close__": self.close_code}
        result: dict[str, Any] = json.loads(message["text"])
        return result

    async def recv_until(
        self, frame_type: str, *, client_ref: str | None = None, wait: float = 30
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Frames up to (and including) the first ``frame_type`` frame; skips heartbeats.

        ``wait`` bounds the whole call: heartbeats keep arriving, so a per-frame timeout
        would wait forever for a frame that never comes.
        """
        seen: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"no {frame_type} frame within {wait} s; saw {seen}")
            frame = await self.recv(remaining)
            if frame.get("type") == "ping":
                continue
            seen.append(frame)
            if "__close__" in frame:
                raise AssertionError(f"socket closed while waiting for {frame_type}: {seen}")
            if frame.get("type") == frame_type and (
                client_ref is None or frame.get("client_ref") == client_ref
            ):
                return frame, seen

    async def authenticate(self, access_token: str) -> dict[str, Any]:
        await self.send({"type": "auth", "access_token": access_token})
        return await self.recv()
