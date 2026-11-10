"""武汉燃气费集成入口。

整个微营业厅挂在微信 OAuth 后面，HA 没法自己登录，所以数据有三种来路：

- ``babel``：微信小程序「武汉燃气」的云接口，只认请求头里的 ``token``。
  token 由抓包容器（gas-token-capture）自动抓到后调 ``wuhan_gas.set_token`` 写进来，
  之后集成按 token 定时拉账单；
- ``api``：用户抓一次包，把那条请求粘进来，集成按原样重放并自动识别字段；
- ``manual``：没有抓包条件时，用 ``wuhan_gas.set_reading`` 服务手工录，
  抓包成功后在集成选项里切回 ``api`` 即可，实体和自动化都不用改。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ACCOUNT,
    CONF_ACCOUNT_NAME,
    CONF_METER_TYPE,
    CONF_ORGID,
    CONF_REQUEST,
    CONF_SOURCE,
    CONF_TOKEN,
    DOMAIN,
    NAME,
    SOURCE_BABEL,
    SOURCE_MANUAL,
)
from .coordinator import (
    BabelGasApi,
    GasData,
    WuhanGasApi,
    WuhanGasCoordinator,
    build_overrides,
)
from .parser import RequestSpec

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

SERVICE_REFRESH = "refresh"
SERVICE_SET_READING = "set_reading"
SERVICE_DUMP = "dump_response"
SERVICE_SET_TOKEN = "set_token"

STORAGE_VERSION = 1

SET_TOKEN_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("token"): cv.string,
        vol.Optional("userno"): cv.string,
        vol.Optional("orgid"): cv.string,
        vol.Optional("meter_type"): cv.string,
        # 从哪儿来的：capture = 抓包代理自动上报，manual = 人手动调服务
        vol.Optional("origin", default="capture"): cv.string,
    }
)

SET_READING_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Optional("balance"): vol.Coerce(float),
        vol.Optional("arrears"): vol.Coerce(float),
        vol.Optional("usage"): vol.Coerce(float),
        vol.Optional("bill"): vol.Coerce(float),
        vol.Optional("price"): vol.Coerce(float),
        vol.Optional("meter"): vol.Coerce(float),
        vol.Optional("read_date"): cv.string,
    }
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """服务在集成层注册一次，跟条目数量无关。"""

    async def _entries(call: ServiceCall) -> list[ConfigEntry]:
        entry_id = call.data.get("entry_id")
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.runtime_data is not None
        ]
        if entry_id:
            entries = [entry for entry in entries if entry.entry_id == entry_id]
        return entries

    async def async_refresh(call: ServiceCall) -> None:
        for entry in await _entries(call):
            await entry.runtime_data.async_request_refresh()

    async def async_set_reading(call: ServiceCall) -> None:
        entries = await _entries(call)
        if not call.data.get("entry_id") and len(entries) > 1:
            _LOGGER.warning("%s 有多个账户，建议指定 entry_id，已全部写入", NAME)
        for entry in entries:
            await entry.runtime_data.async_set_manual(dict(call.data))

    async def async_dump(call: ServiceCall) -> dict[str, Any]:
        """把最近一次接口返回原样导出，方便核对字段、贴给维护者。

        写到 ``/config/www/wuhan_gas_dump.json``，浏览器打开
        ``http://<HA>:8123/local/wuhan_gas_dump.json`` 就能看；同时也作为服务响应返回。
        """
        entries = await _entries(call)
        payload: dict[str, Any] = {}
        for entry in entries:
            data: GasData = entry.runtime_data.data
            key = data.account or entry.entry_id
            payload[key] = {
                "识别到的字段": {
                    name: {"值": item.get("value"), "路径": item.get("path")}
                    for name, item in (data.picked or {}).items()
                },
                "原始返回": data.raw_preview or "（还没有成功取到数据）",
                "更新时间": data.fetched_at,
            }

        if payload:
            target = Path(hass.config.path("www", "wuhan_gas_dump.json"))
            body = json.dumps(payload, ensure_ascii=False, indent=2)
            await hass.async_add_executor_job(_write_file, target, body)
        return {"accounts": payload}

    async def async_set_token(call: ServiceCall) -> dict[str, Any]:
        """把抓包代理抓到的 token 写回配置条目。

        小程序接口只认请求头里的 token，而 token 只在微信登录小程序那一刻出现。
        抓包容器（gas-token-capture）解到 ``wp.babel-group.cn`` 的请求后调这个服务，
        集成这边就自动换 token 并重载，不用人再点「重新授权」。
        """
        token = str(call.data.get("token") or "").strip()
        userno = str(call.data.get("userno") or "").strip()
        if len(token) < 20:
            _LOGGER.warning("%s 收到的 token 太短，忽略（%s…）", NAME, token[:8])
            return {"updated": 0, "reason": "token_too_short"}

        targets = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if str(entry.data.get(CONF_SOURCE) or "") == SOURCE_BABEL
        ]
        entry_id = call.data.get("entry_id")
        if entry_id:
            targets = [entry for entry in targets if entry.entry_id == entry_id]
        if userno:
            matched = [
                entry for entry in targets
                if str(entry.data.get(CONF_ACCOUNT) or "") == userno
            ]
            if matched:
                targets = matched

        updated: list[str] = []
        for entry in targets:
            data = dict(entry.data)
            changed = data.get(CONF_TOKEN) != token
            data[CONF_TOKEN] = token
            if userno:
                data[CONF_ACCOUNT] = userno
            if call.data.get("orgid"):
                data[CONF_ORGID] = str(call.data["orgid"])
            if call.data.get("meter_type"):
                data[CONF_METER_TYPE] = str(call.data["meter_type"])

            if changed:
                # 换 token 要重建 api 对象，交给重载处理
                hass.config_entries.async_update_entry(entry, data=data)
                await hass.config_entries.async_reload(entry.entry_id)
            elif entry.runtime_data is not None:
                await entry.runtime_data.async_request_refresh()
            updated.append(entry.entry_id)

        _LOGGER.info(
            "%s 收到抓包 token（%s…，来源 %s），已处理 %d 个条目",
            NAME,
            token[:8],
            call.data.get("origin") or "capture",
            len(updated),
        )
        return {"updated": len(updated), "entries": updated, "changed": bool(updated)}

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, async_refresh)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_READING, async_set_reading, schema=SET_READING_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DUMP, async_dump, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_TOKEN,
        async_set_token,
        schema=SET_TOKEN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def _write_file(target: Path, body: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def build_api(hass: HomeAssistant, entry: ConfigEntry) -> WuhanGasApi | BabelGasApi:
    if str(entry.data.get(CONF_SOURCE) or "") == SOURCE_BABEL:
        return BabelGasApi(
            async_get_clientsession(hass),
            token=str(entry.data.get(CONF_TOKEN) or ""),
            userno=str(entry.data.get(CONF_ACCOUNT) or ""),
            orgid=str(entry.data.get(CONF_ORGID) or "1"),
            meter_type=str(entry.data.get(CONF_METER_TYPE) or "1"),
            name_hint=str(entry.data.get(CONF_ACCOUNT_NAME) or ""),
        )
    raw = entry.data.get(CONF_REQUEST)
    spec = (
        RequestSpec.from_dict(raw)
        if isinstance(raw, dict)
        else RequestSpec(url=str(raw or ""))
    )
    return WuhanGasApi(
        async_get_clientsession(hass), spec, build_overrides(entry)
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    source = str(entry.data.get(CONF_SOURCE) or "")
    store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")

    manual: GasData | None = None
    stored = await store.async_load()
    if isinstance(stored, dict):
        manual = GasData.from_dict(stored)

    api = None if source == SOURCE_MANUAL else build_api(hass, entry)
    coordinator = WuhanGasCoordinator(hass, entry, api, store, manual)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
