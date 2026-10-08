#!/usr/bin/env python3
"""오버워치 한국 서버 영웅 통계(rates) 수집기.

overwatch.nexon.com/hero/rate 는 Nuxt 서버 렌더링 페이지라, HTML 안
<script id="__NUXT_DATA__"> 에 영웅 전체의 승률/픽률/밴률과 필터 목록이
JSON 으로 들어 있다. 별도 API 키나 인증, 브라우저가 필요 없다.

넥슨 페이지를 쓰는 이유는 **지역에 '한국'이 있어서**다. 블리자드 공식 페이지는
아시아/아메리카/유럽만 제공해 한국 서버 메타를 따로 볼 수 없다.

주의: 이 사이트는 없는 필터 값을 주면 오류 대신 **기본값으로 조용히 폴백**한다
(rank=zzz -> 모든 등급, map=zzz -> 모든 전장, rq=9 -> 빠른 대전). 그래서 값을
상수로 박지 않고 매번 페이지의 필터 목록에서 읽어 쓰고, 응답이 돌려주는 rq 가
요청한 값과 같은지 매번 확인한다.

역할(role) 필터는 표시용이라 role=all 한 번으로 전 역할 데이터가 온다. 따라서
(map, rank, region) 조합 하나당 1회 요청이다.

출력:
  site/data/meta.json                  영웅/맵/필터 메타데이터
  site/data/pc_{rank}_{region}.json    맵 31개 × 영웅 전체의 원본 수치

유효 픽률은 저장하지 않는다. 원본 수치만 저장하고 화면에서 계산한다.
"""

from __future__ import annotations

import argparse
import gzip
import http.cookiejar
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://overwatch.nexon.com/hero/rate"

# 도구 이름을 밝힌 User-Agent 로는 GitHub Actions 러너에서 403 이 떨어진 전례가 있다
# (집 회선에서는 같은 요청이 통과한다). 브라우저가 보내는 것과 같은 헤더 묶음으로 맞춘다.
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

# 첫 응답이 내려주는 세션 쿠키를 이후 요청에 그대로 실어 보낸다. 사람이 필터를
# 바꿔가며 보는 흐름과 같아진다. CookieJar 는 내부 잠금이 있어 여러 워커가 함께 써도 된다.
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)

# 경쟁전 - 역할 고정만 수집한다. 빠른 대전은 밴이 없어(밴률이 전부 0) 유효 픽률이
# 원본 픽률과 같다. rq 번호는 사이트 사정으로 바뀔 수 있고, 없는 번호를 주면 서버가
# 조용히 빠른 대전으로 떨어뜨리므로 상수로 두지 않고 매번 이름으로 찾는다.
COMPETITIVE_LABELS = ("경쟁전", "역할 고정")

# 마우스·키보드만. 콘솔은 요청·데이터가 두 배로 늘어나는데 메타가 크게 달라
# 함께 보기도 어려워 수집하지 않는다.
INPUT = "pc"

# 이 도구가 존재하는 이유. 아시아/아메리카/유럽도 같은 코드로 받을 수 있지만
# (--regions), 기본은 한국만이다.
DEFAULT_REGIONS = ["korea"]

# 맵 편차를 재는 기준선. 필터 목록에서 이 값만 따로 쓴다.
BASELINE_MAP = "all"

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "site" / "data"

_NUXT_RE = re.compile(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)

# 필터 목록을 담은 객체와 영웅 한 줄을 담은 객체는 이 키들로 알아본다.
# payload 안의 위치는 사이트가 바뀌면 달라지므로 키로 찾는다.
_FILTER_KEYS = frozenset({"maps", "ranks", "regions", "rulesetQueues", "inputs"})
_HERO_KEYS = frozenset({"heroId", "pickRate", "banRate", "winRate"})

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


