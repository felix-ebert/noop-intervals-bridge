# NOOP Intervals.icu Bridge

A small, dependency-free bridge from [NOOP](https://github.com/ryanbr/noop) to [Intervals.icu](https://intervals.icu).

This bridge reads NOOP's WHOOP-format `noop-export-*.zip` files and uses the Intervals.icu API to upload NOOP-computed sleep duration, RMSSD HRV, and resting heart rate as wellness data.

```mermaid
flowchart LR
    A[NOOP app] -->|WHOOP-format ZIP export| B[External storage]
    B -->|filesystem event| C[Bridge script]
    C -->|wellness API| D[Intervals.icu]
```

The initial version read NOOP's `.noopbak` backups. It moved to the WHOOP-format CSV export because the backups grew continuously and were unnecessarily large for transferring three daily wellness metrics.

## Requirements

- Python 3.10 or newer
- A directory containing NOOP `noop-export-*.zip` files
- An [Intervals.icu API key](https://intervals.icu/settings)

## How It Works

1. NOOP saves a WHOOP-format ZIP export to local storage.
2. A sync tool such as Nextcloud copies the ZIP to the directory watched by the bridge.
3. The systemd path unit notices the directory change and starts the bridge.
4. The bridge reads `physiological_cycles.csv` and sends changed sleep, HRV, and resting heart-rate values to Intervals.icu.
5. Successfully processed days are stored in a local state file, so unchanged data is not sent again.

The bridge scans every export from `NOOP_START_DATE`, which also backfills previously missed days. If `NOOP_DELETE_EXPORT=true`, it deletes the ZIP only after all API writes succeed.

## Configuration

Copy `.env.example` and set:

```ini
NOOP_EXPORT_DIR=/path/to/noop/exports
NOOP_STATE_FILE=/var/lib/noop-intervals-bridge/state.json
NOOP_START_DATE=2026-07-11
NOOP_DELETE_EXPORT=false
INTERVALS_API_KEY=your-api-key
```

Run without `--live` to preview changes, or add it to write to Intervals.icu:

```bash
set -a
. ./.env
set +a
python3 noop_intervals_bridge.py
python3 noop_intervals_bridge.py --live
```

For automatic processing, the bridge can run as a systemd path-triggered service using the included unit templates in `systemd/`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Disclaimer

This is an independent, unofficial project.

It is not affiliated with, endorsed by, or maintained by NOOP or Intervals.icu.
