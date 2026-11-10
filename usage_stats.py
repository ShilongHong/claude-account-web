"""Read-only Claude transcript statistics and local quota snapshots.

Only numeric usage and model identifiers are indexed. Conversation content and
credentials never enter this database. No outbound requests are made.
"""
from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta, timezone
import calendar
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

PRICE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICE_CHECKED = "2026-11-08"
# USD per million tokens: uncached input, output, 5m write, 1h write, cache read.
# Explicit model matches only: new/unknown models are not silently repriced.
PRICES = {
    "claude-opus-5-5": (4, 20, 5, 8, .2),
    "claude-sonnet-5-5": (2, 10, 2.5, 4, .1),
    "claude-haiku-5-5": (.1, .5, .125, .2, .01),
    "claude-fable-5-1": (10, 50, 12.5, 20, .25),
    "claude-mythos-5-1": (10, 50, 12.5, 20, .25),
    "claude-fable-5": (10, 50, 12.5, 20, 1),
    "claude-mythos-5": (10, 50, 12.5, 20, 1),
    "claude-opus-5": (5, 25, 6.25, 10, .5),
    "claude-opus-4-8": (5, 25, 6.25, 10, .5),
    "claude-opus-4-7": (5, 25, 6.25, 10, .5),
    "claude-opus-4-6": (5, 25, 6.25, 10, .5),
    "claude-opus-4-5": (5, 25, 6.25, 10, .5),
    "claude-opus-4-1": (15, 75, 18.75, 30, 1.5),
    "claude-opus-4": (15, 75, 18.75, 30, 1.5),
    "claude-sonnet-5": (2, 10, 2.5, 4, .2),
    "claude-sonnet-4-6": (3, 15, 3.75, 6, .3),
    "claude-sonnet-4-5": (3, 15, 3.75, 6, .3),
    "claude-sonnet-4": (3, 15, 3.75, 6, .3),
    "claude-haiku-4-5": (1, 5, 1.25, 2, .1),
    "claude-3-5-haiku": (.8, 4, 1, 1.6, .08),
}
TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheWriteTokens", "cacheReadTokens")
MAX_LINE = 8 * 1024 * 1024


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.timestamp() if date.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def count(value):
    return max(0, min(int(value), 10**12)) if numeric(value) else 0


def text(value, maximum=160):
    return value[:maximum] if isinstance(value, str) else ""


def read_json(path):
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def parse_entry(raw, fallback):
    if b'"usage"' not in raw or b'"assistant"' not in raw:
        return None
    record = json.loads(raw)
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return None
    usage = message["usage"]
    ident = text(message.get("id")) or text(record.get("uuid"))
    key = json.dumps([ident, text(record.get("requestId"))]) if ident else fallback
    write = count(usage.get("cache_creation_input_tokens"))
    breakdown = usage.get("cache_creation")
    long_write = min(write, count(breakdown.get("ephemeral_1h_input_tokens"))) if isinstance(breakdown, dict) else 0
    metadata = message.get("metadata")
    owner = (text(record.get("accountUuid")) or (text(metadata.get("account_uuid")) if isinstance(metadata, dict) else "")).casefold()
    return (key, timestamp(record.get("timestamp")), text(message.get("model")) or "unknown",
            count(usage.get("input_tokens")), count(usage.get("output_tokens")),
            write, long_write, count(usage.get("cache_read_input_tokens")),
            int(bool(message.get("stop_reason"))), owner,
            text(usage.get("speed")) or "standard", text(usage.get("inference_geo")),
            text(usage.get("service_tier")) or "standard")


