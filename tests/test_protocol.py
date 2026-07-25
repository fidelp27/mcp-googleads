import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "server_status",
    "list_accounts",
    "execute_gaql_query",
    "get_campaign_performance",
    "get_ad_performance",
    "run_gaql",
    "get_ad_creatives",
    "get_account_currency",
    "get_image_assets",
    "download_image_asset",
    "get_asset_usage",
    "analyze_image_assets",
    "list_resources",
}


@pytest.mark.asyncio
async def test_stdio_transport_lists_tools():
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_ROOT / "google_ads_server.py"), "--transport", "stdio"],
        cwd=str(PROJECT_ROOT),
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            tools = {tool.name: tool for tool in result.tools}
            assert EXPECTED_TOOLS <= tools.keys()
            assert tools["list_accounts"].annotations.readOnlyHint is True
            assert tools["download_image_asset"].annotations.readOnlyHint is False
            status = await session.call_tool("server_status")
            assert status.isError is False
            assert "google-ads-mcp" in str(status.content)


@pytest.mark.skipif(os.name != "nt", reason="Claude launcher is Windows-specific")
@pytest.mark.asyncio
async def test_claude_desktop_launcher_lists_tools():
    parameters = StdioServerParameters(
        command="powershell.exe",
        args=[
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "scripts" / "run_stdio.ps1"),
        ],
        cwd=str(PROJECT_ROOT),
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            assert EXPECTED_TOOLS <= {tool.name for tool in result.tools}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for_port(port: int) -> None:
    for _ in range(100):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Server did not start on port {port}")


@pytest.mark.asyncio
async def test_streamable_http_transport_lists_tools():
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "google_ads_server.py"),
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        await _wait_for_port(port)
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.list_tools()
                tools = {tool.name: tool for tool in result.tools}
                assert EXPECTED_TOOLS <= tools.keys()
                assert tools["list_accounts"].annotations.readOnlyHint is True
                assert tools["download_image_asset"].annotations.readOnlyHint is False
                status = await session.call_tool("server_status")
                assert status.isError is False
                assert "google-ads-mcp" in str(status.content)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
