"""Codex usage/ratelimit Python port from QB Gate (modified integration).

Based on QB Gate, Copyright (C) 2026 smithtaylor7748-ops.
Source code: https://github.com/smithtaylor7748-ops/qb-gate
AGPL-3.0-only with LICENSE-ADDITIONAL-TERMS.md; see NOTICE.md for provenance.
Adds incremental SQLite indexing; never stores conversation content or auth.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from usage_stats import MAX_LINE, count, numeric, text, timestamp

PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICE_CHECKED = "2026-11-08"
# Official Standard API USD / 1M tokens, short then long context:
# ordinary input, output, cache write, cached input. Long = >272K input.
# These are reference API equivalents, independent of the user's subscription.
PRICES = {
    "gpt-6.1-terra": ((1, 5, 1.25, .05), (2, 7.5, 2.5, .1)),
    "gpt-5.6-terra": ((2, 12, 2.5, .2), (4, 18, 5, .4)),
    "gpt-5.6-luna": ((.2, 1.2, .25, .02), (.4, 1.8, .5, .04)),
    "gpt-6.1-sol": ((2, 10, 2.5, .1), (4, 15, 5, .2)),
    "gpt-6-astra": ((10, 50, 12.5, 1), (20, 75, 25, 2)),
    "gpt-6-luna": ((.1, .5, .125, .01), (.2, .75, .25, .02)),
    "gpt-6-sol": ((2, 10, 2.5, .2), (4, 15, 5, .4)),
    "gpt-5.6-sol": ((4, 20, 5, .4), (8, 30, 10, .8)),
}


def price(entry, last_input, delta_input, write_known):
    rates = PRICES.get(entry["model"])
    if not rates or not write_known or last_input is None or last_input > delta_input:
        return None
    # A gap in cumulative samples may cover several requests. Above the short
    # threshold their separate context tiers cannot be recovered from the last.
    if delta_input > 272000 and delta_input != last_input:
        return None
    inp, out, write, hit = rates[int(last_input > 272000)]
    return (entry["inputTokens"] * inp + entry["outputTokens"] * out
            + entry["cacheWriteTokens"] * write + entry["cacheReadTokens"] * hit) / 1e6


def window_name(minutes):
    if minutes > 0 and minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes > 0 and minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟" if minutes > 0 else "窗口长度未知"


def parse_quota(limits, at):
    if not isinstance(limits, dict) or at is None:
        return None
    windows = []
    for key in ("primary", "secondary"):
        value = limits.get(key)
        if not isinstance(value, dict) or not numeric(value.get("used_percent")):
            continue
        minutes = count(value.get("window_minutes"))
        reset = value.get("resets_at")
        reset_at = None
        if numeric(reset) and 0 < reset < 253402300799:
            reset_at = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        windows.append({"label": window_name(minutes), "windowMinutes": minutes,
                        "usedPercent": round(max(0, min(100, value["used_percent"])), 1),
                        "measuredAt": at, "resetAt": reset_at, "source": "codex-log"})
    if not windows:
        return None  # An empty snapshot must never erase the last valid reading.
    return {"windows": windows, "planType": text(limits.get("plan_type")) or None}


def quota_report(snapshot, now=None):
    now = time.time() if now is None else now
    result = {"accountId": "codex-local", "provider": "codex", "name": "Codex · 本机快照（账号未确认）",
              "windows": []}
    if snapshot:
        result["planType"] = snapshot["planType"]
        for original in snapshot["windows"]:
            window = dict(original)
            reset = timestamp(window["resetAt"])
            window.update(ageMinutes=max(0, int((now - window["measuredAt"]) / 60)),
                          resetPassed=reset is not None and reset <= now,
                          stale=(reset is not None and reset <= now) or now - window["measuredAt"] > 1800)
            result["windows"].append(window)
    return result


def aggregate(samples):
    """Reference algorithm: per-session cumulative differences, including history
    before the display range; reasoning is already included in output tokens.
    """
    sessions = {}
    entries, duplicates, incomplete = [], 0, 0
    for row in samples:
        if not row["session"]:
            incomplete += 1
            continue
        sessions.setdefault(row["session"], []).append(row)
    for rows in sessions.values():
        previous = (0, 0, 0, 0, 0, 0)
        previous_write_known = True
        seen = set()
        for row in sorted(rows, key=lambda r: r["at"]):
            current = tuple(row["counts"])
            key = (row["at"], current)
            if key in seen or current == previous:
                duplicates += 1
                continue
            seen.add(key)
            if any(current[i] < previous[i] for i in (0, 1, 4)):
                incomplete += 1
                continue  # Do not reset the baseline on a replay/counter reset.
            inp, out = current[0] - previous[0], current[1] - previous[1]
            hit = min(inp, max(0, current[2] - previous[2]))
            write_delta = max(0, current[5] - previous[5])
            written = min(inp - hit, write_delta)
            write_known = (row["writeKnown"] and previous_write_known
                           and write_delta == written and current[5] >= previous[5])
            reasoning = min(out, max(0, current[3] - previous[3]))
            previous = current
            previous_write_known = row["writeKnown"]
            if not inp + out:
                continue
            entry = {"provider": "codex", "timestamp": row["at"], "model": row["model"],
                     "inputTokens": inp - hit - written, "outputTokens": out, "cacheReadTokens": hit,
                     "cacheWriteTokens": written, "cacheWrite1hTokens": 0, "reasoningTokens": reasoning,
                     "owner": "", "speed": "standard", "geo": "", "tier": "standard",
                     "date": datetime.fromtimestamp(row["at"]).astimezone().date().isoformat(),
                     "estimatedUsd": None}
            entry["estimatedUsd"] = price(entry, row["lastInput"], inp, write_known)
            if entry['estimatedUsd'] is not None:
                input_rate, output_rate, write_rate, hit_rate = PRICES[entry['model']][int(row['lastInput'] > 272000)]
                entry['costBreakdown'] = {
                    'input': entry['inputTokens'] * input_rate / 1e6,
                    'output': out * output_rate / 1e6,
                    'cacheWrite': written * write_rate / 1e6,
                    'cacheRead': hit * hit_rate / 1e6,
                }
            entries.append(entry)
    return entries, {"duplicates": duplicates, "incomplete": incomplete}


class CodexIndex:
    def __init__(self, home, stop):
        self.home = Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser()
        self.stop = stop

    def scan(self, db, progress=lambda done, total: None):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS codex_files (path TEXT PRIMARY KEY, device INTEGER,
                inode INTEGER, offset INTEGER, size INTEGER, mtime INTEGER,
                session TEXT, model TEXT, invalid INTEGER, version INTEGER);
            CREATE TABLE IF NOT EXISTS codex_records (file TEXT, offset INTEGER,
                kind TEXT, at REAL, data TEXT, PRIMARY KEY(file,offset));
        """)
        if "version" not in {r[1] for r in db.execute("PRAGMA table_info(codex_files)")}:
            db.execute("ALTER TABLE codex_files ADD COLUMN version INTEGER DEFAULT 1")
        files = sorted({p.resolve() for name in ("sessions", "archived_sessions")
                        for p in (self.home / name).rglob("*.jsonl") if p.is_file()})
        failed = 0
        known = {r[0] for r in db.execute("SELECT path FROM codex_files")}
        progress(0, len(files))
        for number, path in enumerate(files):
            if self.stop.is_set():
                raise InterruptedError()
            try:
                self._file(db, path)
            except OSError:
                failed += 1
            progress(number + 1, len(files))
        with db:
            for removed in known - {str(p) for p in files}:
                db.execute("DELETE FROM codex_records WHERE file=?", (removed,))
                db.execute("DELETE FROM codex_files WHERE path=?", (removed,))
        samples, snapshot, latest = [], None, 0
        for kind, at, data in db.execute("SELECT kind,at,data FROM codex_records ORDER BY file,offset"):
            if kind == "usage":
                samples.append(json.loads(data))
            elif at > latest and at <= time.time() + 300:
                snapshot, latest = json.loads(data), at
        entries, quality = aggregate(samples)
        quality.update(files=len(files), failedFiles=failed,
                       invalidLines=db.execute("SELECT coalesce(sum(invalid),0) FROM codex_files").fetchone()[0])
        return entries, snapshot, quality

    def _file(self, db, path):
        stat = path.stat()
        old = db.execute("SELECT device,inode,offset,size,mtime,session,model,invalid,version FROM codex_files WHERE path=?", (str(path),)).fetchone()
        if old and old[8] != 2:
            old = None  # Rebuild numeric cache once to include cache-write usage.
        if old and (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) == (old[0], old[1], old[3], old[4]):
            return
        resume = old and (stat.st_dev, stat.st_ino) == old[:2] and stat.st_size > old[3]
        offset, session, model, invalid = (old[2], old[5], old[6], old[7]) if resume else (0, "", "unknown", 0)
        with db:
            if not resume:
                db.execute("DELETE FROM codex_records WHERE file=?", (str(path),))
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
                        offset, invalid = stream.tell(), invalid + 1
                        continue
                    if not raw.endswith(b"\n"):
                        break
                    offset = stream.tell()
                    if not any(tag in raw for tag in (b'"session_meta"', b'"turn_context"', b'"token_count"')):
                        continue
                    try:
                        record = json.loads(raw)
                        payload = record.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        kind = record.get("type")
                        if kind == "session_meta":
                            session = text(payload.get("id")) or text(payload.get("session_id"))
                        elif kind == "turn_context":
                            model = text(payload.get("model")) or model
                        elif kind == "event_msg" and payload.get("type") == "token_count":
                            at = timestamp(record.get("timestamp"))
                            if at is None:
                                invalid += 1
                                continue
                            snapshot = parse_quota(payload.get("rate_limits"), at)
                            if snapshot:
                                # Same line may contain both counters and quota.
                                db.execute("INSERT OR REPLACE INTO codex_records VALUES (?,?,?,?,?)", (str(path), -position - 1, "quota", at, json.dumps(snapshot)))
                            info = payload.get("info")
                            if not isinstance(info, dict):
                                continue
                            usage = info.get("total_token_usage")
                            if not isinstance(usage, dict):
                                invalid += 1
                                continue
                            inp, out = count(usage.get("input_tokens")), count(usage.get("output_tokens"))
                            last = info.get("last_token_usage")
                            last = last.get("input_tokens") if isinstance(last, dict) else None
                            row = {"session": session, "at": at, "model": model,
                                   "counts": [inp, out, min(inp, count(usage.get("cached_input_tokens"))),
                                              min(out, count(usage.get("reasoning_output_tokens"))),
                                              count(usage.get("total_tokens", inp + out)),
                                              count(usage.get("cache_write_input_tokens"))],
                                   "writeKnown": numeric(usage.get("cache_write_input_tokens")),
                                   "lastInput": count(last) if numeric(last) else None}
                            db.execute("INSERT OR REPLACE INTO codex_records VALUES (?,?,?,?,?)", (str(path), position, "usage", at, json.dumps(row)))
                    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                        invalid += 1
            db.execute("INSERT OR REPLACE INTO codex_files VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (str(path), stat.st_dev, stat.st_ino, offset, stat.st_size, stat.st_mtime_ns, session, model, invalid, 2))