def resolve_period(days=7, start=None, end=None, period=None, billing_day=1, today=None):
    today = today or datetime.now().astimezone().date()
    if period is not None and (not isinstance(period, str) or period not in {'month', 'current', 'previous', 'custom'}):
        raise ValueError('未知统计周期。')
    if not isinstance(billing_day, int) or isinstance(billing_day, bool) or not 1 <= billing_day <= 31:
        raise ValueError('结算日须为 1 至 31。')
    def boundary(offset):
        year, month = divmod(today.year * 12 + today.month - 1 + offset, 12)
        month += 1
        return date(year, month, min(billing_day, calendar.monthrange(year, month)[1]))
    if period == 'month':
        first, last = today.replace(day=1), today
    elif period in {'current', 'previous'}:
        offset = 0 if today >= boundary(0) else -1
        if period == 'previous':
            first, last = boundary(offset - 1), boundary(offset) - timedelta(days=1)
        else:
            first, last = boundary(offset), today
    elif start is not None or end is not None or period == 'custom':
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except (TypeError, ValueError):
            raise ValueError('请填写有效的开始和结束日期。') from None
    else:
        if not isinstance(days, int) or isinstance(days, bool) or days not in {1, 7, 30}:
            raise ValueError('统计区间只支持今天、7 天、30 天或自选日期。')
        first, last = today - timedelta(days=days - 1), today
    if first > last:
        raise ValueError('开始日期不能晚于结束日期。')
    if last > today:
        raise ValueError('结束日期不能晚于今天。')
    if (last - first).days >= 3660:
        raise ValueError('一次最多查询 3660 天。')
    return first, last


def price_parts(entry):
    model = re.sub(r"-\d{8}$", "", entry["model"])
    rates = PRICES.get(model)
    if rates is None:
        return None
    prompt = entry["inputTokens"] + entry["cacheWriteTokens"] + entry["cacheReadTokens"]
    if model == "claude-haiku-5-5" and prompt > 100000:
        rates = (.5, 2.5, .625, 1, .05)
    # Older long-context tiers are not priced from a single standard-rate table.
    if prompt > 200000 and model in {"claude-sonnet-4", "claude-sonnet-4-5", "claude-opus-4-5"}:
        return None
    multiplier = 1
    if entry["speed"] == "fast" and model in {"claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"}:
        multiplier *= 2
    elif entry["speed"] != "standard":
        return None
    if entry["tier"] != "standard":
        return None
    if entry["geo"] == "us":
        multiplier *= 1.1
    elif entry["geo"] not in {"", "global", "not_available"}:
        return None
    inp, out, short, long, hit = rates
    return {
        'input': multiplier * entry['inputTokens'] * inp / 1e6,
        'output': multiplier * entry['outputTokens'] * out / 1e6,
        'cacheWrite': multiplier * ((entry['cacheWriteTokens'] - entry['cacheWrite1hTokens']) * short
                                   + entry['cacheWrite1hTokens'] * long) / 1e6,
        'cacheRead': multiplier * entry['cacheReadTokens'] * hit / 1e6,
    }


def price(entry):
    parts = price_parts(entry)
    return sum(parts.values()) if parts is not None else None


def identity(profile, home, linked):
    paths = [profile / ".claude.json"]
    if linked:
        paths.append(home / ".claude.json")
    configs = [read_json(path) for path in paths]
    identities = [c.get("oauthAccount") for c in configs if isinstance(c.get("oauthAccount"), dict)]
    chosen = next((a for a in identities if a.get("accountUuid")), {})
    return {"accountUuid": text(chosen.get("accountUuid")).casefold(),
            "organizationUuid": text(chosen.get("organizationUuid")).casefold()}, configs


