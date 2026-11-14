'use strict';

/* 오버워치 유효 픽률
 * 유효 픽률 = 픽률 / (1 - 밴률)  — 그 영웅이 선택 가능했던 경기에서의 픽률.
 * 수집기는 원본 수치(픽률·밴률·승률)만 저장하고, 계산은 전부 여기서 한다.
 */

// 표시 순서 힌트. 여기에 없는 역할이 새로 생기면 뒤에 붙는다 (숨기지 않는다).
const ROLE_ORDER_HINT = ['tank', 'damage', 'support'];
const ROLE_LABEL = { tank: '돌격', damage: '공격', support: '지원' };
const roleLabel = (role) => ROLE_LABEL[role] || role || '기타';
const TIER_LABEL = {
  all: '모든 등급', bronze: '브론즈', silver: '실버', gold: '골드',
  platinum: '플래티넘', emerald: '에메랄드', diamond: '다이아몬드',
  master: '마스터', grandmaster: '그랜드마스터 및 챔피언',
};
const REGION_LABEL = {
  korea: '한국', asia: '아시아', americas: '아메리카', europe: '유럽',
};
// 맵 편차의 기준선인 '모든 전장'의 슬러그. 수집기가 meta.baselineMap 으로 알려주며,
// meta 를 읽기 전에도 state 기본값이 필요해서 같은 값을 여기에 둔다.
let BASELINE = 'all';
const BAN_WARN = 60;      // 이 밴률을 넘으면 보정 배율이 2.5배를 넘어 수치가 불안정하다
const DENOM_FLOOR = 0.05; // 밴률 95% 이상에서 분모가 0으로 붕괴하는 것을 막는다

const el = (id) => document.getElementById(id);
const main = el('main');
const shardCache = new Map();
let meta = null;
let roles = [];   // meta 의 영웅 목록에서 유도한 실제 역할 목록
let mapSlugs = [];

const state = {
  view: 'maps',
  tier: 'all',
  region: 'korea',
  map: BASELINE,
  map2: BASELINE,
  role: 'ALL',
  topn: 3,
  hero: 'ana',
  sort: 'epr',
  desc: true,
};

/* ---------- 계산 ---------- */

// [픽률, 밴률, 승률] -> 유효 픽률
// 메타 분석 사실 표는 scripts/meta_facts.py 의 effective() 로 같은 값을 미리 계산한다.
// 식이나 DENOM_FLOOR 를 바꾸면 그쪽도 함께 바꾼다.
function effective(stats) {
  if (!stats || stats[0] == null) return null;
  const ban = stats[1] == null ? 0 : stats[1];
  return stats[0] / Math.max(1 - ban / 100, DENOM_FLOOR);
}

// 데이터에 실제로 존재하는 역할을 순서 힌트에 맞춰 정렬해 돌려준다.
// 힌트에 없는 새 역할은 알파벳 순으로 뒤에 붙는다.
function detectRoles() {
  const found = [...new Set(Object.values(meta.heroes).map((hero) => hero.role))];
  const rank = (role) => {
    const index = ROLE_ORDER_HINT.indexOf(role);
    return index < 0 ? ROLE_ORDER_HINT.length : index;
  };
  return found.sort((a, b) => rank(a) - rank(b) || String(a).localeCompare(String(b)));
}

function heroesOf(role) {
  return Object.keys(meta.heroes).filter(
    (id) => role === 'ALL' || meta.heroes[id].role === role
  );
}

// 한 맵의 영웅 목록을 유효 픽률 내림차순으로.
function ranked(mapStats, role) {
  return heroesOf(role)
    .map((id) => ({ id, hero: meta.heroes[id], stats: mapStats[id], epr: effective(mapStats[id]) }))
    .filter((row) => row.epr != null)
    .sort((a, b) => b.epr - a.epr);
}

/* ---------- 데이터 ---------- */

// meta 의 생성 시각을 붙여, 새 영웅이 들어온 meta 와 낡은 캐시 샤드가 섞이지 않게 한다.
// 수집기는 마우스·키보드(PC)만 받으므로 입력장치는 샤드 이름의 고정 접두사다.
function shardUrl(tier, region) {
  const version = meta && meta.generatedAt ? `?v=${encodeURIComponent(meta.generatedAt)}` : '';
  return `data/${meta.input || 'pc'}_${tier}_${region}.json${version}`;
}

