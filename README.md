# 武汉燃气费 · Home Assistant 集成

把「武汉天然气」的燃气账单接进 Home Assistant：余额、欠费、本期气费、用气量、抄表日期、表底数、本年度累计，全部变实体，可以直接做提醒自动化。

数据来自两个地方，按你自己方便选一个：

| 方式 | 需要什么 | 拿到的数据 | 维护成本 |
| --- | --- | --- | --- |
| **小程序接口**（推荐） | 微信小程序「武汉燃气」请求头里的 `token` | 账期账单：本期气费/用气量/起止码/抄表日期/年度累计 | token 失效时重抓一次，可配合抓包容器全自动 |
| 抓包接口 | 微营业厅「账单查询」那条请求（cURL 或 URL+请求头） | 余额/欠费/用气量等，字段自动识别 | 登录态过期需重粘请求 |
| 手动录入 | 什么都不用 | 你自己填的数字 | 手动 |

> 微营业厅整站挂在微信 OAuth2 后面，没有 `code` 连登录页都进不去，HA 不可能替你走微信授权。所以这个集成不去猜接口、也不存你的微信账号 —— 它只重放你亲手给出的一条凭据。

## 实体

小程序模式下（`source: babel`）：

| 实体 | 说明 |
| --- | --- |
| `sensor.wuhan_gas_bill` | 最近一期气费（元） |
| `sensor.wuhan_gas_usage` | 最近一期用气量（m³） |
| `sensor.wuhan_gas_meter` | 最新表底数（m³） |
| `sensor.wuhan_gas_prev_reading` | 上期表底数（m³） |
| `sensor.wuhan_gas_read_date` | 最近抄表日期 |
| `sensor.wuhan_gas_period` | 最近账期（如 `2026-10`） |
| `sensor.wuhan_gas_year_fee` | 本年累计气费（元） |
| `sensor.wuhan_gas_year_usage` | 本年累计用气量（m³） |

抓包/手动模式下额外有 `sensor.wuhan_gas_balance`（余额）、`sensor.wuhan_gas_arrears`（欠费）、`sensor.wuhan_gas_price`（气价），以及两个开关 `binary_sensor.wuhan_gas_unpaid`、`binary_sensor.wuhan_gas_low_balance`（余额 < 20 元）。

多个户号时实体 id 会带户号，形如 `sensor.wuhan_gas_1234567890_bill`。

## 安装

### HACS（推荐）

1. HACS → 右上角 ⋮ → **自定义存储库**
2. 仓库填 `https://github.com/ley108/ha-wuhan-gas`，类别选 **集成**
3. 搜索「武汉燃气费」安装，重启 Home Assistant
4. 设置 → 设备与服务 → 添加集成 → 搜「武汉燃气费」

### 手动

把 `custom_components/wuhan_gas` 整个目录拷到 HA 配置目录下的 `custom_components/`，重启 HA。

```
<HA 配置目录>/
└── custom_components/
    └── wuhan_gas/
        ├── __init__.py
        ├── binary_sensor.py
        ├── config_flow.py
        ├── const.py
        ├── coordinator.py
        ├── manifest.json
        ├── parser.py
        ├── sensor.py
        ├── services.yaml
        ├── strings.json
        └── translations/
```

## 配置

### 方式一：小程序 token（推荐）

1. 微信里打开小程序「武汉燃气」，进入「账期账单」页面
2. 用抓包工具（Stream / Charles / mitmproxy 都行）找到任意一条 `wp.babel-group.cn/user/...` 请求
3. 复制请求头里 `token:` 后面那一整串（约 60~80 位，可能带 `/` 和 `==` 结尾）
4. HA 里添加集成 → 数据来源选「小程序接口」→ 填户号 → 粘贴 token → 提交

集成会先用这个 token 拉一次账单，能拉到才允许保存，所以粘错了当场就会报错，不会建出一个空壳。