def quota_for(account, profile, home, now=None, allow_desktop=True):
    now = time.time() if now is None else now
    who, configs = identity(profile, home, account["source"] == "linked")
    owner, org = who.get("accountUuid"), who.get("organizationUuid")
    candidates = {"fiveHour": [], "sevenDay": []}
    for config in configs:
        cache = config.get("cachedUsageUtilization")
        if not isinstance(cache, dict) or not owner or text(cache.get("accountUuid")).casefold() != owner:
            continue
        at = cache.get("fetchedAtMs")
        windows = cache.get("utilization")
        if not numeric(at) or not isinstance(windows, dict):
            continue
        for source, target in [("five_hour", "fiveHour"), ("seven_day", "sevenDay")]:
            window = windows.get(source)
            if isinstance(window, dict) and numeric(window.get("utilization")):
                candidates[target].append((at / 1000, window["utilization"], text(window.get("resets_at")), "cli-cache"))
    if org and allow_desktop:
        for path in {home / ".config/Claude/plan-usage-history.json", profile / "plan-usage-history.json"}:
            samples = read_json(path).get("samples", [])
            if not isinstance(samples, list):
                continue
            for sample in samples:
                if not isinstance(sample, dict) or text(sample.get("org")).casefold() != org or not numeric(sample.get("t")):
                    continue
                windows = sample.get("u")
                if not isinstance(windows, dict):
                    continue
                for source, target in [("fh", "fiveHour"), ("sd", "sevenDay")]:
                    if numeric(windows.get(source)):
                        candidates[target].append((sample["t"] / 1000, windows[source], "", "desktop"))
    result = {"accountId": account["id"], "name": account["name"]}
    for key, values in candidates.items():
        values = [v for v in values if 0 < v[0] <= now + 300]
        if not values:
            result[key] = None
            continue
        at, used, reset, source = max(values, key=lambda v: v[0])
        if not reset:
            # A newer desktop percentage can use a cache's official reset time,
            # but only if that reset belongs to or follows this measurement.
            matching = [(v[0], v[2]) for v in values if v[2] and (timestamp(v[2]) or 0) > at]
            reset = max(matching)[1] if matching else ""
        reset_at = timestamp(reset)
        passed = reset_at is not None and reset_at <= now
        result[key] = {"usedPercent": round(max(0, min(100, used)), 1), "measuredAt": at,
                       "resetAt": reset or None, "source": source,
                       "ageMinutes": max(0, int((now - at) / 60)),
                       "resetPassed": passed, "stale": passed or now - at > 1800}
    return result


def empty_summary():
    return {"requests": 0, **{key: 0 for key in TOKEN_FIELDS}, "totalTokens": 0,
            "estimatedUsd": 0.0, "unpricedRequests": 0, "pricedRequests": 0,
            "costBreakdown": {key: 0.0 for key in ('input', 'output', 'cacheWrite', 'cacheRead')},
            "cacheHitRate": None, "historicalRequests": 0, "verifiedRequests": 0}


def add_to(summary, entry):
    summary["requests"] += 1
    if entry.get("attribution") in {"desktop-session", "scratch-workspace", "legacy-session"}:
        summary["historicalRequests"] += 1
    elif entry.get("attribution") in {"runtime", "record"}:
        summary["verifiedRequests"] += 1
    for field in TOKEN_FIELDS:
        summary[field] += entry[field]
    summary["totalTokens"] += sum(entry[f] for f in TOKEN_FIELDS)
    if entry["estimatedUsd"] is None:
        summary["unpricedRequests"] += 1
    else:
        summary["estimatedUsd"] += entry["estimatedUsd"]
        summary["pricedRequests"] += 1
        for key, amount in (entry.get('costBreakdown') or {}).items():
            summary['costBreakdown'][key] += amount


def finish(summary):
    inp = summary["inputTokens"] + summary["cacheWriteTokens"] + summary["cacheReadTokens"]
    summary["cacheHitRate"] = summary["cacheReadTokens"] / inp if inp else None
    summary["estimatedUsd"] = round(summary["estimatedUsd"], 6) if summary["pricedRequests"] else None
    return summary


