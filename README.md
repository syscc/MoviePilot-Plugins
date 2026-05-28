# 聚影签到插件

这是一个用于 MoviePilot 的第三方插件仓库，目前仅维护一个插件：`JuyingSign`。

插件会使用账号密码自动登录获取聚影 Cookie，或使用已填写的 Cookie 访问聚影签到页并点击“立即签到”，支持多账号串行轮询、定时执行、手动执行一次、失败重试、消息通知和签到历史展示。

## 插件列表

| 插件 | 目录 | 说明 |
| --- | --- | --- |
| 聚影签到 | `plugins/juyingsign` | 自动访问 `https://share.huamucang.top/checkin` 完成每日签到 |

## 功能

- 用户名/邮箱和密码自动登录
- Cookie 登录态签到
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
3. 找到“聚影签到”并安装。
4. 进入插件配置页面填写用户名/密码或 Cookie，并设置签到周期。

## 配置说明

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 开启后注册定时签到任务 |
| 开启通知 | 签到成功、重复签到、失败和重试时发送站内通知 |
| 立即运行一次 | 保存配置后立即执行一次签到 |
| 多账号配置 | JSON 数组。填写后优先使用多账号配置，单账号字段仅作为备用配置保留 |
| 站点 Cookie | 聚影登录后的完整 Cookie，可选 |
| 用户名/邮箱 | 用于自动登录获取 Cookie |
| 密码 | 用于自动登录获取 Cookie |
| 站点地址 | 默认 `https://share.huamucang.top` |
| 签到周期 | Cron 表达式，默认每天 08:00 |
| 最大重试次数 | 签到失败后的重试次数 |
| 失败重试间隔 | 单个账号签到失败后，等待多少分钟再重试 |
| 账号轮询间隔 | 多账号签到时，两个账号之间等待多少秒 |
| 历史保留天数 | 签到历史记录保留时间 |

## 登录方式

推荐填写“用户名/邮箱”和“密码”，插件会打开聚影登录页自动登录获取 Cookie，并在 Cookie 失效时尝试重新登录。

也可以只填写 Cookie：

1. 在浏览器中打开 `https://share.huamucang.top` 并登录。
2. 打开浏览器开发者工具。
3. 在 Network 或 Application/Storage 中复制当前站点的完整 Cookie。
4. 粘贴到插件配置的“站点 Cookie”字段。

如果没有填写用户名和密码，Cookie 失效后需要重新复制新的 Cookie。

## 多账号配置

在“多账号配置”中填写 JSON 数组即可启用多账号模式。多账号配置优先级高于单账号的用户名、密码和 Cookie 字段。

示例：

```json
[
  {
    "name": "账号1",
    "username": "你的用户名1",
    "password": "你的密码1",
    "cookie": ""
  },
  {
    "name": "账号2",
    "username": "你的用户名2",
    "password": "你的密码2",
    "cookie": ""
  }
]
```

也可以只填写 Cookie：

```json
[
  {
    "name": "账号1",
    "username": "",
    "password": "",
    "cookie": "这里填账号1完整Cookie"
  },
  {
    "name": "账号2",
    "username": "",
    "password": "",
    "cookie": "这里填账号2完整Cookie"
  }
]
```

多账号执行规则：

- 插件按 JSON 数组顺序串行轮询执行，不并行签到。
- 两个账号之间会按“账号轮询间隔”等待，默认 10 秒。
- 单个账号失败后会按“失败重试间隔”等待后重试，默认 3 分钟。
- 如果账号填写了 `username` 和 `password`，`cookie` 可以留空。自动登录成功后，插件会把获取到的 `cookie` 和 `storage_state` 回写到该账号配置中。
- 每个账号的 Cookie、登录态、签到历史、连续天数、站点用户信息和通知都会独立处理。

## 仓库结构

```text
MoviePilot-Plugins/
├── icons/
│   └── juyingsign.png
├── plugins/
│   └── juyingsign/
│       ├── __init__.py
│       ├── playwright_helper.py
│       └── requirements.txt
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
