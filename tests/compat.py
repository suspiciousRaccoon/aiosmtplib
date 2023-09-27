import asyncio


async def cleanup_server(server: asyncio.AbstractServer) -> None:
    async with asyncio.timeout(0.1):
        try:
            await server.wait_closed()
        except asyncio.CancelledError:
            pass