class Unavailable(RuntimeError):
    """사이트가 이 조합을 끝내 내려주지 못했다.

    특정 (맵, 등급, 지역) 조합에서 서버가 응답을 시작만 하고 끝맺지 못하는 일이
    있다. 몇 번을 다시 물어도 똑같고 브라우저로 열어도 마찬가지라, 사이트 쪽
    데이터 문제로 보인다. 다른 조합은 멀쩡하니 이 칸만 비우고 넘어간다.
    """


class NoData(Unavailable):
    """페이지는 정상인데 영웅 목록이 비어 있다.

    시즌이 바뀌어 맵 로테이션에서 빠진 맵(예: 할리우드)은 필터 목록에는 남아 있고
    통계만 빈 목록으로 온다. 응답이 끊긴 경우와 똑같이 그 칸만 비운다.
    """


def _pause_all(seconds: float, reason: str) -> None:
    """모든 워커를 함께 멈춰 세운다.

    진짜 속도 제한(403/429/503)일 때만 쓴다. 한 워커만 물러나 봐야 나머지가 계속
    두드리는 동안에는 제한이 풀리지 않는다.
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


# 속도 제한은 몇 초 기다린다고 풀리지 않아 길게 물러난다. 반면 응답이 멎는 것은
# 기다린다고 나아지지 않으므로 짧게 몇 번만 확인하고 그 칸을 포기한다.
THROTTLE_CODES = (403, 429, 503)
THROTTLE_RETRIES = 5


def fetch_html(params: dict[str, str], *, attempts: int = 3) -> str:
    query = urllib.parse.urlencode(params)
    url = f"{BASE_URL}?{query}"
    last_error: Exception | None = None
    attempt = 0
    throttled = 0
    while attempt < attempts:
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
            if error.code in THROTTLE_CODES:
                # 차단은 이 칸이 깨진 것과 무관하니 아래 시도 횟수를 깎지 않는다.
                if throttled >= THROTTLE_RETRIES:
                    raise RuntimeError(f"차단이 풀리지 않는다: {url} — {detail}")
                retry_after = error.headers.get("Retry-After")
                backoff = (
                    int(retry_after)
                    if retry_after and retry_after.isdigit()
                    else min(15 * 2**throttled, 120)
                )
                throttled += 1
                log(f"  차단 {throttled}/{THROTTLE_RETRIES} ({detail})")
                _pause_all(backoff, f"HTTP {error.code}")
                continue
            if 400 <= error.code < 500:
                raise RuntimeError(f"요청 실패: {url} — {detail}") from error
            backoff = 2**attempt  # 5xx 는 이 조합만의 문제일 때가 많다
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            backoff = 2**attempt
            detail = str(error)
        attempt += 1
        if attempt < attempts:
            log(f"  재시도 {attempt}/{attempts} ({detail}) — {backoff}초 후")
            time.sleep(backoff)
    raise Unavailable(f"{url} — {last_error}") from last_error


# ---------- Nuxt payload 읽기 ----------
#
# devalue 형식이다. 최상위는 평평한 배열이고, 배열/객체의 원소는 값이 아니라 이 배열의
# 색인이다. 같은 값을 여러 번 쓰지 않으려고 이렇게 접는다. 음수 색인은 undefined·NaN
# 같은 특수값을 뜻하므로 None 으로 본다.

_MAX_DEPTH = 32


def parse_payload(page: str) -> list:
    match = _NUXT_RE.search(page)
    if match is None:
        raise ValueError(
            "__NUXT_DATA__ 를 찾지 못했습니다. 페이지 구조가 바뀐 것 같습니다."
        )
    return json.loads(match.group(1))


def hydrate(flat: list, index: int, depth: int = 0) -> object:
    if index < 0:
        return None
    if depth > _MAX_DEPTH:
        raise ValueError("payload 가 너무 깊습니다. 형식이 바뀐 것 같습니다.")
    value = flat[index]
    if isinstance(value, list):
        return [hydrate(flat, i, depth + 1) for i in value]
    if isinstance(value, dict):
        return {k: hydrate(flat, i, depth + 1) for k, i in value.items()}
    return value


def _find(flat: list, keys: frozenset) -> int:
    for index, value in enumerate(flat):
        if isinstance(value, dict) and keys <= value.keys():
            return index
    raise ValueError(
        f"payload 에서 {sorted(keys)} 를 가진 객체를 찾지 못했습니다. "
        "페이지 구조가 바뀐 것 같습니다."
    )


def _number(value: object) -> float | None:
    """데이터가 부족한 칸은 None 으로 정규화한다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def effective_rq(flat: list) -> str | None:
    """응답이 실제로 어느 게임 모드로 계산됐는지.

    요청한 rq 와 다르면 서버가 조용히 폴백한 것이다. 빠른 대전으로 떨어지면 밴률이
    전부 0 으로 와서, 알아채지 못하면 데이터 전체가 조용히 망가진다.
    """
    for value in flat:
        if isinstance(value, dict) and "rq" in value and "status" in value:
            result = hydrate(flat, value["rq"])
            return None if result is None else str(result)
    return None


