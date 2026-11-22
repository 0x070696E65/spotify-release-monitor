"""Spotify Web API, PKCE authorization, and local configuration."""

import base64
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import webbrowser


API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8888
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
SCOPES = "user-follow-read user-read-private playlist-read-private playlist-modify-private"
LOG = logging.getLogger(__name__)


class MonitorError(Exception):
    pass


class ApiError(MonitorError):
    def __init__(self, message, *, status=None, reason=None):
        super().__init__(message)
        self.status = status
        self.reason = reason


class Settings:
    def __init__(self, path):
        self.path = path
        if not path.is_file():
            raise MonitorError(".env がありません。README に従って .env.example をコピーしてください。")
        self.lines = path.read_text(encoding="utf-8").splitlines()
        self.values = {}
        for line in self.lines:
            if line.lstrip().startswith("#") or not line.strip():
                continue
            key, separator, value = line.partition("=")
            if not separator or not key.strip():
                raise MonitorError(".env の設定は1行ずつ KEY=VALUE で記述してください。")
            self.values[key.strip()] = value.strip()

    def get(self, key):
        return self.values.get(key, "")

    def save(self, **updates):
        lines = [
            line for line in self.lines
            if line.lstrip().startswith("#") or line.partition("=")[0].strip() not in updates
        ]
        lines.extend(f"{key}={value}" for key, value in updates.items())
        temporary = tempfile.NamedTemporaryFile(
            dir=self.path.parent, prefix=".env.", mode="w", encoding="utf-8", delete=False
        )
        try:
            with temporary:
                temporary.write("\n".join(lines) + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary.name, self.path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)
        self.lines = lines
        self.values.update(updates)


@contextmanager
def run_lock(env_path):
    with env_path.with_name(".env.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MonitorError("同じ作業フォルダで別の処理が実行中です。") from None
        yield


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def error_details(response):
    try:
        body = json.load(response)
    except (ValueError, OSError, HTTPException):
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        return error.get("reason") or body.get("reason")
    return body.get("reason") or error


def request_json(request, counts):
    path = urlsplit(request.full_url).path
    label = f"{request.method} {path}"
    route = re.sub(r"^(/v1/(?:artists|albums|playlists)/)[^/]+", r"\1{id}", path)
    endpoint = f"{request.method} {route}"
    for attempt in range(5):
        delay = 2 ** attempt
        counts[endpoint] += 1
        try:
            with OPENER.open(request, timeout=20) as response:
                body = json.load(response)
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                return body
        except HTTPError as error:
            status = error.code
            retry_after = error.headers.get("Retry-After", "").strip()
            with error:
                reason = error_details(error)
            if reason == "QUOTA_EXCEEDED":
                raise ApiError(f"{label}: Spotify の利用枠を超えました（{reason}）",
                               status=status, reason=reason) from None
            failure = ApiError(f"{label}: HTTP {status}", status=status, reason=reason)
            retryable = status == 429 or (status in {500, 502, 503, 504} and request.method != "POST")
            if status == 429 and retry_after.isascii() and retry_after.isdecimal():
                delay = max(delay, int(retry_after))
            if not retryable or delay > 300:
                raise failure from None
        except (URLError, OSError, HTTPException, ValueError):
            failure = ApiError(f"{label}: 通信に失敗したか、応答の形式が不正です")
            if request.method == "POST":
                raise failure from None
        if attempt == 4:
            raise failure
        LOG.warning("%s。%s秒後に再試行します", failure, delay)
        time.sleep(delay)


class Spotify:
    def __init__(self, settings):
        self.settings = settings
        self.client_id = settings.get("SPOTIFY_CLIENT_ID")
        if not self.client_id:
            raise MonitorError(".env に SPOTIFY_CLIENT_ID を設定してください。")
        self.access_token = ""
        self.expires = 0
        self.request_counts = Counter()

    def token(self, **fields):
        payload = urlencode({"client_id": self.client_id, **fields}).encode("ascii")
        return request_json(Request(
            f"{ACCOUNTS}/api/token", data=payload, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ), self.request_counts)

    def refresh(self):
        refresh_token = self.settings.get("SPOTIFY_REFRESH_TOKEN")
        if not refresh_token:
            raise MonitorError("リフレッシュトークンがありません。auth を実行してください。")
        try:
            result = self.token(grant_type="refresh_token", refresh_token=refresh_token)
        except ApiError as error:
            if error.reason == "invalid_grant":
                self.settings.save(SPOTIFY_REFRESH_TOKEN="")
                raise MonitorError("Spotify の認証が無効または期限切れです。auth を再実行してください。") from None
            raise
        new_refresh_token = result.get("refresh_token")
        if new_refresh_token and new_refresh_token != refresh_token:
            self.settings.save(SPOTIFY_REFRESH_TOKEN=new_refresh_token)
        self.access_token = result["access_token"]
        self.expires = time.monotonic() + result["expires_in"] - 30

    def call(self, method, path, *, params=None, body=None):
        url = f"{API}{path}" if path.startswith("/") else path
        if not url.startswith(f"{API}/") or urlsplit(url).netloc != "api.spotify.com":
            raise MonitorError("Spotify が返したページ URL が不正です。")
        if params:
            url = f"{url}?{urlencode(params)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(2):
            if attempt or not self.access_token or time.monotonic() >= self.expires:
                self.refresh()
            request = Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            })
            try:
                return request_json(request, self.request_counts)
            except ApiError as error:
                if error.status != 401 or attempt:
                    raise

    def items(self, path, *, params=None, envelope=None, page_loader=None):
        load_page = self.call if page_loader is None else page_loader
        visited = set()
        while path:
            if path in visited:
                raise MonitorError("Spotify のページ URL が循環しています。")
            visited.add(path)
            page = load_page("GET", path, params=params)
            if envelope:
                page = page[envelope]
            items = page["items"]
            if not isinstance(items, list):
                raise MonitorError("Spotify のページに項目の一覧がありません。")
            yield from items
            path, params = page["next"], None
            if path is not None and (not isinstance(path, str) or not path):
                raise MonitorError("Spotify が返したページ URL が不正です。")


