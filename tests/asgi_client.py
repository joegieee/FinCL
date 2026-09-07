"""Portal-free synchronous facade for in-process ASGI tests.

Starlette 1.6's synchronous TestClient relies on AnyIO's cross-thread blocking
portal, which is unavailable on some restricted hosts.  HTTPX2's ASGI transport
is fully asynchronous and does not need that portal; this facade keeps the
existing unittest call sites synchronous while each request runs on its own
local event loop.
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx2 as httpx


class ASGITestClient:
    def __init__(self, app: Any, *, base_url: str) -> None:
        self.app = app
        self.base_url = base_url
        self.cookies = httpx.Cookies()
    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            original_run_sync = anyio.to_thread.run_sync

            async def direct_run_sync(func: Any, *args: Any, **options: Any) -> Any:
                del options
                return func(*args)

            anyio.to_thread.run_sync = direct_run_sync
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url=self.base_url,
                    cookies=self.cookies,
                ) as client:
                    response = await client.request(method, url, **kwargs)
                    self.cookies.update(client.cookies)
                    return response
            finally:
                anyio.to_thread.run_sync = original_run_sync

        return anyio.run(send)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        pass
