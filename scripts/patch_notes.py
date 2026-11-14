#!/usr/bin/env python3
"""넥슨 오버워치 패치노트 수집기.

목록(/news/patchnotes)과 본문(/news/patchnotes/{newsNo}/{slug})도 통계 페이지와
같은 Nuxt 페이지라 __NUXT_DATA__ 에 들어 있다. 본문은 블리자드 패치노트 HTML
그대로여서 PatchNotesHeroUpdate 같은 클래스로 영웅별 변경을 가를 수 있다.

출력: {history}/patches/{newsNo}.json — 한 번 받은 글은 다시 받지 않는다.

목록의 게시일(createdAt)은 게시 직후 '1일 전' 같은 상대 표기로 온다. 하루 한 번
05:00 KST 에 돌므로 새 글은 거의 항상 상대 표기로 처음 만난다. 그래서 수집 시각에서
빼서 날짜로 바꾸고, 처음 저장한 날짜는 고치지 않는다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from fetch_rates import DATA_DIR, fetch_html, hydrate, log, parse_payload, warn, write_json

LIST_URL = "https://overwatch.nexon.com/news/patchnotes"
KST = timezone(timedelta(hours=9))

# 같은 목록에 '알려진 현상 안내'·이벤트 글이 섞여 온다.
PATCH_TITLE = "패치 노트"
# 수집 범위가 역할 고정 경쟁전(5대5)이라 6대6 전용 변경은 버린다.
SIX_V_SIX = "(6대6)"
# 글마다 '개발자의 의견:'과 '개발자 의견:'이 섞여 나오고, 둘이 겹쳐 붙은 문단도 있다.
DEV_PREFIX = re.compile(r"^(?:개발자\s*(?:의)?\s*의견\s*:\s*)+")
# 이 문구가 있는데 영웅 블록이 0개면 클래스 이름이 바뀐 것을 의심한다.
HERO_SECTION_TITLE = "영웅 업데이트"

_LIST_KEYS = frozenset({"newsNo", "title", "slug", "createdAt"})
_ABSOLUTE_DATE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$")
_RELATIVE_DATE = re.compile(r"^(\d+)\s*(분|시간|일)\s*전$")
_RELATIVE_UNIT = {"분": "minutes", "시간": "hours", "일": "days"}


def article_url(news_no: int, slug: str) -> str:
    return f"{LIST_URL}/{news_no}/{urllib.parse.quote(slug)}"


# ---------- 목록 ----------


def parse_created_at(value: object, now: datetime) -> str | None:
    """createdAt 을 KST 날짜(YYYY-MM-DD)로. 모르는 형식이면 None."""
    text = " ".join(str(value or "").split())
    match = _ABSOLUTE_DATE.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    match = _RELATIVE_DATE.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        moment = now.astimezone(KST) - timedelta(**{_RELATIVE_UNIT[unit]: amount})
        return moment.date().isoformat()
    if text == "방금 전":
        return now.astimezone(KST).date().isoformat()
    return None


def parse_list(page: str, now: datetime) -> list[dict]:
    """목록 페이지에서 패치노트 글만 newsNo 내림차순으로.

    페이지에는 옆 칸의 공지 목록도 같은 모양으로 들어 있어 newsNo 로 중복을 없앤다.
    """
    flat = parse_payload(page)
    seen: dict[int, dict] = {}
    for index, value in enumerate(flat):
        if not (isinstance(value, dict) and _LIST_KEYS <= value.keys()):
            continue
        row = hydrate(flat, index)
        title = str(row.get("title") or "")
        if PATCH_TITLE not in title:
            continue
        try:
            news_no = int(row["newsNo"])
        except (TypeError, ValueError):
            continue
        if news_no in seen:
            continue
        date = parse_created_at(row.get("createdAt"), now)
        if date is None:
            warn(f"패치노트 {news_no} 게시일 형식을 모릅니다: {row.get('createdAt')!r}")
        seen[news_no] = {
            "newsNo": news_no,
            "title": title.strip(),
            "slug": str(row["slug"]),
            "date": date,
        }
    return sorted(seen.values(), key=lambda entry: entry["newsNo"], reverse=True)


# ---------- 본문 ----------


def article_content(page: str, news_no: int) -> str:
    """본문 페이지에서 그 글의 HTML. 옆 칸의 다른 글은 content 가 비어 있다."""
    flat = parse_payload(page)
    for index, value in enumerate(flat):
        if isinstance(value, dict) and {"newsNo", "content"} <= value.keys():
            row = hydrate(flat, index)
            if str(row.get("newsNo")) == str(news_no) and isinstance(row.get("content"), str):
                return row["content"]
    raise ValueError(f"패치노트 {news_no} 본문을 찾지 못했습니다. 페이지 구조가 바뀐 것 같습니다.")


_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)


def _clean(text: str) -> str:
    return " ".join(text.split())


class _HeroUpdateParser(HTMLParser):
    """PatchNotesHeroUpdate 블록마다 이름·개발자의 의견·변경 줄을 모은다.

    변경 줄은 (스킬 이름, 문장) 쌍이다. 일반 변경(generalUpdates)은 스킬 이름이 없고,
    그 안의 <p>(예: '추가 강타 - 주요 특전')가 있으면 그 아래 줄의 이름으로 쓴다.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._stack: list[set[str]] = []  # 열린 요소마다 그 요소의 class 목록
        self._block: dict | None = None
        self._block_depth = 0
        self._capture: list[tuple[str, list[str]]] = []  # (용도, 글자 조각)
        self._ability: str | None = None
        self._general_label: str | None = None

    def _inside(self, cls: str) -> bool:
        return any(cls in classes for classes in self._stack[self._block_depth :])

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if tag in _VOID_TAGS:
            return
        self._stack.append(classes)
        if "PatchNotesHeroUpdate" in classes:
            self._block = {"name": "", "dev": [], "lines": []}
            self._block_depth = len(self._stack)
            self._ability = None
            self._general_label = None
            return
        if self._block is None:
            return
        if "PatchNotesHeroUpdate-name" in classes:
            self._capture.append(("name", []))
        elif "PatchNotesHeroUpdate-dev" in classes:
            self._capture.append(("dev", []))
        elif "PatchNotesAbilityUpdate-name" in classes:
            self._capture.append(("ability", []))
        elif tag == "li" and (
            self._inside("PatchNotesAbilityUpdate-detailList")
            or self._inside("PatchNotesHeroUpdate-generalUpdates")
        ):
            self._capture.append(("li", []))
        elif tag == "p" and self._inside("PatchNotesHeroUpdate-generalUpdates"):
            self._capture.append(("label", []))
        else:
            return
        # 이 요소가 끝날 때 무엇을 닫는지 알 수 있게 표시해 둔다.
        classes.add("\0capture")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        pass  # <img/> 같은 빈 요소에는 글자가 없다

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._capture[-1][1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS or not self._stack:
            return
        classes = self._stack.pop()
        if "\0capture" in classes:
            kind, parts = self._capture.pop()
            self._close_capture(kind, _clean("".join(parts)))
        if self._block is not None and len(self._stack) < self._block_depth:
            self._finish_block()

    def _close_capture(self, kind: str, text: str) -> None:
        block = self._block
        if block is None:
            return
        if kind == "name":
            block["name"] = text
        elif kind == "dev":
            if text:
                block["dev"].append(text)
        elif kind == "ability":
            self._ability = text or None
        elif kind == "label":
            self._general_label = text or None
        elif kind == "li" and text:
            in_ability = self._inside("PatchNotesAbilityUpdate-detailList")
            label = self._ability if in_ability else self._general_label
            block["lines"].append({"ability": label, "text": text})
        # 바깥 li 에 안쪽 li 글자가 섞이지 않도록 닫힌 조각은 부모에 넘기지 않는다.

    def _finish_block(self) -> None:
        block = self._block
        self._block = None
        if block is None:
            return
        dev = DEV_PREFIX.sub("", " ".join(block["dev"])).strip()
        lines = [line for line in block["lines"] if not line["text"].endswith(SIX_V_SIX)]
        self.blocks.append({"name": block["name"], "dev": dev or None, "lines": lines})


def parse_hero_updates(content: str) -> list[dict]:
    parser = _HeroUpdateParser()
    parser.feed(content)
    parser.close()
    return parser.blocks


def name_index(heroes: dict[str, dict]) -> dict[str, str]:
    """meta.json 의 한글 이름 -> 슬러그."""
    return {_clean(hero.get("name") or slug): slug for slug, hero in heroes.items()}


def structure_patch(entry: dict, content: str, names: dict[str, str]) -> dict:
    """목록 항목과 본문 HTML 을 patches/{newsNo}.json 모양으로."""
    blocks = parse_hero_updates(content)
    news_no = entry["newsNo"]
    if not blocks and HERO_SECTION_TITLE in content:
        warn(
            f"패치노트 {news_no}: '{HERO_SECTION_TITLE}' 문구가 있는데 영웅 블록이 없습니다. "
            "본문 HTML 구조가 바뀐 것 같습니다."
        )
    heroes: dict[str, dict] = {}
    unmatched: list[dict] = []
    for block in blocks:
        slug = names.get(_clean(block["name"]))
        record = {"dev": block["dev"], "lines": block["lines"]}
        if slug is None:
            warn(f"패치노트 {news_no}: meta.json 에 없는 영웅 이름 {block['name']!r}")
            unmatched.append({"name": block["name"], **record})
            continue
        if slug in heroes:  # 같은 영웅이 두 번 나오면 합친다
            heroes[slug]["lines"] += record["lines"]
            heroes[slug]["dev"] = heroes[slug]["dev"] or record["dev"]
        else:
            heroes[slug] = record
    return {
        "newsNo": news_no,
        "title": entry["title"],
        "slug": entry["slug"],
        "url": article_url(news_no, entry["slug"]),
        "date": entry["date"],
        "heroes": heroes,
        "unmatched": unmatched,
    }


# ---------- 캐시 ----------


def load_patches(history: Path) -> list[dict]:
    patches = []
    for path in sorted((history / "patches").glob("*.json")):
        patches.append(json.loads(path.read_text(encoding="utf-8")))
    return sorted(patches, key=lambda patch: patch["newsNo"])


def sync(
    history: Path,
    heroes: dict[str, dict],
    now: datetime,
    fetch: Callable[[str], str] = lambda url: fetch_html(base_url=url),
) -> list[dict]:
    """목록에서 아직 저장하지 않은 패치노트만 본문을 받아 저장하고, 저장된 전체를 돌려준다.

    목록이나 본문을 못 받아도 멈추지 않는다. 패치노트는 사실 표의 곁가지라 통계 배포를
    막을 이유가 없고, 못 받은 글은 다음 날 다시 시도된다.
    """
    folder = history / "patches"
    try:
        entries = parse_list(fetch(LIST_URL), now)
    except Exception as error:  # 네트워크·구조 변경 모두 같은 처리
        warn(f"패치노트 목록을 받지 못해 저장된 패치만 씁니다: {error}")
        entries = []

    names = name_index(heroes)
    for entry in entries:
        path = folder / f"{entry['newsNo']}.json"
        if path.exists():
            continue  # 처음 저장한 게시일을 고치지 않는다
        url = article_url(entry["newsNo"], entry["slug"])
        try:
            content = article_content(fetch(url), entry["newsNo"])
        except Exception as error:
            warn(f"패치노트 {entry['newsNo']} 본문을 받지 못했습니다 ({url}): {error}")
            continue
        patch = structure_patch(entry, content, names)
        patch["fetchedAt"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        write_json(path, patch)
        log(f"패치노트 {entry['newsNo']} 저장: {entry['title']} ({entry['date']}), 영웅 {len(patch['heroes'])}명")
    return load_patches(history)


def main() -> int:
    parser = argparse.ArgumentParser(description="넥슨 오버워치 패치노트 수집기")
    parser.add_argument("--history", type=Path, default=Path("history"), help="data 브랜치 폴더")
    parser.add_argument("--meta", type=Path, default=DATA_DIR / "meta.json")
    args = parser.parse_args()

    meta = json.loads(args.meta.read_text(encoding="utf-8"))
    patches = sync(args.history, meta["heroes"], datetime.now(KST))
    log(f"저장된 패치노트 {len(patches)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
