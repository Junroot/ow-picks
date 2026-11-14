import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import history  # noqa: E402

TIERS = ["all", "bronze"]


class HistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.history = root / "history"
        self.data.mkdir()
        self.patcher = mock.patch.object(history, "log")
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.tmp.cleanup()

    def write_day(self, generated_at: str, ana_pick: float = 5.0, *, missing: dict | None = None, none_hero: str | None = None) -> None:
        """하루치 수집 결과(meta.json + 등급별 샤드)를 쓴다."""
        meta = {"generatedAt": generated_at, "baselineMap": "all", "tiers": TIERS}
        (self.data / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        for tier in TIERS:
            rows = {"ana": [ana_pick, 1.0, 50.0], "genji": [10.0, 2.0, 49.5]}
            if none_hero and tier == "bronze":
                rows[none_hero] = [None, 1.0, 50.0]
            maps = {"all": rows, "ilios": rows}
            tier_missing = (missing or {}).get(tier, [])
            for slug in tier_missing:
                maps.pop(slug, None)
            shard = {"missing": tier_missing, "tier": tier, "region": "korea", "maps": maps}
            (self.data / f"pc_{tier}_korea.json").write_text(json.dumps(shard), encoding="utf-8")

    def record(self) -> Path | None:
        snapshot, problems = history.build_snapshot(self.data)
        if snapshot is None:
            return None
        return history.record(self.history, snapshot)

    def saved(self) -> list[str]:
        return [date for date, _ in history.load_snapshots(self.history)]

    def test_first_snapshot(self) -> None:
        # 2026-11-13 20:10 UTC = 2026-11-14 05:10 KST
        path = self.record_day("2026-11-13T20:10:00+00:00")
        self.assertEqual(path.name, "2026-11-14.json")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["tiers"]["bronze"]["ana"], [5.0, 1.0, 50.0])
        self.assertEqual(snapshot["region"], "korea")

    def record_day(self, generated_at: str, **kwargs) -> Path | None:
        self.write_day(generated_at, **kwargs)
        return self.record()

    def test_same_values_not_saved(self) -> None:
        self.record_day("2026-11-13T20:10:00+00:00")
        self.assertIsNone(self.record_day("2026-11-14T20:10:00+00:00"))
        self.assertEqual(self.saved(), ["2026-11-14"])

    def test_changed_values_saved(self) -> None:
        self.record_day("2026-11-13T20:10:00+00:00")
        self.record_day("2026-11-14T20:10:00+00:00", ana_pick=6.1)
        self.assertEqual(self.saved(), ["2026-11-14", "2026-11-15"])

    def test_missing_baseline_skips_day(self) -> None:
        self.record_day("2026-11-13T20:10:00+00:00")
        self.write_day("2026-11-14T20:10:00+00:00", ana_pick=6.1, missing={"bronze": ["all"]})
        snapshot, problems = history.build_snapshot(self.data)
        self.assertIsNone(snapshot)
        self.assertTrue(any("bronze" in problem for problem in problems))
        self.assertEqual(self.saved(), ["2026-11-14"])

    def test_missing_other_map_is_fine(self) -> None:
        self.record_day("2026-11-13T20:10:00+00:00")
        path = self.record_day("2026-11-14T20:10:00+00:00", ana_pick=6.1, missing={"bronze": ["ilios"]})
        self.assertIsNotNone(path)

    def test_none_value_skips_day(self) -> None:
        self.write_day("2026-11-13T20:10:00+00:00", none_hero="mercy")
        snapshot, problems = history.build_snapshot(self.data)
        self.assertIsNone(snapshot)
        self.assertTrue(any("mercy" in problem for problem in problems))

    def test_recovery_next_day_keeps_periods(self) -> None:
        self.record_day("2026-11-12T20:10:00+00:00", ana_pick=4.0)
        self.record_day("2026-11-13T20:10:00+00:00", ana_pick=5.0)
        # 11-15 KST: 기준선이 비어 건너뛴다.
        self.assertIsNone(self.record_day("2026-11-14T20:10:00+00:00", missing={"all": ["all"]}))
        # 11-16 KST: 돌아왔고 수치는 11-14 스냅샷과 같다.
        self.assertIsNone(self.record_day("2026-11-15T20:10:00+00:00", ana_pick=5.0))
        current, previous = history.periods(history.load_snapshots(self.history))
        self.assertEqual(current["since"], "2026-11-14")
        self.assertEqual(previous["since"], "2026-11-13")

    def test_periods_single_snapshot(self) -> None:
        self.record_day("2026-11-13T20:10:00+00:00")
        current, previous = history.periods(history.load_snapshots(self.history))
        self.assertEqual(current["since"], "2026-11-14")
        self.assertIsNone(previous)

    def test_periods_empty(self) -> None:
        self.assertEqual(history.periods([]), (None, None))

    def test_periods_two_snapshots(self) -> None:
        self.record_day("2026-11-04T20:10:00+00:00", ana_pick=4.0)
        self.record_day("2026-11-06T20:10:00+00:00", ana_pick=6.0)
        current, previous = history.periods(history.load_snapshots(self.history))
        self.assertEqual((previous["since"], current["since"]), ("2026-11-05", "2026-11-07"))
        self.assertEqual(current["tiers"]["all"]["ana"][0], 6.0)
        self.assertEqual(previous["tiers"]["all"]["ana"][0], 4.0)


if __name__ == "__main__":
    unittest.main()
