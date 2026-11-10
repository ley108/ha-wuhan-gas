"""武汉燃气费集成常量。"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "wuhan_gas"
NAME = "武汉燃气费"

# —— 配置项 ——
CONF_ACCOUNT = "account"           # 燃气户号
CONF_ACCOUNT_NAME = "account_name"  # 户名，仅用于展示
CONF_SOURCE = "source"             # api / manual
CONF_REQUEST = "request"           # 抓包得到的 cURL（或 URL + 请求头）
CONF_COOKIE = "cookie"             # 单独指定的 Cookie，方便过期后只换这一项
CONF_ENDPOINT = "endpoint"         # 手工指定接口地址（覆盖 cURL 里的 URL）
CONF_REFRESH_MINUTES = "refresh_minutes"
CONF_FIELDS = "fields"             # 字段路径覆盖：{semantic: path}
CONF_TOKEN = "token"               # 小程序接口的 token（请求头里的那串）
CONF_ORGID = "orgid"               # 机构号，接口里叫 orgid，默认 1
CONF_METER_TYPE = "meter_type"     # 表类型，接口里叫 meterType，机械表=1

SOURCE_API = "api"
SOURCE_MANUAL = "manual"
SOURCE_BABEL = "babel"             # 微信小程序「武汉燃气」的云接口

SOURCE_LABELS = {
    SOURCE_BABEL: "小程序接口（粘贴 token，自动刷新）",
    SOURCE_API: "抓包接口（自动刷新）",
    SOURCE_MANUAL: "手动录入（先手动，抓包后再改成自动）",
}

# —— 武汉燃气 ——
GAS_PORTAL_URL = "https://wx.whng.com.cn/mobile/index.html"
GAS_OAUTH_APPID = "wxd50a207ac377c083"
GAS_ATTRIBUTION = "数据来自武汉市燃气集团有限公司「武汉天然气」微营业厅"
GAS_MANUFACTURER = "武汉市燃气集团有限公司"
GAS_MODEL = "武汉天然气 微营业厅"

# —— 微信小程序「武汉燃气」的云接口（babel）——
BABEL_BASE = "https://wp.babel-group.cn"
BABEL_BIND_LIST = "/user/get-elder-bind-list"
BABEL_PERIOD = "/user/get-period"
BABEL_REFERER = "https://servicewechat.com/wxf4b325a5170f136c/61/page-frame.html"
BABEL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) "
    "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254200d) XWEB/26073 Flue"
)

# 燃气一天最多几毛钱的变化，一天一次足够，也少给人家系统添麻烦
DEFAULT_SCAN_INTERVAL = timedelta(hours=6)
MIN_SCAN_INTERVAL = timedelta(minutes=30)
REQUEST_TIMEOUT = 25

UNIT_YUAN = "元"
UNIT_CUBIC = "m³"
UNIT_PRICE = "元/m³"
ICON_YUAN = "mdi:cash"

# 传感器语义 -> 中文名，纯展示用，也决定实体 id 后缀
SEMANTIC_LABELS = {
    "balance": "账户余额",
    "arrears": "欠费金额",
    "usage": "最近一期用气量",
    "bill": "最近一期气费",
    "read_date": "最近抄表日期",
    "price": "气价",
    "meter": "最新表底数",
    "period": "最近账期",
    "prev_reading": "上期表底数",
    "year_fee": "本年累计气费",
    "year_usage": "本年累计用气量",
}
