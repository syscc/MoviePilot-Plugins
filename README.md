# 聚影签到插件

这是一个用于 MoviePilot 的第三方插件仓库，目前仅维护一个插件：`JuyingSign`。

插件会使用账号密码自动登录或已登录账号的 Cookie 访问聚影签到页，并自动点击“立即签到”，支持定时执行、手动执行一次、失败重试、消息通知和签到历史展示。

## 插件列表

| 插件 | 目录 | 说明 |
| --- | --- | --- |
| 聚影签到 | `plugins/juyingsign` | 自动访问 `https://share.huamucang.top/checkin` 完成每日签到 |

## 功能

- 用户名/邮箱和密码自动登录
- Cookie 登录态签到
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
| 站点 Cookie | 聚影登录后的完整 Cookie，可选 |
| 用户名/邮箱 | 用于自动登录获取 Cookie |
| 密码 | 用于自动登录获取 Cookie |
| 站点地址 | 默认 `https://share.huamucang.top` |
| 签到周期 | Cron 表达式，默认每天 08:00 |
| 最大重试次数 | 签到失败后的重试次数 |
| 重试间隔 | 每次重试之间的等待秒数 |
| 历史保留天数 | 签到历史记录保留时间 |

## 登录方式

推荐填写“用户名/邮箱”和“密码”，插件会自动登录获取 Cookie，并在 Cookie 失效时尝试重新登录。

也可以只填写 Cookie：

1. 在浏览器中打开 `https://share.huamucang.top` 并登录。
2. 打开浏览器开发者工具。
3. 在 Network 或 Application/Storage 中复制当前站点的完整 Cookie。
4. 粘贴到插件配置的“站点 Cookie”字段。

如果没有填写用户名和密码，Cookie 失效后需要重新复制新的 Cookie。

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
- 插件使用浏览器自动化访问签到页面，宿主环境需要具备 CloakBrowser 或 Playwright 相关能力。
- 测试阶段请使用源码模式运行调试，不要编译、打包或生成二进制程序。

## 许可证

本仓库仅用于 MoviePilot 第三方插件开发与自用维护。