function loadShard(tier, region) {
  const url = shardUrl(tier, region);
  if (!shardCache.has(url)) {
    shardCache.set(
      url,
      fetch(url).then((response) => {
        if (!response.ok) throw new Error(`${url} — HTTP ${response.status}`);
        return response.json();
      })
    );
  }
  return shardCache.get(url);
}

/* ---------- 표시 도우미 ---------- */

const escapeHtml = (value) =>
  String(value).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );

const pct = (value, digits = 1) => (value == null ? '--' : `${value.toFixed(digits)}%`);

function warnFlag(stats) {
  if (!stats || stats[1] == null || stats[1] <= BAN_WARN) return '';
  return ` <span class="warn-flag" title="밴률 ${stats[1]}% — 보정 배율이 커서 수치가 불안정합니다">⚠</span>`;
}

function portrait(hero) {
  if (!hero.portrait) return '';
  return `<img src="${escapeHtml(hero.portrait)}" alt="" loading="lazy"
           onerror="this.style.visibility='hidden'">`;
}

function deltaTag(epr, baseEpr) {
  if (epr == null || baseEpr == null) return '';
  const diff = epr - baseEpr;
  if (Math.abs(diff) < 0.5) return `<span class="delta flat">±0</span>`;
  const cls = diff > 0 ? 'up' : 'down';
  const arrow = diff > 0 ? '▲' : '▼';
  return `<span class="delta ${cls}" title="모든 전장 평균 대비">${arrow}${Math.abs(diff).toFixed(1)}</span>`;
}

/* ---------- 뷰: 맵별 상위 영웅 ---------- */

function renderMaps(shard) {
  const baseline = shard.maps[BASELINE] || {};
  const baseEpr = {};
  for (const id of Object.keys(meta.heroes)) baseEpr[id] = effective(baseline[id]);

  const modes = [];
  for (const gameMap of meta.maps) {
    let group = modes.find((m) => m.mode === gameMap.mode);
    if (!group) modes.push((group = { mode: gameMap.mode, maps: [] }));
    group.maps.push(gameMap);
  }

  const limit = Number(state.topn);
  let html = `<p class="legend">각 칸은 <b>유효 픽률</b>이며, 옆의 ▲▼는 모든 전장 평균 대비 차이(%p)입니다.
    작은 글씨는 원본 픽률과 밴률입니다.</p>`;

  for (const group of modes) {
    html += `<h2 class="mode-title">${escapeHtml(group.mode)}</h2><div class="map-grid">`;
    for (const gameMap of group.maps) {
      const mapStats = shard.maps[gameMap.slug];
      html += `<article class="map-card"><h3>${escapeHtml(gameMap.name)}</h3>`;
      if (!mapStats) {
        html += `<p class="hero-detail">데이터 없음</p></article>`;
        continue;
      }
      for (const role of roles) {
        const rows = ranked(mapStats, role).slice(0, limit);
        if (!rows.length) continue;
        html += `<div class="role-block"><div class="role-name role-${escapeHtml(role)}">${escapeHtml(roleLabel(role))}</div>`;
        for (const row of rows) {
          html += `<div class="hero-row">
            ${portrait(row.hero)}
            <div class="hero-main">
              <div class="hero-name">${escapeHtml(row.hero.name)}${warnFlag(row.stats)}</div>
              <div class="hero-detail">픽 ${pct(row.stats[0])} · 밴 ${pct(row.stats[1])}</div>
            </div>
            <div class="epr">${pct(row.epr)}<br>${deltaTag(row.epr, baseEpr[row.id])}</div>
          </div>`;
        }
        html += `</div>`;
      }
      html += `</article>`;
    }
    html += `</div>`;
  }
  main.innerHTML = html;
}

/* ---------- 뷰: 전체 순위 ---------- */

