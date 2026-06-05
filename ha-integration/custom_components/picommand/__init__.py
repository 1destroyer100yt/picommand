"""PiCommand Home Assistant Integration."""
from __future__ import annotations
import logging
from datetime import timedelta
import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)
DOMAIN = "picommand"
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]
SCAN_INTERVAL = timedelta(seconds=30)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = PiCommandCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

class PiCommandCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.url = entry.data["url"].rstrip("/")
        self.token = entry.data["token"]
        self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"},
                connector=aiohttp.TCPConnector(ssl=False),
            )
        return self._session

    async def api_get(self, path):
        session = await self._get_session()
        async with session.get(f"{self.url}{path}") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def api_post(self, path, data=None):
        session = await self._get_session()
        async with session.post(f"{self.url}{path}", json=data or {}) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _async_update_data(self):
        try:
            nodes = await self.api_get("/api/nodes")
            stats = await self.api_get("/api/stats")
            metrics = {}
            for node in nodes:
                if node.get("is_online"):
                    try:
                        m = await self.api_get(f"/api/nodes/{node['node_id']}/metrics/latest")
                        metrics[node["node_id"]] = m
                    except Exception:
                        metrics[node["node_id"]] = {}
            return {"nodes": {n["node_id"]: n for n in nodes}, "metrics": metrics, "stats": stats}
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error: {err}") from err

    async def run_command(self, node_id, command, timeout=30):
        return await self.api_post(f"/api/nodes/{node_id}/commands", {"command": command, "timeout": timeout})