def extract_stats(page: str, expected_rq: str) -> dict[str, list[float | None]]:
    """영웅 id -> [픽률, 밴률, 승률]"""
    flat = parse_payload(page)
    actual = effective_rq(flat)
    if actual is not None and actual != expected_rq:
        raise ValueError(
            f"요청한 게임 모드(rq={expected_rq})가 아니라 rq={actual} 로 응답했습니다. "
            "사이트가 기본값으로 폴백한 것 같습니다."
        )

    stats: dict[str, list[float | None]] = {}
    for index, value in enumerate(flat):
        if isinstance(value, dict) and _HERO_KEYS <= value.keys():
            row = hydrate(flat, index)
            stats[row["heroId"]] = [
                _number(row.get("pickRate")),
                _number(row.get("banRate")),
                _number(row.get("winRate")),
            ]
    if not stats:
        # 필터 목록까지 읽혔다면 구조는 그대로고, 사이트가 이 조합의 통계를 비워 둔 것이다.
        raise NoData("영웅 목록이 비어 있습니다.")
    return stats


def extract_heroes(page: str) -> dict[str, dict]:
    flat = parse_payload(page)
    heroes: dict[str, dict] = {}
    for index, value in enumerate(flat):
        if isinstance(value, dict) and _HERO_KEYS <= value.keys():
            row = hydrate(flat, index)
            heroes[row["heroId"]] = {
                "name": row.get("name") or row["heroId"],
                "role": row.get("role"),
                "subrole": row.get("subrole"),
                "portrait": row.get("thumbnailUrl"),
            }
    return heroes


def extract_filters(page: str) -> dict[str, list[dict]]:
    """역할·입력·게임모드·등급·지역·맵 선택 목록. 새 값이 생기면 그대로 따라온다."""
    flat = parse_payload(page)
    return hydrate(flat, _find(flat, _FILTER_KEYS))


def detect_rq(filters: dict[str, list[dict]]) -> str:
    """게임 모드 목록에서 '경쟁전 - 역할 고정'의 값을 찾는다.

    번호가 아니라 이름으로 찾으므로 사이트가 번호를 바꿔도 따라간다. 이름이 바뀌거나
    항목이 사라지면 엉뚱한 모드를 수집하느니 멈추는 편이 낫다.
    """
    queues = [(q["value"], q.get("name") or "") for q in filters["rulesetQueues"]]
    matched = [
        value
        for value, name in queues
        if all(keyword in name for keyword in COMPETITIVE_LABELS)
    ]
    if len(matched) != 1:
        raise ValueError(
            f"'{' '.join(COMPETITIVE_LABELS)}' 항목을 하나로 특정하지 못했습니다"
            f"(후보 {matched}). 페이지의 게임 모드 목록은 {queues} 입니다."
        )
    return str(matched[0])