const COLUMNS = [
  { key: 'name', label: '영웅' },
  { key: 'epr', label: '유효 픽률' },
  { key: 'pick', label: '픽률' },
  { key: 'ban', label: '밴률' },
  { key: 'win', label: '승률' },
];

function renderRanking(shard) {
  const mapStats = shard.maps[state.map];
  if (!mapStats) {
    main.innerHTML = `<p class="error">이 전장 데이터가 없습니다.</p>`;
    return;
  }
  const rows = ranked(mapStats, state.role).map((row) => ({
    ...row,
    pick: row.stats[0],
    ban: row.stats[1],
    win: row.stats[2],
    name: row.hero.name,
  }));

  const key = state.sort;
  rows.sort((a, b) => {
    if (key === 'name') return a.name.localeCompare(b.name, 'ko');
    return (b[key] ?? -1) - (a[key] ?? -1);
  });
  if (!state.desc) rows.reverse();

  const mapName =
    state.map === BASELINE
      ? '모든 전장'
      : (meta.maps.find((m) => m.slug === state.map) || {}).name || state.map;

  let html = `<p class="legend">${escapeHtml(mapName)} · ${TIER_LABEL[shard.tier] || shard.tier}
    · ${REGION_LABEL[shard.region] || shard.region} · 열 제목을 누르면 정렬됩니다.</p>
    <div class="table-wrap"><table><thead><tr>`;
  for (const column of COLUMNS) {
    const sorted = key === column.key ? ' sorted' : '';
    const arrow = key === column.key ? (state.desc ? ' ▼' : ' ▲') : '';
    html += `<th class="${sorted}" data-sort="${column.key}">${column.label}${arrow}</th>`;
  }
  html += `</tr></thead><tbody>`;
  for (const row of rows) {
    html += `<tr>
      <td class="hero-cell">${portrait(row.hero)}
        <span>${escapeHtml(row.name)}</span>
        <span class="role-tag">${escapeHtml(roleLabel(row.hero.role))}</span>${warnFlag(row.stats)}</td>
      <td><b>${pct(row.epr)}</b></td>
      <td>${pct(row.pick)}</td>
      <td>${pct(row.ban)}</td>
      <td>${pct(row.win)}</td>
    </tr>`;
  }
  html += `</tbody></table></div>`;
  main.innerHTML = html;

  main.querySelectorAll('th').forEach((th) => {
    th.addEventListener('click', () => {
      const next = th.dataset.sort;
      state.desc = state.sort === next ? !state.desc : true;
      state.sort = next;
      render();
    });
  });
}

/* ---------- 뷰: 티어별 추이 ---------- */

async function renderTiers() {
  const hero = meta.heroes[state.hero];
  if (!hero) {
    main.innerHTML = `<p class="error">영웅을 선택해 주세요.</p>`;
    return;
  }
  const shards = await Promise.all(
    meta.tiers.map((tier) =>
      loadShard(tier, state.region)
        .then((shard) => ({ tier, shard }))
        .catch(() => ({ tier, shard: null }))
    )
  );

  const points = shards.map(({ tier, shard }) => {
    const stats = shard && shard.maps[state.map2] ? shard.maps[state.map2][state.hero] : null;
    return { tier, stats, epr: effective(stats) };
  });
  const max = Math.max(1, ...points.map((p) => p.epr || 0));

  const mapName =
    state.map2 === BASELINE
      ? '모든 전장'
      : (meta.maps.find((m) => m.slug === state.map2) || {}).name || state.map2;

  let html = `<p class="legend">
    <b>${escapeHtml(hero.name)}</b> · ${escapeHtml(mapName)} ·
    ${REGION_LABEL[state.region] || state.region} ·
    막대 전체 길이가 유효 픽률, 진한 부분이 원본 픽률입니다 — 차이가 밴 보정분입니다.</p>
    <div class="tier-rows">`;
  for (const point of points) {
    const width = point.epr ? (point.epr / max) * 100 : 0;
    const rawWidth = point.stats && point.stats[0] ? (point.stats[0] / max) * 100 : 0;
    html += `<div class="tier-row">
      <div class="tier-label">${escapeHtml(TIER_LABEL[point.tier] || point.tier)}</div>
      <div class="bar-track">
        <div class="bar-fill" style="width:${width.toFixed(1)}%"></div>
        <div class="bar-raw" style="width:${rawWidth.toFixed(1)}%"></div>
      </div>
      <div class="tier-value">${pct(point.epr)}${warnFlag(point.stats)}</div>
    </div>`;
  }
  html += `</div><p class="legend">밴률: ${points
    .map((p) => `${TIER_LABEL[p.tier] || p.tier} ${pct(p.stats ? p.stats[1] : null)}`)
    .join(' · ')}</p>`;
  main.innerHTML = html;
}

