#!/usr/bin/env python3
"""오버워치 공식 영웅 통계(rates) 수집기.

overwatch.blizzard.com/ko-kr/rates/ 페이지는 서버 렌더링이라 페이지 HTML 안에
<blz-data-table allrows="[...]"> 속성으로 영웅 전체의 승률/픽률/밴률 JSON이 들어 있다.
별도 API나 인증, 브라우저가 필요 없다.

역할(role) 필터는 클라이언트 사이드라 서버 응답에 영향을 주지 않는다. 따라서
(input, rq, tier, map, region) 조합 하나당 1회 요청으로 전 역할 데이터를 얻는다.

출력:
  site/data/meta.json                  영웅/맵/필터 메타데이터
  site/data/PC_{tier}_{region}.json    맵 31개 × 영웅 전체의 원본 수치

유효 픽률은 저장하지 않는다. 원본 수치만 저장하고 화면에서 계산한다.
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://overwatch.blizzard.com/ko-kr/rates/"
USER_AGENT = "ow-picks/1.0 (effective pick rate calculator; +https://github.com/)"

# 경쟁전 - 역할 고정. 빠른 대전(rq=0)은 밴이 없어 유효 픽률 = 원본 픽률이라 수집하지 않는다.
RQ = "1"

# 마우스·키보드만. 컨트롤러(Console)는 요청·데이터가 두 배로 늘어나는데 메타가 크게
# 달라 함께 보기도 어려워 수집하지 않는다.
INPUT = "PC"

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "site" / "data"

# 속성 값 안의 JSON 큰따옴표는 &quot; 로 이스케이프돼 있어서 [^"]* 로 안전하게 끊긴다.
_ALLROWS_RE = re.compile(r'allrows="([^"]*)"')
_MAP_SELECT_RE = re.compile(
    r'<select[^>]*data-label="map".*?</select>', re.S | re.I
)
# 선택 상자를 순서대로 훑는다. optgroup 시작/끝과 option 을 한 번에 잡아서,
# 새 모드(optgroup)가 생기거나 그룹 밖에 놓인 맵이 추가돼도 놓치지 않는다.
# option 의 이름은 </option> 유무와 무관하게 다음 태그 전까지로 읽는다.
_MAP_TOKEN_RE = re.compile(
    r'<optgroup[^>]*label="(?P<mode>[^"]*)"'
    r"|(?P<groupend></optgroup>)"
    r"|<option(?P<attrs>[^>]*)>(?P<label>[^<]*)",
    re.S,
)
_OPTION_RE = re.compile(r'<option[^>]*value="([^"]*)"', re.S)
_VALUE_RE = re.compile(r'value="([^"]*)"')
_OPTION_RQS_RE = re.compile(r'data-rqs="([^"]*)"')
_SELECT_RE = re.compile(
    r'<select[^>]*data-label="(tier|region)".*?</select>', re.S | re.I
)

_print_lock = threading.Lock()


def log(*args: object) -> None:
    with _print_lock:
        print(*args, file=sys.stderr, flush=True)


def fetch_html(params: dict[str, str], *, retries: int = 4) -> str:
    query = urllib.parse.urlencode(params)
    url = f"{BASE_URL}?{query}"
    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "gzip",
                "Accept-Language": "ko-KR,ko;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            backoff = 2**attempt
            log(f"  재시도 {attempt + 1}/{retries} ({error}) — {backoff}초 후")
            time.sleep(backoff)
    raise RuntimeError(f"요청 실패: {url}") from last_error


def parse_rows(page: str) -> list[dict]:
    """<blz-data-table allrows="..."> 에서 영웅 행 목록을 뽑는다."""
    match = _ALLROWS_RE.search(page)
    if match is None:
        raise ValueError("allrows 속성을 찾지 못했습니다. 페이지 구조가 바뀐 것 같습니다.")
    return json.loads(html.unescape(match.group(1)))


def _number(value: object) -> float | None:
    """'--'(데이터 부족)와 null을 None으로 정규화한다."""
    if isinstance(value, (int, float)):
        return float(value)
    return None


def extract_stats(page: str) -> dict[str, list[float | None]]:
    """영웅 id -> [픽률, 밴률, 승률]"""
    stats: dict[str, list[float | None]] = {}
    for row in parse_rows(page):
        cells = row["cells"]
        stats[row["id"]] = [
            _number(cells.get("pickrate")),
            _number(cells.get("banrate")),
            _number(cells.get("winrate")),
        ]
    return stats


def extract_heroes(page: str) -> dict[str, dict]:
    heroes: dict[str, dict] = {}
    for row in parse_rows(page):
        hero = row["hero"]
        heroes[row["id"]] = {
            "name": hero.get("name") or row["cells"].get("name"),
            "role": hero.get("role"),
            "subrole": hero.get("subrole"),
            "portrait": hero.get("portrait"),
        }
    return heroes


def extract_maps(page: str) -> list[dict]:
    """맵 목록을 게임 모드(optgroup label)와 함께, 사이트에 나오는 순서대로 뽑는다.

    새 맵이나 새 게임 모드가 추가되면 그대로 따라온다. 어느 그룹에도 속하지 않은
    맵은 '기타'로 묶는다. 기준선인 all-maps 와 경쟁전에 없는 맵은 제외한다.
    """
    select = _MAP_SELECT_RE.search(page)
    if select is None:
        raise ValueError("맵 선택 상자를 찾지 못했습니다.")

    maps: list[dict] = []
    mode = None
    for token in _MAP_TOKEN_RE.finditer(select.group(0)):
        if token.group("mode") is not None:
            mode = html.unescape(token.group("mode")).strip()
            continue
        if token.group("groupend") is not None:
            mode = None
            continue

        attrs = token.group("attrs")
        slug_match = _VALUE_RE.search(attrs)
        if slug_match is None or slug_match.group(1) == "all-maps":
            continue
        rqs_match = _OPTION_RQS_RE.search(attrs)
        available_in = rqs_match.group(1).split(",") if rqs_match else [RQ]
        if RQ not in available_in:
            continue  # 경쟁전에 없는 맵은 건너뛴다
        name = html.unescape(token.group("label")).strip()
        maps.append(
            {
                "slug": slug_match.group(1),
                "name": name or slug_match.group(1),
                "mode": mode or "기타",
            }
        )
    return maps


def extract_filter_options(page: str) -> dict[str, list[str]]:
    """티어·지역 선택 상자의 값 목록. 새 티어나 지역이 생기면 그대로 따라온다."""
    options: dict[str, list[str]] = {}
    for select in _SELECT_RE.finditer(page):
        options[select.group(1)] = _OPTION_RE.findall(select.group(0))
    return options


def shard_name(tier: str, region: str) -> str:
    return f"{INPUT}_{tier}_{region}.json"


def build_shard(tier: str, region: str, maps: list[dict], *, delay: float) -> dict:
    per_map: dict[str, dict[str, list[float | None]]] = {}
    # 'all-maps' 는 맵 편차를 재는 기준선으로 함께 받아둔다.
    for slug in ["all-maps"] + [game_map["slug"] for game_map in maps]:
        params = {
            "input": INPUT,
            "map": slug,
            "region": region,
            "rq": RQ,
            "tier": tier,
        }
        page = fetch_html(params)
        per_map[slug] = extract_stats(page)
        if delay:
            time.sleep(delay)
    log(f"완료: {tier} / {region} ({len(per_map)}개 맵)")
    return {
        "input": INPUT,
        "rq": RQ,
        "tier": tier,
        "region": region,
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "columns": ["pickrate", "banrate", "winrate"],
        "maps": per_map,
    }


def write_json(path: Path, payload: object) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="오버워치 영웅 통계 수집기")
    parser.add_argument(
        "--workers", type=int, default=4, help="동시 요청 수 (기본 4)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="같은 워커 안에서 요청 사이 대기 시간(초, 기본 0.3)",
    )
    parser.add_argument(
        "--tiers", help="쉼표로 구분한 티어 목록 (기본: 사이트의 전체 티어)"
    )
    parser.add_argument("--regions", help="쉼표로 구분한 지역 목록")
    parser.add_argument(
        "--limit-maps",
        type=int,
        help="맵 수를 제한 (동작 확인용)",
    )
    args = parser.parse_args()

    log("메타데이터 수집 중...")
    seed = fetch_html(
        {
            "input": INPUT,
            "map": "all-maps",
            "region": "Asia",
            "rq": RQ,
            "tier": "All",
        }
    )
    heroes = extract_heroes(seed)
    maps = extract_maps(seed)
    filters = extract_filter_options(seed)

    tiers = args.tiers.split(",") if args.tiers else filters.get("tier", ["All"])
    regions = (
        args.regions.split(",")
        if args.regions
        else filters.get("region", ["Americas", "Asia", "Europe"])
    )
    if args.limit_maps:
        maps = maps[: args.limit_maps]

    combos = [(t, r) for t in tiers for r in regions]
    log(
        f"영웅 {len(heroes)}명 / 맵 {len(maps)}개(+기준선) / 샤드 {len(combos)}개 "
        f"= 요청 {len(combos) * (len(maps) + 1)}건"
    )

    started = time.monotonic()
    total_bytes = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(build_shard, t, r, maps, delay=args.delay): (t, r)
            for t, r in combos
        }
        for future, (t, r) in futures.items():
            shard = future.result()
            total_bytes += write_json(DATA_DIR / shard_name(t, r), shard)

    write_json(
        DATA_DIR / "meta.json",
        {
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": BASE_URL,
            "rq": RQ,
            "input": INPUT,
            "heroes": heroes,
            "maps": maps,
            "tiers": tiers,
            "regions": regions,
        },
    )

    elapsed = time.monotonic() - started
    log(
        f"끝. 샤드 {len(combos)}개, {total_bytes / 1024:.0f}KB, {elapsed:.0f}초 "
        f"→ {DATA_DIR}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
