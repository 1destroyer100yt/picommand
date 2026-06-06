"""PiCommand sensors."""
from __future__ import annotations
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from . import DOMAIN, PiCommandCoordinator

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for node_id in coordinator.data["nodes"]:
        entities += [
            PiCommandCPUSensor(coordinator, node_id),
            PiCommandRAMSensor(coordinator, node_id),
            PiCommandTempSensor(coordinator, node_id),
            PiCommandDiskSensor(coordinator, node_id),
            PiCommandUptimeSensor(coordinator, node_id),
            PiCommandIPSensor(coordinator, node_id),
            PiCommandLoadSensor(coordinator, node_id),
        ]
    async_add_entities(entities)

class PiCommandBaseSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, node_id):
        super().__init__(coordinator)
        self._node_id = node_id

    @property
    def node(self):
        return self.coordinator.data["nodes"].get(self._node_id, {})

    @property
    def metrics(self):
        return self.coordinator.data["metrics"].get(self._node_id, {})

    @property
    def available(self):
        return self.node.get("is_online", False)

    @property
    def device_info(self):
        n = self.node
        return {
            "identifiers": {(DOMAIN, self._node_id)},
            "name": n.get("display_name", self._node_id),
            "model": n.get("pi_model", "Raspberry Pi"),
            "sw_version": n.get("os_version"),
            "manufacturer": "Raspberry Pi Foundation",
        }

class PiCommandCPUSensor(PiCommandBaseSensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cpu-64-bit"

    @property
    def unique_id(self):
        return f"picommand_{self._node_id}_cpu"

    @property
    def name(self):
        return f"{self.node.get('display_name', self._node_id)} CPU"

    @property
    def native_value(self):
        v = self.metrics.get("cpu_percent")
        return round(v, 1) if v is not None else None

class PiCommandRAMSensor(PiCommandBaseSensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:memory"

    @property
    def unique_id(self):
        return f"picommand_{self._node_id}_ram"

    @property
    def name(self):
        return f"{self.node.get('display_name', self._node_id)} RAM"

    @property
    def native_value(self):
        v = self.metrics.get("ram_percent")
        return round(v, 1) if v is not None else None

    @property
    def extra_state_attributes(self):
        m = self.metrics
        return {"used_mb": m.get("ram_used_mb"), "total_mb": m.get("ram_total_mb")}

class PiCommandTempSensor(PiCommandBaseSensor):
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        return f"picommand_{self._node_id}_temp"

    @property
    def name(self):
        return f"{self.node.get('display_name', self._node_id)} Temperature"

    @property
    def native_value(self):
        v = self.metrics.get("cpu_temp_c")
        return round(v, 1) if v is not None else None

class PiCommandDiskSensor(PiCommandBaseSensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:harddisk"

    @property
    def unique_id(self):
        return f"picommand_{self._node_id}_disk"

    @property
    def name(self):
        return f"{self.node.get('display_name', self._node_id)} Disk"

    @property
    def native_value(self):
        v = self.metrics.get("disk_percent")
        return round(v, 1) if v is not None else None

    @property
    def extra_state_attributes(self):
        m = self.metrics
        return {
            "used_gb": round(m["disk_used_gb"], 2) if m.get("disk_used_gb") else None,
            "total_gb": round(m["disk_total_gb"], 2) if m.get("disk_total_gb") else None,
        }

class PiCommandUptimeSensor(PiCommandBaseSensor):
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:clock-outline"

    @property
    def unique_id(self):
        return f"picommand_{self._node_id}_uptime"

    @property
    def name(self):
        return f"{self.node.get('display_name', self._node_id)} Uptime"

    @property
    def native_value(self):
        return self.metrics.get("uptime_seconds")

class PiCommandIPSensor(PiCommandBaseSensor):
    _attr_icon = "mdi:ip-network"

    @property
    def unique_id(self):
        return f"picommand_{self._node_id}_ip"

    @property
    def name(self):
        return f"{self.node.get('display_name', self._node_id)} IP"

    @property
    def native_value(self):
        return self.node.get("ip_address")

    @property
    def available(self):
        return True

    @property
    def extra_state_attributes(self):
        n = self.node
        return {
            "hostname": n.get("hostname"),
            "location": n.get("location"),
            "last_seen": n.get("last_seen"),
            "os": n.get("os_version"),
            "model": n.get("pi_model"),
        }

class PiCommandLoadSensor(PiCommandBaseSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:gauge"

    @property
    def unique_id(self):
        return f"picommand_{self._node_id}_load"

    @property
    def name(self):
        return f"{self.node.get('display_name', self._node_id)} Load"

    @property
    def native_value(self):
        v = self.metrics.get("load_avg_1")
        return round(v, 2) if v is not None else None

    @property
    def extra_state_attributes(self):
        m = self.metrics
        return {"load_5m": m.get("load_avg_5"), "load_15m": m.get("load_avg_15")}
