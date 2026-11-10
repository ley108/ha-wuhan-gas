"""mitmproxy 插件：把小程序请求头里的 token 抄下来，自动写回 HA 的武汉燃气费集成。

只在 ``wp.babel-group.cn`` 的请求上动手；token 变了才往 HA 发一次，避免刷接口。
HA 那边对应服务：``wuhan_gas.set_token``（会自动换 token 并重载集成）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from mitmproxy import ctx, http

HA_URL = os.environ.get("HA_URL", "http://homeassistant:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "").strip()
HOSTS = {
    h.strip().lower()
    for h in os.environ.get("CAP_HOSTS", "wp.babel-group.cn,babel-group.cn").split(",")
    if h.strip()
}
STATE_PATH = os.environ.get("CAP_STATE", "/data/last-token.json")
LOG_PATH = os.environ.get("CAP_LOG", "/data/capture.log")

# 同一个 token 只上报一次，重启后也能记住（存在卷里）
_last = {"token": "", "at": 0.0, "hits": 0}


def _mask(value: str) -> str:
    """户号在日志里打码，容器日志被人看到也不至于泄了完整户号。"""
    text = str(value or "")
    if len(text) <= 5:
        return text or "-"
    return f"{text[:3]}{'*' * (len(text) - 5)}{text[-2:]}"


def _log(text: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
    ctx.log.info(text)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def load(loader) -> None:
    _log("插件已加载，监听 host=%s，HA=%s" % (",".join(sorted(HOSTS)), HA_URL))
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict) and saved.get("token"):
            _last["token"] = str(saved["token"])
            _log("已载入上次抓到的 token（%s…）" % _last["token"][:8])
    except (OSError, ValueError):
        pass


def _remember(token: str) -> None:
    _last["token"] = token
    _last["at"] = time.time()
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump({"token": token, "at": _last["at"]}, handle)
    except OSError:
        pass


def _push(payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        # 带 return_response 才会拿到服务的返回值，否则 HA 只回一个 []
        HA_URL + "/api/services/wuhan_gas/set_token?return_response",
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + HA_TOKEN,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read() or b"{}"
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if isinstance(data, dict):
        merged = data.get("service_response")
        if isinstance(merged, dict):
            return merged
    return {}


def request(flow: http.HTTPFlow) -> None:
    host = (flow.request.pretty_host or "").lower()
    if not any(host == h or host.endswith("." + h) for h in HOSTS):
        return

    token = flow.request.headers.get("token") or flow.request.headers.get("Token") or ""
    token = token.strip()
    if len(token) < 20:
        return
    if _last["hits"] < 200:
        _log("看到 %s %s" % (flow.request.method, (flow.request.path or "/")[:60]))

    _last["hits"] += 1
    if token == _last["token"]:
        _log("又见到同一个 token（%s…），跳过" % token[:8])
        return

    payload: dict[str, object] = {"token": token, "origin": "capture"}
    try:
        body = json.loads(flow.request.get_text() or "{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    if isinstance(body, dict):
        if body.get("userno"):
            payload["userno"] = str(body["userno"])
        if body.get("orgid"):
            payload["orgid"] = str(body["orgid"])
        if body.get("meterType"):
            payload["meter_type"] = str(body["meterType"])

    try:
        result = _push(payload)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        _log("写入 HA 失败：%s" % error)
        return

    if result.get("updated"):
        _remember(token)
        _log(
            "抓到新 token（%s…，户号 %s），已写入 HA，更新 %s 个条目"
            % (token[:8], _mask(payload.get("userno", "")), result.get("updated"))
        )
    else:
        _log("HA 没接受这个 token：%s" % json.dumps(result, ensure_ascii=False))
