"""配置流程：户号 → 抓包请求（或手动模式）。"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectOptionDict,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ACCOUNT,
    CONF_ACCOUNT_NAME,
    CONF_COOKIE,
    CONF_ENDPOINT,
    CONF_FIELDS,
    CONF_METER_TYPE,
    CONF_ORGID,
    CONF_REFRESH_MINUTES,
    CONF_REQUEST,
    CONF_SOURCE,
    CONF_TOKEN,
    DOMAIN,
    GAS_PORTAL_URL,
    NAME,
    SOURCE_API,
    SOURCE_BABEL,
    SOURCE_LABELS,
    SOURCE_MANUAL,
)
from .coordinator import BabelGasApi, WuhanGasApi, WuhanGasError
from .parser import parse_request

_LOGGER = logging.getLogger(__name__)

FIELD_KEYS = {
    "balance": "账户余额",
    "arrears": "欠费金额",
    "usage": "最近一期用气量",
    "bill": "最近一期气费",
    "read_date": "最近抄表日期",
    "price": "气价",
    "meter": "最新表底数",
}

SOURCE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SOURCE, default=SOURCE_BABEL): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=key, label=label)
                    for key, label in SOURCE_LABELS.items()
                ],
                mode="list",
            )
        ),
        vol.Required(CONF_ACCOUNT): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="off")
        ),
        vol.Optional(CONF_ACCOUNT_NAME, default=""): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="off")
        ),
    }
)


def _api_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_REQUEST, default=str(defaults.get(CONF_REQUEST, ""))
            ): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
            ),
            vol.Optional(CONF_COOKIE, default=str(defaults.get(CONF_COOKIE, ""))): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
            ),
            vol.Optional(
                CONF_REFRESH_MINUTES, default=int(defaults.get(CONF_REFRESH_MINUTES) or 360)
            ): NumberSelector(
                NumberSelectorConfig(min=30, max=1440, step=30, mode=NumberSelectorMode.BOX)
            ),
            vol.Optional("force", default=False): cv.boolean,
        }
    )


def _babel_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_TOKEN, default=str(defaults.get(CONF_TOKEN, ""))): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
            ),
            vol.Optional(
                CONF_REFRESH_MINUTES, default=int(defaults.get(CONF_REFRESH_MINUTES) or 360)
            ): NumberSelector(
                NumberSelectorConfig(min=30, max=1440, step=30, mode=NumberSelectorMode.BOX)
            ),
        }
    )


def _options_schema(options: dict[str, Any], data: dict[str, Any]) -> vol.Schema:
    fields = options.get(CONF_FIELDS) or {}
    schema: dict[Any, Any] = {
        vol.Optional(
            CONF_REQUEST, default=str(data.get(CONF_REQUEST) or "")
        ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)),
        vol.Optional(CONF_COOKIE, default=str(data.get(CONF_COOKIE) or "")): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
        ),
        vol.Optional(
            CONF_REFRESH_MINUTES,
            default=int(options.get(CONF_REFRESH_MINUTES) or 360),
        ): NumberSelector(
            NumberSelectorConfig(min=30, max=1440, step=30, mode=NumberSelectorMode.BOX)
        ),
    }
    for key, label in FIELD_KEYS.items():
        schema[vol.Optional(f"path_{key}", default=str(fields.get(key) or ""))] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="off")
        )
    return vol.Schema(schema)


class WuhanGasConfigFlow(ConfigFlow, domain=DOMAIN):
    """添加一个燃气户号。"""

    VERSION = 1

    def __init__(self) -> None:
        self._account: str = ""
        self._name: str = ""
        self._source: str = SOURCE_MANUAL
        self._entry: ConfigEntry | None = None

    # ------------------------------------------------------------ 首次添加

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            account = str(user_input[CONF_ACCOUNT]).strip()
            name = str(user_input.get(CONF_ACCOUNT_NAME) or "").strip()
            source = str(user_input[CONF_SOURCE])
            if not (3 <= len(account) <= 20):
                errors[CONF_ACCOUNT] = "invalid_account"
            else:
                await self.async_set_unique_id(f"{source}_{account}")
                self._abort_if_unique_id_configured()
                self._account = account
                self._name = name
                self._source = source
                if source == SOURCE_MANUAL:
                    return self.async_create_entry(
                        title=f"{NAME} {name or account}",
                        data={
                            CONF_SOURCE: SOURCE_MANUAL,
                            CONF_ACCOUNT: account,
                            CONF_ACCOUNT_NAME: name,
                        },
                    )
                if source == SOURCE_BABEL:
                    return await self.async_step_babel()
                return await self.async_step_api()
        return self.async_show_form(
            step_id="user",
            data_schema=SOURCE_SCHEMA,
            errors=errors,
            description_placeholders={"portal": GAS_PORTAL_URL},
        )

    async def async_step_babel(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """小程序「武汉燃气」：只认一个 token。"""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(CONF_TOKEN) or "").strip()
            token = token.split()[0] if token.startswith("token") else token
            if len(token) < 20:
                errors[CONF_TOKEN] = "invalid_token"
            else:
                api = BabelGasApi(
                    async_get_clientsession(self.hass), token, userno=self._account
                )
                try:
                    row = await self._babel_probe(api)
                except WuhanGasError as error:
                    errors["base"] = error.code
                else:
                    return self._create_babel(api, row, user_input)
        return self.async_show_form(
            step_id="babel",
            data_schema=_babel_schema(user_input),
            errors=errors,
            description_placeholders={"account": self._account or "（自动识别）"},
        )

    async def _babel_probe(self, api: BabelGasApi) -> dict[str, Any]:
        accounts = await api.async_accounts()
        if not accounts:
            raise WuhanGasError("这个 token 下面没有绑定燃气户号", "no_account")
        row = accounts[0]
        for item in accounts:
            if self._account and str(item.get("userno")) == self._account:
                row = item
                break
        api.userno = str(row.get("userno") or self._account)
        api.orgid = str(row.get("dataorgid") or "1")
        api.meter_type = str(row.get("type") or "1")
        await api.async_fetch()
        return row

    def _create_babel(
        self, api: BabelGasApi, row: dict[str, Any], user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        userno = str(row.get("userno") or api.userno or self._account)
        name = str(row.get("username") or self._name or "")
        return self.async_create_entry(
            title=f"{NAME} {name or userno}",
            data={
                CONF_SOURCE: SOURCE_BABEL,
                CONF_ACCOUNT: userno,
                CONF_ACCOUNT_NAME: name,
                CONF_TOKEN: api.token,
                CONF_ORGID: api.orgid,
                CONF_METER_TYPE: api.meter_type,
                "address": str(row.get("address") or ""),
            },
            options={
                CONF_REFRESH_MINUTES: int(user_input.get(CONF_REFRESH_MINUTES) or 360),
            },
        )

    async def async_step_api(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            request_text = str(user_input.get(CONF_REQUEST) or "").strip()
            cookie = str(user_input.get(CONF_COOKIE) or "").strip()
            force = bool(user_input.get("force"))
            if not request_text and not cookie:
                errors[CONF_REQUEST] = "invalid_request"
            else:
                spec = parse_request(request_text, cookie)
                if not spec.url:
                    errors[CONF_REQUEST] = "invalid_request"
                else:
                    try:
                        await self._check(spec)
                    except WuhanGasError as error:
                        _LOGGER.debug("校验燃气接口失败：%s", error)
                        if force:
                            return self._create_api(spec, user_input)
                        errors["base"] = error.code
                    else:
                        return self._create_api(spec, user_input)
        return self.async_show_form(
            step_id="api",
            data_schema=_api_schema(user_input),
            errors=errors,
            description_placeholders={"portal": GAS_PORTAL_URL},
        )

    def _create_api(self, spec, user_input: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"{NAME} {self._name or self._account}",
            data={
                CONF_SOURCE: SOURCE_API,
                CONF_ACCOUNT: self._account,
                CONF_ACCOUNT_NAME: self._name,
                CONF_REQUEST: spec.as_dict(),
                CONF_COOKIE: str(user_input.get(CONF_COOKIE) or ""),
            },
            options={
                CONF_REFRESH_MINUTES: int(user_input.get(CONF_REFRESH_MINUTES) or 360),
            },
        )

    async def _check(self, spec) -> None:
        api = WuhanGasApi(async_get_clientsession(self.hass), spec)
        data, _payload = await api.async_fetch()
        if data.balance is None and data.arrears is None and data.usage is None:
            raise WuhanGasError("接口通了，但没认出余额/欠费/用气量", "no_field")

    # ------------------------------------------------------------ 令牌过期

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._entry
        if entry is None:
            return self.async_abort(reason="reauth_failed")
        if str(entry.data.get(CONF_SOURCE) or "") == SOURCE_BABEL:
            return await self._async_reauth_babel(entry, user_input)
        errors: dict[str, str] = {}
        if user_input is not None:
            request_text = str(user_input.get(CONF_REQUEST) or "").strip()
            cookie = str(user_input.get(CONF_COOKIE) or "").strip()
            spec = parse_request(request_text, cookie)
            if not spec.url:
                errors[CONF_REQUEST] = "invalid_request"
            else:
                try:
                    await self._check(spec)
                except WuhanGasError as error:
                    if user_input.get("force"):
                        return self._finish_reauth(entry, spec, user_input)
                    errors["base"] = error.code
                else:
                    return self._finish_reauth(entry, spec, user_input)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_api_schema(
                {
                    CONF_REQUEST: (entry.data.get(CONF_REQUEST) or {}).get("url")
                    if isinstance(entry.data.get(CONF_REQUEST), dict)
                    else "",
                    CONF_COOKIE: entry.data.get(CONF_COOKIE, ""),
                    CONF_REFRESH_MINUTES: entry.options.get(CONF_REFRESH_MINUTES, 360),
                }
            ),
            errors=errors,
        )

    def _finish_reauth(self, entry: ConfigEntry, spec, user_input: dict[str, Any]) -> ConfigFlowResult:
        data = dict(entry.data)
        data[CONF_REQUEST] = spec.as_dict()
        data[CONF_COOKIE] = str(user_input.get(CONF_COOKIE) or "")
        self.hass.config_entries.async_update_entry(entry, data=data)
        return self.async_abort(reason="reauth_successful")

    async def _async_reauth_babel(
        self, entry: ConfigEntry, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(CONF_TOKEN) or "").strip()
            if len(token) < 20:
                errors[CONF_TOKEN] = "invalid_token"
            else:
                api = BabelGasApi(
                    async_get_clientsession(self.hass),
                    token,
                    userno=str(entry.data.get(CONF_ACCOUNT) or ""),
                )
                try:
                    await api.async_fetch()
                except WuhanGasError as error:
                    errors["base"] = error.code
                else:
                    data = dict(entry.data)
                    data[CONF_TOKEN] = api.token
                    self.hass.config_entries.async_update_entry(entry, data=data)
                    return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="babel",
            data_schema=_babel_schema(
                {
                    CONF_TOKEN: entry.data.get(CONF_TOKEN, ""),
                    CONF_REFRESH_MINUTES: entry.options.get(CONF_REFRESH_MINUTES, 360),
                }
            ),
            errors=errors,
            description_placeholders={"account": entry.data.get(CONF_ACCOUNT, "")},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WuhanGasOptionsFlow()


class WuhanGasOptionsFlow(OptionsFlow):
    """改请求、换 Cookie、手工指定字段路径。"""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        # 注意：OptionsFlow 只能展示 step_id="init"（或本类里真实存在的方法名），
        # 之前这里 show_form(step_id="babel") 会直接报
        # 「Handler WuhanGasOptionsFlow doesn't support step babel」。
        if str(entry.data.get(CONF_SOURCE) or "") == SOURCE_BABEL:
            return await self._async_init_babel(entry, user_input)
        errors: dict[str, str] = {}
        if user_input is not None:
            options = dict(entry.options)
            options[CONF_REFRESH_MINUTES] = int(user_input.get(CONF_REFRESH_MINUTES) or 360)
            options[CONF_FIELDS] = {
                key: str(user_input.get(f"path_{key}") or "").strip()
                for key in FIELD_KEYS
                if str(user_input.get(f"path_{key}") or "").strip()
            }

            request_text = str(user_input.get(CONF_REQUEST) or "").strip()
            cookie = str(user_input.get(CONF_COOKIE) or "").strip()
            data = dict(entry.data)
            if request_text:
                spec = parse_request(request_text, cookie)
                if not spec.url:
                    errors[CONF_REQUEST] = "invalid_request"
                else:
                    data[CONF_REQUEST] = spec.as_dict()
            data[CONF_COOKIE] = cookie

            if not errors:
                entry_data = dict(data)
                self.hass.config_entries.async_update_entry(entry, data=entry_data)
                return self.async_create_entry(title="", data=options)

        data = dict(entry.data)
        schema = _options_schema(entry.options, data)
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    async def _async_init_babel(
        self, entry: ConfigEntry, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """小程序模式的选项：只换 token 和刷新间隔。"""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(CONF_TOKEN) or "").strip()
            if token and len(token) < 20:
                errors[CONF_TOKEN] = "invalid_token"
            else:
                data = dict(entry.data)
                if token:
                    data[CONF_TOKEN] = token
                self.hass.config_entries.async_update_entry(entry, data=data)
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_REFRESH_MINUTES: int(
                            user_input.get(CONF_REFRESH_MINUTES) or 360
                        )
                    },
                )
        return self.async_show_form(
            step_id="init",
            data_schema=_babel_schema(
                {
                    CONF_TOKEN: entry.data.get(CONF_TOKEN, ""),
                    CONF_REFRESH_MINUTES: entry.options.get(CONF_REFRESH_MINUTES, 360),
                }
            ),
            errors=errors,
            description_placeholders={"account": entry.data.get(CONF_ACCOUNT, "")},
        )
