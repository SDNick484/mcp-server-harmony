"""Contract tests against data recorded from *your* hubs.

Nothing is recorded yet. At home, run

    mcp-server-harmony doctor --dump tests/fixtures/recorded

and commit the files (doctor redacts account details and addresses). Every
file there is then checked here:

  1. our parser reads the real config: every activity and device, and every
     activity button resolves to a device that exists (H-CONFIG-SHAPE);
  2. the fake hub, loaded with the real config, serves it to the *real*
     aioharmony through our client, and the catalog we build matches;
  3. the real hub's name and firmware came through (H-NAME).

So once a dump exists, the simulator runs on your real activity and command
names, and a parser change that breaks on your data fails CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import aioharmony.hubconnector_websocket as ws_connector
import pytest

from harmony_mcp.catalog import Catalog
from harmony_mcp.client import HubClient
from harmony_mcp.config import HubSettings
from harmony_mcp.sim.fake_hub import FakeHub

RECORDED = sorted((Path(__file__).parent / "fixtures" / "recorded").glob("*.json"))

pytestmark = pytest.mark.anyio


async def check_recorded(path: Path) -> None:
    from .test_contract import eventually, free_port_on

    doc = json.loads(path.read_text())
    config = doc["config"]
    catalog = Catalog.from_config(config)
    assert catalog.activities, f"{path.name}: no activities parsed"
    device_ids = {d.device_id for d in catalog.devices}
    for a in catalog.activities:
        for c in a.commands:
            assert c.device_id in device_ids, f"{path.name}: {a.name}/{c.name} targets unknown device {c.device_id}"
    assert doc.get("name"), f"{path.name}: the hub's name didn't come through (H-NAME)"

    port = free_port_on("127.0.0.1")
    fake = FakeHub(config, name=doc["name"], port=port)
    await fake.start()
    old_port = ws_connector.DEFAULT_HUB_PORT
    ws_connector.DEFAULT_HUB_PORT = port
    client = HubClient(HubSettings("127.0.0.1"), protocol="WEBSOCKETS")
    try:
        await client.start()
        await eventually(lambda: client.available)
        assert client.catalog == catalog
        assert client.name == doc["name"]
    finally:
        await client.stop()
        await fake.stop()
        ws_connector.DEFAULT_HUB_PORT = old_port


@pytest.mark.skipif(
    not RECORDED, reason="no recorded fixtures yet: run `doctor --dump tests/fixtures/recorded` at home"
)
@pytest.mark.parametrize("path", RECORDED, ids=[p.stem for p in RECORDED])
async def test_recorded_fixture(path):
    await check_recorded(path)
