from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from .config import AppConfig, DeviceConfig, HTTP_DEVICE_TYPES


class HTTPBackendError(RuntimeError):
    """Raised when an HTTP device request cannot be completed safely."""


HTTPMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
BodyEncoding = Literal["text", "base64"]

_BLOCKED_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "host",
    "proxy-authorization",
}
_SENSITIVE_RESPONSE_HEADERS = {
    "authentication-info",
    "proxy-authenticate",
    "set-cookie",
    "x-xsrf-token",
    "x_xsrf_token",
}
_TEXT_MEDIA_TYPES = (
    "application/ecmascript",
    "application/javascript",
    "application/x-javascript",
    "application/json",
    "application/x-www-form-urlencoded",
    "application/xml",
    "image/svg+xml",
)


@dataclass(frozen=True)
class HTTPResponseResult:
    device: str
    method: str
    path: str
    status_code: int
    headers: dict[str, str]
    body: str
    body_encoding: BodyEncoding
    truncated: bool
    elapsed_ms: int


def _tplink_password_encode(password: str) -> str:
    salt = "RDpbLfCPsJZ7fiv"
    alphabet = (
        "yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4r"
        "BL3cD02KZciXTysVXiV8ngg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro5"
        "10qXeU07kV57fQMc8L6aLgMLwygtc0F10a0Dg70TOoouyFhdysuRMO51yY5ZlOZZL"
        "Eal1h0t9YQW0Ko7oBwmCAHoic4HYbUyVeU3sfQ1xtXcPcf1aT303wAQhv66qzW"
    )
    encoded: list[str] = []
    for index in range(max(len(password), len(salt))):
        password_byte = ord(password[index]) if index < len(password) else 187
        salt_byte = ord(salt[index]) if index < len(salt) else 187
        encoded.append(alphabet[(password_byte ^ salt_byte) % len(alphabet)])
    return "".join(encoded)


