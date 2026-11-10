#!/usr/bin/env python3
"""Connect the current user's normal Bash command to the selected account."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import datetime

BEGIN = "# >>> Claude Accounts terminal >>>"
END = "# <<< Claude Accounts terminal <<<"
ROOT = Path(__file__).resolve().parent


def write_atomic(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".claude-accounts-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    os.umask(0o077)
    home = Path.home()
    data = home / ".local/share/claude-account-web"
    entry = data / "current"
    if not entry.is_dir():
        raise SystemExit("请先在账号面板选择一个账号。")
    launcher = ROOT / "claude-current"
    # POSIX single-quote escaping; the resulting shell never evaluates the path.
    quoted_launcher = "'" + str(launcher).replace("'", "'\"'\"'") + "'"
    export = 'export CLAUDE_CONFIG_DIR="$HOME/.local/share/claude-account-web/current"'
    shell_block = f"{BEGIN}\n{export}\nclaude() {{\n    {quoted_launcher} \"$@\"\n}}\n{END}\n"
    profile_block = f"{BEGIN}\n{export}\n{END}\n"
    environment = home / ".config/environment.d/60-claude-account-web.conf"
    existing_env = environment.read_text() if environment.exists() else ""
    if existing_env and not existing_env.startswith("# Managed by Claude Accounts Web"):
        raise SystemExit("环境配置文件已经存在且不属于本应用，未覆盖。")
    changes = []
    for path, block in [(home / ".bashrc", shell_block), (home / ".profile", profile_block)]:
        original = path.read_text() if path.exists() else ""
        if (BEGIN in original) != (END in original):
            raise SystemExit(f"{path.name} 中的账号配置标记不完整，未修改。")
        clean = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", original, flags=re.S)
        changes.append((path, clean.rstrip() + "\n\n" + block))
    changes.append((environment, f"# Managed by Claude Accounts Web\nCLAUDE_CONFIG_DIR={entry}\n"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = data / "terminal-backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    manifest = []
    for path, content in changes:
        present = path.exists()
        mode = path.stat().st_mode & 0o777 if present else 0o600
        saved = backup / ("bashrc" if path.name == ".bashrc" else "profile" if path.name == ".profile" else "environment.conf")
        if present:
            shutil.copy2(path, saved)
        manifest.append({"path": str(path), "existed": present, "backup": str(saved) if present else None})
        write_atomic(path, content, mode)
    write_atomic(backup / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", 0o600)
    write_atomic(data / "terminal-integration.json", json.dumps({"installedAt": stamp, "backup": str(backup), "entry": str(entry)}, indent=2) + "\n", 0o600)
    # Applies to future user services; existing processes retain their environment.
    result = subprocess.run(["systemctl", "--user", "set-environment", f"CLAUDE_CONFIG_DIR={entry}"], capture_output=True)
    print("已配置 Bash、登录环境和 environment.d。")
    print("systemd 用户环境：" + ("已更新" if result.returncode == 0 else "本次未更新，下次登录时读取配置"))
    print(f"原配置备份：{backup}")
    print("新终端直接输入 claude；已有终端运行 source ~/.bashrc 一次。")


if __name__ == "__main__":
    main()
