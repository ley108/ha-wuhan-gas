"""欠费提醒用的两个开关。"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
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
    NAME,
    SOURCE_BABEL,
)
from .coordinator import GasData, WuhanGasCoordinator


def _has_arrears(data: GasData) -> bool:
    return data.has_arrears


def _low_balance(data: GasData) -> bool:
    return data.is_low


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WuhanGasCoordinator = entry.runtime_data
    # 小程序接口只有账单，没有余额/欠费字段，这两个开关建出来永远是「关」，不如不建
    if str(entry.data.get(CONF_SOURCE) or "") == SOURCE_BABEL:
        return
    single = len(hass.config_entries.async_entries(DOMAIN)) == 1
    async_add_entities(
        [
            GasFlag(
                coordinator,
                entry,
                single,
                BinarySensorEntityDescription(
                    key="unpaid", name="有燃气欠费", icon="mdi:alert-circle"
                ),
                _has_arrears,
            ),
            GasFlag(
                coordinator,
                entry,
                single,
                BinarySensorEntityDescription(
                    key="low_balance", name="燃气余额不足", icon="mdi:cash-alert"
                ),
                _low_balance,
            ),
        ]
    )


class GasFlag(CoordinatorEntity[WuhanGasCoordinator], BinarySensorEntity):
    """一个开关；两格只是判断条件不同。"""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: WuhanGasCoordinator,
        entry: ConfigEntry,
        single: bool,
        description: BinarySensorEntityDescription,
        value_fn,
    ) -> None:
        super().__init__(coordinator)
        # 必须给一个真的 EntityDescription：HA 内部会读
        # entity_description.entity_registry_enabled_default，给 None 会直接抛异常。
        self.entity_description = description
        self._value_fn = value_fn
        account = str(entry.data.get(CONF_ACCOUNT) or "")
        self._account = account
        self._attr_unique_id = f"{account}_{description.key}"
        self._attr_attribution = GAS_ATTRIBUTION
        prefix = "wuhan_gas" if single else f"wuhan_gas_{account}"
        self.entity_id = f"binary_sensor.{prefix}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, account)},
            "name": f"{NAME} {entry.data.get(CONF_ACCOUNT_NAME) or account}",
            "manufacturer": GAS_MANUFACTURER,
            "model": GAS_MODEL,
            "configuration_url": GAS_PORTAL_URL,
        }

    @property
    def is_on(self) -> bool:
        return bool(self._value_fn(self.coordinator.data))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data
        return {
            "账户余额": data.balance,
            "欠费金额": data.arrears,
            "户号": self._account,
        }
