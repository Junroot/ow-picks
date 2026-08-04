'use strict';

/* 오버워치 유효 픽률
 * 유효 픽률 = 픽률 / (1 - 밴률)  — 그 영웅이 선택 가능했던 경기에서의 픽률.
 * 수집기는 원본 수치(픽률·밴률·승률)만 저장하고, 계산은 전부 여기서 한다.
 */

// 표시 순서 힌트. 여기에 없는 역할이 새로 생기면 뒤에 붙는다 (숨기지 않는다).
const ROLE_ORDER_HINT = ['TANK', 'DAMAGE', 'SUPPORT'];
const ROLE_LABEL = { TANK: '돌격', DAMAGE: '공격', SUPPORT: '지원' };
const roleLabel = (role) => ROLE_LABEL[role] || role || '기타';
const TIER_LABEL = {
  All: '모든 등급', Bronze: '브론즈', Silver: '실버', Gold: '골드',
  Platinum: '플래티넘', Diamond: '다이아몬드', Master: '마스터',
  Grandmaster: '그랜드마스터 및 챔피언',
};
const REGION_LABEL = { Americas: '아메리카', Asia: '아시아', Europe: '유럽' };
const BASELINE = 'all-maps';
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
  tier: 'All',
  region: 'Asia',
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
  return `data/${meta.input || 'PC'}_${tier}_${region}.json${version}`;
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
  fallback('view', ['maps', 'ranking', 'tiers'], 'maps');
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