class HTTPDeviceBackend:
    def __init__(
        self,
        config: AppConfig,
        device: DeviceConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.device = device
        scheme = device.scheme or (
            "https" if device.type == "http-mellanox-onyx" else "http"
        )
        port = device.port or (443 if scheme == "https" else 80)
        self.base_url = f"{scheme}://{device.host}:{port}"
        backend = config.backends.http
        self.client = client or httpx.AsyncClient(
            base_url=self.base_url,
            verify=device.verify_tls,
            timeout=httpx.Timeout(
                backend.default_request_timeout_seconds,
                connect=backend.connect_timeout_seconds,
            ),
            follow_redirects=False,
        )
        self._authenticated = False
        self._auth_lock = asyncio.Lock()
        self._secrets: set[str] = set()

    async def close(self) -> None:
        try:
            await self._logout()
        finally:
            await self.client.aclose()

    async def ensure_authenticated(self, *, force: bool = False) -> None:
        if self._authenticated and not force:
            return
        async with self._auth_lock:
            if self._authenticated and not force:
                return
            if self._authenticated:
                await self._logout()
            self.client.cookies.clear()
            self._authenticated = False
            await self._authenticate()
            self._authenticated = True
            self._remember_cookie_secrets()

    async def _logout(self) -> None:
        if not self._authenticated:
            return
        try:
            if self.device.type == "http-tplink-switch":
                await self.client.get("/Logout.htm")
            elif self.device.type == "http-zte-be7200":
                await self.client.post(
                    "/?_type=loginData&_tag=logout_entry",
                    data={
                        "IF_LogOff": "1",
                        "_sessionTOKEN": self._zte_token,
                    },
                )
            elif self.device.type == "http-mellanox-onyx":
                await self.client.get(
                    "/admin/launch?script=rh&template=logout&action=logout"
                )
        except httpx.HTTPError:
            pass
        finally:
            self._authenticated = False

    async def _authenticate(self) -> None:
        if self.device.type == "http-tplink-switch":
            await self._authenticate_tplink()
            return
        if self.device.type == "http-zte-be7200":
            await self._authenticate_zte()
            return
        if self.device.type == "http-mellanox-onyx":
            await self._authenticate_mellanox()
            return
        raise HTTPBackendError(f"unsupported HTTP backend: {self.device.type}")

    def _credentials(self, *, default_username: str | None = None) -> tuple[str, str]:
        username = self.device.account.username or default_username
        password = self.device.account.password
        if not username or password is None:
            raise HTTPBackendError(
                f"backend {self.device.type} for {self.device.name} requires a password"
                " and username"
            )
        return username, password

    async def _authenticate_tplink(self) -> None:
        username, password = self._credentials()
        response = await self.client.post(
            "/logon.cgi",
            data={
                "username": username,
                "password": _tplink_password_encode(password),
                "logon": "Login",
            },
            follow_redirects=True,
        )
        self._capture_tplink_token(response)
        if response.status_code < 400 and not getattr(self, "_tplink_token", None):
            response = await self.client.get("/")
            self._capture_tplink_token(response)
        if (
            response.status_code >= 400
            or "SessionID" not in self.client.cookies
            or not getattr(self, "_tplink_token", None)
        ):
            raise HTTPBackendError(
                f"TP-Link authentication failed for {self.device.name}"
            )

    async def _authenticate_zte(self) -> None:
        username, password = self._credentials(default_username="admin")
        token_response = await self.client.get(
            "/?_type=loginsceneData&_tag=login_token_json"
        )
        try:
            token_data = token_response.json()
            session_token = str(token_data["_sessionToken"])
            login_token = str(token_data["logintoken"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPBackendError(
                f"ZTE login-token request failed for {self.device.name}"
            ) from exc

        self._set_zte_token(session_token)
        password_digest = hashlib.sha256(
            (password + login_token).encode("utf-8")
        ).hexdigest()
        self._secrets.update({login_token, password_digest})
        response = await self.client.post(
            "/?_type=loginData&_tag=login_entry",
            data={
                "Username": username,
                "Password": password_digest,
                "action": "login",
                "Frm_Logintoken": "",
                "captchaCode": "",
                "_sessionTOKEN": session_token,
            },
        )
        self._capture_zte_token(response)
        try:
            result = response.json()
        except ValueError as exc:
            raise HTTPBackendError(
                "ZTE authentication returned an invalid response for "
                f"{self.device.name}"
            ) from exc
        if response.status_code >= 400 or result.get("login_need_refresh") is not True:
            message = result.get("loginErrMsg") or "credentials were rejected"
            raise HTTPBackendError(
                f"ZTE authentication failed for {self.device.name}: {message}"
            )
        if result.get("sess_token"):
            self._set_zte_token(str(result["sess_token"]))

    async def _authenticate_mellanox(self) -> None:
        username, password = self._credentials()
        response = await self.client.post(
            "/admin/launch?script=rh&template=login&action=login",
            data={
                "d_user_id": "user_id",
                "t_user_id": "string",
                "c_user_id": "string",
                "e_user_id": "true",
                "f_user_id": username,
                "f_password": password,
                "Login": "Login",
            },
            follow_redirects=True,
        )
        if (
            response.status_code >= 400
            or "session" not in self.client.cookies
            or self._is_mellanox_login_response(response)
        ):
            raise HTTPBackendError(
                f"Mellanox Onyx authentication failed for {self.device.name}"
            )

    def _set_zte_token(self, token: str) -> None:
        old_token = getattr(self, "_zte_token", None)
        if old_token:
            self._secrets.discard(old_token)
        self._zte_token = token
        self._secrets.add(token)

    def _set_tplink_token(self, token: str) -> None:
        old_token = getattr(self, "_tplink_token", None)
        if old_token:
            self._secrets.discard(old_token)
        self._tplink_token = token
        self._secrets.add(token)

    def _capture_tplink_token(self, response: httpx.Response) -> None:
        match = re.search(r"\bg_tid\s*=\s*['\"]?(\d+)", response.text)
        if match:
            self._set_tplink_token(match.group(1))

    def _capture_zte_token(self, response: httpx.Response) -> None:
        token = response.headers.get("x_xsrf_token") or response.headers.get(
            "x-xsrf-token"
        )
        if token:
            self._set_zte_token(token)

    def _remember_cookie_secrets(self) -> None:
        for cookie in self.client.cookies.jar:
            if cookie.value:
                self._secrets.add(cookie.value)

    def _prepare_query(
        self,
        path: str,
        query: dict[str, str | list[str]] | None,
    ) -> dict[str, str | list[str]] | None:
        if self.device.type != "http-tplink-switch" or not urlsplit(path).path.endswith(
            ".cgi"
        ):
            return query
        prepared = dict(query or {})
        prepared["token"] = self._tplink_token
        return prepared

    def _prepare_form(
        self, method: str, path: str, form: dict[str, str] | None
    ) -> dict[str, str] | None:
        if form is None:
            return None
        prepared = dict(form)
        if self.device.type == "http-tplink-switch" and urlsplit(path).path.endswith(
            ".cgi"
        ):
            prepared["token"] = self._tplink_token
        if self.device.type == "http-zte-be7200" and method == "POST":
            prepared["_sessionTOKEN"] = self._zte_token
        return prepared

    def _prepare_content(
        self,
        method: str,
        headers: dict[str, str],
        content: bytes | None,
    ) -> bytes | None:
        if (
            content is not None
            and self.device.type == "http-zte-be7200"
            and method == "POST"
            and headers.get("content-type", "").split(";", 1)[0].strip().lower()
            == "application/x-www-form-urlencoded"
        ):
            fields = dict(parse_qsl(content.decode("utf-8"), keep_blank_values=True))
            fields["_sessionTOKEN"] = self._zte_token
            return urlencode(fields).encode("utf-8")
        return content

    async def request(
        self,
        method: HTTPMethod,
        path: str,
        *,
        query: dict[str, str | list[str]] | None = None,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        body_base64: str | None = None,
        form: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> HTTPResponseResult:
        method = method.upper()  # type: ignore[assignment]
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise HTTPBackendError(f"unsupported HTTP method: {method}")
        self._validate_path(path)
        supplied_bodies = sum(value is not None for value in (body, body_base64, form))
        if supplied_bodies > 1:
            raise HTTPBackendError("provide only one of body, body_base64, or form")

        clean_headers = self._validate_headers(headers or {})
        if body_base64 is not None:
            try:
                content = base64.b64decode(body_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPBackendError("body_base64 is not valid base64") from exc
        elif body is not None:
            content = body.encode("utf-8")
        else:
            content = None

        await self.ensure_authenticated()
        started = time.monotonic()
        try:
            response = await self._send(
                method,
                path,
                query=query,
                headers=clean_headers,
                content=content,
                form=form,
                timeout_seconds=timeout_seconds,
            )
            if self._is_auth_failure(response):
                await self.ensure_authenticated(force=True)
                response = await self._send(
                    method,
                    path,
                    query=query,
                    headers=clean_headers,
                    content=content,
                    form=form,
                    timeout_seconds=timeout_seconds,
                )
        except httpx.HTTPError as exc:
            raise HTTPBackendError(
                f"HTTP request failed on {self.device.name}: {exc}"
            ) from exc

        return self._result(response, method, path, started)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | list[str]] | None,
        headers: dict[str, str],
        content: bytes | None,
        form: dict[str, str] | None,
        timeout_seconds: float | None,
    ) -> httpx.Response:
        prepared_headers = dict(headers)
        prepared_query = self._prepare_query(path, query)
        prepared_form = self._prepare_form(method, path, form)
        prepared_content = self._prepare_content(method, prepared_headers, content)
        request_options: dict[str, object] = {
            "params": prepared_query,
            "headers": prepared_headers,
            "content": prepared_content,
            "data": prepared_form,
        }
        if timeout_seconds is not None:
            request_options["timeout"] = timeout_seconds
        response = await self.client.request(method, path, **request_options)
        if self.device.type == "http-tplink-switch":
            self._capture_tplink_token(response)
        elif self.device.type == "http-zte-be7200":
            self._capture_zte_token(response)
            if response.url.params.get("_tag") == "login_token_json":
                try:
                    token_data = response.json()
                    self._secrets.add(str(token_data["logintoken"]))
                    self._set_zte_token(str(token_data["_sessionToken"]))
                except (KeyError, TypeError, ValueError):
                    pass
        self._remember_cookie_secrets()
        return response

    def _is_auth_failure(self, response: httpx.Response) -> bool:
        if response.status_code in {401, 403}:
            return True
        if self.device.type == "http-tplink-switch":
            return (
                response.url.path.endswith("/logon.cgi")
                or 'action="/logon.cgi"' in response.text
            )
        if self.device.type == "http-zte-be7200":
            return "SessionTimeout" in response.text
        if self.device.type == "http-mellanox-onyx":
            return self._is_mellanox_login_response(response)
        return False

    @staticmethod
    def _is_mellanox_login_response(response: httpx.Response) -> bool:
        return (
            "template=login" in str(response.url)
            or "Please enter your username and password" in response.text
        )

    @staticmethod
    def _validate_path(path: str) -> None:
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
            raise HTTPBackendError(
                "path must be an absolute path on the selected device"
            )
        if parsed.fragment:
            raise HTTPBackendError("path must not contain a URL fragment")

    @staticmethod
    def _validate_headers(headers: dict[str, str]) -> dict[str, str]:
        clean: dict[str, str] = {}
        for name, value in headers.items():
            normalized = name.strip().lower()
            if normalized in _BLOCKED_REQUEST_HEADERS:
                raise HTTPBackendError(
                    f"header {name} is managed by the HTTP backend and cannot be "
                    "supplied"
                )
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise HTTPBackendError(f"invalid HTTP header name: {name}")
            clean[normalized] = value
        return clean

    def _result(
        self,
        response: httpx.Response,
        method: str,
        path: str,
        started: float,
    ) -> HTTPResponseResult:
        limit = self.config.backends.http.max_response_bytes
        raw = response.content
        truncated = len(raw) > limit
        raw = raw[:limit]
        media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        textual = media_type.startswith("text/") or media_type in _TEXT_MEDIA_TYPES
        if textual:
            encoding = response.encoding or "utf-8"
            body = raw.decode(encoding, errors="replace")
            body_encoding: BodyEncoding = "text"
            body = self._redact(body)
        else:
            body = base64.b64encode(raw).decode("ascii")
            body_encoding = "base64"
        headers = {
            name: self._redact(value)
            for name, value in response.headers.items()
            if name.lower() not in _SENSITIVE_RESPONSE_HEADERS
        }
        return HTTPResponseResult(
            device=self.device.name,
            method=method,
            path=path,
            status_code=response.status_code,
            headers=headers,
            body=body,
            body_encoding=body_encoding,
            truncated=truncated,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )

    def _redact(self, value: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            if len(secret) >= 4:
                value = value.replace(secret, "[REDACTED]")
        return value


class HTTPBackendManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._backends: dict[str, HTTPDeviceBackend] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        device_name: str,
        method: HTTPMethod,
        path: str,
        **kwargs: object,
    ) -> HTTPResponseResult:
        backend = await self._get_backend(device_name)
        return await backend.request(method, path, **kwargs)  # type: ignore[arg-type]

    async def _get_backend(self, device_name: str) -> HTTPDeviceBackend:
        try:
            device = self.config.devices[device_name]
        except KeyError as exc:
            raise HTTPBackendError(f"unknown device: {device_name}") from exc
        if device.type not in HTTP_DEVICE_TYPES:
            raise HTTPBackendError(
                f"device {device_name} has type {device.type}, expected an HTTP backend"
            )
        async with self._lock:
            backend = self._backends.get(device_name)
            if backend is None:
                backend = HTTPDeviceBackend(self.config, device)
                self._backends[device_name] = backend
            return backend

    async def close_all(self) -> None:
        async with self._lock:
            backends = list(self._backends.values())
            self._backends.clear()
        await asyncio.gather(*(backend.close() for backend in backends))
