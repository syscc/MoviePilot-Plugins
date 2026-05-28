"""
聚影签到插件
版本: 1.0.0
作者: syscc
功能:
- 自动访问聚影每日签到页并点击“立即签到”
- 支持账号密码自动登录、Cookie 登录态、定时任务、失败重试、通知和历史记录
"""

import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

try:
    from .playwright_helper import JuyingPlaywrightClient

    BROWSER_READY = True
    IMPORT_ERROR = ""
except Exception as e:
    JuyingPlaywrightClient = None
    BROWSER_READY = False
    IMPORT_ERROR = str(e)


class JuyingSign(_PluginBase):
    plugin_name = "聚影签到"
    plugin_desc = "自动完成聚影每日签到，支持账号密码登录、失败重试和历史记录"
    plugin_icon = "https://raw.githubusercontent.com/syscc/MoviePilot-Plugins/main/icons/juyingsign.png"
    plugin_version = "1.0.0"
    plugin_author = "syscc"
    author_url = ""
    plugin_config_prefix = "juyingsign_"
    plugin_order = 1
    auth_level = 2

    _enabled = False
    _cookie = ""
    _storage_state = ""
    _username = ""
    _password = ""
    _notify = True
    _onlyonce = False
    _cron = "0 8 * * *"
    _base_url = "https://share.huamucang.top"
    _max_retries = 3
    _retry_interval = 30
    _history_days = 30
    _manual_trigger = False
    _scheduler: Optional[BackgroundScheduler] = None
    _current_trigger_type = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        logger.info("============= juyingsign 初始化 =============")

        try:
            if config:
                self._enabled = config.get("enabled", False)
                self._cookie = config.get("cookie") or ""
                self._storage_state = config.get("storage_state") or ""
                self._username = (config.get("username") or "").strip()
                self._password = (config.get("password") or "").strip()
                self._notify = config.get("notify", True)
                self._onlyonce = config.get("onlyonce", False)
                self._cron = config.get("cron") or "0 8 * * *"
                self._base_url = (config.get("base_url") or self._base_url).rstrip("/")
                self._max_retries = max(0, int(config.get("max_retries", 3)))
                self._retry_interval = max(1, int(config.get("retry_interval", 30)))
                self._history_days = max(1, int(config.get("history_days", 30)))
                logger.info(
                    f"聚影签到插件已加载，enabled={self._enabled}, "
                    f"notify={self._notify}, cron={self._cron}, base_url={self._base_url}"
                )

            if self._onlyonce:
                logger.info("执行一次性聚影签到")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
                self._scheduler.add_job(
                    func=self.sign,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="聚影签到",
                )
                self._onlyonce = False
                self.update_config(self._build_config(onlyonce=False))

                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()
        except Exception as e:
            logger.error(f"juyingsign 初始化错误: {e}", exc_info=True)

    def sign(self, retry_count: int = 0):
        start_time = datetime.now()
        self._current_trigger_type = "手动触发" if self._is_manual_trigger() else "定时触发"
        logger.info(f"开始聚影签到，retry={retry_count}, trigger={self._current_trigger_type}")

        try:
            if not self._is_manual_trigger() and self._is_already_signed_today():
                sign_dict = {
                    "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "跳过: 今日已签到",
                    "message": self._get_last_success_message(),
                }
                if self._notify:
                    self._send_sign_notification(sign_dict)
                return sign_dict

            if not self._cookie:
                login_ok, login_message = self._auto_login()
                if not login_ok:
                    sign_dict = {
                        "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "签到失败: 未配置 Cookie",
                        "message": f"请填写聚影登录 Cookie，或配置用户名密码自动登录获取 Cookie。{login_message}",
                    }
                    self._save_sign_history(sign_dict)
                    if self._notify:
                        self._send_sign_notification(sign_dict)
                    return sign_dict

            success, message = self._signin_base()
            if success:
                sign_status = "已签到" if self._is_already_signed_message(message) else "签到成功"
                sign_dict = self._build_success_record(sign_status, message)
                self._save_sign_history(sign_dict)
                self._send_sign_notification(sign_dict)
                return sign_dict

            logger.error(f"聚影签到失败: {message}")
            if self._is_auth_error(message):
                login_ok, login_message = self._auto_login()
                if login_ok:
                    logger.info("聚影登录态失效，已通过账号密码刷新 Cookie，重新执行签到")
                    success, message = self._signin_base()
                    if success:
                        sign_status = "已签到" if self._is_already_signed_message(message) else "签到成功"
                        sign_dict = self._build_success_record(sign_status, message)
                        self._save_sign_history(sign_dict)
                        self._send_sign_notification(sign_dict)
                        return sign_dict
                else:
                    message = f"{message}；自动登录失败: {login_message}"

            if retry_count < self._max_retries:
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【聚影签到重试】",
                        text=f"签到失败: {message}，{self._retry_interval} 秒后进行第 {retry_count + 1} 次重试",
                    )
                time.sleep(self._retry_interval)
                return self.sign(retry_count + 1)

            sign_dict = {
                "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
                "status": f"签到失败: {message}",
                "message": message,
            }
            self._save_sign_history(sign_dict)
            self._send_sign_notification(sign_dict)
            return sign_dict
        except Exception as e:
            logger.error(f"聚影签到异常: {e}", exc_info=True)
            if (datetime.now() - start_time).total_seconds() > 300:
                message = "执行超时"
            else:
                message = str(e)
            sign_dict = {
                "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
                "status": f"签到失败: {message}",
                "message": message,
            }
            self._save_sign_history(sign_dict)
            self._send_sign_notification(sign_dict)
            return sign_dict
        finally:
            self._manual_trigger = False

    def _signin_base(self) -> Tuple[bool, str]:
        if not self._cookie:
            return False, "未配置 Cookie"
        if JuyingPlaywrightClient is None:
            return False, f"浏览器依赖加载失败，请确认插件依赖已安装。错误信息: {IMPORT_ERROR}"

        client = JuyingPlaywrightClient(base_url=self._base_url, headless=True)
        return client.checkin(cookie_str=self._cookie, storage_state=self._storage_state)

    def _auto_login(self) -> Tuple[bool, str]:
        if not self._username or not self._password:
            return False, "未配置用户名或密码"
        if JuyingPlaywrightClient is None:
            return False, f"浏览器依赖加载失败，请确认插件依赖已安装。错误信息: {IMPORT_ERROR}"

        client = JuyingPlaywrightClient(base_url=self._base_url, headless=True)
        success, cookie_str, storage_state, message = client.login(username=self._username, password=self._password)
        if not success:
            return False, message

        self._cookie = cookie_str
        self._storage_state = storage_state
        self.update_config(self._build_config())
        return True, message

    def _build_success_record(self, status: str, message: str) -> Dict[str, Any]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        last_date_str = self.get_data("last_success_date")
        consecutive_days = int(self.get_data("consecutive_days") or 0)

        if last_date_str == today_str:
            pass
        elif last_date_str == (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"):
            consecutive_days += 1
        else:
            consecutive_days = 1

        self.save_data("consecutive_days", consecutive_days)
        self.save_data("last_success_date", today_str)

        return {
            "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "message": message or "签到完成",
            "points": self._extract_points(message),
            "days": consecutive_days,
        }

    def _save_sign_history(self, sign_data: Dict[str, Any]):
        try:
            history = self.get_data("sign_history") or []
            if "date" not in sign_data:
                sign_data["date"] = datetime.today().strftime("%Y-%m-%d %H:%M:%S")
            history.append(sign_data)

            now = datetime.now()
            valid_history = []
            for record in history:
                try:
                    record_date = datetime.strptime(record["date"], "%Y-%m-%d %H:%M:%S")
                    if (now - record_date).days < self._history_days:
                        valid_history.append(record)
                except (ValueError, KeyError):
                    record["date"] = datetime.today().strftime("%Y-%m-%d %H:%M:%S")
                    valid_history.append(record)

            self.save_data(key="sign_history", value=valid_history)
            logger.info(f"保存聚影签到历史，当前共有 {len(valid_history)} 条记录")
        except Exception as e:
            logger.error(f"保存聚影签到历史失败: {e}", exc_info=True)

    def _send_sign_notification(self, sign_dict: Dict[str, Any]):
        if not self._notify:
            return

        status = sign_dict.get("status", "未知")
        message = sign_dict.get("message", "—")
        points = sign_dict.get("points", "—")
        days = sign_dict.get("days", self.get_data("consecutive_days") or "—")
        sign_time = sign_dict.get("date", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        trigger_type = self._current_trigger_type or "未知"

        if "成功" in status:
            title = "【聚影签到成功】"
        elif "已签到" in status or "跳过" in status:
            title = "【聚影重复签到】"
        else:
            title = "【聚影签到失败】"

        text = (
            f"执行结果\n"
            f"时间：{sign_time}\n"
            f"方式：{trigger_type}\n"
            f"状态：{status}\n"
            f"详情：{message}\n"
            f"奖励积分：{points}\n"
            f"连续天数：{days}"
        )

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=title,
            text=text,
        )

    def get_state(self) -> bool:
        logger.info(f"juyingsign 状态: {self._enabled}")
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            logger.info(f"注册聚影签到定时服务: {self._cron}")
            return [{
                "id": "juyingsign",
                "name": "聚影签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign,
                "kwargs": {},
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "开启通知"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "cookie",
                                    "label": "站点 Cookie",
                                    "placeholder": "请输入聚影已登录账号的完整 Cookie",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "username",
                                        "label": "用户名/邮箱",
                                        "placeholder": "用于自动登录获取 Cookie",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "password",
                                        "label": "密码",
                                        "type": "password",
                                        "placeholder": "用于自动登录获取 Cookie",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "base_url",
                                    "label": "站点地址",
                                    "placeholder": "https://share.huamucang.top",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VCronField",
                                    "props": {"model": "cron", "label": "签到周期"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_retries",
                                        "label": "最大重试次数",
                                        "type": "number",
                                        "placeholder": "3",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "retry_interval",
                                        "label": "重试间隔(秒)",
                                        "type": "number",
                                        "placeholder": "30",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "history_days",
                                        "label": "历史保留天数",
                                        "type": "number",
                                        "placeholder": "30",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "使用说明：推荐填写用户名和密码，插件会打开聚影登录页自动登录并保存 Cookie；也可以手动登录聚影后复制完整 Cookie。插件会打开 /checkin 页面并点击“立即签到”。",
                                },
                            }],
                        }],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cookie": "",
            "username": "",
            "password": "",
            "base_url": "https://share.huamucang.top",
            "cron": "0 8 * * *",
            "max_retries": 3,
            "retry_interval": 30,
            "history_days": 30,
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data("sign_history") or []
        consecutive_days = self.get_data("consecutive_days") or 0

        if not historys:
            return [{
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": f"暂无签到记录。当前插件统计连续签到天数：{consecutive_days}",
                    "class": "mb-2",
                },
            }]

        historys = sorted(historys, key=lambda item: item.get("date", ""), reverse=True)
        history_rows = []
        for history in historys:
            status = str(history.get("status", "未知"))
            message = str(history.get("message", "—"))
            if "成功" in status or "已签到" in status:
                status_color = "success"
            elif "失败" in status:
                status_color = "error"
            else:
                status_color = "info"

            history_rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "text-caption"}, "text": history.get("date", "")},
                    {
                        "component": "td",
                        "props": {"title": status, "style": "width: 180px; max-width: 180px; overflow: hidden;"},
                        "content": [{
                            "component": "VChip",
                            "props": {
                                "color": status_color,
                                "size": "small",
                                "variant": "outlined",
                                "class": "text-truncate",
                                "style": "max-width: 100%;",
                            },
                            "text": status,
                        }],
                    },
                    {
                        "component": "td",
                        "props": {"title": message, "style": "max-width: 420px; overflow: hidden;"},
                        "content": [{
                            "component": "span",
                            "props": {
                                "class": "text-truncate d-inline-block",
                                "style": "max-width: 400px; vertical-align: middle;",
                                "title": message,
                            },
                            "text": message,
                        }],
                    },
                    {"component": "td", "text": str(history.get("points", "—"))},
                    {"component": "td", "text": str(history.get("days", "—"))},
                ],
            })

        return [{
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-4"},
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {"class": "text-h6"},
                    "text": f"聚影签到历史（连续 {consecutive_days} 天）",
                },
                {
                    "component": "VCardText",
                    "content": [{
                        "component": "VTable",
                        "props": {"hover": True, "density": "compact"},
                        "content": [
                            {
                                "component": "thead",
                                "content": [{
                                    "component": "tr",
                                    "content": [
                                        {"component": "th", "text": "时间"},
                                        {"component": "th", "props": {"style": "width: 180px; max-width: 180px;"}, "text": "状态"},
                                        {"component": "th", "props": {"style": "max-width: 420px;"}, "text": "详情"},
                                        {"component": "th", "text": "奖励积分"},
                                        {"component": "th", "text": "连续天数"},
                                    ],
                                }],
                            },
                            {"component": "tbody", "content": history_rows},
                        ],
                    }],
                },
            ],
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止聚影签到服务失败: {e}")

    def _build_config(self, onlyonce: Optional[bool] = None) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce if onlyonce is None else onlyonce,
            "cookie": self._cookie,
            "storage_state": self._storage_state,
            "username": self._username,
            "password": self._password,
            "base_url": self._base_url,
            "cron": self._cron,
            "max_retries": self._max_retries,
            "retry_interval": self._retry_interval,
            "history_days": self._history_days,
        }

    def _is_manual_trigger(self) -> bool:
        return getattr(self, "_manual_trigger", False)

    def _is_already_signed_today(self) -> bool:
        history = self.get_data("sign_history") or []
        today = datetime.now().strftime("%Y-%m-%d")
        return any(
            record.get("date", "").startswith(today)
            and record.get("status") in ["签到成功", "已签到"]
            for record in history
        )

    @staticmethod
    def _is_already_signed_message(message: str) -> bool:
        return any(keyword in (message or "") for keyword in ("已签到", "已经签到", "今日已签", "明天再来"))

    @staticmethod
    def _is_auth_error(message: str) -> bool:
        return any(
            keyword in (message or "")
            for keyword in ("Cookie", "登录", "过期", "失效", "未授权", "Unauthorized", "/login")
        )

    @staticmethod
    def _extract_points(message: str) -> Any:
        if not message:
            return "—"
        match = re.search(r"(?:获得|奖励|积分)[^\d]*(\d+)", message)
        return int(match.group(1)) if match else "—"

    def _get_last_success_message(self) -> str:
        history = self.get_data("sign_history") or []
        today = datetime.now().strftime("%Y-%m-%d")
        success = [
            record
            for record in history
            if record.get("date", "").startswith(today)
            and record.get("status") in ["签到成功", "已签到"]
        ]
        if not success:
            return "今日已完成签到"
        latest = max(success, key=lambda item: item.get("date", ""))
        return latest.get("message") or "今日已完成签到"
