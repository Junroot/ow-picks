#!/usr/bin/env python3
"""등급별 '모든 전장' 수치를 data 브랜치에 날짜별 스냅샷으로 쌓는다.

넥슨 통계는 패치가 적용될 때만 갱신되므로 매일 받아도 대개 같은 수치다. 그래서
수치가 직전 스냅샷과 같으면 저장하지 않고, 스냅샷 파일 하나를 한 패치 구간의 시작으로
본다. 현재 구간은 가장 최근 스냅샷, 직전 구간은 그 앞 스냅샷이다.

출력: {history}/snapshots/{YYYY-MM-DD}.json  (날짜는 한국 시간)
  {"fetchedAt": ..., "region": "korea", "tiers": {tier: {hero: [픽률, 밴률, 승률]}}}
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fetch_rates import BASELINE_MAP, DATA_DIR, log, shard_name, warn, write_json

KST = timezone(timedelta(hours=9))
# 수집 지역이 늘어도 분석은 한국만 한다.
ANALYSIS_REGION = "korea"


def build_snapshot(data_dir: Path, region: str = ANALYSIS_REGION) -> tuple[dict | None, list[str]]:
    """샤드들의 기준선 수치를 스냅샷 하나로 묶는다. 쓸 수 없으면 (None, 이유들).

    수집 확인 단계는 기준선 칸이 빠진 샤드도 배포한다(빈 칸 몇 개는 허용). 그런 날을
    스냅샷으로 남기면 수집 실패가 수치 변화로 보여 가짜 구간이 생기므로 그날은 건너뛴다.
    """
    meta = json.loads((data_dir / "meta.json").read_text(encoding="utf-8"))
    baseline = meta.get("baselineMap") or BASELINE_MAP
    tiers: dict[str, dict[str, list]] = {}
    problems: list[str] = []
    for tier in meta["tiers"]:
        path = data_dir / shard_name(tier, region)
        if not path.exists():
            problems.append(f"{path.name} 없음")
            continue
        shard = json.loads(path.read_text(encoding="utf-8"))
        rows = shard.get("maps", {}).get(baseline)
        if baseline in shard.get("missing", []) or rows is None:
            problems.append(f"{tier}: 기준선({baseline}) 칸이 비었습니다")
            continue
        empty = sorted(hero for hero, values in rows.items() if any(value is None for value in values))
        if empty:
            problems.append(f"{tier}: 기준선 값이 빈 영웅 {', '.join(empty[:5])}")
            continue
        tiers[tier] = {hero: list(values) for hero, values in sorted(rows.items())}
    if problems:
        return None, problems
    snapshot = {"fetchedAt": meta.get("generatedAt"), "region": region, "tiers": tiers}
    return snapshot, []


def snapshot_date(snapshot: dict) -> str:
    """수집 시각의 한국 날짜. 시각이 없으면 지금."""
    fetched = snapshot.get("fetchedAt")
    moment = datetime.fromisoformat(fetched) if fetched else datetime.now(timezone.utc)
    return moment.astimezone(KST).date().isoformat()


def load_snapshots(history: Path) -> list[tuple[str, dict]]:
    """(날짜, 스냅샷) 목록, 오래된 것부터."""
    folder = history / "snapshots"
    return [
        (path.stem, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(folder.glob("*.json"))
    ]


def _normalized(tiers: dict) -> str:
    # 파일에서 읽은 값과 방금 만든 값을 같은 모양으로 비교한다 (정수/실수, 키 순서).
    return json.dumps(
        {tier: {hero: [float(v) for v in values] for hero, values in rows.items()} for tier, rows in tiers.items()},
        sort_keys=True,
    )


def record(history: Path, snapshot: dict) -> Path | None:
    """수치가 가장 최근 스냅샷과 다를 때만 저장한다. 저장한 경로 또는 None."""
    existing = load_snapshots(history)
    if existing and _normalized(existing[-1][1]["tiers"]) == _normalized(snapshot["tiers"]):
        return None
    # 같은 날 다시 돌려 수치가 바뀌었다면 그날 파일을 덮어쓴다. 앞 구간은 그대로다.
    path = history / "snapshots" / f"{snapshot_date(snapshot)}.json"
    write_json(path, snapshot)
    return path


def periods(snapshots: list[tuple[str, dict]]) -> tuple[dict | None, dict | None]:
    """(현재 구간, 직전 구간). 각 구간은 {"since": 날짜, "tiers": ...}. 없으면 None."""
    def as_period(item: tuple[str, dict]) -> dict:
        return {"since": item[0], "tiers": item[1]["tiers"]}

    current = as_period(snapshots[-1]) if snapshots else None
    previous = as_period(snapshots[-2]) if len(snapshots) >= 2 else None
    return current, previous


def main() -> int:
    parser = argparse.ArgumentParser(description="등급별 기준선 수치 스냅샷 기록")
    parser.add_argument("--history", type=Path, default=Path("history"), help="data 브랜치 폴더")
    parser.add_argument("--data", type=Path, default=DATA_DIR, help="수집기 출력 폴더")
    args = parser.parse_args()

    snapshot, problems = build_snapshot(args.data)
    if snapshot is None:
        # 통계 배포와 사실 표는 기존 스냅샷으로 계속한다.
        warn("기준선 칸이 온전하지 않아 오늘은 스냅샷을 남기지 않습니다 — " + "; ".join(problems[:5]))
        return 0
    path = record(args.history, snapshot)
    if path is None:
        log("수치가 가장 최근 스냅샷과 같아 저장하지 않습니다.")
    else:
        log(f"새 스냅샷: {path} (등급 {len(snapshot['tiers'])}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
