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
import http.cookiejar
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

# 도구 이름을 밝힌 User-Agent 로는 GitHub Actions 러너에서 403 이 떨어졌다(집 회선에서는
# 같은 요청이 통과한다). 브라우저가 보내는 것과 같은 헤더 묶음으로 맞춘다.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# 첫 응답이 내려주는 locale/session 쿠키를 이후 요청에 그대로 실어 보낸다. 사람이 필터를
# 바꿔가며 보는 흐름과 같아진다. CookieJar 는 내부 잠금이 있어 여러 워커가 함께 써도 된다.
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)

# 경쟁전 - 역할 고정만 수집한다. 빠른 대전은 밴이 없어 유효 픽률 = 원본 픽률이다.
# 경쟁전의 rq 번호는 사이트 사정으로 계속 바뀐다(1 -> 2 -> 1 -> 2 로 바뀐 이력이 있고,
# 그때마다 수집이 깨졌다). 없는 번호를 주면 서버는 오류 대신 빠른 대전으로 조용히
# 폴백하므로 상수로 박아두면 안 된다. 매 실행마다 rq 선택 상자에서 이름으로 찾아낸다.
COMPETITIVE_LABELS = ("경쟁전", "역할 고정")

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
_RQ_SELECT_RE = re.compile(
    r'<select[^>]*data-label="rq".*?</select>', re.S | re.I
)
# rq 항목의 이름은 data-title 속성과 태그 사이 텍스트에 같은 값이 들어 있다.
_RQ_OPTION_RE = re.compile(r"<option(?P<attrs>[^>]*)>(?P<label>[^<]*)", re.S)

_print_lock = threading.Lock()

# 속도 제한에 걸리면 이 시각까지 모든 워커가 요청을 멈춘다. _pause_all 참고.
_throttle_lock = threading.Lock()
_throttle_until = 0.0


def log(*args: object) -> None:
    with _print_lock:
        print(*args, file=sys.stderr, flush=True)


def _describe_http_error(error: urllib.error.HTTPError) -> str:
    """차단당했을 때 원인을 로그만 보고 알 수 있도록 응답을 요약한다.

    WAF 는 보통 응답 헤더나 본문에 식별자를 남긴다. 그게 없으면 다음 실패 때
    또 맨손으로 추측해야 한다.
    """
    interesting = ("server", "cf-ray", "x-akamai-request-id", "retry-after")
    headers = " ".join(
        f"{name}={value}"
        for name, value in error.headers.items()
        if name.lower() in interesting
    )
    try:
        body = error.read(400).decode("utf-8", errors="replace")
    except Exception:  # 본문을 못 읽는다고 재시도까지 막을 이유는 없다
        body = ""
    body = " ".join(body.split())
    return f"HTTP {error.code} [{headers}] {body}"


def _backoff_seconds(attempt: int) -> int:
    """15초에서 시작해 2분까지 늘린다. 속도 제한은 몇 초로는 풀리지 않는다."""
    return min(15 * 2**attempt, 120)


def _pause_all(seconds: float, reason: str) -> None:
    """모든 워커를 함께 멈춰 세운다.

    서버가 지쳤을 때 워커 하나만 물러나 봐야 소용이 없다. 나머지가 계속 두드리는
    동안에는 제한이 풀리지 않고, 실제로 그렇게 두 워커가 나란히 재시도를 소진하며
    수집이 통째로 날아간 적이 있다.
    """
    global _throttle_until
    with _throttle_lock:
        resume = max(_throttle_until, time.monotonic() + seconds)
        widened = resume > _throttle_until
        _throttle_until = resume
    if widened:
        log(f"  전체 대기 {seconds:.0f}초 ({reason})")


def _await_throttle() -> None:
    while True:
        with _throttle_lock:
            remaining = _throttle_until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 5))


