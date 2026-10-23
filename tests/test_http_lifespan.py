"""The HTTP listener owns one context even while clients come and go."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from civ_mcp.http_lifespan import share_http_lifespan


def test_multiple_http_clients_share_one_persistent_game_context():
    async def run():
        events = []
        context = object()

        @asynccontextmanager
        async def game_lifespan(server):
            events.append("game start")
            try:
                yield context
            finally:
                events.append("game stop")

        @asynccontextmanager
        async def transport_lifespan(app):
            events.append("transport start")
            try:
                yield {}
            finally:
                events.append("transport stop")

        server = SimpleNamespace(lifespan=game_lifespan)
        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=transport_lifespan))
        share_http_lifespan(app, server)
        async with app.router.lifespan_context(app):
            async with (
                server.lifespan(server) as first,
                server.lifespan(server) as second,
            ):
                assert first is second is context
            # Disconnecting both clients must not tear down the game context.
            assert events == ["game start", "transport start"]
            async with server.lifespan(server) as third:
                assert third is context
        assert events == ["game start", "transport start", "transport stop", "game stop"]
        assert server.lifespan is game_lifespan
    asyncio.run(run())


def test_http_shutdown_failure_still_closes_game_context():
    async def run():
        events = []

        @asynccontextmanager
        async def game_lifespan(server):
            try:
                yield object()
            finally:
                events.append("closed")

        @asynccontextmanager
        async def transport_lifespan(app):
            yield {}
            raise RuntimeError("transport shutdown failed")

        server = SimpleNamespace(lifespan=game_lifespan)
        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=transport_lifespan))
        share_http_lifespan(app, server)
        try:
            async with app.router.lifespan_context(app):
                pass
        except RuntimeError:
            pass
        assert events == ["closed"]
        assert server.lifespan is game_lifespan
    asyncio.run(run())
