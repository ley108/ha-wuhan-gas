"""武汉燃气费取数：抓包请求重放 + 字段识别，或读手动录入的数字。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BABEL_BASE,
    BABEL_BIND_LIST,
    BABEL_PERIOD,
    BABEL_REFERER,
    BABEL_UA,
    CONF_ACCOUNT,
    CONF_ACCOUNT_NAME,
    CONF_FIELDS,
    CONF_REFRESH_MINUTES,
    CONF_SOURCE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REQUEST_TIMEOUT,
    SOURCE_MANUAL,
)
from .parser import RequestSpec, apply_overrides, parse_payload, sniff

_LOGGER = logging.getLogger(__name__)


class WuhanGasError(Exception):
    """带错误码的业务异常，配置流程直接拿 code 当翻译键。"""

    def __init__(self, message: str, code: str = "cannot_connect") -> None:
        super().__init__(message)
        self.code = code


class GasAuthError(WuhanGasError):
    """登录态失效：被抓包的那条请求已经不被认了。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, "auth_failed")


@dataclass
class GasData:
    """一次刷新拿到的全部气费数据。"""

    account: str = ""
    name: str = ""
    address: str = ""
    balance: float | None = None
    arrears: float | None = None
    usage: float | None = None
    bill: float | None = None
    read_date: str | None = None
    price: float | None = None
    meter: float | None = None
    period: str | None = None
    prev_reading: float | None = None
    year_fee: float | None = None
    year_usage: float | None = None
    months: int = 0
    picked: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str = ""
    fetched_at: str = ""
    raw_preview: str = ""

    @property
    def has_arrears(self) -> bool:
        return (self.arrears or 0) > 0

    @property
    def is_low(self) -> bool:
        """余额低于 20 元按「快用完了」处理，够做提醒自动化。"""
        return self.balance is not None and self.balance < 20

    def as_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "name": self.name,
            "address": self.address,
            "balance": self.balance,
            "arrears": self.arrears,
            "usage": self.usage,
            "bill": self.bill,
            "read_date": self.read_date,
            "price": self.price,
            "meter": self.meter,
            "period": self.period,
            "prev_reading": self.prev_reading,
            "year_fee": self.year_fee,
            "year_usage": self.year_usage,
            "months": self.months,
            "picked": self.picked,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "raw_preview": self.raw_preview,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GasData:
        return cls(
            account=str(raw.get("account") or ""),
            name=str(raw.get("name") or ""),
            address=str(raw.get("address") or ""),
            balance=raw.get("balance"),
            arrears=raw.get("arrears"),
            usage=raw.get("usage"),
            bill=raw.get("bill"),
            read_date=raw.get("read_date"),
            price=raw.get("price"),
            meter=raw.get("meter"),
            period=raw.get("period"),
            prev_reading=raw.get("prev_reading"),
            year_fee=raw.get("year_fee"),
            year_usage=raw.get("year_usage"),
            months=int(raw.get("months") or 0),
            picked=raw.get("picked") or {},
            source=str(raw.get("source") or ""),
            fetched_at=str(raw.get("fetched_at") or ""),
            raw_preview=str(raw.get("raw_preview") or ""),
        )


_MOBILE_RE = re.compile(r"(1[3-9]\d)\d{4}(\d{4})")


def _scrub(text: str, limit: int = 3000) -> str:
    """给用户看/贴出来的原始响应：手机号打码，长度截断。"""
    return _MOBILE_RE.sub(r"\1****\2", text)[:limit]


class WuhanGasApi:
    """把抓包得到的请求原样重放。"""

    def __init__(
        self,
        session: ClientSession,
        spec: RequestSpec,
        overrides: dict[str, str] | None = None,
    ) -> None:
        self._session = session
        self.spec = spec
        self.overrides = overrides or {}

    async def _request(self) -> tuple[int, str]:
        if not self.spec.url:
            raise WuhanGasError("还没填查询地址", "invalid_request")
        try:
            async with self._session.request(
                self.spec.method,
                self.spec.url,
                headers=self.spec.headers,
                data=self.spec.data,
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
                allow_redirects=False,
            ) as response:
                return response.status, await response.text()
        except (ClientError, asyncio.TimeoutError) as error:
            raise WuhanGasError("连不上武汉燃气的查询接口，检查网络后重试") from error

    async def async_fetch(self) -> tuple[GasData, dict[str, Any]]:
        status, text = await self._request()
        body = (text or "").strip()

        if status in (301, 302, 303, 307, 308) or "open.weixin.qq.com" in body[:600]:
            raise GasAuthError(
                "登录态已经过期：微营业厅把请求转去微信授权了，重新抓一条请求粘进来"
            )
        if status in (401, 403):
            raise GasAuthError("接口拒绝了这次请求（401/403）：Cookie 或令牌可能已过期")
        if status != 200:
            raise WuhanGasError(f"查询接口返回 HTTP {status}")

        if not body:
            raise WuhanGasError("接口返回了空内容", "no_data")
        if body[:1] not in "{[":
            if "<html" in body[:400].lower():
                raise GasAuthError("接口返回的是网页而不是数据：多半是登录态过期了")
            raise WuhanGasError("接口返回的不是 JSON，检查粘贴的地址是否是数据接口", "no_data")

        try:
            payload = parse_payload(body)
        except ValueError as error:
            raise WuhanGasError("接口返回的 JSON 解析不了", "no_data") from error

        sniffed = apply_overrides(payload, sniff(payload), self.overrides)
        if not sniffed:
            raise WuhanGasError(
                "接口能通，但没在返回里认出余额/欠费/用气量，请在选项里手工指定字段路径",
                "no_field",
            )

        data = GasData(
            balance=_value(sniffed, "balance"),
            arrears=_value(sniffed, "arrears"),
            usage=_value(sniffed, "usage"),
            bill=_value(sniffed, "bill"),
            read_date=_date_value(sniffed, "read_date"),
            price=_value(sniffed, "price"),
            meter=_value(sniffed, "meter"),
            picked=sniffed,
            source="api",
            fetched_at=dt_util.utcnow().isoformat(),
            raw_preview=_scrub(json.dumps(payload, ensure_ascii=False, indent=1)),
        )
        return data, payload


