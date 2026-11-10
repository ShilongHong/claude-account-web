"""Historical owner index ported from QB Gate, plus runtime usage evidence.

Based on QB Gate, Copyright (C) 2026 smithtaylor7748-ops.
Source code: https://github.com/smithtaylor7748-ops/qb-gate
AGPL-3.0-only with LICENSE-ADDITIONAL-TERMS.md. Modified Python integration.
Hooks store identifiers/offsets only, never hook input or conversation content.
"""
from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys
import tempfile
import time
import uuid

from usage_stats import MAX_LINE, identity, parse_entry, read_json, text

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
EVENTS = ("SessionStart", "Stop", "StopFailure", "SubagentStop", "SessionEnd")


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".attribution-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def database(data):
    path = data / "account-attribution.sqlite3"
    db = sqlite3.connect(path, timeout=10)
    os.chmod(path, 0o600)
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS runs(run TEXT, session TEXT, owner TEXT,
                started REAL, pid INTEGER, ticks TEXT, ended INTEGER DEFAULT 0,
                unsafe INTEGER DEFAULT 0, PRIMARY KEY(run,session));
            CREATE TABLE IF NOT EXISTS offsets(run TEXT, session TEXT, file TEXT,
                device INTEGER, inode INTEGER, offset INTEGER,
                PRIMARY KEY(run,session,file));
            CREATE TABLE IF NOT EXISTS claims(run TEXT, session TEXT, key TEXT,
                owner TEXT, at REAL, PRIMARY KEY(run,session,key));
        """)
        yield db
    finally:
        db.close()


def process_ticks(pid):
    try:
        return (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def install_hooks(profile, data):
    """Preserve all other settings/hooks. Back up the first modified settings."""
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data / "attribution.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = profile / "settings.json"
        if path.is_symlink():
            raise ValueError("不修改链接的 settings.json")
        settings = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(settings, dict) or settings.get("disableAllHooks"):
            raise ValueError("账号配置禁用了 Hook")
        original = json.loads(json.dumps(settings))
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("hooks 配置格式异常")
        command = shlex.join([sys.executable, str(Path(__file__).resolve())])
        for event in EVENTS:
            rows = hooks.setdefault(event, [])
            if not isinstance(rows, list):
                raise ValueError("Hook 列表格式异常")
            own = {"matcher": "", "hooks": [{"type": "command", "command": command, "timeout": 15}]}
            if not any(isinstance(row, dict) and isinstance(row.get("hooks"), list) and any(
                    isinstance(hook, dict) and isinstance(hook.get("command"), str)
                    and hook["command"].endswith(shlex.quote(str(Path(__file__).resolve())))
                    for hook in row["hooks"]) for row in rows):
                rows.append(own)
        if settings != original:
            backups = data / "attribution-backups"
            backups.mkdir(exist_ok=True, mode=0o700)
            atomic_json(backups / (uuid.uuid4().hex + ".json"),
                        {"path": str(path), "existed": path.exists(), "settings": original})
            atomic_json(path, settings)
        state = data / "attribution.json"
        if not state.exists():
            atomic_json(state, {"enabledAt": time.time(), "version": 1})


def launcher_environment(profile, data, home):
    accounts = read_json(data / "accounts.json").get("accounts", [])
    linked = profile.resolve() == (home / ".claude").resolve()
    managed = any(isinstance(a, dict) and a.get("source") == "managed"
                  and profile.resolve() == (data / "profiles" / a.get("id", "")).resolve() for a in accounts)
    if not linked and not managed:
        return {}
    who, _ = identity(profile, home, linked)
    owner = who.get("accountUuid")
    if not owner:
        return {}
    install_hooks(profile, data)
    return {"CLAUDE_ACCOUNT_WEB_DATA": str(data), "CLAUDE_ACCOUNT_WEB_RUN": uuid.uuid4().hex,
            "CLAUDE_ACCOUNT_WEB_OWNER": owner, "CLAUDE_ACCOUNT_WEB_PID": str(os.getpid()),
            "CLAUDE_ACCOUNT_WEB_TICKS": process_ticks(os.getpid())}


def transcript_files(path):
    return [path, *sorted(path.with_suffix("").glob("subagents/**/*.jsonl"))]


def handle_hook(data, context, payload):
    event, session = payload.get("hook_event_name"), text(payload.get("session_id"))
    if event not in EVENTS or not session or not context.get("run") or not context.get("owner"):
        return
    raw = payload.get("transcript_path")
    if not isinstance(raw, str):
        return
    transcript = Path(raw).expanduser().resolve()
    # Hooks can only read transcripts inside this process's selected projects.
    projects = Path(context["profile"]).resolve() / "projects"
    if not transcript.is_relative_to(projects.resolve()) or transcript.suffix != ".jsonl":
        return
    paths = [path.resolve() for path in transcript_files(transcript)
             if path.resolve().is_relative_to(projects.resolve())]
    agent = payload.get("agent_transcript_path")
    if isinstance(agent, str):
        agent = Path(agent).expanduser().resolve()
        if agent.is_relative_to(transcript.with_suffix("")) and agent.suffix == ".jsonl" and agent not in paths:
            paths.append(agent)
    run, owner = context["run"], context["owner"]
    with database(data) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT started,unsafe FROM runs WHERE run=? AND session=?", (run, session)).fetchone()
        if event == "SessionStart":
            if row:
                return  # Compaction must not reset this run's baseline.
            now = time.time()
            overlaps = [(r, s) for r, s, pid, ticks in db.execute(
                "SELECT run,session,pid,ticks FROM runs WHERE session=? AND ended=0 AND run<>?", (session, run))
                if ticks and process_ticks(pid) == ticks]
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,0,?)",
                       (run, session, owner, now, context["pid"], context["ticks"], int(bool(overlaps))))
            for other_run, other_session in overlaps:
                db.execute("UPDATE runs SET unsafe=1 WHERE run=? AND session=?", (other_run, other_session))
                db.execute("DELETE FROM claims WHERE run=? AND session=?", (other_run, other_session))
            for path in paths:
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                db.execute("INSERT OR REPLACE INTO offsets VALUES (?,?,?,?,?,?)",
                           (run, session, str(path), stat.st_dev, stat.st_ino, stat.st_size))
            return
        if not row:
            return  # No start baseline: never claim an existing transcript.
        started, unsafe = row
        if not unsafe:
            for path in paths:
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                old = db.execute("SELECT device,inode,offset FROM offsets WHERE run=? AND session=? AND file=?", (run, session, str(path))).fetchone()
                offset = old[2] if old and old[:2] == (stat.st_dev, stat.st_ino) and stat.st_size >= old[2] else 0
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while True:
                        position = stream.tell()
                        raw = stream.readline(MAX_LINE + 1)
                        if not raw or not raw.endswith(b"\n"):
                            break
                        offset = stream.tell()
                        if len(raw) > MAX_LINE:
                            while raw and not raw.endswith(b"\n"):
                                raw = stream.readline(MAX_LINE + 1)
                            offset = stream.tell()
                            continue
                        try:
                            entry = parse_entry(raw, f"line:{path}:{position}")
                        except (ValueError, TypeError):
                            continue
                        # Replayed old messages retain their timestamps; exclude
                        # them even if copied into a newly created transcript.
                        if entry and entry[1] is not None and started <= entry[1] <= time.time() + 5:
                            db.execute("INSERT OR IGNORE INTO claims VALUES (?,?,?,?,?)", (run, session, entry[0], owner, entry[1]))
                db.execute("INSERT OR REPLACE INTO offsets VALUES (?,?,?,?,?,?)", (run, session, str(path), stat.st_dev, stat.st_ino, offset))
        if event == "SessionEnd":
            db.execute("UPDATE runs SET ended=1 WHERE run=? AND session=?", (run, session))


class OwnerIndex:
    def __init__(self, home):
        self.owners = {}
        for root in [home / ".config/Claude", *sorted((home / ".config").glob("Claude-*"))]:
            for path in (root / "claude-code-sessions").glob("*/*/local_*.json"):
                owner = path.parents[1].name.casefold()
                session = read_json(path).get("cliSessionId")
                if re.fullmatch(UUID, owner) and isinstance(session, str):
                    self.owners.setdefault(session.casefold(), set()).add(owner)

    def for_file(self, path):
        parts = path.parts
        session = parts[parts.index("subagents") - 1] if "subagents" in parts else path.stem
        owners = self.owners.get(session.casefold())
        if owners:
            return {(owner, "desktop-session") for owner in owners}
        for part in parts:
            match = re.search(r"scratch-workspaces-(" + UUID + r")-" + UUID, part, re.I)
            if match:
                return {(match[1].casefold(), "scratch-workspace")}
        # Old transcripts carry the owner marker at session level. Child
        # transcripts inherit the parent marker, rather than an unrelated file.
        source = Path(*parts[:parts.index("subagents")]).with_suffix(".jsonl") if "subagents" in parts else path
        found = set()
        try:
            with source.open("rb") as stream:
                while True:
                    raw = stream.readline(MAX_LINE + 1)
                    if not raw:
                        break
                    if len(raw) <= MAX_LINE and b'"ownerAccountUuid"' in raw:
                        try:
                            record = json.loads(raw)
                            if record.get("type") == "bridge-session" and re.fullmatch(UUID, text(record.get("ownerAccountUuid")), re.I):
                                found.add((record["ownerAccountUuid"].casefold(), "legacy-session"))
                        except (ValueError, AttributeError):
                            pass
        except OSError:
            pass
        return found


def sync_live_evidence(data):
    """Collect flushed replies while a turn is still running, using its fixed identity."""
    path = data / "account-attribution.sqlite3"
    if not path.exists():
        return
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as db:
        runs = list(db.execute("SELECT run,session,owner,pid,ticks FROM runs WHERE ended=0 AND unsafe=0"))
        files = {(run, session): file for run, session, file in db.execute(
            "SELECT run,session,file FROM offsets WHERE file NOT LIKE '%/subagents/%' ORDER BY file")}
    for run, session, owner, pid, ticks in runs:
        if not ticks or process_ticks(pid) != ticks:
            continue
        try:
            env = dict(item.split(b"=", 1) for item in (Path('/proc') / str(pid) / 'environ').read_bytes().split(b'\0') if b'=' in item)
            if env.get(b'CLAUDE_ACCOUNT_WEB_RUN', b'').decode() != run or env.get(b'CLAUDE_ACCOUNT_WEB_OWNER', b'').decode() != owner:
                continue
            profile = env[b'CLAUDE_CONFIG_DIR'].decode()
            transcript = files.get((run, session))
            if transcript:
                handle_hook(data, {"run": run, "owner": owner, "pid": pid, "ticks": ticks, "profile": profile},
                            {"hook_event_name": "Stop", "session_id": session, "transcript_path": transcript})
        except (OSError, KeyError, UnicodeError):
            continue


def load_evidence(data):
    cutoff = read_json(data / "attribution.json").get("enabledAt", 0)
    claims = {}
    path = data / "account-attribution.sqlite3"
    if path.exists():
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as db:
            for key, owner in db.execute("SELECT DISTINCT c.key,c.owner FROM claims c JOIN runs r ON r.run=c.run AND r.session=c.session WHERE r.unsafe=0"):
                claims.setdefault(key, set()).add(owner)
    return cutoff, claims


def resolve_owner(key, at, explicit, historical, claims, cutoff):
    observed = claims.get(key, set()) if not cutoff or (at is not None and at >= cutoff) else set()
    if len(observed | explicit) > 1:
        return "", "conflict"
    if observed:
        return next(iter(observed)), "runtime"
    if explicit:
        return next(iter(explicit)), "record"
    if not cutoff or (at is not None and at < cutoff):
        owners = {owner for owner, _ in historical}
        if len(owners) == 1:
            owner = next(iter(owners))
            return owner, next(basis for candidate, basis in historical if candidate == owner)
        if owners:
            return "", "conflict"
    return "", "unattributed"


def main():
    try:
        data = Path(os.environ["CLAUDE_ACCOUNT_WEB_DATA"])
        context = {"run": os.environ["CLAUDE_ACCOUNT_WEB_RUN"], "owner": os.environ["CLAUDE_ACCOUNT_WEB_OWNER"],
                   "pid": int(os.environ["CLAUDE_ACCOUNT_WEB_PID"]), "ticks": os.environ["CLAUDE_ACCOUNT_WEB_TICKS"],
                   "profile": os.environ["CLAUDE_CONFIG_DIR"]}
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) <= 1024 * 1024:
            handle_hook(data, context, json.loads(raw))
    except Exception:
        # Read-only bookkeeping must never block or change a Claude response.
        pass


if __name__ == "__main__":
    main()
