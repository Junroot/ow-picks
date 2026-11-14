import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import commentary_prompt  # noqa: E402
import verify_commentary  # noqa: E402

HEROES = {
    "junker-queen": {"name": "정커퀸", "role": "tank"},
    "mauga": {"name": "마우가", "role": "tank"},
    "soldier-76": {"name": "솔저: 76", "role": "damage"},
    "ana": {"name": "아나", "role": "support"},
}


def row(tier: str, hero: str, role: str, now: dict, prev: dict | None = None, delta: dict | None = None, changes=()) -> dict:
    return {"id": f"{tier}.{hero}", "hero": hero, "role": role, "now": now, "prev": prev, "delta": delta, "changes": list(changes)}


def stats(pick: float, ban: float, win: float, epr: float) -> dict:
    return {"pick": pick, "ban": ban, "win": win, "epr": epr}


FACTS = {
    "region": "korea",
    "current": {"since": "2026-11-07"},
    "previous": {"since": "2026-11-05"},
    "patches": [{"newsNo": 862, "date": "2026-11-06", "title": "패치", "url": "u"}],
    "tiers": {
        "grandmaster": [
            row("grandmaster", "junker-queen", "tank", stats(6.1, 1.2, 51.8, 6.2), stats(4.0, 0.9, 48.3, 4.0), stats(2.1, 0.3, 3.5, 2.2), ["862.junker-queen"]),
            row("grandmaster", "mauga", "tank", stats(3.0, 0.5, 47.0, 3.0), stats(3.5, 0.5, 48.0, 3.5), stats(-0.5, 0.0, -1.0, -0.5)),
            row("grandmaster", "soldier-76", "damage", stats(8.0, 2.0, 50.0, 8.2), stats(6.8, 2.0, 50.0, 6.9), stats(1.2, 0.0, 0.0, 1.3)),
            row("grandmaster", "ana", "support", stats(20.0, 5.0, 51.0, 21.1), stats(20.0, 5.0, 51.0, 21.1), stats(0.0, 0.0, 0.0, 0.0)),
        ],
        "silver": [
            row("silver", "junker-queen", "tank", stats(7.0, 1.0, 52.0, 7.1), stats(5.0, 1.0, 49.0, 5.1), stats(2.0, 0.0, 3.0, 2.0), ["862.junker-queen"]),
            row("silver", "mauga", "tank", stats(4.0, 1.0, 49.0, 4.0), stats(4.0, 1.0, 49.0, 4.0), stats(0.0, 0.0, 0.0, 0.0)),
        ],
    },
    "changes": {
        "862.junker-queen": {"newsNo": 862, "hero": "junker-queen", "dev": "생명력이 낮았습니다.", "lines": ["기본 생명력이 375에서 425로 증가했습니다.", "도륙: 재사용 대기시간이 9초에서 7초로 감소했습니다."]},
    },
}

JQ = "grandmaster.junker-queen"


def sentence(text: str, *cites: str) -> dict:
    return {"text": text, "cites": list(cites) or [JQ]}


def output(**overrides) -> dict:
    base = {
        "overview": sentence("정커퀸 승률이 3.5%p 올랐다."),
        "tiers": {
            "grandmaster": {"headline": sentence("정커퀸 승률이 51.8%다."), "notes": [{"hero": "junker-queen", **sentence("유효 픽률이 6.2%다.")}]},
            "silver": {"headline": sentence("정커퀸 픽률이 7.0%다.", "silver.junker-queen"), "notes": []},
        },
    }
    base.update(overrides)
    return base


class SentenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = verify_commentary._Checker(FACTS, HEROES)

    def problem(self, text: str, *cites: str) -> str | None:
        return self.checker.problem(sentence(text, *cites))

    def test_number_in_cited_row(self) -> None:
        self.assertIsNone(self.problem("그랜드마스터 승률이 3.5%p 올랐다"))

    def test_negative_delta_written_without_sign(self) -> None:
        self.assertIsNone(self.problem("마우가 승률이 1.0%p 내렸다", "grandmaster.mauga"))

    def test_number_not_in_facts(self) -> None:
        self.assertIn("4.2", self.problem("승률이 4.2%p 올랐다"))

    def test_number_from_uncited_row(self) -> None:
        self.assertIsNotNone(self.problem("마우가 승률은 47.0%다"))

    def test_unknown_id(self) -> None:
        self.assertIn("없는 ID", self.problem("정커퀸이 올랐다", "grandmaster.doomfist"))

    def test_no_cites(self) -> None:
        self.assertIsNotNone(self.checker.problem({"text": "정커퀸이 올랐다", "cites": []}))

    def test_patch_line_numbers(self) -> None:
        self.assertIsNone(self.problem("기본 생명력이 375에서 425로 늘었다", "862.junker-queen"))

    def test_patch_line_numbers_need_change_cite(self) -> None:
        self.assertIsNotNone(self.problem("기본 생명력이 375에서 425로 늘었다"))

    def test_patch_date_numbers(self) -> None:
        self.assertIsNone(self.problem("11월 6일 패치에서 도륙 재사용 대기시간이 줄었다", "862.junker-queen"))

    def test_period_since_numbers(self) -> None:
        self.assertIsNone(self.problem("11월 7일 구간에서 정커퀸 승률이 3.5%p 올랐다"))
        self.assertIsNone(self.problem("2026년 11월 5일 구간과 비교하면 승률이 3.5%p 올랐다"))

    def test_hero_name_number(self) -> None:
        self.assertIsNone(self.problem("솔저: 76의 픽률이 1.2%p 올랐다", "grandmaster.soldier-76"))

    def test_unknown_hero_slug(self) -> None:
        self.assertIsNotNone(self.checker.problem({"hero": "nobody", **sentence("올랐다")}, "nobody", note=True))


class VerifyTest(unittest.TestCase):
    def verify(self, data: dict):
        with mock.patch.object(verify_commentary, "log"), mock.patch.object(verify_commentary, "warn"):
            return verify_commentary.verify(data, FACTS, HEROES)

    def test_all_pass(self) -> None:
        commentary, report = self.verify(output())
        self.assertEqual(report["dropped"], 0)
        self.assertEqual(report["total"], 4)
        self.assertEqual(commentary["tiers"]["grandmaster"]["notes"][0]["hero"], "junker-queen")

    def test_bad_note_dropped_only(self) -> None:
        data = output()
        data["tiers"]["grandmaster"]["notes"].append({"hero": "junker-queen", **sentence("승률이 9.9%p 올랐다")})
        commentary, report = self.verify(data)
        self.assertEqual(report["dropped"], 1)
        self.assertEqual(len(commentary["tiers"]["grandmaster"]["notes"]), 1)

    def test_headline_failure_empties_tier(self) -> None:
        data = output()
        data["tiers"]["silver"]["headline"] = sentence("실버 승률이 9.9%p 올랐다", "silver.junker-queen")
        commentary, _ = self.verify(data)
        self.assertIsNone(commentary["tiers"]["silver"])
        self.assertIsNotNone(commentary["tiers"]["grandmaster"])

    def test_overview_failure_keeps_tiers(self) -> None:
        commentary, _ = self.verify(output(overview=sentence("승률이 9.9%p 올랐다")))
        self.assertIsNone(commentary["overview"])
        self.assertIsNotNone(commentary["tiers"]["grandmaster"])

    def test_mass_failure(self) -> None:
        # 40문장 중 21문장이 틀리면 전체를 버린다.
        # overview·headline 2문장 + 통과 17문장 + 실패 21문장 = 40문장
        good = [{"hero": "junker-queen", **sentence("승률이 3.5%p 올랐다")} for _ in range(17)]
        bad = [{"hero": "junker-queen", **sentence("승률이 9.9%p 올랐다")} for _ in range(21)]
        data = output()
        data["tiers"] = {"grandmaster": {"headline": sentence("정커퀸 승률이 51.8%다."), "notes": good + bad}}
        commentary, report = self.verify(data)
        self.assertEqual((report["total"], report["dropped"]), (40, 21))
        self.assertIsNone(commentary)

    def test_exactly_half_is_kept(self) -> None:
        data = output()
        data["tiers"]["grandmaster"]["notes"] = [{"hero": "junker-queen", **sentence("승률이 9.9%p")}, {"hero": "junker-queen", **sentence("승률이 8.8%p")}]
        commentary, report = self.verify(data)
        self.assertEqual((report["total"], report["dropped"]), (5, 2))
        self.assertIsNotNone(commentary)

    def test_analysis_sentences(self) -> None:
        data = output()
        data["tiers"]["grandmaster"]["analysis"] = [
            sentence("정커퀸이 승률 51.8%로 돌격 축을 잡은 것으로 보인다."),
            sentence("돌격 한 명 구도에서 9.9%p 차이가 난다."),
        ]
        commentary, report = self.verify(data)
        self.assertEqual(report["dropped"], 1)
        self.assertEqual([a["text"] for a in commentary["tiers"]["grandmaster"]["analysis"]], ["정커퀸이 승률 51.8%로 돌격 축을 잡은 것으로 보인다."])

    def test_missing_analysis_is_empty_list(self) -> None:
        commentary, _ = self.verify(output())
        self.assertEqual(commentary["tiers"]["grandmaster"]["analysis"], [])

    def test_unknown_tier_dropped(self) -> None:
        data = output()
        data["tiers"]["champion"] = {"headline": sentence("정커퀸 승률이 51.8%다."), "notes": []}
        commentary, report = self.verify(data)
        self.assertNotIn("champion", commentary["tiers"])
        self.assertEqual(report["dropped"], 1)