def extract_maps(filters: dict[str, list[dict]]) -> list[dict]:
    """맵 목록을 게임 모드와 함께, 사이트에 나오는 순서대로 뽑는다.

    맵 목록은 평평하지만 parentValue 로 계층을 이룬다. 다른 항목이 부모로 가리키는
    항목(쟁탈·호위 등)은 모드 머리글이고, 그 아래가 실제 맵이다. 새 맵이나 새 모드가
    추가되면 그대로 따라온다. 어느 모드에도 속하지 않은 맵은 '기타'로 묶는다.
    """
    entries = filters["maps"]
    names = {entry["value"]: entry.get("name") or entry["value"] for entry in entries}
    parents = {entry.get("parentValue") for entry in entries}

    maps: list[dict] = []
    for entry in entries:
        slug = entry["value"]
        if slug == BASELINE_MAP or slug in parents:
            continue  # 기준선과 모드 머리글은 맵이 아니다
        maps.append(
            {
                "slug": slug,
                "name": names[slug],
                "mode": names.get(entry.get("parentValue"), "기타"),
            }
        )
    if not maps:
        raise ValueError(
            f"맵을 하나도 찾지 못했습니다. 페이지의 맵 목록은 {entries} 입니다."
        )
    return maps


def option_values(filters: dict[str, list[dict]], key: str) -> list[str]:
    return [str(entry["value"]) for entry in filters[key]]


def shard_name(rank: str, region: str) -> str:
    return f"{INPUT}_{rank}_{region}.json"


