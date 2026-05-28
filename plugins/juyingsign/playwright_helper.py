__all__ = ["JuyingPlaywrightClient", "JuyingBrowserError"]

from contextlib import contextmanager
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
import json
from socket import AF_INET, SO_REUSEADDR, SOCK_STREAM, SOL_SOCKET, socket
from sys import platform
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler, urlopen

try:
    from cloakbrowser import launch_context as cloak_launch_context
except Exception:
    cloak_launch_context = None

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from app.core.config import settings


class JuyingBrowserError(Exception):
    """
    聚影网页签到浏览器异常。
    """


class JuyingPlaywrightClient:
    """
    聚影站点 Playwright/CloakBrowser 客户端。
    """

    _UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, base_url: str = "https://share.huamucang.top", headless: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.checkin_path = "/checkin"
        self.login_path = "/login"
        self._headless = headless

    @staticmethod
    def _parse_cookie_str(cookie_str: str) -> Dict[str, str]:
        cookies: Dict[str, str] = {}
        for item in (cookie_str or "").split(";"):
            if "=" not in item:
                continue
            name, value = item.strip().split("=", 1)
            if name.strip():
                cookies[name.strip()] = value.strip()
        return cookies

    @staticmethod
    def _proxy_url_from_settings() -> Optional[str]:
        proxy = getattr(settings, "PROXY", None)
        if not proxy:
            return None
        if isinstance(proxy, str):
            return proxy
        if isinstance(proxy, dict):
            url = proxy.get("https") or proxy.get("http")
            return str(url) if url else None
        return None

    @staticmethod
    def _playwright_proxy_settings() -> Optional[Dict[str, str]]:
        raw = JuyingPlaywrightClient._proxy_url_from_settings()
        if not raw:
            return None
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.hostname:
            return None
        if parsed.scheme in ("socks", "socks5") and (parsed.username or parsed.password):
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        proxy: Dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}:{port}"}
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy

    @staticmethod
    def _urllib_proxy_handler() -> Optional[ProxyHandler]:
        raw = JuyingPlaywrightClient._proxy_url_from_settings()
        if not raw:
            return None
        return ProxyHandler({"http": raw, "https": raw})

    @staticmethod
    @contextmanager
    def _socks5_slippers_if_needed() -> Iterator[Optional[Dict[str, str]]]:
        raw = JuyingPlaywrightClient._proxy_url_from_settings()
        if not raw:
            yield None
            return
        parsed = urlparse(raw)
        if parsed.scheme not in ("socks", "socks5") or not (parsed.username or parsed.password):
            yield None
            return

        sock = socket(AF_INET, SOCK_STREAM)
        try:
            sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            local_port = sock.getsockname()[1]
        finally:
            sock.close()

        try:
            from slippers import Proxy
        except ImportError:
            yield None
            return

        proxy = Proxy(raw, host="127.0.0.1", port=local_port)
        with proxy:
            yield {"server": proxy.url()}

    @staticmethod
    @contextmanager
    def _browser_runtime() -> Iterator[Optional[Any]]:
        if cloak_launch_context is not None:
            yield None
            return
        if sync_playwright is None:
            raise JuyingBrowserError("当前环境缺少 CloakBrowser 或 Playwright 依赖")
        with sync_playwright() as playwright:
            yield playwright

    @staticmethod
    def _chromium_launch_args() -> list[str]:
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        if platform == "linux":
            args.extend(["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"])
        return args

    def _make_context(
        self,
        playwright: Any,
        proxy: Optional[Dict[str, str]] = None,
    ) -> Tuple[Any, Any]:
        if cloak_launch_context is not None:
            kwargs: Dict[str, Any] = {
                "headless": self._headless,
                "user_agent": self._UA,
                "humanize": getattr(settings, "CLOAKBROWSER_HUMANIZE", True),
                "human_preset": getattr(settings, "CLOAKBROWSER_HUMAN_PRESET", "default"),
                "viewport": {"width": 1280, "height": 720},
            }
            if proxy:
                kwargs["proxy"] = proxy
            context = cloak_launch_context(**kwargs)
            return context, context

        browser = playwright.chromium.launch(
            headless=self._headless,
            args=JuyingPlaywrightClient._chromium_launch_args(),
            proxy=proxy,
        )
        context = browser.new_context(
            user_agent=self._UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 720},
        )
        return browser, context

    def _add_cookies(self, context: Any, cookie_str: str) -> None:
        cookies = JuyingPlaywrightClient._parse_cookie_str(cookie_str)
        if not cookies:
            raise JuyingBrowserError("未配置有效 Cookie")

        domain = urlparse(self.base_url).hostname
        if not domain:
            raise JuyingBrowserError("站点地址无效")

        context.add_cookies([
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
            }
            for name, value in cookies.items()
        ])

    def _restore_storage_state(self, context: Any, storage_state: str) -> None:
        if not storage_state:
            return
        try:
            state = json.loads(storage_state)
        except Exception:
            return
        if not isinstance(state, dict):
            return

        page = context.new_page()
        try:
            page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
            origin = self.base_url
            origins = state.get("origins") or []
            for item in origins:
                if item.get("origin") != origin:
                    continue
                for entry in item.get("localStorage") or []:
                    name = entry.get("name")
                    value = entry.get("value")
                    if name is not None and value is not None:
                        page.evaluate(
                            "([key, val]) => window.localStorage.setItem(key, val)",
                            [str(name), str(value)],
                        )
                for entry in item.get("sessionStorage") or []:
                    name = entry.get("name")
                    value = entry.get("value")
                    if name is not None and value is not None:
                        page.evaluate(
                            "([key, val]) => window.sessionStorage.setItem(key, val)",
                            [str(name), str(value)],
                        )
        finally:
            page.close()

    @staticmethod
    def _cookie_list_to_str(cookies: list[dict[str, Any]]) -> str:
        parts = []
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if name and value is not None:
                parts.append(f"{name}={value}")
        return "; ".join(parts)

    @staticmethod
    def _cookiejar_to_str(cookie_jar: CookieJar) -> str:
        parts = []
        for cookie in cookie_jar:
            parts.append(f"{cookie.name}={cookie.value}")
        return "; ".join(parts)

    @staticmethod
    def _set_cookie_headers_to_str(headers: list[str]) -> str:
        cookie = SimpleCookie()
        for header in headers or []:
            cookie.load(header)
        return "; ".join(f"{name}={morsel.value}" for name, morsel in cookie.items())

    def _storage_state_from_token(self, token: str) -> str:
        if not token:
            return ""
        expires_ms = 7 * 24 * 60 * 60 * 1000
        state = {
            "cookies": [],
            "origins": [{
                "origin": self.base_url,
                "localStorage": [
                    {"name": "app_user_token", "value": token},
                    {"name": "app_user_token_expires", "value": str(self._now_ms() + expires_ms)},
                ],
            }],
        }
        return json.dumps(state, ensure_ascii=False)

    @staticmethod
    def _now_ms() -> int:
        import time
        return int(time.time() * 1000)

    @staticmethod
    def _make_cookie(name: str, value: str, domain: str) -> Cookie:
        return Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )

    def _api_request_with_login_state(
        self,
        path: str,
        method: str = "GET",
        cookie_str: str = "",
        token: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any], str]:
        cookie_jar = CookieJar()
        domain = urlparse(self.base_url).hostname or "share.huamucang.top"
        for name, value in self._parse_cookie_str(cookie_str).items():
            cookie_jar.set_cookie(self._make_cookie(name, value, domain))

        handlers = [HTTPCookieProcessor(cookie_jar)]
        proxy = JuyingPlaywrightClient._urllib_proxy_handler()
        if proxy:
            handlers.append(proxy)
        opener = build_opener(*handlers)

        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "User-Agent": self._UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}{self.checkin_path}",
        }
        if token:
            headers["X-App-User-Token"] = token
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            response = opener.open(request, timeout=30)
            with response:
                body = response.read().decode("utf-8", errors="replace")
                status = response.status
        except HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")
            status = err.code

        try:
            data = json.loads(body or "{}")
        except Exception:
            data = {}
        return status, data, self._cookiejar_to_str(cookie_jar)

    def _login_by_api_cookie(self, username: str, password: str) -> Tuple[bool, str, str, str]:
        status, data, cookie_str = self._api_request_with_login_state(
            path="/api/app/login/",
            method="POST",
            payload={"username": username, "password": password},
        )
        if status >= 400 or data.get("status") == "error":
            return False, "", "", str(data.get("message") or f"登录接口 HTTP {status}")

        if not cookie_str:
            return False, "", "", "登录接口未返回 Cookie"

        storage_state = self._storage_state_from_token(str(data.get("token") or ""))
        return True, cookie_str, storage_state, "登录成功"

    def get_profile(self, cookie_str: str, storage_state: str = "") -> Dict[str, Any]:
        token = self._token_from_storage_state(storage_state)
        status, data, _ = self._api_request_with_login_state(
            path="/api/app/profile/",
            method="GET",
            cookie_str=cookie_str,
            token=token,
        )
        if status >= 400 or not isinstance(data, dict):
            return {}
        user = data.get("user")
        return user if isinstance(user, dict) else data

    def get_checkin_stats(self, cookie_str: str, storage_state: str = "") -> Dict[str, Any]:
        token = self._token_from_storage_state(storage_state)
        status, data, _ = self._api_request_with_login_state(
            path="/api/app/checkin/stats/",
            method="GET",
            cookie_str=cookie_str,
            token=token,
        )
        if status >= 400 or not isinstance(data, dict):
            return {}
        return data

    @staticmethod
    def _token_from_storage_state(storage_state: str) -> str:
        if not storage_state:
            return ""
        try:
            state = json.loads(storage_state)
        except Exception:
            return ""
        for origin in state.get("origins") or []:
            for item in origin.get("localStorage") or []:
                if item.get("name") == "app_user_token":
                    return str(item.get("value") or "")
        return ""

    @staticmethod
    def _extract_page_message(page: Any) -> str:
        selectors = [
            ".n-message",
            ".n-notification",
            ".n-alert",
            ".checkin-cta",
            ".section-card",
            "body",
        ]
        for selector in selectors:
            try:
                text = (page.locator(selector).last.inner_text(timeout=1500) or "").strip()
                if text:
                    return " ".join(text.split())
            except Exception:
                continue
        return ""

    @staticmethod
    def _is_already_signed_text(text: str) -> bool:
        return any(keyword in text for keyword in ("已签到", "已经签到", "今日已签", "明天再来"))

    def checkin(self, cookie_str: str, storage_state: str = "") -> Tuple[bool, str]:
        if not cookie_str:
            return False, "未配置 Cookie"

        try:
            with JuyingPlaywrightClient._browser_runtime() as playwright:
                with JuyingPlaywrightClient._socks5_slippers_if_needed() as slip:
                    proxy = slip if slip is not None else JuyingPlaywrightClient._playwright_proxy_settings()
                    browser, context = self._make_context(playwright, proxy)
                    try:
                        self._add_cookies(context, cookie_str)
                        self._restore_storage_state(context, storage_state)
                        page = context.new_page()
                        page.goto(
                            f"{self.base_url}{self.checkin_path}",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        page.wait_for_load_state("networkidle", timeout=30000)

                        if "/login" in page.url:
                            return False, "Cookie 无效或登录已过期，站点跳转到登录页"

                        page_text = self._extract_page_message(page)
                        if JuyingPlaywrightClient._is_already_signed_text(page_text):
                            return True, page_text or "今日已签到"

                        button = page.locator("button:has-text('立即签到')").first
                        try:
                            button.wait_for(state="visible", timeout=15000)
                        except PlaywrightTimeoutError:
                            page_text = self._extract_page_message(page)
                            if JuyingPlaywrightClient._is_already_signed_text(page_text):
                                return True, page_text or "今日已签到"
                            return False, page_text or "未找到立即签到按钮"

                        button.click(timeout=10000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        try:
                            page.wait_for_timeout(2000)
                        except Exception:
                            pass

                        result_text = self._extract_page_message(page)
                        success_keywords = ("签到成功", "成功", "已签到", "已经签到", "今日已签", "获得", "积分")
                        if any(keyword in result_text for keyword in success_keywords):
                            return True, result_text or "签到请求已完成"
                        return False, result_text or "签到后未识别到成功提示"
                    finally:
                        browser.close()
        except JuyingBrowserError as err:
            return False, str(err)
        except Exception as err:
            return False, f"签到异常: {err}"

    def login(self, username: str, password: str) -> Tuple[bool, str, str, str]:
        if not username or not password:
            return False, "", "", "未配置用户名或密码"

        success, cookie_str, storage_state, message = self._login_by_api_cookie(username, password)
        if success:
            return True, cookie_str, storage_state, message

        try:
            with JuyingPlaywrightClient._browser_runtime() as playwright:
                with JuyingPlaywrightClient._socks5_slippers_if_needed() as slip:
                    proxy = slip if slip is not None else JuyingPlaywrightClient._playwright_proxy_settings()
                    browser, context = self._make_context(playwright, proxy)
                    try:
                        page = context.new_page()
                        page.goto(
                            f"{self.base_url}{self.login_path}",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        page.wait_for_selector("input", timeout=15000)

                        if not self._fill_login_form(page, username, password):
                            return False, "", "", "未找到可用的登录输入框"

                        try:
                            page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
                        except Exception:
                            pass
                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass

                        if "/login" in page.url:
                            page_text = self._extract_page_message(page)
                            if any(keyword in page_text for keyword in ("错误", "失败", "密码", "验证码")):
                                return False, "", "", page_text
                            return False, "", "", page_text or "登录后仍停留在登录页"

                        page.goto(
                            f"{self.base_url}{self.checkin_path}",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        if "/login" in page.url:
                            page_text = self._extract_page_message(page)
                            return False, "", "", page_text or "账号密码登录未保持有效登录态"

                        raw_cookies = context.cookies()
                        cookie_str = self._cookie_list_to_str(raw_cookies)
                        if not cookie_str:
                            return False, "", "", "登录后未获取到 Cookie"
                        try:
                            storage_state = json.dumps(context.storage_state(), ensure_ascii=False)
                        except Exception:
                            storage_state = ""
                        return True, cookie_str, storage_state, "登录成功"
                    finally:
                        browser.close()
        except JuyingBrowserError as err:
            return False, "", "", str(err)
        except Exception as err:
            return False, "", "", f"登录异常: {err}"

    @staticmethod
    def _fill_login_form(page: Any, username: str, password: str) -> bool:
        user_selectors = [
            "input[name='username']",
            "input[name='email']",
            "input[type='email']",
            "input[placeholder*='邮箱']",
            "input[placeholder*='账号']",
            "input[placeholder*='用户名']",
            "input[placeholder*='手机号']",
        ]
        password_selectors = [
            "input[name='password']",
            "input[type='password']",
            "input[placeholder*='密码']",
        ]

        filled_user = False
        for selector in user_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    locator.fill(username, timeout=5000)
                    filled_user = True
                    break
            except Exception:
                continue

        filled_password = False
        for selector in password_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    locator.fill(password, timeout=5000)
                    filled_password = True
                    break
            except Exception:
                continue

        if not filled_user or not filled_password:
            return False

        clicked = False
        for selector in ("button[type='submit']", "button:has-text('登录')", "button:has-text('Login')"):
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    locator.click(timeout=10000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            page.keyboard.press("Enter")
        return True
