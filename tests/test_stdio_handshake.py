"""MCP stdio smoke tests: tool registration, and that instructive errors
survive the trip to the client (MCPServer hides non-ToolError exceptions
behind a bare "Error executing tool X"). No QEMU needed."""

import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "replay_record",
    "replay_recordings",
    "replay_start",
    "replay_status",
    "replay_stop",
    "replay_continue",
    "replay_reverse_continue",
    "replay_step",
    "replay_reverse_step",
    "replay_goto",
    "replay_wait",
    "replay_interrupt",
    "replay_break",
    "replay_delete",
    "replay_registers",
    "replay_memory",
    "replay_backtrace",
    "replay_last_write",
}


@pytest.mark.anyio
async def test_stdio_handshake_lists_expected_tools():
    params = StdioServerParameters(command=sys.executable, args=["-m", "qemu_replay_mcp"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init_result = await session.initialize()
        assert init_result.server_info.name == "qemu-replay"
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert names == EXPECTED_TOOLS, (
            f"missing: {EXPECTED_TOOLS - names}, unexpected: {names - EXPECTED_TOOLS}"
        )


@pytest.mark.anyio
async def test_instructive_errors_reach_the_agent(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "qemu_replay_mcp"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        result = await session.call_tool("replay_last_write", {"name": "nope", "location": "x"})
        assert result.is_error
        assert "no replay named 'nope'" in result.content[0].text
        assert "call replay_start first" in result.content[0].text

        result = await session.call_tool(
            "replay_start", {"name": "ghost", "directory": str(tmp_path)})
        assert result.is_error and "meta.json missing" in result.content[0].text

        result = await session.call_tool("replay_record", {"name": "a/b", "kernel": "k"})
        assert result.is_error and "invalid name 'a/b'" in result.content[0].text


@pytest.fixture
def anyio_backend():
    return "asyncio"
