"""把用户抓到的那一条请求变成可重放的 HTTP 调用，再从陌生的 JSON 里抠出气费数字。

武汉燃气的「微营业厅」（``wx.whng.com.cn``）整站挂在微信 OAuth2 后面 —— 没有 code 连
登录页都进不去，HA 又不可能走微信授权。所以这个集成不去猜接口：用户用手机抓一次包
（可以是 cURL，也可以是「URL + 请求头」），粘贴进来，集成按原样重放，再用关键词把
余额、欠费、用气量这些字段认出来。

好处是接口改版只要重新粘一次；坏处是字段名不保证认得准，所以每个识别结果都带
``path``，界面上展示得清清楚楚，认错了可以在选项里手工指定路径。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# ---------------------------------------------------------------- 请求解析


@dataclass
class RequestSpec:
    """一条可重放的请求。headers 里的东西原样发回去，不做任何猜测。"""

    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    data: str | None = None

    @property
    def host(self) -> str:
        match = re.match(r"https?://([^/?#]+)", self.url)
        return match.group(1) if match else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "headers": self.headers,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RequestSpec:
        return cls(
            method=str(raw.get("method") or "GET").upper(),
            url=str(raw.get("url") or ""),
            headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
            data=raw.get("data"),
        )


_URL_RE = re.compile(r"https?://[^\s'\"\\]+")
_HEADER_LINE_RE = re.compile(r"^\s*([A-Za-z0-9\-_]+)\s*:\s*(.+?)\s*$")
_DATA_FLAGS = ("--data-raw", "--data-binary", "--data-urlencode", "--data", "-d")
_METHOD_FLAGS = ("-X", "--request")
_COOKIE_FLAGS = ("-b", "--cookie")


def _shell_tokens(text: str) -> list[str]:
    """够用的 shell 词法：认单/双引号，认 \\ 转义，别的当普通字符。"""
    tokens: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    started = False
    index = 0
    while index < len(text):
        char = text[index]
        if quote is None:
            if char in " \t\r\n":
                if started:
                    tokens.append("".join(buffer))
                    buffer = []
                    started = False
                index += 1
                continue
            if char in "'\"":
                quote = char
                started = True
                index += 1
                continue
            if char == "\\" and index + 1 < len(text):
                buffer.append(text[index + 1])
                started = True
                index += 2
                continue
            buffer.append(char)
            started = True
        else:
            if char == "\\" and index + 1 < len(text) and quote == '"':
                buffer.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
                index += 1
                continue
            buffer.append(char)
        index += 1
    if started:
        tokens.append("".join(buffer))
    return tokens


def _canon_header(name: str) -> str:
    """请求头名统一成 ``Content-Type`` 这种写法。

    别小看这一步：抓包工具里 ``cookie`` 是小写的，直接塞给 aiohttp 会和
    另加的 ``Cookie`` 变成两个同名头发出去，服务端只认第一个。
    """
    return "-".join(part.capitalize() for part in name.strip().split("-"))


def _split_headers(raw_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw_headers:
        # 抓包工具常把多个头塞进一个 -H，逐行拆
        for line in item.replace("\r", "\n").split("\n"):
            match = _HEADER_LINE_RE.match(line)
            if match:
                headers[_canon_header(match.group(1))] = match.group(2)
    return headers


def _clean(value: str) -> str:
    """cmd 里复制出来的 cURL 带 ^ 续行和多余换行，先收拾干净。"""
    text = value.replace("^\r\n", " ").replace("^\n", " ")
    text = text.replace("\r\n", "\n").strip().strip("'\"")
    return text


def parse_request(text: str, cookie: str = "", endpoint: str = "") -> RequestSpec:
    """把 cURL / 裸 URL + 请求头 / 纯 Cookie 统一成 RequestSpec。

    ``cookie`` 单独填的 Cookie 会和请求头里的合并（单独填的优先，方便只换 Cookie 不重粘）。
    """
    raw = _clean(text or "")
    spec = RequestSpec()
    if raw:
        if raw.lower().startswith("curl") or " -H " in raw or "--data" in raw:
            tokens = _shell_tokens(raw)
            raw_headers: list[str] = []
            index = 0
            while index < len(tokens):
                token = tokens[index]
                following = tokens[index + 1] if index + 1 < len(tokens) else ""
                if token in _METHOD_FLAGS and following:
                    spec.method = following.upper()
                    index += 2
                    continue
                if token in _DATA_FLAGS and following:
                    spec.data = following
                    if spec.method == "GET":
                        spec.method = "POST"
                    index += 2
                    continue
                if token in _COOKIE_FLAGS and following:
                    raw_headers.append(f"Cookie: {following}")
                    index += 2
                    continue
                if token == "-H" and following:
                    raw_headers.append(following)
                    index += 2
                    continue
                if not spec.url and token.lower().startswith("http"):
                    spec.url = token
                index += 1
            spec.headers.update(_split_headers(raw_headers))
            if not spec.url:
                found = _URL_RE.search(raw)
                if found:
                    spec.url = found.group(0)
        elif raw.lower().startswith("http"):
            # 「URL + 若干行请求头」的粘法
            lines = raw.split("\n")
            spec.url = lines[0].strip()
            spec.headers.update(_split_headers(lines[1:]))
        elif ":" in raw and "=" in raw:
            # 纯 Cookie 串
            spec.headers["Cookie"] = raw

    if endpoint.strip():
        spec.url = endpoint.strip()
    if cookie.strip():
        spec.headers["Cookie"] = cookie.strip()

    spec.headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50 NetType/WIFI Language/zh_CN",
    )
    spec.headers.setdefault("Accept", "application/json, text/plain, */*")
    if spec.host.startswith("wx.whng.com.cn"):
        spec.headers.setdefault("Referer", "https://wx.whng.com.cn/mobile/index.html")
    return spec


# ---------------------------------------------------------------- 字段识别

# 键名归一化后到「语义」的映射。武汉燃气的接口字段名不可知，但同类业务系统的命名
# 无非是拼音缩写、英文驼峰、中文这几种，覆盖面够用。
FIELD_KEYS: dict[str, set[str]] = {
    "balance": {
        "balance", "accountbalance", "restmoney", "rest", "surplus", "remaining",
        "ye", "yue", "zhye", "gasmoney", "money", "余额", "账户余额", "结余", "可用余额",
    },
    "arrears": {
        "arrears", "arrearage", "owe", "owed", "unpaid", "due", "dueamount",
        "qianfei", "qfje", "qf", "yqje", "欠费", "欠费金额", "应缴", "应缴金额", "应缴气费",
    },
    "usage": {
        "usage", "usegas", "gasnum", "gasusage", "gasvolume", "useamount", "volume",
        "ql", "qiliang", "bqql", "用气量", "气量", "用量", "本期气量", "本期用气量",
    },
    "bill": {
        "amount", "fee", "je", "jine", "total", "totalamount", "billamount",
        "payamount", "gasfee", "qifei", "金额", "气费", "本期气费", "账单金额", "费用", "应缴金额",
    },
    "read_date": {
        "date", "rq", "readdate", "chaobiaodate", "chaobiaorq", "billdate", "period",
        "抄表日期", "日期", "账期", "抄表时间", "出账日期",
    },
    "price": {"price", "dj", "danjia", "gasprice", "单价", "气价", "阶梯气价"},
    "meter": {
        "reading", "meter", "meterreading", "bd", "dushu", "codereading",
        "读数", "表数", "起码", "止码", "本次读数", "表底数",
    },
}


def _norm_key(key: str) -> str:
    return re.sub(r"[\s_\-]", "", str(key)).lower()


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("元", "").replace("m³", "")
        if not text or not re.fullmatch(r"-?\d+(\.\d+)?", text):
            return None
        return float(text)
    return None


_DATE_PATTERNS = (
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
)


def _as_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    for pattern in _DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _walk(payload: Any, path: str = "") -> list[tuple[str, str, Any]]:
    """把任意嵌套的 JSON 摊平成 (路径, 键名, 值) 列表。"""
    found: list[tuple[str, str, Any]] = []
    if isinstance(payload, dict):
        # 有些接口把业务数据包成 JSON 字符串再嵌套一层
        for key, value in payload.items():
            child = f"{path}.{key}" if path else str(key)
            found.append((child, str(key), value))
            if isinstance(value, (dict, list)):
                found.extend(_walk(value, child))
            elif isinstance(value, str) and value[:1] in "[{":
                try:
                    found.extend(_walk(json.loads(value), child))
                except ValueError:
                    pass
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            child = f"{path}[{index}]"
            if isinstance(item, (dict, list)):
                found.extend(_walk(item, child))
            else:
                found.append((child, "", item))
    return found


def _score_key(norm: str, parent_norm: str, keys: set[str]) -> int:
    """给一个键名和某个语义的贴合程度打分。

    精确命中 > 以关键字结尾 > 只是包含 > 靠上级路径（比如 ``{"account": {"money": 1}}``）。
    """
    if norm in keys:
        return 100
    best = 0
    for candidate in keys:
        if len(candidate) < 2:
            continue
        if norm.endswith(candidate):
            best = max(best, 80)
        elif candidate in norm:
            best = max(best, 60)
    if best:
        return best
    for candidate in keys:
        if len(candidate) >= 3 and candidate in parent_norm:
            return 30
    return 0


def sniff(payload: Any) -> dict[str, dict[str, Any]]:
    """在陌生 JSON 里认出余额/欠费/用气量等，返回 ``{语义: {value, path, key}}``。

    逐个节点打分后**全局按分数分配**，一个节点只能属于一个语义 —— 否则
    ``qfje``（欠费金额）会同时被「欠费」和「金额」抢走，账单金额就取错了。
    """
    if isinstance(payload, str):
        try:
            payload = parse_payload(payload)
        except ValueError:
            return {}
    scored: list[tuple[int, int, float, str, dict[str, Any]]] = []
    for path, key, value in _walk(payload):
        norm = _norm_key(key)
        if not norm:
            continue
        parent_norm = _norm_key(path.rsplit(".", 1)[0] if "." in path else "")
        for semantic, keys in FIELD_KEYS.items():
            score = _score_key(norm, parent_norm, keys)
            if score == 0:
                continue
            if semantic == "read_date":
                iso = _as_date(value)
                if iso is None:
                    continue
                entry = {"value": iso, "path": path, "key": key}
                weight = 0.0
            else:
                number = _to_number(value)
                if number is None:
                    continue
                entry = {"value": number, "path": path, "key": key}
                weight = abs(number)
            depth = path.count(".") + path.count("[")
            scored.append((score, depth, weight, semantic, entry))

    # 分数高的先挑；同分取层级浅的，再同分取绝对值小的
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))

    result: dict[str, dict[str, Any]] = {}
    used_paths: set[str] = set()
    for _score, _depth, _weight, semantic, entry in scored:
        if semantic in result or entry["path"] in used_paths:
            continue
        result[semantic] = entry
        used_paths.add(entry["path"])
    return result


def _get_path(payload: Any, path: str) -> Any:
    """按 ``a.b[0].c`` 取值，供用户手工指定字段时用。"""
    if not path:
        return None
    node = payload
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            index = int(part[1:-1])
            if not isinstance(node, list) or index >= len(node):
                return None
            node = node[index]
        else:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
    return node


def apply_overrides(
    payload: Any, sniffed: dict[str, dict[str, Any]], overrides: dict[str, str] | None
) -> dict[str, dict[str, Any]]:
    """用户手工指定的路径优先，其余沿用自动识别结果。"""
    result = dict(sniffed)
    for semantic, path in (overrides or {}).items():
        if not path:
            continue
        value = _get_path(payload, path)
        if semantic == "read_date":
            iso = _as_date(value)
            if iso is not None:
                result[semantic] = {"value": iso, "path": path, "key": "manual"}
        else:
            number = _to_number(value)
            if number is not None:
                result[semantic] = {"value": number, "path": path, "key": "manual"}
    return result


def parse_payload(text: str) -> Any:
    """接口有时把 JSON 再包一层字符串，这里统一剥开。"""
    data = json.loads(text)
    if isinstance(data, str):
        data = json.loads(data)
    return data
