import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import patch_notes  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HEROES = json.loads((FIXTURES / "meta_heroes.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 14, 5, 0, tzinfo=patch_notes.KST)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def structured(news_no: int, warnings: list[str] | None = None) -> dict:
    content = patch_notes.article_content(fixture(f"patchnote_{news_no}.html"), news_no)
    entry = {"newsNo": news_no, "title": "t", "slug": "s", "date": "2026-11-04"}
    with mock.patch.object(patch_notes, "warn", (warnings if warnings is not None else []).append):
        return patch_notes.structure_patch(entry, content, patch_notes.name_index(HEROES))


class ListTest(unittest.TestCase):
    def setUp(self) -> None:
        with mock.patch.object(patch_notes, "warn"):
            self.entries = patch_notes.parse_list(fixture("patchnotes_list.html"), NOW)

    def test_only_patch_notes(self) -> None:
        numbers = [entry["newsNo"] for entry in self.entries]
        self.assertIn(858, numbers)
        self.assertIn(862, numbers)
        self.assertNotIn(859, numbers)  # 2026: 5시즌 알려진 현상 안내
        self.assertNotIn(864, numbers)  # 대회 모집 공지
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_absolute_date(self) -> None:
        by_no = {entry["newsNo"]: entry for entry in self.entries}
        self.assertEqual(by_no[862]["date"], "2026-11-06")
        self.assertEqual(by_no[862]["slug"], "patch-2026-11-05")


class CreatedAtTest(unittest.TestCase):
    def test_absolute(self) -> None:
        self.assertEqual(patch_notes.parse_created_at("2026.11.06", NOW), "2026-11-06")

    def test_days_ago(self) -> None:
        self.assertEqual(patch_notes.parse_created_at("1일 전", NOW), "2026-11-13")

    def test_hours_ago_crosses_midnight(self) -> None:
        self.assertEqual(patch_notes.parse_created_at("6시간 전", NOW), "2026-11-13")
        self.assertEqual(patch_notes.parse_created_at("3시간 전", NOW), "2026-11-14")

    def test_minutes_ago(self) -> None:
        self.assertEqual(patch_notes.parse_created_at("30분 전", NOW), "2026-11-14")

    def test_uses_kst_even_if_now_is_utc(self) -> None:
        utc_now = NOW.astimezone(patch_notes.timezone.utc)  # 11-13 20:00 UTC
        self.assertEqual(patch_notes.parse_created_at("1일 전", utc_now), "2026-11-13")

    def test_unknown(self) -> None:
        self.assertIsNone(patch_notes.parse_created_at("지난주", NOW))
        self.assertIsNone(patch_notes.parse_created_at(None, NOW))

    def test_unknown_in_list_warns(self) -> None:
        page = fixture("patchnotes_list.html").replace("2026.11.06", "어제쯤")
        warnings: list[str] = []
        with mock.patch.object(patch_notes, "warn", warnings.append):
            entries = patch_notes.parse_list(page, NOW)
        by_no = {entry["newsNo"]: entry for entry in entries}
        self.assertIsNone(by_no[862]["date"])
        self.assertTrue(any("862" in message for message in warnings))


class BodyTest(unittest.TestCase):
    def test_junker_queen_general_and_ability_lines(self) -> None:
        lines = structured(858)["heroes"]["junker-queen"]["lines"]
        self.assertIn({"ability": None, "text": "기본 생명력이 375에서 425로 증가했습니다."}, lines)
        self.assertIn({"ability": "도륙", "text": "재사용 대기시간이 9초에서 7초로 감소했습니다."}, lines)

    def test_dev_comment_without_prefix(self) -> None:
        dev = structured(858)["heroes"]["junker-queen"]["dev"]
        self.assertTrue(dev.startswith("정커퀸은 생명력이 낮아"))

    def test_dev_comment_prefix_variant(self) -> None:
        # 독트린 블록은 '개발자 의견:'으로 시작한다.
        unmatched = structured(858)["unmatched"]
        self.assertTrue(unmatched[0]["dev"].startswith("독트린은"))

    def test_six_v_six_dropped(self) -> None:
        texts = [line["text"] for line in structured(858)["heroes"]["junker-queen"]["lines"]]
        self.assertFalse(any(text.endswith("(6대6)") for text in texts))
        self.assertNotIn("기본 생명력이 450에서 500으로 증가했습니다. (6대6)", texts)

    def test_general_update_label(self) -> None:
        lines = structured(858)["heroes"]["dmon"]["lines"]
        self.assertEqual(lines[0]["ability"], "추가 강타 - 주요 특전")

    def test_hotfix(self) -> None:
        warnings: list[str] = []
        patch = structured(862, warnings)
        self.assertEqual(list(patch["heroes"]), ["wrecking-ball"])
        self.assertIsNone(patch["heroes"]["wrecking-ball"]["dev"])
        self.assertEqual(len(patch["heroes"]["wrecking-ball"]["lines"]), 2)
        self.assertEqual(warnings, [])

    def test_unmatched_name(self) -> None:
        warnings: list[str] = []
        patch = structured(858, warnings)
        self.assertEqual([block["name"] for block in patch["unmatched"]], ["독트린"])
        self.assertTrue(any("독트린" in message for message in warnings))

    def test_no_hero_section_is_quiet(self) -> None:
        content = '<div class="PatchNotes-section"><h4>버그 수정</h4><ul><li>고쳤습니다.</li></ul></div>'
        warnings: list[str] = []
        entry = {"newsNo": 1, "title": "t", "slug": "s", "date": None}
        with mock.patch.object(patch_notes, "warn", warnings.append):
            patch = patch_notes.structure_patch(entry, content, {})
        self.assertEqual(patch["heroes"], {})
        self.assertEqual(warnings, [])

    def test_hero_section_without_blocks_warns(self) -> None:
        content = '<div class="PatchNotes-section"><h4>영웅 업데이트</h4><div class="Renamed">정커퀸</div></div>'
        warnings: list[str] = []
        entry = {"newsNo": 1, "title": "t", "slug": "s", "date": None}
        with mock.patch.object(patch_notes, "warn", warnings.append):
            patch = patch_notes.structure_patch(entry, content, {})
        self.assertEqual(patch["heroes"], {})
        self.assertEqual(len(warnings), 1)


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.history = Path(self.tmp.name)
        self.requested: list[str] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fake_fetch(self, url: str) -> str:
        self.requested.append(url)
        if url == patch_notes.LIST_URL:
            return fixture("patchnotes_list.html")
        for news_no in (858, 862):
            if f"/{news_no}/" in url:
                return fixture(f"patchnote_{news_no}.html")
        raise RuntimeError("본문 없음")

    def run_sync(self, fetch=None) -> list[dict]:
        with mock.patch.object(patch_notes, "warn"), mock.patch.object(patch_notes, "log"):
            return patch_notes.sync(self.history, HEROES, NOW, fetch or self.fake_fetch)

    def test_saves_new_patches_with_date(self) -> None:
        patches = {patch["newsNo"]: patch for patch in self.run_sync()}
        self.assertEqual(patches[862]["date"], "2026-11-06")
        self.assertIn("wrecking-ball", patches[862]["heroes"])
        self.assertTrue((self.history / "patches" / "858.json").exists())

    def test_cached_patch_not_refetched_and_date_kept(self) -> None:
        folder = self.history / "patches"
        folder.mkdir()
        cached = {"newsNo": 862, "title": "t", "slug": "s", "url": "u", "date": "2026-11-05", "heroes": {}, "unmatched": []}
        (folder / "862.json").write_text(json.dumps(cached), encoding="utf-8")
        patches = {patch["newsNo"]: patch for patch in self.run_sync()}
        self.assertFalse(any("/862/" in url for url in self.requested))
        self.assertEqual(patches[862]["date"], "2026-11-05")

    def test_list_failure_uses_cache(self) -> None:
        folder = self.history / "patches"
        folder.mkdir()
        (folder / "858.json").write_text(json.dumps({"newsNo": 858, "date": "2026-11-04"}), encoding="utf-8")

        def broken(url: str) -> str:
            raise RuntimeError("목록 실패")

        self.assertEqual([patch["newsNo"] for patch in self.run_sync(broken)], [858])


if __name__ == "__main__":
    unittest.main()
