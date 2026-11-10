#!/usr/bin/env python3
"""Local Claude account manager. No third-party Python dependencies (Linux)."""
from __future__ import annotations

import argparse
import codecs
import contextlib
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
if __name__ == '__main__':
    sys.modules.setdefault('app', sys.modules[__name__])
ANSI = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")
URL = re.compile(r"https://[^\s<>\x1b]+")
ID = re.compile(r"^[a-f0-9]{16}$")
AUTH_ENV = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "GOOGLE_", "AZURE_")


class AppError(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


def atomic_json(path: Path, value):
    temp = path.with_name(path.name + "." + secrets.token_hex(5) + ".tmp")
    try:
        with open(temp, "x", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def cli_environment(config_dir: Path):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(AUTH_ENV) and k not in {
               "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDECODE",
               "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
           }}
    env.update(CLAUDE_CONFIG_DIR=str(config_dir), BROWSER="/bin/true",
               TERM="dumb", NO_COLOR="1")
    return env


def claude_processes(exclude=()):
    """Conservatively block switching while any user-owned Claude CLI is alive."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in exclude:
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            args = entry.joinpath("cmdline").read_bytes().split(b"\0")
            args = [a.decode("utf-8", "replace") for a in args if a]
            exe = entry.joinpath("exe").resolve().name
            direct = bool(args) and Path(args[0]).name in {"claude", "claude.exe"}
            node_cli = (len(args) > 1 and exe.startswith("node") and
                        (Path(args[1]).name == "claude" or
                         ("claude" in args[1] and args[1].endswith(("cli.js", "cli.mjs")))))
            if direct or exe in {"claude", "claude.exe"} or node_cli:
                found.append({"pid": int(entry.name), "name": "Claude Code"})
        except (OSError, ValueError):
            continue
    return found


class LoginJob:
    def __init__(self, manager, account, command=None, environment=None):
        self.manager, self.account_id = manager, account["id"]
        self.id = secrets.token_hex(8)
        self.output, self.error, self.status = "", None, "running"
        self.created = time.time()
        self.lock = threading.RLock()
        self.cancelled = False
        master, slave = pty.openpty()
        self.master = master
        # Large columns keep the official OAuth URL on one line. Disable input echo.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 240, 0, 0))
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        try:
            self.process = subprocess.Popen(
                command or [manager.claude, "auth", "login", "--claudeai"],
                stdin=slave, stdout=slave, stderr=slave,
                env=environment if environment is not None else cli_environment(manager.profile(account)),
                cwd=manager.profile(account), start_new_session=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._timeout, daemon=True).start()

    def _timeout(self):
        try:
            self.process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            self.cancel("登录等待超过 10 分钟，请重新登录。")

    def _read(self):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                try:
                    block = os.read(self.master, 4096)
                except OSError:
                    break
                if not block:
                    break
                with self.lock:
                    self.output = (self.output + decoder.decode(block))[-48000:]
            code = self.process.wait()
            if self.cancelled:
                self.status = "cancelled"
            elif code == 0:
                result = self.manager.refresh(self.account_id)
                if result.get("loggedIn"):
                    self.status = "completed"
                else:
                    self.status = "failed"
                    self.error = "CLI 已退出，但未读取到账号登录信息。请重试。"
            else:
                self.status = "failed"
                self.error = f"官方登录进程退出（{code}）。请查看下方 CLI 输出。"
        except Exception:
            self.status = "failed"
            self.error = "读取登录进程失败，请取消后重新登录。"
        finally:
            with self.lock:
                with contextlib.suppress(OSError):
                    os.close(self.master)
                self.master = -1

    def cancel(self, reason=None):
        with self.lock:
            if self.process.poll() is None:
                self.cancelled = True
                self.error = reason
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)

    def send(self, value):
        if not isinstance(value, str) or len(value) > 4096:
            raise AppError("输入过长。")
        if any(ord(c) < 32 for c in value):
            raise AppError("请输入一行授权码，不包含控制字符。")
        with self.lock:
            if self.status != "running" or self.master < 0 or self.process.poll() is not None:
                raise AppError("登录进程已结束。", 409)
            os.write(self.master, (value + "\n").encode())

    def public(self):
        with self.lock:
            output = ANSI.sub("", self.output).replace("\r", "")
            # Auth codes are not echoed; redact token-looking strings defensively.
            output = re.sub(r"sk-ant-[A-Za-z0-9_-]+", "[已隐藏凭据]", output)
            links = []
            for match in URL.findall(output):
                link = match.rstrip(".,)'")
                host = urlsplit(link).hostname or ""
                if host in {"claude.ai", "claude.com", "console.anthropic.com", "platform.claude.com", "auth.openai.com", "chatgpt.com"}:
                    if link not in links:
                        links.append(link)
            return {"id": self.id, "accountId": self.account_id, "status": self.status,
                    "output": output, "links": links, "error": self.error}


class Manager:
    def __init__(self, data: Path, home: Path, claude: str, auto_import=True):
        self.data, self.home, self.claude = data.absolute(), home.absolute(), claude
        self.lock = threading.RLock()
        self.jobs = {}
        self.usage = None
        self.quota_lock = threading.Lock()
        self.quota_last_started = float('-inf')
        self.codex = None
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        self.lockfile = open(self.data / "manager.lock", "a")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AppError("这个数据目录已有管理服务在运行。")
        (self.data / "profiles").mkdir(exist_ok=True, mode=0o700)
        self.registry_path = self.data / "accounts.json"
        self.registry = json.loads(self.registry_path.read_text()) if self.registry_path.exists() else {"accounts": []}
        for account in self.registry["accounts"]:
            if not ID.fullmatch(account.get("id", "")):
                raise AppError("账号记录格式不正确。")
        native_projects = self.home / ".claude" / "projects"
        saved_shared = self.registry.get("sharedPath")
        if isinstance(saved_shared, str) and Path(saved_shared).is_absolute():
            self.shared = Path(saved_shared)
        else:
            use_native = native_projects.is_dir() or (self.home / ".claude/.credentials.json").is_file()
            self.shared = native_projects.resolve() if use_native else self.data / "shared-projects"
        self.registry["sharedPath"] = str(self.shared)
        if not self.shared.exists():
            self.shared.mkdir(mode=0o700)
        if auto_import and not self.registry_path.exists():
            self._import_local()
            self._save()

    def _save(self):
        atomic_json(self.registry_path, self.registry)

    def profile(self, account):
        if account["source"] == "linked":
            return self.home / ".claude"
        return self.data / "profiles" / account["id"]

    def account(self, ident):
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            raise AppError("账号不存在。", 404)
        for item in self.registry["accounts"]:
            if item["id"] == ident:
                return item
        raise AppError("账号不存在。", 404)

    def _new(self, name, source="managed"):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 48 or any(ord(c) < 32 for c in name):
            raise AppError("账号名称需要 1–48 个字符。")
        name = name.strip()
        if any(a["name"].casefold() == name.casefold() for a in self.registry["accounts"]):
            raise AppError("这个名称已经存在，请换一个。", 409)
        if len(self.registry["accounts"]) >= 30:
            raise AppError("原型最多管理 30 个账号。")
        item = {"id": secrets.token_hex(8), "name": name, "source": source,
                "createdAt": time.time(), "auth": {"loggedIn": False}}
        if source == "managed":
            path = self.profile(item)
            path.mkdir(mode=0o700)
            (path / "projects").symlink_to(self.shared, target_is_directory=True)
        return item

    def _import_local(self):
        credentials = self.home / ".claude" / ".credentials.json"
        if not credentials.is_file():
            return
        item = self._new("本机现有账号", "linked")
        profile = self.profile(item)
        # Point at the actual native directory: token rotations may replace files
        # atomically, so a second credentials.json symlink is not sufficient.
        item["auth"] = self.auth_status(profile)
        self.registry["accounts"].append(item)
        # This creates only our own entry point; it does not change native Claude.
        self._point_to(item)

    def auth_status(self, profile):
        try:
            result = subprocess.run([self.claude, "auth", "status", "--json"],
                                    env=cli_environment(profile), cwd=profile,
                                    capture_output=True, text=True, timeout=20)
            raw = json.loads(result.stdout)
            auth = {k: raw[k] for k in ("loggedIn", "authMethod", "email", "subscriptionType")
                    if isinstance(raw.get(k), (str, bool))}
            auth["loggedIn"] = bool(raw.get("loggedIn")) and raw.get("authMethod") == "claude.ai"
            # CLAUDE_CONFIG_DIR changes the identity JSON lookup. Keep native
            # identity read-only; the CLI credential remains in the same directory.
            if auth["loggedIn"] and not auth.get("email") and profile.resolve() == (self.home / ".claude").resolve():
                with contextlib.suppress(OSError, ValueError, AttributeError):
                    identity = json.loads((self.home / ".claude.json").read_text()).get("oauthAccount", {})
                    if isinstance(identity.get("emailAddress"), str):
                        auth["email"] = identity["emailAddress"]
            auth["checkedAt"] = time.time()
            return auth
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {"loggedIn": False, "error": "无法读取 CLI 登录状态，请检查 Claude 安装。", "checkedAt": time.time()}

    def refresh(self, ident):
        with self.lock:
            account = self.account(ident)
            account["auth"] = self.auth_status(self.profile(account))
            self._save()
            return account["auth"]

    def create(self, name):
        with self.lock:
            item = self._new(name)
            self.registry["accounts"].append(item)
            self._save()
            return item["id"]

    def active_id(self):
        link = self.data / "current"
        if not link.is_symlink():
            return None
        for item in self.registry["accounts"]:
            if link.resolve() == self.profile(item).resolve():
                return item["id"]
        return None

    def _point_to(self, item):
        link = self.data / "current"
        if link.exists() and not link.is_symlink():
            raise AppError("固定入口不是符号链接，已停止切换以保护现有文件。", 409)
        temp = self.data / (".current-" + secrets.token_hex(5))
        try:
            temp.symlink_to(self.profile(item), target_is_directory=True)
            os.replace(temp, link)
        finally:
            temp.unlink(missing_ok=True)

    def activate(self, ident):
        with self.lock:
            item = self.account(ident)
            if any(j.status == "running" for j in self.jobs.values()):
                raise AppError("请先完成或取消正在进行的登录。", 409)
            processes = claude_processes()
            if processes:
                raise AppError(f"检测到 {len(processes)} 个 Claude 进程。请先在 Anywhere 停止 Claude Runtime，并退出其他 Claude 终端，再切换。", 409)
            if not self.refresh(ident).get("loggedIn"):
                raise AppError("账号尚未登录 Claude 订阅，请先完成登录。", 409)
            self._point_to(item)
            return {"activeId": ident, "message": "已切换固定入口。重新启动 Anywhere 的 Claude Runtime 后生效。"}

    def start_login(self, ident):
        with self.lock:
            account = self.account(ident)
            if account["source"] != "managed":
                raise AppError("本机账号与原 CLI 共用凭据，请在原终端登录；也可以添加一个独立账号。", 409)
            if any(j.status == "running" for j in self.jobs.values()):
                raise AppError("已有登录正在进行，请先完成或取消。", 409)
            if self.active_id() == ident and claude_processes():
                raise AppError("这个账号可能正在使用，请先停止 Claude 进程后重新登录。", 409)
            # Retain only the current/most recent job: OAuth links stay in memory.
            self.jobs.clear()
            job = LoginJob(self, account)
            self.jobs[job.id] = job
            return job.public()

    def job(self, ident):
        if not isinstance(ident, str) or ident not in self.jobs:
            raise AppError("登录任务不存在，请重新开始。", 404)
        return self.jobs[ident]

    def state(self):
        with self.lock:
            accounts = [{**a, "configDir": str(self.profile(a)),
                         "historyShared": (self.profile(a) / "projects").is_dir()
                         and (self.profile(a) / "projects").resolve() == self.shared.resolve()}
                        for a in self.registry["accounts"]]
            return {"accounts": accounts, "activeId": self.active_id(),
                    "entryPath": str(self.data / "current"), "sharedPath": str(self.shared),
                    "claudePath": self.claude, "processes": claude_processes(),
                    "jobs": [j.public() for j in self.jobs.values()],
                    "integration": {"environment": {"CLAUDE_CONFIG_DIR": str(self.data / "current")},
                                    "executablePath": str(ROOT / "claude-current"),
                                    "automaticRestart": False}}

    def close(self):
        if self.codex:
            self.codex.close()
        if self.usage:
            self.usage.close()
        for job in list(self.jobs.values()):
            job.cancel()
        self.lockfile.close()

    def usage_report(self, days=7, refresh=False, provider="all", start=None, end=None, period=None, billing_day=1):
        from usage_stats import resolve_period
        try:
            resolve_period(days, start, end, period, billing_day)
        except ValueError as exc:
            raise AppError(str(exc)) from None
        if not isinstance(provider, str) or provider not in {"all", "claude", "codex"}:
            raise AppError("统计来源只支持全部、Claude 或 Codex。")
        with self.lock:
            if self.usage is None:
                from usage_stats import UsageIndex
                self.usage = UsageIndex(self)
            usage = self.usage
        if refresh:
            usage.request_refresh()
        return usage.report(days, provider, start, end, period, billing_day)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager, password=None, allowed_hosts=()):
        if address[0] not in {"127.0.0.1", "localhost"} and not password:
            raise AppError("局域网监听必须设置访问口令。")
        self.manager = manager
        self.password = password
        self.allowed_hosts = {"127.0.0.1", "localhost", *allowed_hosts}
        if address[0] != "0.0.0.0":
            self.allowed_hosts.add(address[0])
        self.csrf, self.session = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.auth_lock = threading.RLock()
        self.auth_sessions = {}
        self.auth_attempts = {}
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log OAuth URLs, codes, or request bodies.

    def _host(self):
        expected = {f"{host}:{self.server.server_port}" for host in self.server.allowed_hosts}
        host = self.headers.get("Host", "")
        if host not in expected:
            raise AppError("访问地址不在允许列表中。", 403)
        return host

    def _authenticate(self):
        if self.server.password is None:
            return
        with self.server.auth_lock:
            expires = self.server.auth_sessions.get(self._session_cookie(), 0)
        if expires <= time.time():
            raise AppError("请先登录面板。", 401)

    def _session_cookie(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        session = cookies.get("cw_session")
        return session.value if session else ""

    def _same_origin(self):
        host = self._host()
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            raise AppError("请求来源不匹配。", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise AppError("不接受跨站请求。", 403)

    def _csrf(self):
        token = self.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(token, self.server.csrf):
            raise AppError("页面已过期，请刷新。", 403)

    def _guard(self):
        self._same_origin()
        self._authenticate()
        if self.server.password is None and not secrets.compare_digest(self._session_cookie(), self.server.session):
            raise AppError("请重新打开页面。", 403)
        self._csrf()

    def _login(self, password):
        self._same_origin()
        self._csrf()
        if self.server.password is None:
            return self._reply(200, {"ok": True}, cookie=self.server.session)
        now, address = time.time(), self.client_address[0]
        with self.server.auth_lock:
            self.server.auth_attempts = {key: row for key, row in self.server.auth_attempts.items() if now - row[0] < 60}
            since, attempts = self.server.auth_attempts.get(address, (now, 0))
            if attempts >= 5:
                raise AppError("尝试次数较多，请稍后再试。", 429)
            if not isinstance(password, str) or len(password) > 256 or not secrets.compare_digest(password.encode(), self.server.password.encode()):
                self.server.auth_attempts[address] = (since, attempts + 1)
                raise AppError("密码不正确，请重试。", 401)
            self.server.auth_attempts.pop(address, None)
            self.server.auth_sessions = {key: expires for key, expires in self.server.auth_sessions.items() if expires > now}
            self.server.auth_sessions.pop(self._session_cookie(), None)
            session = secrets.token_urlsafe(32)
            self.server.auth_sessions[session] = now + 7 * 86400
        return self._reply(200, {"ok": True}, cookie=session, max_age=7 * 86400)

    def _reply(self, status, body, content_type="application/json; charset=utf-8", cookie=False, max_age=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie is not False:
            value = self.server.session if cookie is True else cookie
            age = f"; Max-Age={max_age}" if max_age is not None else ""
            self.send_header("Set-Cookie", f"cw_session={value}; HttpOnly; SameSite=Strict; Path=/{age}")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_GET(self):
        try:
            self._host()
            path = urlsplit(self.path).path
            public_assets = {"/style.css": ("style.css", "text/css; charset=utf-8"),
                             "/favicon.svg": ("favicon.svg", "image/svg+xml"),
                             "/login.js": ("login.js", "text/javascript; charset=utf-8")}
            if path in public_assets:
                name, mime = public_assets[path]
                return self._reply(200, (ROOT / "static" / name).read_bytes(), mime)
            if path == "/":
                try:
                    self._authenticate()
                except AppError as exc:
                    if exc.status != 401:
                        raise
                    html = (ROOT / "static/login.html").read_text().replace("__CSRF__", self.server.csrf)
                    return self._reply(200, html.encode(), "text/html; charset=utf-8")
            self._authenticate()
            if path == "/source.zip":
                from source_archive import source_archive
                return self._reply(200, source_archive(ROOT), "application/zip")
            if path == "/":
                html = (ROOT / "static/index.html").read_text().replace("__CSRF__", self.server.csrf)
                html = html.replace("__AUTH_CONTROLS__", '<button id="logout" class="quiet" type="button">退出登录</button>' if self.server.password is not None else "")
                return self._reply(200, html.encode(), "text/html; charset=utf-8", cookie=self.server.session if self.server.password is None else False)
            assets = {"/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8"),
                      "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if path in assets:
                name, mime = assets[path]
                return self._reply(200, (ROOT / "static" / name).read_bytes(), mime)
            self._guard()
            if path == "/api/codex/state":
                from codex_accounts import get_manager
                return self._reply(200, get_manager(self.server.manager).state())
            if path == "/api/state":
                return self._reply(200, self.server.manager.state())
            if path == "/api/usage":
                query = parse_qs(urlsplit(self.path).query)
                try:
                    days = int(query.get("days", ["7"])[0])
                    billing_day = int(query.get("billingDay", ["1"])[0])
                except ValueError:
                    raise AppError("统计区间格式不正确。")
                return self._reply(200, self.server.manager.usage_report(days, provider=query.get("provider", ["all"])[0],
                    start=query.get("start", [None])[0], end=query.get("end", [None])[0],
                    period=query.get("period", [None])[0], billing_day=billing_day))
            raise AppError("页面不存在。", 404)
        except AppError as exc:
            self._reply(exc.status, {"error": str(exc)})
        except Exception:
            self._reply(500, {"error": "读取失败，请检查服务状态。"})

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            if path == "/api/auth/login":
                self._same_origin()
                self._csrf()
            else:
                self._guard()
            if self.headers.get_content_type() != "application/json":
                raise AppError("需要 JSON 请求。", 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError()
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                raise AppError("请求格式不正确。")
            manager = self.server.manager
            if path == "/api/auth/login":
                return self._login(body.get("password"))
            if path == "/api/auth/logout":
                with self.server.auth_lock:
                    self.server.auth_sessions.pop(self._session_cookie(), None)
                return self._reply(200, {"ok": True}, cookie="", max_age=0)
            if path.startswith('/api/codex/'):
                from codex_accounts import get_manager
                codex = get_manager(manager)
                route = path.removeprefix('/api/codex/')
                if route == 'accounts':
                    result = {'id': codex.create(body.get('name'))}
                elif route in {'refresh', 'activate', 'login'}:
                    method = {'refresh': codex.refresh, 'activate': codex.activate, 'login': codex.start_login}[route]
                    result = method(body.get('id'))
                elif route == 'login/cancel':
                    codex.job(body.get('id')).cancel()
                    result = {'ok': True}
                else:
                    raise AppError('接口不存在。', 404)
            elif path == "/api/accounts":
                result = {"id": manager.create(body.get("name"))}
            elif path == "/api/refresh":
                result = manager.refresh(body.get("id"))
            elif path == "/api/activate":
                result = manager.activate(body.get("id"))
            elif path == "/api/login":
                result = manager.start_login(body.get("id"))
            elif path == "/api/login/input":
                manager.job(body.get("id")).send(body.get("input"))
                result = {"ok": True}
            elif path == "/api/login/cancel":
                manager.job(body.get("id")).cancel()
                result = {"ok": True}
            elif path == "/api/quota/refresh":
                from manual_quota import refresh_quota
                result = refresh_quota(manager, body.get('id'))
            elif path == "/api/usage/refresh":
                result = manager.usage_report(body.get("days", 7), refresh=True, provider=body.get("provider", "all"),
                    start=body.get("start"), end=body.get("end"), period=body.get("period"), billing_day=body.get("billingDay", 1))
            else:
                raise AppError("接口不存在。", 404)
            self._reply(200, result)
        except AppError as exc:
            self._reply(exc.status, {"error": str(exc)})
        except Exception:
            self._reply(500, {"error": "操作失败，请检查 CLI 和数据目录权限。"})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18769)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；局域网使用 0.0.0.0")
    parser.add_argument("--allow-host", action="append", default=[], help="允许访问的局域网 IP 或主机名，可重复")
    parser.add_argument("--password-file", type=Path, help="访问口令文件；局域网模式默认自动生成")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/claude-account-web")
    parser.add_argument("--claude", default=shutil.which("claude"))
    args = parser.parse_args()
    if not args.claude:
        parser.error("找不到 Claude Code，请先安装或用 --claude 指定路径。")
    if any(not re.fullmatch(r"[A-Za-z0-9.-]+", host) for host in args.allow_host):
        parser.error("--allow-host 只填写 IP 或主机名，不要包含协议和端口。")
    manager = Manager(args.data_dir.expanduser(), Path.home(), args.claude)
    try:
        password = None
        if args.host not in {"127.0.0.1", "localhost"} or args.password_file:
            password_file = args.password_file.expanduser() if args.password_file else manager.data / "access-password"
            if not password_file.exists():
                with open(password_file, "x", encoding="utf-8") as stream:
                    os.chmod(password_file, 0o600)
                    stream.write(secrets.token_urlsafe(15) + "\n")
            password = password_file.read_text().strip()
            if not password or len(password) > 256:
                raise AppError("访问口令文件需要包含 1–256 个字符。")
        server = Server((args.host, args.port), manager, password=password, allowed_hosts=args.allow_host)
    except BaseException:
        manager.close()
        raise
    print(f"Claude Accounts: http://127.0.0.1:{server.server_port}", flush=True)
    print(f"Data: {manager.data}", flush=True)
    if password:
        print(f"Access: web login (password file: {password_file})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.close()


if __name__ == "__main__":
    main()