/* ---------- 뷰: 메타 분석 ---------- */
// data/analysis.json 은 scripts/meta_facts.py 가 만든다. 직전 패치 구간과 현재 구간의
// 등급별 수치(소수점 첫째 자리로 반올림됨), 구간 사이 패치의 영웅별 변경, 자동 해설을 담는다.

const MOVERS_LIMIT = 8; // 패치 변경 없이 크게 움직인 영웅을 몇 명까지 보여줄지

let analysisPromise = null;

// 실패해도 다른 탭에는 영향이 없도록, 이 탭을 열 때 처음 한 번만 받는다.
function loadAnalysis() {
  if (!analysisPromise) {
    const version = meta && meta.generatedAt ? `?v=${encodeURIComponent(meta.generatedAt)}` : '';
    analysisPromise = fetch(`data/analysis.json${version}`, { cache: 'no-cache' }).then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    });
  }
  return analysisPromise;
}

// '2026-11-07' -> '11월 7일'
function dayLabel(iso) {
  const [, month, day] = String(iso).split('-');
  return month && day ? `${Number(month)}월 ${Number(day)}일` : String(iso);
}

const autoTag = `<span class="auto-tag" title="사실 표를 읽고 Claude 가 자동으로 쓴 문장입니다. 숫자는 사실 표와 대조했지만 해석은 틀릴 수 있습니다.">자동 생성</span>`;

function commentaryLine(sentence, cls) {
  if (!sentence || !sentence.text) return '';
  return `<p class="${cls}">${autoTag} ${escapeHtml(sentence.text)}</p>`;
}

// 직전 구간 대비 변화(%p). 밴률은 오르내림이 좋고 나쁨을 뜻하지 않아 색을 빼고 보여준다.
function changeTag(delta, neutral = false) {
  if (delta == null) return '';
  if (Math.abs(delta) < 0.05) return `<span class="delta flat">±0</span>`;
  const cls = neutral ? 'flat' : delta > 0 ? 'up' : 'down';
  const arrow = delta > 0 ? '▲' : '▼';
  return `<span class="delta ${cls}" title="직전 구간 대비">${arrow}${Math.abs(delta).toFixed(1)}</span>`;
}

function analysisStats(row) {
  const raw = [row.now.pick, row.now.ban, row.now.win];
  const delta = row.delta || {};
  const cell = (label, key, extra = '', neutral = false) => `<span class="an-stat">
      <span class="an-stat-label">${label}</span>
      <b>${pct(row.now[key])}</b>${extra} ${changeTag(delta[key], neutral)}</span>`;
  return `<span class="an-stats">
    ${cell('유효 픽률', 'epr', warnFlag(raw))}${cell('픽률', 'pick')}${cell('밴률', 'ban', '', true)}${cell('승률', 'win')}
  </span>`;
}

function heroHead(row) {
  const hero = meta.heroes[row.hero] || { name: row.hero };
  return `<span class="an-hero-name">${portrait(hero)}<span>${escapeHtml(hero.name)}</span>
    <span class="role-tag">${escapeHtml(roleLabel(hero.role))}</span></span>${analysisStats(row)}`;
}

