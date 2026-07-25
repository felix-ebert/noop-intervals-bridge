import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from noop_intervals_bridge import changes, file_hash, main, payload_hash, read_payloads


CSV = """Cycle start time,Cycle end time,Cycle timezone,Resting heart rate (bpm),\
Heart rate variability (ms),Asleep duration (min),Source
2026-07-22 00:00:00,,UTC+00:00,40,70.39942461791019,519.4666666666667,noop (APPROXIMATE)
2026-07-23 00:00:00,,UTC+00:00,44,70.1070906538977,486.18333333333334,noop (APPROXIMATE)
2026-07-24 00:00:00,,UTC+00:00,40,64.46524780513417,456.9,noop (APPROXIMATE)
"""


class BridgeTests(unittest.TestCase):
    def make_export(self, path: Path, csv_text: str = CSV) -> None:
        with zipfile.ZipFile(path, "w") as output:
            output.writestr("physiological_cycles.csv", csv_text)
            output.writestr("sleeps.csv", "header\n")

    def test_reads_wellness_from_noop_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "noop-export-2026-07-24.zip"
            self.make_export(export)
            self.assertEqual(
                read_payloads(export, "2026-07-23"),
                {
                    "2026-07-23": {
                        "sleepSecs": 29171,
                        "hrv": 70.1070906538977,
                        "restingHR": 44.0,
                    },
                    "2026-07-24": {
                        "sleepSecs": 27414,
                        "hrv": 64.46524780513417,
                        "restingHR": 40.0,
                    },
                },
            )

    def test_emits_tombstone_for_removed_field(self) -> None:
        old_payload = {"sleepSecs": 27000, "hrv": 50, "restingHR": 45}
        state = {
            "days": {
                "2026-07-22": {
                    "hash": payload_hash(old_payload),
                    "payload": old_payload,
                }
            }
        }
        pending = changes({"2026-07-22": {"hrv": 51, "restingHR": 45}}, state)
        self.assertEqual(pending[0][1], {"hrv": 51, "restingHR": 45, "sleepSecs": -1})

    def test_emits_uncached_older_day_when_newer_day_is_cached(self) -> None:
        newer = {"sleepSecs": 27414, "hrv": 64.46524780513417, "restingHR": 40.0}
        state = {
            "days": {
                "2026-07-24": {
                    "hash": payload_hash(newer),
                    "payload": newer,
                }
            }
        }
        payloads = {
            "2026-07-23": {
                "sleepSecs": 29171,
                "hrv": 70.1070906538977,
                "restingHR": 44.0,
            },
            "2026-07-24": newer,
        }
        pending = changes(payloads, state)
        self.assertEqual(
            pending,
            [("2026-07-23", payloads["2026-07-23"], payloads["2026-07-23"])],
        )

    def test_rejects_export_without_cycles_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "noop-export-2026-07-24.zip"
            with zipfile.ZipFile(export, "w") as output:
                output.writestr("sleeps.csv", "header\n")
            with self.assertRaises(RuntimeError):
                read_payloads(export, "2026-07-22")

    def test_file_hash_tracks_content_not_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.zip"
            second = Path(directory) / "second.zip"
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            self.assertEqual(file_hash(first), file_hash(second))
            second.write_bytes(b"changed")
            self.assertNotEqual(file_hash(first), file_hash(second))

    def test_unchanged_export_skips_api_and_keeps_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "noop-export-2026-07-24.zip"
            export.write_bytes(b"unchanged")
            state = Path(directory) / "state.json"
            state.write_text(f'{{"exportHash":"{file_hash(export)}","days":{{}}}}')
            argv = [
                "bridge",
                "--export-dir",
                directory,
                "--state-file",
                str(state),
                "--start-date",
                "2026-07-22",
                "--stability-wait",
                "0",
                "--live",
                "--delete-export",
            ]
            with (
                patch("sys.argv", argv),
                patch("noop_intervals_bridge.write_wellness") as write,
            ):
                self.assertEqual(main(), 0)
            write.assert_not_called()
            self.assertTrue(export.exists())

    def test_empty_export_directory_is_successful_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            argv = [
                "bridge",
                "--export-dir",
                directory,
                "--state-file",
                str(state),
                "--start-date",
                "2026-07-11",
                "--stability-wait",
                "0",
                "--live",
            ]
            with (
                patch("sys.argv", argv),
                patch("noop_intervals_bridge.write_wellness") as write,
            ):
                self.assertEqual(main(), 0)
            write.assert_not_called()
            self.assertFalse(state.exists())

    def test_successful_live_run_records_hash_and_deletes_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "noop-export-2026-07-24.zip"
            self.make_export(export)
            expected_hash = file_hash(export)
            state = Path(directory) / "state.json"
            argv = [
                "bridge",
                "--export-dir",
                directory,
                "--state-file",
                str(state),
                "--start-date",
                "2026-07-24",
                "--stability-wait",
                "0",
                "--live",
                "--delete-export",
            ]
            with (
                patch("sys.argv", argv),
                patch.dict("os.environ", {"INTERVALS_API_KEY": "key"}),
                patch("noop_intervals_bridge.write_wellness") as write,
            ):
                self.assertEqual(main(), 0)
            write.assert_called_once_with(
                "https://intervals.icu/api/v1",
                "key",
                "2026-07-24",
                {
                    "sleepSecs": 27414,
                    "hrv": 64.46524780513417,
                    "restingHR": 40.0,
                },
            )
            self.assertFalse(export.exists())
            self.assertEqual(json.loads(state.read_text())["exportHash"], expected_hash)

    def test_failed_live_run_keeps_export_and_does_not_record_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "noop-export-2026-07-24.zip"
            self.make_export(export)
            state = Path(directory) / "state.json"
            argv = [
                "bridge",
                "--export-dir",
                directory,
                "--state-file",
                str(state),
                "--start-date",
                "2026-07-24",
                "--stability-wait",
                "0",
                "--live",
                "--delete-export",
            ]
            with (
                patch("sys.argv", argv),
                patch.dict("os.environ", {"INTERVALS_API_KEY": "key"}),
                patch(
                    "noop_intervals_bridge.write_wellness",
                    side_effect=RuntimeError("failed"),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    main()
            self.assertTrue(export.exists())
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
