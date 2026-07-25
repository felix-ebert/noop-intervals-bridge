#!/usr/bin/env python3
import argparse
import base64
import csv
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, TypeAlias

FIELDS = ("sleepSecs", "hrv", "restingHR")
CSV_ENTRY = "physiological_cycles.csv"
CSV_FIELDS = {
    "sleepSecs": "Asleep duration (min)",
    "hrv": "Heart rate variability (ms)",
    "restingHR": "Resting heart rate (bpm)",
}
Payload: TypeAlias = dict[str, float | int]
State: TypeAlias = dict[str, Any]
Change: TypeAlias = tuple[str, Payload, Payload]


def latest_export(export_dir: str) -> Path | None:
    exports = list(Path(export_dir).glob("noop-export-*.zip"))
    return max(exports, key=lambda path: path.stat().st_mtime_ns) if exports else None


def stable_file(path: Path, wait_seconds: float) -> None:
    before = path.stat()
    time.sleep(wait_seconds)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"export is still changing: {path}")


def read_payloads(export: Path, start_date: str) -> dict[str, Payload]:
    with zipfile.ZipFile(export) as archive:
        if archive.testzip() is not None:
            raise RuntimeError(f"invalid ZIP member in {export}")
        if archive.namelist().count(CSV_ENTRY) != 1:
            raise RuntimeError(f"export must contain exactly one {CSV_ENTRY}")
        with archive.open(CSV_ENTRY) as source:
            text = io.TextIOWrapper(source, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            missing = {"Cycle start time", *CSV_FIELDS.values()} - set(
                reader.fieldnames or ()
            )
            if missing:
                raise RuntimeError(f"missing CSV columns: {sorted(missing)}")
            rows = list(reader)
    payloads: dict[str, Payload] = {}
    for row in rows:
        day = row["Cycle start time"].split(" ", 1)[0]
        date.fromisoformat(day)
        if day < start_date:
            continue
        payload: Payload = {}
        for field, column in CSV_FIELDS.items():
            value = row[column].strip()
            if not value:
                continue
            number = float(value)
            payload[field] = round(number * 60) if field == "sleepSecs" else number
        if payload:
            payloads[day] = payload
    return payloads


def load_state(path: Path) -> State:
    if not path.exists():
        return {"days": {}}
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state.get("days"), dict):
        raise RuntimeError("invalid state file")
    return state


def payload_hash(payload: Payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def changes(payloads: dict[str, Payload], state: State) -> list[Change]:
    result: list[Change] = []
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


def write_wellness(
    base_url: str, api_key: str, day: str, payload: Payload
) -> None:
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


def save_state(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-dir", default=os.getenv("NOOP_EXPORT_DIR"))
    parser.add_argument(
        "--state-file",
        default=os.getenv(
            "NOOP_STATE_FILE", "/var/lib/noop-intervals-bridge/state.json"
        ),
    )
    parser.add_argument("--start-date", default=os.getenv("NOOP_START_DATE"))
    parser.add_argument("--stability-wait", type=float, default=2)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--delete-export",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("NOOP_DELETE_EXPORT", "").lower() in ("1", "true", "yes"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.export_dir:
        raise RuntimeError("NOOP_EXPORT_DIR is required")
    if not args.start_date:
        raise RuntimeError("NOOP_START_DATE is required")
    date.fromisoformat(args.start_date)
    export = latest_export(args.export_dir)
    if export is None:
        print(f"no noop-export-*.zip file found in {args.export_dir}")
        return 0
    stable_file(export, args.stability_wait)
    state_path = Path(args.state_file)
    state = load_state(state_path)
    export_hash = file_hash(export)
    if state.get("exportHash") == export_hash:
        print(f"skipped unchanged export {export.name}")
        return 0
    payloads = read_payloads(export, args.start_date)
    pending = changes(payloads, state)

    if not args.live:
        updates = [{"id": day, **payload} for day, payload, _ in pending]
        print(
            json.dumps(
                {"export": export.name, "dryRun": True, "updates": updates},
                indent=2,
            )
        )
        return 0

    api_key = os.getenv("INTERVALS_API_KEY")
    if not api_key:
        raise RuntimeError("INTERVALS_API_KEY is required in live mode")
    base_url = os.getenv("INTERVALS_BASE_URL", "https://intervals.icu/api/v1")
    for day, update, current in pending:
        write_wellness(base_url, api_key, day, update)
        state["days"][day] = {"hash": payload_hash(update), "payload": current}
        save_state(state_path, state)
    state["exportHash"] = export_hash
    save_state(state_path, state)
    if args.delete_export:
        export.unlink()
    print(f"processed {export.name}: {len(pending)} update(s)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
