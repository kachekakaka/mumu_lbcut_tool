"""真实 loopback HTTP，验证桥接的敏感响应大小、类型与重定向边界。"""

import asyncio
import json
import uuid

import pytest

from src.orchestrator.upload_core_client import PNG_LIMIT, BridgeError, call


def roundtrip(body, content_type, status=200, **kwargs):
    async def exercise():
        async def serve(reader, writer):
            incoming = await reader.readuntil(b"\r\n\r\n")
            assert b"x-upload-platform-client: gui-bridge-v1" in incoming.lower()
            writer.write(
                f"HTTP/1.1 {status} test\r\nContent-Type: {content_type}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
            return await call(
                server.sockets[0].getsockname()[1], "/api/v1/system/health", **kwargs
            )

    return asyncio.run(exercise())


@pytest.mark.parametrize(
    ("body", "mime", "status"),
    [
        (bytes(PNG_LIMIT + 1), "image/png", 200),
        (b"other", "image/png", 200),
        (b"{}", "text/plain", 200),
        (b"{}", "application/json", 302),
    ],
    ids=["oversize", "invalid-png", "wrong-type", "redirect"],
)
def test_invalid_or_oversize_image(body, mime, status):
    with pytest.raises(BridgeError, match="无法验证"):
        roundtrip(body, mime, status, png=True)


@pytest.mark.parametrize("status", [409, 410])
def test_image_failure_only_returns_safe_code(status):
    private_marker = str(uuid.uuid4())
    with pytest.raises(BridgeError) as info:
        roundtrip(
            json.dumps(
                {"code": "login_challenge_expired", "message": private_marker}
            ).encode(),
            "application/json",
            status,
            png=True,
        )
    assert info.value.status == status and info.value.code == "login_challenge_expired"
    assert private_marker not in str(info.value)
