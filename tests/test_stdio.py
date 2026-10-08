"""End to end: launch the installed entry point and talk MCP over stdio.

Covers what the in-process tests skip: the console script, `serve` as the
default, real settings loading, and the stdio transport across a process
boundary. The server runs with no hub configured, so no network is needed.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest
from mcp import Client, StdioServerParameters

from .test_tools import TOOL_NAMES

pytestmark = pytest.mark.anyio


async def test_serve_over_stdio(tmp_path):
    exe = shutil.which("mcp-server-harmony", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("entry point not installed (pip install -e .)")
    env = {**os.environ, "HARMONY_CONFIG_DIR": str(tmp_path)}
    env.pop("HARMONY_HOST", None)
    async with Client(StdioServerParameters(command=exe, args=[], env=env)) as c:
        assert {t.name for t in (await c.list_tools()).tools} == TOOL_NAMES
        status = (await c.call_tool("get_status", {})).structured_content
        assert (status["host"], status["reachable"]) == (None, False)
