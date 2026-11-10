"""Run the official interactive /usage command only for explicit POST actions."""
import contextlib
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import time

from app import AppError, ANSI, cli_environment
from usage_stats import identity, quota_for


def cache_stamp(profile, home, linked):
    _, configs = identity(profile, home, linked)
    return max((config.get('cachedUsageUtilization', {}).get('fetchedAtMs', 0)
                for config in configs if isinstance(config.get('cachedUsageUtilization'), dict)
                and isinstance(config['cachedUsageUtilization'].get('fetchedAtMs', 0), (int, float))), default=0)


def run_usage(manager, account, cwd, timeout=45):
    profile = manager.profile(account).resolve()
    linked = account['source'] == 'linked'
    owner, _ = identity(profile, manager.home, linked)
    if not owner.get('accountUuid'):
        raise AppError('没有找到该账号的登录身份，请先登录。', 409)
    before = cache_stamp(profile, manager.home, linked)
    master, slave = pty.openpty()
    process = None
    started = time.time()
    output = b''
    trust_confirmed = False
    theme_selected = False
    security_notes_acknowledged = False
    confirm_at = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 45, 160, 0, 0))
        env = cli_environment(profile)
        # The CLI also classifies /api/oauth/usage as nonessential traffic.
        # Suppress background reporting individually so /usage can still run,
        # including when the service inherited the blanket opt-out variable.
        env.pop('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', None)
        env.update(TERM='xterm-256color', DISABLE_TELEMETRY='1',
                   DISABLE_ERROR_REPORTING='1', DISABLE_AUTOUPDATER='1')
        # No resume/continue, project customizations, MCP, hooks or model tools.
        process = subprocess.Popen([manager.claude, '/usage', '--safe-mode', '--tools', '', '--ax-screen-reader'],
                                   stdin=slave, stdout=slave, stderr=slave, env=env,
                                   cwd=cwd, start_new_session=True)
        os.close(slave)
        slave = -1
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            # Ink handles the choice and submit in separate render cycles.
            if confirm_at is not None and time.monotonic() >= confirm_at:
                os.write(master, b'\r')
                confirm_at = None
            if select.select([master], [], [], .2)[0]:
                try:
                    block = os.read(master, 65536)
                except OSError:
                    block = b''
                output = (output + block)[-128 * 1024:]
            text = ANSI.sub('', output.decode('utf-8', 'replace')).casefold()
            if ('choose the text style' in text and 'select with numbers' in text
                    and not theme_selected):
                os.write(master, b'3')  # First-run appearance only: light mode.
                confirm_at = time.monotonic() + .3
                theme_selected = True
                output = b''
                continue
            if ('security notes:' in text and 'press enter to continue' in text
                    and not security_notes_acknowledged):
                os.write(master, b'\r')
                security_notes_acknowledged = True
                output = b''
                continue
            if 'enter y/n' in text:
                # User authorized this fixed home only. Never answer another
                # directory's prompt or an unrelated CLI confirmation.
                lines = {line.strip() for line in text.splitlines()}
                if (trust_confirmed or cwd.resolve() != manager.home.resolve()
                        or str(manager.home.resolve()).casefold() not in lines
                        or 'accessing workspace:' not in text or 'trust this folder' not in text):
                    raise AppError('CLI 的目录信任提示与主目录不匹配，已停止查询。', 409)
                os.write(master, b'y')
                confirm_at = time.monotonic() + .3
                trust_confirmed = True
                output = b''
                continue
            if any(marker in text for marker in ('please run /login', 'not logged in', 'please log in')):
                raise AppError('CLI 要求重新登录，请先完成该账号登录。', 409)
            if 'failed to load usage data' in text:
                raise AppError('官方 CLI 无法加载该账号的额度数据。旧数据已保留，请稍后手动重试。', 502)
            if cache_stamp(profile, manager.home, linked) > max(before, (started - 2) * 1000):
                quota = quota_for(account, profile, manager.home, allow_desktop=False)
                if any(quota.get(key) and quota[key]['measuredAt'] >= started - 2 for key in ('fiveHour', 'sevenDay')):
                    return {'ok': True, 'accountId': account['id'], 'name': account['name'],
                            'cwd': str(cwd), 'queriedAt': time.time(), 'quota': quota,
                            'homeTrustConfirmed': trust_confirmed}
            if process.poll() is not None:
                break
        raise AppError('CLI /usage 没有写入新的额度缓存；可能网络异常或官方查询受限。旧数据已保留，请稍后手动重试。', 502)
    except OSError:
        raise AppError('无法启动官方 Claude CLI，请检查安装和项目目录权限。', 502)
    finally:
        if slave >= 0:
            os.close(slave)
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        os.close(master)


def refresh_quota(manager, ident):
    if not manager.quota_lock.acquire(blocking=False):
        raise AppError('已有额度查询正在进行，请等待完成。', 409)
    try:
        with manager.lock:
            account = dict(manager.account(ident))
            if any(j.status == 'running' for j in manager.jobs.values()):
                raise AppError('请先完成或取消账号登录。', 409)
            now = time.monotonic()
            if now - manager.quota_last_started < 10:
                raise AppError('刚刚已发起额度查询，请稍后手动重试。', 429)
            cwd = manager.home
            manager.quota_last_started = now
        return run_usage(manager, account, cwd)
    finally:
        manager.quota_lock.release()
