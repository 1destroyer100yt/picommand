"""PiCommand buttons — reboot, update, run command service."""
from __future__ import annotations
import logging
import voluptuous as vol
import homeassistant.helpers.config_validation as cv
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from . import DOMAIN, PiCommandCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for node_id in coordinator.data["nodes"]:
        entities += [PiCommandRebootButton(coordinator, node_id), PiCommandUpdateButton(coordinator, node_id)]
    async_add_entities(entities)

    async def handle_run_command(call: ServiceCall):
        node_id = call.data["node_id"]
        command = call.data["command"]
        timeout = call.data.get("timeout", 30)
        try:
            result = await coordinator.run_command(node_id, command, timeout)
            _LOGGER.info("PiCommand [%s] exit=%s stdout=%s", node_id, result.get("exit_code"), result.get("stdout","")[:200])
            hass.bus.async_fire("picommand_command_result", {
                "node_id": node_id, "command": command,
                "exit_code": result.get("exit_code"),
                "stdout": result.get("stdout",""), "stderr": result.get("stderr",""),
            })
        except Exception as e:
            _LOGGER.error("PiCommand run_command failed: %s", e)

    hass.services.async_register(DOMAIN, "run_command", handle_run_command,
        schema=vol.Schema({
            vol.Required("node_id"): cv.string,
            vol.Required("command"): cv.string,
            vol.Optional("timeout", default=30): vol.Coerce(int),
        }))

class PiCommandBaseButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator, node_id):
        super().__init__(coordinator)
        self._node_id = node_id
    @property
    def node(self): return self.coordinator.data["nodes"].get(self._node_id, {})
    @property
    def available(self): return self.node.get("is_online", False)
    @property
    def device_info(self):
        n = self.node
        return {"identifiers": {(DOMAIN, self._node_id)}, "name": n.get("display_name", self._node_id),
                "model": n.get("pi_model", "Raspberry Pi"), "manufacturer": "Raspberry Pi Foundation"}

class PiCommandRebootButton(PiCommandBaseButton):
    _attr_icon = "mdi:restart"
    @property
    def unique_id(self): return f"picommand_{self._node_id}_reboot"
    @property
    def name(self): return f"{self.node.get('display_name', self._node_id)} Reboot"
    async def async_press(self):
        try:
            await self.coordinator.run_command(self._node_id, "sudo reboot", timeout=10)
        except Exception:
            pass

class PiCommandUpdateButton(PiCommandBaseButton):
    _attr_icon = "mdi:update"
    @property
    def unique_id(self): return f"picommand_{self._node_id}_update"
    @property
    def name(self): return f"{self.node.get('display_name', self._node_id)} Update"
    async def async_press(self):
        await self.coordinator.run_command(self._node_id,
            "sudo apt-get update -q && sudo apt-get upgrade -y -q", timeout=300)
        await self.coordinator.async_request_refresh()