class ClaudeOutputTest(unittest.TestCase):
    def load(self, data: object) -> dict | None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(data, handle) if not isinstance(data, str) else handle.write(data)
        with mock.patch.object(verify_commentary, "warn"):
            return verify_commentary.load_claude_output(Path(handle.name))

    def test_structured_output(self) -> None:
        self.assertEqual(self.load({"type": "result", "is_error": False, "structured_output": output()}), output())

    def test_result_text_fallback(self) -> None:
        self.assertEqual(self.load({"type": "result", "is_error": False, "result": json.dumps(output())}), output())

    def test_error_result(self) -> None:
        self.assertIsNone(self.load({"type": "result", "is_error": True, "subtype": "error", "result": "Invalid API key"}))

    def test_not_json(self) -> None:
        self.assertIsNone(self.load("oops"))


class SignatureTest(unittest.TestCase):
    def test_same_facts_same_signature(self) -> None:
        self.assertEqual(commentary_prompt.signature(FACTS), commentary_prompt.signature(copy.deepcopy(FACTS)))

    def test_values_change_without_order_change(self) -> None:
        facts = copy.deepcopy(FACTS)
        facts["tiers"]["grandmaster"][0]["now"]["win"] = 52.0
        self.assertEqual(commentary_prompt.signature(FACTS), commentary_prompt.signature(facts))

    def test_role_order_change(self) -> None:
        facts = copy.deepcopy(FACTS)
        facts["tiers"]["grandmaster"][1]["now"]["epr"] = 9.0  # 마우가가 정커퀸을 앞선다
        self.assertNotEqual(commentary_prompt.signature(FACTS), commentary_prompt.signature(facts))

    def test_win_order_change(self) -> None:
        facts = copy.deepcopy(FACTS)
        facts["tiers"]["grandmaster"][1]["now"]["win"] = 60.0
        self.assertNotEqual(commentary_prompt.signature(FACTS), commentary_prompt.signature(facts))

    def test_patch_list_change(self) -> None:
        facts = copy.deepcopy(FACTS)
        facts["patches"] = []
        self.assertNotEqual(commentary_prompt.signature(FACTS), commentary_prompt.signature(facts))

    def test_prompt_change(self) -> None:
        before = commentary_prompt.signature(FACTS)
        with mock.patch.object(commentary_prompt, "INSTRUCTIONS", commentary_prompt.INSTRUCTIONS + "추가 지시\n"):
            self.assertNotEqual(before, commentary_prompt.signature(FACTS))

    def test_decide(self) -> None:
        sig = commentary_prompt.signature(FACTS)
        reuse = {"signature": sig, "commentary": output()}
        self.assertFalse(commentary_prompt.decide(FACTS, reuse, True)[0])
        self.assertTrue(commentary_prompt.decide(FACTS, {"signature": sig, "commentary": None}, True)[0])
        self.assertTrue(commentary_prompt.decide(FACTS, {"signature": "old", "commentary": output()}, True)[0])
        self.assertTrue(commentary_prompt.decide(FACTS, None, True)[0])
        self.assertFalse(commentary_prompt.decide(FACTS, None, False)[0])


class PromptTest(unittest.TestCase):
    def test_prompt_contains_ids_and_rules(self) -> None:
        prompt = commentary_prompt.build_prompt(FACTS, HEROES)
        self.assertIn("grandmaster.junker-queen", prompt)
        self.assertIn("### 862.junker-queen", prompt)
        self.assertIn("도륙: 재사용 대기시간이 9초에서 7초로 감소했습니다.", prompt)
        self.assertIn("~로 보인다", prompt)  # 해석은 추정 표현으로 허용한다
        self.assertIn("subrole", prompt)
        # 검증기가 허용하지 않는 숫자를 프롬프트가 가르치면 그 숫자를 쓴 문장이 모두 버려진다.
        for number in ("60", "100", "200"):
            self.assertNotIn(number, commentary_prompt.INSTRUCTIONS)
        self.assertNotIn("직전 구간이 없다", prompt)

    def test_first_period_note(self) -> None:
        facts = copy.deepcopy(FACTS)
        facts["previous"] = None
        self.assertIn("직전 구간이 없다", commentary_prompt.build_prompt(facts, HEROES))


if __name__ == "__main__":
    unittest.main()