> 嫌每次手动抓麻烦 → 见下面的 [自动抓 token](#可选自动抓-tokendocker)，配好之后 token 换新会自动写进 HA。

### 方式二：抓包接口

微营业厅 → 账单查询，抓那条查询请求，「复制为 cURL」整段粘进来。也支持两种简写：

- 第一行接口地址，下面几行请求头
- 只填 Cookie

集成会按原样重放，再用关键词把余额、欠费、用气量这些字段认出来。每个识别结果都带 `path`，认错了可以在集成选项里手工指定路径（如 `data.account.balance`）。

### 方式三：手动录入

先建一个空账户，然后用服务 `wuhan_gas.set_reading` 写数字，以后抓到包再在选项里切到自动模式，实体和自动化都不用改。

## 服务

| 服务 | 用途 |
| --- | --- |
| `wuhan_gas.refresh` | 立刻拉一次，不等下个刷新周期 |
| `wuhan_gas.set_reading` | 手动写余额/欠费/用气量/气费/气价/表底数/抄表日期 |
| `wuhan_gas.dump_response` | 把最近一次接口返回和识别到的字段路径导出到 `/config/www/wuhan_gas_dump.json`，排字段用 |
| `wuhan_gas.set_token` | 写入 token（自动抓包容器调的就是它），token 变了会自动重载集成 |

`set_token` 支持 `?return_response`，抓包容器靠它判断有没有写成功：

```bash
curl -X POST http://<HA>:8123/api/services/wuhan_gas/set_token?return_response \
  -H "Authorization: Bearer <长期访问令牌>" \
  -H "Content-Type: application/json" \
  -d '{"token":"<40 位以上的 token>","userno":"1234567890"}'
```

## 可选：自动抓 token（Docker）

token 只在微信登录小程序那一刻出现在请求头里，服务端不提供「用户号换 token」的接口 —— 所以只能用一次中间人代理，把登录瞬间的 token 抄下来自动写回 HA。配好之后你只要偶尔打开一次小程序，其余全自动。

### 目录

```
capture/
├── docker-compose.yml
├── .env.example        # 复制成 .env 再改
└── addon/
    └── token_hook.py   # mitmproxy 插件：抄 token + 调 HA 服务
```

### 启动

```bash
cd capture
cp .env.example .env
vi .env                      # 填 HA_URL 和 HA_TOKEN

docker compose up -d
docker compose logs -f       # 看到「插件已加载」就对了
```

`.env` 三行：

```ini
# HA 在容器里且在同一个 Docker 网络：http://homeassistant:8123
# HA 在宿主机上：http://host.docker.internal:8123
# HA 有固定 IP：http://192.168.1.10:8123
HA_URL=http://host.docker.internal:8123
HA_TOKEN=<HA 长期访问令牌>
CAP_HOSTS=wp.babel-group.cn,babel-group.cn
```

长期访问令牌在 HA 网页左下角点头像 → 安全 → 长期访问令牌 → 创建。它只用来调 `wuhan_gas.set_token`，别提交到 git。

### 装根证书

mitmproxy 要解密 HTTPS，设备得信任它的根证书：

```bash
docker cp gas-token-capture:/data/conf/mitmproxy-ca-cert.cer ./mitmproxy-ca-cert.cer
```

- **Windows / macOS**：双击导入到「受信任的根证书颁发机构」
- **iOS**：先在 Safari 下载，设置 → 通用 → VPN与设备管理 → 装描述文件；再到 通用 → 关于本机 → 证书信任设置 → 打开完全信任
- **Android**：设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书（7.0+ 需要系统级证书或 root，见下方 FAQ）
- 顺手拷一份到 HA 的 `config/www/` 也行，手机直接访问 `http://<HA>:8123/local/mitmproxy-ca-cert.cer` 下载

### 让流量走代理

把设备的 **HTTP 代理** 指向 `宿主IP:8080`（iOS/Windows 支持 Wi-Fi 代理；Android 需 root 或用 Postern/VPN 类工具，见 FAQ）。

然后**在微信里打开小程序「武汉燃气」点一次「账单查询」**。容器日志会出现：

```
抓到新 token（AbCdEfGh…，户号 123****890），已写入 HA，更新 1 个条目
```

HA 那边集成会自动重载，实体立刻恢复。同一个 token 只上报一次（存在 `capture/data/last-token.json`），不会反复刷接口。

`http://宿主IP:8081` 是 mitmweb 面板，可以实时看抓到的请求，排查时很有用 —— 但它是明文界面，看完记得把系统代理关掉，或者只在内网用。

### 抓完之后

**把设备的系统代理关掉**。抓包容器不用一直开着，`docker compose stop` 即可；HA 里的 token 已经存下来了，正常刷新不需要它。

## 自动化示例

余额不足提醒（抓包/手动模式）：

```yaml
alias: 燃气余额不足提醒
trigger:
  - platform: numeric_state
    entity_id: sensor.wuhan_gas_balance
    below: 30
action:
  - service: notify.mobile_app_your_phone
    data:
      title: 燃气余额不足
      message: >-
        当前余额 {{ states('sensor.wuhan_gas_balance') }} 元，
        建议尽快充值。
mode: single
```

新账单出炉提醒（小程序模式，账期变化即触发）：

```yaml
alias: 燃气新账单
trigger:
  - platform: state
    entity_id: sensor.wuhan_gas_period
action:
  - service: notify.mobile_app_your_phone
    data:
      title: 新一期燃气账单
      message: >-
        {{ states('sensor.wuhan_gas_period') }}：
        用气 {{ states('sensor.wuhan_gas_usage') }} m³，
        气费 {{ states('sensor.wuhan_gas_bill') }} 元。
mode: single
```

## FAQ

**token 能撑多久？**

由服务方决定，实测能撑较长时间。失效时集成会自动弹出「重新授权」，重抓一串粘进去即可；配了抓包容器的话，打开一次小程序就自动好了。

**为什么抓不到？**

- 只对 `wp.babel-group.cn` 的请求生效，别的域名不会上报
- 安卓 7.0+ 的 App 默认不信任用户证书，微信小程序可能绕过系统代理 —— 这也是目前最麻烦的一环，可行路径：用 iOS 设备抓、或在 root 过的安卓上把证书装进系统区、或走 Postern 之类的全局 VPN 代理
- 确认代理指向的是**宿主机 IP 和 8080 端口**，不是 8081

**日志报 `handshake_failure` / `SSLError`？**

`wp.babel-group.cn` 只认 `AES256-SHA256`（RSA 密钥交换），OpenSSL 3 默认 `SECLEVEL=2` 会拒绝。集成内部已经单独给这个域名降了一档 `DEFAULT@SECLEVEL=1`，容器也加了 `--set=ciphers_client=DEFAULT@SECLEVEL=1`。自己改配置时别把这两行去掉。

**字段认不出来 / 认错了？**

先用 `wuhan_gas.dump_response` 把原始返回导出来，看真实字段名，然后在集成选项里逐个填路径。

**为什么金额实体没有 `device_class: monetary`？**

HA 要求 monetary 配 ISO 货币代码，而「元」不是，前端会拿它去喂 `Intl.NumberFormat` 直接抛错。所以金额一律用普通传感器 + 单位「元」。

## 免责声明

- 本项目与武汉市燃气集团有限公司无关，纯个人自用工具，接口随时可能改版失效
- 抓包只用于获取**你自己账户**的账单数据，请勿用于任何其他用途
- token / Cookie 等同于登录凭据，泄露等于把账户交出去：不要贴到 issue、截图、聊天记录里
- 请控制刷新频率（集成默认 6 小时一次，最小 30 分钟），别给人家系统添麻烦

## License

MIT
