# NOOP Intervals.icu Bridge

A small, dependency-free bridge from [NOOP](https://github.com/ryanbr/noop) to [Intervals.icu](https://intervals.icu).

NOOP provides built-in backup functionality that creates `.noopbak` files.

This bridge reads those backups and uses the Intervals.icu API to upload NOOP-computed sleep duration, RMSSD HRV, and resting heart rate as wellness data.

## Requirements

- Python 3.10 or newer
- A directory containing NOOP `.noopbak` files
- An [Intervals.icu API key](https://intervals.icu/settings)

## Usage

Copy `.env.example` to a protected environment file and set the backup directory, first export date, and API key. Keep it readable only by the service account:

```ini
NOOP_BACKUP_DIR=/path/to/noop/backups
NOOP_STATE_FILE=/var/lib/noop-intervals-bridge/state.json
NOOP_START_DATE=2026-01-01
INTERVALS_API_KEY=your-api-key
```

Load the environment and inspect the pending updates without writing:

```bash
set -a
. ./.env
set +a
python3 noop_intervals_bridge.py
```

Add `--live` only after reviewing the dry-run:

```bash
python3 noop_intervals_bridge.py --live
```

The bridge uses Intervals.icu athlete ID `0`, which officially refers to the athlete associated with the API key.

The bridge validates the ZIP and SQLite database, selects only NOOP-computed values, and stores hashes after successful writes.

Repeated runs skip unchanged days.

If a previously exported value disappears, it is cleared in Intervals.icu with `-1`.

## Daily Service

Example systemd units are provided in `systemd/` for running the bridge every day at 10:00 in the server's local timezone.

Review the paths and service user, then install and enable them:

```bash
sudo install -d /opt/noop-intervals-bridge
sudo install -m 0755 noop_intervals_bridge.py /opt/noop-intervals-bridge/
sudo install -m 0600 .env /etc/noop-intervals-bridge.env
sudo install -m 0644 systemd/noop-intervals-bridge.service systemd/noop-intervals-bridge.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now noop-intervals-bridge.timer
```

Verify the schedule or trigger a run manually:

```bash
systemctl list-timers noop-intervals-bridge.timer
sudo systemctl start noop-intervals-bridge.service
sudo journalctl -u noop-intervals-bridge.service -n 50 --no-pager
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Disclaimer

This is an independent, unofficial project.

It is not affiliated with, endorsed by, or maintained by NOOP or Intervals.icu.
