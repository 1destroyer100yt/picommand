"""PiCommand binary sensors."""
from __future__ import annotations
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from . import DOMAIN

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PiCommandOnlineSensor(coordinator, nid) for nid in coordinator.data["nodes"]])

class PiCommandOnlineSensor(CoordinatorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, node_id):
        super().__init__(coordinator)
        self._node_id = node_id

    @property
    def node(self):
        return self.coordinator.data["nodes"].get(self._node_id, {})

    @property
    def unique_id(self):
        return f"picommand_{self._node_id}_online"

    @property
    def name(self):
        return f"{self.node.get('display_name', self._node_id)} Online"

    @property
    def is_on(self):
        return self.node.get("is_online", False)

    @property
    def available(self):
        return True

    @property
    def device_info(self):
        n = self.node
        return {
            "identifiers": {(DOMAIN, self._node_id)},
            "name": n.get("display_name", self._node_id),
            "model": n.get("pi_model", "Raspberry Pi"),
            "manufacturer": "Raspberry Pi Foundation",
        }

    @property
    def extra_state_attributes(self):
        n = self.node
        return {
            "node_id": self._node_id,
            "location": n.get("location"),
            "last_seen": n.get("last_seen"),
        }
