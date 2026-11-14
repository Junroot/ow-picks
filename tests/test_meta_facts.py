import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import meta_facts  # noqa: E402

HEROES = {
    "junker-queen": {"name": "정커퀸", "role": "tank"},
    "mauga": {"name": "마우가", "role": "tank"},
    "ana": {"name": "아나", "role": "support"},
    "newbie": {"name": "신규", "role": "damage"},
}


def period(since: str, rows: dict) -> dict:
    return {"since": since, "tiers": {"grandmaster": rows}}


def patch(news_no: int, date: str | None, heroes: dict) -> dict:
    return {"newsNo": news_no, "date": date, "title": f"패치 {news_no}", "url": f"u/{news_no}", "heroes": heroes}


JQ_CHANGE = {"dev": "생명력이 낮았습니다.", "lines": [{"ability": None, "text": "기본 생명력이 375에서 425로 증가했습니다."}, {"ability": "도륙", "text": "재사용 대기시간이 9초에서 7초로 감소했습니다."}]}


def row_of(facts: dict, hero: str, tier: str = "grandmaster") -> dict:
    return next(row for row in facts["tiers"][tier] if row["hero"] == hero)


class EffectiveTest(unittest.TestCase):
    def test_formula(self) -> None:
        self.assertAlmostEqual(meta_facts.effective([10.0, 50.0, 50.0]), 20.0)

    def test_floor_at_97_ban(self) -> None:
        self.assertAlmostEqual(meta_facts.effective([2.0, 97.0, 50.0]), 40.0)

    def test_missing_pick(self) -> None:
        self.assertIsNone(meta_facts.effective([None, 1.0, 50.0]))


class FactsTest(unittest.TestCase):
    def build(self, current, previous, patches=()) -> dict:
        return meta_facts.build_facts(current, previous, list(patches), HEROES)

    def test_delta(self) -> None:
        facts = self.build(
            period("2026-11-07", {"junker-queen": [6.1, 1.2, 51.8]}),
            period("2026-11-05", {"junker-queen": [4.0, 0.9, 48.3]}),
        )
        row = row_of(facts, "junker-queen")
        self.assertEqual(row["id"], "grandmaster.junker-queen")
        self.assertEqual(row["role"], "tank")
        self.assertEqual(row["delta"]["win"], 3.5)
        self.assertEqual(row["delta"]["pick"], 2.1)

    def test_rounding_uses_raw_difference(self) -> None:
        facts = self.build(
            period("2026-11-07", {"ana": [6.27, 0.0, 50.0]}),
            period("2026-11-05", {"ana": [4.04, 0.0, 50.0]}),
        )
        row = row_of(facts, "ana")
        self.assertEqual((row["prev"]["epr"], row["now"]["epr"], row["delta"]["epr"]), (4.0, 6.3, 2.2))

    def test_no_negative_zero(self) -> None:
        facts = self.build(
            period("2026-11-07", {"ana": [5.0, 0.0, 50.0]}),
            period("2026-11-05", {"ana": [5.02, 0.0, 50.0]}),
        )
        self.assertEqual(json.dumps(row_of(facts, "ana")["delta"]["pick"]), "0.0")

    def test_epr_with_97_ban(self) -> None:
        facts = self.build(period("2026-11-07", {"ana": [2.0, 97.0, 50.0]}), None)
        self.assertEqual(row_of(facts, "ana")["now"]["epr"], 40.0)

    def test_hero_only_in_current(self) -> None:
        facts = self.build(
            period("2026-11-07", {"ana": [5.0, 1.0, 50.0], "newbie": [3.0, 1.0, 47.0]}),
            period("2026-11-05", {"ana": [5.0, 1.0, 50.0]}),
        )
        row = row_of(facts, "newbie")
        self.assertIsNone(row["prev"])
        self.assertIsNone(row["delta"])
        self.assertEqual(row["now"]["pick"], 3.0)

    def test_first_period(self) -> None:
        patches = [patch(851, "2026-10-21", {}), patch(858, "2026-11-04", {"junker-queen": JQ_CHANGE}), patch(870, "2026-11-19", {})]
        facts = self.build(period("2026-11-07", {"junker-queen": [6.1, 1.2, 51.8]}), None, patches)
        self.assertIsNone(facts["previous"])
        self.assertEqual([p["newsNo"] for p in facts["patches"]], [858])
        row = row_of(facts, "junker-queen")
        self.assertIsNone(row["prev"])
        self.assertEqual(row["changes"], ["858.junker-queen"])

    def test_two_patches_between(self) -> None:
        patches = [
            patch(858, "2026-11-04", {"junker-queen": JQ_CHANGE}),
            patch(862, "2026-11-06", {"junker-queen": {"dev": None, "lines": [{"ability": None, "text": "줄"}]}}),
            patch(850, "2026-11-03", {"junker-queen": JQ_CHANGE}),  # 직전 구간 시작일 당일: 빠진다
            patch(871, "2026-11-08", {"junker-queen": JQ_CHANGE}),  # 현재 구간 이후: 빠진다
        ]
        facts = self.build(
            period("2026-11-07", {"junker-queen": [6.1, 1.2, 51.8], "ana": [5.0, 1.0, 50.0]}),
            period("2026-11-03", {"junker-queen": [4.0, 0.9, 48.3], "ana": [5.0, 1.0, 50.0]}),
            patches,
        )
        self.assertEqual([p["newsNo"] for p in facts["patches"]], [858, 862])
        self.assertEqual(row_of(facts, "junker-queen")["changes"], ["858.junker-queen", "862.junker-queen"])
        self.assertEqual(row_of(facts, "ana")["changes"], [])
        self.assertEqual(
            facts["changes"]["858.junker-queen"]["lines"],
            ["기본 생명력이 375에서 425로 증가했습니다.", "도륙: 재사용 대기시간이 9초에서 7초로 감소했습니다."],
        )
        self.assertEqual(facts["changes"]["858.junker-queen"]["dev"], "생명력이 낮았습니다.")

    def test_patch_on_current_since_is_included(self) -> None:
        facts = self.build(
            period("2026-11-07", {"ana": [5.0, 1.0, 50.0]}),
            period("2026-11-05", {"ana": [5.0, 1.0, 50.0]}),
            [patch(862, "2026-11-07", {})],
        )
        self.assertEqual([p["newsNo"] for p in facts["patches"]], [862])

    def test_undated_patch_excluded(self) -> None:
        facts = self.build(
            period("2026-11-07", {"junker-queen": [6.1, 1.2, 51.8]}),
            period("2026-11-05", {"junker-queen": [4.0, 0.9, 48.3]}),
            [patch(862, None, {"junker-queen": JQ_CHANGE})],
        )
        self.assertEqual(facts["patches"], [])
        self.assertEqual(facts["changes"], {})
        self.assertEqual(row_of(facts, "junker-queen")["changes"], [])

    def test_undated_patch_excluded_from_first_period(self) -> None:
        facts = self.build(period("2026-11-07", {"ana": [5.0, 1.0, 50.0]}), None, [patch(858, "2026-11-04", {}), patch(862, None, {})])
        self.assertEqual([p["newsNo"] for p in facts["patches"]], [858])


class PublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.history, self.data = root / "history", root / "data"
        self.facts = meta_facts.build_facts(
            period("2026-11-07", {"junker-queen": [6.1, 1.2, 51.8]}),
            period("2026-11-05", {"junker-queen": [4.0, 0.9, 48.3]}),
            [],
            HEROES,
        )
        self.commentary = {
            "overview": {"text": "정커퀸 승률이 3.5%p 올랐다.", "cites": ["grandmaster.junker-queen"]},
            "tiers": {"grandmaster": {"headline": {"text": "정커퀸이 51.8%다.", "cites": ["grandmaster.junker-queen"]}, "notes": []}},
        }
        patcher = mock.patch.multiple(meta_facts, log=mock.DEFAULT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def stored(self) -> dict:
        return json.loads((self.history / "analysis" / "latest.json").read_text(encoding="utf-8"))

    def test_publish_writes_both_files(self) -> None:
        meta_facts.publish(self.facts, None, self.history, self.data)
        site = json.loads((self.data / "analysis.json").read_text(encoding="utf-8"))
        self.assertIsNone(site["commentary"])
        self.assertNotIn("signature", site)
        self.assertEqual(site["tiers"], self.facts["tiers"])
        self.assertEqual(self.stored()["signature"], meta_facts.signature(self.facts))

    def test_reuses_stored_commentary(self) -> None:
        meta_facts.publish(self.facts, self.commentary, self.history, self.data)
        chosen = meta_facts.choose_commentary(self.facts, self.stored(), None, HEROES)
        self.assertEqual(chosen, self.commentary)

    def test_verifies_new_raw_output(self) -> None:
        raw = Path(self.tmp.name) / "raw.json"
        raw.write_text(json.dumps({"type": "result", "is_error": False, "structured_output": self.commentary}), encoding="utf-8")
        with mock.patch("verify_commentary.log"):
            chosen = meta_facts.choose_commentary(self.facts, None, raw, HEROES)
        self.assertEqual(chosen["tiers"]["grandmaster"]["headline"]["text"], "정커퀸이 51.8%다.")

    def test_no_raw_means_no_commentary(self) -> None:
        self.assertIsNone(meta_facts.choose_commentary(self.facts, None, None, HEROES))


if __name__ == "__main__":
    unittest.main()
