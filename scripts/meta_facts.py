#!/usr/bin/env python3
"""직전 패치 구간과 현재 구간을 등급별로 비교한 사실 표를 만들고 배포 파일로 내보낸다.

  facts    스냅샷과 패치노트로 사실 표를 만든다.
  publish  사실 표에 해설(재사용·새로 검증·없음)을 붙여
           site/data/analysis.json 과 {history}/analysis/latest.json 에 쓴다.

해설 문장은 사실 표의 행 ID({tier}.{hero})와 변경 ID({newsNo}.{hero})를 인용하고,
scripts/verify_commentary.py 가 그 인용과 숫자를 대조한다. 그래서 모든 수치를 여기서
소수점 첫째 자리로 반올림해 두고, 해설도 화면도 이 값만 본다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import history as snapshots
from commentary_prompt import can_reuse, load_stored, signature
from fetch_rates import DATA_DIR, log, warn, write_json
from patch_notes import load_patches
from verify_commentary import load_claude_output, verify

# site/app.js 의 DENOM_FLOOR·effective() 와 같은 식이어야 한다. 한쪽을 바꾸면 다른 쪽도 바꾼다.
DENOM_FLOOR = 0.05  # 밴률 95% 이상에서 분모가 0으로 붕괴하는 것을 막는다
FIELDS = ("pick", "ban", "win", "epr")


def effective(stats: list | None) -> float | None:
    """[픽률, 밴률, 승률] -> 유효 픽률. site/app.js 의 effective() 와 같은 식."""
    if not stats or stats[0] is None:
        return None
    ban = 0 if stats[1] is None else stats[1]
    return stats[0] / max(1 - ban / 100, DENOM_FLOOR)


def _round(value: float) -> float:
    return round(value, 1) + 0.0  # -0.0 을 0.0 으로


def _values(stats: list) -> dict[str, float]:
    pick, ban, win = stats
    return {"pick": pick, "ban": ban, "win": win, "epr": effective(stats)}


def hero_row(tier: str, hero: str, info: dict, now: list, prev: list | None) -> dict:
    """사실 표의 행 하나. 변화량은 반올림한 값끼리가 아니라 원본 차이를 반올림한다."""
    now_raw = _values(now)
    prev_raw = _values(prev) if prev else None
    return {
        "id": f"{tier}.{hero}",
        "hero": hero,
        "role": info.get("role"),
        "subrole": info.get("subrole"),
        "now": {key: _round(now_raw[key]) for key in FIELDS},
        "prev": {key: _round(prev_raw[key]) for key in FIELDS} if prev_raw else None,
        "delta": {key: _round(now_raw[key] - prev_raw[key]) for key in FIELDS} if prev_raw else None,
        "changes": [],
    }


def linked_patches(patches: list[dict], current_since: str, previous_since: str | None) -> list[dict]:
    """구간 사이 패치. 직전 구간이 없으면 현재 구간 시작일 이전의 가장 최근 패치 하나.

    게시일이 없는 패치는 어느 구간에도 붙이지 않는다.
    """
    dated = sorted((p for p in patches if p.get("date")), key=lambda p: (p["date"], p["newsNo"]))
    if previous_since is None:
        before = [p for p in dated if p["date"] <= current_since]
        return before[-1:]
    return [p for p in dated if previous_since < p["date"] <= current_since]


def change_lines(record: dict) -> list[str]:
    return [f"{line['ability']}: {line['text']}" if line.get("ability") else line["text"] for line in record["lines"]]


def build_facts(
    current: dict,
    previous: dict | None,
    patches: list[dict],
    heroes: dict[str, dict],
    region: str = snapshots.ANALYSIS_REGION,
) -> dict:
    linked = linked_patches(patches, current["since"], previous["since"] if previous else None)

    changes: dict[str, dict] = {}
    changed_by_hero: dict[str, list[str]] = {}
    for patch in linked:
        for hero, record in patch.get("heroes", {}).items():
            if not record.get("lines") and not record.get("dev"):
                continue
            change_id = f"{patch['newsNo']}.{hero}"
            changes[change_id] = {
                "newsNo": patch["newsNo"],
                "hero": hero,
                "dev": record.get("dev"),
                "lines": change_lines(record),
            }
            changed_by_hero.setdefault(hero, []).append(change_id)

    tiers: dict[str, list[dict]] = {}
    for tier, rows in current["tiers"].items():
        prev_rows = previous["tiers"].get(tier, {}) if previous else {}
        tier_rows = []
        for hero, now in rows.items():
            row = hero_row(tier, hero, heroes.get(hero) or {}, now, prev_rows.get(hero))
            row["changes"] = list(changed_by_hero.get(hero, []))
            tier_rows.append(row)
        tiers[tier] = tier_rows

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "region": region,
        "current": {"since": current["since"]},
        "previous": {"since": previous["since"]} if previous else None,
        "patches": [
            {"newsNo": p["newsNo"], "date": p["date"], "title": p.get("title"), "url": p.get("url")}
            for p in linked
        ],
        "tiers": tiers,
        "changes": changes,
    }


# ---------- 배포 ----------


def choose_commentary(facts: dict, stored: dict | None, raw: Path | None, heroes: dict[str, dict]) -> dict | None:
    if can_reuse(stored, signature(facts)):
        log("순위 서명이 그대로라 저장된 해설을 다시 씁니다.")
        return stored["commentary"]
    if raw is None or not raw.exists():
        log("새 해설이 없습니다. 해설 없이 내보냅니다.")
        return None
    output = load_claude_output(raw)
    if output is None:
        return None
    commentary, report = verify(output, facts, heroes)
    log(f"해설 검증: 문장 {report['total']}개 중 {report['dropped']}개 버림")
    return commentary


def publish(facts: dict, commentary: dict | None, history: Path, data_dir: Path) -> None:
    site = {**facts, "commentary": commentary}
    write_json(data_dir / "analysis.json", site)
    write_json(history / "analysis" / "latest.json", {**site, "signature": signature(facts)})


def main() -> int:
    parser = argparse.ArgumentParser(description="메타 분석 사실 표")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("facts", "publish"):
        command = sub.add_parser(name)
        command.add_argument("--history", type=Path, default=Path("history"), help="data 브랜치 폴더")
        command.add_argument("--meta", type=Path, default=DATA_DIR / "meta.json")
        command.add_argument("--facts", type=Path, required=True, help="사실 표 JSON 경로")
    sub.choices["publish"].add_argument("--raw", type=Path, help="claude -p 의 JSON 출력 (없으면 해설 없음)")
    sub.choices["publish"].add_argument("--data", type=Path, default=DATA_DIR, help="사이트 데이터 폴더")
    args = parser.parse_args()

    heroes = json.loads(args.meta.read_text(encoding="utf-8"))["heroes"]

    if args.command == "facts":
        current, previous = snapshots.periods(snapshots.load_snapshots(args.history))
        if current is None:
            warn("스냅샷이 하나도 없어 사실 표를 만들지 않습니다.")
            return 1
        facts = build_facts(current, previous, load_patches(args.history), heroes)
        write_json(args.facts, facts)
        since = f"{facts['previous']['since']} → " if facts["previous"] else "(첫 구간) "
        log(f"사실 표: {since}{facts['current']['since']}, 패치 {[p['newsNo'] for p in facts['patches']]}")
        return 0

    facts = json.loads(args.facts.read_text(encoding="utf-8"))
    commentary = choose_commentary(facts, load_stored(args.history), args.raw, heroes)
    publish(facts, commentary, args.history, args.data)
    log(f"analysis.json 작성 (해설 {'있음' if commentary else '없음'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