def _legacy_ssl_context() -> ssl.SSLContext:
    """OpenSSL 3.5 默认 SECLEVEL=2 会拒绝 AES256-SHA256（RSA 密钥交换），
    wp.babel-group.cn 这种老 Tengine 只认这一档，握手会直接 handshake_failure。
    这里单独给小程序接口降一档，不影响 HA 其它请求。"""
    context = ssl.create_default_context()
    try:
        context.set_ciphers("DEFAULT@SECLEVEL=1")
    except ssl.SSLError:
        pass
    return context


class BabelGasApi:
    """微信小程序「武汉燃气」的云接口。

    小程序里所有数据都靠请求头里的 ``token`` 认人，一个 token 换一年账单，
    不需要 Cookie、不需要微信环境，HA 直接重放就行。
    """

    def __init__(
        self,
        session: ClientSession,
        token: str,
        userno: str = "",
        orgid: str = "1",
        meter_type: str = "1",
        name_hint: str = "",
    ) -> None:
        self._session = session
        self.token = token.strip()
        self.userno = userno.strip()
        self.orgid = orgid or "1"
        self.meter_type = meter_type or "1"
        self.name_hint = name_hint
        # 延迟到后台线程创建：create_default_context() 会同步读证书链，
        # 直接在 __init__ 里做会触发 HA 的 blocking-call 告警。
        self._ssl: ssl.SSLContext | None = None

    async def _async_ssl(self) -> ssl.SSLContext:
        if self._ssl is None:
            self._ssl = await asyncio.get_running_loop().run_in_executor(
                None, _legacy_ssl_context
            )
        return self._ssl

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "token": self.token,
            "xweb_xhr": "1",
            "Accept": "*/*",
            "User-Agent": BABEL_UA,
            "Referer": BABEL_REFERER,
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        ssl_context = await self._async_ssl()
        try:
            async with self._session.post(
                f"{BABEL_BASE}{path}",
                json=payload,
                headers=self._headers(),
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
                ssl=ssl_context,
            ) as response:
                status = response.status
                text = await response.text()
        except (ClientError, asyncio.TimeoutError) as error:
            raise WuhanGasError("连不上小程序里的燃气接口，检查 HA 的出网") from error

        if status != 200:
            raise WuhanGasError(f"小程序接口返回 HTTP {status}")

        try:
            payload_json = json.loads(text)
        except ValueError as error:
            raise GasAuthError("小程序接口返回的不是 JSON，token 多半已经失效") from error
        if not isinstance(payload_json, dict):
            raise WuhanGasError("小程序接口返回了意外的结构", "no_data")

        if str(payload_json.get("code")) != "0":
            message = str(payload_json.get("msg") or "未知错误")
            if "鉴权" in message or "登录" in message or "token" in message.lower():
                raise GasAuthError(f"token 失效了（{message}），请重新抓一次")
            raise WuhanGasError(f"小程序接口报错：{message}", "no_data")
        return payload_json

    async def async_accounts(self) -> list[dict[str, Any]]:
        """这个 token 名下绑定的燃气户号。"""
        payload = await self._post(BABEL_BIND_LIST, {})
        rows = payload.get("data") or []
        return [row for row in rows if isinstance(row, dict)]

    async def async_fetch(self) -> tuple[GasData, dict[str, Any]]:
        accounts = await self.async_accounts()
        chosen: dict[str, Any] = {}
        for row in accounts:
            if self.userno and str(row.get("userno")) == self.userno:
                chosen = row
                break
        if not chosen and accounts:
            chosen = accounts[0]
        if chosen:
            self.userno = str(chosen.get("userno") or self.userno)
            self.orgid = str(chosen.get("dataorgid") or self.orgid)
            self.meter_type = str(chosen.get("type") or self.meter_type)

        if not self.userno:
            raise WuhanGasError("这个 token 下面没有绑定燃气户号", "no_data")

        query = {"userno": self.userno, "meterType": self.meter_type, "orgid": self.orgid, "type": 1}
        payload = await self._post(BABEL_PERIOD, {"year": datetime.now().year, **query})
        periods = _periods(payload)
        if not periods:
            # 一月账还没出时，退回上一年，至少让实体有数
            payload = await self._post(BABEL_PERIOD, {"year": datetime.now().year - 1, **query})
            periods = _periods(payload)
        if not periods:
            raise WuhanGasError("接口里这一年没有账期数据", "no_data")

        periods.sort(key=lambda row: str(row.get("yrmonth") or ""), reverse=True)
        latest = periods[0]

        def _num(row: dict[str, Any], key: str) -> float | None:
            raw = row.get(key)
            if raw in (None, ""):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        year_fee = 0.0
        year_usage = 0.0
        for row in periods:
            year_fee += _num(row, "own_fee") or 0.0
            year_usage += _num(row, "amount") or 0.0

        period = str(latest.get("yrmonth") or "")
        data = GasData(
            account=self.userno,
            name=str(chosen.get("username") or self.name_hint or ""),
            address=str(chosen.get("address") or ""),
            usage=_num(latest, "amount"),
            bill=_num(latest, "own_fee"),
            meter=_num(latest, "curr_reading"),
            prev_reading=_num(latest, "last_reading"),
            read_date=_fmt_date(str(latest.get("own_date") or "")),
            period=_fmt_period(period),
            year_fee=round(year_fee, 2),
            year_usage=round(year_usage, 2),
            months=len(periods),
            source="babel",
            fetched_at=dt_util.utcnow().isoformat(),
            raw_preview=_scrub(json.dumps(payload, ensure_ascii=False, indent=1)),
        )
        return data, payload


