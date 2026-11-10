"""Codex isolated login profiles, adapted from QB Gate's Codex slot design.

AGPL-3.0-only with LICENSE-ADDITIONAL-TERMS.md; see NOTICE.md.
Official CLI owns login and credential refresh. Tokens never enter API responses.
"""
import base64
import contextlib
import json
import os
from pathlib import Path
import secrets
import shutil
import threading

from app import AppError, ID, LoginJob, atomic_json


def get_manager(manager):
    with manager.lock:
        if manager.codex is None:
            manager.codex = CodexAccounts(manager.data / 'codex', manager.home)
        return manager.codex


class CodexAccounts:
    def __init__(self, data, home):
        self.data, self.home = data, home
        self.lock = threading.RLock()
        self.jobs = {}
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = data / 'accounts.json'
        self.registry = json.loads(self.path.read_text()) if self.path.exists() else {'accounts': []}
        if not self.path.exists() and (home / '.codex/auth.json').is_file():
            self.registry['accounts'].append({'id': secrets.token_hex(8), 'name': '本机 Codex', 'source': 'linked'})
            atomic_json(self.path, self.registry)
            self.point(self.registry['accounts'][0])

    def profile(self, account):
        return self.home / '.codex' if account['source'] == 'linked' else self.data / 'profiles' / account['id']

    def account(self, ident):
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            raise AppError('账号不存在。', 404)
        for account in self.registry['accounts']:
            if account['id'] == ident:
                return account
        raise AppError('账号不存在。', 404)

    def auth(self, account):
        try:
            d = json.loads((self.profile(account) / 'auth.json').read_text())
            tokens = d.get('tokens') or {}
            result = {'loggedIn': bool(tokens.get('access_token') and tokens.get('refresh_token')),
                      'authMethod': 'chatgpt', 'accountId': tokens.get('account_id')}
            jwt = tokens.get('id_token', '').split('.')
            if len(jwt) == 3 and len(jwt[1]) < 32768:
                claims = json.loads(base64.urlsafe_b64decode(jwt[1] + '=' * (-len(jwt[1]) % 4)))
                result['email'] = claims.get('email')
                result['subscriptionType'] = claims.get('https://api.openai.com/auth', {}).get('chatgpt_plan_type')
            return result
        except (OSError, ValueError, TypeError, AttributeError):
            return {'loggedIn': False}

    def refresh(self, ident):
        return self.auth(self.account(ident))

    def create(self, name):
        with self.lock:
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 48 or any(ord(c) < 32 for c in name):
                raise AppError('账号名称需要 1–48 个字符。')
            if any(a['name'].casefold() == name.strip().casefold() for a in self.registry['accounts']):
                raise AppError('这个名称已经存在。', 409)
            if len(self.registry['accounts']) >= 30:
                raise AppError('最多管理 30 个 Codex 账号。')
            a = {'id': secrets.token_hex(8), 'name': name.strip(), 'source': 'managed'}
            p = self.profile(a)
            p.mkdir(parents=True, mode=0o700)
            (p / 'config.toml').write_text('forced_login_method = "chatgpt"\ncli_auth_credentials_store = "file"\n')
            # Official clients retain their own state databases; share transcript directories only.
            for folder in ('sessions', 'archived_sessions'):
                shared = self.home / '.codex' / folder
                shared.mkdir(parents=True, exist_ok=True, mode=0o700)
                (p / folder).symlink_to(shared, target_is_directory=True)
            self.registry['accounts'].append(a)
            atomic_json(self.path, self.registry)
            return a['id']

    def point(self, account):
        link = self.data / 'current'
        if link.exists() and not link.is_symlink():
            raise AppError('Codex 固定入口不是符号链接。', 409)
        temp = self.data / ('.current-' + secrets.token_hex(5))
        try:
            temp.symlink_to(self.profile(account), target_is_directory=True)
            os.replace(temp, link)
        finally:
            temp.unlink(missing_ok=True)

    def activate(self, ident):
        with self.lock:
            account = self.account(ident)
            if any(j.status == 'running' for j in self.jobs.values()):
                raise AppError('请先完成或取消登录。', 409)
            if not self.auth(account)['loggedIn']:
                raise AppError('请先登录 ChatGPT 账号。', 409)
            self.point(account)
            return {'activeId': ident}

    def start_login(self, ident):
        with self.lock:
            account = self.account(ident)
            if account['source'] == 'linked':
                raise AppError('本机账号请在原终端登录；可以添加独立账号。', 409)
            if any(j.status == 'running' for j in self.jobs.values()):
                raise AppError('已有登录正在进行。', 409)
            for process in Path('/proc').iterdir():
                if not process.name.isdigit():
                    continue
                try:
                    env_items = dict(item.split(b'=', 1) for item in (process / 'environ').read_bytes().split(b'\0') if b'=' in item)
                    configured = env_items.get(b'CODEX_HOME', b'').decode()
                    if configured and Path(configured).resolve() == self.profile(account).resolve() and 'codex' in (process / 'comm').read_text().casefold():
                        raise AppError('这个账号有 Codex 进程运行中，请退出后重新登录。', 409)
                except (OSError, UnicodeError):
                    continue
            binary = shutil.which('codex')
            if not binary and (self.home / '.npm-global/bin/codex').is_file():
                binary = str(self.home / '.npm-global/bin/codex')
            if not binary:
                raise AppError('未找到 Codex CLI。')
            env = {k: v for k, v in os.environ.items() if k not in {'OPENAI_API_KEY', 'OPENAI_ACCESS_TOKEN', 'CODEX_HOME', 'CODEX_API_KEY'}}
            env.update(CODEX_HOME=str(self.profile(account)), BROWSER='/bin/true', NO_COLOR='1', TERM='dumb')
            job = LoginJob(self, account, [binary, 'login', '--device-auth'], env)
            self.jobs = {job.id: job}
            return job.public()

    def job(self, ident):
        if not isinstance(ident, str) or ident not in self.jobs:
            raise AppError('登录任务不存在。', 404)
        return self.jobs[ident]

    def state(self):
        with self.lock:
            current = (self.data / 'current').resolve()
            accounts = [{**a, 'auth': self.auth(a), 'historyShared': True} for a in self.registry['accounts']]
            return {'accounts': accounts, 'activeId': next((a['id'] for a in self.registry['accounts'] if self.profile(a).resolve() == current), None),
                    'jobs': [j.public() for j in self.jobs.values()], 'entryPath': str(self.data / 'current'),
                    'launcher': str(Path(__file__).parent / 'codex-current')}

    def close(self):
        for job in self.jobs.values():
            job.cancel()
