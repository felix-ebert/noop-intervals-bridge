import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from noop_intervals_bridge import changes, extract_database, payload_hash, read_payloads


class BridgeTests(unittest.TestCase):
    def make_database(self, path):
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE pairedDevice (id TEXT, status TEXT);
            CREATE TABLE dailyMetric (
                deviceId TEXT, day TEXT, totalSleepMin REAL, avgHrv REAL, restingHr INTEGER
            );
            INSERT INTO pairedDevice VALUES ('strap', 'active');
            INSERT INTO dailyMetric VALUES ('my-whoop-noop', '2026-07-22', 400, 40, 50);
            INSERT INTO dailyMetric VALUES ('strap-noop', '2026-07-22', 450.5, 55.2, 45);
            INSERT INTO dailyMetric VALUES ('health-connect', '2026-07-22', 999, 99, 99);
        """)
        connection.close()

    def test_reads_active_computed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "noop.sqlite"
            self.make_database(database)
            self.assertEqual(
                read_payloads(database, "2026-07-22"),
                {"2026-07-22": {"sleepSecs": 27030, "hrv": 55.2, "restingHR": 45}},
            )

    def test_emits_tombstone_for_removed_field(self):
        old_payload = {"sleepSecs": 27000, "hrv": 50, "restingHR": 45}
        state = {"days": {"2026-07-22": {"hash": payload_hash(old_payload), "payload": old_payload}}}
        pending = changes({"2026-07-22": {"hrv": 51, "restingHR": 45}}, state)
        self.assertEqual(pending[0][1], {"hrv": 51, "restingHR": 45, "sleepSecs": -1})

    def test_rejects_unexpected_archive_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "backup.noopbak"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("noop-backup.sqlite", b"db")
                output.writestr("unexpected", b"value")
            with self.assertRaises(RuntimeError):
                extract_database(archive, Path(directory) / "output.sqlite")


if __name__ == "__main__":
    unittest.main()
