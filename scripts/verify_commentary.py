#!/usr/bin/env python3
"""해설 문장을 사실 표와 대조해, 사실 표에 없는 숫자나 근거를 쓴 문장을 버린다.

문장(overview, 등급별 headline·analysis[]·notes[])마다 확인한다.
  1. cites 가 하나 이상이고, 모두 사실 표의 행 ID({tier}.{hero})나 변경 ID({newsNo}.{hero})다.
  2. notes 의 hero 가 meta.json 에 있는 슬러그다.
  3. 문장 속 숫자가 모두 허용 집합 안에 있다. 허용 집합은
     - 인용한 행의 now·prev·delta 값 (부호 없이 쓰므로 절댓값도)
     - 인용한 변경의 변경 줄·개발자의 의견 속 숫자와 그 패치의 게시일 숫자
     - 모든 문장 공통: 두 구간 시작일의 연·월·일, 영웅 표시 이름 속 숫자(솔저: 76 의 76)

등급의 headline 이 버려지면 그 등급 해설을 통째로 비우고, 전체 문장의 절반 넘게
버려지면 모델이 사실 표를 잘못 읽은 것으로 보고 해설 전체를 버린다.

  python scripts/verify_commentary.py --facts F --raw R   # 검증 결과를 출력 (로컬 확인용)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from fetch_rates import DATA_DIR, log, warn

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def numbers(text: str | None) -> set[float]:
    if not text:
        return set()
    return {float(match) for match in _NUMBER.findall(_THOUSANDS.sub("", text))}


class _Checker:
    def __init__(self, facts: dict, heroes: dict[str, dict]) -> None:
        self.rows = {row["id"]: row for rows in facts["tiers"].values() for row in rows}
        self.changes = facts.get("changes") or {}
        self.patch_dates = {patch["newsNo"]: patch.get("date") for patch in facts.get("patches", [])}
        self.heroes = heroes
        self.common: set[float] = set()
        for period in (facts.get("current"), facts.get("previous")):
            if period:
                self.common |= numbers(period.get("since"))
        for hero in heroes.values():
            self.common |= numbers(hero.get("name"))

    def allowed(self, cites: list[str]) -> set[float]:
        allowed = set(self.common)
        for cite in cites:
            row = self.rows.get(cite)
            if row:
                for part in ("now", "prev", "delta"):
                    for value in (row.get(part) or {}).values():
                        if value is not None:
                            allowed |= {float(value), abs(float(value))}
            change = self.changes.get(cite)
            if change:
                for line in change.get("lines", []):
                    allowed |= numbers(line)
                allowed |= numbers(change.get("dev"))
                allowed |= numbers(self.patch_dates.get(change.get("newsNo")))
        return allowed

    def problem(self, sentence: object, hero: str | None = None, *, note: bool = False) -> str | None:
        """문장이 통과하면 None, 아니면 버리는 이유."""
        if not isinstance(sentence, dict) or not isinstance(sentence.get("text"), str) or not sentence["text"].strip():
            return "문장이 비었거나 형식이 다릅니다"
        cites = sentence.get("cites")
        if not isinstance(cites, list) or not cites:
            return "근거(cites)가 없습니다"
        unknown = [cite for cite in cites if cite not in self.rows and cite not in self.changes]
        if unknown:
            return f"사실 표에 없는 ID {unknown}"
        if note and hero not in self.heroes:
            return f"meta.json 에 없는 영웅 슬러그 {hero!r}"
        extra = sorted(numbers(sentence["text"]) - self.allowed(cites))
        if extra:
            shown = ", ".join(f"{value:g}" for value in extra)
            return f"사실 표에 없는 숫자 {shown}"
        return None


def verify(output: dict, facts: dict, heroes: dict[str, dict]) -> tuple[dict | None, dict]:
    """(검증을 통과한 해설 또는 None, 보고서)."""
    checker = _Checker(facts, heroes)
    total = dropped = 0
    reasons: list[str] = []

    def check(where: str, sentence: object, hero: str | None = None, *, note: bool = False) -> bool:
        nonlocal total, dropped
        total += 1
        problem = checker.problem(sentence, hero, note=note)
        if problem:
            dropped += 1
            text = sentence.get("text") if isinstance(sentence, dict) else sentence
            reasons.append(f"{where}: {problem} — {text!r}")
        return problem is None

    overview = output.get("overview")
    result: dict = {"overview": overview if check("overview", overview) else None, "tiers": {}}

    tiers = output.get("tiers") if isinstance(output.get("tiers"), dict) else {}
    for tier, block in tiers.items():
        block = block if isinstance(block, dict) else {}
        notes = block.get("notes") if isinstance(block.get("notes"), list) else []
        analysis = block.get("analysis") if isinstance(block.get("analysis"), list) else []
        if tier not in facts["tiers"]:
            count = 1 + len(analysis) + len(notes)
            total += count
            dropped += count
            reasons.append(f"{tier}: 사실 표에 없는 등급이라 해설 {count}문장을 버립니다")
            continue
        headline_ok = check(f"{tier}.headline", block.get("headline"))
        kept_analysis = [
            sentence for index, sentence in enumerate(analysis) if check(f"{tier}.analysis[{index}]", sentence)
        ]
        kept = [
            note
            for index, note in enumerate(notes)
            if check(f"{tier}.notes[{index}]", note, note.get("hero") if isinstance(note, dict) else None, note=True)
        ]
        if not headline_ok:
            reasons.append(f"{tier}: headline 이 버려져 이 등급 해설을 비웁니다")
            result["tiers"][tier] = None
            continue
        result["tiers"][tier] = {
            "headline": block["headline"],
            "analysis": [{"text": a["text"], "cites": a["cites"]} for a in kept_analysis],
            "notes": [{"hero": n["hero"], "text": n["text"], "cites": n["cites"]} for n in kept],
        }

    report = {"total": total, "dropped": dropped, "reasons": reasons}
    for reason in reasons:
        log(f"  버림 {reason}")

    if total == 0:
        return None, report
    if dropped * 2 > total:
        warn(f"해설 문장 {total}개 중 {dropped}개가 검증에 실패해 해설 전체를 버립니다.")
        return None, report
    return result, report


def load_claude_output(path: Path) -> dict | None:
    """claude -p --output-format json 의 출력에서 해설 JSON 을 꺼낸다. 실패하면 None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        warn(f"해설 출력을 읽지 못했습니다: {error}")
        return None
    if isinstance(data, dict) and data.get("type") == "result":
        if data.get("is_error"):
            warn(f"해설 생성이 실패했습니다: {data.get('subtype')} {str(data.get('result'))[:300]}")
            return None
        structured = data.get("structured_output")
        if structured is None and isinstance(data.get("result"), str):
            try:
                structured = json.loads(data["result"])
            except json.JSONDecodeError:
                structured = None
        data = structured
    if not isinstance(data, dict) or not isinstance(data.get("tiers"), dict):
        warn("해설 출력이 스키마 모양이 아닙니다.")
        return None
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="해설 검증 (로컬 확인용)")
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True, help="claude -p 의 JSON 출력")
    parser.add_argument("--meta", type=Path, default=DATA_DIR / "meta.json")
    args = parser.parse_args()

    facts = json.loads(args.facts.read_text(encoding="utf-8"))
    heroes = json.loads(args.meta.read_text(encoding="utf-8"))["heroes"]
    output = load_claude_output(args.raw)
    if output is None:
        return 1
    commentary, report = verify(output, facts, heroes)
    passed = report["total"] - report["dropped"]
    log(f"통과 {passed}/{report['total']}")
    print(json.dumps(commentary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
