__all__ = ["AggregateSignClient", "AggregateSignBrowserError"]

import base64
from contextlib import contextmanager
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
import json
import re
from socket import AF_INET, SO_REUSEADDR, SOCK_STREAM, SOL_SOCKET, socket
from sys import platform
import time
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse, urlunparse
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler

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


class AggregateSignBrowserError(Exception):
    """
    聚合签到浏览器异常。
    """


class AggregateSignClient:
    """
    聚合签到 Playwright/CloakBrowser 客户端。
    """

    _UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    _NAVIGATION_TIMEOUT = 60000
    _NETWORK_IDLE_TIMEOUT = 20000

    def __init__(
        self,
        base_url: str = "https://www.jying.top",
        headless: bool = True,
        site_key: str = "juying",
        checkin_path: str = "",
        login_path: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.site_key = site_key or "juying"
        default_checkin_path = {
            "dian115": "/me/signin",
            "hdhive": "/",
        }.get(self.site_key, "/checkin")
        self.checkin_path = checkin_path or default_checkin_path
        self.login_path = login_path or "/login"
        self._headless = headless
        self._updated_cookie_str = ""
        self._updated_storage_state = ""

    @staticmethod
    def _migrate_hdhive_storage_state(storage_state: str, target_origin: str) -> str:
        """将旧影巢域名的浏览器状态迁移到新入口。"""
        if not storage_state or not target_origin:
            return storage_state
        try:
            state = json.loads(storage_state)
        except Exception:
            return storage_state
        if not isinstance(state, dict):
            return storage_state

        target = urlparse(target_origin)
        target_host = (target.hostname or "").lower()
        if not target_host:
            return storage_state
        legacy_hosts = {"hdhive.com", "www.hdhive.com"}
        changed = False

        for cookie in state.get("cookies") or []:
            if not isinstance(cookie, dict):
                continue
            raw_domain = str(cookie.get("domain") or "")
            domain_host = raw_domain.lstrip(".").lower()
            if domain_host in legacy_hosts:
                cookie["domain"] = f".{target_host}" if raw_domain.startswith(".") else target_host
                changed = True
            raw_url = str(cookie.get("url") or "")
            parsed_url = urlparse(raw_url)
            if parsed_url.hostname and parsed_url.hostname.lower() in legacy_hosts:
                cookie["url"] = urlunparse((
                    target.scheme or parsed_url.scheme,
                    target.netloc or parsed_url.netloc,
                    parsed_url.path or "/",
                    parsed_url.params,
                    parsed_url.query,
                    parsed_url.fragment,
                ))
                changed = True

        for origin in state.get("origins") or []:
            if not isinstance(origin, dict):
                continue
            raw_origin = str(origin.get("origin") or "")
            parsed_origin = urlparse(raw_origin)
            if parsed_origin.hostname and parsed_origin.hostname.lower() in legacy_hosts:
                origin["origin"] = target_origin.rstrip("/")
                changed = True

        if not changed:
            return storage_state
        return json.dumps(state, ensure_ascii=False)

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
        raw = AggregateSignClient._proxy_url_from_settings()
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
        raw = AggregateSignClient._proxy_url_from_settings()
        if not raw:
            return None
        return ProxyHandler({"http": raw, "https": raw})

    @staticmethod
    @contextmanager
    def _socks5_slippers_if_needed() -> Iterator[Optional[Dict[str, str]]]:
        raw = AggregateSignClient._proxy_url_from_settings()
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
            raise AggregateSignBrowserError("当前环境缺少 CloakBrowser 或 Playwright 依赖")
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
        storage_state: str = "",
    ) -> Tuple[Any, Any]:
        if self.site_key == "hdhive":
            storage_state = self._migrate_hdhive_storage_state(
                storage_state,
                self.base_url,
            )
        state: Optional[Dict[str, Any]] = None
        if storage_state:
            try:
                parsed_state = json.loads(storage_state)
                if isinstance(parsed_state, dict):
                    state = parsed_state
            except Exception:
                state = None

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
            if state:
                kwargs["storage_state"] = state
            try:
                context = cloak_launch_context(**kwargs)
            except TypeError:
                kwargs.pop("storage_state", None)
                context = cloak_launch_context(**kwargs)
            return context, context

        browser = playwright.chromium.launch(
            headless=self._headless,
            args=AggregateSignClient._chromium_launch_args(),
            proxy=proxy,
        )
        context_kwargs: Dict[str, Any] = {
            "user_agent": self._UA,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1280, "height": 720},
        }
        if state:
            context_kwargs["storage_state"] = state
        context = browser.new_context(
            **context_kwargs,
        )
        return browser, context

    def _goto_page(self, page: Any, url: str, wait_until: str = "domcontentloaded") -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                return page.goto(
                    url,
                    wait_until=wait_until,
                    timeout=AggregateSignClient._NAVIGATION_TIMEOUT,
                )
            except PlaywrightTimeoutError as err:
                last_error = err
                if attempt >= 2:
                    raise
                try:
                    page.wait_for_timeout(2000)
                except Exception:
                    pass
                try:
                    page.goto("about:blank", timeout=10000)
                except Exception:
                    pass
        if last_error:
            raise last_error
        return None

    @staticmethod
    def _wait_network_idle(page: Any, timeout: int = _NETWORK_IDLE_TIMEOUT) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    def _add_cookies(self, context: Any, cookie_str: str) -> None:
        cookies = AggregateSignClient._parse_cookie_str(cookie_str)
        if not cookies:
            raise AggregateSignBrowserError("未配置有效 Cookie")

        base_url = self.base_url
        if not urlparse(base_url).hostname:
            raise AggregateSignBrowserError("站点地址无效")

        # 使用 url 创建 host-only Cookie，兼容 __Host- Cookie（不能显式设置 domain）。
        context.add_cookies([
            {
                "name": name,
                "value": value,
                "url": f"{base_url}/",
                "secure": base_url.startswith("https://"),
            }
            for name, value in cookies.items()
        ])

    def _context_has_site_cookies(self, context: Any) -> bool:
        try:
            cookies = context.cookies(self.base_url)
        except TypeError:
            cookies = context.cookies()
        except Exception:
            return False
        if not isinstance(cookies, list):
            return False
        hostname = urlparse(self.base_url).hostname or ""
        for cookie in cookies:
            if not isinstance(cookie, dict) or not cookie.get("name") or cookie.get("value") is None:
                continue
            domain = str(cookie.get("domain") or "").lstrip(".")
            url = str(cookie.get("url") or "")
            if domain == hostname or url.startswith(f"{self.base_url}/"):
                return True
        return False

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
            self._goto_page(page, self.base_url)
            origin = self.base_url
            compatible_origins = {origin}
            if self.site_key == "juying" and origin in ("https://jying.top", "https://www.jying.top"):
                compatible_origins.update({
                    "https://jying.top",
                    "https://www.jying.top",
                    "https://share.huamucang.top",
                })
            origins = state.get("origins") or []
            for item in origins:
                if item.get("origin") not in compatible_origins:
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
        domain = urlparse(self.base_url).hostname or "www.jying.top"
        for name, value in self._parse_cookie_str(cookie_str).items():
            cookie_jar.set_cookie(self._make_cookie(name, value, domain))

        handlers = [HTTPCookieProcessor(cookie_jar)]
        proxy = AggregateSignClient._urllib_proxy_handler()
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

    def _dian115_api_request(
        self,
        path: str,
        method: str = "GET",
        cookie_str: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any], str]:
        cookie_jar = CookieJar()
        domain = urlparse(self.base_url).hostname or "m.dian115.com"
        for name, value in self._parse_cookie_str(cookie_str).items():
            cookie_jar.set_cookie(self._make_cookie(name, value, domain))

        handlers = [HTTPCookieProcessor(cookie_jar)]
        proxy = AggregateSignClient._urllib_proxy_handler()
        if proxy:
            handlers.append(proxy)
        opener = build_opener(*handlers)

        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "User-Agent": self._UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}{self.checkin_path}",
        }
        request = Request(
            f"{self.base_url}/api/portal{path}",
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

    def _dian115_login(self, username: str, password: str) -> Tuple[bool, str, str, str]:
        status, data, cookie_str = self._dian115_api_request(
            path="/auth/login",
            method="POST",
            payload={"email": username, "password": password},
        )
        code = str(data.get("code") or "")
        message = str(data.get("msg") or data.get("message") or f"登录接口 HTTP {status}")
        if code == "turnstile_failed":
            return False, "", "", "站点启用了 Turnstile 人机验证，无法自动登录；请手动登录后复制 Cookie 到多账号配置"
        if status >= 400 or code not in ("", "ok"):
            return False, "", "", message
        if not cookie_str:
            return False, "", "", "登录接口未返回 Cookie"
        return True, cookie_str, "", "登录成功"

    @staticmethod
    def _dian115_auth_failed(status: int, data: Dict[str, Any]) -> bool:
        return status == 401 or data.get("code") in ("no_token", "invalid_token", "token_revoked")

    def _dian115_checkin_result(self, status: int, data: Dict[str, Any], mode: str) -> Tuple[bool, str]:
        code = data.get("code")
        method_name = self._dian115_method_name(mode)
        if code == "already_signed":
            return True, f"{method_name}: 今日已签到"
        if status >= 400 or code not in ("", "ok", None):
            msg = data.get("msg") or data.get("message") or f"HTTP {status}"
            return False, f"{method_name}失败: {msg}"

        award = data.get("award")
        balance = data.get("new_balance")
        tier = data.get("lucky_tier")
        detail = f"{method_name}成功"
        if award is not None:
            detail += f"，奖励积分 {award}"
        if balance is not None:
            detail += f"，当前积分 {balance}"
        if tier:
            detail += f"，运气结果 {tier}"
        return True, detail

    @staticmethod
    def _dian115_profile_from_response(data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        user = data.get("user")
        if isinstance(user, dict):
            profile = dict(data)
            profile.update(user)
            return profile
        return data

    @staticmethod
    def _dian115_today_signin_points(ledger: Any) -> Any:
        if not isinstance(ledger, dict):
            return None
        today = time.strftime("%Y-%m-%d")
        for item in ledger.get("items") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("created_at") or "")[:10] != today:
                continue
            description = " ".join(
                str(item.get(key) or "")
                for key in ("reason", "related_type", "note")
            ).lower()
            if "签到" not in description and "signin" not in description and "checkin" not in description:
                continue
            if item.get("delta") is not None:
                return item.get("delta")
        return None

    def _dian115_browser_profile(
        self,
        cookie_str: str,
        storage_state: str = "",
    ) -> Dict[str, Any]:
        try:
            with AggregateSignClient._browser_runtime() as playwright:
                with AggregateSignClient._socks5_slippers_if_needed() as slip:
                    proxy = slip if slip is not None else AggregateSignClient._playwright_proxy_settings()
                    browser, context = self._make_context(
                        playwright,
                        proxy,
                        storage_state=storage_state,
                    )
                    try:
                        if not self._context_has_site_cookies(context):
                            self._add_cookies(context, cookie_str)
                        page = context.new_page()
                        captured: Dict[str, Any] = {}

                        def capture_response(response: Any) -> None:
                            try:
                                if response.request.method != "GET":
                                    return
                                path = urlparse(response.url).path.rstrip("/")
                                key = {
                                    "/api/portal/me": "profile",
                                    "/api/portal/me/points/ledger": "ledger",
                                }.get(path)
                                if not key:
                                    return
                                current = captured.get(key)
                                if current is None or getattr(response, "status", 500) < 400:
                                    captured[key] = response
                            except Exception:
                                return

                        page.on("response", capture_response)
                        self._goto_page(page, f"{self.base_url}/me")
                        self._wait_network_idle(page)
                        if "/login" in page.url:
                            return {}

                        profile_response = captured.get("profile")
                        if profile_response is None or getattr(profile_response, "status", 500) >= 400:
                            return {}
                        try:
                            profile_data = profile_response.json()
                        except Exception:
                            profile_data = {}
                        profile = self._dian115_profile_from_response(profile_data)
                        if not profile:
                            return {}

                        self._goto_page(page, f"{self.base_url}/me/points")
                        self._wait_network_idle(page)
                        ledger_response = captured.get("ledger")
                        if ledger_response is not None and getattr(ledger_response, "status", 500) < 400:
                            try:
                                ledger_data = ledger_response.json()
                            except Exception:
                                ledger_data = {}
                            today_points = self._dian115_today_signin_points(ledger_data)
                            if today_points is not None:
                                profile["today_signin_points"] = today_points
                        return profile
                    finally:
                        browser.close()
        except Exception:
            return {}

    def _dian115_browser_checkin(
        self,
        cookie_str: str,
        modes: list[str],
        storage_state: str = "",
    ) -> Tuple[bool, str]:
        results = []
        all_success = True
        try:
            with AggregateSignClient._browser_runtime() as playwright:
                with AggregateSignClient._socks5_slippers_if_needed() as slip:
                    proxy = slip if slip is not None else AggregateSignClient._playwright_proxy_settings()
                    browser, context = self._make_context(
                        playwright,
                        proxy,
                        storage_state=storage_state,
                    )
                    try:
                        if not self._context_has_site_cookies(context):
                            self._add_cookies(context, cookie_str)
                        page = context.new_page()
                        self._goto_page(page, f"{self.base_url}{self.checkin_path}")
                        self._wait_network_idle(page)

                        if "/login" in page.url:
                            return False, "Cookie 无效或登录已过期，站点跳转到登录页"

                        for mode in modes:
                            method_name = self._dian115_method_name(mode)
                            button = page.locator(f"button:has-text('{method_name}')").first
                            try:
                                button.wait_for(state="visible", timeout=15000)
                            except PlaywrightTimeoutError:
                                page_text = self._extract_page_message(page)
                                return False, page_text or f"未找到{method_name}按钮"

                            try:
                                with page.expect_response(
                                    lambda response: (
                                        response.request.method == "POST"
                                        and urlparse(response.url).path.rstrip("/") == "/api/portal/signin"
                                    ),
                                    timeout=15000,
                                ) as response_info:
                                    button.click(timeout=10000)
                                checkin_response = response_info.value
                            except PlaywrightTimeoutError:
                                page_text = self._extract_page_message(page)
                                return False, page_text or f"{method_name}未收到签到响应"

                            try:
                                response_data = checkin_response.json()
                            except Exception:
                                response_data = {}
                            if not isinstance(response_data, dict):
                                response_data = {}
                            if self._dian115_auth_failed(checkin_response.status, response_data):
                                return False, "Cookie 无效或登录已过期"

                            success, detail = self._dian115_checkin_result(
                                checkin_response.status,
                                response_data,
                                mode,
                            )
                            results.append(detail)
                            all_success = all_success and success

                        return all_success and bool(results), "；".join(results) if results else "签到完成"
                    finally:
                        browser.close()
        except AggregateSignBrowserError as err:
            return False, str(err)
        except Exception as err:
            return False, f"签到异常: {err}"

    def _dian115_checkin(
        self,
        cookie_str: str,
        storage_state: str = "",
        methods: Optional[list[str]] = None,
    ) -> Tuple[bool, str]:
        if not cookie_str:
            return False, "未配置 Cookie"
        modes = [
            "lucky" if str(method).lower() in ("lucky", "luck", "运气签到", "运气") else "normal"
            for method in (methods or ["normal"])
        ]
        results = []
        all_success = True
        for mode in modes:
            status, data, _ = self._dian115_api_request(
                path="/signin",
                method="POST",
                cookie_str=cookie_str,
                payload={"mode": mode},
            )
            if not isinstance(data, dict):
                data = {}
            if self._dian115_auth_failed(status, data):
                return False, "Cookie 无效或登录已过期"
            if status == 403:
                return self._dian115_browser_checkin(
                    cookie_str=cookie_str,
                    storage_state=storage_state,
                    modes=modes,
                )

            success, detail = self._dian115_checkin_result(status, data, mode)
            results.append(detail)
            all_success = all_success and success
        return all_success and bool(results), "；".join(results) if results else "签到完成"

    @staticmethod
    def _dian115_method_name(method: str) -> str:
        return "运气签到" if method == "lucky" else "普通签到"

    def _hdhive_request(
        self,
        url: str,
        cookie_str: str,
        headers: Dict[str, str],
        method: str = "GET",
        body: Optional[bytes] = None,
    ) -> Tuple[int, str]:
        cookie_jar = CookieJar()
        domain = urlparse(self.base_url).hostname or "re0.me"
        for name, value in self._parse_cookie_str(cookie_str).items():
            cookie_jar.set_cookie(self._make_cookie(name, value, domain))

        handlers = [HTTPCookieProcessor(cookie_jar)]
        proxy = AggregateSignClient._urllib_proxy_handler()
        if proxy:
            handlers.append(proxy)
        opener = build_opener(*handlers)
        request = Request(url, data=body, method=method, headers=headers)
        try:
            response = opener.open(request, timeout=30)
            with response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except HTTPError as err:
            return err.code, err.read().decode("utf-8", errors="replace")

    @staticmethod
    def _hdhive_result(data: Dict[str, Any], status: int = 200) -> Tuple[bool, str]:
        if status in (401, 403):
            return False, "Cookie 无效或登录已过期"
        if not isinstance(data, dict):
            return False, f"影巢签到返回格式异常，HTTP {status}"
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        payload_error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        message = str(
            payload.get("description")
            or payload.get("message")
            or payload_error.get("description")
            or payload_error.get("message")
            or data.get("description")
            or data.get("message")
            or error.get("description")
            or error.get("message")
            or ""
        )
        already_signed = any(
            keyword in message
            for keyword in ("已经签到", "已经签到过", "签到过", "已签到", "明天再来")
        )
        success = bool(data.get("success") or payload.get("success")) or already_signed
        if not message:
            message = "签到成功" if success else f"影巢签到失败，HTTP {status}"
        return success, message

    @staticmethod
    def _hdhive_parse_rsc_result(text: str) -> Dict[str, Any]:
        for line in (text or "").splitlines():
            if ":{" not in line:
                continue
            try:
                result = json.loads(line.split(":", 1)[1])
            except Exception:
                continue
            if not isinstance(result, dict):
                continue
            if isinstance(result.get("error"), dict):
                return result["error"]
            if isinstance(result.get("response"), dict):
                return result["response"]
            if any(key in result for key in ("success", "message", "description")):
                return result
        return {}

    @staticmethod
    def _compact_page_text(text: str, limit: int = 300) -> str:
        value = " ".join(str(text or "").split())
        return f"{value[:limit]}..." if len(value) > limit else value

    @staticmethod
    def _hdhive_action_hash(page: Any, attempts: int = 3) -> str:
        script = """
            async () => {
              const pattern = /createServerReference\\)\\(\\s*[\"']([0-9a-f]{40,})[\"'][\\s\\S]{0,400}?[\"']checkIn[\"']/;
              const urls = Array.from(new Set([
                ...Array.from(document.scripts).map(script => script.src),
                ...performance.getEntriesByType('resource').map(entry => entry.name),
              ])).filter(url => {
                try {
                  const pathname = new URL(url, location.href).pathname;
                  return pathname.includes('/_next/static/chunks/') && pathname.endsWith('.js');
                } catch (_) {
                  return false;
                }
              });
              const sources = await Promise.all(urls.map(async url => {
                try {
                  const response = await fetch(url, {
                    cache: 'no-store',
                    credentials: 'same-origin',
                  });
                  return response.ok ? await response.text() : '';
                } catch (_) {
                  return '';
                }
              }));
              for (const source of sources) {
                const match = source.match(pattern);
                if (match) return match[1];
              }
              return '';
            }
            """
        total_attempts = max(1, int(attempts or 1))
        for attempt in range(total_attempts):
            try:
                action_hash = str(page.evaluate(script) or "")
            except Exception:
                action_hash = ""
            if action_hash:
                return action_hash
            if attempt + 1 < total_attempts:
                try:
                    page.wait_for_timeout(750)
                except Exception:
                    pass
        return ""

    @staticmethod
    def _hdhive_direct_checkin(
        page: Any,
        action_hash: str,
        gamble: bool,
    ) -> Tuple[bool, str, int]:
        result = page.evaluate(
            """
            async ({ actionHash, gamble }) => {
              const response = await fetch('/', {
                method: 'POST',
                headers: {
                  'Accept': 'text/x-component',
                  'Content-Type': 'text/plain;charset=UTF-8',
                  'next-action': actionHash,
                },
                body: JSON.stringify([gamble]),
              });
              const text = await response.text();
              const contentType = response.headers.get('content-type') || '';
              if (contentType.includes('application/json')) {
                return { status: response.status, contentType, text };
              }
              const businessLines = text.split('\\n').filter(line =>
                /^\\w+:\\{/.test(line) &&
                /\"(error|response|success|message|description)\"/.test(line)
              );
              return { status: response.status, contentType, text: businessLines.join('\\n') };
            }
            """,
            {"actionHash": action_hash, "gamble": gamble},
        )
        status = int(result.get("status") or 0) if isinstance(result, dict) else 0
        text = str(result.get("text") or "") if isinstance(result, dict) else ""
        content_type = str(result.get("contentType") or "") if isinstance(result, dict) else ""
        if "application/json" in content_type:
            try:
                data = json.loads(text or "{}")
            except Exception:
                data = {}
        else:
            data = AggregateSignClient._hdhive_parse_rsc_result(text)
        if not data:
            return False, f"影巢签到返回格式异常，HTTP {status}", status
        success, message = AggregateSignClient._hdhive_result(data, status)
        if status >= 400 and f"HTTP {status}" not in message:
            message = f"{message} (HTTP {status})"
        return success, message, status

    def _capture_hdhive_state(self, context: Any, authenticated: bool = False) -> None:
        if not authenticated:
            return
        try:
            cookie_str = self._cookie_list_to_str(context.cookies())
            if not self._parse_cookie_str(cookie_str).get("token"):
                return
            self._updated_cookie_str = cookie_str
        except Exception:
            return
        try:
            try:
                state = context.storage_state(indexed_db=True)
            except TypeError:
                state = context.storage_state()
            if isinstance(state, dict):
                self._updated_storage_state = json.dumps(state, ensure_ascii=False)
        except Exception:
            pass

    def get_updated_login_state(self) -> Tuple[str, str]:
        return self._updated_cookie_str, self._updated_storage_state

    @staticmethod
    def _hdhive_token_expired(token: str) -> bool:
        try:
            payload = token.split(".", 2)[1]
            payload += "=" * (-len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
            expires_at = float(decoded.get("exp") or 0)
            return bool(expires_at and expires_at <= time.time())
        except Exception:
            return False

    @staticmethod
    def _hdhive_user_menu(page: Any) -> Any:
        return page.locator(
            "button[aria-label='打开用户菜单'], "
            "button[aria-label='用户菜单'], "
            "button[data-slot='drawer-trigger']:not([aria-label='打开导航'])"
        ).first

    @staticmethod
    def _dismiss_hdhive_announcement(page: Any) -> None:
        selector = "[role='dialog'] button"
        for attempt in range(15):
            try:
                button = page.locator(selector).filter(
                    has_text=re.compile(r"^\s*我知道了(?:\s*\(\d+s\))?\s*$")
                ).first
                if button.count() > 0 and button.is_visible():
                    button.click(timeout=5000)
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        pass
                    return
            except Exception:
                pass
            if attempt + 1 < 15:
                try:
                    page.wait_for_timeout(300)
                except Exception:
                    pass

    def _hdhive_checkin(
        self,
        cookie_str: str,
        storage_state: str = "",
        methods: Optional[list[str]] = None,
    ) -> Tuple[bool, str]:
        cookies = self._parse_cookie_str(cookie_str)
        token = cookies.get("token")
        if not token:
            return False, "Cookie 缺少 token，登录已失效或 Cookie 不完整"
        if self._hdhive_token_expired(token):
            return False, "Cookie 无效或登录已过期"
        methods = methods or ["normal"]
        gamble = any(str(method).lower() == "gamble" for method in methods)
        label = "赌狗签到" if gamble else "每日签到"
        self._updated_cookie_str = ""
        self._updated_storage_state = ""
        authenticated = False
        try:
            with AggregateSignClient._browser_runtime() as playwright:
                with AggregateSignClient._socks5_slippers_if_needed() as slip:
                    proxy = slip if slip is not None else AggregateSignClient._playwright_proxy_settings()
                    browser, context = self._make_context(
                        playwright,
                        proxy,
                        storage_state=storage_state,
                    )
                    try:
                        self._add_cookies(context, cookie_str)
                        page = context.new_page()
                        self._goto_page(page, self.base_url, wait_until="domcontentloaded")
                        try:
                            page.wait_for_load_state("load", timeout=30000)
                        except Exception:
                            pass
                        if "/login" in page.url:
                            return False, "Cookie 无效或登录已过期，站点跳转到登录页"

                        self._dismiss_hdhive_announcement(page)
                        page_text = self._extract_page_message(page)
                        if self._is_already_signed_text(page_text):
                            authenticated = True
                            return True, "今日已签到"

                        action_hash = self._hdhive_action_hash(page)
                        if action_hash:
                            authenticated = True
                            success, message, status = self._hdhive_direct_checkin(
                                page,
                                action_hash,
                                gamble,
                            )
                            if status in (0, 401, 403, 409):
                                authenticated = False
                            if status == 409:
                                return False, f"登录安全会话已失效: {message}"
                            return success, message

                        user_menu = self._hdhive_user_menu(page)
                        try:
                            user_menu.wait_for(state="visible", timeout=15000)
                        except Exception:
                            pass
                        try:
                            menu_available = user_menu.count() > 0 and user_menu.is_visible()
                        except Exception:
                            menu_available = False
                        if not menu_available:
                            # 页面脚本可能刚完成加载，再探测一次动态 Server Action。
                            action_hash = self._hdhive_action_hash(page, attempts=2)
                            if action_hash:
                                authenticated = True
                                success, message, status = self._hdhive_direct_checkin(
                                    page,
                                    action_hash,
                                    gamble,
                                )
                                if status in (0, 401, 403, 409):
                                    authenticated = False
                                if status == 409:
                                    return False, f"登录安全会话已失效: {message}"
                                return success, message
                            return False, "Cookie 无效或登录已过期，未检测到影巢登录入口"

                        authenticated = True
                        try:
                            user_menu.click(timeout=10000)
                            page.wait_for_timeout(300)
                        except Exception as err:
                            self._dismiss_hdhive_announcement(page)
                            try:
                                user_menu.wait_for(state="visible", timeout=5000)
                                user_menu.click(timeout=10000)
                                page.wait_for_timeout(300)
                            except Exception as retry_err:
                                error = self._compact_page_text(str(retry_err or err), limit=200)
                                return False, f"打开影巢用户菜单失败: {error}"

                        labels = ("赌狗签到", "赌狗") if gamble else ("每日签到", "立即签到", "签到")
                        button = None
                        for button_text in labels:
                            locator = page.locator("button").filter(
                                has_text=re.compile(rf"^\s*{re.escape(button_text)}\s*$")
                            ).first
                            try:
                                if locator.count() > 0 and locator.is_visible():
                                    button = locator
                                    break
                            except Exception:
                                continue
                        if button is None:
                            buttons = page.locator("button")
                            for index in range(buttons.count()):
                                candidate = buttons.nth(index)
                                try:
                                    text = (candidate.inner_text(timeout=1000) or "").strip()
                                    matches_mode = (
                                        "赌狗" in text
                                        if gamble
                                        else "签到" in text and "赌狗" not in text
                                    )
                                    if matches_mode and candidate.is_visible():
                                        button = candidate
                                        break
                                except Exception:
                                    continue
                        if button is None:
                            return False, f"未找到{label}按钮，影巢页面可能已更新"

                        button.click(timeout=10000)
                        page.wait_for_timeout(4000)
                        result_text = self._extract_page_message(page)
                        if self._is_already_signed_text(result_text):
                            return True, self._compact_page_text(result_text)
                        if any(keyword in result_text for keyword in ("签到成功", "获得", "积分")):
                            return True, self._compact_page_text(result_text)
                        return False, f"{label}后未识别到签到结果"
                    finally:
                        self._capture_hdhive_state(context, authenticated=authenticated)
                        browser.close()
        except AggregateSignBrowserError as err:
            return False, str(err)
        except Exception as err:
            return False, f"影巢签到异常: {err}"

    @staticmethod
    def _jwt_subject(token: str) -> str:
        try:
            payload = token.split(".", 2)[1]
            payload += "=" * (-len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
            return str(decoded.get("sub") or decoded.get("user_id") or "")
        except Exception:
            return ""

    def _hdhive_profile(self, cookie_str: str) -> Dict[str, Any]:
        cookies = self._parse_cookie_str(cookie_str)
        token = cookies.get("token")
        if not token:
            return {}
        user_id = self._jwt_subject(token)
        profile_url = f"{self.base_url}/user/{user_id}" if user_id else f"{self.base_url}/"
        headers = {
            "User-Agent": self._UA,
            "Accept": "text/x-component",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": self.base_url,
            "Referer": profile_url,
            "rsc": "1",
        }
        status, text = self._hdhive_request(profile_url, cookie_str, headers)
        if status >= 400:
            return {}
        profile: Dict[str, Any] = {}
        patterns = {
            "nickname": r'"nickname":"([^\"]+)"',
            "points": r'"points":(-?\d+)',
            "signin_days_total": r'"signin_days_total":(\d+)',
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, text)
            if not match:
                continue
            value: Any = match.group(1)
            if key != "nickname":
                value = int(value)
            profile[key] = value
        return profile

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
        if self.site_key == "dian115":
            status, data, _ = self._dian115_api_request(
                path="/me",
                method="GET",
                cookie_str=cookie_str,
            )
            if status >= 400 or not isinstance(data, dict):
                return self._dian115_browser_profile(
                    cookie_str=cookie_str,
                    storage_state=storage_state,
                )
            profile = self._dian115_profile_from_response(data)
            if profile.get("points") is None or profile.get("today_signin_points") is None:
                browser_profile = self._dian115_browser_profile(
                    cookie_str=cookie_str,
                    storage_state=storage_state,
                )
                for key, value in browser_profile.items():
                    if value not in (None, "", "—"):
                        profile[key] = value
            return profile
        if self.site_key == "hdhive":
            return self._hdhive_profile(cookie_str)

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
            "[role='alert']",
            ".MuiAlert-message",
            ".MuiSnackbar-root",
            ".Toastify__toast-body",
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

    @staticmethod
    def _dismiss_juying_announcements(page: Any) -> None:
        for _ in range(10):
            try:
                announcement = page.locator(".announcement-dialog:visible").last
                if announcement.count() == 0:
                    return
                close_button = announcement.locator("button[aria-label*='关闭']").first
                if close_button.count() == 0:
                    close_button = announcement.locator("button:has-text('关闭')").first
                if close_button.count() == 0:
                    return
                close_button.click(timeout=5000)
                page.wait_for_timeout(300)
            except Exception:
                return

    @staticmethod
    def _juying_checkin_result(status: int, data: Dict[str, Any]) -> Tuple[bool, str]:
        if status in (401, 403):
            return False, "Cookie 无效或登录已过期"

        payload = data if isinstance(data, dict) else {}
        message = str(payload.get("message") or "").strip()
        already_signed = AggregateSignClient._is_already_signed_text(message)
        success = already_signed or (
            status < 400
            and (payload.get("status") == "success" or payload.get("success") is True)
        )
        if success:
            return True, message or ("今日已签到" if already_signed else "签到成功")
        return False, message or f"聚影签到接口 HTTP {status}"

    def checkin(
        self,
        cookie_str: str,
        storage_state: str = "",
        methods: Optional[list[str]] = None,
    ) -> Tuple[bool, str]:
        if self.site_key == "dian115":
            return self._dian115_checkin(
                cookie_str=cookie_str,
                storage_state=storage_state,
                methods=methods,
            )
        if self.site_key == "hdhive":
            return self._hdhive_checkin(
                cookie_str=cookie_str,
                storage_state=storage_state,
                methods=methods,
            )

        if not cookie_str:
            return False, "未配置 Cookie"

        try:
            token = self._token_from_storage_state(storage_state)
            auth_status, _, _ = self._api_request_with_login_state(
                path="/api/app/profile/",
                method="GET",
                cookie_str=cookie_str,
                token=token,
            )
            if auth_status in (401, 403):
                return False, "Cookie 无效或登录已过期"

            with AggregateSignClient._browser_runtime() as playwright:
                with AggregateSignClient._socks5_slippers_if_needed() as slip:
                    proxy = slip if slip is not None else AggregateSignClient._playwright_proxy_settings()
                    browser, context = self._make_context(playwright, proxy)
                    try:
                        self._add_cookies(context, cookie_str)
                        self._restore_storage_state(context, storage_state)
                        page = context.new_page()
                        self._goto_page(page, f"{self.base_url}{self.checkin_path}")
                        self._wait_network_idle(page)

                        if "/login" in page.url:
                            return False, "Cookie 无效或登录已过期，站点跳转到登录页"

                        page_text = self._extract_page_message(page)
                        if AggregateSignClient._is_already_signed_text(page_text):
                            return True, page_text or "今日已签到"

                        button = page.locator("button:has-text('立即签到')").first
                        try:
                            button.wait_for(state="visible", timeout=15000)
                        except PlaywrightTimeoutError:
                            page_text = self._extract_page_message(page)
                            if AggregateSignClient._is_already_signed_text(page_text):
                                return True, page_text or "今日已签到"
                            return False, page_text or "未找到立即签到按钮"

                        self._dismiss_juying_announcements(page)
                        checkin_response = None
                        try:
                            with page.expect_response(
                                lambda response: (
                                    response.request.method == "POST"
                                    and urlparse(response.url).path.rstrip("/") == "/api/app/checkin/do"
                                ),
                                timeout=15000,
                            ) as response_info:
                                button.click(timeout=10000)
                            checkin_response = response_info.value
                        except PlaywrightTimeoutError:
                            checkin_response = None

                        if checkin_response is not None:
                            try:
                                response_data = checkin_response.json()
                            except Exception:
                                response_data = {}
                            if response_data or checkin_response.status >= 400:
                                return AggregateSignClient._juying_checkin_result(
                                    checkin_response.status,
                                    response_data,
                                )

                        try:
                            page.wait_for_timeout(2000)
                        except Exception:
                            pass

                        result_text = self._extract_page_message(page)
                        success_keywords = (
                            "签到成功", "成功", "已签到", "已经签到", "今日已签", "已完成", "获得", "积分"
                        )
                        if any(keyword in result_text for keyword in success_keywords):
                            return True, result_text or "签到请求已完成"
                        return False, result_text or "签到后未识别到成功提示"
                    finally:
                        browser.close()
        except AggregateSignBrowserError as err:
            return False, str(err)
        except Exception as err:
            return False, f"签到异常: {err}"

    def _wait_for_login_form(self, page: Any, login_url: str) -> None:
        username_selector = (
            "input[name='username'], input[autocomplete='username'], input[type='email']"
        )
        password_selector = (
            "input[name='password'], input[autocomplete='current-password'], input[type='password']"
        )
        submit_selector = (
            "button[type='submit'], button:has-text('登录'), button:has-text('Login')"
        )
        last_error: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                try:
                    page.wait_for_load_state("load", timeout=30000)
                except Exception:
                    pass
                page.wait_for_selector(
                    username_selector,
                    state="visible",
                    timeout=30000,
                )
                page.wait_for_selector(
                    password_selector,
                    state="visible",
                    timeout=30000,
                )
                page.wait_for_selector(
                    submit_selector,
                    state="visible",
                    timeout=15000,
                )
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                return
            except Exception as err:
                last_error = err
                if attempt >= 2:
                    break
                try:
                    self._goto_page(page, login_url)
                except Exception as goto_err:
                    last_error = goto_err

        try:
            raw_page_url = str(page.url or "")
            parsed_page_url = urlparse(raw_page_url)
            page_url = urlunparse((
                parsed_page_url.scheme,
                parsed_page_url.netloc,
                parsed_page_url.path or "/",
                "",
                "",
                "",
            ))
        except Exception:
            page_url = ""
        try:
            page_title = str(page.title() or "")
        except Exception:
            page_title = ""
        try:
            input_count = page.locator("input").count()
        except Exception:
            input_count = -1
        try:
            page_text = self._extract_page_message(page).lower()
        except Exception:
            page_text = ""
        diagnostic = (
            "登录页未准备好用户名/密码输入框"
            f"（url={page_url or '未知'}, title={page_title or '未知'}, input_count={input_count}）"
        )
        if any(
            keyword in f"{page_url} {page_title} {page_text}"
            for keyword in (
                "captcha",
                "challenge",
                "access denied",
                "blocked",
                "forbidden",
                "验证码",
                "安全验证",
                "地区限制",
                "大陆ip",
                "屏蔽",
            )
        ):
            diagnostic += "；可能被站点安全验证或地区限制拦截，请检查代理或先在同一环境手动登录"
        elif last_error:
            diagnostic += "；登录页脚本可能未完成加载，请检查站点访问和浏览器运行环境"
        raise AggregateSignBrowserError(diagnostic) from last_error

    def _wait_for_hdhive_login_state(self, context: Any, page: Any, timeout: int = 30000) -> bool:
        deadline = time.time() + max(1, timeout) / 1000
        while time.time() < deadline:
            try:
                cookie_names = {
                    str(cookie.get("name") or "")
                    for cookie in context.cookies()
                    if isinstance(cookie, dict)
                }
                if "token" in cookie_names:
                    # token 已由登录 Server Action 写入；菜单属于异步 UI，不作为登录成功的必要条件。
                    return True
            except Exception:
                pass
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
        return False

    def login(self, username: str, password: str) -> Tuple[bool, str, str, str]:
        if not username or not password:
            return False, "", "", "未配置用户名或密码"

        api_login_message = ""
        if self.site_key == "dian115":
            try:
                success, cookie_str, storage_state, message = self._dian115_login(
                    username=username,
                    password=password,
                )
                if success:
                    return success, cookie_str, storage_state, message
                api_login_message = message
            except Exception:
                api_login_message = "登录接口请求异常"

        if self.site_key not in ("dian115", "hdhive"):
            try:
                success, cookie_str, storage_state, message = self._login_by_api_cookie(username, password)
                if success:
                    return True, cookie_str, storage_state, message
            except Exception as err:
                message = f"登录接口异常: {err}"

        def browser_failure_message(message: str) -> str:
            if api_login_message:
                return f"{message}；dian115 API 登录失败：{api_login_message}"
            return message

        try:
            with AggregateSignClient._browser_runtime() as playwright:
                with AggregateSignClient._socks5_slippers_if_needed() as slip:
                    proxy = slip if slip is not None else AggregateSignClient._playwright_proxy_settings()
                    browser, context = self._make_context(playwright, proxy)
                    try:
                        page = context.new_page()
                        login_url = f"{self.base_url}{self.login_path}"
                        self._goto_page(page, login_url)
                        self._wait_for_login_form(page, login_url)

                        if not self._fill_login_form(page, username, password):
                            return False, "", "", browser_failure_message("未找到可用的登录输入框")

                        try:
                            page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
                        except Exception:
                            pass
                        try:
                            page.wait_for_load_state("networkidle", timeout=30000)
                        except Exception:
                            pass

                        if "/login" in page.url:
                            page_text = self._extract_page_message(page)
                            if any(keyword in page_text for keyword in ("错误", "失败", "密码", "验证码")):
                                return False, "", "", browser_failure_message(page_text)
                            return False, "", "", browser_failure_message(
                                "登录后仍停留在登录页，可能账号密码错误、站点页面变化或站点响应过慢"
                            )

                        if self.site_key == "hdhive" and not self._wait_for_hdhive_login_state(context, page):
                            page_text = self._extract_page_message(page)
                            detail = "影巢登录后未获取到完整登录态"
                            if page_text:
                                detail = f"{detail}: {self._compact_page_text(page_text, limit=160)}"
                            return False, "", "", browser_failure_message(detail)

                        self._goto_page(
                            page,
                            f"{self.base_url}{self.checkin_path}",
                            wait_until="domcontentloaded",
                        )
                        try:
                            page.wait_for_load_state("load", timeout=30000)
                        except Exception:
                            pass
                        if "/login" in page.url:
                            page_text = self._extract_page_message(page)
                            return False, "", "", browser_failure_message(
                                page_text or "账号密码登录未保持有效登录态"
                            )

                        raw_cookies = context.cookies()
                        cookie_str = self._cookie_list_to_str(raw_cookies)
                        if not cookie_str:
                            return False, "", "", browser_failure_message("登录后未获取到 Cookie")
                        if self.site_key == "hdhive" and "token" not in self._parse_cookie_str(cookie_str):
                            return False, "", "", "影巢登录后未获取到 token Cookie"
                        try:
                            if self.site_key == "hdhive":
                                try:
                                    state = context.storage_state(indexed_db=True)
                                except TypeError:
                                    state = context.storage_state()
                            else:
                                state = context.storage_state()
                            storage_state = json.dumps(state, ensure_ascii=False)
                        except Exception:
                            storage_state = ""
                        return True, cookie_str, storage_state, "登录成功"
                    finally:
                        browser.close()
        except AggregateSignBrowserError as err:
            return False, "", "", browser_failure_message(str(err))
        except Exception as err:
            return False, "", "", browser_failure_message(f"登录异常: {err}")

    @staticmethod
    def _fill_login_form(page: Any, username: str, password: str) -> bool:
        user_selectors = [
            "input[name='username']",
            "input[autocomplete='username']",
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
