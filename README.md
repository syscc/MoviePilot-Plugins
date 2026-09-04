# MoviePilot 第三方插件

这是一个用于 MoviePilot 的第三方插件仓库，目前维护 `AggregateSign` 和 `MessageNotify`。

其中聚合签到插件使用 JSON 多账号配置统一管理多个站点，当前支持聚影、癫影和影巢。三个站点均支持账号密码自动登录或 Cookie 登录态。

## 插件列表

| 插件 | 目录 | 说明 |
| --- | --- | --- |
| 聚合签到 | `plugins/aggregatesign` | 聚合多个站点的每日签到，支持多账号、多站点和多签到方式 |
| 消息通知 | `plugins/messagenotify` | 聚合多个自定义消息通知渠道 |

## 功能

- 聚影账号密码自动登录
- Cookie 登录态签到
- 聚影、癫影、影巢多站点签到
- 癫影普通签到和运气签到方式配置
- 多账号按配置顺序串行轮询签到
- 多账号独立保存 Cookie、登录态、历史和通知
- 定时任务签到
- 手动立即运行一次
- 签到失败自动重试
- MoviePilot 站内消息通知
- 签到历史记录展示
- 连续签到天数统计

## 安装

1. 在 MoviePilot 插件市场中添加本仓库地址。
2. 同步插件市场。
3. 找到需要的插件并安装。
4. 进入插件配置页面填写配置并保存。

## 配置说明

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 开启后注册定时签到任务 |
| 开启通知 | 签到成功、重复签到、失败和重试时发送站内通知 |
| 立即运行一次 | 保存配置后立即执行一次签到 |
| 多账号配置 | JSON 数组。唯一账号配置入口，支持 `id`、`site`、`name`、`base_url`、`username`、`password`、`cookie`、`methods` |
| 签到周期 | Cron 表达式，默认每天 08:00 |
| 最大重试次数 | 签到失败后的重试次数 |
| 失败重试间隔 | 单个账号签到失败后，等待多少分钟再重试 |
| 账号轮询间隔 | 多账号签到时，两个账号之间等待多少秒 |
| 历史保留天数 | 签到历史记录保留时间 |

## 支持站点

| site | 站点 | 登录方式 | 签到方式 |
| --- | --- | --- | --- |
| `juying` | 国内 `https://jying.top`；国外 `https://www.jying.top` | 账号密码自动登录或 Cookie | `normal` |
| `dian115` | `https://m.dian115.com` | 账号密码自动登录或 Cookie | `normal` 普通签到、`lucky` 运气签到 |
| `hdhive` | `https://re0.me` | 账号密码自动登录（推荐）或完整浏览器登录态 | `normal` 普通签到、`gamble` 赌狗签到 |

## 登录方式

聚影、癫影和影巢都可以填写 `username` 和 `password`，插件会自动登录获取并保存登录态。聚影和癫影也可以只填写 Cookie；影巢会把安全绑定数据保存在 IndexedDB，单独复制 Cookie 到其他浏览器环境可能被站点判定为失效，因此建议使用账号密码自动登录。

复制 Cookie 的通用步骤：

1. 在浏览器中打开对应站点并登录。
2. 打开浏览器开发者工具。
3. 在 Network 或 Application/Storage 中复制当前站点的完整 Cookie。
4. 粘贴到多账号 JSON 的 `cookie` 字段。

Cookie 失效后，如果该账号配置了 `username` 和 `password`，插件会尝试自动重新登录并回写新的 Cookie。

## 多账号配置

在“多账号配置”中填写 JSON 数组即可启用多账号模式。插件只使用这个 JSON 入口，不再单独提供 Cookie、用户名、密码和站点地址输入框。

示例：

