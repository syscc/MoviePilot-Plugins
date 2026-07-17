"""
聚合签到插件
版本: 2.0
作者: syscc
功能:
- 使用多账号 JSON 配置统一管理多个站点签到
- 支持聚影、癫影、影巢自动登录和 Cookie 签到、定时任务、失败重试、通知和历史记录
"""

import json
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
    from .playwright_helper import AggregateSignClient

    BROWSER_READY = True
    IMPORT_ERROR = ""
except Exception as e:
    AggregateSignClient = None
    BROWSER_READY = False
    IMPORT_ERROR = str(e)


class AggregateSign(_PluginBase):
    plugin_name = "聚合签到"
    plugin_desc = "聚合多个站点的每日签到，支持多账号、多站点和多签到方式"
    plugin_icon = "https://raw.githubusercontent.com/syscc/MoviePilot-Plugins/main/icons/aggregatesign.png"
    plugin_version = "2.0"
    plugin_author = "syscc"
    author_url = "https://github.com/syscc/MoviePilot-Plugins"
    plugin_config_prefix = "aggregatesign_"
    plugin_order = 1
    auth_level = 2

    _enabled = False
    _cookie = ""
    _storage_state = ""
    _username = ""
    _password = ""
    _accounts_text = ""
    _accounts: List[Dict[str, Any]] = []
    _current_account: Dict[str, Any] = {}
    _legacy_account: Dict[str, str] = {}
    _current_account_key = "default"
    _current_account_name = "默认账号"
    _current_site_key = "juying"
    _current_site_name = "聚影"
    _current_methods: List[str] = ["normal"]
    _notify = True
    _onlyonce = False
    _cron = "0 8 * * *"
    _base_url = "https://share.huamucang.top"
    _max_retries = 3
    _retry_interval_minutes = 3
    _retry_interval = 180
    _account_interval = 10
    _history_days = 30
    _manual_trigger = False
    _scheduler: Optional[BackgroundScheduler] = None
    _current_trigger_type = None

    _site_defaults = {
        "juying": {
            "name": "聚影",
            "base_url": "https://share.huamucang.top",
            "methods": ["normal"],
            "auto_login": True,
            "checkin_path": "/checkin",
            "login_path": "/login",
        },
        "dian115": {
            "name": "癫影",
            "base_url": "https://m.dian115.com",
            "methods": ["normal"],
            "auto_login": True,
            "checkin_path": "/me/signin",
            "login_path": "/login",
        },
        "hdhive": {
            "name": "影巢",
            "base_url": "https://hdhive.com",
            "methods": ["normal"],
            "auto_login": True,
            "checkin_path": "/",
            "login_path": "/login",
        },
    }

    _default_accounts = [
        {
            "site": "juying",
            "name": "聚影账号1",
            "username": "你的用户名或邮箱",
            "password": "你的密码",
            "cookie": "",
            "methods": ["normal"],
        },
        {
            "site": "dian115",
            "name": "癫影账号1",
            "username": "你的邮箱",
            "password": "你的密码",
            "cookie": "",
            "methods": ["normal"],
        },
        {
            "site": "hdhive",
            "name": "影巢账号1",
            "username": "你的用户名或邮箱",
            "password": "你的密码",
            "cookie": "",
            "methods": ["normal"],
        },
    ]

    def init_plugin(self, config: dict = None):
        self.stop_service()
        logger.info("============= 聚合签到初始化 =============")

        try:
            if config:
                self._enabled = config.get("enabled", False)
                self._cookie = config.get("cookie") or ""
                self._storage_state = config.get("storage_state") or ""
                self._username = (config.get("username") or "").strip()
                self._password = (config.get("password") or "").strip()
                self._legacy_account = {
                    "cookie": self._cookie,
                    "storage_state": self._storage_state,
                    "username": self._username,
                    "password": self._password,
                }
                self._accounts_text = config.get("accounts") or ""
                self._notify = self._as_bool(config.get("notify", True))
                self._onlyonce = config.get("onlyonce", False)
                self._cron = config.get("cron") or "0 8 * * *"
                self._base_url = (config.get("base_url") or self._base_url).rstrip("/")
                self._max_retries = max(0, int(config.get("max_retries", 3)))
                self._retry_interval_minutes = self._parse_retry_interval_minutes(config)
                self._retry_interval = self._retry_interval_minutes * 60
                self._account_interval = max(0, int(config.get("account_interval", 10)))
                self._history_days = max(1, int(config.get("history_days", 30)))
                logger.info(
                    f"聚合签到插件已加载，enabled={self._enabled}, "
                    f"notify={self._notify}, cron={self._cron}, base_url={self._base_url}, "
                    f"retry_interval={self._retry_interval_minutes}分钟, "
                    f"account_interval={self._account_interval}秒"
                )

            if self._onlyonce:
                logger.info("执行一次性聚合签到")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
                self._scheduler.add_job(
                    func=self.sign,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="聚合签到",
                )
                self._onlyonce = False
                self.update_config(self._build_config(onlyonce=False))

                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()
        except Exception as e:
            logger.error(f"聚合签到初始化错误: {e}", exc_info=True)

    def sign(self, retry_count: int = 0):
        start_time = datetime.now()
        self._current_trigger_type = "手动触发" if self._is_manual_trigger() else "定时触发"
        logger.info(f"开始聚合签到，retry={retry_count}, trigger={self._current_trigger_type}")

        try:
            if retry_count == 0:
                accounts, error = self._load_accounts()
                if error:
                    sign_dict = {
                        "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "签到失败: 多账号配置错误",
                        "message": error,
                    }
                    self._send_sign_notification(sign_dict)
                    return sign_dict
                if len(accounts) > 1:
                    results = []
                    logger.info(
                        f"聚合签到按配置顺序串行轮询，共 {len(accounts)} 个账号，"
                        f"账号间隔 {self._account_interval} 秒"
                    )
                    for index, account in enumerate(accounts, start=1):
                        if index > 1 and self._account_interval > 0:
                            logger.info(f"等待 {self._account_interval} 秒后执行下一个账号")
                            time.sleep(self._account_interval)
                        self._apply_account(account)
                        results.append(self._sign_current_account(0))
                    self._restore_legacy_account()
                    result = self._build_multi_result(results)
                    self._log_sign_finished(result, start_time)
                    return result
                if accounts:
                    self._apply_account(accounts[0])

            result = self._sign_current_account(retry_count)
            self._log_sign_finished(result, start_time)
            return result
        except Exception as e:
            logger.error(f"聚合签到异常: {e}", exc_info=True)
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

    def _sign_current_account(self, retry_count: int = 0):
        logger.info(
            f"开始账号签到，site={self._current_site_name}, account={self._current_account_name}, "
            f"retry={retry_count}, trigger={self._current_trigger_type}"
        )
        try:
            if not self._cookie:
                login_ok, login_message = self._auto_login()
                if not login_ok:
                    if self._is_transient_error(login_message) and retry_count < self._max_retries:
                        logger.info(
                            f"{self._current_site_name}自动登录遇到临时网络异常，account={self._current_account_name}, "
                            f"retry={retry_count}, {self._retry_interval_minutes} 分钟后静默重试: {login_message}"
                        )
                        time.sleep(self._retry_interval)
                        return self._sign_current_account(retry_count + 1)
                    login_message = self._compact_login_message(login_message)
                    if self._username and self._password:
                        status = "签到失败: 自动登录失败"
                        message = (
                            f"{self._current_site_name} 自动登录失败: {login_message}。"
                            "可稍后重试，或手动登录后填写 Cookie。"
                        )
                    else:
                        status = "签到失败: 未配置 Cookie"
                        message = f"请填写 {self._current_site_name} 登录 Cookie，或配置可用的自动登录信息。"
                    sign_dict = {
                        "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": status,
                        "message": message,
                    }
                    self._save_sign_history(sign_dict)
                    self._send_sign_notification(sign_dict)
                    return sign_dict

            if self._is_already_signed_today():
                sign_dict = self._build_repeat_record()
                self._send_sign_notification(sign_dict)
                return sign_dict

            success, message = self._signin_base()
            if success:
                sign_status = "已签到" if self._is_already_signed_message(message) else "签到成功"
                sign_dict = self._build_success_record(sign_status, message)
                self._save_sign_history(sign_dict)
                self._send_sign_notification(sign_dict)
                return sign_dict

            if self._is_transient_error(message) and retry_count < self._max_retries:
                logger.info(
                    f"{self._current_site_name}临时网络异常，account={self._current_account_name}, "
                    f"retry={retry_count}, {self._retry_interval_minutes} 分钟后静默重试: {message}"
                )
                time.sleep(self._retry_interval)
                return self._sign_current_account(retry_count + 1)

            logger.error(f"{self._current_site_name}签到失败: {message}")
            if self._is_auth_error(message):
                login_ok, login_message = self._auto_login()
                if login_ok:
                    logger.info(f"{self._current_site_name}登录态失效，已通过账号密码刷新 Cookie，重新执行签到")
                    success, message = self._signin_base()
                    if success:
                        sign_status = "已签到" if self._is_already_signed_message(message) else "签到成功"
                        sign_dict = self._build_success_record(sign_status, message)
                        self._save_sign_history(sign_dict)
                        self._send_sign_notification(sign_dict)
                        return sign_dict
                    logger.error(
                        f"{self._current_site_name}刷新登录态后签到仍失败，"
                        f"account={self._current_account_name}: {message}"
                    )
                else:
                    message = f"{message}；自动登录失败: {login_message}"

            sign_dict = {
                "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
                "status": f"签到失败: {message}",
                "message": message,
            }
            self._save_sign_history(sign_dict)
            self._send_sign_notification(sign_dict)
            return sign_dict
        except Exception as e:
            message = str(e)
            if self._is_transient_error(message) and retry_count < self._max_retries:
                logger.info(
                    f"{self._current_site_name}账号签到遇到临时网络异常，account={self._current_account_name}, "
                    f"retry={retry_count}, {self._retry_interval_minutes} 分钟后静默重试: {message}"
                )
                time.sleep(self._retry_interval)
                return self._sign_current_account(retry_count + 1)
            logger.error(f"{self._current_site_name}账号签到异常: {message}", exc_info=True)
            sign_dict = {
                "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
                "status": f"签到失败: {message}",
                "message": message,
            }
            self._save_sign_history(sign_dict)
            self._send_sign_notification(sign_dict)
            return sign_dict

    def _signin_base(self) -> Tuple[bool, str]:
        if not self._cookie:
            return False, "未配置 Cookie"
        if AggregateSignClient is None:
            return False, f"浏览器依赖加载失败，请确认插件依赖已安装。错误信息: {IMPORT_ERROR}"

        site = self._site_defaults.get(self._current_site_key, self._site_defaults["juying"])
        client = AggregateSignClient(
            base_url=self._base_url,
            headless=True,
            site_key=self._current_site_key,
            checkin_path=site.get("checkin_path", ""),
            login_path=site.get("login_path", ""),
        )
        success, message = client.checkin(
            cookie_str=self._cookie,
            storage_state=self._storage_state,
            methods=self._current_methods,
        )
        updated_cookie, updated_storage_state = client.get_updated_login_state()
        if self._current_site_key == "hdhive" and (updated_cookie or updated_storage_state):
            if updated_cookie:
                self._cookie = updated_cookie
            if updated_storage_state:
                self._storage_state = updated_storage_state
            if self._current_account:
                self._current_account["cookie"] = self._cookie
                self._current_account["storage_state"] = self._storage_state
                self._save_current_account_config()
            self.update_config(self._build_config())
            logger.info(
                f"已更新影巢签到安全会话，account={self._current_account_name}, "
                f"cookie_len={len(self._cookie)}, storage_state_len={len(self._storage_state)}"
            )
        return success, message

    def _auto_login(self) -> Tuple[bool, str]:
        if not self._username or not self._password:
            return False, "未配置用户名或密码"
        site = self._site_defaults.get(self._current_site_key, self._site_defaults["juying"])
        if not site.get("auto_login", False):
            return False, f"{self._current_site_name} 不支持自动登录，请手动登录后在多账号配置中填写 Cookie"
        if AggregateSignClient is None:
            return False, f"浏览器依赖加载失败，请确认插件依赖已安装。错误信息: {IMPORT_ERROR}"

        client = AggregateSignClient(
            base_url=self._base_url,
            headless=True,
            site_key=self._current_site_key,
            checkin_path=site.get("checkin_path", ""),
            login_path=site.get("login_path", ""),
        )
        success, cookie_str, storage_state, message = client.login(username=self._username, password=self._password)
        if not success:
            return False, message

        self._cookie = cookie_str
        self._storage_state = storage_state
        if self._current_account:
            self._current_account["cookie"] = cookie_str
            self._current_account["storage_state"] = storage_state
            self._save_current_account_config()
        self.update_config(self._build_config())
        return True, message

    def _load_accounts(self) -> Tuple[List[Dict[str, Any]], str]:
        if self._accounts_text.strip():
            try:
                raw_accounts = json.loads(self._accounts_text)
            except Exception as e:
                return [], f"多账号配置不是有效 JSON: {e}"
            if not isinstance(raw_accounts, list):
                return [], "多账号配置必须是 JSON 数组"
            accounts = []
            for index, item in enumerate(raw_accounts, start=1):
                if not isinstance(item, dict):
                    return [], f"第 {index} 个账号配置必须是对象"
                site_key = str(item.get("site") or item.get("site_key") or "juying").strip().lower()
                if site_key not in self._site_defaults:
                    return [], f"第 {index} 个账号的 site 不支持: {site_key}"
                site = self._site_defaults[site_key]
                methods = item.get("methods")
                if not isinstance(methods, list) or not methods:
                    methods = site.get("methods") or ["normal"]
                methods = [self._normalize_method(method) for method in methods]
                account = {
                    "site": site_key,
                    "site_name": site.get("name") or site_key,
                    "index": index,
                    "base_url": str(item.get("base_url") or site.get("base_url") or "").rstrip("/"),
                    "name": str(item.get("name") or item.get("username") or f"账号{index}").strip(),
                    "username": str(item.get("username") or "").strip(),
                    "password": str(item.get("password") or "").strip(),
                    "cookie": str(item.get("cookie") or ""),
                    "storage_state": str(item.get("storage_state") or ""),
                    "methods": methods,
                }
                if not account["cookie"] and (not account["username"] or not account["password"]):
                    return [], f"第 {index} 个账号需填写 cookie，或同时填写 username/password"
                account["key"] = self._account_key(account, index)
                accounts.append(account)
            return accounts, ""

        legacy = {
            "site": "juying",
            "site_name": "聚影",
            "base_url": self._base_url,
            "name": self._username or "默认账号",
            "username": self._username,
            "password": self._password,
            "cookie": self._cookie,
            "storage_state": self._storage_state,
            "methods": ["normal"],
            "key": "default",
        }
        return [legacy], ""

    def _apply_account(self, account: Dict[str, Any]):
        self._current_account = account
        self._current_account_key = account.get("key") or "default"
        self._current_site_key = account.get("site") or "juying"
        self._current_site_name = account.get("site_name") or self._site_defaults.get(self._current_site_key, {}).get("name") or self._current_site_key
        self._current_account_name = account.get("name") or account.get("username") or self._current_account_key
        self._username = account.get("username") or ""
        self._password = account.get("password") or ""
        self._cookie = account.get("cookie") or ""
        self._storage_state = account.get("storage_state") or ""
        self._current_methods = account.get("methods") or ["normal"]
        self._base_url = (account.get("base_url") or self._site_defaults.get(self._current_site_key, {}).get("base_url") or self._base_url).rstrip("/")

    def _save_current_account_config(self):
        if not self._accounts_text.strip():
            return
        try:
            raw_accounts = json.loads(self._accounts_text)
        except Exception:
            return
        if not isinstance(raw_accounts, list):
            return
        target_index = int(self._current_account.get("index") or 0)
        if 1 <= target_index <= len(raw_accounts):
            item = raw_accounts[target_index - 1]
            if isinstance(item, dict):
                account_key = self._account_key({
                    "site": str(item.get("site") or item.get("site_key") or "juying").strip().lower(),
                    "name": str(item.get("name") or item.get("username") or f"账号{target_index}").strip(),
                    "username": str(item.get("username") or "").strip(),
                }, target_index)
                if account_key == self._current_account_key:
                    item["cookie"] = self._cookie
                    item["storage_state"] = self._storage_state
                    self._accounts_text = json.dumps(raw_accounts, ensure_ascii=False, indent=2)
                    logger.info(
                        f"已回写{self._current_site_name}登录态，account={self._current_account_name}, "
                        f"index={target_index}, cookie_len={len(self._cookie)}, storage_state_len={len(self._storage_state)}"
                    )
                    return

        for index, item in enumerate(raw_accounts, start=1):
            if not isinstance(item, dict):
                continue
            account_key = self._account_key({
                "site": str(item.get("site") or item.get("site_key") or "juying").strip().lower(),
                "name": str(item.get("name") or item.get("username") or f"账号{index}").strip(),
                "username": str(item.get("username") or "").strip(),
            }, index)
            if account_key == self._current_account_key:
                item["cookie"] = self._cookie
                item["storage_state"] = self._storage_state
                logger.info(
                    f"已回写{self._current_site_name}登录态，account={self._current_account_name}, "
                    f"index={index}, cookie_len={len(self._cookie)}, storage_state_len={len(self._storage_state)}"
                )
                break
        self._accounts_text = json.dumps(raw_accounts, ensure_ascii=False, indent=2)

    @staticmethod
    def _account_key(account: Dict[str, Any], index: int) -> str:
        site = str(account.get("site") or "juying").strip()
        raw = f"{site}_{account.get('name') or account.get('username') or f'account_{index}'}".strip()
        key = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", raw)
        return key or f"account_{index}"

    @staticmethod
    def _normalize_method(method: Any) -> str:
        value = str(method or "").strip().lower()
        if value in ("gamble", "赌狗", "赌狗签到"):
            return "gamble"
        if value in ("lucky", "luck", "运气", "运气签到"):
            return "lucky"
        return "normal"

    @staticmethod
    def _build_multi_result(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        success_count = sum(1 for item in results if "失败" not in str(item.get("status", "")))
        return {
            "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
            "status": f"多账号完成: {success_count}/{len(results)}",
            "results": results,
        }

    def _log_sign_finished(self, result: Dict[str, Any], start_time: datetime):
        elapsed = round((datetime.now() - start_time).total_seconds(), 1)
        results = result.get("results") if isinstance(result, dict) else None
        if isinstance(results, list):
            total = len(results)
            success_count = sum(1 for item in results if "失败" not in str(item.get("status", "")))
            fail_count = total - success_count
            logger.info(
                f"聚合签到完成，trigger={self._current_trigger_type}, total={total}, "
                f"success={success_count}, failed={fail_count}, elapsed={elapsed}秒"
            )
            return
        logger.info(
            f"聚合签到完成，trigger={self._current_trigger_type}, "
            f"site={result.get('site', self._current_site_name)}, "
            f"account={result.get('account', self._current_account_name)}, "
            f"status={result.get('status', '未知')}, elapsed={elapsed}秒"
        )

    def _restore_legacy_account(self):
        self._current_account = {}
        self._current_account_key = "default"
        self._current_site_key = "juying"
        self._current_site_name = "聚影"
        self._cookie = self._legacy_account.get("cookie", "")
        self._storage_state = self._legacy_account.get("storage_state", "")
        self._username = self._legacy_account.get("username", "")
        self._password = self._legacy_account.get("password", "")
        self._current_account_name = self._username or "默认账号"
        self._current_methods = ["normal"]
        self._base_url = self._site_defaults["juying"]["base_url"]

    def _data_key(self, key: str) -> str:
        if self._current_account_key == "default":
            return key
        return f"{self._current_account_key}_{key}"

    @staticmethod
    def _parse_retry_interval_minutes(config: Dict[str, Any]) -> int:
        if "retry_interval_minutes" in config:
            return max(1, int(config.get("retry_interval_minutes") or 3))
        retry_interval = int(config.get("retry_interval", 180) or 180)
        return max(1, (retry_interval + 59) // 60)

    def _all_histories(self) -> List[Dict[str, Any]]:
        histories = list(self.get_data("sign_history") or [])
        accounts, error = self._load_accounts()
        if error:
            return histories
        for account in accounts:
            account_key = account.get("key") or "default"
            if account_key == "default":
                continue
            account_history = self.get_data(f"{account_key}_sign_history") or []
            histories.extend(account_history)
        return histories

    def _build_success_record(self, status: str, message: str) -> Dict[str, Any]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        last_date_str = self.get_data(self._data_key("last_success_date"))
        consecutive_days = int(self.get_data(self._data_key("consecutive_days")) or 0)

        if last_date_str == today_str:
            pass
        elif last_date_str == (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"):
            consecutive_days += 1
        else:
            consecutive_days = 1

        self.save_data(self._data_key("consecutive_days"), consecutive_days)
        self.save_data(self._data_key("last_success_date"), today_str)
        site_info = self._fetch_site_info()

        return {
            "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
            "site": self._current_site_name,
            "site_key": self._current_site_key,
            "account": self._current_account_name,
            "account_key": self._current_account_key,
            "status": status,
            "message": message or "签到完成",
            "points": self._extract_points(message),
            "site_username": site_info.get("site_username", "—"),
            "site_level": site_info.get("site_level", "—"),
            "total_points": site_info.get("total_points", "—"),
            "site_total_days": site_info.get("site_total_days", "—"),
            "days": consecutive_days,
        }

    def _save_sign_history(self, sign_data: Dict[str, Any]):
        try:
            history_key = self._data_key("sign_history")
            history = self.get_data(history_key) or []
            if "date" not in sign_data:
                sign_data["date"] = datetime.today().strftime("%Y-%m-%d %H:%M:%S")
            sign_data.setdefault("site", self._current_site_name)
            sign_data.setdefault("site_key", self._current_site_key)
            sign_data.setdefault("account", self._current_account_name)
            sign_data.setdefault("account_key", self._current_account_key)
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

            self.save_data(key=history_key, value=valid_history)
            logger.info(f"保存签到历史，site={self._current_site_name}, account={self._current_account_name}, 当前共有 {len(valid_history)} 条记录")
        except Exception as e:
            logger.error(f"保存签到历史失败: {e}", exc_info=True)

    def _send_sign_notification(self, sign_dict: Dict[str, Any]):
        if not self._notify:
            logger.info("聚合签到通知开关未开启，跳过发送")
            return

        status = sign_dict.get("status", "未知")
        site = sign_dict.get("site", self._current_site_name)
        account = sign_dict.get("account", self._current_account_name)
        message = sign_dict.get("message", "—")
        points = sign_dict.get("points", "—")
        site_username = sign_dict.get("site_username", "—")
        total_points = sign_dict.get("total_points", "—")
        site_total_days = sign_dict.get("site_total_days", "—")
        days = sign_dict.get("days", self.get_data(self._data_key("consecutive_days")) or "—")
        sign_time = sign_dict.get("date", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        trigger_type = self._current_trigger_type or "未知"

        if "成功" in status:
            title = f"【{site}签到成功】"
        elif "已签到" in status or "跳过" in status:
            title = f"【{site}重复签到】"
        else:
            title = f"【{site}签到失败】"

        text = (
            f"执行结果\n"
            f"━━━━━━━━━━\n"
            f"时间：{sign_time}\n"
            f"方式：{trigger_type}\n"
            f"站点：{site}\n"
            f"账号：{account}\n"
            f"状态：{status}\n"
            f"用户：{site_username}\n"
            f"━━━━━━━━━━\n"
            f"签到信息\n"
            f"详情：{message}\n"
            f"奖励积分：{points}\n"
            f"总积分：{total_points}\n"
            f"累计签到：{site_total_days}\n"
            f"连续天数：{days}\n"
            f"━━━━━━━━━━"
        )
        if "失败" in status:
            text = (
                f"{text}\n"
                f"可能的解决方法\n"
                f"检查 Cookie 是否有效\n"
                f"确认账号密码或 Cookie 是否能正常登录\n"
                f"查看站点是否正常访问"
            )

        self._post_notification(title=title, text=text)

    def _post_notification(self, title: str, text: str):
        if not self._notify:
            logger.info(f"聚合签到通知开关未开启，跳过发送: {title}")
            return
        try:
            logger.info(f"发送聚合签到通知: {title}")
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=title,
                text=text,
            )
            logger.info(f"聚合签到通知已提交: {title}")
        except Exception as e:
            logger.error(f"发送聚合签到通知失败: {e}", exc_info=True)

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y", "on", "开启", "是")
        return bool(value)

    def get_state(self) -> bool:
        logger.info(f"聚合签到状态: {self._enabled}")
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            logger.info(f"注册聚合签到定时服务: {self._cron}")
            return [{
                "id": "aggregatesign",
                "name": "聚合签到",
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
                                "component": "VTextarea",
                                "props": {
                                    "model": "accounts",
                                    "label": "多账号配置",
                                    "rows": 16,
                                    "placeholder": (
                                        "[\n"
                                        "  {\n"
                                        "    \"site\": \"juying\",\n"
                                        "    \"name\": \"聚影账号1\",\n"
                                        "    \"username\": \"你的用户名或邮箱\",\n"
                                        "    \"password\": \"你的密码\",\n"
                                        "    \"cookie\": \"\",\n"
                                        "    \"methods\": [\"normal\"]\n"
                                        "  },\n"
                                        "  {\n"
                                        "    \"site\": \"dian115\",\n"
                                        "    \"name\": \"癫影账号1\",\n"
                                        "    \"username\": \"你的邮箱\",\n"
                                        "    \"password\": \"你的密码\",\n"
                                        "    \"cookie\": \"\",\n"
                                        "    \"methods\": [\"normal\"]\n"
                                        "  },\n"
                                        "  {\n"
                                        "    \"site\": \"hdhive\",\n"
                                        "    \"name\": \"影巢账号1\",\n"
                                        "    \"username\": \"你的用户名或邮箱\",\n"
                                        "    \"password\": \"你的密码\",\n"
                                        "    \"cookie\": \"\",\n"
                                        "    \"methods\": [\"normal\"]\n"
                                        "  }\n"
                                        "]"
                                    ),
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
                                        "model": "retry_interval_minutes",
                                        "label": "失败重试间隔(分钟)",
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
                                        "model": "account_interval",
                                        "label": "账号轮询间隔(秒)",
                                        "type": "number",
                                        "placeholder": "10",
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
                                    "text": "使用说明：只使用上方 JSON 多账号配置。site 支持 juying、dian115 和 hdhive；三个站点都可填写 username/password 自动登录。影巢登录态绑定浏览器环境，推荐使用账号密码，插件会保存完整浏览器状态。methods 支持 normal；dian115 还支持 lucky，hdhive 还支持 gamble（风险签到可能扣积分，不建议默认开启）。",
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
            "accounts": self._default_accounts_text(),
            "base_url": "https://share.huamucang.top",
            "cron": "0 8 * * *",
            "max_retries": 3,
            "retry_interval_minutes": 3,
            "account_interval": 10,
            "history_days": 30,
        }

    def get_page(self) -> List[dict]:
        historys = self._all_histories()
        consecutive_days = self.get_data(self._data_key("consecutive_days")) or 0

        if not historys:
            return [{
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "暂无签到记录。",
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
                    {"component": "td", "text": str(history.get("site", "聚影"))},
                    {"component": "td", "text": str(history.get("account", "默认账号"))},
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
                    "text": f"聚合签到历史（当前账号连续 {consecutive_days} 天）",
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
                                        {"component": "th", "text": "站点"},
                                        {"component": "th", "text": "账号"},
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
            logger.error(f"停止聚合签到服务失败: {e}")

    def _build_config(self, onlyonce: Optional[bool] = None) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce if onlyonce is None else onlyonce,
            "cookie": self._legacy_account.get("cookie", self._cookie) if self._accounts_text.strip() else self._cookie,
            "storage_state": (
                self._legacy_account.get("storage_state", self._storage_state)
                if self._accounts_text.strip()
                else self._storage_state
            ),
            "username": self._legacy_account.get("username", self._username) if self._accounts_text.strip() else self._username,
            "password": self._legacy_account.get("password", self._password) if self._accounts_text.strip() else self._password,
            "accounts": self._accounts_text,
            "base_url": self._base_url,
            "cron": self._cron,
            "max_retries": self._max_retries,
            "retry_interval_minutes": self._retry_interval_minutes,
            "retry_interval": self._retry_interval,
            "account_interval": self._account_interval,
            "history_days": self._history_days,
        }

    @classmethod
    def _default_accounts_text(cls) -> str:
        return json.dumps(cls._default_accounts, ensure_ascii=False, indent=2)

    def _is_manual_trigger(self) -> bool:
        return getattr(self, "_manual_trigger", False)

    def _is_already_signed_today(self) -> bool:
        history = self.get_data(self._data_key("sign_history")) or []
        today = datetime.now().strftime("%Y-%m-%d")
        return any(
            record.get("date", "").startswith(today)
            and record.get("status") in ["签到成功", "已签到"]
            for record in history
        )

    def _build_repeat_record(self) -> Dict[str, Any]:
        history = self.get_data(self._data_key("sign_history")) or []
        today = datetime.now().strftime("%Y-%m-%d")
        today_success = [
            record
            for record in history
            if record.get("date", "").startswith(today)
            and record.get("status") in ["签到成功", "已签到"]
        ]
        today_success = sorted(today_success, key=lambda item: item.get("date", ""), reverse=True)
        latest = max(today_success, key=lambda item: item.get("date", ""), default={})
        site_info = self._fetch_site_info()
        return {
            "date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"),
            "site": self._current_site_name,
            "site_key": self._current_site_key,
            "account": self._current_account_name,
            "account_key": self._current_account_key,
            "status": "已签到" if self._is_manual_trigger() else "跳过: 今日已签到",
            "message": self._first_existing_value(
                latest.get("message"),
                *(record.get("message") for record in today_success[1:]),
                "今日已完成签到",
            ),
            "points": self._first_existing_value(
                latest.get("points"),
                *(record.get("points") for record in today_success[1:]),
                "—",
            ),
            "site_username": self._first_existing_value(
                site_info.get("site_username"),
                *(record.get("site_username") for record in today_success),
                self._current_account_name,
                "—",
            ),
            "site_level": self._first_existing_value(
                site_info.get("site_level"),
                *(record.get("site_level") for record in today_success),
                "—",
            ),
            "total_points": self._first_existing_value(
                site_info.get("total_points"),
                *(record.get("total_points") for record in today_success),
                "—",
            ),
            "site_total_days": self._first_existing_value(
                site_info.get("site_total_days"),
                *(record.get("site_total_days") for record in today_success),
                "—",
            ),
            "days": latest.get("days", self.get_data(self._data_key("consecutive_days")) or "—"),
        }

    def _fetch_site_info(self) -> Dict[str, Any]:
        info = {
            "site_username": self.get_data(self._data_key("site_username")) or self._current_account_name or "—",
            "site_level": self.get_data(self._data_key("site_level")) or "—",
            "total_points": self.get_data(self._data_key("total_points")) or "—",
            "site_total_days": self.get_data(self._data_key("site_total_days")) or "—",
        }
        if AggregateSignClient is None or not self._cookie:
            return info
        site = self._site_defaults.get(self._current_site_key, self._site_defaults["juying"])
        client = AggregateSignClient(
            base_url=self._base_url,
            headless=True,
            site_key=self._current_site_key,
            checkin_path=site.get("checkin_path", ""),
            login_path=site.get("login_path", ""),
        )

        profile: Dict[str, Any] = {}
        try:
            profile = client.get_profile(cookie_str=self._cookie, storage_state=self._storage_state)
            site_username = self._profile_display_name(profile)
            if site_username:
                info["site_username"] = site_username
                self.save_data(self._data_key("site_username"), site_username)

            site_level = self._first_profile_value(
                profile,
                ("level_name", "level_label", "level_title", "vip_name", "vip_level_name", "level", "user_level", "vip_level"),
            )
            if site_level is not None:
                info["site_level"] = site_level
                self.save_data(self._data_key("site_level"), site_level)

            total = self._first_profile_value(
                profile,
                ("points", "point", "score", "credit", "credits", "balance", "coin", "coins", "new_balance"),
            )
            if total is not None:
                info["total_points"] = total
                self.save_data(self._data_key("total_points"), total)
        except Exception as e:
            logger.warning(f"获取{self._current_site_name}用户信息失败: {e}")

        if self._current_site_key in ("dian115", "hdhive"):
            try:
                site_total_days = self._first_profile_value(
                    profile,
                    (
                        "signin_days",
                        "sign_days",
                        "checkin_days",
                        "total_signin_days",
                        "total_checkin_days",
                        "continuous_signin_days",
                        "streak_days",
                        "signin_days_total",
                    ),
                )
                if site_total_days is not None:
                    info["site_total_days"] = site_total_days
                    self.save_data(self._data_key("site_total_days"), site_total_days)
            except Exception as e:
                logger.warning(f"获取{self._current_site_name}签到统计失败: {e}")
        elif self._current_site_key == "juying":
            try:
                stats = client.get_checkin_stats(cookie_str=self._cookie, storage_state=self._storage_state)
                site_total_days = self._first_profile_value(
                    stats,
                    ("my_total_days", "total_days", "checkin_days", "sign_days", "signin_days", "days"),
                )
                if site_total_days is not None:
                    info["site_total_days"] = site_total_days
                    self.save_data(self._data_key("site_total_days"), site_total_days)
                total = self._first_profile_value(
                    stats,
                    ("points", "total_points", "score", "credit", "credits", "balance"),
                )
                if total is not None:
                    info["total_points"] = total
                    self.save_data(self._data_key("total_points"), total)
            except Exception as e:
                logger.warning(f"获取{self._current_site_name}签到统计失败: {e}")
        return info

    @staticmethod
    def _prefer_site_info(site_info: Dict[str, Any], latest: Dict[str, Any], key: str) -> Any:
        value = site_info.get(key)
        if value not in (None, "", "—"):
            return value
        return latest.get(key, "—")

    @staticmethod
    def _first_profile_value(profile: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
        for key in keys:
            value = profile.get(key)
            if value not in (None, "", "—"):
                return value
        return None

    @staticmethod
    def _first_existing_value(*values: Any) -> Any:
        for value in values:
            if value not in (None, "", "—"):
                return value
        return "—"

    @staticmethod
    def _compact_login_message(message: str) -> str:
        text = " ".join(str(message or "").split())
        if not text:
            return "登录失败"
        if "用户名或邮箱" in text and "注册账号" in text and "找回密码" in text:
            return "登录后仍停留在登录页，可能账号密码错误、站点登录页面变化或站点响应过慢"
        if len(text) > 120:
            return f"{text[:120]}..."
        return text

    def _profile_display_name(self, profile: Dict[str, Any]) -> str:
        if not isinstance(profile, dict):
            return ""
        if self._current_site_key == "dian115":
            for key in ("email", "nickname", "name", "display_name"):
                value = str(profile.get(key) or "").strip()
                if value:
                    return value
            username = str(profile.get("username") or profile.get("sub") or "").strip()
            if username.startswith("u_") and self._current_account_name:
                return self._current_account_name
            return username
        for key in ("username", "name", "nickname", "email", "display_name"):
            value = str(profile.get(key) or "").strip()
            if value:
                return value
        return ""

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
    def _is_transient_error(message: str) -> bool:
        return any(
            keyword in (message or "").lower()
            for keyword in (
                "timeout",
                "timed out",
                "dns",
                "failed to query",
                "failed to receive reply",
                "remote end closed",
                "connection reset",
                "connection aborted",
                "connection refused",
                "temporarily unavailable",
                "network",
                "net::",
                "econnreset",
                "etimedout",
                "enotfound",
            )
        )

    @staticmethod
    def _extract_points(message: str) -> Any:
        if not message:
            return "—"
        match = re.search(r"(?:获得|奖励|奖励积分|积分)[^\-\d]*(-?\d+)", message)
        return int(match.group(1)) if match else "—"

    def _get_last_success_message(self) -> str:
        history = self.get_data(self._data_key("sign_history")) or []
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