def authorize(spotify):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
    state = secrets.token_urlsafe(32)
    authorization_url = f"{ACCOUNTS}/authorize?" + urlencode({
        "client_id": spotify.client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPES, "state": state, "code_challenge_method": "S256", "code_challenge": challenge,
    })
    callback = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, format, *args):
            pass  # The callback URL contains the authorization code.

        def do_GET(self):
            path = urlsplit(self.path)
            if path.path != CALLBACK_PATH:
                self.send_error(404)
                return
            query = parse_qs(path.query)
            if not secrets.compare_digest(query.get("state", [""])[0].encode(), state.encode()):
                self.send_error(400, "Invalid OAuth state")
                return
            callback.update(query)
            message = b"Spotify response received. Return to the terminal for the result."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

    with HTTPServer((CALLBACK_HOST, CALLBACK_PORT), CallbackHandler) as server:
        print(f"ブラウザで Spotify のアクセスを許可してください。\n{authorization_url}", flush=True)
        if sys.platform == "darwin" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            webbrowser.open(authorization_url)
        deadline = time.monotonic() + 300
        while not callback and time.monotonic() < deadline:
            server.timeout = min(1, max(0, deadline - time.monotonic()))
            server.handle_request()

    if not callback.get("code") or callback.get("error"):
        raise MonitorError("Spotify 認証が拒否されたか、5分以内に完了しませんでした。")
    token = spotify.token(grant_type="authorization_code", code=callback["code"][0],
                          redirect_uri=REDIRECT_URI, code_verifier=verifier)
    if not set(SCOPES.split()).issubset(token.get("scope", "").split()):
        raise MonitorError("Spotify の権限が不足しています。auth でアクセスを許可してください。")
    spotify.settings.save(SPOTIFY_REFRESH_TOKEN=token["refresh_token"])
    LOG.info("Spotify 認証が完了しました。リフレッシュトークンを .env に保存しました。")