class UsageIndex:
    def __init__(self, manager, start=True):
        self.manager = manager
        self.path = manager.data / "usage.sqlite3"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.entries = []
        self.codex_quota = None
        self.codex_quality = {}
        self.index = {"scanning": False, "completedFiles": 0, "totalFiles": 0,
                      "lastScannedAt": None, "duplicates": 0, "undatedRecords": 0,
                      "emptyReplies": 0, "invalidLines": 0, "error": None}
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._run, daemon=True, name="usage-index")
            self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                self.scan()
            except InterruptedError:
                break
            except Exception:
                with self.lock:
                    self.index.update(scanning=False, error="统计读取失败，保留上次结果；可以点击重新扫描。")
            self.wake.wait(60)

    def request_refresh(self):
        with self.lock:
            if not self.index["scanning"]:
                self.index["scanning"] = True
                self.wake.set()

    def _accounts(self):
        with self.manager.lock:
            return [(dict(a), self.manager.profile(a)) for a in self.manager.registry["accounts"]]

    def scan(self):
        with self.lock:
            self.index.update(scanning=True, completedFiles=0, error=None)
        roots = {self.manager.shared.resolve()}
        roots.update((profile / "projects").resolve() for _, profile in self._accounts() if (profile / "projects").is_dir())
        files = sorted({path.resolve() for root in roots for path in root.rglob("*.jsonl") if path.is_file()})
        with self.lock:
            self.index["totalFiles"] = len(files)
        db = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, device INTEGER,
                    inode INTEGER, offset INTEGER, size INTEGER, mtime INTEGER, invalid INTEGER);
                CREATE TABLE IF NOT EXISTS events (file TEXT, offset INTEGER, key TEXT, at REAL,
                    model TEXT, input INTEGER, output INTEGER, write INTEGER, write1h INTEGER,
                    hit INTEGER, complete INTEGER, owner TEXT, speed TEXT, geo TEXT, tier TEXT,
                    PRIMARY KEY(file, offset));
                CREATE INDEX IF NOT EXISTS events_key ON events(key);
            """)
            known = {row[0] for row in db.execute("SELECT path FROM files")}
            for index, path in enumerate(files):
                if self.stop.is_set():
                    raise InterruptedError()
                self._scan_file(db, path)
                with self.lock:
                    self.index["completedFiles"] = index + 1
            removed = known - {str(path) for path in files}
            with db:
                for path in removed:
                    db.execute("DELETE FROM events WHERE file=?", (path,))
                    db.execute("DELETE FROM files WHERE path=?", (path,))
            from account_attribution import OwnerIndex, load_evidence, resolve_owner, sync_live_evidence
            sync_live_evidence(self.manager.data)
            owner_index = OwnerIndex(self.manager.home)
            historical_files = {str(path): owner_index.for_file(path) for path in files}
            cutoff, claims = load_evidence(self.manager.data)
            best, first, owners, historical, rows = {}, {}, {}, {}, 0
            for row in db.execute("SELECT file,key,at,model,input,output,write,write1h,hit,complete,owner,speed,geo,tier FROM events"):
                rows += 1
                file, key, at, model, inp, out, write, long, hit, complete, owner, speed, geo, tier = row
                rank = (int(inp + out + write + hit > 0), complete, out, inp + out + write + hit)
                if key not in best or rank > best[key][0]:
                    best[key] = (rank, {"timestamp": at, "model": model, "inputTokens": inp,
                                       "outputTokens": out, "cacheWriteTokens": write,
                                       "cacheWrite1hTokens": long, "cacheReadTokens": hit,
                                       "owner": owner, "speed": speed, "geo": geo, "tier": tier})
                if at is not None:
                    first[key] = min(first.get(key, at), at)
                if owner:
                    owners.setdefault(key, set()).add(owner)
                historical.setdefault(key, set()).update(historical_files.get(file, set()))
            entries, undated, empty = [], 0, 0
            for key, (_, entry) in best.items():
                entry["timestamp"] = first.get(key)
                entry["owner"], entry["attribution"] = resolve_owner(
                    key, entry["timestamp"], owners.get(key, set()), historical.get(key, set()), claims, cutoff)
                if not sum(entry[f] for f in TOKEN_FIELDS):
                    empty += 1
                    continue
                if entry["timestamp"] is None:
                    undated += 1
                    continue
                try:
                    entry["date"] = datetime.fromtimestamp(entry["timestamp"]).astimezone().date().isoformat()
                except (OverflowError, ValueError, OSError):
                    undated += 1
                    continue
                entry["provider"] = "claude"
                entry["costBreakdown"] = price_parts(entry)
                entry["estimatedUsd"] = price(entry)
                entries.append(entry)
            invalid = db.execute("SELECT coalesce(sum(invalid),0) FROM files").fetchone()[0]
            from codex_stats import CodexIndex
            def progress(done, total):
                with self.lock:
                    self.index.update(completedFiles=len(files) + done, totalFiles=len(files) + total)
            codex, codex_quota, codex_quality = CodexIndex(self.manager.home, self.stop).scan(db, progress)
            with self.lock:
                self.entries = entries + codex
                self.codex_quota, self.codex_quality = codex_quota, codex_quality
                self.index.update(scanning=False, lastScannedAt=time.time(), duplicates=rows - len(best) + codex_quality["duplicates"],
                                  undatedRecords=undated, emptyReplies=empty,
                                  invalidLines=invalid + codex_quality["invalidLines"])
        finally:
            db.close()

    def _scan_file(self, db, path):
        stat = path.stat()
        previous = db.execute("SELECT device,inode,offset,size,mtime,invalid FROM files WHERE path=?", (str(path),)).fetchone()
        if previous and (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) == (previous[0], previous[1], previous[3], previous[4]):
            return
        resume = previous and (stat.st_dev, stat.st_ino) == previous[:2] and stat.st_size > previous[3]
        offset, invalid = (previous[2], previous[5]) if resume else (0, 0)
        with db:
            if not resume:
                db.execute("DELETE FROM events WHERE file=?", (str(path),))
            with path.open("rb") as stream:
                stream.seek(offset)
                while True:
                    if self.stop.is_set():
                        raise InterruptedError()
                    position = stream.tell()
                    raw = stream.readline(MAX_LINE + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_LINE:
                        while raw and not raw.endswith(b"\n"):
                            raw = stream.readline(MAX_LINE + 1)
                        if not raw:
                            break
                        invalid += 1
                        offset = stream.tell()
                        continue
                    if not raw.endswith(b"\n"):
                        break  # An actively appended final line will be retried.
                    offset = stream.tell()
                    try:
                        entry = parse_entry(raw, f"line:{path}:{position}")
                    except (ValueError, TypeError, OverflowError):
                        invalid += 1
                        continue
                    if entry:
                        db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (str(path), position, *entry))
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
                       (str(path), stat.st_dev, stat.st_ino, offset, stat.st_size, stat.st_mtime_ns, invalid))

    def report(self, days=7, provider="all", start=None, end=None, period=None, billing_day=1):
        from codex_stats import PRICE_SOURCE as CODEX_PRICE_SOURCE, PRICE_CHECKED as CODEX_PRICE_CHECKED
        first_day, last_day = resolve_period(days, start, end, period, billing_day)
        days = (last_day - first_day).days + 1
        if provider not in {"all", "claude", "codex"}:
            raise ValueError("未知统计来源。")
        today = datetime.now().astimezone().date()
        with self.lock:
            entries, index = self.entries, dict(self.index)
            codex_quota, codex_quality = self.codex_quota, dict(self.codex_quality)
        entries = [e for e in entries if provider == "all" or e["provider"] == provider]
        accounts = self._accounts()
        account_by_uuid, org_owners = {}, {}
        for account, profile in accounts:
            who, _ = identity(profile, self.manager.home, account["source"] == "linked")
            if who.get("accountUuid"):
                account_by_uuid[who["accountUuid"]] = account
            if who.get("organizationUuid"):
                org_owners.setdefault(who["organizationUuid"], set()).add(who.get("accountUuid"))
        quotas = []
        for account, profile in accounts:
            who, _ = identity(profile, self.manager.home, account["source"] == "linked")
            if provider != "codex":
                quotas.append({"provider": "claude", **quota_for(account, profile, self.manager.home,
                                    allow_desktop=len(org_owners.get(who.get("organizationUuid"), ())) == 1)})
        if provider != "claude":
            from codex_stats import quota_report
            quotas.append(quota_report(codex_quota))
        windows = {n: empty_summary() for n in (1, 7, 30)}
        selected = empty_summary()
        by_day = {(first_day + timedelta(days=i)).isoformat(): empty_summary() for i in range(days)}
        by_hour = {hour: empty_summary() for hour in range(24)}
        by_model, by_account, recent = {}, {}, []
        selected_start, selected_end = first_day.isoformat(), last_day.isoformat()
        today_iso = today.isoformat()
        starts = {n: (today - timedelta(days=n - 1)).isoformat() for n in windows}
        for entry in entries:
            date = entry["date"]
            if date > today_iso:
                continue
            for number, summary in windows.items():
                if date >= starts[number]:
                    add_to(summary, entry)
            if date == (selected_start if days == 1 else today_iso):
                hour = datetime.fromtimestamp(entry["timestamp"]).astimezone().hour
                add_to(by_hour[hour], entry)
            if not selected_start <= date <= selected_end:
                continue
            add_to(selected, entry)
            add_to(by_day[date], entry)
            label = entry["model"] + (" · " + entry["speed"] if entry["speed"] != "standard" else "")
            if entry["provider"] == "codex":
                label = "Codex · " + label
            add_to(by_model.setdefault(label, empty_summary()), entry)
            account = account_by_uuid.get(entry["owner"]) if entry["provider"] == "claude" else None
            account_id = ("codex-local" if entry["provider"] == "codex" else account["id"] if account
                          else "external:" + entry["owner"] if entry["owner"] else "unattributed")
            add_to(by_account.setdefault(account_id, empty_summary()), entry)
            recent.append({"timestamp": entry["timestamp"], "model": label,
                           "account": "Codex · 本机记录" if entry["provider"] == "codex" else account["name"] if account else "未添加账号 · " + entry["owner"][:8] if entry["owner"] else "未归属",
                           "attribution": entry.get("attribution", "unattributed"),
                           "totalTokens": sum(entry[f] for f in TOKEN_FIELDS), "estimatedUsd": entry["estimatedUsd"]})
        account_rows = []
        if provider != "codex":
            account_rows = [{"id": a["id"], "name": a["name"], **finish(by_account.get(a["id"], empty_summary()))} for a, _ in accounts]
            for ident, summary in sorted(by_account.items()):
                if ident.startswith("external:"):
                    account_rows.append({"id": ident, "name": "未添加账号 · " + ident.removeprefix("external:")[:8], **finish(summary)})
            account_rows.append({"id": "unattributed", "name": "未归属（共享历史）", **finish(by_account.get("unattributed", empty_summary()))})
        if provider != "claude" and ("codex-local" in by_account or provider == "codex"):
            account_rows.append({"id": "codex-local", "name": "Codex · 本机记录（账号未确认）", **finish(by_account.get("codex-local", empty_summary()))})
        return {"days": days, "provider": provider, "generatedAt": time.time(), "index": index, "codexQuality": codex_quality,
                "summary": finish(selected),
                "range": {"start": selected_start, "end": selected_end, "period": period, "billingDay": billing_day},
                "overview": {str(n): finish(summary) for n, summary in windows.items()},
                "daily": [{"date": day, **finish(summary)} for day, summary in by_day.items()],
                "hourly": [{"hour": hour, **finish(summary)} for hour, summary in by_hour.items()],
                "models": sorted([{"model": model, **finish(summary)} for model, summary in by_model.items()], key=lambda m: m["totalTokens"], reverse=True),
                "accounts": account_rows, "quotas": quotas,
                "recent": sorted(recent, key=lambda e: e["timestamp"], reverse=True)[:40],
                "pricing": {"source": CODEX_PRICE_SOURCE if provider == "codex" else PRICE_SOURCE,
                            "checkedAt": CODEX_PRICE_CHECKED if provider == "codex" else PRICE_CHECKED,
                            "checkedDates": {"claude": PRICE_CHECKED, "codex": CODEX_PRICE_CHECKED},
                            "sources": ([{"name": "Claude 官方价格", "url": PRICE_SOURCE}] if provider != "codex" else [])
                                     + ([{"name": "OpenAI 官方价格", "url": CODEX_PRICE_SOURCE}] if provider != "claude" else []),
                            "basis": "Codex 按官方 Standard API 价格折算；超过 272K 输入使用长上下文档位，缓存写入单独计价。不推算实际服务档位、地域附加费或订阅账单。未知模型、缓存写入字段缺失或无法区分档位的累计记录不估价。"},
                "attributionNote": "历史按桌面端会话索引、工作区标识或旧日志归属，不能拆分当时跨账号续聊的消耗；启用记录后的新增回复优先按实际运行账号归属。缺少证据或有冲突的记录单列。"}

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=3)
