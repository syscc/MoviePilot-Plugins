__all__ = ["JuyingPlaywrightClient", "JuyingBrowserError"]

from contextlib import contextmanager
from socket import AF_INET, SO_REUSEADDR, SOCK_STREAM, SOL_SOCKET, socket
from sys import platform
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import unquote, urlparse

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

    def checkin(self, cookie_str: str) -> Tuple[bool, str]:
        if not cookie_str:
            return False, "未配置 Cookie"

        try:
            with JuyingPlaywrightClient._browser_runtime() as playwright:
                with JuyingPlaywrightClient._socks5_slippers_if_needed() as slip:
                    proxy = slip if slip is not None else JuyingPlaywrightClient._playwright_proxy_settings()
                    browser, context = self._make_context(playwright, proxy)
                    try:
                        self._add_cookies(context, cookie_str)
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

    def login(self, username: str, password: str) -> Tuple[bool, str, str]:
        if not username or not password:
            return False, "", "未配置用户名或密码"

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
                            return False, "", "未找到可用的登录输入框"

                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass

                        if "/login" in page.url:
                            page_text = self._extract_page_message(page)
                            if any(keyword in page_text for keyword in ("错误", "失败", "密码", "验证码")):
                                return False, "", page_text

                        raw_cookies = context.cookies()
                        cookie_str = self._cookie_list_to_str(raw_cookies)
                        if not cookie_str:
                            return False, "", "登录后未获取到 Cookie"
                        return True, cookie_str, "登录成功"
                    finally:
                        browser.close()
        except JuyingBrowserError as err:
            return False, "", str(err)
        except Exception as err:
            return False, "", f"登录异常: {err}"

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
