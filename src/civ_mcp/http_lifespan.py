"""Share one game context across all HTTP sessions for the app's lifetime."""

from contextlib import asynccontextmanager


def share_http_lifespan(app, server):
    """Move the MCP context from per-session lifetime to ASGI app lifetime.

    The SDK calls Server.lifespan for every HTTP session. Without this wrapper,
    a single HTTP process still opens one tuner connection per client.
    """
    game_lifespan = server.lifespan
    transport_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def app_lifespan(asgi_app):
        async with game_lifespan(server) as context:
            @asynccontextmanager
            async def session_lifespan(_server):
                yield context

            server.lifespan = session_lifespan
            try:
                # Stop sessions before closing their shared connection/services.
                async with transport_lifespan(asgi_app) as state:
                    yield state
            finally:
                server.lifespan = game_lifespan

    app.router.lifespan_context = app_lifespan
    return app
