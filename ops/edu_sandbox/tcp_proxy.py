"""Minimal fixed-target TCP bridge for loopback access to an internal sandbox."""

from __future__ import annotations

import asyncio
import os
import re

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080
TARGET_HOST = os.environ.get("ODIN_EDU_PROXY_TARGET", "odin")
TARGET_PORT = 8000

if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,126}", TARGET_HOST):
    raise SystemExit("invalid fixed proxy target")


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def _handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    try:
        target_reader, target_writer = await asyncio.open_connection(
            TARGET_HOST, TARGET_PORT
        )
    except OSError:
        client_writer.close()
        await client_writer.wait_closed()
        return
    await asyncio.gather(
        _copy(client_reader, target_writer),
        _copy(target_reader, client_writer),
        return_exceptions=True,
    )


async def main() -> None:
    server = await asyncio.start_server(_handle, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