def _periods(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("data") or []
    return [row for row in rows if isinstance(row, dict) and row.get("yrmonth")]


def _fmt_period(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 6:
        return f"{digits[:4]}-{digits[4:]}"
    return raw or None


def _fmt_date(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return raw or None


def _value(sniffed: dict[str, dict[str, Any]], semantic: str) -> float | None:
    item = sniffed.get(semantic)
    if not isinstance(item, dict):
        return None
    value = item.get("value")
    return float(value) if isinstance(value, (int, float)) else None


def _date_value(sniffed: dict[str, dict[str, Any]], semantic: str) -> str | None:
    item = sniffed.get(semantic)
    if not isinstance(item, dict):
        return None
    value = item.get("value")
    return str(value) if isinstance(value, str) else None


class WuhanGasCoordinator(DataUpdateCoordinator[GasData]):
    """定时刷新；手动模式就只读本地存的那份数字。"""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: WuhanGasApi | BabelGasApi | None,
        store: Store,
        manual: GasData | None = None,
    ) -> None:
        interval = entry.options.get(CONF_REFRESH_MINUTES)
        update_interval = (
            DEFAULT_SCAN_INTERVAL if not interval else _minutes(interval)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=update_interval,
            config_entry=entry,
        )
        self.api = api
        self.store = store
        self.manual = manual
        self.entry = entry

    @property
    def is_manual(self) -> bool:
        return str(self.entry.data.get(CONF_SOURCE) or "") == SOURCE_MANUAL

    async def _async_update_data(self) -> GasData:
        if self.is_manual or self.api is None:
            if self.manual is None:
                self.manual = GasData(
                    account=str(self.entry.data.get(CONF_ACCOUNT) or ""),
                    name=str(self.entry.data.get(CONF_ACCOUNT_NAME) or ""),
                    source="manual",
                )
            return self.manual

        try:
            data, _payload = await self.api.async_fetch()
        except GasAuthError as error:
            self.entry.async_start_reauth(self.hass)
            raise UpdateFailed(str(error)) from error
        except WuhanGasError as error:
            raise UpdateFailed(str(error)) from error

        data.account = str(self.entry.data.get(CONF_ACCOUNT) or "") or data.account
        data.name = str(self.entry.data.get(CONF_ACCOUNT_NAME) or "") or data.name
        return data

    async def async_set_manual(self, values: dict[str, Any]) -> None:
        """手动录入：只覆盖传进来的字段，其余保留。"""
        base = self.manual or GasData(
            account=str(self.entry.data.get(CONF_ACCOUNT) or ""),
            name=str(self.entry.data.get(CONF_ACCOUNT_NAME) or ""),
            source="manual",
        )
        for key in ("balance", "arrears", "usage", "bill", "price", "meter"):
            if values.get(key) is not None:
                setattr(base, key, float(values[key]))
        if values.get("read_date"):
            base.read_date = str(values["read_date"])
        base.source = "manual"
        base.fetched_at = dt_util.utcnow().isoformat()
        self.manual = base

        if self.is_manual or self.api is None:
            await self.store.async_save(base.as_dict())
            self.async_set_updated_data(base)


def _minutes(value: Any) -> timedelta:
    try:
        return timedelta(minutes=max(30, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_SCAN_INTERVAL


def build_overrides(entry: ConfigEntry) -> dict[str, str]:
    raw = entry.options.get(CONF_FIELDS) or {}
    return {str(k): str(v) for k, v in raw.items() if v}