def fetch_html(params: dict[str, str], *, retries: int = 7) -> str:
    query = urllib.parse.urlencode(params)
    url = f"{BASE_URL}?{query}"
    last_error: Exception | None = None
    for attempt in range(retries):
        _await_throttle()
        request = urllib.request.Request(url, headers=HEADERS)
        try:
            with _OPENER.open(request, timeout=30) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            last_error = error
            detail = _describe_http_error(error)
            # 4xx 중 차단·속도 제한이 아닌 것은 기다려도 그대로다. 요청 자체가 틀렸다.
            if 400 <= error.code < 500 and error.code not in (403, 408, 429):
                raise RuntimeError(f"요청 실패: {url} — {detail}") from error
            retry_after = error.headers.get("Retry-After")
            backoff = (
                int(retry_after)
                if retry_after and retry_after.isdigit()
                else _backoff_seconds(attempt)
            )
            log(f"  재시도 {attempt + 1}/{retries} ({detail})")
            _pause_all(backoff, f"HTTP {error.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            # 응답이 오다 멎는 것(read timeout)도 사실상 속도 제한이다. 서버는 500 을
            # 주다가 아예 침묵하는 쪽으로 넘어간다. 잠깐 쉬는 정도로는 회복되지 않아
            # 5xx 와 똑같이 길게 기다린다.
            last_error = error
            backoff = _backoff_seconds(attempt)
            log(f"  재시도 {attempt + 1}/{retries} ({error})")
            _pause_all(backoff, str(error))
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


def detect_rq(page: str) -> str:
    """rq 선택 상자에서 '경쟁전 - 역할 고정'의 값을 찾는다.

    번호가 아니라 이름으로 찾으므로 사이트가 번호를 바꿔도 따라간다. 이름이 바뀌거나
    항목이 사라지면 엉뚱한 모드를 수집하느니 멈추는 편이 낫다.
    """
    select = _RQ_SELECT_RE.search(page)
    if select is None:
        raise ValueError("rq 선택 상자를 찾지 못했습니다. 페이지 구조가 바뀐 것 같습니다.")

    options: list[tuple[str, str]] = []
    for option in _RQ_OPTION_RE.finditer(select.group(0)):
        value_match = _VALUE_RE.search(option.group("attrs"))
        if value_match is None:
            continue
        options.append(
            (value_match.group(1), html.unescape(option.group("label")).strip())
        )

    matched = [
        value
        for value, label in options
        if all(keyword in label for keyword in COMPETITIVE_LABELS)
    ]
    if len(matched) != 1:
        raise ValueError(
            f"'{' '.join(COMPETITIVE_LABELS)}' 항목을 하나로 특정하지 못했습니다"
            f"(후보 {matched}). 페이지의 rq 항목은 {options} 입니다."
        )
    return matched[0]


def extract_maps(page: str, rq: str) -> list[dict]:
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
        available_in = rqs_match.group(1).split(",") if rqs_match else [rq]
        if rq not in available_in:
            continue  # 경쟁전에 없는 맵은 건너뛴다
        name = html.unescape(token.group("label")).strip()
        maps.append(
            {
                "slug": slug_match.group(1),
                "name": name or slug_match.group(1),
                "mode": mode or "기타",
            }
        )
    if not maps:
        seen = sorted({m.group(1) for m in _OPTION_RQS_RE.finditer(select.group(0))})
        raise ValueError(
            f"경쟁전(rq={rq})에 해당하는 맵이 없습니다. 페이지의 data-rqs 값은 "
            f"{seen} 입니다."
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


def build_shard(
    tier: str, region: str, maps: list[dict], rq: str, *, delay: float
) -> dict:
    per_map: dict[str, dict[str, list[float | None]]] = {}
    # 'all-maps' 는 맵 편차를 재는 기준선으로 함께 받아둔다.
    for slug in ["all-maps"] + [game_map["slug"] for game_map in maps]:
        params = {
            "input": INPUT,
            "map": slug,
            "region": region,
            "rq": rq,
            "tier": tier,
        }
        page = fetch_html(params)
        per_map[slug] = extract_stats(page)
        if delay:
            time.sleep(delay)
    log(f"완료: {tier} / {region} ({len(per_map)}개 맵)")
    return {
        "input": INPUT,
        "rq": rq,
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
    # 경쟁전 번호를 아직 모르니 rq 없이 한 번 받아(서버는 빠른 대전을 내려준다) 선택
    # 상자에서 번호를 알아낸 뒤, 같은 페이지를 경쟁전으로 다시 받아 메타를 뽑는다.
    # 영웅 목록이 모드마다 다를 수 있어 메타는 경쟁전 페이지 기준으로 맞춘다.
    probe_params = {
        "input": INPUT,
        "map": "all-maps",
        "region": "Asia",
        "tier": "All",
    }
    rq = detect_rq(fetch_html(probe_params))
    log(f"경쟁전 - 역할 고정 = rq {rq}")

    seed = fetch_html({**probe_params, "rq": rq})
    heroes = extract_heroes(seed)
    maps = extract_maps(seed, rq)
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
            pool.submit(build_shard, t, r, maps, rq, delay=args.delay): (t, r)
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
            "rq": rq,
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