```json
[
  {
    "id": "juying-1",
    "site": "juying",
    "name": "聚影账号1",
    "base_url": "https://www.jying.top",
    "username": "你的用户名或邮箱",
    "password": "你的密码",
    "cookie": "",
    "methods": ["normal"]
  },
  {
    "id": "dian115-1",
    "site": "dian115",
    "name": "癫影账号1",
    "username": "你的邮箱",
    "password": "你的密码",
    "cookie": "",
    "methods": ["normal"]
  },
  {
    "id": "hdhive-1",
    "site": "hdhive",
    "name": "影巢账号1",
    "base_url": "https://re0.me",
    "username": "你的用户名或邮箱",
    "password": "你的密码",
    "cookie": "",
    "methods": ["normal"]
  }
]
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `id` | 推荐填写的稳定账号标识，同一站点内必须唯一；用于隔离历史、连续天数和登录态 |
| `site` | 站点标志，支持 `juying`、`dian115`、`hdhive` |
| `name` | 账号显示名，用于历史和通知 |
| `base_url` | 站点入口。聚影国内入口填 `https://jying.top`，国外入口填 `https://www.jying.top`；影巢入口填 `https://re0.me`；不填时使用对应站点默认入口 |
| `username` / `password` | 用于自动登录获取 Cookie，三个站点均可用 |
| `cookie` | 完整 Cookie，可选；留空时会尝试用账号密码自动登录 |
| `methods` | 签到方式数组。`normal` 为普通签到；癫影支持 `lucky`，影巢支持 `gamble` |

多账号执行规则：

- 插件按 JSON 数组顺序串行轮询执行，不并行签到。
- 已有签到任务运行时，重复的手动或定时触发会被跳过，避免账号登录态互相覆盖。
- 未填写 `id` 时继续兼容旧配置；同站点同名账号会按配置序号临时隔离，建议补充固定 `id`，避免调整顺序后历史归属变化。
- 两个账号之间会按“账号轮询间隔”等待，默认 10 秒。
- 单个账号失败后会按“失败重试间隔”等待后重试，默认 3 分钟。
- 账号填写 `username` 和 `password` 时，`cookie` 可以留空。自动登录成功后，插件会把获取到的 `cookie` 和 `storage_state` 回写到该账号配置中。
- 聚影每个账号可独立配置 `base_url`，国内和国外入口可以混合使用；切换入口后若原登录态失效，会按账号密码自动重新登录并更新 Cookie。
- 癫影 `methods` 设置为 `lucky` 时可能扣积分，不建议默认开启。
- 影巢 `methods` 设置为 `gamble` 时启用赌狗签到，可能产生积分风险，不建议默认开启；若数组中同时存在其他方式，影巢只执行赌狗签到一次。
- 影巢旧 `hdhive.com` 配置会自动迁移到 `https://re0.me`；旧 `storage_state` 中的影巢 Cookie 和 IndexedDB 来源也会在启动浏览器时迁移，若安全会话仍失效则用账号密码自动刷新。
- 影巢当前签到接口需要站点前端完成请求签名，因此签到和账号密码登录都依赖 MoviePilot 运行环境中的 CloakBrowser 或 Playwright；自动登录后插件会保存包含 IndexedDB 的完整浏览器状态。
- 每个账号的 Cookie、登录态、签到历史、连续天数、站点用户信息和通知都会独立处理。

## 仓库结构

```text
MoviePilot-Plugins/
├── icons/
│   ├── aggregatesign.png
│   └── messagenotify.png
├── plugins/
│   ├── aggregatesign/
│   │   ├── __init__.py
│   │   ├── playwright_helper.py
│   │   └── requirements.txt
│   └── messagenotify/
│       ├── __init__.py
│       ├── channel/
│       ├── module.py
│       ├── requirements.txt
│       └── util.py
├── package.json
└── README.md
```

## 注意事项

- 本仓库只提供插件源码与索引，不包含 MoviePilot 主程序。
- 插件依赖 MoviePilot 宿主环境运行。
- 插件使用浏览器自动化登录和访问签到页面，宿主环境需要具备 CloakBrowser 或 Playwright 相关能力。
- 测试阶段请使用源码模式运行调试，不要编译、打包或生成二进制程序。

## 许可证

本仓库仅用于 MoviePilot 第三方插件开发与自用维护。
