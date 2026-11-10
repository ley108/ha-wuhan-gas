"""武汉燃气费的传感器。

金额一律不带 ``device_class: monetary``：HA 要求那玩意儿配 ISO 货币代码，
而「元」不是，前端会拿它去喂 Intl.NumberFormat 直接抛错（水费集成踩过这个坑）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ACCOUNT,
    CONF_ACCOUNT_NAME,
    CONF_SOURCE,
    DOMAIN,
    GAS_ATTRIBUTION,
    GAS_MANUFACTURER,
    GAS_MODEL,
    GAS_PORTAL_URL,
    ICON_YUAN,
    NAME,
    SOURCE_API,
    SOURCE_BABEL,
    SOURCE_MANUAL,
    UNIT_CUBIC,
    UNIT_PRICE,
    UNIT_YUAN,
)
from .coordinator import GasData, WuhanGasCoordinator


def _picked_path(data: GasData, key: str) -> str | None:
    item = (data.picked or {}).get(key)
    if isinstance(item, dict):
        path = item.get("path")
        return str(path) if path else None
    return None


@dataclass(frozen=True, kw_only=True)
class GasSensorDescription(SensorEntityDescription):
    value_fn: Callable[[GasData], object]
    attributes_fn: Callable[[GasData], dict[str, object]] | None = None
    sources: tuple[str, ...] | None = None


SENSORS: tuple[GasSensorDescription, ...] = (
    GasSensorDescription(
        key="balance",
        name="账户余额",
        icon=ICON_YUAN,
        native_unit_of_measurement=UNIT_YUAN,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.balance,
        sources=(SOURCE_API, SOURCE_MANUAL),
    ),
    GasSensorDescription(
        key="arrears",
        name="欠费金额",
        icon="mdi:alert-circle-outline",
        native_unit_of_measurement=UNIT_YUAN,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.arrears,
        sources=(SOURCE_API, SOURCE_MANUAL),
    ),
    GasSensorDescription(
        key="bill",
        name="最近一期气费",
        icon="mdi:fire",
        native_unit_of_measurement=UNIT_YUAN,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.bill,
    ),
    GasSensorDescription(
        key="usage",
        name="最近一期用气量",
        icon="mdi:meter-gas",
        native_unit_of_measurement=UNIT_CUBIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.usage,
    ),
    GasSensorDescription(
        key="read_date",
        name="最近抄表日期",
        icon="mdi:calendar-check",
        value_fn=lambda data: data.read_date,
        attributes_fn=lambda data: {"来源字段": _picked_path(data, "read_date")},
    ),
    GasSensorDescription(
        key="price",
        name="气价",
        icon="mdi:currency-cny",
        native_unit_of_measurement=UNIT_PRICE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.price,
        sources=(SOURCE_API, SOURCE_MANUAL),
    ),
    GasSensorDescription(
        key="meter",
        name="最新表底数",
        icon="mdi:gauge",
        native_unit_of_measurement=UNIT_CUBIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.meter,
    ),
    GasSensorDescription(
        key="period",
        name="最近账期",
        icon="mdi:calendar-month",
        value_fn=lambda data: data.period,
        sources=(SOURCE_BABEL,),
    ),
    GasSensorDescription(
        key="prev_reading",
        name="上期表底数",
        icon="mdi:gauge-low",
        native_unit_of_measurement=UNIT_CUBIC,
        value_fn=lambda data: data.prev_reading,
        sources=(SOURCE_BABEL,),
    ),
    GasSensorDescription(
        key="year_fee",
        name="本年累计气费",
        icon="mdi:cash-multiple",
        native_unit_of_measurement=UNIT_YUAN,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.year_fee,
        sources=(SOURCE_BABEL,),
    ),
    GasSensorDescription(
        key="year_usage",
        name="本年累计用气量",
        icon="mdi:gas-cylinder",
        native_unit_of_measurement=UNIT_CUBIC,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.year_usage,
        sources=(SOURCE_BABEL,),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WuhanGasCoordinator = entry.runtime_data
    single = len(hass.config_entries.async_entries(DOMAIN)) == 1
    source = str(entry.data.get(CONF_SOURCE) or "")
    async_add_entities(
        GasSensor(coordinator, entry, description, single)
        for description in SENSORS
        if description.sources is None or source in description.sources
    )


class GasSensor(CoordinatorEntity[WuhanGasCoordinator], SensorEntity):
    """一格气费读数。"""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WuhanGasCoordinator,
        entry: ConfigEntry,
        description: GasSensorDescription,
        single: bool,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        account = str(entry.data.get(CONF_ACCOUNT) or "")
        name = str(entry.data.get(CONF_ACCOUNT_NAME) or "")
        self._account = account
        self._name = name
        self._attr_unique_id = f"{account}_{description.key}"
        self._attr_attribution = GAS_ATTRIBUTION
        # 只有一个账户时给最短的实体 id，面板和自动化里都好写
        prefix = "wuhan_gas" if single else f"wuhan_gas_{account}"
        self.entity_id = f"sensor.{prefix}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, account)},
            "name": f"{NAME} {name or account}",
            "manufacturer": GAS_MANUFACTURER,
            "model": GAS_MODEL,
            "configuration_url": GAS_PORTAL_URL,
        }

    @property
    def native_value(self) -> object:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data
        base: dict[str, object] = {
            "户号": self._account,
            "户名": self._name or data.name,
            "数据来源": "手动录入" if data.source == SOURCE_MANUAL else "微营业厅接口",
            "更新时间": data.fetched_at or None,
        }
        if data.address:
            base["地址"] = data.address
        if data.source == SOURCE_BABEL:
            base["数据来源"] = "微信小程序「武汉燃气」"
            base["本年账期数"] = data.months
        if self.entity_description.attributes_fn is not None:
            base.update(self.entity_description.attributes_fn(data) or {})
        if data.source == SOURCE_API:
            base["识别路径"] = _picked_path(data, self.entity_description.key)
        return base