function heroCard(row, analysis, notes) {
  const noteHtml = notes.map((note) => commentaryLine(note, 'an-note')).join('');
  if (!row.changes.length) {
    return `<div class="an-hero"><div class="an-hero-head">${heroHead(row)}</div>${noteHtml}</div>`;
  }
  const patchOf = (newsNo) => analysis.patches.find((p) => p.newsNo === newsNo) || {};
  const changes = row.changes
    .map((id) => analysis.changes[id])
    .filter(Boolean)
    .map((change) => {
      const patch = patchOf(change.newsNo);
      const title = patch.title ? `${escapeHtml(patch.title)}` : `패치 ${escapeHtml(change.newsNo)}`;
      const lines = change.lines.map((line) => `<li>${escapeHtml(line)}</li>`).join('');
      const dev = change.dev ? `<p class="an-dev"><b>개발자의 의견</b> ${escapeHtml(change.dev)}</p>` : '';
      return `<div class="an-change"><div class="an-change-title">${title}</div>${lines ? `<ul>${lines}</ul>` : ''}${dev}</div>`;
    })
    .join('');
  return `<div class="an-hero">
    <details>
      <summary class="an-hero-head">${heroHead(row)}<span class="an-toggle">변경 내용</span></summary>
      <div class="an-changes">${changes}</div>
    </details>
    ${noteHtml}
  </div>`;
}

function byRole(rows) {
  return roles
    .map((role) => ({
      role,
      rows: rows
        .filter((row) => (meta.heroes[row.hero] || {}).role === role)
        .sort((a, b) => b.now.epr - a.now.epr),
    }))
    .filter((group) => group.rows.length);
}

async function renderAnalysis() {
  let analysis;
  try {
    analysis = await loadAnalysis();
  } catch (error) {
    main.innerHTML = `<p class="notice">분석 데이터 없음 — 아직 기록이 없거나 오늘 분석 단계가 실패했습니다.
      다른 탭은 그대로 쓸 수 있습니다. <span class="fine">(${escapeHtml(error.message)})</span></p>`;
    return;
  }
  const rows = (analysis.tiers || {})[state.tier];
  const tierName = TIER_LABEL[state.tier] || state.tier;
  if (!rows || !rows.length) {
    main.innerHTML = `<p class="notice">${escapeHtml(tierName)} 분석 데이터가 없습니다.</p>`;
    return;
  }
  const first = !analysis.previous;

  // 1. 구간과 패치
  const period = first
    ? `<b>${dayLabel(analysis.current.since)} 구간</b>`
    : `<b>${dayLabel(analysis.previous.since)} 구간</b> → <b>${dayLabel(analysis.current.since)} 구간</b>`;
  const patches = [...analysis.patches].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const patchLinks = patches.length
    ? patches
        .map((p) => {
          const title = escapeHtml(p.title || `패치 ${p.newsNo}`);
          const link = /^https:\/\//.test(p.url || '')
            ? `<a href="${escapeHtml(p.url)}" target="_blank" rel="noopener">${title}</a>`
            : title;
          return `<li>${link} <span class="fine">${dayLabel(p.date)}</span></li>`;
        })
        .join('')
    : `<li class="fine">${first ? '최근 패치노트를 찾지 못했습니다.' : '구간 사이에 나온 패치노트가 없습니다.'}</li>`;

  let html = `<section class="an-period">
    <p>${period} · ${escapeHtml(tierName)} · 한국</p>
    <p class="an-label">${first ? '최근 패치' : '구간 사이 패치'}</p>
    <ul class="an-patches">${patchLinks}</ul>
    ${first ? `<p class="notice">직전 구간 기록이 없어 현재 수치와 최근 패치만 보여줍니다. 다음 패치 이후 비교가 시작됩니다.</p>` : ''}
  </section>`;

  // 2. 해설
  const commentary = analysis.commentary;
  const tierCommentary = commentary && commentary.tiers ? commentary.tiers[state.tier] : null;
  const notesOf = (hero) =>
    tierCommentary && tierCommentary.notes ? tierCommentary.notes.filter((note) => note.hero === hero) : [];
  if (commentary && (commentary.overview || tierCommentary)) {
    html += `<section class="an-commentary">
      ${commentaryLine(commentary.overview, 'an-overview')}
      ${tierCommentary ? commentaryLine(tierCommentary.headline, 'an-headline') : ''}
      ${tierCommentary ? '' : `<p class="fine">${escapeHtml(tierName)} 자동 해설은 없습니다.</p>`}
    </section>`;
  } else {
    html += `<p class="notice">자동 해설이 없습니다. 아래 사실만 보여줍니다.</p>`;
  }

  html += first
    ? `<p class="legend">굵은 숫자는 현재 구간 값입니다. 행을 누르면 패치 변경 내용이 나옵니다.</p>`
    : `<p class="legend">굵은 숫자는 현재 구간 값이고, ▲▼는 직전 구간 대비 변화(%p)입니다.
    구간 사이 패치와 수치 변화는 시간 순서가 겹칠 뿐, 패치가 원인이라고 확인된 것은 아닙니다.</p>`;

  // 3. 구간 사이 패치에서 바뀐 영웅
  const changed = rows.filter((row) => row.changes.length);
  html += `<h2 class="mode-title">${first ? '최근 패치에서 바뀐 영웅' : '구간 사이 패치에서 바뀐 영웅'}</h2>`;
  if (!changed.length) html += `<p class="fine">바뀐 영웅이 없습니다.</p>`;
  for (const group of byRole(changed)) {
    html += `<div class="an-role"><div class="role-name role-${escapeHtml(group.role)}">${escapeHtml(roleLabel(group.role))}</div>
      ${group.rows.map((row) => heroCard(row, analysis, notesOf(row.hero))).join('')}</div>`;
  }

  // 4. 변경 없이 크게 움직인 영웅 (+ 해설이 붙은 나머지 영웅)
  if (!first) {
    const score = (row) => (row.delta ? Math.max(Math.abs(row.delta.win), Math.abs(row.delta.epr)) : 0);
    const unchanged = rows.filter((row) => !row.changes.length && row.delta);
    const top = [...unchanged].sort((a, b) => score(b) - score(a)).filter((row) => score(row) > 0).slice(0, MOVERS_LIMIT);
    const movers = unchanged.filter((row) => top.includes(row) || notesOf(row.hero).length);
    movers.sort((a, b) => score(b) - score(a));
    html += `<h2 class="mode-title">변경 없이 크게 움직인 영웅</h2>`;
    html += movers.length
      ? movers.map((row) => heroCard(row, analysis, notesOf(row.hero))).join('')
      : `<p class="fine">눈에 띄게 움직인 영웅이 없습니다.</p>`;
  }

  main.innerHTML = html;
}