def build_shard(
    rank: str, region: str, maps: list[dict], rq: str, *, delay: float
) -> dict:
    per_map: dict[str, dict[str, list[float | None]]] = {}
    missing: list[str] = []
    # 기준선('모든 전장')을 함께 받아 맵 편차를 잰다.
    for slug in [BASELINE_MAP] + [game_map["slug"] for game_map in maps]:
        params = {
            "role": "all",
            "rq": rq,
            "rank": rank,
            "map": slug,
            "input": INPUT,
            "region": region,
        }
        try:
            page = fetch_html(params)
            per_map[slug] = extract_stats(page, rq)
        except Unavailable as error:
            # 이 칸 하나 때문에 나머지 수백 건을 버릴 이유가 없다. 화면은 빠진 맵을
            # '데이터 없음'으로 그린다.
            missing.append(slug)
            log(f"  비움: {rank}/{region}/{slug} — {error}")
            continue
        if delay:
            time.sleep(delay)
    note = f", 빈 칸 {len(missing)}개" if missing else ""
    log(f"완료: {rank} / {region} ({len(per_map)}개 맵{note})")
    return {
        "missing": missing,
        "input": INPUT,
        "rq": rq,
        "tier": rank,
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


def _choose(requested: str | None, available: list[str], label: str) -> list[str]:
    """--tiers/--regions 로 받은 값을 사이트가 실제로 가진 값으로 제한한다.

    사이트는 모르는 값을 주면 오류 대신 기본값으로 조용히 폴백한다. 오타 하나로
    전부 '모든 등급' 데이터를 아홉 번 받아 적는 일이 없도록 여기서 막는다.
    """
    if requested is None:
        return available
    wanted = [value.strip() for value in requested.split(",") if value.strip()]
    unknown = [value for value in wanted if value not in available]
    if unknown:
        raise SystemExit(
            f"사이트에 없는 {label} 값입니다: {', '.join(unknown)} "
            f"(가능한 값: {', '.join(available)})"
        )
    return wanted


def main() -> int:
    parser = argparse.ArgumentParser(description="오버워치 한국 서버 영웅 통계 수집기")
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
        "--tiers", help="쉼표로 구분한 등급 목록 (기본: 사이트의 전체 등급)"
    )
    parser.add_argument(
        "--regions",
        help=f"쉼표로 구분한 지역 목록 (기본: {','.join(DEFAULT_REGIONS)})",
    )
    parser.add_argument(
        "--limit-maps",
        type=int,
        help="맵 수를 제한 (동작 확인용)",
    )
    args = parser.parse_args()

    log("메타데이터 수집 중...")
    # 경쟁전 번호를 아직 모르니 rq 없이 한 번 받아(서버는 빠른 대전을 내려준다) 필터
    # 목록에서 번호를 알아낸 뒤, 같은 페이지를 경쟁전으로 다시 받아 메타를 뽑는다.
    # 영웅 목록이 모드마다 다를 수 있어 메타는 경쟁전 페이지 기준으로 맞춘다.
    probe_params = {
        "role": "all",
        "rank": "all",
        "map": BASELINE_MAP,
        "input": INPUT,
        "region": DEFAULT_REGIONS[0],
    }
    rq = detect_rq(extract_filters(fetch_html(probe_params)))
    log(f"경쟁전 - 역할 고정 = rq {rq}")

    seed = fetch_html({**probe_params, "rq": rq})
    filters = extract_filters(seed)
    heroes = extract_heroes(seed)
    maps = extract_maps(filters)
    if not heroes:
        # 맵 하나가 비는 것과 달리 '모든 전장'까지 비면 받을 것이 없다. 시즌 교체 직후
        # 사이트가 통계를 잠시 비워 두는 동안 이렇게 된 적이 있다.
        raise SystemExit("경쟁전 영웅 목록이 비어 있습니다. 사이트가 통계를 갱신 중인 것 같습니다.")

    ranks = _choose(args.tiers, option_values(filters, "ranks"), "등급")
    available_regions = option_values(filters, "regions")
    if args.regions:
        regions = _choose(args.regions, available_regions, "지역")
    else:
        # 기본값도 사이트 목록에 있는지 확인한다. 없는 값을 그냥 보내면 다른 지역
        # 데이터를 한국이라고 적어 넣게 된다.
        regions = [r for r in DEFAULT_REGIONS if r in available_regions]
        if not regions:
            raise SystemExit(
                f"기본 지역 {DEFAULT_REGIONS} 가 사이트 목록에 없습니다 "
                f"(사이트: {', '.join(available_regions)})."
            )
    if args.limit_maps:
        maps = maps[: args.limit_maps]

    combos = [(t, r) for t in ranks for r in regions]
    log(
        f"영웅 {len(heroes)}명 / 맵 {len(maps)}개(+기준선) / 샤드 {len(combos)}개 "
        f"= 요청 {len(combos) * (len(maps) + 1)}건"
    )

    started = time.monotonic()
    total_bytes = 0
    requested = len(combos) * (len(maps) + 1)
    missing: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(build_shard, t, r, maps, rq, delay=args.delay): (t, r)
            for t, r in combos
        }
        for future, (t, r) in futures.items():
            shard = future.result()
            missing += [f"{t}/{r}/{slug}" for slug in shard["missing"]]
            total_bytes += write_json(DATA_DIR / shard_name(t, r), shard)

    # 빈 칸 몇 개는 사이트 사정이라 넘어가지만, 이만큼 비면 수집기나 사이트가 크게
    # 어긋난 것이다. 반쪽짜리 통계를 배포하느니 멈춘다.
    allowed = max(1, requested // 20)  # 5%
    if len(missing) > allowed:
        raise SystemExit(
            f"빈 칸이 {len(missing)}개입니다(요청 {requested}건 중 허용 {allowed}개). "
            f"예: {', '.join(sorted(missing)[:5])}"
        )
    if missing:
        by_slug = Counter(cell.rsplit("/", 1)[1] for cell in missing)
        log(f"사이트가 못 준 칸 {len(missing)}개 — {dict(by_slug)}")

    write_json(
        DATA_DIR / "meta.json",
        {
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": BASE_URL,
            "rq": rq,
            "input": INPUT,
            "baselineMap": BASELINE_MAP,
            "heroes": heroes,
            "maps": maps,
            "tiers": ranks,
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
