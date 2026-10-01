"""Run a coroutine from synchronous fixtures.

Sync fixtures may execute while pytest-asyncio's event loop is running, where
``asyncio.run`` is not allowed; a short-lived worker thread gets its own loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
