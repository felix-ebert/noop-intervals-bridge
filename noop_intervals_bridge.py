#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

FIELDS = ("sleepSecs", "hrv", "restingHR")


def latest_backup(backup_dir):
    backups = list(Path(backup_dir).glob("*.noopbak"))
    if not backups:
        raise RuntimeError(f"no .noopbak file found in {backup_dir}")
    return max(backups, key=lambda path: path.stat().st_mtime_ns)


def stable_file(path, wait_seconds):
    before = path.stat()
    time.sleep(wait_seconds)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"backup is still changing: {path}")


def extract_database(backup, destination):
    with zipfile.ZipFile(backup) as archive:
        if archive.testzip() is not None:
            raise RuntimeError(f"invalid ZIP member in {backup}")
        names = archive.namelist()
        if names.count("noop-backup.sqlite") != 1:
            raise RuntimeError("backup must contain exactly one noop-backup.sqlite")
        unexpected = set(names) - {"noop-backup.sqlite", "settings.json"}
        if unexpected:
            raise RuntimeError(f"unexpected backup entries: {sorted(unexpected)}")
        with archive.open("noop-backup.sqlite") as source, destination.open("wb") as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)


def read_payloads(database, start_date):
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed")
        row = connection.execute(
            "SELECT id FROM pairedDevice WHERE status = 'active' LIMIT 1"
        ).fetchone()
        active_id = row[0] if row else "my-whoop"
        source_ids = list(dict.fromkeys((f"{active_id}-noop", "my-whoop-noop")))
        placeholders = ",".join("?" for _ in source_ids)
        rows = connection.execute(
            f"""
            SELECT deviceId, day, totalSleepMin, avgHrv, restingHr
            FROM dailyMetric
            WHERE deviceId IN ({placeholders}) AND day >= ?
            ORDER BY day, CASE WHEN deviceId = ? THEN 0 ELSE 1 END
            """,
            (*source_ids, start_date, source_ids[0]),
        ).fetchall()
    finally:
        connection.close()

    payloads = {}
    for _, day, sleep_minutes, hrv, resting_hr in rows:
        if day in payloads:
            continue
        payload = {}
        if sleep_minutes is not None:
            payload["sleepSecs"] = round(sleep_minutes * 60)
        if hrv is not None:
            payload["hrv"] = hrv
        if resting_hr is not None:
            payload["restingHR"] = resting_hr
        if payload:
            payloads[day] = payload
    return payloads


def load_state(path):
    if not path.exists():
        return {"days": {}}
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state.get("days"), dict):
        raise RuntimeError("invalid state file")
    return state


def payload_hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def changes(payloads, state):
    result = []
    previous_days = state["days"]
    for day in sorted(set(payloads) | set(previous_days)):
        current = payloads.get(day, {})
        previous = previous_days.get(day, {}).get("payload", {})
        update = dict(current)
        for field in FIELDS:
            if field in previous and field not in current:
                update[field] = -1
        if update and payload_hash(update) != previous_days.get(day, {}).get("hash"):
            result.append((day, update, current))
    return result


def write_wellness(base_url, api_key, day, payload):
    credentials = base64.b64encode(f"API_KEY:{api_key}".encode()).decode()
    body = {"id": day, **payload}
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/athlete/0/wellness",
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
            "User-Agent": "noop-intervals-bridge/1.0",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"Intervals.icu returned HTTP {response.status}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Intervals.icu returned HTTP {error.code}") from error


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-dir", default=os.getenv("NOOP_BACKUP_DIR"))
    parser.add_argument("--state-file", default=os.getenv("NOOP_STATE_FILE", "/var/lib/noop-intervals-bridge/state.json"))
    parser.add_argument("--start-date", default=os.getenv("NOOP_START_DATE"))
    parser.add_argument("--stability-wait", type=float, default=2)
    parser.add_argument("--live", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.backup_dir:
        raise RuntimeError("NOOP_BACKUP_DIR is required")
    if not args.start_date:
        raise RuntimeError("NOOP_START_DATE is required")
    date.fromisoformat(args.start_date)
    backup = latest_backup(args.backup_dir)
    stable_file(backup, args.stability_wait)
    state_path = Path(args.state_file)
    state = load_state(state_path)
    with tempfile.TemporaryDirectory(prefix="noop-intervals-") as directory:
        database = Path(directory) / "noop.sqlite"
        extract_database(backup, database)
        payloads = read_payloads(database, args.start_date)
    pending = changes(payloads, state)

    if not args.live:
        print(json.dumps({"backup": backup.name, "dryRun": True, "updates": [{"id": day, **payload} for day, payload, _ in pending]}, indent=2))
        return 0

    api_key = os.getenv("INTERVALS_API_KEY")
    if not api_key:
        raise RuntimeError("INTERVALS_API_KEY is required in live mode")
    base_url = os.getenv("INTERVALS_BASE_URL", "https://intervals.icu/api/v1")
    for day, update, current in pending:
        write_wellness(base_url, api_key, day, update)
        state["days"][day] = {"hash": payload_hash(update), "payload": current}
        save_state(state_path, state)
    print(f"processed {backup.name}: {len(pending)} update(s)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