/* ---------- 컨트롤 ---------- */

function fillSelect(select, values, labeler) {
  select.innerHTML = values
    .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(labeler(value))}</option>`)
    .join('');
}

function syncControls() {
  el('f-tier').value = state.tier;
  el('f-region').value = state.region;
  el('f-map').value = state.map;
  el('f-map2').value = state.map2;
  el('f-role').value = state.role;
  el('f-topn').value = String(state.topn);
  el('f-hero').value = state.hero;

  document.querySelectorAll('#filters label[data-only]').forEach((label) => {
    label.hidden = label.dataset.only !== state.view;
  });
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === state.view);
  });
  el('f-tier').closest('label').hidden = state.view === 'tiers';
  // 분석은 한국만 한다.
  el('f-region').closest('label').hidden = state.view === 'analysis';
}

function pushUrl() {
  const params = new URLSearchParams();
  for (const key of ['view', 'tier', 'region', 'map', 'map2', 'role', 'topn', 'hero']) {
    params.set(key, state[key]);
  }
  history.replaceState(null, '', `?${params}`);
}

function readUrl() {
  const params = new URLSearchParams(location.search);
  for (const key of ['view', 'tier', 'region', 'map', 'map2', 'role', 'topn', 'hero']) {
    if (params.has(key)) state[key] = params.get(key);
  }
}

/* 영웅·맵·티어가 바뀌면 예전에 공유된 링크의 값이 더 이상 존재하지 않을 수 있다.
 * 그럴 때 404 나 빈 화면 대신 있는 값으로 되돌린다. 기본값과 URL 값 모두에 적용한다. */
function validateState() {
  const heroIds = Object.keys(meta.heroes);
  const fallback = (key, allowed, first) => {
    if (!allowed.includes(state[key])) state[key] = first;
  };
  fallback('view', ['maps', 'ranking', 'tiers', 'analysis'], 'maps');
  fallback('tier', meta.tiers, meta.tiers[0]);
  fallback('region', meta.regions, meta.regions[0]);
  fallback('map', mapSlugs, BASELINE);
  fallback('map2', mapSlugs, BASELINE);
  fallback('role', ['ALL', ...roles], 'ALL');
  fallback('hero', heroIds, heroIds[0]);
  const topnValues = [...el('f-topn').options].map((option) => option.value);
  fallback('topn', topnValues, topnValues[0]);
}

async function render() {
  syncControls();
  pushUrl();
  main.innerHTML = `<p class="loading">불러오는 중...</p>`;
  try {
    if (state.view === 'tiers') {
      await renderTiers();
      return;
    }
    if (state.view === 'analysis') {
      await renderAnalysis();
      return;
    }
    const shard = await loadShard(state.tier, state.region);
    if (state.view === 'ranking') renderRanking(shard);
    else renderMaps(shard);
  } catch (error) {
    main.innerHTML = `<p class="error">데이터를 불러오지 못했습니다: ${escapeHtml(error.message)}</p>`;
  }
}

async function init() {
  try {
    // meta 는 항상 최신을 확인한다 (12KB). 샤드는 meta 버전으로 캐시를 가른다.
    meta = await fetch('data/meta.json', { cache: 'no-cache' }).then((response) => {
      if (!response.ok) throw new Error(`meta.json — HTTP ${response.status}`);
      return response.json();
    });
  } catch (error) {
    main.innerHTML = `<p class="error">메타데이터를 불러오지 못했습니다: ${escapeHtml(error.message)}
      <br>수집기를 먼저 실행해 주세요: <code>python3 scripts/fetch_rates.py</code></p>`;
    return;
  }

  // 선택 상자는 전부 meta 에서 만든다. 영웅·맵·모드·티어가 늘어나면 그대로 따라온다.
  if (meta.baselineMap) BASELINE = meta.baselineMap;
  roles = detectRoles();
  mapSlugs = [BASELINE, ...meta.maps.map((m) => m.slug)];
  const mapLabel = (slug) =>
    slug === BASELINE ? '모든 전장' : (meta.maps.find((m) => m.slug === slug) || {}).name || slug;

  fillSelect(el('f-tier'), meta.tiers, (v) => TIER_LABEL[v] || v);
  fillSelect(el('f-region'), meta.regions, (v) => REGION_LABEL[v] || v);
  fillSelect(el('f-map'), mapSlugs, mapLabel);
  fillSelect(el('f-map2'), mapSlugs, mapLabel);
  fillSelect(el('f-role'), ['ALL', ...roles], (v) => (v === 'ALL' ? '모든 역할' : roleLabel(v)));

  const rankOf = (id) => {
    const index = roles.indexOf(meta.heroes[id].role);
    return index < 0 ? roles.length : index;
  };
  const heroIds = Object.keys(meta.heroes).sort(
    (a, b) =>
      rankOf(a) - rankOf(b) ||
      meta.heroes[a].name.localeCompare(meta.heroes[b].name, 'ko')
  );
  fillSelect(
    el('f-hero'),
    heroIds,
    (id) => `${meta.heroes[id].name} (${roleLabel(meta.heroes[id].role)})`
  );

  readUrl();
  validateState();

  if (meta.generatedAt) {
    const when = new Date(meta.generatedAt);
    el('updated').textContent = `갱신: ${when.toLocaleString('ko-KR', {
      dateStyle: 'medium',
      timeStyle: 'short',
    })}`;
  }

  const bind = (id, key) =>
    el(id).addEventListener('change', (event) => {
      state[key] = event.target.value;
      render();
    });
  bind('f-tier', 'tier');
  bind('f-region', 'region');
  bind('f-map', 'map');
  bind('f-map2', 'map2');
  bind('f-role', 'role');
  bind('f-topn', 'topn');
  bind('f-hero', 'hero');

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      state.view = tab.dataset.view;
      render();
    });
  });

  render();
}

init();
