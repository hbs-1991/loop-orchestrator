import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

T = TypeVar("T")


async def with_retries(fn: Callable[[], Awaitable[T]], attempts: int = 3, base_delay: float = 2.0) -> T:
    for i in range(attempts):
        try:
            return await fn()
        except httpx.TransportError:
            if i == attempts - 1:
                raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500 or i == attempts - 1:
                raise
        await asyncio.sleep(base_delay * 2 ** i)
    raise AssertionError("unreachable")
