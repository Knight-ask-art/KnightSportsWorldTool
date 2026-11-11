/* ---------- tiny helpers ---------- */
const $ = s => document.querySelector(s);
/* Read a skin token for Leaflet/canvas, which cannot use CSS variables
   directly.  A missing token falls back to the paper palette so a partially
   loaded stylesheet never leaves an invisible route line. */
function themeColor(name, fallback){
  try{
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue(name).trim();
    return value || fallback;
  }catch(e){ return fallback; }
}
/* Keyboard events dispatched on document (tests, extensions) have no
   Element target; calling closest() there is an uncaught TypeError. */
const eventTargetEl = ev => {
  const t = ev && ev.target;
  return (t && typeof t.closest === 'function') ? t : null;
};
const toastEl = $('#toast');
let toastTimer = null;
/* The boot splash covers the window for the first seconds.  A toast shown
   then is invisible, and its 2.6 s life is over before the splash leaves, so
   "update available" / "updated to vX" used to be lost.  Queue while the
   splash is visible and let splash.js's hxp:boot-done release the queue. */
const toastQueue = [];
let bootFlushTimer = null;
function bootSplashVisible(){
  const splash = document.getElementById('bootSplash');
  if(!splash) return false;
  const style = getComputedStyle(splash);
  return style.display !== 'none' && style.visibility !== 'hidden' &&
         style.opacity !== '0';
}
function flushBootToasts(){
  bootFlushTimer = null;
  if(!toastQueue.length) return;
  if(bootSplashVisible()){
    bootFlushTimer = setTimeout(flushBootToasts, 500);
    return;
  }
  toast(toastQueue.shift());
  if(toastQueue.length) setTimeout(flushBootToasts, 2700);
}
function toast(msg){
  if(bootSplashVisible()){
    if(!toastQueue.includes(msg)) toastQueue.push(msg);
    if(toastQueue.length > 3) toastQueue.shift();
    if(!bootFlushTimer) bootFlushTimer = setTimeout(flushBootToasts, 500);
    return;
  }
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2600);
}
/* Uiverse-style press ripple.  Delegated once so re-rendered rows and
   buttons keep the feedback without re-binding. */
const RIPPLE_HOSTS =
  '.btn,.new-run,.filter,.rtab,.route-item,.record-row,.onboard-x';
document.addEventListener('pointerdown', ev => {
  if(ev.button !== 0) return;
  if(window.matchMedia &&
     window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const host = eventTargetEl(ev) ? eventTargetEl(ev).closest(RIPPLE_HOSTS) : null;
  if(!host || host.disabled || host.getAttribute('aria-disabled') === 'true') return;
  const rect = host.getBoundingClientRect();
  if(!rect.width || !rect.height) return;
  const size = Math.max(rect.width, rect.height) * 1.2;
  const dot = document.createElement('span');
  dot.className = 'ink-ripple';
  dot.style.width = dot.style.height = size + 'px';
  dot.style.left = (ev.clientX - rect.left - size / 2) + 'px';
  dot.style.top = (ev.clientY - rect.top - size / 2) + 'px';
  host.appendChild(dot);
  setTimeout(() => dot.remove(), 620);
}, {passive: true});
function focusablesIn(root){
  if(!root) return [];
  return [...root.querySelectorAll(
    'a[href],button:not([disabled]),input:not([disabled]),' +
    'select:not([disabled]),textarea:not([disabled]),' +
    '[tabindex]:not([tabindex="-1"])')]
    .filter(el => !el.hidden && el.offsetParent !== null &&
                  getComputedStyle(el).visibility !== 'hidden');
}
function trapTabIn(root, ev){
  if(!root || ev.key !== 'Tab') return false;
  const items = focusablesIn(root);
  if(!items.length) return false;
  const first = items[0], last = items[items.length - 1];
  const active = document.activeElement;
  if(!root.contains(active)){
    ev.preventDefault();
    (ev.shiftKey ? last : first).focus();
    return true;
  }
  if(ev.shiftKey && active === first){ ev.preventDefault(); last.focus(); return true; }
  if(!ev.shiftKey && active === last){ ev.preventDefault(); first.focus(); return true; }
  return false;
}
/* one delegated handler, so links rebuilt by the checklist keep working */
document.addEventListener('click', ev => {
  const t = eventTargetEl(ev);
  if(!t) return;
  const el = t.closest('[data-toast]');
  if(!el) return;
  if(el.tagName === 'A') ev.preventDefault();
  toast(el.dataset.toast);
});

/* Request/data sequence counters.  Declared before the first top-level call
   (redraw -> scheduleModelPreview) so no let/const is read in its TDZ. */
let _pointsSeq = 0, _recordsSeq = 0, _overviewSeq = 0;
let _receiptsSeq = 0, _dateCountsSeq = 0, _detailSeq = 0, _modelSeq = 0;
let _pointsKmTimer = 0, _routeFinalTimer = 0, _pointsStateTimer = 0;
let _profileSeq = 0, _violSeq = 0;
let _headerDay = '', _modelTimer = null, accountBusy = false;
let _pointsAutoAt = 0;
let PENDING_SUBMIT = null;
let POST_SUBMIT = null;
let distanceManual = false, lastRouteLength = 0;
function invalidateLiveRequests(){
  // Bump every request sequence so replies that belong to a previous account
  // (or a previous logout) cannot repopulate the UI after the switch.
  _pointsSeq++; _recordsSeq++; _overviewSeq++;
  _receiptsSeq++; _dateCountsSeq++; _detailSeq++; _modelSeq++;
  _profileSeq++; _violSeq++;
}

/* ---------- map ---------- */
/* Neutral fallback: the tool must never pretend another campus is yours.
   A real session centre (fence or manual) replaces this once known. */
const CENTER = [35.8617, 104.1954];
const CENTER_ZOOM = 4;
const map = L.map('map', {zoomControl: false, attributionControl: false, doubleClickZoom: false})
  .setView(CENTER, CENTER_ZOOM);

function sessionCenter(){
  const s = (typeof LIVE !== 'undefined' && LIVE.session) ? LIVE.session : null;
  if(!s) return null;
  const lat = +s.lat, lon = +s.lon;
  if(!isFinite(lat) || !isFinite(lon)) return null;
  if(Math.abs(lat) < 1e-6 && Math.abs(lon) < 1e-6) return null;
  return [lat, lon];
}

/* 高德卫星/路网在 z19 对部分区域返回“此区域无卫星图”占位瓦片。
   把原生瓦片封顶在 z18，z19 由 Leaflet 放大 z18 影像，避免整屏占位图。 */
const street = L.tileLayer(
  'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
  {subdomains: '1234', maxNativeZoom: 18, maxZoom: 19, attribution: '高德地图'});
const satellite = L.tileLayer(
  'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
  {subdomains: '1234', maxNativeZoom: 18, maxZoom: 19, attribution: '高德卫星'}).addTo(map);

/* live map layers: filled from /bridge/real_fetch_points; real checkpoints only */
let fenceLayer = L.polygon([], {
  color: themeColor('--stamp', '#B6483C'), weight: 1.8, dashArray: '6 5',
  fillColor: themeColor('--stamp', '#B6483C'), fillOpacity: .035
}).addTo(map).bindTooltip('跑步围栏', {sticky: true, className: 'pt-tip'});
let extraFenceLayers = [];
let fenceBounds = L.latLngBounds([]);
const pointLayer = L.layerGroup().addTo(map);
/* kept name for the existing route code; it contains only real checkpoints */
let livePoints = [];
/* Split editor state is touched by redraw() during boot (before the later
   `let` block would initialise), so it must be hoisted — see the TDZ note at
   the top of this file. */
var splitRows = null;           // [{km, seconds}]; null = generated on preview
var splitAutoRows = [];         // last generated preview rows
var splitsManual = false;
var _splitsTimer = null;

function renderPoints(points, opts){
  opts = opts || {};
  livePoints = (points || []).map((p, i) => {
    // Accept both the server shape (name/lat/lon/isMustPoint) and the
    // normalised shape this function produces (n/lat/lng/must).  Toggling a
    // checkbox re-renders from the latter; reading only p.lon blanked every
    // checkpoint on the map.
    const lat = p.lat != null ? p.lat : p.glat;
    const lng = p.lon != null ? p.lon : p.lng;
    return {
      n: p.name || p.pointName || p.n || ('点位 ' + (i + 1)),
      lat: +lat, lng: +lng,
      must: p.isMustPoint != null ? !!p.isMustPoint : !!p.must,
      isFixed: p.isFixed,
      position: p.position,
      seq: p.seq,
      r: 0
    };
  }).filter(p => isFinite(p.lat) && isFinite(p.lng));

  pointLayer.clearLayers();
  const list = $('#ptList');
  if(list) list.innerHTML = '';

  livePoints.forEach((p, i) => {
    const icon = L.divIcon({
      className: '',
      html: `<div class="pt-pin ${p.must ? 'must' : ''}">${i + 1}${p.must ? '<span class="star">★</span>' : ''}</div>`,
      iconSize: [25, 25], iconAnchor: [12, 12]
    });
    L.marker([p.lat, p.lng], {icon, keyboard: false})
      .addTo(pointLayer)
      .bindTooltip(esc(p.n) + (p.must ? ' · 必经' : ''), {direction: 'top', offset: [0, -11], className: 'pt-tip'});

    if(list){
      const row = document.createElement('div');
      row.innerHTML = `<span class="star">${p.must ? '★' : '·'}</span>${esc(p.n)}`;
      row.title = '在地图上定位：' + p.n;
      row.tabIndex = 0;
      row.setAttribute('role', 'button');
      const focusPoint = () => map.setView([p.lat, p.lng], Math.max(map.getZoom(), 18));
      row.addEventListener('click', focusPoint);
      row.addEventListener('keydown', e => {
        if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); focusPoint(); }
      });
      list.appendChild(row);
    }
  });

  const must = livePoints.filter(p => p.must).length;
  const badge = $('#ptBadge');
  if(badge){
    if(freeRunOn()){
      badge.textContent = '跳过点位 · 不查打卡点';
      badge.className = 'tag warn';
    }else if(livePoints.length){
      const distMismatch = pointsDistanceStale();
      badge.textContent = livePoints.length + ' 个 · ' + must + ' 必经' +
        (LIVE.pointsStale ? ' · 缓存'
          : (distMismatch ? ' · 距离待刷新' : ''));
      badge.className = 'tag ' +
        ((LIVE.pointsStale || distMismatch) ? 'warn' : 'ok');
    }else if(LIVE.pointsEmpty){
      badge.textContent = '无打卡点（需确认）';
      badge.className = 'tag warn';
    }else{
      badge.textContent = '未加载点位';
      badge.className = 'tag warn';
    }
  }
  if(list && !livePoints.length){
    let msg;
    if(freeRunOn()) msg = '已勾选「跳过打卡点」：提交时不拉取打卡点。';
    else if(!hasSession()) msg = '登录后从服务器拉取打卡点。';
    else if(LIVE.pointsError) msg = '点位读取失败：' + LIVE.pointsError +
      '。已保留上次加载的点位；' + (pointsCooldownLeft() > 0
        ? ('约 ' + pointsCooldownLeft() + ' 秒后可重试。')
        : '点「刷新」重试。');
    else if(LIVE.pointsEmpty) msg = '服务器没有返回打卡点，无法确认该校区是否允许自由跑；提交默认阻止。如已确认，请勾选「跳过打卡点」。';
    else msg = '服务器没有返回打卡点；点「刷新」重试。';
    list.innerHTML = '<div class="empty-state">' + msg + '</div>';
  }
  if(livePoints.length && opts.fit === true){
    const b = L.latLngBounds(livePoints.map(p => [p.lat, p.lng]));
    map.fitBounds(b.pad(.18), runMapPadding());
  }
}
function _fenceLatLngs(ring){
  return (Array.isArray(ring) ? ring : [])
    .map(p => [p && (p.lat ?? p.glat), p && (p.lon ?? p.glon)])
    .filter(p => isFinite(p[0]) && isFinite(p[1]));
}
function setFences(polygons, name, opts){
  // The official `isInArea` accepts a point inside any matching fence, so
  // draw every ring and keep the first one in `fenceLayer` for compatibility.
  const rings = (Array.isArray(polygons) ? polygons : [])
    .map(_fenceLatLngs).filter(r => r.length >= 3);
  const first = rings.length ? rings[0] : [];
  extraFenceLayers.forEach(layer => map.removeLayer(layer));
  extraFenceLayers = [];
  fenceLayer.setLatLngs(first);
  rings.slice(1).forEach(ring => {
    const layer = L.polygon(ring, {
      color: themeColor('--stamp', '#B6483C'), weight: 1.8, dashArray: '6 5',
      fillColor: themeColor('--stamp', '#B6483C'), fillOpacity: .035
    }).addTo(map).bindTooltip('跑步围栏', {sticky: true, className: 'pt-tip'});
    extraFenceLayers.push(layer);
  });
  const suffix = rings.length > 1 ? ('（' + rings.length + ' 个）')
    : (rings.length ? '' : '（未加载）');
  fenceLayer.setTooltipContent(esc(name || '跑步围栏') + suffix);
  fenceBounds = L.latLngBounds([].concat(...rings));
  try{
    routeFenceLayer.setLatLngs(first);
    routeFenceExtra.forEach(layer => routeMap.removeLayer(layer));
    routeFenceExtra = [];
    rings.slice(1).forEach(ring => {
      const layer = L.polygon(ring, {
        color: themeColor('--stamp', '#B6483C'), weight: 1.6, dashArray: '6 5',
        fillColor: themeColor('--stamp', '#B6483C'), fillOpacity: .03
      }).addTo(routeMap);
      routeFenceExtra.push(layer);
    });
  }catch(e){}
  if(first.length && opts && opts.fit === true){
    map.fitBounds(fenceBounds.pad(.18), runMapPadding());
  }
}
function setFence(polygon, name, opts){
  setFences(polygon ? [polygon] : [], name, opts);
}
function fenceRingsFromData(d){
  let rings = [];
  if(d && Array.isArray(d.fences) && d.fences.length){
    rings = d.fences.map(f => Array.isArray(f && f.points) ? f.points
      : (Array.isArray(f) ? f : []));
  }else if(d && Array.isArray(d.polygons) && d.polygons.length){
    rings = d.polygons;
  }else if(d && Array.isArray(d.polygon)){
    rings = [d.polygon];
  }
  return rings.map(ring => (ring || []).filter(
    p => p && isFinite(+(p.lat ?? p.glat)) && isFinite(+(p.lon ?? p.glon))))
    .filter(ring => ring.length >= 3);
}

let route = [];

const hi = L.polyline([], {color: themeColor('--highlighter', '#F2D25C'), weight: 15, opacity: .5, lineCap: 'round', lineJoin: 'round'}).addTo(map);
const ink = L.polyline([], {color: themeColor('--ink-blue', '#2E4A7D'), weight: 3.1, opacity: .96, lineCap: 'round', lineJoin: 'round'}).addTo(map);

const start = L.circleMarker([0, 0], {radius: 5, color: themeColor('--paper-2', '#FBF8F0'), weight: 2, fillColor: themeColor('--ink-blue', '#2E4A7D'), fillOpacity: 1})
  .bindTooltip('起点', {permanent: true, direction: 'top', offset: [0, -6], className: 'pt-tip'});

function runMapPadding(){
  const small = window.innerWidth <= 900;
  return {
    paddingTopLeft: [36, 38],
    // On narrow windows the run panel sits at the bottom, so don't reserve
    // 390px on the right and hide the route behind the panel.
    paddingBottomRight: [small ? 16 : 390,
                         small ? Math.round(window.innerHeight * 0.48) : 38]
  };
}
function fitRunMap(){
  if(fenceBounds.isValid()){
    map.fitBounds(fenceBounds, runMapPadding());
  }else{
    const c = sessionCenter();
    map.setView(c || CENTER, c ? 16 : CENTER_ZOOM);
  }
}

/* ---------- route library map ---------- */
const routeMap = L.map('routeMap', {zoomControl: true, attributionControl: true,
  scrollWheelZoom: false})
  .setView(CENTER, CENTER_ZOOM);
L.tileLayer(
  'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
  {subdomains: '1234', maxNativeZoom: 18, maxZoom: 19, attribution: '高德卫星'}).addTo(routeMap);
let routeFenceLayer = L.polygon([], {
  color: themeColor('--stamp', '#B6483C'), weight: 1.6, dashArray: '6 5',
  fillColor: themeColor('--stamp', '#B6483C'), fillOpacity: .03
}).addTo(routeMap);
let routeFenceExtra = [];

const routeHi = L.polyline([], {color: themeColor('--highlighter', '#F2D25C'), weight: 13, opacity: .45, lineCap: 'round'}).addTo(routeMap);
const routeInk = L.polyline([], {color: themeColor('--ink-blue', '#2E4A7D'), weight: 3, opacity: .95, lineCap: 'round'}).addTo(routeMap);
const routeStart = L.circleMarker([0, 0], {radius: 5, color: themeColor('--paper-2', '#FBF8F0'), weight: 2, fillColor: themeColor('--ink-blue', '#2E4A7D'), fillOpacity: 1});

/* ---------- draw: click to add, smooth curve ---------- */
let pen = false, smoothOn = true, routeClosed = false;
let ctrlDots = [];
let selRoutePoint = -1;
let dragPointIndex = -1, dragPointSnapshot = null, dragPointMoved = false;
let routeClickGuardUntil = 0;

/* Route edits are snapshots, not a single pop().  Deleting the 3rd point and
   then pressing undo must restore the 3rd point, not remove the last one. */
const ROUTE_HISTORY_LIMIT = 50;
const routeHistory = {undo: [], redo: []};
function routeSnapshot(){
  return {route: route.map(p => [+p[0], +p[1]]), closed: !!routeClosed};
}
function routeSnapshotKey(snap){
  return JSON.stringify([snap.route || [], !!snap.closed]);
}
function pushRouteSnapshot(snap, label){
  if(!snap) return false;
  const top = routeHistory.undo[routeHistory.undo.length - 1];
  if(top && routeSnapshotKey(top) === routeSnapshotKey(snap)) return false;
  routeHistory.undo.push({
    route: (snap.route || []).map(p => [+p[0], +p[1]]),
    closed: !!snap.closed,
    label: label || ''
  });
  if(routeHistory.undo.length > ROUTE_HISTORY_LIMIT) routeHistory.undo.shift();
  routeHistory.redo = [];
  syncRouteHistoryButtons();
  return true;
}
function pushRouteHistory(label){
  return pushRouteSnapshot(routeSnapshot(), label);
}
function syncRouteHistoryButtons(){
  const canUndo = routeHistory.undo.length > 0;
  const top = canUndo ? routeHistory.undo[routeHistory.undo.length - 1] : null;
  ['#undoTool', '#undoBtn'].forEach(sel => {
    const btn = $(sel);
    if(!btn) return;
    btn.disabled = !canUndo;
    btn.title = canUndo
      ? ('撤销' + (top.label ? '：' + top.label : '') +
         '（Ctrl+Z；重做 Ctrl+Shift+Z）')
      : '没有可撤销的操作（重做 Ctrl+Shift+Z）';
  });
}
function resetRouteHistory(){
  routeHistory.undo = [];
  routeHistory.redo = [];
  selRoutePoint = -1;
  syncRouteHistoryButtons();
}
function applyRouteSnapshot(snap){
  route = (snap.route || []).map(p => [+p[0], +p[1]]);
  setCloseUI(!!snap.closed);
  selRoutePoint = -1;
  redraw();
  refreshChecks();
  if(typeof scheduleModelPreview === 'function') scheduleModelPreview();
  if(!pen) scheduleRouteFinalize(600, 'undo');
}
function popDifferentSnapshot(stack){
  const current = routeSnapshotKey(routeSnapshot());
  let skipped = 0;
  while(stack.length){
    const snap = stack.pop();
    if(routeSnapshotKey(snap) !== current) return {snap, skipped};
    // An edit that returned to the previous state (add B, delete B) can
    // leave a no-op snapshot; skip it instead of showing "已撤销" with no
    // visible change.
    skipped++;
  }
  return {snap: null, skipped};
}
function undoRoute(){
  const {snap: prev} = popDifferentSnapshot(routeHistory.undo);
  if(!prev){ toast('没有可撤销的操作'); return; }
  routeHistory.redo.push(Object.assign(routeSnapshot(), {label: prev.label}));
  applyRouteSnapshot(prev);
  syncRouteHistoryButtons();
  toast(prev.label ? ('已撤销：' + prev.label) : '已撤销');
}
function redoRoute(){
  const {snap: next} = popDifferentSnapshot(routeHistory.redo);
  if(!next){ toast('没有可重做的操作'); return; }
  routeHistory.undo.push(Object.assign(routeSnapshot(), {label: next.label}));
  if(routeHistory.undo.length > ROUTE_HISTORY_LIMIT) routeHistory.undo.shift();
  applyRouteSnapshot(next);
  syncRouteHistoryButtons();
  toast('已重做');
}

function setPen(on, silent){
  pen = on;
  const btn = $('#penBtn');
  btn.classList.toggle('active', on);
  btn.setAttribute('aria-pressed', String(on));
  map.getContainer().style.cursor = on ? 'crosshair' : '';
  if(on && !silent) toast('画笔已打开：单击加点，右键删点；地图仍可拖动');
}
$('#penBtn').addEventListener('click', () => {
  const next = !pen;
  setPen(next);
  // Closing the pen is the clearest "line is done" signal.
  if(!next) scheduleRouteFinalize(300, 'pen-off');
});
setPen(true, true);

function setCloseUI(on){
  routeClosed = on;
  const toggle = $('#closeToggle');
  toggle.classList.toggle('on', on);
  toggle.setAttribute('aria-pressed', String(on));
  toggle.setAttribute('aria-label', '闭合路线（当前：' + (on ? '开' : '关') + '）');
  toggle.setAttribute('title', on
    ? '闭合开：3 个点也会首尾相连'
    : '闭合关：保留起点到终点的开放折线');
  const state = $('#closeState');
  if(state){
    state.textContent = on ? '开' : '关';
    state.classList.toggle('on', on);
  }
}
setCloseUI(false);

function distM(a, b){
  const dy = (b[0] - a[0]) * 110540;
  const dx = (b[1] - a[1]) * 111320 * Math.cos(a[0] * Math.PI / 180);
  return Math.hypot(dx, dy);
}
function pathLength(pts){
  let m = 0;
  for(let i = 1; i < pts.length; i++) m += distM(pts[i - 1], pts[i]);
  return m;
}

/* centripetal Catmull-Rom: passes through every control point */
function _cr(a, b, ta, tb, t){
  const w = (t - ta) / ((tb - ta) || 1e-6);
  return [a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w];
}
function _crPoint(p0, p1, p2, p3, t){
  const d = (a, b) => Math.pow(Math.hypot(a[0] - b[0], a[1] - b[1]), 0.5) || 1e-6;
  const t0 = 0, t1 = t0 + d(p0, p1), t2 = t1 + d(p1, p2), t3 = t2 + d(p2, p3);
  const tt = t1 + (t2 - t1) * t;
  const A1 = _cr(p0, p1, t0, t1, tt), A2 = _cr(p1, p2, t1, t2, tt), A3 = _cr(p2, p3, t2, t3, tt);
  const B1 = _cr(A1, A2, t0, t2, tt), B2 = _cr(A2, A3, t1, t3, tt);
  return _cr(B1, B2, t1, t2, tt);
}
function smoothPath(pts, closed, samples = 9){
  if(pts.length < 3) return pts.slice();
  const p = closed
    ? [pts[pts.length - 1], ...pts, pts[0], pts[1]]
    : [pts[0], ...pts, pts[pts.length - 1]];
  const out = [];
  for(let i = 1; i < p.length - 2; i++){
    for(let j = 0; j < samples; j++){
      out.push(_crPoint(p[i - 1], p[i], p[i + 1], p[i + 2], j / samples));
    }
  }
  out.push(closed ? out[0] : pts[pts.length - 1]);
  return out;
}
function displayPath(){
  if(route.length < 3) return route.slice();
  if(!smoothOn) return routeClosed ? route.concat([route[0]]) : route.slice();
  return smoothPath(route, routeClosed);
}
function routeDotStyle(i){
  if(i === selRoutePoint){
    return {radius: 7, color: themeColor('--stamp', '#B6483C'), weight: 2.2,
            fillColor: themeColor('--highlighter', '#F2D25C'), fillOpacity: 1};
  }
  return {
    radius: i === 0 ? 5 : 3.5,
    color: themeColor('--ink-blue', '#2E4A7D'), weight: 1.3,
    fillColor: i === 0 ? themeColor('--ink-blue', '#2E4A7D')
                       : themeColor('--paper-2', '#FBF8F0'), fillOpacity: 1
  };
}
function paintDots(){
  ctrlDots.forEach((d, i) => {
    const style = routeDotStyle(i);
    d.setStyle(style);
    if(d.setRadius) d.setRadius(style.radius);
  });
}
function routeCountText(){
  const base = route.length + ' 个控制点';
  return (selRoutePoint >= 0 && selRoutePoint < route.length)
    ? (base + ' · 已选第 ' + (selRoutePoint + 1) + ' 点') : base;
}
function drawDots(){
  ctrlDots.forEach(d => map.removeLayer(d));
  ctrlDots = [];
  if(selRoutePoint >= route.length) selRoutePoint = -1;
  route.forEach((p, i) => {
    const style = routeDotStyle(i);
    const d = L.circleMarker(p, Object.assign({
      bubblingMouseEvents: false,
      className: 'route-dot'
    }, style)).addTo(map);
    // Every control point says which one it is; after loading a saved route
    // the old UI only labelled the start, so point order was invisible.
    d.bindTooltip(i === 0 ? '起点 · 第 1 点' : ('第 ' + (i + 1) + ' 点'),
      {direction: 'top', offset: [0, -5], className: 'pt-tip'});
    d.on('click', ev => {
      if(ev && ev.originalEvent) L.DomEvent.stopPropagation(ev.originalEvent);
      selectRoutePoint(i);
    });
    d.on('contextmenu', ev => {
      if(ev && ev.originalEvent){
        L.DomEvent.stopPropagation(ev.originalEvent);
        L.DomEvent.preventDefault(ev.originalEvent);
      }
      routeClickGuardUntil = Date.now() + 300;
      deleteRoutePoint(i);
    });
    d.on('mousedown', ev => {
      if(ev && ev.originalEvent && ev.originalEvent.button !== 0) return;
      if(ev && ev.originalEvent) L.DomEvent.stopPropagation(ev.originalEvent);
      beginRoutePointDrag(i, ev);
    });
    ctrlDots.push(d);
  });
}
function selectRoutePoint(i){
  if(i < 0 || i >= route.length) return;
  selRoutePoint = i;
  paintDots();
  if($('#ptCount')) $('#ptCount').textContent = routeCountText();
  toast('已选中第 ' + (i + 1) + ' 点；拖拽移动，Delete 删除，Esc 取消');
}
function deleteRoutePoint(i){
  if(i < 0 || i >= route.length) return;
  pushRouteHistory('删除第 ' + (i + 1) + ' 点');
  route.splice(i, 1);
  selRoutePoint = -1;
  redraw();
  if(!pen) scheduleRouteFinalize(1200, 'delete');
  toast('已删除第 ' + (i + 1) + ' 点；Ctrl+Z 撤销');
}
function beginRoutePointDrag(i, ev){
  if(i < 0 || i >= route.length) return;
  selRoutePoint = i;
  paintDots();
  dragPointIndex = i;
  dragPointSnapshot = routeSnapshot();
  dragPointMoved = false;
  routeClickGuardUntil = Date.now() + 300;
  map.dragging.disable();
  document.body.classList.add('dragging-route-point');
  if(ev && ev.originalEvent) ev.originalEvent.preventDefault();
}
document.addEventListener('mousemove', ev => {
  if(dragPointIndex < 0) return;
  const box = map.getContainer().getBoundingClientRect();
  if(ev.clientX < box.left || ev.clientX > box.right ||
     ev.clientY < box.top || ev.clientY > box.bottom) return;
  const ll = map.mouseEventToLatLng(ev);
  route[dragPointIndex] = [+ll.lat, +ll.lng];
  dragPointMoved = true;
  const dot = ctrlDots[dragPointIndex];
  if(dot) dot.setLatLng(ll);
  const path = displayPath();
  hi.setLatLngs(path);
  ink.setLatLngs(path);
  ev.preventDefault();
});
document.addEventListener('mouseup', () => {
  if(dragPointIndex < 0) return;
  const i = dragPointIndex;
  dragPointIndex = -1;
  map.dragging.enable();
  document.body.classList.remove('dragging-route-point');
  if(dragPointMoved && dragPointSnapshot){
    routeClickGuardUntil = Date.now() + 300;
    pushRouteSnapshot(dragPointSnapshot, '移动第 ' + (i + 1) + ' 点');
    redraw();
    scheduleRouteFinalize(1500, 'drag');
    toast('已移动第 ' + (i + 1) + ' 点');
  }
  dragPointSnapshot = null;
  dragPointMoved = false;
});
function autoFillDistanceFromRoute(m){
  // Keep the target distance tied to the line until the operator edits it.
  // A closed line is one lap: 996 m must not round up to 1.00 km and become
  // two laps, so the lap calculation below also uses a small tolerance.
  if(distanceManual || !(m > 0)) return false;
  const value = Math.min(RUN_LIMITS.km.max,
    Math.max(RUN_LIMITS.km.min, Math.round(m / 10) / 100));
  const el = $('#inKm');
  if(!el) return false;
  const current = +el.value;
  if(isFinite(current) && Math.abs(current - value) < 1e-9) return false;
  el.value = String(Math.round(value * 100) / 100);
  refreshParams();
  updateRunDateLabel();
  // The checkpoint endpoint is distance-scoped, but drawing changes the
  // target every 10 m and each call returns a different random five-point
  // set (3 calls / 5 min).  Pull at most once per 5 min, after the line has
  // settled; submit reuses whatever set is on the map.
  schedulePointsForDistance(true);
  return true;
}
function redraw(){
  const path = displayPath();
  hi.setLatLngs(path);
  ink.setLatLngs(path);
  drawDots();

  if(!path.length){
    // Reset the “manual target” flag only when a route actually disappears;
    // setting a target before drawing must not be immediately overwritten.
    if(lastRouteLength) distanceManual = false;
    lastRouteLength = 0;
    // No line means no target.  A manually entered distance is kept; the
    // auto-filled value from the deleted line goes back to 0.
    const kmEl = $('#inKm');
    if(kmEl && !distanceManual && +kmEl.value !== 0){
      kmEl.value = '0';
      refreshParams();
    }
    if(map.hasLayer(start)) map.removeLayer(start);
    $('#routeKm').innerHTML = '0.00<small>km</small>';
    $('#routeNote').innerHTML = '<b>·</b> 点击地图开始';
    $('#ptCount').textContent = '0 个控制点';
    $('#runSummary').textContent = '单圈 0.00 km · 等待画线';
    $('#mapNoteKm').textContent = '0.00 km';
    $('#mapNoteMeta').textContent = '等待画线';
    refreshPointsDistanceState();
    if(typeof scheduleModelPreview === 'function') scheduleModelPreview();
    return;
  }

  if(!map.hasLayer(start)) start.addTo(map);
  start.setLatLng(path[0]);

  const m = pathLength(path);
  lastRouteLength = route.length;
  autoFillDistanceFromRoute(m);
  const km = m / 1000;
  $('#routeKm').innerHTML = km.toFixed(2) + '<small>km</small>';
  const target = (+$('#inKm').value || 0) * 1000;
  const closed = routeClosed && route.length >= 3;
  // 0.5% tolerance: a target equal to one lap must stay one lap after the
  // route length was rounded to centimetres for the input field.
  const laps = closed && m > 5 && target > 0
    ? Math.max(1, Math.ceil(target / m - 0.005)) : 0;
  const cov = typeof computeCoverage === 'function' ? computeCoverage() : null;
  let status;
  if(!routeClosed) status = '开放路线 · 再点可以继续';
  else if(route.length < 3) status = '再点一个点形成闭合';
  else if(m < 80) status = '路线太短（<80m），画大一点';
  else if(!(target > 0)) status = '闭合路线 · 先画完或填写目标距离';
  else status = '闭合路线 · 打卡点 ' + (cov && cov.total ? (cov.passed + '/' + cov.total)
    : (LIVE.pointsEmpty ? '无（需确认）' : '未加载')) + ' · ' + laps + ' 圈';
  $('#routeNote').innerHTML = closed && m >= 80
    ? '<b>✓</b> ' + status
    : '<b>·</b> ' + status;
  $('#ptCount').textContent = routeCountText();
  const pace = (+$('#inPace').value || 6);
  const totalMin = (target / 1000) * pace;
  $('#runSummary').textContent = '单圈 ' + km.toFixed(2) + ' km · ' + _paceText(pace) +
    ' · ' + (closed ? ('闭合 · 打卡 ' + (cov && cov.total ? (cov.passed + '/' + cov.total) : '未加载')) : '开放路线');

  $('#mapNoteKm').textContent = km.toFixed(2) + ' km';
  $('#mapNoteMeta').textContent = closed && m >= 80 && target > 0
    ? '闭合 · ×' + laps + ' 圈 · ' + durationText(totalMin)
    : status;
  if(typeof scheduleModelPreview === 'function') scheduleModelPreview();
}

map.on('click', e => {
  if(!pen) return;
  // A control-point click/drag must never fall through to "add a new point".
  if(Date.now() < routeClickGuardUntil) return;
  const p = [e.latlng.lat, e.latlng.lng];
  if(route.length && distM(route[route.length - 1], p) < 6) return;
  pushRouteHistory('添加第 ' + (route.length + 1) + ' 点');
  selRoutePoint = -1;
  route.push(p);
  redraw();
  toast('已加点：' + route.length + ' 个；右键删点');
});
map.on('contextmenu', e => {
  if(!pen) return;
  let best = -1, bd = 1e9;
  route.forEach((p, i) => {
    const sp = map.latLngToContainerPoint(p);
    const d = sp.distanceTo(e.containerPoint);
    if(d < bd){ bd = d; best = i; }
  });
  if(best >= 0 && bd < 26) deleteRoutePoint(best);
});

$('#undoTool').addEventListener('click', undoRoute);
$('#clearBtn').addEventListener('click', () => {
  if(route.length >= 3 && !confirm('清空当前路线？')) return;
  if(route.length) pushRouteHistory('清空路线');
  clearTimeout(_routeFinalTimer);
  route = [];
  selRoutePoint = -1;
  redraw();
  toast('已清空：从你的第一个落点开始画');
});
$('#smoothToggle').addEventListener('click', () => {
  smoothOn = !smoothOn;
  $('#smoothToggle').classList.toggle('on', smoothOn);
  $('#smoothToggle').setAttribute('aria-pressed', String(smoothOn));
  redraw();
  toast(smoothOn ? '平滑曲线已开' : '平滑已关，按折线显示');
});
$('#closeToggle').addEventListener('click', () => {
  if(route.length) pushRouteHistory(routeClosed ? '关闭闭合' : '打开闭合');
  setCloseUI(!routeClosed);
  redraw();
  refreshChecks();
  if(typeof scheduleModelPreview === 'function') scheduleModelPreview();
  if(!pen) scheduleRouteFinalize(600, 'closed');
  toast(routeClosed
    ? '闭合已开：3 个点也会首尾相连；关掉可画开放路线'
    : '闭合已关：保留起点到终点');
});
$('#snapTool').addEventListener('click', () => $('#snapBtn').click());

redraw();
syncRouteHistoryButtons();

/* ---------- base layer toggle (satellite default) ---------- */
let sat = true;
$('#layerTool').addEventListener('click', () => {
  sat = !sat;
  if(sat){ map.removeLayer(street); satellite.addTo(map); toast('卫星底图'); }
  else   { map.removeLayer(satellite); street.addTo(map); toast('街道底图'); }
});

/* ---------- blocks collapse ---------- */
document.querySelectorAll('[data-toggle]').forEach(head => {
  head.addEventListener('click', ev => {
    if(ev.target.closest('button, a, input, label')) return;
    head.closest('.block').classList.toggle('collapsed');
  });
});

/* ---------- run panel state ---------- */
const dock = $('#dock'), dockBtn = $('#dockBtn'), dockText = $('#dockText');
let modelConfigured = false;

function _paceText(p){
  p = +p;
  if(!p || !isFinite(p)) return '—';
  // Round once, in seconds.  Flooring minutes then rounding the remainder
  // produced 5'60" for values such as 5.999.
  const total = Math.max(0, Math.round(p * 60));
  return Math.floor(total / 60) + "'" + String(total % 60).padStart(2, '0') + '"';
}
function durationText(totalMin){
  const total = Math.max(0, Math.round((+totalMin || 0) * 60));
  return Math.floor(total / 60) + "'" + String(total % 60).padStart(2, '0') + '"';
}
function kmText(v){
  return (+v || 0).toFixed(2).replace(/\.?0+$/, '');
}
function splitFmt(sec){
  sec = Math.max(0, Math.round(+sec || 0));
  return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
}
function splitParse(text){
  text = String(text || '').trim().replace(/[’'"]/g, '');
  let m = text.match(/^(\d+)\s*[:：]\s*(\d{1,2})$/);
  if(m) return (+m[1]) * 60 + (+m[2]);
  m = text.match(/^(\d+(?:\.\d+)?)$/);   // decimal minutes, like the pace field
  if(m) return Math.round((+m[1]) * 60);
  return null;
}
const RUN_LIMITS = {
  km: {min: 0.1, max: 42.2, label: '目标距离', unit: 'km'},
  pace: {min: 2, max: 20, label: '配速', unit: 'min/km'},
  cadence: {min: 60, max: 260, label: '步频', unit: 'spm', integer: true},
  altitude: {min: -100, max: 5000, label: '海拔基准', unit: 'm'}
};
let altProfile = null;
let altTemplateLabel = '';
let altAdjustOpen = false;
function readAltitudeOpts(){
  const mode = ($('#altMode') && $('#altMode').value) || 'auto';
  const loRaw = ($('#altMin') && $('#altMin').value || '').trim();
  const hiRaw = ($('#altMax') && $('#altMax').value || '').trim();
  const usesRange = mode === 'range' || mode === 'template';
  const range = usesRange && (loRaw !== '' || hiRaw !== '')
    ? [+loRaw, +hiRaw] : null;
  return {
    mode: mode,
    range: range,
    profile: mode === 'template' ? altProfile : null
  };
}
function altitudeArgs(p){
  return {
    altitude_mode: p.altitudeMode || 'auto',
    altitude_range: p.altitudeRange || null,
    altitude_profile: p.altitudeProfile || null
  };
}
function readRunParams(){
  const alt = readAltitudeOpts();
  return {
    km: +$('#inKm').value,
    pace: +$('#inPace').value,
    cadence: +$('#inCad').value,
    altitude: +$('#inAlt').value,
    altitudeMode: alt.mode,
    altitudeRange: alt.range,
    altitudeProfile: alt.profile
  };
}
function validateRunParams(p){
  p = p || readRunParams();
  for(const key of Object.keys(RUN_LIMITS)){
    const spec = RUN_LIMITS[key], v = p[key];
    if(!isFinite(v)) return '请填写有效的' + spec.label;
    // The target starts at 0 and is filled from the hand-drawn line.  A 0
    // target is "not drawn yet", not an out-of-range number.
    if(key === 'km' && !(v > 0)){
      return '先手绘一条路线，或在参数页填写目标距离';
    }
    if(spec.integer && Math.abs(v - Math.round(v)) > 1e-9){
      return spec.label + '需要是整数';
    }
    if(v < spec.min || v > spec.max){
      return spec.label + '需要在 ' + spec.min + '–' + spec.max + ' ' +
        spec.unit + ' 之间';
    }
  }
  if(p.altitudeRange){
    const lo = +p.altitudeRange[0], hi = +p.altitudeRange[1];
    if(!isFinite(lo) || !isFinite(hi)){
      return '海拔区间需要两个数字';
    }
    if(lo > hi) return '最低海拔不能大于最高海拔';
    if(lo < RUN_LIMITS.altitude.min || hi > RUN_LIMITS.altitude.max){
      return '海拔区间需要在 ' + RUN_LIMITS.altitude.min + '–' +
        RUN_LIMITS.altitude.max + ' m 之间';
    }
  }
  if(p.altitudeMode === 'range' && !p.altitudeRange){
    return '区间模式需要填写最低和最高海拔';
  }
  if(p.altitudeMode === 'template' &&
     !(Array.isArray(p.altitudeProfile) && p.altitudeProfile.length >= 2)){
    return '模板模式需要先选择一个 GPX/JSON 文件';
  }
  return '';
}
const ALT_HINTS = {
  auto: '以「基准海拔」为中心轻微波动，像真实气压计。',
  fixed: '全程固定在「固定海拔」，爬升为 0。',
  range: '按最低–最高海拔合成坡形；基准海拔不参与。',
  template: '本机 GPX/JSON，不上传；区间可留空。'
};
function updateAltSummary(){
  const el = $('#altSummary');
  if(!el) return;
  const mode = ($('#altMode') && $('#altMode').value) || 'auto';
  const base = +($('#inAlt') && $('#inAlt').value);
  const safeBase = isFinite(base) ? base : 25;
  if(mode === 'auto'){
    el.textContent = '自动 · 基准 ' + safeBase + ' m';
  }else if(mode === 'fixed'){
    el.textContent = '固定 ' + safeBase + ' m · 零爬升';
  }else if(mode === 'range'){
    const lo = +($('#altMin') && $('#altMin').value);
    const hi = +($('#altMax') && $('#altMax').value);
    el.textContent = (isFinite(lo) && isFinite(hi))
      ? ('区间 ' + lo + '–' + hi + ' m') : '区间 · 待填写';
  }else{
    el.textContent = altTemplateLabel
      ? ('模板 · ' + altTemplateLabel) : '模板 · 未选择文件';
  }
}
function syncAltThumb(){
  const wrap = document.querySelector('.alt-modes');
  const thumb = $('#altThumb');
  if(!wrap || !thumb) return;
  const active = wrap.querySelector('button.on');
  if(!active){ thumb.style.width = '0px'; return; }
  const width = active.offsetWidth;
  if(!width) return;             // the params tab is not laid out yet
  thumb.style.width = width + 'px';
  thumb.style.transform = 'translateX(' + (active.offsetLeft - 3) + 'px)';
}
function altModeChanged(){
  const mode = ($('#altMode') && $('#altMode').value) || 'auto';
  const usesBase = mode === 'auto' || mode === 'fixed';
  document.querySelectorAll('[data-alt-mode]').forEach(b =>
    b.classList.toggle('on', b.dataset.altMode === mode));
  syncAltThumb();
  requestAnimationFrame(syncAltThumb);
  if($('#inAltLabelText')){
    $('#inAltLabelText').textContent = mode === 'fixed' ? '固定海拔' : '基准海拔';
  }
  if($('#inAltUse')){
    $('#inAltUse').textContent = usesBase
      ? (mode === 'fixed' ? '固定模式使用' : '自动模式使用')
      : '本模式不用';
  }
  if($('#inAlt')){
    $('#inAlt').readOnly = !usesBase;
    $('#inAlt').setAttribute('aria-disabled', usesBase ? 'false' : 'true');
  }
  if($('#altBaseWrap')) $('#altBaseWrap').hidden = !(usesBase && (mode === 'fixed' || altAdjustOpen));
  if($('#altAdjustToggle')){
    $('#altAdjustToggle').hidden = !usesBase;
    $('#altAdjustToggle').textContent = altAdjustOpen ? '收起' : '调整';
    $('#altAdjustToggle').setAttribute('aria-expanded', altAdjustOpen ? 'true' : 'false');
  }
  if($('#altRangeWrap')) $('#altRangeWrap').hidden = !(mode === 'range' || mode === 'template');
  if($('#altTemplateWrap')) $('#altTemplateWrap').hidden = mode !== 'template';
  if(mode === 'range' && $('#altMin') && $('#altMax') &&
     !$('#altMin').value && !$('#altMax').value){
    const base = +$('#inAlt').value || 25;
    $('#altMin').value = Math.max(-100, Math.round(base - 10));
    $('#altMax').value = Math.min(5000, Math.round(base + 25));
  }
  if($('#altHint')) $('#altHint').textContent = ALT_HINTS[mode] || ALT_HINTS.auto;
  if($('#altRangeNote')) $('#altRangeNote').textContent =
    mode === 'range' ? '必填 · 最低 ≤ 最高' : '可留空 · 沿用模板海拔';
  updateAltSummary();
  scheduleModelPreview();
  refreshChecks();
}
async function loadAltitudeTemplate(file){
  if(!file) return;
  if(file.size > 8 * 1024 * 1024){
    toast('模板超过 8 MiB，请先裁剪');
    return;
  }
  const nameEl = $('#altTemplateName');
  if(nameEl) nameEl.textContent = '解析中…';
  let text = '';
  try{ text = await file.text(); }
  catch(e){ toast('读取文件失败：' + e); if(nameEl) nameEl.textContent = '未选择'; return; }
  const j = await liveApi('real_altitude_template', {text: text}, 30000);
  if(!liveOk(j)){
    toast(j.message || '模板解析失败');
    if(nameEl) nameEl.textContent = '解析失败';
    return;
  }
  const d = j.data || {};
  altProfile = (d.profile || []).map(Number).filter(v => isFinite(v));
  altTemplateLabel = file.name;
  if(nameEl){
    nameEl.textContent = file.name + ' · ' + (d.points || altProfile.length) +
      ' 点 · ' + d.min + '–' + d.max + ' m';
  }
  if($('#altMode')) $('#altMode').value = 'template';
  altModeChanged();
  toast('海拔模板已载入：' + (d.points || altProfile.length) + ' 点');
}
function refreshParams(){
  const km = +$('#inKm').value || 0;
  const pace = +$('#inPace').value || 0;
  const cad = +$('#inCad').value || 0;
  const totalMin = km * pace;
  const total = durationText(totalMin);
  const speed = pace > 0 ? 1000 / (pace * 60) : 0;
  const stride = cad > 0 ? speed / (cad / 60) : 0;
  $('#heroKm').textContent = kmText(km);
  $('#heroPace').textContent = _paceText(pace);
  $('#heroTime').textContent = total;
  const kcal = Math.round(km * 75.6);
  $('#heroKcal').textContent = kcal + ' kcal';
  $('#sumTime').textContent = total;
  $('#sumKcal').textContent = kcal;
  $('#sumSteps').textContent = Math.round(cad * totalMin).toLocaleString('en-US');
  $('#sumStride').textContent = (stride > 0 ? stride.toFixed(2) : '—') + ' m';
  $('#paceScaleText').textContent = _paceText(pace) + ' /km';
  const marker = $('#paceMarker');
  if(marker) marker.style.left = Math.max(0, Math.min(100, (pace - 4) / 6 * 100)) + '%';
  $('#goalLine1').textContent = '目标 ' + kmText(km) + ' km · ' + _paceText(pace);
  $('#goalLine2').textContent = '预计 ' + total + ' · ' + kcal + ' kcal';
  $('#paceSpeedHint').textContent = '配速 ' + _paceText(pace) + ' ≈ ' +
    (speed * 3.6).toFixed(1) + ' km/h；模型会用这些值做配速/步频/步幅一致性检查。';
  $('#genPts').textContent = Math.round(totalMin * 60).toLocaleString('en-US');
  $('#genSteps').textContent = Math.round(cad * totalMin).toLocaleString('en-US');
  const zone = $('#paceZone'), zoneNote = $('#paceScaleNote');
  if(zone){
    let liveRules = null;
    try{ liveRules = (LIVE.rules && LIVE.rules.configured) ? LIVE.rules.rules : null; }catch(e){}
    if(liveRules){
      const lo = Math.max(4, Math.min(10, +liveRules.pace_min || 4));
      const hi = Math.max(4, Math.min(10, +liveRules.pace_max || 10));
      zone.style.display = 'block';
      zone.style.left = ((lo - 4) / 6 * 100) + '%';
      zone.style.width = Math.max(0, (hi - lo) / 6 * 100) + '%';
      if(zoneNote) zoneNote.textContent = '绿色括号 = 学校允许范围；深色竖线 = 当前配速。';
    }else{
      zone.style.display = 'none';
      if(zoneNote) zoneNote.textContent = '配速尺：左快右慢；保存学校要求后显示允许范围。';
    }
  }
}
refreshParams();

/* ---------- per-kilometre splits: automatic by default ---------- */
function splitLabel(row, i){
  const dist = +row.km || 0;
  if(Math.abs(dist - 1) < 0.005) return '第 ' + (i + 1) + ' km';
  return '最后 ' + kmText(dist) + ' km';
}
function splitRowsForView(){
  return (splitsManual && splitRows && splitRows.length) ? splitRows : splitAutoRows;
}
function renderSplits(){
  const card = $('#splitsCard'), list = $('#splitsList');
  if(!card || !list) return;
  const rows = splitRowsForView();
  const manual = !!splitsManual;
  card.hidden = false;
  card.classList.toggle('auto', !manual);
  if($('#splitsMode')) $('#splitsMode').textContent = manual ? '手动' : '自动';
  if($('#splitsToggle')){
    $('#splitsToggle').textContent = manual ? '切回自动' : '手动微调';
    $('#splitsToggle').setAttribute('aria-pressed', manual ? 'true' : 'false');
  }
  if($('#splitAuto')) $('#splitAuto').hidden = manual;
  if($('#splitManual')) $('#splitManual').hidden = !manual;
  if($('#splitChips')){
    $('#splitChips').innerHTML = rows.length
      ? rows.map((row, i) => '<span class="split-chip">' + splitLabel(row, i) +
          ' <b>' + splitFmt(row.seconds) + '</b></span>').join('')
      : '<span class="mini">登录后按配速自动生成</span>';
  }
  if($('#splitTotal')){
    if(rows.length){
      const sumSec = rows.reduce((a, r) => a + (+r.seconds || 0), 0);
      const sumKm = rows.reduce((a, r) => a + (+r.km || 0), 0);
      $('#splitTotal').innerHTML = '合计 <b>' + kmText(sumKm) + ' km</b> · <b>' +
        splitFmt(sumSec) + '</b> · 平均 ' +
        _paceText(sumSec / (60 * Math.max(0.001, sumKm))) + '/km';
    }else{
      $('#splitTotal').textContent = '合计 — · 平均 —';
    }
  }
  if(!manual){ list.innerHTML = ''; return; }
  if(!rows.length){ list.innerHTML = '<div class="mini">还没有分段；点「切回自动」恢复生成。</div>'; return; }
  list.innerHTML = rows.map((row, i) => {
    const dist = +row.km || 0;
    const pace = dist > 0 ? (+row.seconds || 0) / (60 * dist) : 0;
    return '<div class="split-row">' +
      '<span class="split-km">' + splitLabel(row, i) + '</span>' +
      '<input class="split-input" data-split="' + i + '" value="' + splitFmt(row.seconds) +
        '" inputmode="text" aria-label="' + splitLabel(row, i) + '用时">' +
      '<button type="button" class="split-step" data-split-step="-5" data-split="' + i + '" aria-label="减 5 秒">−</button>' +
      '<button type="button" class="split-step" data-split-step="5" data-split="' + i + '" aria-label="加 5 秒">＋</button>' +
      '<span class="split-hint">' + (dist >= 0.05 ? _paceText(pace) + '/km' : '') + '</span>' +
      '</div>';
  }).join('');
  const sumSec = rows.reduce((a, r) => a + (+r.seconds || 0), 0);
  const sumKm = rows.reduce((a, r) => a + (+r.km || 0), 0);
  list.innerHTML += '<div class="split-total">合计 <b>' + kmText(sumKm) + ' km</b> · <b>' +
    splitFmt(sumSec) + '</b> · 平均 ' +
    _paceText(sumSec / (60 * Math.max(0.001, sumKm))) + '/km</div>';
}

function setSplitsManual(on){
  splitsManual = !!on;
  if(splitsManual){
    if(!splitRows || !splitRows.length){
      splitRows = (splitAutoRows || []).map(r => ({km: +r.km, seconds: Math.round(+r.seconds || 0)}));
      // Toggled before the first preview came back: fetch the rows instead of
      // showing an empty manual editor until the next parameter change.
      if(!splitRows.length) scheduleSplitsPreview();
    }
  }else{
    splitRows = null;
  }
  renderSplits();
  if(!splitsManual) scheduleSplitsPreview();
}
function applySplitsToPace(){
  const km = +$('#inKm').value || 0;
  if(!splitRows || !splitRows.length || !km) return;
  const total = splitRows.reduce((a, r) => a + (+r.seconds || 0), 0);
  const pace = total / 60 / km;
  if(isFinite(pace) && pace >= RUN_LIMITS.pace.min && pace <= RUN_LIMITS.pace.max){
    $('#inPace').value = pace.toFixed(2);
  }
  refreshParams();
  renderSplits();
  updateRunDateLabel();
  if(typeof refreshChecks === 'function') refreshChecks();
  scheduleModelPreview();
}

function scheduleSplitsPreview(){
  clearTimeout(_splitsTimer);
  _splitsTimer = setTimeout(refreshSplits, 260);
}

async function refreshSplits(){
  if(!hasSession() || LIVE.submitting) return;
  const params = readRunParams();
  if(validateRunParams(params)) return;
  const j = await liveApi('preview_route', {
    km: params.km, pace: params.pace, cadence: params.cadence,
    waypoints: route && route.length >= 2 ? route : null,
    closed: !!routeClosed,
    km_times: splitsManual && splitRows ? splitRows.map(r => Math.round(+r.seconds || 0)) : null
  });
  if(liveOk(j) && j.data && Array.isArray(j.data.splits)){
    splitAutoRows = j.data.splits.map(s => ({km: +s.km,
                                             seconds: Math.round(+s.seconds || 0)}));
    if(!splitsManual) splitRows = null;
    else if(!splitRows || !splitRows.length){
      splitRows = splitAutoRows.map(r => ({km: +r.km, seconds: +r.seconds}));
    }
    renderSplits();
  }
}

/* ---------- run panel tabs / collapse ---------- */
function syncRunTabThumb(){
  const wrap = document.querySelector('.run-tabs');
  const thumb = $('#rtabThumb');
  if(!wrap || !thumb) return;
  const active = wrap.querySelector('.rtab.active');
  if(!active){ thumb.style.width = '0px'; return; }
  const width = active.offsetWidth;
  if(!width){ thumb.style.width = '0px'; return; }
  // The highlighter is inset 8% on each side, matching the old painted mark.
  const inner = Math.max(0, width * 0.84);
  thumb.style.width = inner + 'px';
  thumb.style.transform =
    'translateX(' + (active.offsetLeft + (width - inner) / 2) + 'px)';
}
function switchRunTab(name){
  // Leaving the route tab means the operator is done drawing/editing; this
  // is the natural point to fetch the set for the final line length.
  if(name !== 'route') scheduleRouteFinalize(150, 'tab:' + name);
  if(name !== 'params') closeDatePop();
  document.querySelectorAll('.rtab').forEach(t => {
    const on = t.dataset.rtab === name;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.rsec').forEach(s =>
    s.classList.toggle('active', s.id === 'rsec-' + name));
  requestAnimationFrame(syncRunTabThumb);
  if(name === 'params') requestAnimationFrame(syncAltThumb);
  const body = document.querySelector('.run-body');
  if(body) body.scrollTop = 0;
}
document.querySelectorAll('.rtab').forEach(t => {
  t.addEventListener('click', () => switchRunTab(t.dataset.rtab));
});
if($('#rtabThumb')){
  window.addEventListener('resize', () => requestAnimationFrame(syncRunTabThumb));
  requestAnimationFrame(syncRunTabThumb);
  if(document.fonts && document.fonts.ready){
    document.fonts.ready.then(() => requestAnimationFrame(syncRunTabThumb));
  }
}
if($('#navThumb')){
  window.addEventListener('resize', () => requestAnimationFrame(syncNavThumb));
  requestAnimationFrame(syncNavThumb);
  if(document.fonts && document.fonts.ready){
    document.fonts.ready.then(() => requestAnimationFrame(syncNavThumb));
  }
}
['inKm', 'inPace', 'inCad', 'inAlt'].forEach(id => {
  $('#' + id).addEventListener('input', () => {
    if(id === 'inKm') distanceManual = true;
    refreshParams();
    if(id !== 'inAlt') redraw();
    if(id === 'inKm') schedulePointsForDistance();
    if(modelConfigured) refreshModel();
    scheduleModelPreview();
  });
});
document.querySelectorAll('[data-km]').forEach(b => {
  b.addEventListener('click', () => {
    distanceManual = true;
    $('#inKm').value = b.dataset.km;
    refreshParams(); redraw(); updateRunDateLabel();
    schedulePointsForDistance();
    if(modelConfigured) refreshModel();
    scheduleModelPreview();
  });
});
document.querySelectorAll('[data-pace]').forEach(b => {
  b.addEventListener('click', () => {
    $('#inPace').value = b.dataset.pace;
    refreshParams(); redraw(); updateRunDateLabel();
    if(modelConfigured) refreshModel();
    scheduleModelPreview();
  });
});
$('#randomParams').addEventListener('click', () => {
  const kms = [2.0, 2.5, 3.0, 3.5, 4.0];
  const paces = [5.8, 6.0, 6.3, 6.6, 7.0];
  const cads = [140, 145, 150, 155, 160];
  const pick = a => a[Math.floor(Math.random() * a.length)];
  distanceManual = true;
  $('#inKm').value = pick(kms);
  $('#inPace').value = pick(paces);
  $('#inCad').value = pick(cads);
  refreshParams(); redraw(); updateRunDateLabel();
  schedulePointsForDistance();
  if(modelConfigured) refreshModel();
  scheduleModelPreview();
  toast('随机档位已填入');
});

let currentMode = 'draw';
const MODE_HELP = {
  draw: '手绘：点地图加点；第一个落点为起点；默认不闭合，需要绕圈时再打开「闭合」。',
  auto: '自动：仍在开发中，不建议使用；请改用手绘，或导入 GPX / 使用田径场。',
  file: '导入：读取 GPX / GeoJSON / JSON / CSV（手表或别的 App 导出的轨迹）。',
  stadium: '田径场：生成 200/300/400m 标准跑道；提交时按目标距离刷圈。'
};
const ROUTE_HINT = {
  draw: '点左上角画笔，在地图上<b>单击加点</b>；右键删点。默认不闭合，需要绕圈时再打开「闭合」。<br>点位和围栏来自登录后的真实接口；没有 token 时不会生成假点位。',
  auto: '<b>自动功能仍在开发中，不建议使用。</b>请切到「手绘」逐点画，或导入 GPX / 使用田径场。<br>当前自动模式只是按已加载点位连成一圈，路线质量不稳定。',
  file: '导入模式：读取 GPX / GeoJSON / JSON / CSV；手表或别的 App 导出的轨迹。文件只在本机解析。',
  stadium: '田径场模式：选 200/300/400m 生成标准跑道；提交时按目标距离刷圈。'
};
function autoGenerate(){
  const left = livePoints.map(p => [p.lat, p.lng]);
  if(left.length < 2){ toast('自动仍在开发中；请先登录刷新点位，或改用手绘'); return; }
  const out = [];
  let cur = left.shift();
  out.push(cur);
  while(left.length){
    let bi = 0, bd = 1e9;
    left.forEach((p, i) => {
      const d = distM(cur, p);
      if(d < bd){ bd = d; bi = i; }
    });
    cur = left.splice(bi, 1)[0];
    out.push(cur);
  }
  if(out.length) pushRouteHistory('生成自动路线');
  selRoutePoint = -1;
  route = out;
  setCloseUI(true);
  redraw();
  scheduleRouteFinalize(300, 'auto');
  liveLog('自动路线仍在开发中', '仅作试验；建议改用手绘或导入 GPX', 'warn');
  toast('自动仍在开发中，不建议使用；已按 ' + livePoints.length + ' 个点位生成试验路线');
}
function stadiumGenerate(){
  const size = +($('#stadiumSize').value || 400);
  let cx = CENTER[0], cy = CENTER[1];
  if(LIVE.session && LIVE.session.lat && LIVE.session.lon){
    cx = +LIVE.session.lat; cy = +LIVE.session.lon;
  }else if(fenceBounds && fenceBounds.isValid()){
    const c = fenceBounds.getCenter(); cx = c.lat; cy = c.lng;
  }else{
    const c = map.getCenter(); cx = c.lat; cy = c.lng;
  }
  const rLat = size / (2 * Math.PI) / 110540;
  const rLng = size / (2 * Math.PI) /
    (111320 * Math.cos(cx * Math.PI / 180));
  const pts = [];
  for(let i = 0; i < 20; i++){
    const a = 2 * Math.PI * i / 20;
    pts.push([cx + rLat * Math.sin(a), cy + rLng * Math.cos(a)]);
  }
  pushRouteHistory('生成田径场路线');
  selRoutePoint = -1;
  route = pts;
  setCloseUI(true);
  redraw();
  scheduleRouteFinalize(300, 'stadium');
  toast('田径场 ' + size + 'm：生成一圈，提交时按目标距离刷圈');
}
function setMode(mode){
  const prev = currentMode;
  currentMode = mode;
  document.querySelectorAll('.mode-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.mode === mode));
  $('#modeHelp').textContent = MODE_HELP[mode] || '';
  $('#routeHint').innerHTML = ROUTE_HINT[mode] || '';
  $('#autoGenerate').style.display = mode === 'auto' ? 'inline-flex' : 'none';
  $('#importFile').style.display = mode === 'file' ? 'inline-flex' : 'none';
  $('#stadiumSize').style.display = mode === 'stadium' ? 'inline-block' : 'none';
  $('#stadiumGenerate').style.display = mode === 'stadium' ? 'inline-flex' : 'none';
  if(mode === 'draw'){
    if(prev === 'draw'){ setPen(true); return; }
    // Do not wipe an auto/imported/stadium route just because the operator
    // switched tabs; 清空 is the explicit way to start over.
    setPen(true);
    if(!route.length) setCloseUI(false);
    redraw();
    toast('手绘：可继续编辑当前路线；要重画先点「清空」');
  }else if(mode === 'auto'){
    setPen(false);
    autoGenerate();
  }else{
    setPen(false);
  }
}
document.querySelectorAll('.mode-tab').forEach(t => {
  t.addEventListener('click', () => setMode(t.dataset.mode));
});
$('#autoGenerate').addEventListener('click', autoGenerate);
function parseRouteText(text, name){
  const out = [];
  const push = (a, b) => {
    let lat = +a, lng = +b;
    if(!isFinite(lat) || !isFinite(lng)) return;
    if(Math.abs(lat) > 90 && Math.abs(lng) <= 90){ const t = lat; lat = lng; lng = t; }
    if(Math.abs(lat) <= 90 && Math.abs(lng) <= 180) out.push([lat, lng]);
  };
  const lower = String(name || '').toLowerCase();
  if(lower.endsWith('.gpx') || text.trim().startsWith('<')){
    try{
      const doc = new DOMParser().parseFromString(text, 'application/xml');
      doc.querySelectorAll('trkpt, rtept').forEach(el => push(el.getAttribute('lat'), el.getAttribute('lon')));
    }catch(e){}
  }
  if(out.length < 2 && (lower.endsWith('.json') || lower.endsWith('.geojson') || text.trim().startsWith('{') || text.trim().startsWith('['))){
    try{
      const obj = JSON.parse(text);
      const walk = v => {
        if(!v) return;
        if(Array.isArray(v)){
          if(v.length >= 2 && typeof v[0] === 'number' && typeof v[1] === 'number') push(v[0], v[1]);
          else v.forEach(walk);
        }else if(typeof v === 'object'){
          if(v.type === 'Feature') walk(v.geometry);
          else if(v.type === 'FeatureCollection') walk(v.features);
          else if(v.type === 'GeometryCollection') walk(v.geometries);
          else if(v.coordinates) walk(v.coordinates);
          else if(v.lat != null && (v.lng != null || v.lon != null)) push(v.lat, v.lng ?? v.lon);
        }
      };
      walk(obj);
    }catch(e){}
  }
  if(out.length < 2){
    text.split(/\r?\n/).forEach(line => {
      const m = line.match(/[-+]?\d+(?:\.\d+)?/g);
      if(m && m.length >= 2) push(m[0], m[1]);
    });
  }
  return out;
}
function capRoutePoints(pts, max){
  max = max || 2000;
  if(!Array.isArray(pts) || pts.length <= max) return pts || [];
  const step = Math.ceil(pts.length / max);
  const out = pts.filter((_, i) => i % step === 0);
  if(out[out.length - 1] !== pts[pts.length - 1]) out.push(pts[pts.length - 1]);
  return out;
}
function saveBlob(blob, filename){
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  // The anchor has to be in the document for some browsers, and revoking the
  // object URL in the same tick can cancel the download.
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function downloadGpx(name, points){
  const pts = (points || []).map(p => Array.isArray(p)
    ? [+p[0], +p[1]] : [+p.lat, +(p.lng != null ? p.lng : p.lon)])
    .filter(p => isFinite(p[0]) && isFinite(p[1]));
  if(pts.length < 2){ toast('没有可导出的轨迹点'); return; }
  const safe = String(name || 'haoxiangpao-route').replace(/[\\/:*?"<>|]+/g, '_').slice(0, 60);
  const body = pts.map(p => `      <trkpt lat="${p[0].toFixed(7)}" lon="${p[1].toFixed(7)}"></trkpt>`).join('\n');
  const gpx = `<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="好想跑" xmlns="http://www.topografix.com/GPX/1/1">\n  <trk><name>${esc(safe)}</name><trkseg>\n${body}\n  </trkseg></trk>\n</gpx>`;
  const blob = new Blob([gpx], {type: 'application/gpx+xml;charset=utf-8'});
  saveBlob(blob, safe + '.gpx');
}
function exportRouteGpx(r){
  if(!r){ toast('先选一条路线'); return; }
  downloadGpx(r.name || '路线', routeLine(r));
}
function importRouteToLibrary(){
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.gpx,.geojson,.json,.csv,.txt';
  inp.onchange = async () => {
    const f = inp.files && inp.files[0];
    if(!f) return;
    if(f.size > 5 * 1024 * 1024){ toast('文件太大（上限 5 MB）'); return; }
    let text = '';
    try{ text = await f.text(); }
    catch(e){ toast('读取文件失败'); return; }
    const parsed = parseRouteText(text, f.name);
    if(parsed.length < 2){ toast('没有解析到轨迹点'); return; }
    const line = capRoutePoints(parsed);
    const name = prompt('路线名称', f.name.replace(/\.[^.]+$/, ''));
    if(!name) return;
    const item = {name: name, type: 'file', line: line, closed: false,
                  saved_at: beijingTodayKey()};
    const ok = await persistRoutes(LIVE.routes.concat([item]));
    LIVE.selectedRoute = LIVE.routes.length - 1;
    renderRouteList(); selectRoute(LIVE.selectedRoute);
    routeSaveToast(ok, '已导入路线库');
  };
  inp.click();
}
function importRouteFile(){
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.gpx,.geojson,.json,.csv,.txt';
  inp.onchange = async () => {
    const f = inp.files && inp.files[0];
    if(!f) return;
    if(f.size > 5 * 1024 * 1024){ toast('文件太大（上限 5 MB）'); return; }
    let text = '';
    try{ text = await f.text(); }
    catch(e){ toast('读取文件失败'); return; }
    const parsed = parseRouteText(text, f.name);
    if(parsed.length < 2){ toast('没有解析到轨迹点'); return; }
    const pts = capRoutePoints(parsed);
    if(pts.length < parsed.length){
      liveLog('导入路线已抽稀', `${parsed.length} → ${pts.length} 点（避免生成过慢）`, 'warn');
    }
    pushRouteHistory('导入路线文件');
    selRoutePoint = -1;
    route = pts;
    setCloseUI(false);
    redraw();
    scheduleRouteFinalize(300, 'import');
    toast('已导入 ' + pts.length + ' 个点');
    liveLog('路线已导入', f.name + ' · ' + pts.length + ' 点', 'ok');
  };
  inp.click();
}
$('#importFile').addEventListener('click', importRouteFile);
$('#stadiumGenerate').addEventListener('click', stadiumGenerate);
$('#importQuick').addEventListener('click', () => setMode('file'));

$('#togglePts').addEventListener('click', () => {
  const list = $('#ptList');
  list.hidden = !list.hidden;
  $('#togglePts').textContent = list.hidden ? '列表' : '收起';
});
$('#zoomIn').addEventListener('click', () => map.zoomIn());
$('#zoomOut').addEventListener('click', () => map.zoomOut());
$('#undoBtn').addEventListener('click', () => $('#undoTool').click());

dockBtn.addEventListener('click', () => {
  if(typeof liveDockClick === 'function') liveDockClick();
});

/* ============================================================
   v0.1.8 P0–P2 feature pack: audit, error help, read-only verify,
   realism settings, presets, stats, FIT / diagnostics export,
   inspection and the local replay canvas.

   Rules for every function below:
   - one bridge call per click, never on page load and never on a timer;
   - a missing bridge method disables the control with a visible reason;
   - backend `message` is shown verbatim, never swallowed.
   ============================================================ */
const REALISM_DEFAULTS = {obs_key_mode: 'single', pause_profile: 'none',
                          gps_profile: 'urban', terrain_coupling: false};
function normaliseRealism(r){
  r = r || {};
  return {
    obs_key_mode: r.obs_key_mode === 'legacy' ? 'legacy' : 'single',
    pause_profile: r.pause_profile === 'auto' ? 'auto' : 'none',
    gps_profile: ['clean', 'tunnel'].includes(r.gps_profile)
      ? r.gps_profile : 'urban',
    terrain_coupling: !!r.terrain_coupling
  };
}
function realismNow(){
  return normaliseRealism(LIVE.realism || REALISM_DEFAULTS);
}
function setHint(el, text, tone){
  if(!el) return;
  el.textContent = text || '';
  el.className = (el.id === 'rulesStatus' || /^mini/.test(el.className))
    ? ('mini' + (tone ? ' ' + tone : ''))
    : ('hint' + (tone ? ' ' + tone : ''));
}
function jsonText(obj){
  try{ return JSON.stringify(obj, null, 1); }catch(e){ return String(obj); }
}

/* ---------------- P0: OBS key + realism profile (settings page) ---------- */
async function loadRealism(){
  if(!$('#realismStatus') || LIVE.realismLoading) return;
  LIVE.realismLoading = true;
  try{ await loadRealismInner(); }
  finally{
    LIVE.realismLoading = false;
    if(typeof updateRunDateLabel === 'function') updateRunDateLabel();
  }
}
async function loadRealismInner(){
  const j = await liveMaybe('real_obs_key_mode', {}, 10000);
  if(bridgeMissing(j)){
    LIVE.realismError = bridgeMessage(j);
    $('#realismStatus').textContent = '未接入';
    $('#realismStatus').className = 'mini warn';
    $('#setRealismSave').disabled = true;
    $('#setRealismSave').title = LIVE.realismError;
    $('#setRealismHint').textContent =
      LIVE.realismError + '。当前按推荐默认值显示。';
    applyRealismForm(realismNow());
    return;
  }
  if(!liveOk(j)){
    LIVE.realismError = bridgeMessage(j);
    $('#realismStatus').textContent = '读取失败';
    $('#realismStatus').className = 'mini warn';
    $('#setRealismHint').textContent = LIVE.realismError +
      '；可以稍后重试，或保存当前显示值覆盖。';
    if($('#setRealismSave')){
      $('#setRealismSave').disabled = false;
      $('#setRealismSave').title = '读取失败；保存会把当前显示值写入本机';
    }
    return;
  }
  const d = j.data || {};
  LIVE.realism = {obs_key_mode: d.mode === 'legacy' ? 'legacy' : 'single',
                  pause_profile: d.pause_profile || 'none',
                  gps_profile: d.gps_profile || 'urban',
                  terrain_coupling: !!d.terrain_coupling,
                  saved: !!d.saved};
  LIVE.realismRead = !!d.saved;      // false: bridge returned built-in defaults
  LIVE.realismError = null;
  $('#setRealismSave').disabled = false;
  $('#setRealismSave').title = '';
  $('#realismStatus').textContent = LIVE.realismRead ? '已保存' : '默认';
  $('#realismStatus').className = 'mini' + (LIVE.realismRead ? ' ok' : '');
  let hint = LIVE.realismRead
    ? '已保存到本机。只影响之后生成的预览和提交。'
    : '当前是推荐默认值，尚未保存；点「保存并应用」才会写入。';
  if(LIVE.realism.obs_key_mode === 'legacy'){
    hint = '已启用旧服务器兼容：会额外写入一份旧格式记录。一般不用改。';
  }
  $('#setRealismHint').textContent = hint;
  applyRealismForm(realismNow());
}
function applyRealismForm(r){
  const mode = normaliseRealism(r || REALISM_DEFAULTS);
  if($('#setObsKey')) $('#setObsKey').value = mode.obs_key_mode;
  if($('#setPause')) $('#setPause').value = mode.pause_profile;
  if($('#setGps')) $('#setGps').value = mode.gps_profile;
  if($('#setTerrain')) $('#setTerrain').value = mode.terrain_coupling ? '1' : '0';
}
async function saveRealism(){
  const btn = $('#setRealismSave');
  if(btn && btn.disabled) return;
  const want = {
    obs_key_mode: ($('#setObsKey') || {}).value || 'single',
    pause_profile: ($('#setPause') || {}).value || 'none',
    gps_profile: ($('#setGps') || {}).value || 'urban',
    terrain_coupling: ($('#setTerrain') || {}).value === '1'
  };
  if(btn){ btn.disabled = true; btn.textContent = '保存中…'; }
  try{
    const j = await liveMaybe('real_obs_key_mode', {
      mode: want.obs_key_mode,
      pause_profile: want.pause_profile,
      gps_profile: want.gps_profile,
      terrain_coupling: want.terrain_coupling
    }, 15000);
    if(bridgeMissing(j)){ LIVE.realismError = bridgeMessage(j); toast(LIVE.realismError); return; }
    if(!liveOk(j)){ toast('保存失败：' + bridgeMessage(j)); return; }
    LIVE.realism = want;
    await loadRealism();
    if(typeof updateRunDateLabel === 'function') updateRunDateLabel();
    refreshModelPreview().catch(() => {});
    toast('已保存，之后生成的跑步数据会使用这些设置');
    liveLog('跑步数据效果已保存', jsonText(want).replace(/\s+/g, ' '), 'ok');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '保存并应用'; }
  }
}
function resetRealismForm(){
  applyRealismForm(REALISM_DEFAULTS);
  toast('已恢复推荐默认值；点「保存并应用」才会写入');
}

/* ---------------- P0: error explanation ---------------- */
async function explainError(code, message, context){
  const j = await liveMaybe('real_explain_error',
    {code: code == null ? null : code, message: message || '', context: context || null}, 10000);
  if(!liveOk(j)){
    return {title: '未识别', cause: message || String(code || ''),
            action: bridgeMessage(j), retryable: false, ok: false};
  }
  const d = j.data || {};
  return {title: d.title || '错误', cause: d.cause || '', action: d.action || '',
          retryable: !!d.retryable, ok: true};
}
function renderExplainBox(title, cause, action){
  if($('#ovErrorTitle')) $('#ovErrorTitle').textContent = title || '—';
  if($('#ovErrorCause')) $('#ovErrorCause').textContent = cause ? ('原因：' + cause) : '';
  if($('#ovErrorAction')) $('#ovErrorAction').textContent = action ? ('下一步：' + action) : '';
  if($('#ovErrorNote')) $('#ovErrorNote').hidden = false;
}
function renderLogExplain(exp){
  const box = $('#logExplainBox');
  if(!box) return;
  box.hidden = false;
  if($('#logExplainTitle')) $('#logExplainTitle').textContent = exp.title || '—';
  if($('#logExplainCause')) $('#logExplainCause').textContent = exp.cause || '—';
  if($('#logExplainAction')) $('#logExplainAction').textContent = exp.action || '—';
}
function selectLogEntry(index){
  LIVE.logSel = index;
  document.querySelectorAll('#logEvents .log-event').forEach((li, i) =>
    li.classList.toggle('selected', i === index));
  if($('#logExplain')) $('#logExplain').disabled = !(index >= 0);
}
async function explainSelectedLog(){
  const entry = LIVE.log[LIVE.logSel];
  if(!entry){ toast('先在日志里点一条提醒或错误'); return; }
  const btn = $('#logExplain');
  if(btn){ btn.disabled = true; btn.textContent = '解析中…'; }
  try{
    const exp = await explainError(codeFromText(entry.detail) || codeFromText(entry.msg),
                                  entry.msg + (entry.detail ? ('：' + entry.detail) : ''),
                                  {source: 'log', level: entry.level});
    renderLogExplain(exp);
    renderExplainBox(exp.title, exp.cause, exp.action);
    if(!exp.ok) liveLog('错误解释未接入', exp.action, 'warn');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '解释选中'; }
  }
}
function codeFromText(text){
  const m = /(?:^|[^\d])(1\d{4})(?!\d)/.exec(String(text || ''));
  return m ? +m[1] : null;
}
function recordErrorForExplain(text, level){
  LIVE.logSel = -1;
  explainError(codeFromText(text), String(text || ''), {source: 'result'})
    .then(exp => renderExplainBox(exp.title, exp.cause, exp.action))
    .catch(() => {});
}

/* ---------------- P0: pre-submit audit ---------------- */
function auditUsable(){
  // The generation path is offline: `real_audit_run(**preview)` generates
  // the run locally and audits it, so a token is not required for that part.
  // (Record audits need a session; the bridge says so in its message.)
  if(LIVE.submitting) return '提交进行中，完成后再审计';
  return '';
}
function methodMissing(method){
  return !!(liveMaybe.missing && liveMaybe.missing.has(method));
}
function refreshAuditButton(){
  const btn = $('#auditBtn');
  if(!btn) return;
  const reason = auditUsable();
  btn.disabled = !!reason || LIVE.auditBusy || methodMissing('real_audit_run');
  btn.title = methodMissing('real_audit_run')
    ? bridgeMessage({bridgeMissing: true}) : (reason || '按当前参数做离线审计');
}
function auditArgs(){
  const params = readRunParams();
  const ti = selectedTimeInfo();
  if(!ti.ok) return {error: '开始时间无效：' + ti.note};
  const invalid = validateRunParams(params);
  if(invalid) return {error: invalid};
  const args = Object.assign(previewArgs(params, ti), {seed: LIVE.previewSeed || null});
  Object.assign(args, realismArgs());
  return {args};
}
function realismArgs(){
  const r = realismNow();
  return {pause_profile: r.pause_profile, gps_profile: r.gps_profile,
          terrain_coupling: r.terrain_coupling};
}
const AUDIT_LEVEL_LABEL = {error: '失败', warn: '警告', info: '信息'};
function auditLevel(c){
  // `level` describes the severity when a check fails; it is not the status of
  // a passing check (every passing check still carries level='error').
  if(c.ok) return 'ok';
  return c.level === 'warn' ? 'warn' : 'error';
}
function auditRow(c){
  const label = c.ok ? '通过'
    : (c.level === 'warn' ? '警告' : (c.level === 'info' ? '信息' : '失败'));
  return '<li class="audit-row ' + auditLevel(c) + '">' +
    '<span class="audit-level">' + esc(label) + '</span>' +
    '<span class="audit-label">' + esc(c.label || c.id || '') + '</span>' +
    '<span class="audit-detail">' + esc(c.detail || '') + '</span></li>';
}
function renderAudit(){
  const box = $('#auditChecks');
  if(!box) return;
  const a = LIVE.audit;
  const meta = $('#auditMeta');
  const toggle = $('#auditToggle');
  if(!a){
    box.innerHTML = '';
    if($('#auditSummary')){ $('#auditSummary').textContent = '未审计'; $('#auditSummary').className = ''; }
    if(meta) meta.textContent = '';
    if(toggle) toggle.hidden = true;
    if($('#auditCopy')) $('#auditCopy').disabled = true;
    return;
  }
  if(a.error){
    box.innerHTML = '<li class="audit-row error"><span class="audit-level">失败</span><span class="audit-label">审计没有完成</span><span class="audit-detail">' + esc(a.error) + '</span></li>';
    if($('#auditSummary')){ $('#auditSummary').textContent = '审计失败'; $('#auditSummary').className = 'warn'; }
    if(meta) meta.textContent = '';
    if(toggle) toggle.hidden = true;
    if($('#auditCopy')) $('#auditCopy').disabled = true;
    return;
  }
  const s = a.summary || {};
  const all = a.checks || [];
  const bad = all.filter(c => !c.ok);
  if($('#auditSummary')){
    $('#auditSummary').textContent = s.ok ? '通过' : '有问题';
    $('#auditSummary').className = s.ok ? 'ok' : 'warn';
  }
  if(meta){
    const parts = [];
    if(s.passed) parts.push(s.passed + ' 通过');
    if(s.warned) parts.push(s.warned + ' 警告');
    if(s.failed) parts.push(s.failed + ' 失败');
    meta.textContent = (parts.join(' · ') || (all.length + ' 项')) +
      (a.at ? (' · ' + a.at) : '');
  }
  if($('#auditCopy')) $('#auditCopy').disabled = false;
  const checks = all.slice().sort((x, y) => {
    const w = c => c.ok ? 2 : (c.level === 'warn' ? 1 : 0);
    return w(x) - w(y);
  });
  const showAll = !!LIVE.auditShowAll;
  const shown = showAll ? checks : bad;
  box.innerHTML = shown.length
    ? shown.map(auditRow).join('')
    : '<li class="audit-row ok"><span class="audit-level">通过</span>' +
      '<span class="audit-label">全部通过</span>' +
      '<span class="audit-detail">' + all.length + ' 项检查</span></li>';
  if(toggle){
    // Nothing to filter when every check passed; keep the card quiet.
    toggle.hidden = !all.length || !bad.length;
    toggle.disabled = !all.length;
    toggle.textContent = showAll
      ? ('只看问题（' + bad.length + '）')
      : ('显示全部 ' + all.length + ' 项');
  }
  if(bad.length) liveLog('提交前审计', bad.slice(0, 3).map(c => (c.label || c.id) + '：' + (c.detail || '')).join('；'),
                         bad.some(c => c.level === 'error') ? 'err' : 'warn');
}
async function runAudit(){
  if(LIVE.auditBusy) return;
  const prepared = auditArgs();
  if(prepared.error){ toast(prepared.error); return; }
  LIVE.auditBusy = true;
  LIVE.auditShowAll = false;
  refreshAuditButton();
  if($('#auditBtn')) $('#auditBtn').textContent = '审计中…';
  try{
    const j = await liveMaybe('real_audit_run', prepared.args, 60000);
    if(bridgeMissing(j)){
      LIVE.audit = {error: bridgeMessage(j)};
      if($('#auditBtn')){ $('#auditBtn').disabled = true; $('#auditBtn').title = LIVE.audit.error; }
      renderAudit();
      toast(LIVE.audit.error);
      return;
    }
    if(!liveOk(j)){
      LIVE.audit = {error: bridgeMessage(j)};
      renderAudit();
      refreshAuditButton();
      toast('审计失败：' + bridgeMessage(j));
      return;
    }
    LIVE.audit = j.data || {};
    LIVE.auditShowAll = false;
    renderAudit();
    if(LIVE.audit.summary && LIVE.audit.summary.ok === false){
      switchRunTab('submit');
    }
  }finally{
    LIVE.auditBusy = false;
    if($('#auditBtn')) $('#auditBtn').textContent = '开始审计';
    refreshAuditButton();
  }
}
async function copyAuditJson(){
  if(!LIVE.audit || LIVE.audit.error) return;
  const ok = await copyPlainText(jsonText({summary: LIVE.audit.summary,
    checks: LIVE.audit.checks, record: LIVE.audit.record, at: LIVE.audit.at}));
  toast(ok ? '审计结果已复制' : '复制失败，请手动选择');
}

/* ---------------- P0: read-only verification ---------------- */
const VERDICT_LABEL = {ok: '正常', warn: '需留意', issue: '有问题', unknown: '无法判定'};
function renderVerify(box, v, errorText){
  if(!box) return;
  box.hidden = false;
  if(errorText){
    box.innerHTML = '<div class="verify-empty">核验失败：' + esc(errorText) + '</div>';
    return;
  }
  const head = '<div class="verify-head"><span class="verify-verdict ' +
    esc(v.verdict || 'unknown') + '">' + esc(VERDICT_LABEL[v.verdict] || v.verdict || '无法判定') +
    '</span><span class="mini">' + esc((v.track && v.track.points ? v.track.points + ' 点' : '无轨迹') +
    (v.track && v.track.source ? ' · ' + v.track.source : '') + (v.at ? ' · ' + v.at : '')) +
    '</span></div>';
  // Per-step timestamps are optional; when the backend omits them, do not
  // reserve an empty 60px column that squeezes the text into a ragged stack.
  const timeline = v.timeline || [];
  const hasAt = timeline.some(t => t && t.at);
  const rows = timeline.map(t =>
    '<li class="verify-step ' + (t.ok === false ? 'bad' : 'ok') + '">' +
    (hasAt ? '<span class="verify-at">' + esc(t.at || '') + '</span>' : '') +
    '<span class="verify-name">' + esc(t.step || '') + '</span>' +
    '<span class="verify-text">' + esc(t.text || '') + '</span></li>').join('');
  const next = v.next ? ('<div class="verify-next">' + esc(v.next) + '</div>') : '';
  box.innerHTML = head + '<ul class="verify-list' + (hasAt ? ' has-at' : '') + '">' + rows + '</ul>' + next;
}
async function verifyRecord(target){
  const r = target || {};
  if(!r.rrid && !r.uuid){ toast('需要 rrid 或 uuid 才能核验'); return; }
  if(LIVE.verifyBusy){ toast('核验进行中'); return; }
  LIVE.verifyBusy = true;
  const submitBox = $('#submitVerify'), detailBox = $('#rdVerifyBox');
  const btn = r.via === 'submit' ? $('#submitResultVerify') : $('#rdVerify');
  if(btn){ btn.disabled = true; btn.textContent = '核验中…'; }
  try{
    const j = await liveMaybe('real_verify_record',
      {rrid: r.rrid || null, uuid: r.uuid || null}, 60000);
    const box = r.via === 'submit' ? submitBox : detailBox;
    if(bridgeMissing(j)){
      renderVerify(box, null, bridgeMessage(j));
      if(btn) btn.title = bridgeMessage(j);
      toast(bridgeMessage(j));
      return;
    }
    if(!liveOk(j)){
      renderVerify(box, null, bridgeMessage(j));
      toast('核验失败：' + bridgeMessage(j));
      return;
    }
    const v = j.data || {};
    LIVE.verify = v;
    renderVerify(box, v, '');
    liveLog('只读核验', (VERDICT_LABEL[v.verdict] || v.verdict || '?') +
      (v.next ? (' · ' + v.next) : ''), v.verdict === 'issue' ? 'warn' : 'ok');
  }finally{
    LIVE.verifyBusy = false;
    if(btn){ btn.disabled = false; btn.textContent = '只读核验'; }
  }
}

/* ---------------- P1: presets ---------------- */
let presetSeq = 0;
function presetCanWrite(){
  if(hasSession()) return '';
  return '登录后可保存预设';
}
async function loadPresets(){
  const seq = ++presetSeq;
  const j = await liveMaybe('real_presets', {action: 'list'}, 10000);
  if(seq !== presetSeq) return;
  if(bridgeMissing(j)){
    LIVE.presetsLoaded = false;
    LIVE.presetsError = bridgeMessage(j);
    renderPresets();
    return;
  }
  if(!liveOk(j)){
    LIVE.presetsLoaded = false;
    LIVE.presetsError = bridgeMessage(j);
    renderPresets();
    return;
  }
  LIVE.presets = (j.data && j.data.presets) || [];
  LIVE.presetsLoaded = true;
  LIVE.presetsError = null;
  renderPresets();
}
function renderPresets(){
  const sel = $('#presetSel');
  if(!sel) return;
  const reason = LIVE.presetsLoaded ? '' : (LIVE.presetsError || '预设未读取');
  if(!LIVE.presetsLoaded){
    sel.innerHTML = '<option value="">' + esc(reason) + '</option>';
    sel.disabled = true;
  }else if(!LIVE.presets.length){
    sel.innerHTML = '<option value="">还没有预设</option>';
    sel.disabled = true;
  }else{
    const keep = sel.value;
    sel.innerHTML = '<option value="">— 选择预设 —</option>' + LIVE.presets.map(r =>
      '<option value="' + esc(r.name) + '">' + esc(r.name) +
      (r.data && r.data.km ? ' · ' + (+r.data.km).toFixed(2) + ' km' : '') +
      '</option>').join('');
    sel.disabled = false;
    if(LIVE.presets.some(r => r.name === keep)) sel.value = keep;
  }
  const has = LIVE.presetsLoaded && !!sel.value;
  const writeBlock = presetCanWrite();
  if($('#presetApply')) $('#presetApply').disabled = !has;
  if($('#presetSave')){
    $('#presetSave').disabled = !!writeBlock || methodMissing('real_presets');
    $('#presetSave').title = writeBlock || (methodMissing('real_presets')
      ? bridgeMessage({bridgeMissing: true}) : '保存当前参数为预设');
  }
  if($('#presetDelete')){
    $('#presetDelete').disabled = !has;
    $('#presetDelete').title = has ? '' : '先选择一个预设';
  }
  if($('#presetStatus')){
    $('#presetStatus').textContent = LIVE.presetsLoaded
      ? (LIVE.presets.length + ' 条') : (LIVE.presetsError ? '未接入' : '未读取');
    $('#presetStatus').className = LIVE.presetsLoaded ? 'ok' : 'warn';
  }
  if($('#presetHint')) $('#presetHint').textContent = LIVE.presetsLoaded
    ? '保存当前距离、配速、步频、海拔与路线；最多 30 条，只存本机，不保存任何账号信息。'
    : (LIVE.presetsError || '');
}
function presetDataFromForm(){
  const p = readRunParams();
  const data = {
    km: p.km, pace: p.pace, cadence: p.cadence, altitude: p.altitude,
    closed: !!routeClosed, altitude_mode: p.altitudeMode,
    altitude_range: p.altitudeRange || null,
    sequential: !!LIVE.sequential,
    free_run: freeRunOn(),
    run_area_id: LIVE.runArea && LIVE.runArea.id != null ? LIVE.runArea.id : null,
    km_times: splitsManual && splitRows ? splitRows.map(r => Math.round(+r.seconds || 0)) : null,
    route: route && route.length >= 2 ? route.map(pt => [+pt[0], +pt[1]]) : null
  };
  if(Array.isArray(data.km_times) && data.km_times.length > 100) data.km_times = null;
  if(p.altitudeProfile && p.altitudeProfile.length) data.altitude_profile = p.altitudeProfile;
  if(data.altitude_mode !== 'template') delete data.altitude_profile;
  return data;
}
async function savePreset(){
  const name = ($('#presetName') && $('#presetName').value || '').trim();
  if(!name){ toast('请先填写预设名称'); if($('#presetName')) $('#presetName').focus(); return; }
  const invalid = validateRunParams(readRunParams());
  if(invalid){ toast(invalid); return; }
  const btn = $('#presetSave');
  if(btn){ btn.disabled = true; btn.textContent = '保存中…'; }
  try{
    const j = await liveMaybe('real_presets', {action: 'save', name: name, data: presetDataFromForm()}, 15000);
    if(!liveOk(j)){ toast('保存失败：' + bridgeMessage(j)); return; }
    const d = j.data || {};
    if(d.dropped && d.dropped.length) liveLog('预设已满', '已移除最早：' + d.dropped.join('、'), 'warn');
    if($('#presetName')) $('#presetName').value = '';
    await loadPresets();
    if($('#presetSel')) $('#presetSel').value = d.name || name;
    renderPresets();
    toast((d.replaced ? '已更新预设：' : '已保存预设：') + (d.name || name));
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '保存'; }
  }
}
async function deletePreset(){
  const name = ($('#presetSel') && $('#presetSel').value) || '';
  if(!name){ toast('先选择一个预设'); return; }
  if(!confirm('删除预设「' + name + '」？只删除本机记录。')) return;
  const j = await liveMaybe('real_presets', {action: 'delete', name: name}, 15000);
  if(!liveOk(j)){ toast('删除失败：' + bridgeMessage(j)); return; }
  await loadPresets();
  toast('已删除预设：' + name);
}
function applyPreset(){
  const name = ($('#presetSel') && $('#presetSel').value) || '';
  const row = (LIVE.presets || []).find(r => r.name === name);
  if(!row){ toast('先选择一个预设'); return; }
  const d = row.data || {};
  if(isFinite(+d.km) && +d.km > 0 && $('#inKm')) $('#inKm').value = (+d.km).toFixed(2);
  if(isFinite(+d.pace) && $('#inPace')) $('#inPace').value = (+d.pace).toFixed(2);
  if(isFinite(+d.cadence) && $('#inCad')) $('#inCad').value = Math.round(+d.cadence);
  if(isFinite(+d.altitude) && $('#inAlt')) $('#inAlt').value = Math.round(+d.altitude);
  if(typeof d.altitude_mode === 'string' && $('#altMode')){
    $('#altMode').value = d.altitude_mode;
    altAdjustOpen = false;
    altModeChanged();
  }
  if(Array.isArray(d.altitude_range)){
    if($('#altMin')) $('#altMin').value = d.altitude_range[0] == null ? '' : d.altitude_range[0];
    if($('#altMax')) $('#altMax').value = d.altitude_range[1] == null ? '' : d.altitude_range[1];
  }
  // Geometry is round-tripped explicitly; no other switch may redraw it.
  if(Array.isArray(d.route) && d.route.length >= 2){
    route = d.route.map(pt => [+pt[0], +pt[1]]);
    lastRouteLength = 0;
    redraw();
  }
  if(d.closed != null) setCloseUI(!!d.closed);
  if(Array.isArray(d.km_times) && d.km_times.length){
    if(typeof splitsManual !== 'undefined' && splitsManual){
      splitRows = d.km_times.map((s, i) => ({km: splitRows && splitRows[i] ? +splitRows[i].km : (i + 1 <= (+d.km || 0) ? 1 : 0),
                                             seconds: +s}));
      renderSplits();
    }else{
      toast('预设含手动分段；如需保留请到参数页点「手动微调」');
    }
  }
  refreshParams();
  updateRunDateLabel();
  scheduleModelPreview();
  toast('已应用预设：' + name + '（可再调整参数）');
  liveLog('应用预设', name, 'ok');
}

/* ---------------- P2: local statistics ---------------- */
function fmtPaceText(p){ return p == null ? '—' : paceText(p); }
async function loadStats(){
  if(LIVE.statsBusy) return;
  const weeks = +($('#statsWeeks') && $('#statsWeeks').value || 12);
  LIVE.statsBusy = true;
  const btn = $('#statsRun');
  if(btn){ btn.disabled = true; btn.textContent = '读取中…'; }
  try{
    const j = await liveMaybe('real_stats', {weeks: weeks}, 60000);
    if(bridgeMissing(j)){
      LIVE.stats = {error: bridgeMessage(j)};
    }else if(!liveOk(j)){
      LIVE.stats = {error: bridgeMessage(j)};
    }else{
      LIVE.stats = j.data || {};
    }
    renderStats();
  }finally{
    LIVE.statsBusy = false;
    if(btn){ btn.disabled = false; btn.textContent = '读取'; }
  }
}
function renderStats(){
  const box = $('#statsBody');
  if(!box) return;
  const s = LIVE.stats;
  if(!s || !Object.keys(s).length){
    box.innerHTML = '<div class="receipt-empty">点「读取」按本机回执与服务器记录统计；只读。</div>';
    return;
  }
  if(s.error){
    box.innerHTML = '<div class="receipt-empty">统计失败：' + esc(s.error) + '</div>';
    return;
  }
  const t = s.totals || {};
  const sub = s.submission || {};
  const weeks = (t.by_week || []);
  const maxKm = Math.max(0.1, ...weeks.map(w => +w.distance_km || 0));
  const src = s.source === 'server+local' ? '服务器记录 + 本机回执'
    : '仅本机回执（服务器记录未读取' + (s.record_error ? '：' + s.record_error : '') + '）';
  const tiles = [
    ['跑量', (+t.distance_km || 0).toFixed(1), 'km'],
    ['次数', t.runs || 0, ''],
    ['平均配速', fmtPaceText(t.avg_pace_min_per_km), '/km'],
    ['平均步频', t.avg_cadence == null ? '—' : Math.round(t.avg_cadence), 'spm'],
    ['本月', (+((s.month || {}).distance_km) || 0).toFixed(1), 'km'],
    ['提交成功率', sub.success_rate == null ? '—' : sub.success_rate, '%']
  ];
  const bars = weeks.map(w => {
    const km = +w.distance_km || 0;
    const h = km ? Math.max(6, Math.round(70 * km / maxKm)) : 2;
    return '<div class="bar" title="' + esc(w.week_start + ' – ' + w.week_end) + '"><b>' +
      (km ? km.toFixed(1) : '–') + '</b><i style="height:' + h + '%"></i><span>' +
      esc((w.week_start || '').slice(5)) + '</span></div>';
  }).join('');
  const fails = (sub.top_failures || []).map(f =>
    '<li><b>' + esc(f.reason) + '</b><span>× ' + esc(f.count) + '</span></li>').join('') ||
    '<li class="mini">没有失败记录</li>';
  const wdNames = ['一', '二', '三', '四', '五', '六', '日'];
  const wd = (s.weekday_km || []).map((km, i) =>
    '<span title="周' + wdNames[i] + '"><i>' + wdNames[i] + '</i>' +
    esc(+km ? km.toFixed(1) : '–') + '</span>').join('');
  box.innerHTML =
    '<div class="stats-source">' + esc(src) + (s.window ? (' · ' + esc(s.window.start) + ' 至 ' + esc(s.window.end)) : '') + '</div>' +
    '<div class="stat-strip stats-strip">' + tiles.map(([k, v, u]) =>
      '<div class="stat"><b>' + esc(v) + (u ? '<small>' + esc(u) + '</small>' : '') + '</b><span>' + esc(k) + '</span></div>').join('') + '</div>' +
    '<div class="bars stats-bars">' + (bars || '<div class="mini">窗口内没有记录</div>') + '</div>' +
    '<div class="stats-split"><div><div class="detail-subhead">提交结果</div>' +
    '<div class="mini">尝试 ' + (sub.attempts || 0) + ' · 成功 ' + (sub.accepted || 0) +
    ' · 失败 ' + (sub.rejected || 0) + ' · 未决 ' + (sub.open || 0) + '</div>' +
    '<div class="detail-subhead" style="margin-top:6px">失败原因 Top ' + (sub.top_failures || []).length + '</div>' +
    '<ul class="stats-fails">' + fails + '</ul></div>' +
    '<div><div class="detail-subhead">星期分布（km）</div><div class="weekday-grid">' + wd + '</div></div></div>';
}

/* ---------------- P2: FIT / diagnostics download ---------------- */
function saveB64(b64, filename, mime){
  const bin = atob(b64 || '');
  const bytes = new Uint8Array(bin.length);
  for(let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  saveBlob(new Blob([bytes], {type: mime || 'application/octet-stream'}), filename || 'download');
}
async function exportFit(target, btn){
  const r = target || {};
  if(!r.rrid && !r.uuid){ toast('需要 rrid 或 uuid 才能导出 FIT'); return; }
  if(btn){ btn.disabled = true; btn.textContent = '生成中…'; }
  try{
    const j = await liveMaybe('real_export_fit', {rrid: r.rrid || null, uuid: r.uuid || null}, 60000);
    if(!liveOk(j)){ toast('FIT 导出失败：' + bridgeMessage(j)); return; }
    const d = j.data || {};
    if(!d.data_b64){ toast('FIT 导出失败：后端没有返回文件内容'); return; }
    saveB64(d.data_b64, d.filename || 'run.fit', d.mime || 'application/vnd.garmin.fit');
    if(!d.points) liveLog('导出 FIT', '记录没有轨迹点时后端会报错；本次返回 ' + (d.bytes || 0) + ' 字节', 'warn');
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '导出 FIT'; }
  }
}
async function downloadDiagZip(btn){
  if(btn){ btn.disabled = true; btn.textContent = '生成中…'; }
  try{
    const j = await liveMaybe('real_diagnostics_zip', {}, 60000);
    if(!liveOk(j)){ toast('诊断包生成失败：' + bridgeMessage(j)); return; }
    const d = j.data || {};
    if(!d.data_b64){ toast('诊断包生成失败：后端没有返回文件内容'); return; }
    saveB64(d.data_b64, d.filename || 'haoxiangpao-diagnostics.zip', d.mime || 'application/zip');
    toast('诊断包已下载（已脱敏）');
    liveLog('诊断包 ZIP', (d.files || []).join(', ') + ' · ' + (d.bytes || 0) + ' 字节', 'ok');
  }finally{
    if(btn){
      btn.disabled = !hasSession();
      btn.textContent = btn.id === 'diagZip' ? '下载脱敏诊断包 ZIP' : '诊断包';
    }
  }
}

/* ---------------- P2: read-only inspection ---------------- */
const INSPECT_LEVEL = {ok: '正常', warn: '注意', error: '阻止', info: '信息'};
function renderInspect(){
  const list = $('#inspectList');
  if(!list) return;
  const s = LIVE.inspect;
  if(!s){
    list.innerHTML = '<div class="inspect-empty">登录后可手动巡检。</div>';
    if($('#inspectAt')) $('#inspectAt').textContent = hasSession() ? '待巡检' : '未登录';
    return;
  }
  if(s.error){
    list.innerHTML = '<div class="inspect-empty">巡检失败：' + esc(s.error) + '</div>';
    return;
  }
  if($('#inspectAt')) $('#inspectAt').textContent = (s.ok ? '全部正常' : '有关注项') +
    (s.at ? ' · ' + s.at : '');
  list.innerHTML = (s.items || []).map(it =>
    '<div class="inspect-row ' + esc(it.level || 'info') + '">' +
    '<b>' + esc(it.label || it.id || '') + '</b>' +
    '<span>' + esc(it.text || '') + '</span>' +
    '<i>' + esc(INSPECT_LEVEL[it.level] || it.level || '') + '</i></div>').join('') ||
    '<div class="inspect-empty">没有检查项</div>';
  if($('#inspectHint')) $('#inspectHint').className = 'hint';
  refreshAuditButton();
}
async function runInspection(){
  if(LIVE.inspectBusy || !hasSession()) return;
  LIVE.inspectBusy = true;
  const btn = $('#inspectRefresh');
  if(btn){ btn.disabled = true; btn.textContent = '巡检中…'; }
  try{
    const j = await liveMaybe('real_inspection',
      {freeze_seconds: +LIVE.freezeSeconds || 0}, 60000);
    if(bridgeMissing(j)) LIVE.inspect = {error: bridgeMessage(j)};
    else if(!liveOk(j)) LIVE.inspect = {error: bridgeMessage(j)};
    else LIVE.inspect = j.data || {};
    renderInspect();
    if(LIVE.inspect.error){
      toast(LIVE.inspect.error);
      if(btn) btn.title = LIVE.inspect.error;
    }
  }finally{
    LIVE.inspectBusy = false;
    if(btn){
      btn.disabled = !hasSession() || methodMissing('real_inspection');
      btn.textContent = '巡检';
    }
  }
}

/* ---------------- local replay canvas ---------------- */
const REPLAY = {raf: 0, playing: false, t: 0, total: 1, speed: 4, last: 0};
function replayClose(){
  REPLAY.playing = false;
  if(REPLAY.raf) cancelAnimationFrame(REPLAY.raf);
  REPLAY.raf = 0;
  if($('#replayModal')) $('#replayModal').hidden = true;
}
/* Build the animation model from whatever the app already has.  Track
   points come from record detail (`lat/lng` + `duration_s` + `distance_m`)
   or a generated preview (`allLocJson` rows); the 10 s series supplies the
   curves; fixed points supply the checkpoints. */
function replayModelFromDetail(detail){
  const d = detail || {};
  const meta = d.detail || {};
  const track = (d.track || []).filter(p => isFinite(+p.lat) && isFinite(+p.lng));
  if(track.length < 2) return null;
  const pts = track.map((p, i) => ({
    lat: +p.lat, lng: +p.lng, alt: p.altitude == null ? null : +p.altitude,
    t: p.duration_s == null ? null : +p.duration_s,
    d: p.distance_m == null ? null : +p.distance_m
  }));
  return finishReplayModel(pts, (d.analysis || {}).series || [],
    d.five_point || [], meta.start_ms || 0, '记录 ' + (meta.rrid || ''));
}
function replayModelFromRun(run, fivePoints, cps){
  const locs = ((run && run.allLocJson) || []).filter(p => isFinite(+p.gLat) && isFinite(+p.gLng));
  if(locs.length < 2) return null;
  const pts = locs.map(p => ({
    lat: +p.gLat, lng: +p.gLng, alt: p.bdA == null ? null : +p.bdA,
    // totalTime is the active clock (seconds); gainTime is a wall-clock
    // string, so `+gainTime` would be NaN and freeze the animation.
    t: p.totalTime == null ? null : +p.totalTime,
    d: p.totalDis == null ? null : +p.totalDis
  }));
  return finishReplayModel(pts, previewSeriesFromRun(run || {}),
    fivePoints || [], (run && run.start_ms) || 0, '本次轨迹', cps);
}
function previewSeriesFromRun(run){
  // The preview path already returns `preview.series`; this fallback keeps
  // the curves for a raw run dict handed in without it.
  const speeds = (run.speed_json || []).filter(s => s && s.beginTime != null);
  const steps = {};
  (run.step_freq_json || []).forEach(s => { if(s) steps[s.beginTime] = s; });
  const first = speeds.length ? +speeds[0].beginTime : 0;
  const out = [];
  speeds.forEach(s => {
    const dt = Math.max(0.001, (+s.endTime - +s.beginTime) / 1000);
    const dist = +s.distance || 0;
    const st = steps[s.beginTime] || {};
    const n = +st.stepsNum || 0;
    out.push({t: (+s.beginTime - first) / 1000,
              pace: dist > 0 && dt >= 6 ? (dt / 60) / (dist / 1000) : null,
              cadence: n > 0 && dt >= 6 ? n / (dt / 60) : null,
              alt: null});
  });
  return out;
}
function finishReplayModel(pts, series, five, startMs, label, cps){
  pts = pts.slice().sort((a, b) => (a.t == null ? 0 : a.t) - (b.t == null ? 0 : b.t));
  // A point without a time sits on the previous one's clock: the animation
  // holds still there instead of inventing an interval.
  let t = 0;
  pts.forEach(p => { if(p.t == null) p.t = t; else t = p.t; });
  const duration = Math.max(1, +((pts[pts.length - 1] || {}).t || 0));
  // Distance: the record's own value where present, otherwise path length.
  const PX_PER_M_LAT = 110540, PX_PER_M_LNG = 111320;
  const midLat = pts.reduce((s, p) => s + p.lat, 0) / pts.length;
  let acc = 0, prev = null;
  pts.forEach(p => {
    if(prev) acc += Math.hypot((p.lat - prev.lat) * PX_PER_M_LAT,
                               (p.lng - prev.lng) * PX_PER_M_LNG * Math.cos(midLat * Math.PI / 180));
    if(p.d == null) p.d = acc; prev = p;
  });
  const dist = pts[pts.length - 1].d || acc;
  const seriesClean = (series || []).filter(s => s && isFinite(+s.t));
  const cpRaw = (five && five.length ? five : (cps || []));
  const checkpoints = cpRaw.map((p, i) => {
    const lat = +(p.glat ?? p.gLat ?? p.lat), lng = +(p.glon ?? p.gLng ?? p.lng ?? p.lon);
    if(!isFinite(lat) || !isFinite(lng)) return null;
    let best = 0, bd = Infinity;
    pts.forEach((q, j) => {
      const dm = Math.hypot((q.lat - lat) * PX_PER_M_LAT, (q.lng - lng) * PX_PER_M_LNG);
      if(dm < bd){ bd = dm; best = j; }
    });
    const pass = !!(p.isPass ?? p.is_pass ?? p.pass);
    return {lat, lng, index: best, t: pts[best].t, pass, name: p.pointName || p.name || ('点位 ' + (i + 1))};
  }).filter(Boolean);
  return {pts, series: seriesClean, checkpoints, duration, distance: dist,
          startMs, label: label || '回放',
          hasAlt: pts.some(p => p.alt != null) || seriesClean.some(s => s.alt != null)};
}
function replayAt(model, t){
  const pts = model.pts;
  let i = 0;
  while(i + 1 < pts.length && pts[i + 1].t <= t) i++;
  const a = pts[i], b = pts[Math.min(i + 1, pts.length - 1)];
  const span = (b.t - a.t) || 1;
  const k = a === b ? 1 : Math.max(0, Math.min(1, (t - a.t) / span));
  const lat = a.lat + (b.lat - a.lat) * k, lng = a.lng + (b.lng - a.lng) * k;
  const alt = a.alt == null || b.alt == null ? (a.alt ?? b.alt) : a.alt + (b.alt - a.alt) * k;
  const dist = (a.d || 0) + ((b.d || 0) - (a.d || 0)) * k;
  let series = null;
  const s = model.series || [];
  if(s.length){
    let j = 0;
    while(j + 1 < s.length && +s[j + 1].t <= t) j++;
    series = s[j];
  }
  return {lat, lng, alt, dist, index: i, series};
}
function replayResize(canvas){
  if(!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || canvas.width;
  const cssH = canvas.clientHeight || canvas.height;
  if(canvas.width !== Math.round(cssW * ratio) || canvas.height !== Math.round(cssH * ratio)){
    canvas.width = Math.round(cssW * ratio);
    canvas.height = Math.round(cssH * ratio);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return {ctx, w: cssW, h: cssH};
}
const REPLAY_COLORS = {track: themeColor('--ink-blue', '#2E4A7D'),
                       done: themeColor('--stamp', '#B6483C'),
                       ink: themeColor('--ink', '#24231F'),
                       faint: themeColor('--faint', '#938B79'),
                       rule: themeColor('--rule', '#D8D1BF'),
                       hi: themeColor('--highlighter', '#F2D25C'),
                       green: themeColor('--green', '#4E7657'),
                       paper: themeColor('--paper-2', '#FBF8F0')};
function refreshReplayColors(){
  REPLAY_COLORS.track = themeColor('--ink-blue', '#2E4A7D');
  REPLAY_COLORS.done = themeColor('--stamp', '#B6483C');
  REPLAY_COLORS.ink = themeColor('--ink', '#24231F');
  REPLAY_COLORS.faint = themeColor('--faint', '#938B79');
  REPLAY_COLORS.rule = themeColor('--rule', '#D8D1BF');
  REPLAY_COLORS.hi = themeColor('--highlighter', '#F2D25C');
  REPLAY_COLORS.green = themeColor('--green', '#4E7657');
  REPLAY_COLORS.paper = themeColor('--paper-2', '#FBF8F0');
}
function drawReplayTrack(model, t){
  const c = replayResize($('#replayCanvas'));
  if(!c) return;
  const {ctx, w, h} = c;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = REPLAY_COLORS.paper;
  ctx.fillRect(0, 0, w, h);
  // Fit: mirror longitude so a run heading east goes right.
  const lats = model.pts.map(p => p.lat), lngs = model.pts.map(p => p.lng);
  const minLat = Math.min(...lats), maxLat = Math.max(...lats);
  const minLng = Math.min(...lngs), maxLng = Math.max(...lngs);
  const midLat = (minLat + maxLat) / 2;
  const kx = Math.cos(midLat * Math.PI / 180);
  const spanX = Math.max(1e-7, (maxLng - minLng) * kx), spanY = Math.max(1e-7, maxLat - minLat);
  const pad = 22;
  const scale = Math.min((w - pad * 2) / spanX, (h - pad * 2) / spanY);
  const ox = (w - spanX * scale) / 2, oy = (h - spanY * scale) / 2;
  const X = lng => ox + (lng - minLng) * kx * scale;
  const Y = lat => h - oy - (lat - minLat) * scale;
  ctx.lineJoin = ctx.lineCap = 'round';
  ctx.strokeStyle = REPLAY_COLORS.track;
  ctx.globalAlpha = 0.28;
  ctx.lineWidth = 4;
  ctx.beginPath();
  model.pts.forEach((p, i) => { i ? ctx.lineTo(X(p.lng), Y(p.lat)) : ctx.moveTo(X(p.lng), Y(p.lat)); });
  ctx.stroke();
  ctx.globalAlpha = 1;
  const head = replayAt(model, t);
  ctx.strokeStyle = REPLAY_COLORS.done;
  ctx.lineWidth = 2.6;
  ctx.beginPath();
  model.pts.forEach((p, i) => { if(p.t > t) return; i ? ctx.lineTo(X(p.lng), Y(p.lat)) : ctx.moveTo(X(p.lng), Y(p.lat)); });
  if(head.index >= 0) ctx.lineTo(X(head.lng), Y(head.lat));
  ctx.stroke();
  // checkpoints
  model.checkpoints.forEach(cp => {
    const reached = cp.t <= t;
    ctx.beginPath();
    ctx.arc(X(cp.lng), Y(cp.lat), reached ? 6 : 4.5, 0, Math.PI * 2);
    ctx.fillStyle = cp.pass === false ? REPLAY_COLORS.done : (reached ? REPLAY_COLORS.green : REPLAY_COLORS.faint);
    ctx.fill();
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = REPLAY_COLORS.paper;
    ctx.stroke();
    if(reached){
      ctx.fillStyle = REPLAY_COLORS.ink;
      ctx.font = '10px ' + (getComputedStyle(document.documentElement)
        .getPropertyValue('--sans') || 'sans-serif');
      ctx.fillText(cp.name.slice(0, 12), X(cp.lng) + 8, Y(cp.lat) - 6);
    }
  });
  // start / head
  const s0 = model.pts[0];
  ctx.fillStyle = REPLAY_COLORS.track;
  ctx.beginPath(); ctx.arc(X(s0.lng), Y(s0.lat), 4, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = REPLAY_COLORS.hi;
  ctx.strokeStyle = REPLAY_COLORS.ink;
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(X(head.lng), Y(head.lat), 6.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
}
function drawReplayCurve(canvas, model, key, opts){
  const c = replayResize(canvas);
  if(!c) return;
  const {ctx, w, h} = c;
  ctx.clearRect(0, 0, w, h);
  const pad = {l: 36, r: 8, t: 6, b: 12};
  const W = w - pad.l - pad.r, H = h - pad.t - pad.b;
  const series = model.series.filter(s => s[key] != null && isFinite(+s[key]));
  ctx.strokeStyle = REPLAY_COLORS.rule;
  ctx.lineWidth = 1;
  ctx.strokeRect(pad.l, pad.t, W, H);
  const X = t => pad.l + (t / model.duration) * W;
  model.checkpoints.forEach(cp => {
    ctx.strokeStyle = REPLAY_COLORS.green;
    ctx.globalAlpha = 0.35;
    ctx.beginPath(); ctx.moveTo(X(cp.t), pad.t); ctx.lineTo(X(cp.t), pad.t + H); ctx.stroke();
    ctx.globalAlpha = 1;
  });
  if(!series.length){
    ctx.fillStyle = REPLAY_COLORS.faint;
    ctx.font = '11px sans-serif';
    ctx.fillText('没有' + opts.empty, pad.l + 6, pad.t + 18);
    return;
  }
  const vals = series.map(s => +s[key]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if(opts.clamp){ lo = Math.max(lo, opts.clamp[0]); hi = Math.min(hi, opts.clamp[1]); }
  if(hi - lo < 1e-6){ hi += 1; lo -= 1; }
  const Y = v => opts.invert
    ? pad.t + ((Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo)) * H
    : pad.t + H - ((Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo)) * H;
  ctx.fillStyle = REPLAY_COLORS.faint;
  ctx.font = '9px monospace';
  ctx.fillText(opts.fmt(hi), 2, pad.t + 8);
  ctx.fillText(opts.fmt(lo), 2, pad.t + H);
  ctx.strokeStyle = opts.color || REPLAY_COLORS.track;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  series.forEach((s, i) => {
    const x = X(+s.t), y = Y(+s[key]);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  // play head
  const playhead = X(REPLAY.t);
  ctx.strokeStyle = REPLAY_COLORS.done;
  ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(playhead, pad.t); ctx.lineTo(playhead, pad.t + H); ctx.stroke();
  ctx.setLineDash([]);
}
function replayCurves(){
  const m = LIVE.replay;
  if(!m) return;
  drawReplayCurve($('#replayPaceCurve'), m, 'pace', {
    invert: true, fmt: v => HXPCharts.fmtPace(v), color: REPLAY_COLORS.done, empty: '配速曲线'});
  drawReplayCurve($('#replayCadCurve'), m, 'cadence', {
    fmt: v => Math.round(v), color: REPLAY_COLORS.green, empty: '步频曲线'});
  drawReplayCurve($('#replayAltCurve'), m, 'alt', {
    fmt: v => Math.round(v), color: REPLAY_COLORS.track, empty: '海拔曲线'});
}
function replayReadout(){
  const m = LIVE.replay;
  if(!m) return;
  const head = replayAt(m, REPLAY.t);
  const pct = Math.round(REPLAY.t / m.duration * 100);
  if($('#replayProgress')) $('#replayProgress').textContent = pct + '% · ' +
    (head.dist / 1000).toFixed(2) + ' km';
  if($('#replayClock')) $('#replayClock').textContent = hms(Math.round(REPLAY.t)) +
    ' / ' + hms(Math.round(m.duration));
  const series = head.series || {};
  if($('#replayPace')) $('#replayPace').textContent = series.pace != null
    ? HXPCharts.fmtPace(series.pace) : '—';
  if($('#replayCadence')) $('#replayCadence').textContent = series.cadence != null
    ? Math.round(series.cadence) : '—';
  if($('#replayAlt')) $('#replayAlt').textContent = head.alt != null
    ? Math.round(head.alt) + ' m' : '—';
  const hit = m.checkpoints.filter(cp => cp.t <= REPLAY.t).length;
  if($('#replayCp')) $('#replayCp').textContent = m.checkpoints.length
    ? (hit + ' / ' + m.checkpoints.length) : '无';
  if($('#replayScrub')) $('#replayScrub').value = String(Math.round(REPLAY.t / m.duration * 1000));
}
function replayDraw(){
  const m = LIVE.replay;
  if(!m) return;
  drawReplayTrack(m, REPLAY.t);
  replayCurves();
  replayReadout();
}
function replayTick(ts){
  const m = LIVE.replay;
  if(!m || !REPLAY.playing) return;
  if(!REPLAY.last) REPLAY.last = ts;
  const dt = Math.min(0.25, (ts - REPLAY.last) / 1000);
  REPLAY.last = ts;
  REPLAY.t = Math.min(m.duration, REPLAY.t + dt * REPLAY.speed);
  replayDraw();
  if(REPLAY.t >= m.duration){
    REPLAY.playing = false;
    if($('#replayPlay')) $('#replayPlay').textContent = '重播';
    return;
  }
  REPLAY.raf = requestAnimationFrame(replayTick);
}
function replayToggle(){
  const m = LIVE.replay;
  if(!m) return;
  if(REPLAY.playing){
    REPLAY.playing = false;
    if(REPLAY.raf) cancelAnimationFrame(REPLAY.raf);
    REPLAY.raf = 0;
    if($('#replayPlay')) $('#replayPlay').textContent = '继续';
    return;
  }
  if(REPLAY.t >= m.duration) REPLAY.t = 0;
  REPLAY.playing = true;
  REPLAY.last = 0;
  if($('#replayPlay')) $('#replayPlay').textContent = '暂停';
  REPLAY.raf = requestAnimationFrame(replayTick);
}
function openReplay(model){
  if(!model){ toast('没有可回放的轨迹点'); return; }
  LIVE.replay = model;
  REPLAY.playing = false;
  REPLAY.t = 0;
  if($('#replayTitle')) $('#replayTitle').textContent = model.label || '轨迹回放';
  if($('#replayKicker')) $('#replayKicker').textContent = model.checkpoints.length
    ? (model.checkpoints.length + ' 个打卡点') : '无打卡点';
  if($('#replayNote')) $('#replayNote').textContent =
    '本地动画，不发任何请求 · ' + model.pts.length + ' 点 · ' +
    (model.distance / 1000).toFixed(2) + ' km';
  if($('#replayPlay')) $('#replayPlay').textContent = '播放';
  if($('#replaySpeed')) REPLAY.speed = +$('#replaySpeed').value || 4;
  if($('#replayModal')) $('#replayModal').hidden = false;
  // Canvas needs a laid-out box before it can size itself.
  setTimeout(() => {
    replayDraw();
    const close = $('#replayClose');
    if(close) close.focus();
    replayToggle();
  }, 40);
}
function openRecordReplay(){
  const d = LIVE.lastDetail;
  const model = d ? replayModelFromDetail(d) : null;
  openReplay(model);
}

/* ---------- page routing ---------- */
const PAGES = ['overview', 'accounts', 'run', 'routes', 'records', 'profile', 'settings'];
let runMapFitted = false;
function syncNavThumb(){
  const nav = document.querySelector('.nav');
  const thumb = $('#navThumb');
  if(!nav || !thumb) return;
  const active = nav.querySelector('.nav-item.active');
  if(!active){ thumb.style.height = '0px'; return; }
  thumb.style.height = active.offsetHeight + 'px';
  thumb.style.transform = 'translateY(' + active.offsetTop + 'px)';
}
function go(page){
  if(!PAGES.includes(page)) page = 'overview';
  document.querySelectorAll('.page').forEach(p =>
    p.classList.toggle('active', p.id === 'page-' + page));
  document.querySelectorAll('.nav-item').forEach(n =>
    n.classList.toggle('active', n.dataset.page === page));
  requestAnimationFrame(syncNavThumb);
  if(history.replaceState) history.replaceState(null, '', '#' + page);
  if(page === 'run'){
    requestAnimationFrame(syncRunTabThumb);
    setTimeout(() => {
      map.invalidateSize();
      if(!runMapFitted){ fitRunMap(); runMapFitted = true; }
      // Entering the run page is the operator's natural "is my campus still
      // current?" moment.  The backend has a 300 s cache, so this does not
      // hammer the points endpoint; 5 s only prevents double-fire from a
      // click that both navigates and switches the tab.
      if(hasSession() && pointsCooldownLeft() === 0 &&
         Date.now() - _pointsAutoAt > 5000){
        _pointsAutoAt = Date.now();
        loadPoints(false, {quiet: true});
      }
      // A school can change validTime between releases.  Refresh in the
      // background (throttled) so the time picker does not keep an old band.
      if(hasSession() && LIVE.rules && LIVE.rules.configured &&
         LIVE.rules.rules && LIVE.rules.rules.auto_fetch !== false &&
         Date.now() - (LIVE.rulesSyncedAt || 0) > 5 * 60 * 1000){
        syncSchoolRulesQuiet();
      }
      // Presets are a local list; one read when the page is first opened.
      // No polling: a save/delete refreshes explicitly.
      if(!LIVE.presetsLoaded && !methodMissing('real_presets')) loadPresets();
      refreshAuditButton();
    }, 80);
  }
  if(page === 'routes'){
    setTimeout(() => { routeMap.invalidateSize(); selectRoute(LIVE.selectedRoute); }, 80);
  }
  if(page === 'records'){
    if(!LIVE.receiptsLoaded) loadReceipts();
    setTimeout(() => { if(rdMap) rdMap.invalidateSize(); }, 80);
  }
  if(page === 'profile' && hasSession() && !LIVE.profile && !LIVE.profileLoading &&
     !LIVE.profileError){
    loadProfile({silent: true});
  }
  if(page === 'profile'){
    if(LIVE.violations) renderViolations(LIVE.violations);
    else if(LIVE.violationsError) renderViolations(null, LIVE.violationsError);
  }
  if(page === 'settings' && hasSession() &&
     !(LIVE.rules && (LIVE.rules.configured ||
        (LIVE.rules.rules && LIVE.rules.rules.auto_fetch === false)))){
    fetchSchoolRules({silent: true});
  }
  if(page === 'settings' && !LIVE.realism && !LIVE.realismLoading){
    loadRealism();
  }
  if(page === 'settings' && !LIVE.deviceCatalog && !LIVE.deviceCatalogLoading &&
     !methodMissing('real_device_options')){
    loadDeviceOptions();
  }
  if(page === 'records'){
    if($('#recDiagZip')) $('#recDiagZip').disabled = !hasSession();
    if($('#recFit')) $('#recFit').disabled = true;   // needs a selected record
  }
  if(page === 'overview'){
    if($('#inspectRefresh')){
      $('#inspectRefresh').disabled = !hasSession() ||
        methodMissing('real_inspection');
      $('#inspectRefresh').title = hasSession()
        ? (methodMissing('real_inspection') ? bridgeMessage({bridgeMissing: true})
                                            : '手动巡检一次')
        : '登录后才能巡检';
    }
    renderInspect();
  }
}
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => go(btn.dataset.page));
});
function followDataGoto(el){
  if(!el) return;
  if(el.dataset.modeTarget) setMode(el.dataset.modeTarget);
  const page = el.dataset.goto;
  if(page === 'run'){
    switchRunTab(el.dataset.rtab || 'route');
  }else if(el.dataset.rtab){
    switchRunTab(el.dataset.rtab);
  }
  if(page) go(page);
  if(el.dataset.focus){
    const id = el.dataset.focus;
    setTimeout(() => {
      const target = $('#' + id);
      if(target) target.focus();
    }, 90);
  }
  if(el.dataset.checkAction === 'refresh-points'){
    setTimeout(() => {
      if(typeof loadPoints === 'function') loadPoints(true, {quiet: false});
    }, 160);
  }
}
document.addEventListener('click', ev => {
  const t = eventTargetEl(ev);
  if(!t) return;
  const el = t.closest('[data-goto]');
  if(!el) return;
  if(el.tagName === 'A') ev.preventDefault();
  followDataGoto(el);
});
document.addEventListener('keydown', ev => {
  const t = eventTargetEl(ev);
  if(!t) return;
  const el = t.closest('[data-goto]');
  if(!el) return;
  if(ev.key === 'Enter' || ev.key === ' '){
    ev.preventDefault();
    followDataGoto(el);
  }
});
document.addEventListener('keydown', ev => {
  if(!document.querySelector('#page-run.active')) return;
  const t = eventTargetEl(ev);
  if(t && t.closest('input,textarea,select,[contenteditable="true"]')) return;
  if($('#dataPreview') && !$('#dataPreview').hidden) return;
  if($('#postSubmit') && !$('#postSubmit').hidden) return;
  if((ev.ctrlKey || ev.metaKey) && !ev.altKey &&
     String(ev.key || '').toLowerCase() === 'z'){
    ev.preventDefault();
    if(ev.shiftKey) redoRoute(); else undoRoute();
    return;
  }
  if((ev.key === 'Delete' || ev.key === 'Backspace') && selRoutePoint >= 0){
    ev.preventDefault();
    deleteRoutePoint(selRoutePoint);
    return;
  }
  if(ev.key === 'Escape' && selRoutePoint >= 0){
    ev.preventDefault();
    selRoutePoint = -1;
    paintDots();
    redraw();
  }
});
document.getElementById('newRunBtn').addEventListener('click', () => {
  go('run');
  switchRunTab('route');
});
window.addEventListener('hashchange', () => {
  const p = (location.hash || '#overview').slice(1);
  if(PAGES.includes(p)) go(p);
});
const startPage = (location.hash || '#overview').slice(1);


/* ============================================================
   Live bridge: the UI reads session / points / records / rules
   from the local Python bridge.  No synthetic rows are generated.
   ============================================================ */
const LIVE = {
  session: null, checked: null, points: [], fence: [], fences: [],
  fenceMissing: false, fenceCount: 0, pointsMissing: false, pointsEmpty: false,
  fenceError: '', pointsError: '', pointsDistance: 0, hitRadiusM: 25,
  pointsStale: false, pointsSource: '', pointsFitted: false,
  pointsCooldownUntil: 0, pointsCooldownKey: '', pointsAutoAt: 0,
  pointsLoading: false,
  runArea: null, runAreas: [], runAreaChoice: null, runAreaChoiceKey: '',
  runAreaPrefs: {},
  sequential: false,
  freezeSeconds: 0, faceRequired: false, faceReason: '',
  records: [], recordsLoaded: false, recordsLoading: false, recordFilter: 'all',
  routes: [], selectedRoute: -1,
  routeFilter: 'all', rules: null, rulesSyncedAt: 0, model: null, plan: null,
  modelWarnings: [],
  runDate: null, runDateTouched: false, daily: null,
  dailyCounts: {}, dailyCountsAt: 0,
  dailyCountsUid: null,
  recordsError: null, weekError: null, planError: null,
  semester: null, semesterError: null,
  profile: null, profileError: null, profileLoading: false, profileAt: 0,
  violations: null, violationsError: null, violationsAt: 0,
  receipts: [], receiptsLoaded: false, receiptsError: null,
  preflight: null, lastDetail: null,
  tokenUpdatedAt: null,
  log: [], submitting: false, lastSubmit: null, submitResult: null,
  loginCover: '', coverBusy: false,
  skin: 'paper',
  // v0.1.8 feature pack (P0–P2).  All of it is opt-in: nothing here is
  // fetched or sent until the operator clicks.
  loginUserId: '',
  presets: [], presetsLoaded: false, presetsError: null,
  realism: null, realismError: null, realismSaved: false,
  audit: null, auditBusy: false,
  explain: null,
  stats: null, statsBusy: false,
  inspect: null, inspectBusy: false,
  verify: null, verifyBusy: false,
  replay: null,
  logSel: -1,
  deviceCatalog: null, deviceCatalogError: null, deviceCatalogLoading: false
};

/* ---------------- appearance skins ---------------- */
const SKIN_META = {
  paper:   {name: '纸感手账', hint: '暖纸 · 手写 · 荧光笔'},
  poster:  {name: '赛场海报', hint: '粗体 · 硬边 · 信号红'},
  minimal: {name: '极简白',   hint: '留白 · 细线 · 单强调色'},
  archive: {name: '复古档案', hint: '牛皮纸 · 打字机 · 红章'}
};
const SKIN_KEY = 'hxp_skin';
function normalizeSkin(skin){
  return Object.prototype.hasOwnProperty.call(SKIN_META, skin)
    ? skin : 'paper';
}
function currentSkin(){
  return normalizeSkin(document.documentElement.getAttribute('data-skin'));
}
function renderSkinCards(){
  const id = currentSkin();
  document.querySelectorAll('.skin-card').forEach(card => {
    const on = card.dataset.skin === id;
    card.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  if($('#skinStatus')){
    $('#skinStatus').textContent = SKIN_META[id].name + ' · 已应用';
    $('#skinStatus').className = 'mini ok';
  }
  if($('#skinHint')){
    $('#skinHint').textContent = '当前：' + SKIN_META[id].name + ' · ' +
      SKIN_META[id].hint + '。切换后立即生效，自动保存在本机。';
  }
}
function refreshMapTheme(){
  const stamp = themeColor('--stamp', '#B6483C');
  const highlight = themeColor('--highlighter', '#F2D25C');
  const blue = themeColor('--ink-blue', '#2E4A7D');
  const paper = themeColor('--paper-2', '#FBF8F0');
  const green = themeColor('--green', '#4E7657');
  try{
    fenceLayer.setStyle({color: stamp, fillColor: stamp});
    extraFenceLayers.forEach(layer => layer.setStyle({color: stamp, fillColor: stamp}));
    hi.setStyle({color: highlight});
    ink.setStyle({color: blue});
    start.setStyle({color: paper, fillColor: blue});
    routeHi.setStyle({color: highlight});
    routeInk.setStyle({color: blue});
    routeStart.setStyle({color: paper, fillColor: blue});
    routeFenceLayer.setStyle({color: stamp, fillColor: stamp});
    routeFenceExtra.forEach(layer => layer.setStyle({color: stamp, fillColor: stamp}));
  }catch(e){}
  try{
    if(rdTrackGlow) rdTrackGlow.setStyle({color: highlight});
    if(rdTrackLine) rdTrackLine.setStyle({color: blue});
    if(rdStart) rdStart.setStyle({color: paper, fillColor: blue});
    if(rdEnd) rdEnd.setStyle({color: paper, fillColor: stamp});
    if(rdPtLayer) rdPtLayer.eachLayer(layer => {
      if(layer && layer.setStyle){
        layer.setStyle({color: paper,
                        fillColor: layer.__pass ? green : stamp});
      }
    });
  }catch(e){}
  try{ if(typeof paintDots === 'function') paintDots(); }catch(e){}
  refreshReplayColors();
  if(LIVE.replay && $('#replayModal') && !$('#replayModal').hidden){
    try{ replayDraw(); }catch(e){}
  }
}
function applySkin(skin, opts){
  opts = opts || {};
  const id = normalizeSkin(skin);
  LIVE.skin = id;
  document.documentElement.setAttribute('data-skin', id);
  try{ localStorage.setItem(SKIN_KEY, id); }catch(e){}
  renderSkinCards();
  refreshMapTheme();
  // A skin only changes presentation; do not fire a model-preview network
  // request just because the operator picked a color.
  if(typeof renderModel === 'function') renderModel();
  if(typeof refreshChecks === 'function') refreshChecks();
  if(opts.save){
    liveApi('real_set_ui_state', {skin: id}, 8000).then(j => {
      if(!liveOk(j)) liveLog('外观保存失败', j.message || '', 'warn');
    }).catch(() => {});
  }
  if(opts.toast) toast('已切换外观：' + SKIN_META[id].name);
}
function wireSkinCards(){
  document.querySelectorAll('.skin-card').forEach(card => {
    card.addEventListener('click', () => {
      const id = normalizeSkin(card.dataset.skin);
      if(id === currentSkin()) return;
      applySkin(id, {save: true, toast: true});
      liveLog('外观已切换', SKIN_META[id].name, 'ok');
    });
  });
  if($('#skinReset')){
    $('#skinReset').addEventListener('click', () => {
      applySkin('paper', {save: true, toast: true});
      liveLog('外观已恢复默认', '纸感手账', 'ok');
    });
  }
}

/* The bridge answers an unknown method with HTTP 404 + this message.  A
   missing method must show up as "未接入" on the card, never as a page error:
   this frontend ships ahead of the backend wiring on purpose. */
function bridgeMissing(j){
  if(!j) return true;
  if(j.bridgeMissing) return true;
  return j.code === 404 && /unknown api/i.test(String(j.message || ''));
}
function bridgeMessage(j){
  if(bridgeMissing(j)) return '该功能还没有接入本地 bridge（后端未就绪），升级或等待主程序更新';
  // The HTTP layer turns binding errors and exceptions into code 400/500.
  // Before v0.1.8 the native bridge surfaced the same thing as `error: 404`.
  if(j && j.error !== undefined && j.error !== null){
    return 'bridge ' + j.error + (j.message ? ('：' + j.message) : '');
  }
  return String((j && j.message) || '未知错误');
}
/* One request per explicit click.  Cards remember "not wired" so a second
   click does not repeat a request that cannot succeed. */
async function liveMaybe(method, args, timeoutMs){
  if(liveMaybe.missing && liveMaybe.missing.has(method)){
    return {code: 404, bridgeMissing: true, data: null,
            message: 'unknown api ' + method};
  }
  const j = await liveApi(method, args, timeoutMs);
  if(bridgeMissing(j)){
    liveMaybe.missing = liveMaybe.missing || new Set();
    liveMaybe.missing.add(method);
    return {code: 404, bridgeMissing: true, data: null,
            message: 'unknown api ' + method};
  }
  return j;
}

async function liveApi(method, args, timeoutMs){
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs || 60000);
  try{
    const r = await fetch('/bridge/' + method, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({args: args || {}}),
      signal: ctl.signal
    });
    const j = await r.json();
    return (j && typeof j === 'object') ? j : {code: 500, message: 'bridge 返回非 JSON'};
  }catch(e){
    return {code: 500, message: (e && e.name === 'AbortError')
      ? '本地 bridge 超时（后台可能仍在执行，请勿重复提交）'
      : ('本地 bridge 不可用：' + e)};
  }finally{
    clearTimeout(timer);
  }
}
const liveOk = j => !!(j && j.code === 200);
const hasSession = () => !!(LIVE.session &&
  (LIVE.session.token_present || LIVE.session.token) &&
  !LIVE.session.disabled);

/* ---------------- disclaimer gate ---------------- */
const DISCLAIMER_VERSION = 3;
const DISCLAIMER_CACHE_KEY = 'hxp.disclaimer.v3';
const DISCLAIMER = {version: 0, acceptedAt: '', loaded: false};
let DISCLAIMER_LAST_FOCUS = null;

function readDisclaimerCache(){
  try{
    const doc = JSON.parse(localStorage.getItem(DISCLAIMER_CACHE_KEY) || 'null');
    if(!doc || typeof doc !== 'object') return null;
    const version = +doc.version || 0;
    return version > 0 ? {version: version, at: doc.at || ''} : null;
  }catch(e){
    return null;
  }
}
function writeDisclaimerCache(doc){
  try{ localStorage.setItem(DISCLAIMER_CACHE_KEY, JSON.stringify(doc)); }catch(e){}
}
function clearDisclaimerCache(){
  try{ localStorage.removeItem(DISCLAIMER_CACHE_KEY); }catch(e){}
}
function disclaimerAccepted(){
  return +DISCLAIMER.version >= DISCLAIMER_VERSION;
}
function syncDisclaimerUI(){
  const box = $('#loginAgree');
  if(box) box.checked = disclaimerAccepted();
  const hint = $('#loginAgreeHint');
  if(hint) hint.hidden = disclaimerAccepted();
}
async function loadDisclaimerState(){
  let version = 0, at = '';
  try{
    const j = await liveApi('real_get_ui_state', {}, 8000);
    if(liveOk(j) && j.data){
      version = +j.data.disclaimer_version || 0;
      at = j.data.disclaimer_accepted_at || '';
    }
  }catch(e){}
  const cached = readDisclaimerCache();
  if(cached && +cached.version > version){
    version = +cached.version;
    at = cached.at || at;
  }
  DISCLAIMER.version = version;
  DISCLAIMER.acceptedAt = at;
  DISCLAIMER.loaded = true;
  syncDisclaimerUI();
  // Chrome may restore a checked checkbox after the first paint.  That is not
  // consent: re-assert the real stored state unless the user has clicked.
  setTimeout(() => {
    if(!disclaimerAccepted()) syncDisclaimerUI();
  }, 400);
  return disclaimerAccepted();
}
async function acceptDisclaimer(){
  DISCLAIMER.version = DISCLAIMER_VERSION;
  DISCLAIMER.acceptedAt = new Date().toISOString();
  DISCLAIMER.loaded = true;
  writeDisclaimerCache({version: DISCLAIMER_VERSION, at: DISCLAIMER.acceptedAt});
  syncDisclaimerUI();
  try{
    const j = await liveApi('real_set_ui_state',
      {disclaimer_version: DISCLAIMER_VERSION}, 8000);
    if(!liveOk(j)){
      liveLog('免责声明接受状态保存失败', j.message || '', 'warn');
    }
  }catch(e){
    liveLog('免责声明接受状态保存失败',
            e && e.message ? e.message : String(e), 'warn');
  }
}
async function revokeDisclaimer(){
  DISCLAIMER.version = 0;
  DISCLAIMER.acceptedAt = '';
  clearDisclaimerCache();
  syncDisclaimerUI();
  try{
    const j = await liveApi('real_set_ui_state', {disclaimer_version: 0}, 8000);
    if(!liveOk(j)){
      liveLog('免责声明撤回状态保存失败', j.message || '', 'warn');
    }
  }catch(e){
    liveLog('免责声明撤回状态保存失败',
            e && e.message ? e.message : String(e), 'warn');
  }
}
function toggleDisclaimerAgree(){
  const box = $('#loginAgree');
  if(!box) return;
  const row = document.querySelector('.login-agree-row');
  if(row) row.classList.remove('is-missing');
  if(box.checked){
    acceptDisclaimer();
  }else{
    revokeDisclaimer();
  }
}
function openDisclaimer(){
  const modal = $('#disclaimerModal');
  if(!modal) return;
  DISCLAIMER_LAST_FOCUS = document.activeElement;
  modal.hidden = false;
  document.body.classList.add('disclaimer-open');
  const gate = $('#loginGate'), app = $('#appShell');
  if(gate) gate.inert = true;
  if(app) app.inert = true;
  const close = $('#disclaimerClose');
  if(close) close.focus();
}
function closeDisclaimer(){
  const modal = $('#disclaimerModal');
  if(!modal) return;
  modal.hidden = true;
  document.body.classList.remove('disclaimer-open');
  const gate = $('#loginGate'), app = $('#appShell');
  if(gate) gate.inert = false;
  if(app) app.inert = document.body.classList.contains('login-locked');
  if(DISCLAIMER_LAST_FOCUS && typeof DISCLAIMER_LAST_FOCUS.focus === 'function'){
    try{ DISCLAIMER_LAST_FOCUS.focus(); }catch(e){}
  }
  DISCLAIMER_LAST_FOCUS = null;
}
function agreeDisclaimerFromModal(){
  const box = $('#loginAgree');
  if(box) box.checked = true;
  acceptDisclaimer();
  closeDisclaimer();
}
async function enforceDisclaimerGate(){
  // A pre-existing token must not skip the legal gate.  Local logout stops the
  // keeper and clears the token; the next login still has to pass the box.
  try{ await liveApi('real_logout', {local_only: true}, 10000); }catch(e){}
  LIVE.session = {}; LIVE.checked = null;
  setLoginBusy(false);
  showLoginGate('使用前请阅读并勾选《免责声明》，否则无法登录。');
  syncDisclaimerUI();
}
function afterBootAuth(){
  initRunDate();
  updateRunDateLabel();
  refreshChecks();
  refreshDailyCount();
  refreshModelPreview();
}

/* ---------------- login gate ---------------- */
const LOGIN = {visible: true, busy: false, timer: null, startedAt: 0};

function setLoginStatus(text, tone){
  const el = $('#loginStatus');
  if(!el) return;
  el.textContent = text || '';
  el.className = 'login-status' + (tone ? ' ' + tone : '');
}
function setLoginButton(label, busy){
  const btn = $('#loginBtn'), txt = $('#loginBtnText');
  if(txt) txt.textContent = label || '登录';
  if(btn){
    btn.disabled = !!busy;
    btn.setAttribute('aria-busy', busy ? 'true' : 'false');
  }
}
function setLoginBusy(on, text, tone){
  LOGIN.busy = !!on;
  setLoginButton(on ? '登录中…' : '登录', !!on);
  ['#loginUser', '#loginPass'].forEach(sel => {
    const el = $(sel);
    if(el) el.readOnly = !!on;
  });
  const eye = $('#loginEye');
  if(eye) eye.disabled = !!on;
  const remember = $('#loginRemember');
  if(remember) remember.disabled = !!on;
  if(on){
    const err = $('#loginError');
    if(err){ err.hidden = true; err.textContent = ''; }
    if(text) setLoginStatus(text, tone || 'busy');
  }else if(text !== undefined){
    setLoginStatus(text, tone || '');
  }
}
async function restoreSavedLogin(){
  try{
    const j = await liveApi('get_saved_login', {}, 5000);
    if(!liveOk(j)) return false;
    const d = j.data || {};
    const box = $('#loginRemember');
    if(!d.saved){
      if(box) box.checked = false;
      return false;
    }
    if(box) box.checked = true;
    const user = $('#loginUser'), pass = $('#loginPass');
    // Do not overwrite a password manager or an operator who started typing
    // while the bridge call was in flight.
    if(user && !user.value) user.value = d.account || '';
    if(pass && !pass.value) pass.value = d.password || '';
    return true;
  }catch(e){
    return false;
  }
}
async function rememberLoginChoice(account, password){
  const box = $('#loginRemember');
  const remember = !!(box && box.checked);
  try{
    const j = remember
      ? await liveApi('save_login', {account: account, password: password}, 8000)
      : await liveApi('clear_saved_login', {}, 5000);
    if(!liveOk(j)) throw new Error(j.message || '本地保存失败');
    return true;
  }catch(e){
    liveLog('记住密码保存失败', e && e.message ? e.message : String(e), 'warn');
    return false;
  }
}
async function forgetSavedLogin(){
  try{
    const j = await liveApi('clear_saved_login', {}, 5000);
    if(!liveOk(j)) throw new Error(j.message || '清除失败');
    return true;
  }catch(e){
    const box = $('#loginRemember');
    // The file may still be there; do not leave the UI claiming it forgot.
    if(box) box.checked = true;
    setLoginStatus('取消记住密码失败，请重试。', 'error');
    liveLog('清除本地保存失败', e && e.message ? e.message : String(e), 'warn');
    return false;
  }
}
function stopLoginClock(){
  if(LOGIN.timer){ clearInterval(LOGIN.timer); LOGIN.timer = null; }
}
function startLoginClock(){
  stopLoginClock();
  LOGIN.startedAt = Date.now();
  LOGIN.timer = setInterval(() => {
    if(!LOGIN.busy) return;
    const sec = Math.max(1, Math.floor((Date.now() - LOGIN.startedAt) / 1000));
    setLoginStatus('正在登录… 已等待 ' + sec + ' 秒（通常 5–15 秒）', 'busy');
  }, 1000);
}
function prettyLoginError(raw){
  let msg = String(raw || '登录失败，请重试。').trim();
  msg = msg.replace(/^登录失败\s*[:：]\s*/, '').replace(/^Error:\s*/, '');
  if(/10121/.test(msg)) return '这个账号还在手机端登录。请先在手机上退出，再回来登录。';
  if(/10175|账号或密码错误/.test(msg)) return '手机号或密码不对，请检查后重试。';
  if(/超时|timed out|timeout|bridge 超时/i.test(msg)) return '登录等待超时了。后台可能仍在处理，请稍等几秒再试。';
  if(/手动滑一下|手动验证/.test(msg)) return '自动验证没通过，请点下面的「手动滑一下」，自己把缺口对上。';
  if(/极验|gee|gt4|captcha/i.test(msg)) return '验证没有通过，请重试。';
  if(/bridge 不可用|Failed to fetch|NetworkError|ECONN|连接/i.test(msg)) return '连不上本地服务，请重启好想跑后再试。';
  return msg || '登录失败，请重试。';
}
function loginFail(msg, focusSelector){
  stopLoginClock();
  setLoginBusy(false);
  setLoginStatus('登录没有完成，请检查后重试。', 'error');
  const err = $('#loginError');
  if(err){ err.textContent = msg; err.hidden = false; }
  const pass = $('#loginPass');
  if(pass){
    pass.type = 'password';
    const eye = $('#loginEye');
    if(eye){
      eye.textContent = '显示';
      eye.setAttribute('aria-pressed', 'false');
      eye.setAttribute('aria-label', '显示密码');
    }
  }
  const target = $(focusSelector || '#loginPass') || pass;
  if(target){
    target.focus();
    try{ target.select(); }catch(e){}
  }
}
function showLoginGate(message, tone){
  const gate = $('#loginGate'), app = $('#appShell');
  if(!gate) return;
  try{ closeDatePop(); }catch(e){}
  document.title = '好想跑 · 登录';
  document.body.classList.add('login-locked');
  if(app){ app.setAttribute('aria-hidden', 'true'); app.inert = true; }
  gate.hidden = false;
  gate.classList.remove('leaving');
  LOGIN.visible = true;
  stopLoginClock();
  const passEl = $('#loginPass');
  if(passEl) passEl.type = 'password';
  const eyeEl = $('#loginEye');
  if(eyeEl){
    eyeEl.textContent = '显示';
    eyeEl.setAttribute('aria-pressed', 'false');
    eyeEl.setAttribute('aria-label', '显示密码');
    eyeEl.disabled = false;
  }
  setLoginBusy(false);
  setLoginStatus(message || '请输入手机号和密码登录。', tone || '');
  const err = $('#loginError');
  if(err){ err.hidden = true; err.textContent = ''; }
  setTimeout(() => {
    // The splash owns focus while it is on screen (the login gate is inert);
    // stealing it here would put typing into the hidden mobile field.
    if(LOGIN.visible && !LOGIN.busy && !$('#bootSplash')){
      const u = $('#loginUser'); if(u) u.focus();
    }
  }, 80);
}
/* ---------- login cover: settings → 登录页封面 ---------- */
const LOGIN_COVER_DEFAULT = 'login-bg.jpg';
function coverHintText(hasCustom, info){
  if(hasCustom && info && info.width){
    return (info.width + '×' + info.height + ' · ' + bytesLabel(info.size || 0) +
            ' · 只存在本机 data');
  }
  return 'JPG / PNG / WebP；自动压缩，只存本机 data。';
}
function applyLoginCover(url){
  const gate = $('#loginGate');
  if(gate){
    gate.classList.toggle('has-custom-cover', !!url);
    if(url) gate.style.backgroundImage = 'url("' + url + '")';
    else{
      gate.style.removeProperty('background-image');
      // login-cover.css is rendered by the server for the first paint; on a
      // reset it would keep the old cover, so drop the link and fall back to
      // the packaged login-bg.jpg.
      const link = document.querySelector('link[data-login-cover]');
      if(link) link.remove();
    }
  }
  const img = $('#coverPreview');
  if(img) img.src = url || LOGIN_COVER_DEFAULT;
}
function renderLoginCover(hasCustom, info){
  if($('#coverStatus')) $('#coverStatus').textContent = hasCustom ? '自定义横图' : '默认封面';
  if($('#coverHint')) $('#coverHint').textContent = coverHintText(hasCustom, info);
  if($('#coverReset')) $('#coverReset').disabled = !hasCustom;
}
async function loadLoginCover(){
  let j;
  try{ j = await liveApi('real_get_login_cover', {}, 8000); }catch(e){ return; }
  if(!liveOk(j) || !j.data) return;
  LIVE.loginCover = j.data.has_custom ? (j.data.url || '') : '';
  applyLoginCover(LIVE.loginCover);
  renderLoginCover(!!j.data.has_custom, j.data);
}
async function pickLoginCover(file){
  if(!file || LIVE.coverBusy) return;
  if(!/^image\//.test(file.type || '')){ toast('请选择图片文件'); return; }
  if(file.size > 20 * 1024 * 1024){ toast('图片太大，请控制在 20 MB 内'); return; }
  LIVE.coverBusy = true;
  const pick = $('#coverPick');
  if(pick){ pick.disabled = true; pick.textContent = '处理中…'; }
  const objUrl = URL.createObjectURL(file);
  try{
    const img = await new Promise((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error('图片解码失败，换一张试试'));
      el.src = objUrl;
    });
    if(!(img.naturalWidth > img.naturalHeight)){
      toast('只支持横图：宽度必须大于高度');
      if($('#coverHint')) $('#coverHint').textContent = '这张不是横图，已拒绝。';
      return;
    }
    if(img.naturalWidth > 8000 || img.naturalHeight > 8000){
      toast('图片尺寸过大（最大 8000 像素）');
      if($('#coverHint')) $('#coverHint').textContent = '图片尺寸过大（最大 8000 像素）。';
      return;
    }
    // Minimum *area*, not a minimum height: a 3200x600 banner is a perfectly
    // good cover even though the width cap shrinks it to 1920x360.
    if(img.naturalWidth < 800 ||
       img.naturalWidth * img.naturalHeight < 800 * 400){
      toast('图片太小：宽度至少 800，面积至少 800×400');
      if($('#coverHint')) $('#coverHint').textContent = '图片太小：宽度至少 800，面积至少 800×400。';
      return;
    }
    // Re-encode large photos so a 12 MP camera shot does not become a 20 MB
    // base64 POST.  A very wide banner (21:9 and beyond) would drop below the
    // server's area floor after the width cap; keep its original size instead.
    let scale = Math.min(1, 1920 / img.naturalWidth);
    if (img.naturalWidth * img.naturalHeight * scale * scale < 800 * 400) {
      scale = 1;
    }
    const w = Math.max(1, Math.round(img.naturalWidth * scale));
    const h = Math.max(1, Math.round(img.naturalHeight * scale));
    const canvas = document.createElement('canvas');
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext('2d');
    // JPEG has no alpha; a transparent PNG would otherwise become a black
    // rectangle.  Fill with the login gate's own colour first.
    ctx.fillStyle = '#1b1d20';
    ctx.fillRect(0, 0, w, h);
    ctx.drawImage(img, 0, 0, w, h);
    const base64 = (canvas.toDataURL('image/jpeg', 0.86).split(',')[1] || '');
    if(!base64){ toast('图片转换失败'); return; }
    if($('#coverHint')) $('#coverHint').textContent = '正在保存…';
    const j = await liveApi('real_set_login_cover', {
      data: base64, mime: 'image/jpeg', width: w, height: h
    }, 60000);
    if(!liveOk(j)){
      const msg = (j && j.message) || '封面保存失败';
      toast(msg);
      if($('#coverHint')) $('#coverHint').textContent = msg;
      return;
    }
    LIVE.loginCover = (j.data && j.data.url) || '';
    applyLoginCover(LIVE.loginCover);
    renderLoginCover(true, j.data || {});
    toast('登录页封面已更新');
    liveLog('登录页封面已更新', w + '×' + h, 'ok');
  }catch(e){
    toast(String((e && e.message) || e));
  }finally{
    URL.revokeObjectURL(objUrl);
    LIVE.coverBusy = false;
    if(pick){ pick.disabled = false; pick.textContent = '选择图片'; }
  }
}
async function resetLoginCover(){
  if(LIVE.coverBusy || !LIVE.loginCover) return;
  if(!confirm('恢复默认封面？当前自定义图片会从 data 里删除。')) return;
  LIVE.coverBusy = true;
  try{
    const j = await liveApi('real_clear_login_cover', {}, 15000);
    if(!liveOk(j)){ toast((j && j.message) || '恢复失败'); return; }
    LIVE.loginCover = '';
    applyLoginCover('');
    renderLoginCover(false, {});
    toast('已恢复默认封面');
  }catch(e){
    toast(String((e && e.message) || e));
  }finally{ LIVE.coverBusy = false; }
}

function enterApp(resetPage){
  const gate = $('#loginGate'), app = $('#appShell');
  if(!gate) return;
  stopLoginClock();
  LOGIN.visible = false;
  // The saved password is only needed to prefill the gate.  Once the app is
  // unlocked, do not leave it sitting in the hidden login DOM.
  const passEl = $('#loginPass');
  if(passEl) passEl.value = '';
  document.title = '好想跑 · 工作台';
  document.body.classList.remove('login-locked');
  if(app){ app.removeAttribute('aria-hidden'); app.inert = false; }
  try{
    if(document.activeElement && document.activeElement.closest('#loginGate')){
      document.activeElement.blur();
    }
  }catch(e){}
  gate.classList.add('leaving');
  setTimeout(() => {
    if(!LOGIN.visible){ gate.hidden = true; gate.classList.remove('leaving'); }
  }, 230);
  if(resetPage) go('overview');
  setTimeout(() => {
    try{ map.invalidateSize(); }catch(e){}
    try{ routeMap.invalidateSize(); }catch(e){}
  }, 280);
  // Fill the split table as soon as the workbench is actually visible; the
  // preview also runs on every parameter change, but the first login should
  // not require touching a field to see it.
  scheduleSplitsPreview();
  // First-run guide only makes sense after the login gate is gone.  The
  // acknowledgement lives in data/ui_state.json, not localStorage, because
  // app.py picks a fresh port on every launch.
  setTimeout(async () => {
    const guide = await maybeShowOnboard();
    if(!guide && window.Announcements) Announcements.maybeShow();
  }, 900);
  setTimeout(checkLastUpdateResult, 1200);
}
async function requestLogin(username, password){
  const j = await liveApi('real_login', {username, password}, 120000);
  if(!liveOk(j)) throw new Error(j.message || '登录失败');
  return j.data || {};
}
function fmtKeepaliveInterval(sec){
  sec = Math.max(1, Math.round(+sec || 21600));
  if(sec >= 3600) return Math.round(sec / 3600) + ' 小时';
  if(sec >= 60) return Math.round(sec / 60) + ' 分钟';
  return sec + ' 秒';
}
async function ensureKeepalive(){
  try{
    const st = await liveApi('real_keepalive_status');
    if(!liveOk(st)) return false;
    const d = st.data || {};
    if(d.disabled || !d.token_present) return false;
    if(d.running) return true;
    const j = await liveApi('real_keepalive_start', {
      interval_s: +d.interval_s || 21600,
      auto_relogin: false
    });
    if(liveOk(j)){
      liveLog('token 保活已自动开启', '每 ' + fmtKeepaliveInterval(d.interval_s) + '探测一次', 'ok');
      await loadKeepalive();
      return true;
    }
    liveLog('token 保活开启失败', j.message || '', 'warn');
  }catch(e){}
  return false;
}
function clearLoginError(){
  const err = $('#loginError');
  if(err){ err.textContent = ''; err.hidden = true; }
}

async function gateLogin(){
  if(LOGIN.busy) return;
  const user = ($('#loginUser') ? $('#loginUser').value : '').trim();
  const pass = $('#loginPass') ? $('#loginPass').value : '';
  if(!user){ loginFail('请输入手机号', '#loginUser'); return; }
  if(!pass){ loginFail('请输入密码', '#loginPass'); return; }
  const agree = $('#loginAgree');
  // Both checks matter: a browser-restored checkbox is not user consent.
  if(!agree || !agree.checked || !disclaimerAccepted()){
    if(agree) agree.checked = false;
    const hint = $('#loginAgreeHint');
    if(hint) hint.hidden = false;
    loginFail('请先阅读并勾选《免责声明》，否则无法登录。', '#loginAgree');
    const row = document.querySelector('.login-agree-row');
    if(row){
      row.classList.add('is-missing');
      setTimeout(() => row.classList.remove('is-missing'), 1200);
    }
    return;
  }
  clearLoginError();
  const manualBtn = $('#loginManual');
  if(manualBtn) manualBtn.hidden = true;
  setLoginBusy(true, '正在登录… 通常 5–15 秒', 'busy');
  startLoginClock();
  liveLog('登录中', '正在验证账号', 'info');
  try{
    const profile = await requestLogin(user, pass);
    await finishLogin(profile, user, pass);
  }catch(e){
    const raw = e && e.message ? e.message : String(e);
    // Only offer the manual widget when the captcha is what actually failed;
    // showing it after a wrong password would just confuse people.
    if(manualBtn && /极验|gee|gt4|captcha|10003|滑块/i.test(raw)){
      manualBtn.hidden = false;
    }
    loginFail(prettyLoginError(raw));
  }
}

async function finishLogin(profile, user, pass){
  const rememberBox = $('#loginRemember');
  const wantedRemember = !!(rememberBox && rememberBox.checked);
  const remembered = await rememberLoginChoice(user, pass);
  const passEl = $('#loginPass');
  if(passEl) passEl.value = '';
  clearAccountActivity();
  liveLog('登录成功', profile.name || profile.campusName || '会话已保存', 'ok');
  stopLoginClock();
  setLoginStatus('登录成功，正在读取你的账号…', 'ok');
  const res = await liveInitSession();
  if(!res || res.state !== 'app'){
    throw new Error((res && res.message) || '登录后仍无法验证会话');
  }
  await loadRules();
  ensureKeepalive();
  syncSchoolRulesQuiet();
  setLoginStatus('已登录，正在进入工作台…', 'ok');
  setLoginButton('正在进入…', true);
  LOGIN.busy = true;
  setTimeout(() => {
    LOGIN.busy = false;
    setLoginBusy(false);
    enterApp(true);
    if(wantedRemember && !remembered){
      toast('已登录，但本地保存密码失败；下次需要重新输入');
    }
  }, 320);
}

// ---- manual captcha: protocol-level slider drawn by the app itself ----
//
// GeeTest's own `gt4.js` cannot be used here.  On the gcaptcha4.js build
// 1.9.8-0d53fd the `embed` product throws inside slide.init (TypeError: ...
// is not a function) and `float` does call back and reach onReady, yet inserts
// zero nodes -- measured in both the automation Chromium and the app's own
// WebView2: 15 elements total, 0 canvas, 0 img, while /load, bg and slice all
// returned 200.  A button that opens an empty box is not a fallback.
//
// So the fallback works one level lower.  The bridge fetches the same /load
// and hands us the same two PNGs; the operator drags the slice onto the gap;
// the offset goes back and the bridge builds the same `w` the solver would
// have built.  Nothing here depends on GeeTest's JS, so it cannot break the
// way the widget did, and a failure still leaves the usual diagnostics.
const GT4 = {
  token: '', busy: false, ready: false, dragging: false, pointerId: null,
  ratio: 0, bgWidth: 0, bgHeight: 0, sliceWidth: 0, sliceHeight: 0,
  gapTop: 0, maxLeft: 0, scale: 1
};

function setGt4Status(msg, kind){
  const el = $('#gt4Status');
  if(!el) return;
  el.textContent = msg || '';
  el.className = 'gt4-status' + (kind ? ' ' + kind : '');
}

function gt4SetEnabled(on){
  GT4.ready = !!on;
  const track = $('#gt4Track');
  if(!track) return;
  track.classList.toggle('is-disabled', !on);
  track.setAttribute('aria-disabled', on ? 'false' : 'true');
}

// Park the slice at `ratio` of its travel and mirror that on the track.
// Both are driven from the same number, so what the operator sees aligned is
// exactly what gets sent: the ratio is only ever converted once, at commit.
function gt4Place(r){
  GT4.ratio = Math.min(1, Math.max(0, +r || 0));
  const sl = $('#gt4Slice');
  const track = $('#gt4Track'), knob = $('#gt4Knob'), fill = $('#gt4Fill');
  if(sl && GT4.maxLeft > 0){
    sl.style.transform =
      'translateX(' + (GT4.ratio * GT4.maxLeft * GT4.scale).toFixed(2) + 'px)';
  }
  if(track && knob){
    const travel = Math.max(0, track.clientWidth - knob.offsetWidth);
    const x = GT4.ratio * travel;
    knob.style.transform = 'translateX(' + x.toFixed(2) + 'px)';
    if(fill) fill.style.width = (knob.offsetWidth / 2 + x).toFixed(2) + 'px';
    track.setAttribute('aria-valuenow', String(Math.round(GT4.ratio * 100)));
  }
}

// Size the slice in display pixels using the background's own scale, so the
// overlay stays pixel-accurate at any card width.  `top` is the row the gap
// sits on: the piece only travels horizontally, but drawing it at 0 leaves it
// visually nowhere near the notch even when the x is perfect.
function gt4Layout(){
  const stage = $('#gt4Stage'), sl = $('#gt4Slice');
  if(!stage || !sl || !GT4.bgWidth) return;
  GT4.scale = stage.clientWidth / GT4.bgWidth;
  sl.style.width = (GT4.sliceWidth * GT4.scale).toFixed(2) + 'px';
  sl.style.height = (GT4.sliceHeight * GT4.scale).toFixed(2) + 'px';
  sl.style.top = (GT4.gapTop * GT4.scale).toFixed(2) + 'px';
  gt4Place(GT4.ratio);
}

function gt4Reset(){
  GT4.token = '';
  GT4.ratio = 0;
  GT4.dragging = false;
  GT4.pointerId = null;
  GT4.bgWidth = GT4.bgHeight = 0;
  GT4.sliceWidth = GT4.sliceHeight = 0;
  GT4.gapTop = 0;
  GT4.maxLeft = 0;
  const bg = $('#gt4Bg'), sl = $('#gt4Slice'), stage = $('#gt4Stage');
  if(bg) bg.removeAttribute('src');
  if(sl){ sl.removeAttribute('src'); sl.style.top = '0px'; }
  if(stage){
    stage.classList.add('is-empty');
    stage.style.aspectRatio = '';
  }
  gt4SetEnabled(false);
  gt4Place(0);
  const retry = $('#gt4Retry');
  if(retry) retry.hidden = true;
}

function closeManualCaptcha(){
  // A submit is in flight: closing now would hide the only place the result
  // can be shown, and the request would still finish in the background.
  if(GT4.busy) return;
  const modal = $('#gt4Modal');
  if(modal) modal.hidden = true;
  const token = GT4.token;
  GT4.token = '';
  // Free the cached challenge instead of leaving two PNGs parked in RAM
  // until the TTL fires.
  if(token) liveApi('real_gt4_manual_cancel', {token: token}, 5000).catch(() => {});
  gt4Reset();
  setGt4Status('', '');
}

async function openManualCaptcha(){
  if(GT4.busy) return;
  const modal = $('#gt4Modal');
  if(!modal || !modal.hidden) return;
  const user = ($('#loginUser') ? $('#loginUser').value : '').trim();
  const pass = $('#loginPass') ? $('#loginPass').value : '';
  if(!user || !pass){ loginFail('请先填好手机号和密码，再手动验证'); return; }
  // Open before awaiting the bridge: a second click must see the modal and
  // bail out instead of fetching a second challenge.
  modal.hidden = false;
  gt4Reset();
  setGt4Status('正在读取题目…', 'busy');
  await gt4LoadChallenge();
}

// `img.decode()` hangs forever on an already-decoded data URL in this
// Chromium/WebView2 build (measured: complete=true, naturalWidth=300, promise
// still pending after 3s), which parked the dialog on "正在读取题目…" with a
// dead slider.  The element's own load/error events are the only signal
// needed, and a timeout keeps a stuck decoder from blocking the dialog.
function gt4ImageReady(img){
  if(img.complete && img.naturalWidth) return Promise.resolve(true);
  return new Promise(resolve => {
    let done = false;
    let timer = null;
    const finish = ok => {
      if(done) return;
      done = true;
      img.removeEventListener('load', onload);
      img.removeEventListener('error', onerror);
      if(timer) clearTimeout(timer);
      resolve(ok);
    };
    const onload = () => finish(true);
    const onerror = () => finish(false);
    img.addEventListener('load', onload);
    img.addEventListener('error', onerror);
    timer = setTimeout(() => finish(!!img.naturalWidth), 4000);
  });
}

async function gt4LoadChallenge(){
  const retry = $('#gt4Retry');
  if(retry) retry.hidden = true;
  const j = await liveApi('real_gt4_manual_start', {}, 40000);
  const modal = $('#gt4Modal');
  if(!modal || modal.hidden){
    // Cancelled while the bridge was fetching: do not leak the challenge.
    if(liveOk(j) && j.data && j.data.token){
      liveApi('real_gt4_manual_cancel', {token: j.data.token}, 5000).catch(() => {});
    }
    return;
  }
  if(!liveOk(j)){
    setGt4Status((j && j.message) || '题目读取失败', 'error');
    if(retry) retry.hidden = false;
    return;
  }
  const d = j.data || {};
  GT4.token = d.token || '';
  GT4.bgWidth = +d.bg_width || 0;
  GT4.bgHeight = +d.bg_height || 0;
  GT4.sliceWidth = +d.slice_width || 0;
  GT4.sliceHeight = +d.slice_height || 0;
  GT4.gapTop = +d.slice_top || 0;
  GT4.maxLeft = Math.max(0, +d.max_left || 0);
  const bg = $('#gt4Bg'), sl = $('#gt4Slice'), stage = $('#gt4Stage');
  if(!bg || !sl || !stage){ setGt4Status('页面缺少验证码容器', 'error'); return; }
  bg.src = d.bg || '';
  sl.src = d.slice || '';
  if(GT4.bgWidth > 0 && GT4.bgHeight > 0){
    stage.style.aspectRatio = GT4.bgWidth + ' / ' + GT4.bgHeight;
  }
  stage.classList.remove('is-empty');
  gt4Place(0);
  await Promise.all([gt4ImageReady(bg), gt4ImageReady(sl)]);
  gt4Layout();
  if(!bg.naturalWidth || !sl.naturalWidth){
    setGt4Status('验证图片没有加载出来，换一张试试', 'error');
    if(retry) retry.hidden = false;
    return;
  }
  if(GT4.maxLeft <= 0){
    setGt4Status('这张图没有可用的缺口，换一张试试', 'error');
    if(retry) retry.hidden = false;
    return;
  }
  gt4SetEnabled(true);
  setGt4Status('把小图拖到缺口上，松手就提交', '');
}

// The pointer position -> travel ratio.  The knob's centre is the handle, so
// a click at the track's left edge lands on 0 and at the right edge on 1.
function gt4RatioFromEvent(ev){
  const track = $('#gt4Track'), knob = $('#gt4Knob');
  if(!track || !knob) return GT4.ratio;
  const rect = track.getBoundingClientRect();
  const travel = Math.max(1, rect.width - knob.offsetWidth);
  return Math.min(1, Math.max(0,
    (ev.clientX - rect.left - knob.offsetWidth / 2) / travel));
}

function gt4PointerDown(ev){
  if(!GT4.ready || GT4.busy) return;
  if(ev.button !== undefined && ev.button !== 0) return;
  ev.preventDefault();
  GT4.dragging = true;
  GT4.pointerId = ev.pointerId;
  const track = $('#gt4Track');
  try{ track.setPointerCapture(ev.pointerId); }catch(e){}
  gt4Place(gt4RatioFromEvent(ev));
}

function gt4PointerMove(ev){
  if(!GT4.dragging || ev.pointerId !== GT4.pointerId) return;
  ev.preventDefault();
  gt4Place(gt4RatioFromEvent(ev));
}

function gt4PointerUp(ev){
  if(!GT4.dragging) return;
  if(ev.pointerId !== undefined && ev.pointerId !== GT4.pointerId) return;
  GT4.dragging = false;
  const track = $('#gt4Track');
  try{ track.releasePointerCapture(GT4.pointerId); }catch(e){}
  GT4.pointerId = null;
  // pointercancel has no usable coordinates; keep the last good ratio.
  if(typeof ev.clientX === 'number' && ev.type !== 'pointercancel'){
    gt4Place(gt4RatioFromEvent(ev));
  }
  gt4Commit();
}

function gt4KeyDown(ev){
  if(!GT4.ready || GT4.busy) return;
  const step = ev.shiftKey ? 0.02 : 0.005;
  if(ev.key === 'ArrowLeft'){
    ev.preventDefault(); gt4Place(GT4.ratio - step);
  }else if(ev.key === 'ArrowRight'){
    ev.preventDefault(); gt4Place(GT4.ratio + step);
  }else if(ev.key === 'Enter' || ev.key === ' '){
    ev.preventDefault(); gt4Commit();
  }
}

async function gt4Commit(){
  if(GT4.busy || !GT4.ready || !GT4.token) return;
  const user = ($('#loginUser') ? $('#loginUser').value : '').trim();
  const pass = $('#loginPass') ? $('#loginPass').value : '';
  if(!user || !pass){
    setGt4Status('手机号或密码空了，请取消后重新填写', 'error');
    return;
  }
  // One conversion, in image pixels: the same unit `locate_gap` returns, so
  // the payload builder cannot tell this from a solver run.
  const setLeft = Math.round(GT4.ratio * GT4.maxLeft);
  GT4.busy = true;
  gt4SetEnabled(false);
  setGt4Status('验证中…', 'busy');
  try{
    const j = await liveApi('real_gt4_manual_finish', {
      token: GT4.token, set_left: setLeft,
      username: user, password: pass
    }, 120000);
    GT4.token = '';                 // the bridge consumed it either way
    if(!liveOk(j)){
      GT4.busy = false;
      const msg = (j && j.message) || '验证没有通过';
      // The captcha passed and the server rejected the account instead:
      // keeping the dialog up would hide the real problem behind a puzzle the
      // operator can no longer retry (the challenge is already consumed).
      if(/10175|10121|10007|账号或密码|风控/.test(msg)){
        closeManualCaptcha();
        loginFail(prettyLoginError(msg));
        return;
      }
      setGt4Status(msg, 'error');
      const retry = $('#gt4Retry');
      if(retry) retry.hidden = false;
      return;
    }
    // Keep the dialog open until the session is verified: finishLogin can
    // still fail on liveInitSession, and closing first would swallow that.
    try{
      await finishLogin(j.data || {}, user, pass);
    }catch(e){
      GT4.busy = false;
      closeManualCaptcha();
      loginFail(prettyLoginError(e && e.message ? e.message : e));
      return;
    }
    GT4.busy = false;
    closeManualCaptcha();
    clearLoginError();
  }catch(e){
    GT4.busy = false;
    setGt4Status('登录失败：' + (e.message || e), 'error');
    gt4SetEnabled(true);
  }
}

function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[c]));
}
async function copyPlainText(text){
  // Clipboard API first; fall back to execCommand for WebView2/HTTP contexts
  // where navigator.clipboard is missing or permission is denied.
  try{
    if(navigator.clipboard && navigator.clipboard.writeText){
      await navigator.clipboard.writeText(text);
      return true;
    }
  }catch(e){}
  const prev = document.activeElement;
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '0';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try{ ok = document.execCommand('copy'); }catch(e){ ok = false; }
  ta.remove();
  if(prev && prev.focus){
    try{ prev.focus({preventScroll: true}); }catch(e){}
  }
  return ok;
}
function hms(sec){
  sec = Math.max(0, Math.round(+sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}
function paceText(p){
  return _paceText(p);
}
function paceFromSec(sec, km){
  sec = +sec || 0; km = +km || 0;
  return km > 0 ? sec / 60 / km : 0;
}
function shortDate(ms){
  if(!ms) return '—';
  const d = new Date(ms + 8 * 3600 * 1000);
  return String(d.getUTCMonth() + 1).padStart(2, '0') + '/' +
         String(d.getUTCDate()).padStart(2, '0');
}
function beijingParts(){
  const d = new Date(Date.now() + 8 * 3600 * 1000);
  return {
    iso: d.toISOString().slice(0, 10),
    month: d.getUTCMonth() + 1,
    day: d.getUTCDate(),
    weekday: '周' + '日一二三四五六'[d.getUTCDay()]
  };
}
function greetingText(){
  const h = beijingNowDate().getHours();
  if(h < 5) return '夜深了';
  if(h < 12) return '早上好';
  if(h < 18) return '下午好';
  return '晚上好';
}
function refreshDateHeader(){
  // The window may stay open across midnight.  Keep the greeting/date and
  // "today" data honest without re-rendering the whole account card.
  const bp = beijingParts();
  const s = LIVE.session || {};
  const logged = !!((s.token_present || s.token) && !s.disabled);
  const name = logged ? (s.nickname || s.name || '') : '';
  if($('#ovGreeting')) $('#ovGreeting').textContent = greetingText() + (name ? '，' + name : '');
  if($('#ovDate')) $('#ovDate').textContent = bp.iso + ' ' + bp.weekday;
  if($('#ovTodayEyebrow')){
    $('#ovTodayEyebrow').textContent = '今天 · ' + bp.month + ' 月 ' + bp.day + ' 日';
  }
  const dayChanged = !!(_headerDay && _headerDay !== bp.iso);
  _headerDay = bp.iso;
  if(!dayChanged || LOGIN.visible || !hasSession()) return;
  try{ closeDatePop(); }catch(e){}
  if(!LIVE.runDateTouched) LIVE.runDate = beijingTodayKey();
  LIVE.daily = null;
  LIVE.dailyCounts = {}; LIVE.dailyCountsAt = 0; LIVE.dailyCountsUid = null;
  updateRunDateLabel();
  updateTodayNav();
  refreshChecks();
  loadOverview();
  refreshDailyCount();
}
function nowHM(){
  // Logs are read alongside server timestamps; always show Beijing time even
  // when the desktop is set to another timezone.
  const d = beijingNowDate();
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
function beijingStamp(ts){
  if(!ts) return '—';
  try{
    return new Date(ts * 1000).toLocaleString('zh-CN', {
      timeZone: 'Asia/Shanghai', hour12: false
    });
  }catch(e){
    return new Date(ts * 1000).toLocaleString('zh-CN', {hour12: false});
  }
}
function maskToken(t){
  t = String(t || '');
  return t ? t.slice(0, 4) + '…' + t.slice(-4) : '—';
}

function liveLog(msg, detail, level){
  level = level || 'info';
  LIVE.log.push({time: nowHM(), msg: msg || '', detail: detail || '', level: level});
  if(LIVE.log.length > 100) LIVE.log.shift();
  renderLog();
}
function renderLog(){
  const list = $('#logEvents');
  if(!list) return;
  const filter = (document.querySelector('.log-filter.active') || {}).dataset?.logFilter || 'all';
  list.innerHTML = LIVE.log.map((e, idx) => {
    const show = filter === 'all' ||
      (filter === 'warn' && (e.level === 'warn' || e.level === 'wait')) ||
      (filter === 'err' && e.level === 'err');
    return `<li class="log-event ${esc(e.level)}" data-level="${esc(e.level)}" data-log-index="${idx}" ${show ? '' : 'hidden'}>
      <span class="log-time">${esc(e.time)}</span><span class="log-dot"></span>
      <div><div class="log-msg">${esc(e.msg)}</div><div class="log-detail">${esc(e.detail)}</div></div>
    </li>`;
  }).join('');
  const warn = LIVE.log.filter(e => e.level === 'warn' || e.level === 'wait').length;
  const err = LIVE.log.filter(e => e.level === 'err').length;
  const ok = LIVE.log.filter(e => e.level === 'ok').length;
  const total = LIVE.log.length;
  if($('#logSub')) $('#logSub').textContent = total ? (total + ' 条 · ' + warn + ' 提醒 · ' + err + ' 错误') : '还没有事件';
  if($('#logChips')) $('#logChips').innerHTML = `<span class="tag ok">${ok} 成功</span><span class="tag warn">${warn} 提醒</span><span class="tag">${err} 错误</span>`;
  if($('#logCount')) $('#logCount').textContent = String(total);
  if($('#logSession')) $('#logSession').textContent = hasSession() ? (LIVE.session.campusName || '已登录') : (LIVE.session && LIVE.session.disabled ? '已禁用' : '未登录');
  if($('#logModel')) $('#logModel').textContent = LIVE.model && LIVE.model.configured ? (LIVE.model.score + ' 分') : '—';
  if($('#logSubmit')) $('#logSubmit').textContent = LIVE.lastSubmit && LIVE.lastSubmit.rrid ? ('rrid ' + LIVE.lastSubmit.rrid) : '未提交';
  document.querySelectorAll('.log-filter').forEach(b => {
    if(b.dataset.logFilter === 'warn') b.textContent = '提醒 ' + warn;
    if(b.dataset.logFilter === 'err') b.textContent = '错误 ' + err;
    if(b.dataset.logFilter === 'all') b.textContent = '全部 ' + total;
  });
}

function clearAccountActivity(){
  // Switching accounts must not leave the previous operator's run log,
  // rrid, plan, records or model in the UI.  Local route-library files stay
  // as-is.  Bump the request sequences first: a reply that was already in
  // flight for the old account must not land after this point.
  invalidateLiveRequests();
  LIVE.log = [];
  LIVE.checked = null;
  LIVE.tokenUpdatedAt = null;
  LIVE.lastSubmit = null;
  LIVE.submitResult = null;
  if(typeof renderSubmitResult === 'function') renderSubmitResult();
  LIVE.pointsLoading = false;
  LIVE.pointsCooldownUntil = 0;
  LIVE.pointsCooldownKey = '';
  if(typeof stopPointsCooldownTick === 'function') stopPointsCooldownTick();
  if(typeof syncPointsCooldownUI === 'function') syncPointsCooldownUI();
  LIVE.plan = null;
  LIVE.daily = null;
  LIVE.model = null;
  LIVE.modelWarnings = [];
  LIVE.records = []; LIVE.recordsLoaded = false; LIVE.recordsLoading = false;
  LIVE.week = null; LIVE.weekLoaded = false;
  LIVE.weekError = null;
  LIVE.planError = null;
  LIVE.recordsError = null;
  LIVE.semester = null; LIVE.semesterError = null;
  LIVE.profile = null; LIVE.profileError = null; LIVE.profileLoading = false;
  LIVE.profileAt = 0;
  LIVE.violations = null; LIVE.violationsError = null; LIVE.violationsAt = 0;
  LIVE.receipts = []; LIVE.receiptsLoaded = false; LIVE.receiptsError = null;
  LIVE.dailyCounts = {}; LIVE.dailyCountsAt = 0; LIVE.dailyCountsUid = null;
  LIVE.lastDetail = null;
  // v0.1.8 per-account panels: audit results, verification timelines, stats,
  // inspection and the error note all describe the old account.
  LIVE.logSel = -1;
  LIVE.audit = null;
  LIVE.auditShowAll = false;
  LIVE.verify = null;
  LIVE.stats = null;
  LIVE.inspect = null;
  LIVE.explain = null;
  REPLAY.playing = false;
  if(REPLAY.raf) cancelAnimationFrame(REPLAY.raf);
  REPLAY.raf = 0;
  if($('#replayModal')) $('#replayModal').hidden = true;
  if($('#logExplainBox')) $('#logExplainBox').hidden = true;
  if($('#logExplain')) $('#logExplain').disabled = true;
  if($('#ovErrorNote')) $('#ovErrorNote').hidden = true;
  if($('#submitVerify')){ $('#submitVerify').hidden = true; $('#submitVerify').innerHTML = ''; }
  if($('#rdVerifyBox')){ $('#rdVerifyBox').hidden = true; $('#rdVerifyBox').innerHTML = ''; }
  if($('#statsPanel')) $('#statsPanel').hidden = true;
  if(typeof renderAudit === 'function') renderAudit();
  if(typeof renderStats === 'function'){ LIVE.stats = null; renderStats(); }
  if(typeof renderInspect === 'function'){ renderInspect(); }
  if(typeof renderPresets === 'function') renderPresets();
  if($('#inspectRefresh')) $('#inspectRefresh').disabled = true;
  if($('#recDiagZip')) $('#recDiagZip').disabled = true;
  if($('#recFit')) $('#recFit').disabled = true;
  renderLog();
  renderRecords();
  renderProfileEmpty();
}

/* ---------------- session / account ---------------- */
function deviceLabelText(s){
  s = s || {};
  const os = s.device_os
    ? ((s.device_platform === 'ios' ? 'iOS ' : 'Android ') + s.device_os)
    : '';
  return [s.device_name, os].filter(Boolean).join(' · ');
}
function renderSession(){
  const s = LIVE.session || {};
  const logged = !!( (s.token_present || s.token) && !s.disabled );
  // Do not show a stale name/campus after logout or reload: the session file
  // may retain the display name, but there is no usable token.
  const name = logged ? (s.nickname || s.name || '') : '';
  const display = s.disabled ? '旧账号已停用' : (logged ? (name || ('uid ' + (s.uid || '—'))) : '未登录');
  const verified = !!(LIVE.checked && liveOk(LIVE.checked));
  const avatar = (name || (s.disabled ? '!' : '?')).slice(0, 1);
  $('#sideAvatar').textContent = avatar;
  $('#sideName').textContent = display;
  $('#sideMeta').textContent = s.disabled ? '旧会话已停用' :
    (logged ? ((s.campusName || '已加载会话') + ' · token') : '等待登录');
  $('#sideCampus').textContent = logged ? '运动世界 · 本地工作台' : '本地工作台';

  $('#accAvatar').textContent = avatar;
  $('#accName').textContent = display;
  $('#accMeta').textContent = s.disabled ? '该会话已按用户要求停用' :
    (logged ? ([s.schoolName, s.campusName].filter(Boolean).join(' · ') || '登录后显示学校 / 校区') : '登录后显示学校 / 校区');
  $('#accCount').textContent = s.disabled ? '已禁用' : (logged ? '1 个会话' : '未登录');
  $('#accTopStatus').textContent = s.disabled ? '已禁用' : (logged ? (verified ? 'token 有效' : '已加载') : '未登录');
  $('#accTopStatus').className = 'chip ' + (s.disabled ? 'red' : (logged ? (verified ? 'ok' : 'warn') : ''));
  $('#accTokenChip').textContent = s.disabled ? '账号已禁用' : (logged ? (verified ? 'token 有效' : 'token 已加载') : 'token 未加载');
  $('#accTokenChip').className = 'chip ' + (s.disabled ? 'red' : (logged ? (verified ? 'ok' : 'warn') : ''));
  const hasDevice = !!s.device_id_present;
  const deviceSource = {
    generated: '本机随机',
    'legacy-default': '旧版冻结',
    'legacy-session': '旧版会话',
    rotated: '已更换',
    custom: '自定义',
    migrated: '旧版已修复',
    repaired: '已修正',
    session: '手动',
    error: '读取失败'
  }[s.device_source] || '已冻结';
  const deviceLabel = deviceLabelText(s);
  if($('#setDeviceStatus')){
    $('#setDeviceStatus').textContent = s.device_source === 'error'
      ? '设备读取失败' : ('设备 · ' + deviceSource);
    $('#setDeviceStatus').className = 'mini' +
      (s.device_source === 'error' ? ' risk-bad' : '');
    $('#setDeviceStatus').title = s.device_source === 'error'
      ? (s.device_error || '设备指纹读取失败')
      : (s.device_platform === 'ios'
         ? 'iOS 链路已真机验证（2026-10-25）；登录、提交和 OBS 共用同一台设备。'
         : '设备指纹在本机生成一次，登录和提交共用；不要频繁更换。');
  }
  $('#accUidChip').textContent = s.uid ? ('uid ' + s.uid) : 'uid —';
  $('#accDisabledChip').hidden = !s.disabled;
  $('#accTokenMasked').textContent = logged
    ? (s.token_masked || maskToken(s.token)) : '—';
  if($('#setDeviceProfile')) $('#setDeviceProfile').textContent = hasDevice ? (deviceLabel || '—') : '—';
  if($('#setDeviceId')) $('#setDeviceId').textContent = hasDevice
    ? (s.device_id_masked || '—') : '—';
  if($('#setDevicePlatform')) $('#setDevicePlatform').textContent = hasDevice
    ? (s.device_platform === 'ios' ? 'iOS' : 'Android') : '—';
  if($('#setDeviceScreen')){
    const screen = [s.device_physic_pixel, s.device_logic_pixel].filter(Boolean).join(' / ');
    $('#setDeviceScreen').textContent = hasDevice ? (screen || '—') : '—';
  }
  if($('#setDeviceScale')){
    const bits = [];
    if(s.device_scale) bits.push('@' + s.device_scale + 'x');
    if(+s.device_installed_days > 0) bits.push('安装 ' + s.device_installed_days + ' 天');
    $('#setDeviceScale').textContent = hasDevice ? (bits.join(' · ') || '—') : '—';
  }
  if($('#setDeviceSource')) $('#setDeviceSource').textContent = hasDevice ? deviceSource : '—';
  if($('#setDeviceEnv')){
    const ok = s.device_environment_ok;
    $('#setDeviceEnv').textContent = hasDevice
      ? (ok === true ? '正常' : (ok === false ? '异常' : '未知')) : '—';
    $('#setDeviceEnv').className = hasDevice && ok === false ? 'risk-bad' : '';
    $('#setDeviceEnv').title = hasDevice && s.device_cpu
      ? ('CPU ' + s.device_cpu + ' · root=' + (s.device_is_root || '0'))
      : '';
  }
  if($('#setDeviceUa')) $('#setDeviceUa').textContent = hasDevice ? (s.device_ua || '—') : '—';
  $('#accCampus').textContent = logged ? (s.campusName || '—') : '—';
  $('#accUnid').textContent = logged ? (s.unid || '—') : '—';
  if($('#deviceRotateBtn')) $('#deviceRotateBtn').disabled = !logged || LIVE.submitting;
  if(!logged && $('#deviceRotateConfirm')) $('#deviceRotateConfirm').value = '';
  if(typeof updateCustomSaveState === 'function') updateCustomSaveState();

  $('#setSession').textContent = s.disabled ? '账号已禁用' : (logged ? '已加载 token' : '未登录');
  $('#setCampus').textContent = logged ? ([s.schoolName, s.campusName].filter(Boolean).join(' · ') || '—') : '—';
  $('#setDevice').textContent = logged ? (deviceLabel || '—') : '—';

  // The "我的" page hero mirrors the session immediately; the detailed
  // profile arrives from real_profile and only fills the remaining rows.
  const pp = (logged && LIVE.profile && LIVE.profile.profile) || {};
  const heroName = pp.nickname || pp.name || display;
  if($('#profAvatar')) $('#profAvatar').textContent =
    s.disabled ? '!' : (heroName || '?').slice(0, 1);
  if($('#profName')) $('#profName').textContent = heroName;
  if($('#profMeta')) $('#profMeta').textContent = s.disabled
    ? '该会话已按用户要求停用'
    : (logged ? ([pp.schoolName || s.schoolName,
                  pp.campusName || s.campusName]
                  .filter(Boolean).join(' · ') || '登录后显示学校 / 校区')
               : '登录后显示学校 / 校区');
  if($('#profUidChip')) $('#profUidChip').textContent =
    (logged && s.uid) ? ('uid ' + s.uid) : 'uid —';
  if($('#profUnidChip')) $('#profUnidChip').textContent =
    (logged && s.unid) ? ('unid ' + s.unid) : 'unid —';
  if($('#profLoginState')) $('#profLoginState').textContent = s.disabled
    ? '账号已停用' : (logged ? (verified ? '已登录 · 已验证' : '已登录 · 未验证') : '未登录');
  if($('#profDevice')) $('#profDevice').textContent = logged
    ? (deviceLabel || '—') : '—';
  if($('#profSession')) $('#profSession').textContent = logged
    ? '已加载 token' : (s.disabled ? '已停用' : '未登录');
  if($('#violList') && !LIVE.violations && !LIVE.violationsError){
    $('#violList').innerHTML = '<div class="violation-empty">'
      + (logged ? '点「查询违规」读取本账号的违规记录。'
                : '登录后点「查询违规」。') + '</div>';
  }

  const table = $('#accTable');
  if(table){
    const head = '<div class="thead"><span>账号</span><span>uid</span><span>校区</span><span>状态</span><span></span></div>';
    const rowName = s.disabled ? '旧账号' : (name || s.username || '本地会话');
    const rowTag = s.disabled ? 'warn' : (logged ? 'ok' : '');
    const row = s.uid ? `<div class="trow"><span>${esc(rowName)}</span><span class="mono">${esc(s.uid)}</span><span>${esc(s.campusName || '—')}</span><span><span class="tag ${rowTag}">${s.disabled ? '已禁用' : (logged ? '已加载' : '未验证')}</span></span><span></span></div>` : '';
    table.innerHTML = head + row;
  }

  $('#runLoginChip').textContent = s.disabled ? '账号已禁用' : (logged ? (verified ? 'token 有效' : 'token 已加载') : '未登录');
  $('#runLoginChip').className = 'chip ' + (s.disabled ? 'red' : (logged ? (verified ? 'ok' : 'warn') : 'warn'));
  $('#runLoginChip').innerHTML = '<i></i>' + $('#runLoginChip').textContent;
  $('#ovLoginChip').textContent = s.disabled ? '账号已禁用' : (logged ? (verified ? 'token 有效' : 'token 已加载') : '未登录');
  $('#ovLoginChip').className = 'chip ' + (s.disabled ? 'red' : (logged ? (verified ? 'ok' : 'warn') : 'warn'));
  $('#ovLoginChip').innerHTML = '<i></i>' + $('#ovLoginChip').textContent;
  const bp = beijingParts();
  $('#ovGreeting').textContent = greetingText() + (name ? '，' + name : '');
  $('#ovDate').textContent = bp.iso + ' ' + bp.weekday;
  // #runDate is owned by updateRunDateLabel(); resetting it here overwrote a
  // backfill date the operator had already chosen.
  if(!logged){
    // No usable token: do not leave the previous account's profile or
    // violation rows on screen while waiting for the next keeper probe.
    _profileSeq++; _violSeq++;
    LIVE.profile = null; LIVE.profileError = null; LIVE.profileLoading = false;
    LIVE.violations = null; LIVE.violationsError = null;
    renderProfileEmpty();
  }
}

async function liveInitSession(){
  const j = await liveApi('real_get_session');
  LIVE.session = liveOk(j) ? (j.data || {}) : {};
  focusCampusOnMap();
  LIVE.dailyCounts = {}; LIVE.dailyCountsAt = 0; LIVE.dailyCountsUid = null;
  if(!liveOk(j)) liveLog('读取会话失败', j.message || '', 'err');
  renderSession();
  await loadKeepalive();
  if(LIVE.session.device_migration_notice){
    liveLog('设备指纹已修复', LIVE.session.device_migration_notice, 'warn');
    renderEmptyData();
    return {state: 'login', message: LIVE.session.device_migration_notice};
  }
  if(LIVE.session.disabled){
    liveLog('账号已禁用', '不会探测或重登；请登录新账号', 'warn');
    renderEmptyData();
    return {state: 'login', message: '这个本地会话已停用，请登录你的账号。'};
  }
  if(!(LIVE.session.token_present || LIVE.session.token)){
    liveLog('未登录', '登录后读取真实围栏、点位和记录', 'warn');
    renderEmptyData();
    return {state: 'login',
            message: liveOk(j) ? '请输入手机号和密码登录。' : '读取本地会话失败，请重启后再试。'};
  }
  LIVE.checked = null;
  const chk = await liveApi('real_check_session', {}, 30000);
  LIVE.checked = chk;
  if(liveOk(chk) && chk.data){
    ['name', 'nickname', 'schoolName', 'campusName', 'unid', 'uid'].forEach(k => {
      if(chk.data[k] != null && chk.data[k] !== '') LIVE.session[k] = chk.data[k];
    });
  }
  renderSession();
  if(!liveOk(chk)){
    liveLog('token 检查失败', chk.message || '', 'err');
    renderEmptyData();
    refreshChecks();
    const why = String(chk.message || '');
    const invalid = /HTTP\s*(401|403)|error=(401|403|10121)|token|登录|未配置 token/i.test(why);
    return {state: 'login',
            message: invalid ? '登录已失效，请重新登录。'
                             : '暂时无法验证登录状态，请检查网络后重试。'};
  }
  liveLog('token 有效', (LIVE.session.campusName || '') + ' · ' + (LIVE.session.schoolName || ''), 'ok');
  await loadUiPrefs();
  applySavedRunArea();
  await Promise.all([loadPoints(false), loadOverview()]);
  refreshChecks();
  refreshModelPreview();
  // A direct reload on #profile runs go('profile') before the session is
  // restored, so that first call is a no-op.  Load it again now that the
  // token is verified.
  if(document.querySelector('.page.active')
     && document.querySelector('.page.active').id === 'page-profile'
     && !LIVE.profile && !LIVE.profileLoading){
    loadProfile({silent: true});
  }
  return {state: 'app'};
}

async function bootAuth(){
  showLoginGate('正在读取本地会话…', 'busy');
  await restoreSavedLogin();
  const accepted = await loadDisclaimerState();
  if(!accepted){
    await enforceDisclaimerGate();
    afterBootAuth();
    return;
  }
  setLoginBusy(true, '正在读取本地会话…', 'busy');
  setLoginButton('读取中…', true);
  try{ await loadRules(); }catch(e){ /* local rules are optional for login */ }
  try{ await loadNetworkConfig(); }catch(e){ /* settings are optional for login */ }
  let res;
  try{
    res = await liveInitSession();
  }catch(e){
    res = {state: 'login', message: '读取本地会话失败，请重试。'};
  }
  setLoginBusy(false);
  if(res && res.state === 'app'){
    enterApp(false);
    ensureKeepalive();
    syncSchoolRulesQuiet();
  }else{
    showLoginGate((res && res.message) || '请输入手机号和密码登录。');
  }
  afterBootAuth();
}

function renderEmptyData(){
  invalidateLiveRequests();
  // "Skip checkpoints" is an explicit per-run confirmation.  It must never
  // survive a logout/account switch, or the next account would silently run
  // without checkpoints.
  const free = $('#freeRun');
  if(free) free.checked = false;
  LIVE.pointsError = '';
  LIVE.pointsEmpty = false;
  LIVE.pointsStale = false;
  renderPoints([]);
  setFences([], '跑步围栏');
  clearTimeout(_routeFinalTimer);
  route = [];
  resetRouteHistory();
  redraw();
  LIVE.points = [];
  LIVE.fence = [];
  LIVE.fences = [];
  LIVE.fenceMissing = false;
  LIVE.fenceCount = 0;
  LIVE.pointsMissing = false;
  LIVE.pointsEmpty = false;
  LIVE.fenceError = '';
  LIVE.pointsError = '';
  LIVE.pointsDistance = 0;
  LIVE.pointsStale = false;
  LIVE.pointsSource = '';
  LIVE.pointsFitted = false;
  LIVE.pointsCooldownUntil = 0;
  LIVE.pointsCooldownKey = '';
  LIVE.pointsAutoAt = 0;
  LIVE.pointsLoading = false;
  stopPointsCooldownTick();
  if(typeof syncPointsCooldownUI === 'function') syncPointsCooldownUI();
  LIVE.hitRadiusM = 25;
  LIVE.runArea = null;
  LIVE.runAreas = [];
  LIVE.runAreaChoice = null;
  LIVE.runAreaChoiceKey = '';
  LIVE.sequential = false;
  LIVE.freezeSeconds = 0;
  LIVE.faceRequired = false;
  LIVE.faceReason = '';
  LIVE.records = [];
  LIVE.recordsLoaded = false;
  LIVE.recordsLoading = false;
  LIVE.recordsError = null;
  LIVE.week = null; LIVE.weekLoaded = false;
  LIVE.weekError = null;
  LIVE.planError = null;
  LIVE.plan = null;
  LIVE.daily = null;
  LIVE.lastSubmit = null;
  LIVE.submitResult = null;
  if(typeof renderSubmitResult === 'function') renderSubmitResult();
  LIVE.preflight = null;
  LIVE.semester = null; LIVE.semesterError = null;
  LIVE.profile = null; LIVE.profileError = null; LIVE.profileLoading = false;
  LIVE.profileAt = 0;
  LIVE.violations = null; LIVE.violationsError = null; LIVE.violationsAt = 0;
  LIVE.receipts = []; LIVE.receiptsLoaded = false; LIVE.receiptsError = null;
  LIVE.dailyCounts = {}; LIVE.dailyCountsAt = 0; LIVE.dailyCountsUid = null;
  updateFenceChip();
  LIVE.model = null;
  renderModel();
  renderRecords();
  renderReceipts();
  renderOverviewEmpty();
  renderProfileEmpty();
  refreshChecks();
}

async function liveLogin(){
  if(LIVE.submitting){ toast('提交进行中，等结果出来后再切换账号'); return; }
  if(accountBusy){ toast('正在登录，请稍候'); return; }
  const btn = $('#accLogin');
  if(btn && btn.disabled) return;
  const u = $('#accUser').value.trim(), p = $('#accPass').value;
  if(!u || !p){ toast('请填写手机号和密码'); return; }
  accountBusy = true;
  btn.disabled = true; btn.textContent = '登录中…';
  liveLog('登录中', '正在验证账号', 'info');
  try{
    const profile = await requestLogin(u, p);
    const remembered = await rememberLoginChoice(u, p);
    clearAccountActivity();
    liveLog('登录成功', profile.name || profile.campusName || '会话已保存', 'ok');
    $('#accPass').value = '';
    const res = await liveInitSession();
    if(!res || res.state !== 'app'){
      throw new Error((res && res.message) || '登录后仍无法验证会话');
    }
    await loadRules();
    ensureKeepalive();
    enterApp(false);
    syncSchoolRulesQuiet();
    toast(remembered ? '登录成功' : '登录成功；本地保存密码失败');
  }catch(e){
    const msg = prettyLoginError(e && e.message ? e.message : e);
    liveLog('登录失败', msg, 'err');
    toast('登录失败：' + msg);
  }finally{
    accountBusy = false;
    btn.disabled = false; btn.textContent = '登录';
  }
}

async function liveTestSession(){
  if(accountBusy){ toast('正在登录，请稍候'); return; }
  if(!hasSession()){ toast('没有可测试的 token'); return; }
  const btn = $('#accTest');
  if(btn && btn.disabled) return;
  const sessionAtStart = LIVE.session;
  if(btn){ btn.disabled = true; btn.textContent = '测试中…'; }
  liveLog('测试 token', 'GET user/info', 'info');
  try{
    const j = await liveApi('real_check_session');
    if(LIVE.session !== sessionAtStart || !hasSession()) return;
    LIVE.checked = j;
    renderSession();
    if(liveOk(j)){
      liveLog('token 有效', (j.data && j.data.name) ? j.data.name : '', 'ok');
      toast('token 有效');
    }else{
      liveLog('token 无效', j.message || '', 'err');
      toast('token 无效：' + (j.message || ''));
    }
    refreshChecks();
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '测试 token'; }
  }
}

async function liveLogout(){
  if(LIVE.submitting){ toast('提交进行中，等结果出来后再退出'); return; }
  if(accountBusy){ toast('正在登录，请稍候'); return; }
  if(!confirm('退出登录？有 token 时会验证 token 已失效；退出后回到登录页。')) return;
  accountBusy = true;
  try{ await liveApi('real_keepalive_stop'); }catch(e){}
  // Do not wait for the server: after the click the local gate closes at
  // once, while a best-effort server logout continues in the background.
  const j = await liveApi('real_logout', {local_only: true});
  if(liveOk(j)){
    const d = j.data || {};
    LIVE.session = {}; LIVE.checked = null; LIVE.points = [];
    LIVE.fence = []; LIVE.fences = []; LIVE.fenceCount = 0;
    LIVE.daily = null; LIVE.dailyCounts = {}; LIVE.dailyCountsAt = 0;
    LIVE.dailyCountsUid = null; LIVE.receipts = []; LIVE.receiptsLoaded = false;
    LIVE.receiptsError = null;
    LIVE.semester = null;
    LIVE.lastDetail = null;
    clearAccountActivity();
    const verified = !!d.verified;
    const detail = verified
      ? '服务端已验证 token 失效'
      : (d.background ? '本机已退出；服务端注销在后台尝试'
                      : (d.warning || '本机 token 已清除'));
    liveLog('已退出登录', detail, verified ? 'ok' : 'warn');
    renderSession(); renderEmptyData(); await loadKeepalive();
    ['#accUser', '#accPass', '#loginUser', '#loginPass'].forEach(sel => {
      const el = $(sel);
      if(el) el.value = '';
    });
    showLoginGate(verified ? '已退出登录，请重新登录。'
                           : '已退出本机登录，请重新登录。');
    await restoreSavedLogin();
    toast(verified ? '已退出登录' : '已退出本机登录');
  }else{
    // Real_logout clears local identity even when verification fails.  This
    // branch only remains for an old/broken bridge or a malformed response.
    liveLog('退出失败', j.message || '', 'err');
    toast('退出失败：' + (j.message || ''));
  }
  accountBusy = false;
}

/* ---------------- device rotation (advanced) ---------------- */
async function rotateDeviceProfile(){
  if(LIVE.submitting){ toast('提交进行中，完成后再更换设备指纹'); return; }
  if(!hasSession()){ toast('先登录再更换设备指纹'); return; }
  const input = $('#deviceRotateConfirm');
  if(!input || input.value.trim() !== '更换'){
    toast('请输入“更换”确认');
    if(input) input.focus();
    return;
  }
  const platformSel = $('#deviceRotatePlatform');
  const platform = platformSel ? platformSel.value : 'random';
  const platformLabel = {random: '随机', android: 'Android', ios: 'iOS'}[platform] || '随机';
  if(!confirm('确定更换设备指纹？\n\n· 新平台：' + platformLabel +
              '\n· 会像换了一台手机\n· 旧指纹会先备份\n· 更换后需要重新登录\n· 不要频繁更换')) return;
  const btn = $('#deviceRotateBtn');
  if(btn){ btn.disabled = true; btn.textContent = '更换中…'; }
  try{
    const j = await liveApi('real_rotate_device',
      {confirm: '更换设备指纹', platform: platform}, 30000);
    if(!liveOk(j)){
      liveLog('更换设备指纹失败', j.message || '', 'err');
      toast('更换失败：' + (j.message || '未知错误'));
      // The backend ends the local session before attempting the rotate, so
      // even a failed rotate can leave no usable token.  Do not keep showing
      // the old account as logged in.
      LIVE.session = {}; LIVE.checked = null;
      clearAccountActivity();
      renderSession();
      renderEmptyData();
      await loadKeepalive();
      showLoginGate('更换设备指纹失败，请重新登录。');
      await restoreSavedLogin();
      return;
    }
    const d = j.data || {};
    const dev = d.device || {};
    const osLabel = dev.device_os
      ? ((dev.device_platform === 'ios' ? 'iOS ' : 'Android ') + dev.device_os)
      : '';
    input.value = '';
    liveLog('设备指纹已更换',
      [platformLabel, dev.device_name, osLabel,
       dev.device_id_masked].filter(Boolean).join(' · '), 'warn');
    if(d.backup) liveLog('旧指纹已备份', d.backup, 'info');
    // The backend already cleared the local session; never reuse the old
    // token with the new device.
    LIVE.session = {}; LIVE.checked = null;
    clearAccountActivity();
    renderSession();
    renderEmptyData();
    await loadKeepalive();
    showLoginGate('设备指纹已更换，请重新登录。');
    await restoreSavedLogin();
    toast('设备指纹已更换，请重新登录');
  }catch(e){
    liveLog('更换设备指纹失败', e && e.message ? e.message : String(e), 'err');
    toast('更换失败：' + (e && e.message ? e.message : String(e)));
  }finally{
    if(btn){
      btn.disabled = !hasSession();
      btn.textContent = '更换设备指纹';
    }
  }
}

/* ---------------- custom device (advanced) ---------------- */
function deviceCatalogRows(platform){
  const cat = LIVE.deviceCatalog || {};
  return Array.isArray(cat[platform]) ? cat[platform] : [];
}
function customDeviceRow(){
  if(!$('#customDevicePlatform') || !$('#customDeviceModel')) return null;
  const platform = $('#customDevicePlatform').value;
  const model = $('#customDeviceModel').value;
  return deviceCatalogRows(platform).find(r => r.deviceName === model) || null;
}
function fillCustomDeviceModels(){
  if(!$('#customDeviceModel')) return;
  const platform = $('#customDevicePlatform').value;
  const rows = deviceCatalogRows(platform);
  const prev = $('#customDeviceModel').value;
  $('#customDeviceModel').innerHTML = rows.map(r => {
    const label = r.label && r.label !== r.deviceName
      ? r.label + ' · ' + r.deviceName : r.deviceName;
    return `<option value="${esc(r.deviceName)}">${esc(label)}</option>`;
  }).join('');
  if(rows.some(r => r.deviceName === prev)) $('#customDeviceModel').value = prev;
  fillCustomDeviceOs();
}
function fillCustomDeviceOs(){
  if(!$('#customDeviceOs')) return;
  const row = customDeviceRow();
  const prev = $('#customDeviceOs').value;
  $('#customDeviceOs').innerHTML = row
    ? row.osVersions.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('')
    : '';
  if(row && row.osVersions.includes(prev)) $('#customDeviceOs').value = prev;
  updateCustomDeviceHint();
}
function updateCustomDeviceHint(){
  const row = customDeviceRow();
  const hint = $('#customDeviceHint');
  if(!row){
    if(hint) hint.textContent = '先选择平台和机型。';
  }else if(hint){
    hint.textContent = `自动匹配：${row.physicPixel} / ${row.logicPixel} · CPU ${row.cpuModel} · root=0`;
  }
  updateCustomSaveState();
}
function updateCustomSaveState(){
  const btn = $('#customDeviceSave');
  if(!btn) return;
  const typed = $('#customDeviceConfirm') ? $('#customDeviceConfirm').value.trim() : '';
  const id = $('#customDeviceId') ? $('#customDeviceId').value.trim() : '';
  btn.disabled = !LIVE.deviceCatalog ||
    !(typed === '自定义' && id.length > 0) || !!LIVE.submitting;
}
async function loadDeviceOptions(){
  if(LIVE.deviceCatalogLoading) return;
  LIVE.deviceCatalogLoading = true;
  try{
    const j = await liveApi('real_device_options', {}, 15000);
    if(!liveOk(j)){
      LIVE.deviceCatalogError = (j && j.message) || '设备目录读取失败';
      if($('#customDeviceHint')) $('#customDeviceHint').textContent = LIVE.deviceCatalogError;
      return;
    }
    LIVE.deviceCatalog = (j.data && j.data.catalog) || {};
    LIVE.deviceCatalogError = null;
    fillCustomDeviceModels();
  }catch(e){
    LIVE.deviceCatalogError = e && e.message ? e.message : String(e);
    if($('#customDeviceHint')) $('#customDeviceHint').textContent = LIVE.deviceCatalogError;
  }finally{
    LIVE.deviceCatalogLoading = false;
  }
}
async function resetAfterDeviceChange(message, relogin){
  LIVE.session = {}; LIVE.checked = null;
  clearAccountActivity();
  renderSession();
  renderEmptyData();
  await loadKeepalive();
  if(relogin) showLoginGate(message);
  await restoreSavedLogin();
}
async function saveCustomDevice(){
  if(LIVE.submitting){ toast('提交进行中，完成后再修改设备'); return; }
  const confirmInput = $('#customDeviceConfirm');
  if(!confirmInput || confirmInput.value.trim() !== '自定义'){
    toast('请输入“自定义”确认');
    if(confirmInput) confirmInput.focus();
    return;
  }
  const platform = $('#customDevicePlatform').value;
  const deviceName = $('#customDeviceModel').value;
  const osVersion = $('#customDeviceOs').value;
  const DeviceId = $('#customDeviceId').value.trim();
  const installDays = $('#customDeviceAge') ? $('#customDeviceAge').value.trim() : '';
  if(!DeviceId){ toast('请填写 DeviceId'); $('#customDeviceId').focus(); return; }
  if(!confirm('确定保存自定义设备？\n\n· 设备：' + deviceName + ' · ' + osVersion +
              '\n· DeviceId：' + DeviceId +
              '\n· 旧指纹会先备份\n· 保存后需要重新登录\n· 不要频繁更换')) return;
  const btn = $('#customDeviceSave');
  if(btn){ btn.disabled = true; btn.textContent = '保存中…'; }
  try{
    const j = await liveApi('real_set_custom_device', {
      confirm: '自定义设备', platform: platform, deviceName: deviceName,
      osVersion: osVersion, DeviceId: DeviceId, installDays: installDays
    }, 30000);
    if(!liveOk(j)){
      const msg = (j && j.message) || '未知错误';
      toast('保存失败：' + msg);
      if($('#customDeviceResult')) $('#customDeviceResult').textContent = msg;
      liveLog('自定义设备保存失败', msg, 'err');
      return;
    }
    const d = j.data || {};
    const dev = d.device || {};
    const osLabel = dev.device_os
      ? ((dev.device_platform === 'ios' ? 'iOS ' : 'Android ') + dev.device_os)
      : '';
    liveLog('自定义设备已保存',
      [dev.device_name, osLabel, dev.device_id_masked].filter(Boolean).join(' · '), 'warn');
    if(d.backup) liveLog('旧指纹已备份', d.backup, 'info');
    if($('#customDeviceConfirm')) $('#customDeviceConfirm').value = '';
    if($('#customDeviceId')) $('#customDeviceId').value = '';
    if($('#customDeviceAge')) $('#customDeviceAge').value = '';
    if($('#customDeviceResult')) $('#customDeviceResult').textContent = '已保存';
    await resetAfterDeviceChange('自定义设备已保存，请重新登录。', !!d.relogin_required);
    toast('自定义设备已保存');
  }catch(e){
    const msg = e && e.message ? e.message : String(e);
    toast('保存失败：' + msg);
    liveLog('自定义设备保存失败', msg, 'err');
  }finally{
    if(btn) btn.textContent = '保存自定义设备';
    updateCustomSaveState();
  }
}

/* ---------------- keepalive ---------------- */
async function loadKeepalive(){
  const sessionAtStart = LIVE.session;
  let expectedSession = sessionAtStart;
  const j = await liveApi('real_keepalive_status');
  if(LIVE.session !== expectedSession) return;
  const badge = $('#kaBadge'), info = $('#kaInfo');
  if(!liveOk(j)){
    if(badge){ badge.textContent = '读取失败'; badge.className = 'tag warn'; }
    if(info) info.textContent = j.message || '';
    return;
  }
  const d = j.data || {};
  if(d.disabled){
    if(LIVE.session){
      LIVE.session = Object.assign({}, LIVE.session, {disabled: true});
      LIVE.checked = null;
      invalidateLiveRequests();
      renderSession();
      // Clear only the personal page: a token probe must not wipe records or
      // a submission that is still in flight.
      LIVE.profile = null; LIVE.profileError = null; LIVE.profileLoading = false;
      LIVE.profileAt = 0;
      LIVE.violations = null; LIVE.violationsError = null; LIVE.violationsAt = 0;
      renderProfileEmpty();
      refreshChecks();
      if(!LOGIN.visible && !LIVE.submitting && !accountBusy){
        showLoginGate('这个本地会话已停用，请登录你的账号。');
      }
    }
    badge.textContent = '账号已禁用'; badge.className = 'tag warn';
    info.textContent = '该会话已停用，不会探测或重登。'; return;
  }
  if(!d.token_present){
    if(LIVE.session){
      LIVE.session = Object.assign({}, LIVE.session,
        {token_present: false, token: ''});
      LIVE.checked = null;
      invalidateLiveRequests();
      renderSession();
      // Same as the disabled branch: only the "我的" data is cleared here.
      LIVE.profile = null; LIVE.profileError = null; LIVE.profileLoading = false;
      LIVE.profileAt = 0;
      LIVE.violations = null; LIVE.violationsError = null; LIVE.violationsAt = 0;
      renderProfileEmpty();
      refreshChecks();
      if(!LOGIN.visible && !LIVE.submitting && !accountBusy){
        showLoginGate('登录已失效，请重新登录。');
      }
    }
    badge.textContent = '无 token'; badge.className = 'tag warn';
    info.textContent = '先登录，或保存 token。'; return;
  }
  // Auto-relogin replaces the token on disk without the page knowing.  Pull
  // the new session and re-verify it, otherwise the account card and submit
  // gate would keep testing the old token.
  const stamp = +(d.token_updated_at || 0);
  if(stamp && LIVE.tokenUpdatedAt !== null && stamp !== LIVE.tokenUpdatedAt){
    const sj = await liveApi('real_get_session');
    if(LIVE.session !== expectedSession) return;
    if(liveOk(sj)){
      const newSession = sj.data || {};
      LIVE.session = newSession;
      expectedSession = newSession;
      const cj = await liveApi('real_check_session');
      if(LIVE.session !== expectedSession) return;
      LIVE.checked = cj;
      if(liveOk(cj) && cj.data){
        ['name', 'nickname', 'schoolName', 'campusName', 'unid', 'uid'].forEach(k => {
          if(cj.data[k] != null && cj.data[k] !== '') LIVE.session[k] = cj.data[k];
        });
      }
      renderSession();
      refreshChecks();
    }
  }
  if(LIVE.session !== expectedSession) return;
  LIVE.tokenUpdatedAt = stamp;
  if(d.token_expired){
    badge.textContent = 'token 失效'; badge.className = 'tag warn';
    LIVE.checked = {code: 500, message: 'token 失效'};
    refreshChecks();
  }
  else if(d.running){ badge.textContent = '保活中'; badge.className = 'tag ok'; }
  else if(d.enabled){ badge.textContent = '待恢复'; badge.className = 'tag warn'; }
  else { badge.textContent = '未开启'; badge.className = 'tag'; }
  const parts = [];
  parts.push('间隔 ' + fmtKeepaliveInterval(d.interval_s));
  parts.push('上次检查 ' + beijingStamp(d.last_check_at));
  parts.push('上次有效 ' + beijingStamp(d.last_ok_at));
  if(d.fail_count) parts.push('失败 ' + d.fail_count + ' 次');
  if(d.auto_relogin) parts.push(d.can_auto_relogin ? '自动重登就绪' : '自动重登待输入密码');
  if(d.last_error) parts.push('错误: ' + d.last_error);
  info.textContent = parts.join(' · ');
  if($('#kaInterval') && d.interval_s){
    const v = String(d.interval_s);
    if([...$('#kaInterval').options].some(o => o.value === v)) $('#kaInterval').value = v;
  }
  if($('#kaAuto')) $('#kaAuto').checked = !!d.auto_relogin;
}
async function liveKeepaliveStart(){
  if(accountBusy){ toast('正在登录，请稍候'); return; }
  const auto = $('#kaAuto').checked;
  const u = $('#accUser').value.trim(), p = $('#accPass').value;
  if(auto && (!u || !p)){ toast('自动重登需要手机号和密码（只在内存）'); return; }
  const j = await liveApi('real_keepalive_start', {
    interval_s: +$('#kaInterval').value || 21600,
    auto_relogin: auto, username: auto ? u : '', password: auto ? p : ''
  });
  if(liveOk(j)){ liveLog('保活已开启', (auto ? '失效自动重登' : '仅探测'), 'ok'); toast('保活已开启'); }
  else { liveLog('开启保活失败', j.message || '', 'err'); toast('开启失败：' + (j.message || '')); }
  await loadKeepalive();
}
async function liveKeepaliveCheck(){
  if(accountBusy){ toast('正在登录，请稍候'); return; }
  const btn = $('#kaCheck');
  if(btn && btn.disabled) return;
  if(btn){ btn.disabled = true; btn.textContent = '检查中…'; }
  try{
    const j = await liveApi('real_keepalive_once');
    if(liveOk(j)){ liveLog('保活检查', (j.data && j.data.action) + ' · ' + (j.data && j.data.message), 'ok'); toast('检查完成'); }
    else { liveLog('保活检查失败', j.message || '', 'warn'); toast('检查失败：' + (j.message || '')); }
    await loadKeepalive();
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '立即检查'; }
  }
}
async function liveKeepaliveStop(){
  if(accountBusy){ toast('正在登录，请稍候'); return; }
  const j = await liveApi('real_keepalive_stop');
  if(liveOk(j)) liveLog('保活已停止', '内存密码已清除', 'info');
  await loadKeepalive();
}

/* ---------------- points / fence ---------------- */
function freeRunOn(){
  return !!($('#freeRun') && $('#freeRun').checked);
}
function focusCampusOnMap(){
  const c = sessionCenter();
  if(!c) return false;
  map.setView(c, 16);
  return true;
}
const POINTS_AUTO_GAP_MS = 5 * 60 * 1000;
const POINTS_STALE_M = 50;
const POINTS_FINAL_DELAY_MS = 900;
function pointsSessionKey(){
  const s = LIVE.session || {};
  return String(s.uid || '') + ':' + String(s.unid || '');
}
function pointsCooldownLeft(){
  // The server cooldown is per uid|unid; a 10603 on campus A must not lock
  // campus B on the same machine.
  if(LIVE.pointsCooldownKey !== pointsSessionKey()) return 0;
  return Math.max(0,
    Math.ceil(((LIVE.pointsCooldownUntil || 0) - Date.now()) / 1000));
}
let pointsCooldownTimer = null;
function stopPointsCooldownTick(){
  if(pointsCooldownTimer){
    clearInterval(pointsCooldownTimer);
    pointsCooldownTimer = null;
  }
}
function syncPointsCooldownUI(){
  const left = pointsCooldownLeft();
  const btn = $('#fetchPoints');
  if(btn && !LIVE.pointsLoading){
    if(left > 0){
      btn.disabled = true;
      btn.textContent = '冷却 ' + left + 's';
      btn.title = '点位接口限流中，' + left + ' 秒后可重试';
    }else{
      btn.disabled = false;
      btn.textContent = '刷新';
      btn.title = '';
    }
  }
  if(left > 0){
    const chip = $('#fenceChip');
    if(chip) chip.title = '限流冷却：约 ' + left + ' 秒后可重试';
  }else{
    stopPointsCooldownTick();
    if(btn && !LIVE.pointsLoading && btn.title === '') btn.disabled = false;
    if(typeof updateFenceChip === 'function') updateFenceChip();
  }
}
function startPointsCooldownTick(){
  if(pointsCooldownTimer) return;
  pointsCooldownTimer = setInterval(syncPointsCooldownUI, 1000);
}
function notePointsCooldown(seconds){
  const s = +seconds || 0;
  if(s <= 0) return;
  LIVE.pointsCooldownKey = pointsSessionKey();
  LIVE.pointsCooldownUntil = Math.max(LIVE.pointsCooldownUntil || 0,
                                      Date.now() + s * 1000);
  startPointsCooldownTick();
  syncPointsCooldownUI();
}
function pointsTargetM(){
  const km = +($('#inKm') && $('#inKm').value) || 0;
  return km > 0 ? Math.max(1, Math.round(km * 1000)) : 0;
}
function pointsDistanceStale(){
  if(!(LIVE.pointsDistance > 0) || !livePoints.length) return false;
  const target = pointsTargetM();
  if(!(target > 0)) return false;
  return Math.abs(target - LIVE.pointsDistance) >= POINTS_STALE_M;
}
function pointsDistanceText(){
  const target = pointsTargetM();
  return '点位按 ' + (LIVE.pointsDistance / 1000).toFixed(2) +
    ' km 加载，当前目标 ' + (target / 1000).toFixed(2) + ' km';
}
function refreshPointsDistanceState(){
  // redraw() runs once during boot, before LIVE/hasSession are initialized.
  // Defer both the chip and the checklist so a mid-draw state change never
  // touches those bindings early.
  clearTimeout(_pointsStateTimer);
  _pointsStateTimer = setTimeout(() => {
    if(typeof updateFenceChip === 'function') updateFenceChip();
    if(typeof refreshChecks === 'function') refreshChecks();
  }, 30);
}
function scheduleRouteFinalize(delay, reason){
  clearTimeout(_routeFinalTimer);
  _routeFinalTimer = setTimeout(() => {
    _routeFinalTimer = 0;
    finalizeRoute(reason);
  }, Math.max(0, +delay || 0));
}
function finalizeRoute(reason){
  if(!hasSession() || freeRunOn()){ refreshPointsDistanceState(); return; }
  if(LIVE.pointsLoading){
    scheduleRouteFinalize(1000, reason);
    return;
  }
  // An auto-filled target from a two-point stub is not a final line.  Keep
  // waiting; a manual target is explicit and may be used without a route.
  if(!distanceManual){
    const m = route && route.length >= 2 ? pathLength(displayPath()) : 0;
    if(!(m >= 80)){ refreshPointsDistanceState(); return; }
  }
  const want = pointsTargetM();
  if(!(want > 0)){ refreshPointsDistanceState(); return; }
  if(LIVE.pointsDistance && Math.abs(want - LIVE.pointsDistance) < 1){
    refreshPointsDistanceState();
    return;
  }
  if(pointsCooldownLeft() > 0){ refreshPointsDistanceState(); return; }
  if(Date.now() - (LIVE.pointsAutoAt || 0) < POINTS_AUTO_GAP_MS){
    refreshPointsDistanceState();
    return;
  }
  // One request per finalized line.  The caller passes the exact target so a
  // route loaded from the library and a manual km entry both agree.
  LIVE.pointsAutoAt = Date.now();
  loadPoints(false, {quiet: true, distance_m: want, final: reason || 'route'});
}
function schedulePointsForDistance(auto){
  clearTimeout(_pointsKmTimer);
  if(auto){
    // While drawing, the length is still moving.  Never call the endpoint
    // here: just expose that the displayed set belongs to an older length.
    // The actual request happens when the route is finalized (pen off, tab
    // switch, import, drag end, or a manual distance entry).
    refreshPointsDistanceState();
    return;
  }
  refreshPointsDistanceState();
  _pointsKmTimer = setTimeout(() => {
    finalizeRoute('distance');
  }, POINTS_FINAL_DELAY_MS);
}
async function setCampusFromMap(){
  if(!hasSession()){ toast('先登录再设置校区'); return; }
  const c = map.getCenter();
  const lat = +c.lat.toFixed(6), lon = +c.lng.toFixed(6);
  const j = await liveApi('real_set_campus_center', {lat, lon}, 15000);
  if(!liveOk(j)){
    toast((j && j.message) || '校区坐标保存失败');
    return;
  }
  LIVE.session = Object.assign({}, LIVE.session, {lat, lon});
  liveLog('校区坐标已保存', lat + ', ' + lon, 'ok');
  toast('已把地图中心设为校区');
  loadPoints(true, {quiet: false});
}

async function loadPoints(force, opts){
  opts = opts || {};
  if(!hasSession()){ if(!opts.quiet) toast('先登录后再刷新点位'); return; }
  const cooling = pointsCooldownLeft();
  if(cooling > 0){
    // `force` only bypasses the 300 s cache; it must not burn another call
    // while the server is still refusing this account.
    syncPointsCooldownUI();
    refreshPointsDistanceState();
    if(!opts.quiet)
      toast('点位接口限流中，约 ' + cooling + ' 秒后可重试');
    return;
  }
  const seq = ++_pointsSeq;
  const s = LIVE.session;
  const btn = $('#fetchPoints');
  LIVE.pointsLoading = true;
  if(btn){ btn.textContent = '刷新中…'; btn.disabled = true; }
  // The checkpoint endpoint is distance-scoped.  Use today's target so the
  // points drawn on the map are the ones submit will actually route through;
  // a hard-coded 2500 m can be an invalid distance for another school.
  // `distance_m` from a finalized route is exact.  Without it (first visit,
  // before anything is drawn) use a neutral campus preview distance so the
  // fence still loads; the target input itself stays at 0.
  const km = +($('#inKm') && $('#inKm').value) || 2.5;
  const distance_m = opts.distance_m > 0
    ? Math.max(1, Math.round(+opts.distance_m))
    : Math.max(1, Math.round(km * 1000));
  const j = await liveApi('real_fetch_points', {
    school: s.schoolName || '', glat: +s.lat || 0, glon: +s.lon || 0,
    unid: s.unid || null, force: !!force, distance_m,
    run_area_id: chosenRunAreaId(s)
  });
  // A slower older request must not overwrite a newer campus/distance.
  if(seq !== _pointsSeq) return;
  LIVE.pointsLoading = false;
  syncPointsCooldownUI();
  if(!liveOk(j)){
    const errors = (j.data && Array.isArray(j.data.errors)) ? j.data.errors : [];
    liveLog('围栏/点位加载失败',
      [j.message || '', errors.join('；')].filter(Boolean).join('；'),
      opts.quiet ? 'warn' : 'err');
    // A transient refresh failure is not proof that the last known fence is
    // wrong.  Keep it and say so; only an empty state needs an explicit clear.
    // If the backend still returned the polygons (e.g. multi-fence centre is
    // ambiguous), draw them so the operator can pick the right campus.
    const failD = j.data || {};
    const failRings = fenceRingsFromData(failD);
    LIVE.fenceError = failD.fence_error || LIVE.fenceError;
    LIVE.pointsError = failD.points_error || LIVE.pointsError;
    notePointsCooldown(failD.points_retry_after_s);
    if(failRings.length){
      LIVE.fences = failRings;
      LIVE.fence = failRings[0];
      LIVE.fenceCount = Math.max(failRings.length, +(failD.fence_count || 0));
      LIVE.fenceMissing = !!failD.fence_missing;
      // A failed background refresh must not move the camera away from the
      // line the operator is drawing.
      setFences(failRings, (s.campusName || '') + '跑步围栏', {fit: false});
    }
    if(!(LIVE.points.length || LIVE.fence.length)){
      setFences([], '跑步围栏');
      renderPoints([]);
      LIVE.points = []; LIVE.fence = []; LIVE.fences = [];
      LIVE.fenceCount = 0;
    }else{
      const chip = $('#fetchPoints');
      if(chip) chip.title = '上次刷新失败，仍使用上次点位：' + (j.message || '');
    }
    updateFenceChip();
    refreshChecks();
    updateCampusControls();
    return;
  }
  const d = j.data || {};
  // The backend may have corrected the centre from the real fence.  Adopt it
  // so the map, preview and the next request all use the same anchor.
  if(d.center && isFinite(+d.center.lat) && isFinite(+d.center.lon)){
    LIVE.session = Object.assign({}, LIVE.session,
      {lat: +d.center.lat, lon: +d.center.lon});
  }
  LIVE.fences = fenceRingsFromData(d);
  LIVE.fence = LIVE.fences.length ? LIVE.fences[0] : [];
  LIVE.fenceCount = Math.max(LIVE.fences.length, +(d.fence_count || 0));
  // A failed checkpoint call still comes back as a successful envelope (the
  // fence/policy reply is usable).  `checkpoints` is null in that case; only
  // replace the map when the backend actually returned a list, otherwise a
  // transient 10603 would wipe the points submit is going to use.
  const gotPoints = Array.isArray(d.checkpoints);
  if(gotPoints){
    LIVE.points = d.checkpoints
      .filter(p => p && isFinite(+(p.lat ?? p.glat)) && isFinite(+(p.lon ?? p.glon)));
    LIVE.pointsEmpty = !!d.points_empty || !!d.points_missing;
    LIVE.pointsMissing = LIVE.pointsEmpty;   // legacy name used by older code
    LIVE.pointsError = '';
    LIVE.pointsStale = !!d.points_stale;
    LIVE.pointsSource = d.points_source || '';
    LIVE.pointsDistance = +d.points_distance_m || distance_m;
  }else{
    LIVE.pointsError = d.points_error || LIVE.pointsError || '点位读取失败';
  }
  LIVE.fenceMissing = !!d.fence_missing;
  LIVE.fenceError = d.fence_error || '';
  notePointsCooldown(d.points_retry_after_s);
  // Backend owns the checkpoint radius (25 m); never hard-code it here again.
  const hitR = +d.hit_radius_m;
  LIVE.hitRadiusM = (isFinite(hitR) && hitR > 0) ? hitR : 25;
  LIVE.runArea = d.run_area || null;
  LIVE.runAreas = Array.isArray(d.run_areas) ? d.run_areas : [];
  // Keep an explicit pick only while the backend confirms it; if the area was
  // invalid or vanished, fall back to the account's own area.
  if(LIVE.runAreaChoice != null &&
     (!LIVE.runArea || +LIVE.runArea.id !== +LIVE.runAreaChoice)){
    LIVE.runAreaChoice = null;
    LIVE.runAreaChoiceKey = '';
  }
  LIVE.sequential = !!d.sequential;
  LIVE.freezeSeconds = +d.freeze_seconds || 0;
  LIVE.faceRequired = !!d.face_required;
  LIVE.faceReason = d.face_reason || '';
  LIVE.pointsAt = Date.now();
  // Always redraw, even when the server returned an empty list: otherwise the
  // previous campus's fence/points stayed on the map after switching accounts.
  const hasPoints = LIVE.points.length > 0;
  // Fit once per campus/session, and never while a route is being drawn or
  // after a background distance refresh: the old behaviour snapped the camera
  // back to the fence on every 900 ms refresh.
  const wantFit = opts.fit === true ||
    (!route.length && (!LIVE.pointsFitted || force));
  setFences(LIVE.fences, (s.campusName || '') + '跑步围栏',
            {fit: wantFit && !hasPoints});
  renderPoints(LIVE.points, {fit: wantFit && hasPoints});
  if(wantFit && (hasPoints || LIVE.fences.length)) LIVE.pointsFitted = true;
  updateCampusControls();
  if((Array.isArray(d.errors) && d.errors.length) ||
     (Array.isArray(d.notes) && d.notes.length)){
    liveLog('围栏/点位提示',
      [...(d.notes || []), ...(d.errors || [])].join('；'), 'warn');
  }
  if(!opts.quiet){
    if(LIVE.freezeSeconds > 0){
      const m = Math.ceil(LIVE.freezeSeconds / 60);
      liveLog('账号处于跑步冻结期', `剩余约 ${m} 分钟；官方客户端此时不能开始跑步，本工具只提醒，不阻止提交`, 'warn');
    }
    if(LIVE.faceRequired){
      liveLog('学校启用了人脸抽检', `${LIVE.faceReason || 'faceVerify'}；本工具无法完成人脸校验，提交会被阻止`, 'warn');
    }
    const fenceText = LIVE.fences.length
      ? (LIVE.fences.length > 1
        ? (LIVE.fences.length + ' 个围栏')
        : (LIVE.fence.length + ' 个围栏顶点'))
      : (LIVE.fenceMissing ? '无围栏' : '围栏未加载');
    const pointText = LIVE.points.length
      ? (LIVE.points.length + ' 个打卡点' +
         (LIVE.pointsStale
           ? '（缓存 ' + (LIVE.pointsDistance / 1000).toFixed(2) + ' km）'
           : (pointsDistanceStale()
             ? '（按 ' + (LIVE.pointsDistance / 1000).toFixed(2) +
               ' km 取，目标 ' + (pointsTargetM() / 1000).toFixed(2) + ' km）'
             : '')))
      : (LIVE.pointsEmpty ? '无打卡点（需确认）'
        : (LIVE.pointsError ? '点位读取失败（沿用上次点位）' : '点位未加载'));
    const areaText = LIVE.runArea
      ? (' · 跑区 ' + (LIVE.runArea.name || LIVE.runArea.id)) : '';
    liveLog('围栏/点位已加载',
      `${fenceText} · ${pointText}${areaText} · 目标 ${(distance_m / 1000).toFixed(2)} km`, 'ok');
  }
  updateFenceChip();
  refreshChecks();
  refreshModelPreview();
  if(typeof renderRouteList === 'function'){
    renderRouteList();
    if(LIVE.selectedRoute >= 0) selectRoute(LIVE.selectedRoute);
  }
}
function updateFenceChip(){
  const chip = $('#fenceChip');
  const top = $('#runFenceChip');
  const fenceOk = LIVE.fences.length > 0 || !!LIVE.fenceMissing;
  const distMismatch = pointsDistanceStale();
  const pointOk = livePoints.length > 0 || freeRunOn();
  const ok = fenceOk && pointOk && !distMismatch;
  const fenceLabel = LIVE.fenceMissing ? '无围栏'
    : (LIVE.fences.length > 1 ? (LIVE.fences.length + ' 个围栏')
      : (LIVE.fences.length ? '围栏已加载' : '围栏未加载'));
  let label;
  if(!fenceOk && !pointOk) label = LIVE.fenceError ? '围栏读取失败' : '围栏/点位未加载';
  else if(!fenceOk) label = LIVE.fenceError ? '围栏读取失败 · 点位已加载'
    : '无围栏 · 点位已加载';
  else if(!pointOk) label = LIVE.pointsError
    ? (fenceOk ? fenceLabel + ' · 点位读取失败' : '点位读取失败')
    : (LIVE.pointsEmpty ? fenceLabel + ' · 无打卡点（需确认）'
      : fenceLabel + ' · 无点位');
  else if(freeRunOn()) label = (LIVE.fenceMissing ? '无围栏' : fenceLabel)
    + ' · 跳过点位';
  else if(LIVE.fenceMissing && LIVE.pointsEmpty) label = '无围栏 · 无打卡点（需确认）';
  else if(LIVE.fenceMissing) label = '无围栏 · 点位已加载';
  else if(LIVE.pointsStale) label = fenceLabel + ' · 点位已加载（缓存 ' +
    (LIVE.pointsDistance / 1000).toFixed(2) + ' km）';
  else if(distMismatch) label = fenceLabel + ' · 点位按 ' +
    (LIVE.pointsDistance / 1000).toFixed(2) + ' km 取（目标 ' +
    (pointsTargetM() / 1000).toFixed(2) + ' km）';
  else label = fenceLabel + ' · 点位已加载';
  if(chip){
    chip.textContent = label;
    chip.className = 'tag ' +
      ((ok && !LIVE.pointsStale && !distMismatch) ? '' : 'warn');
    const bits = [];
    if(LIVE.pointsAt){
      bits.push('上次同步：' + new Date(LIVE.pointsAt)
        .toLocaleTimeString('zh-CN', {hour12:false, hour:'2-digit', minute:'2-digit'}));
    }
    if(LIVE.pointsError && LIVE.points.length){
      bits.push('点位读取失败，沿用上次点位：' + LIVE.pointsError);
    }
    if(distMismatch){
      bits.push(pointsDistanceText() + '；点「刷新」按当前距离重取');
    }
    if(pointsCooldownLeft() > 0){
      bits.push('限流冷却：约 ' + pointsCooldownLeft() + ' 秒');
    }
    chip.title = bits.join('；');
  }
  if(top){
    top.textContent = fenceOk ? fenceLabel
      : (LIVE.fenceError ? '围栏读取失败' : '围栏未加载');
    top.className = 'chip ' + (fenceOk ? (LIVE.fenceMissing ? 'warn' : 'ok') : 'warn');
    top.innerHTML = '<i></i>' + top.textContent;
  }
  if($('#ovFenceChip')){
    $('#ovFenceChip').textContent = top ? top.textContent : fenceLabel;
    $('#ovFenceChip').className = 'chip ' +
      (fenceOk && !LIVE.fenceMissing ? 'ok' : 'warn');
    $('#ovFenceChip').innerHTML = '<i></i>' + $('#ovFenceChip').textContent;
  }
}
function runAreaSessionKey(s){
  s = s || LIVE.session || {};
  return String(s.uid || '') + ':' + String(s.unid || '');
}
async function loadUiPrefs(){
  try{
    const j = await liveApi('real_get_ui_state', {}, 8000);
    if(liveOk(j) && j.data){
      const prefs = j.data.run_area_choice;
      LIVE.runAreaPrefs = (prefs && typeof prefs === 'object') ? prefs : {};
      if(j.data.skin) applySkin(j.data.skin, {save: false, refresh: false});
    }
  }catch(e){}
}
function applySavedRunArea(){
  // ui_state stores {uid:unid -> areaId}; the backend re-validates the id
  // against runAreaModels and falls back to the account's default when the
  // saved area is gone.
  if(!LIVE.session) return false;
  const key = runAreaSessionKey(LIVE.session);
  const saved = (LIVE.runAreaPrefs || {})[key];
  if(saved == null) return false;
  LIVE.runAreaChoice = +saved;
  LIVE.runAreaChoiceKey = key;
  return true;
}
function chosenRunAreaId(s){
  // Only an explicit pick made for the current account is sent: an area id
  // from another account can collide and point at a different campus.
  if(LIVE.runAreaChoice == null) return null;
  return LIVE.runAreaChoiceKey === runAreaSessionKey(s)
    ? LIVE.runAreaChoice : null;
}
function renderRunAreas(){
  // Official `RunningAreaDialog`: the account can belong to several run areas
  // (campuses), each with its own fence.  Invalid areas stay visible but
  // cannot be clicked, just like the grey rows in the official dialog.
  const sel = $('#runAreaSel');
  if(!sel) return;
  const areas = (LIVE.runAreas || []).filter(a => a && a.id != null);
  if(areas.length < 2){
    sel.hidden = true;
    sel.innerHTML = '';
    return;
  }
  const chosen = chosenRunAreaId();
  const cur = chosen != null ? +chosen : null;
  sel.innerHTML =
    `<option value=""${cur == null ? ' selected' : ''}>自动（按账号校区）</option>` +
    areas.map(a => {
      const id = +a.id;
      const name = a.name || ('跑区 ' + id);
      const invalid = a.valid === false;
      return `<option value="${id}"${invalid ? ' disabled' : ''}` +
        `${id === cur ? ' selected' : ''}>${esc(name)}` +
        `${invalid ? '（无效）' : ''}</option>`;
    }).join('');
  sel.hidden = false;
  sel.title = '切换跑区：使用该跑区自己的围栏和打卡点；「自动」按账号校区匹配';
}
function reloadRunArea(message){
  // The displayed set belongs to the old area.  A failed refresh must not
  // leave it eligible for submit (cache fallback is scoped by area id, so
  // the backend would at least not reuse the old area's cache).
  LIVE.points = [];
  LIVE.pointsEmpty = false;
  LIVE.pointsStale = false;
  LIVE.pointsError = '';
  LIVE.pointsDistance = 0;
  LIVE.pointsFitted = false;
  LIVE.runArea = null;
  renderPoints([]);
  updateFenceChip();
  liveLog('已切换跑区', message, 'info');
  loadPoints(true, {quiet: false});
}
function updateCampusControls(){
  // Official default is runAreaModels[0].  A manual campus point is only
  // needed when there is no fence, or when several fences make the campus
  // ambiguous; with one fence its centroid is the anchor.
  const btn = $('#setCampusBtn');
  const count = Math.max(LIVE.fences.length, +LIVE.fenceCount || 0);
  if(btn){
    btn.hidden = count === 1;
    btn.title = count > 1
      ? '检测到多个围栏：把地图拖到你的学校后保存校区中心'
      : '学校没有围栏时：先把地图拖到学校，再点这里保存校区坐标';
  }
  renderRunAreas();
}

/* ---------------- overview ---------------- */
function renderOverviewEmpty(){
  LIVE.recordsError = null;
  LIVE.week = null; LIVE.weekLoaded = false;
  LIVE.weekError = null;
  LIVE.planError = null;
  LIVE.plan = null;
  LIVE.planDays = null;
  LIVE.semester = null;
  LIVE.semesterError = null;
  const bp = beijingParts();
  $('#ovTodayEyebrow').textContent = '今天 · ' + bp.month + ' 月 ' + bp.day + ' 日';
  $('#ovTodayTitle').textContent = '还没有记录';
  $('#ovTodayText').textContent = '登录后从服务器读取今天的跑步记录。';
  $('#ovTodayCount').textContent = '—';
  $('#ovTodayLimit').textContent = '/— 次';
  $('#ovKm').innerHTML = '—<small>km</small>';
  $('#ovCount').textContent = '—';
  $('#ovPace').textContent = '—';
  $('#ovDays').textContent = '—';
  $('#ovWeekRange').textContent = '—';
  $('#ovBars').innerHTML = '<div class="hint">本周还没有记录。</div>';
  $('#ovWeekTrend').innerHTML = '';
  $('#ovPlanGrid').innerHTML = '<div class="hint">登录后生成计划。</div>';
  $('#ovPlanRange').textContent = '—';
  renderSemesterCard(null);
  $('#ovRecords').innerHTML = '<div class="empty-state">登录后从服务器读取记录。</div>';
  $('#ovChecklist').innerHTML = '<div class="empty-state">登录后检查 token、围栏和点位。</div>';
  $('#ovPlan').textContent = '登录后生成计划。';
  $('#ovRhythm').textContent = '登录后显示。';
  const rulesConfigured = !!(LIVE.rules && LIVE.rules.configured);
  $('#ovModelChip').textContent = rulesConfigured ? '模型待预览' : '模型未配置';
  $('#ovModelChip').className = 'chip warn';
  $('#ovModelChip').innerHTML = '<i></i>' + $('#ovModelChip').textContent;
}
function recordState(r){
  if(r && (r.state === 'ok' || r.state === 'warn' || r.state === 'issue')) return r.state;
  const reasons = (r && r.reasons) || [];
  if(r && (r.normal_run === false || String(r.normal_run).toLowerCase() === 'false')) return 'issue';
  const rawCode = String(r && (r.status_code != null ? r.status_code : r.status));
  if(r && (rawCode === '1' || rawCode === '2')) return 'issue';
  if(reasons.some(x => !x.complete)) return 'issue';
  const text = String((r && r.status) || '');
  if(/问题|异常|无效|违规|可疑|失败/.test(text)) return 'issue';
  return r && r.complete ? 'ok' : 'warn';
}
function recordTag(r){
  const s = recordState(r);
  if(s === 'issue') return '<span class="tag red">有问题</span>';
  if(s === 'ok') return '<span class="tag ok">已通过</span>';
  return '<span class="tag warn">待核对</span>';
}
function overviewRecordRow(r){
  const tag = recordTag(r);
  const rr = r.rrid ? (String(r.rrid).slice(0, 4) + '…' + String(r.rrid).slice(-4)) : '—';
  return `<div class="record-row" data-goto="records" role="button" tabindex="0"><span class="rdate">${shortDate(r.start_ms)}</span><span class="rkm">${(+r.distance_km || 0).toFixed(2)} km</span><span class="rpace">${paceText(r.pace_min_per_km)}</span><span>${tag}</span><span class="rrid">${esc(rr)}</span></div>`;
}
function renderWeekTrend(weeks){
  const wrap = $('#ovWeekTrend');
  if(!wrap) return;
  if(!weeks.length){
    wrap.innerHTML = LIVE.weekError
      ? `<div class="hint" style="align-self:center">周汇总加载失败：${esc(LIVE.weekError)}</div>`
      : '<div class="hint" style="align-self:center">近 4 周还没有数据。</div>';
    return;
  }
  const max = Math.max(0.1, ...weeks.map(w => +w.distance_km || 0));
  wrap.innerHTML = weeks.map((w, i) => {
    const km = +w.distance_km || 0;
    const h = km ? Math.max(5, Math.round(46 * km / max)) : 2;
    const label = (w.week_start || '').slice(5).replace('-', '.');
    return `<div class="wb ${i === weeks.length - 1 ? 'cur' : ''}" title="${esc((w.week_start || '') + ' – ' + (w.week_end || ''))}"><i style="height:${h}px"></i><span>${esc(label)}</span></div>`;
  }).join('');
}
function renderSemesterInto(prefix, semester, error){
  const v = (semester && semester.view) || null;
  const el = suffix => $('#' + prefix + suffix);
  const title = el('SemesterTitle'), pct = el('SemesterPct');
  const done = el('SemesterDone'), left = el('SemesterLeft'), days = el('SemesterDays');
  const doneLabel = el('SemesterDoneLabel'), leftLabel = el('SemesterLeftLabel');
  const bar = el('SemesterBar'), source = el('SemesterSource'), note = el('SemesterNote');
  const goalLink = el('SemesterGoalLink');
  if(!v){
    if(title) title.textContent = error ? '学期数据读取失败' : '登录后读取';
    if(pct) pct.textContent = '—';
    if(done) done.textContent = '—';
    if(left) left.textContent = '—';
    if(doneLabel) doneLabel.textContent = '已完成';
    if(leftLabel) leftLabel.textContent = '还差';
    if(days) days.textContent = '—';
    if(bar){
      bar.style.width = '0%';
      if(bar.parentElement) bar.parentElement.hidden = false;
    }
    if(source) source.textContent = error ? '失败' : '—';
    if(note){ note.hidden = true; note.textContent = ''; }
    if(goalLink) goalLink.hidden = true;
    return;
  }
  // goalMethod=2 is the indoor-exercise counter, not the semester goal: the
  // official app renders it as its own ring segment ("次") and keeps the
  // outdoor distance separate.  A local target (设置 → 本学期目标) can fill
  // in the outdoor goal when the server has none.
  const hasIndoor = v.indoor_goal != null;
  const countMode = v.goal_km == null && v.goal_count != null;
  const distanceMode = v.goal_km != null;
  const goal = countMode ? v.goal_count : v.goal_km;
  const got = countMode ? v.valid_count : v.done_km;
  const percent = v.completed_pct != null ? +v.completed_pct
    : (goal && got != null ? Math.min(100, got / goal * 100) : 0);
  if(title) title.textContent = v.sname || '本学期';
  if(pct){
    if(distanceMode || countMode){
      pct.textContent = (percent != null ? percent.toFixed(1) + '%' : '—') +
        (v.goal_count != null ? (' · ' + (v.valid_count || 0) + '/' + v.goal_count + ' 次') : '');
    }else{
      pct.textContent = v.completed_pct != null
        ? (+v.completed_pct).toFixed(1) + '%'
        : (v.outdoor_km != null ? '户外未设目标' : '未设目标');
    }
  }
  if(done) done.innerHTML = got != null
    ? (countMode ? (got + '<small> 次</small>') : (got.toFixed(1) + '<small> km</small>'))
    : '—';
  if(left) left.innerHTML = distanceMode
    ? (v.remaining_km != null ? (v.remaining_km.toFixed(1) + '<small> km</small>') : '—')
    : (countMode
      ? (v.remaining_count != null ? (v.remaining_count + '<small> 次</small>') : '—')
      : (hasIndoor && v.indoor_remaining != null
        ? (v.indoor_remaining + '<small> 次</small>') : '—'));
  if(doneLabel) doneLabel.textContent = (distanceMode || countMode) ? '已完成' : '户外里程';
  if(leftLabel) leftLabel.textContent = (distanceMode || countMode)
    ? '还差' : (hasIndoor ? '室内还差' : '还差');
  if(days) days.textContent = v.days_left != null
    ? (v.days_left <= 0 ? '已结束' : (v.days_left + ' 天')) : '—';
  // A 0% bar with no goal behind it reads as "nothing recorded"; hide the
  // track instead of drawing a progress bar that can never fill.
  const showBar = v.completed_pct != null || distanceMode || countMode;
  if(bar){
    bar.style.width = Math.max(0, Math.min(100, percent)).toFixed(1) + '%';
    if(bar.parentElement) bar.parentElement.hidden = !showBar;
  }
  if(source) source.textContent = v.goal_source === 'local'
    ? '学期汇总 · 本机目标'
    : (v.source === 'personal' ? '个人完成度' : (v.source === 'summary' ? '学期汇总' : '—'));
  if(goalLink) goalLink.hidden = distanceMode || countMode;
  if(note){
    if(hasIndoor){
      note.hidden = false;
      note.textContent = '室内锻炼 ' + (v.indoor_valid || 0) + '/' + v.indoor_goal
        + ' 次（本学期目标）· 户外跑不计入';
    }else if(v.outdoor_km != null && countMode){
      note.hidden = false;
      note.textContent = '本学期有效里程 ' + v.outdoor_km.toFixed(2) + ' km';
    }else{
      note.hidden = true;
      note.textContent = '';
    }
  }
}
function renderSemesterCard(semester){
  renderSemesterInto('ov', semester, LIVE.semesterError);
}
function beijingDateTime(ms){
  if(!ms) return '';
  let n = +ms;
  if(!isFinite(n) || n <= 0) return String(ms);
  if(n < 1e11) n *= 1000;
  try{
    return new Date(n).toLocaleString('zh-CN',
      {timeZone: 'Asia/Shanghai', hour12: false});
  }catch(e){
    return new Date(n).toLocaleString('zh-CN', {hour12: false});
  }
}
function violationTime(v){
  if(v == null || v === '') return '—';
  if(typeof v === 'number') return beijingDateTime(v);
  const s = String(v).trim();
  return /^\d{10,13}$/.test(s) ? beijingDateTime(+s) : s;
}
function renderProfileEmpty(){
  const s = LIVE.session || {};
  const logged = !!((s.token_present || s.token) && !s.disabled);
  const name = logged ? (s.nickname || s.name || '') : '';
  const avatar = s.disabled ? '!' : (name || '?').slice(0, 1);
  const display = s.disabled ? '旧账号已停用'
    : (name || (logged ? ('uid ' + (s.uid || '—')) : '未登录'));
  const setText = (id, v) => {
    const el = $('#' + id);
    if(el) el.textContent = (v == null || v === '') ? '—' : v;
  };
  if($('#profAvatar')) $('#profAvatar').textContent = avatar;
  if($('#profName')) $('#profName').textContent = display;
  if($('#profMeta')) $('#profMeta').textContent = s.disabled
    ? '该会话已按用户要求停用'
    : (logged ? ([s.schoolName, s.campusName].filter(Boolean).join(' · ')
                 || '登录后显示学校 / 校区') : '登录后显示学校 / 校区');
  if($('#profUidChip')) $('#profUidChip').textContent =
    (logged && s.uid) ? ('uid ' + s.uid) : 'uid —';
  if($('#profUnidChip')) $('#profUnidChip').textContent =
    (logged && s.unid) ? ('unid ' + s.unid) : 'unid —';
  if($('#profVerifiedChip')){
    $('#profVerifiedChip').textContent = s.disabled ? '账号已停用'
      : (logged ? '资料未加载' : '未验证');
    $('#profVerifiedChip').className = 'chip '
      + (s.disabled ? 'red' : (logged ? 'warn' : ''));
  }
  if($('#profSemesterChip')){
    $('#profSemesterChip').textContent = '学期未加载';
    $('#profSemesterChip').className = 'chip';
  }
  if($('#profUpdated')) $('#profUpdated').textContent = '—';
  if($('#profSource')) $('#profSource').textContent = '—';
  ['profRealName', 'profNickname', 'profGradeClass', 'profSchool',
   'profCampus', 'profBody', 'profUid', 'profUnid'].forEach(id => setText(id, ''));
  setText('profLoginState', s.disabled ? '账号已停用'
    : (logged ? '已登录' : '未登录'));
  setText('profDevice', logged ? (deviceLabelText(s) || '—') : '—');
  setText('profSession', s.disabled ? '已停用'
    : (logged ? '已加载 token' : '未登录'));
  renderSemesterInto('prof', null, '');
  if($('#violState')){
    $('#violState').textContent = '未查询';
    $('#violState').className = 'tag';
  }
  if($('#violList')){
    $('#violList').innerHTML = '<div class="violation-empty">'
      + (logged ? '点「查询违规」读取本账号的违规记录。'
                : '登录后点「查询违规」。') + '</div>';
  }
  if($('#violCheck')){
    $('#violCheck').disabled = false;
    $('#violCheck').textContent = '查询违规';
  }
  if($('#profRefresh')){
    $('#profRefresh').disabled = false;
    $('#profRefresh').textContent = '刷新资料';
  }
}
async function loadProfile(opts){
  opts = opts || {};
  if(!hasSession()){
    _profileSeq++;
    LIVE.profile = null; LIVE.profileError = null; LIVE.profileLoading = false;
    renderProfileEmpty();
    return;
  }
  const seq = ++_profileSeq;
  LIVE.profileLoading = true;
  const btn = $('#profRefresh');
  if(btn){ btn.disabled = true; btn.textContent = '刷新中…'; }
  try{
    const j = await liveApi('real_profile', {}, 60000);
    if(seq !== _profileSeq) return;
    if(!liveOk(j)){
      LIVE.profile = null;
      LIVE.profileError = j.message || '资料读取失败';
      renderProfile();
      liveLog('个人资料读取失败', LIVE.profileError, 'warn');
      if(!opts.silent) toast('资料读取失败：' + LIVE.profileError);
      return;
    }
    LIVE.profile = j.data || {};
    LIVE.profileError = null;
    LIVE.profileAt = Date.now();
    renderProfile();
    const errs = Array.isArray(LIVE.profile.errors) ? LIVE.profile.errors : [];
    if(errs.length) liveLog('部分资料读取失败', errs.join('；'), 'warn');
    else liveLog('个人资料已刷新',
      (LIVE.profile.profile && (LIVE.profile.profile.nickname
        || LIVE.profile.profile.name)) || '已读取', 'ok');
    if(!opts.silent) toast('个人资料已刷新');
  } finally {
    if(seq === _profileSeq){
      LIVE.profileLoading = false;
      if(btn){ btn.disabled = false; btn.textContent = '刷新资料'; }
    }
  }
}
function renderProfile(){
  const d = LIVE.profile || {};
  const p = d.profile || {};
  const s = LIVE.session || {};
  const logged = hasSession();
  const name = p.nickname || p.name || s.nickname || s.name || '';
  const setText = (id, v) => {
    const el = $('#' + id);
    if(el) el.textContent = (v == null || v === '') ? '—' : v;
  };
  if($('#profAvatar')) $('#profAvatar').textContent = (name || '?').slice(0, 1);
  if($('#profName')) $('#profName').textContent =
    name || (logged ? ('uid ' + (p.uid || s.uid || '—')) : '未登录');
  if($('#profMeta')) $('#profMeta').textContent =
    ([p.schoolName || s.schoolName, p.campusName || s.campusName]
      .filter(Boolean).join(' · ') || '登录后显示学校 / 校区');
  if($('#profUidChip')) $('#profUidChip').textContent = (p.uid || s.uid)
    ? ('uid ' + (p.uid || s.uid)) : 'uid —';
  if($('#profUnidChip')) $('#profUnidChip').textContent = (p.unid || s.unid)
    ? ('unid ' + (p.unid || s.unid)) : 'unid —';

  if($('#profVerifiedChip')){
    if(LIVE.profileError){
      $('#profVerifiedChip').textContent = '资料读取失败';
      $('#profVerifiedChip').className = 'chip red';
    }else if(p.verified){
      $('#profVerifiedChip').textContent = '资料已验证';
      $('#profVerifiedChip').className = 'chip ok';
    }else{
      $('#profVerifiedChip').textContent = logged ? '本地资料' : '未验证';
      $('#profVerifiedChip').className = 'chip ' + (logged ? 'warn' : '');
    }
  }
  if($('#profUpdated')) $('#profUpdated').textContent =
    LIVE.profileAt ? ('更新 ' + nowHM()) : '—';
  if($('#profSource')) $('#profSource').textContent =
    p.verified ? 'user/info'
      : ((p.name || p.nickname || s.name || s.nickname) ? '本地会话' : '—');

  const withUnit = (v, unit) => {
    if(v == null || v === '' || +v === 0) return '';
    const text = String(v).trim();
    return /^-?\d+(\.\d+)?$/.test(text) ? (text + ' ' + unit) : text;
  };
  const body = [
    withUnit(p.height, 'cm'),
    withUnit(p.weight, 'kg')
  ].filter(Boolean).join(' · ');
  setText('profRealName', p.name || s.name);
  setText('profNickname', p.nickname || s.nickname);
  setText('profGradeClass', p.gradeClass);
  setText('profSchool', p.schoolName || s.schoolName);
  setText('profCampus', p.campusName || s.campusName);
  setText('profBody', body);
  setText('profUid', p.uid || s.uid);
  setText('profUnid', p.unid || s.unid);
  setText('profLoginState', s.disabled ? '账号已停用'
    : (logged ? (p.verified ? '已登录 · 已验证' : '已登录') : '未登录'));
  setText('profDevice', logged ? (deviceLabelText(s) || '—') : '—');
  setText('profSession', logged ? '已加载 token' : '未登录');

  const sem = d.semester || LIVE.semester;
  const semErr = d.semester_error || LIVE.semesterError;
  renderSemesterInto('prof', sem, semErr);
  if($('#profSemesterChip')){
    const hasSem = !!(sem && sem.view);
    $('#profSemesterChip').textContent = hasSem ? '学期已加载'
      : (semErr ? '学期读取失败' : '学期未加载');
    $('#profSemesterChip').className = 'chip '
      + (hasSem ? 'ok' : (semErr ? 'warn' : ''));
  }
  if(!LIVE.violations && !LIVE.violationsError && $('#violList')){
    $('#violList').innerHTML = '<div class="violation-empty">'
      + '点「查询违规」读取本账号的违规记录。</div>';
  }
}
async function loadViolations(){
  if(!hasSession()){ toast('先登录'); return; }
  const btn = $('#violCheck');
  if(btn && btn.disabled) return;
  const seq = ++_violSeq;
  if(btn){ btn.disabled = true; btn.textContent = '查询中…'; }
  try{
    const j = await liveApi('real_violations', {page: 1, page_size: 20}, 60000);
    if(seq !== _violSeq) return;
    if(!liveOk(j)){
      LIVE.violations = null;
      LIVE.violationsError = j.message || '违规查询失败';
      renderViolations(null, LIVE.violationsError);
      liveLog('违规自查失败', LIVE.violationsError, 'err');
      toast('违规查询失败：' + LIVE.violationsError);
      return;
    }
    LIVE.violations = j.data || {};
    LIVE.violationsError = null;
    LIVE.violationsAt = Date.now();
    renderViolations(LIVE.violations);
    const state = LIVE.violations.state;
    if(state === 'violations'){
      liveLog('违规自查：发现记录',
        (LIVE.violations.count || (LIVE.violations.records || []).length)
        + ' 条；以官方 App 为准', 'warn');
      toast('查询到违规记录，请查看列表');
    }else if(state === 'none_found'){
      liveLog('违规自查：本次未发现', '只代表本次查询', 'ok');
      toast('本次查询暂未发现违规记录');
    }else if(state === 'unknown'){
      liveLog('违规自查：结果未知', LIVE.violations.message || '', 'warn');
      toast('结果未知，请勿当作正常');
    }else{
      liveLog('违规自查失败', LIVE.violations.message || '未知状态', 'err');
      toast('查询失败，请稍后重试');
    }
  } finally {
    if(seq === _violSeq && btn){
      btn.disabled = false; btn.textContent = '查询违规';
    }
  }
}
function renderViolations(data, error){
  const tag = $('#violState'), list = $('#violList');
  if(!tag || !list) return;
  const state = error ? 'failed' : ((data && data.state) || 'unknown');
  const meta = {
    none_found: ['未发现', 'tag ok'],
    violations: ['有记录', 'tag red'],
    unknown: ['结果未知', 'tag warn'],
    failed: ['查询失败', 'tag red']
  }[state] || ['结果未知', 'tag warn'];
  tag.textContent = meta[0];
  tag.className = meta[1];

  const checked = data && data.checked_at ? beijingDateTime(data.checked_at) : '';
  let checkedMs = data && data.checked_at ? +data.checked_at : 0;
  if(checkedMs && checkedMs < 1e11) checkedMs *= 1000;
  const stale = !!(data && (data.stale ||
    (checkedMs && Date.now() - checkedMs > 5 * 60 * 1000)));
  let msg = error || (data && data.message) || '';
  if(state === 'none_found') msg = msg || '本次查询暂未发现违规记录';
  if(state === 'unknown') msg = msg || '响应缺少个人违规状态字段，结果未知';
  if(state === 'failed') msg = msg || '查询失败';
  const records = Array.isArray(data && data.records) ? data.records : [];
  if(!records.length){
    list.innerHTML = '<div class="violation-empty">' + esc(msg)
      + (checked ? ' · ' + esc(checked) : '')
      + (stale ? ' · 结果已过期' : '') + '</div>';
    return;
  }
  const rows = records.map(r => {
    const reason = r.reason || '（服务器未返回原因）';
    const rrid = r.rrid ? ('rrid ' + r.rrid) : '';
    return '<div class="violation-row">'
      + '<span class="vtime">' + esc(violationTime(r.time)) + '</span>'
      + '<span class="vreason">' + esc(reason) + '</span>'
      + '<span class="vrrid">' + esc(rrid) + '</span>'
      + '</div>';
  }).join('');
  list.innerHTML = rows + (stale
    ? '<div class="violation-empty">结果已过期，建议重新查询。</div>' : '');
}
function renderPlanGrid(){
  const wrap = $('#ovPlanGrid');
  if(!wrap) return;
  if($('#ovPlanTitle')){
    const n = LIVE.planDays;
    $('#ovPlanTitle').textContent = n === 0
      ? '本学期已结束'
      : ('未来 ' + (n == null ? 14 : n) + ' 天');
  }
  const slots = (LIVE.plan && LIVE.plan.slots) || [];
  if(!slots.length){
    wrap.innerHTML = LIVE.planError
      ? `<div class="hint">计划生成失败：${esc(LIVE.planError)}</div>`
      : (LIVE.planDays === 0
        ? '<div class="hint">本学期已结束，不再生成计划。</div>'
        : '<div class="hint">暂时没有可用计划。</div>');
    if($('#ovPlanRange')) $('#ovPlanRange').textContent = '—';
    return;
  }
  const doneDays = new Set((LIVE.records || []).map(r => r.date).filter(Boolean));
  let first = '', last = '';
  const base = parseDateKey(beijingTodayKey());
  wrap.innerHTML = slots.map((s, idx) => {
    if(!s){
      const rd = new Date(base.getFullYear(), base.getMonth(), base.getDate() + idx);
      const rk = (rd.getMonth() + 1) + '/' + rd.getDate();
      return `<div class="plan-row rest"><span class="pd">${rk}</span><span class="pv">休息 / 恢复</span><span></span></div>`;
    }
    first = first || s.day;
    last = s.day;
    const isDone = doneDays.has(s.day);
    const daysAgo = dateToDaysAgo(s.day);
    const allowed = planSlotAllowed(s.day);
    const future = daysAgo !== null && daysAgo < 0;
    const disabled = isDone || !allowed;
    const button = isDone ? '已完成' : (allowed ? '填' : (future ? '未来' : '过期'));
    const title = isDone ? '当天已有记录'
      : (allowed ? '填入这一天的距离、配速、步频'
        : (future ? '未来日期不能提交' : '超过 3 天，不能再补交'));
    return `<div class="plan-row ${isDone ? 'done' : ''}"><span class="pd">${esc((s.day || '').slice(5).replace('-', '/'))} ${esc(s.weekday ? '周' + s.weekday : '')}</span><span class="pv">${esc(s.distance_km)} km · ${esc(s.pace_hhmmss || '')}${isDone ? ' · 已完成' : ''}</span><button class="pf" data-plan-day="${esc(s.day)}" title="${esc(title)}" ${disabled ? 'disabled' : ''}>${button}</button></div>`;
  }).join('');
  if($('#ovPlanRange')) $('#ovPlanRange').textContent = first ? (first.slice(5) + ' – ' + last.slice(5)) : '—';
}
function planSlotAllowed(day){
  // Submissions only accept today and the previous three days.  The 14-day
  // plan contains mostly future rows; they stay visible but cannot fill a
  // date the backend would reject.
  const n = dateToDaysAgo(day);
  return n !== null && n >= 0 && n <= 3;
}
function nextPlanSlot(slots){
  // The "next suggestion" should skip days that already have a record;
  // otherwise a finished run keeps being recommended.
  const today = beijingTodayKey();
  const done = new Set((LIVE.records || []).map(r => r.date).filter(Boolean));
  return (slots || []).find(s =>
    s && s.day && s.day >= today && !done.has(s.day)) || null;
}
function applyPlanSlot(slot, opts){
  opts = opts || {};
  if(!slot) return false;
  const day = slot.day || '';
  const allowed = planSlotAllowed(day);
  const km = +slot.distance_km;
  const pace = +slot.pace_min_per_km;
  const cadence = +slot.cadence;
  if(isFinite(km)){ distanceManual = true; $('#inKm').value = km; }
  if(isFinite(pace)) $('#inPace').value = pace.toFixed(2);
  if(isFinite(cadence)) $('#inCad').value = cadence;
  if(allowed && day){
    LIVE.runDate = day;
    LIVE.daily = null;
    updateRunDateLabel();
    refreshChecks();
    refreshDailyCount();
  }
  refreshParams();
  redraw();
  schedulePointsForDistance();
  scheduleModelPreview();
  go('run');
  if(opts.tab) switchRunTab(opts.tab);
  if(allowed){
    toast('已填入 ' + dateDisplay(day) + ' 的档位');
  }else{
    toast('已填入参数；未来日期不能提交，提交日期仍为 ' +
      dateDisplay(LIVE.runDate || beijingTodayKey()));
  }
  return allowed;
}

async function loadOverview(){
  const seq = ++_overviewSeq;
  if(!hasSession()){
    LIVE.records = []; LIVE.recordsLoaded = false;
    LIVE.recordsLoading = false; LIVE.recordsError = null;
    LIVE.week = null; LIVE.weekLoaded = false;
    LIVE.weekError = null; LIVE.planError = null;
    renderRecords();
    renderOverviewEmpty();
    return;
  }
  // The plan has to stop at the semester deadline: a 14-day horizon that runs
  // past it would recommend runs the school will never count.  The semester
  // call is therefore awaited first, then the plan is generated for the days
  // that actually remain (today inclusive).
  const [rj, wj, sj] = await Promise.all([
    liveApi('real_recent_records', {limit: 20}),
    liveApi('real_weekly_summary', {weeks: 4}),
    liveApi('real_semester')
  ]);
  if(seq !== _overviewSeq) return;
  const semOk = liveOk(sj);
  const semView = semOk && sj.data ? sj.data.view : null;
  const planDays = planHorizonDays(semView);
  const pj = planDays > 0
    ? await liveApi('real_plan', {days: planDays})
    : {code: 200, message: 'semester over', data: {days: 0, slots: []}};
  // Logout or an account switch may have happened while these requests were
  // in flight.  Applying them now would show the previous account's records
  // and semester under the new session.
  if(seq !== _overviewSeq) return;
  const recordsOk = liveOk(rj);
  const records = recordsOk ? ((rj.data && rj.data.records) || []) : [];
  LIVE.records = records;
  LIVE.recordsLoaded = recordsOk;
  LIVE.recordsError = recordsOk ? null : (rj.message || '记录加载失败');
  renderRecords();
  const weekOk = liveOk(wj);
  const weeks = weekOk ? (((wj.data && wj.data.weeks) || [])) : [];
  const week = weeks[weeks.length - 1] || null;
  LIVE.week = week;
  LIVE.weekLoaded = weekOk;
  LIVE.weekError = weekOk ? null : (wj.message || '周汇总加载失败');
  LIVE.planDays = planDays;
  LIVE.planError = liveOk(pj) ? null : (pj.message || '计划生成失败');
  LIVE.plan = liveOk(pj) ? (pj.data || null) : null;
  LIVE.semester = semOk ? (sj.data || null) : null;
  LIVE.semesterError = semOk ? null : (sj.message || '学期数据加载失败');
  renderOverview(records, week, weeks, LIVE.semester);
  loadSemesterGoal();          // the sid only becomes known with the semester
  if(!recordsOk) liveLog('记录加载失败', LIVE.recordsError, 'err');
  if(!weekOk) liveLog('周汇总加载失败', LIVE.weekError, 'warn');
  if(!liveOk(pj)) liveLog('计划生成失败', LIVE.planError, 'warn');
  if(!semOk) liveLog('学期数据加载失败', LIVE.semesterError, 'warn');
}

function planHorizonDays(view){
  // 14 days is the default only while the semester end is unknown.
  if(!view || view.days_left == null) return 14;
  if(view.days_left < 0) return 0;          // semester already ended
  return Math.max(1, Math.min(14, +view.days_left + 1));
}

/* The overview's right-hand cards depend on data that arrives after its first
 * paint (points, rules, model, week count).  Keep them reactive instead of
 * baking the values into renderOverview and leaving stale "未加载/未配置". */
function refreshOverviewState(){
  const wrap = $('#ovChecklist');
  if(!wrap || !hasSession()) return;
  const tokenOk = !!(LIVE.checked && liveOk(LIVE.checked));
  const pointsOk = livePoints.length > 0;
  const rulesOk = !!(LIVE.rules && LIVE.rules.configured);
  const checklist = [
    {ok: tokenOk, text: tokenOk ? 'token 有效' : 'token 未验证'},
    {ok: pointsOk, text: pointsOk ? ('点位已加载 · ' + livePoints.length + ' 个') : '点位未加载'},
    {ok: rulesOk, text: rulesOk ? '学校要求已配置' : '学校要求未配置'}
  ];
  wrap.innerHTML = checklist.map(x =>
    `<div class="mini-check ${x.ok ? 'done' : 'warn'}">${esc(x.text)}</div>`).join('');

  const cap = LIVE.rules && LIVE.rules.configured && LIVE.rules.rules
    ? LIVE.rules.rules.max_per_day : null;
  if($('#ovTodayLimit')) $('#ovTodayLimit').textContent = cap
    ? ('/' + cap + ' 次') : '/— 次';

  if($('#ovRhythm')){
    const week = LIVE.week;
    if(LIVE.weekLoaded || LIVE.weekError){
      $('#ovRhythm').textContent = !LIVE.weekLoaded
        ? ('周汇总加载失败：' + LIVE.weekError)
        : (`本周已跑 ${(week && week.count) || 0} 次` +
           (cap ? ` · 学校每日上限 ${cap} 次` : ' · 未配置学校每日上限'));
    }
  }

  if($('#ovModelChip')){
    const modelOk = !!(LIVE.model && LIVE.model.configured);
    const modelBad = modelOk && LIVE.model.verdict === 'fail';
    $('#ovModelChip').textContent = modelOk
      ? ('模型 ' + LIVE.model.score)
      : (rulesOk ? '模型待预览' : '模型未配置');
    $('#ovModelChip').className = 'chip ' + (modelOk ? (modelBad ? 'warn' : 'ok') : 'warn');
    $('#ovModelChip').innerHTML = '<i></i>' + $('#ovModelChip').textContent;
  }
}
function renderOverview(records, week, weeks, semester){
  const bp = beijingParts();
  const recordsFailed = !!LIVE.recordsError;
  const today = records.filter(r => r.date === bp.iso);
  $('#ovTodayEyebrow').textContent = '今天 · ' + bp.month + ' 月 ' + bp.day + ' 日';
  if(recordsFailed){
    $('#ovTodayTitle').textContent = '记录加载失败';
    $('#ovTodayText').textContent = LIVE.recordsError;
    $('#ovTodayCount').textContent = '—';
  }else{
    $('#ovTodayTitle').textContent = today.length ? ('今天已跑 ' + today.length + ' 次') : '今天还没有记录';
    if(today.length){
      const r = today[0];
      const state = recordState(r);
      $('#ovTodayText').textContent = `${(+r.distance_km || 0).toFixed(2)} km · ${paceText(r.pace_min_per_km)} · ${state === 'issue' ? '有问题' : state === 'ok' ? '已通过' : '待核对'}`;
    }else{
      $('#ovTodayText').textContent = '还没有跑步记录；可以去跑步页生成一条。';
    }
    $('#ovTodayCount').textContent = String(today.length);
  }

  if(week){
    $('#ovKm').innerHTML = (+week.distance_km || 0).toFixed(1) + '<small>km</small>';
    $('#ovCount').textContent = String(week.count || 0);
    $('#ovPace').textContent = paceText(week.avg_pace_min_per_km);
    $('#ovDays').textContent = String(week.active_days || 0);
    $('#ovWeekRange').textContent = (week.week_start || '').slice(5) + ' – ' + (week.week_end || '').slice(5);
    const daily = week.daily || [];
    const max = Math.max(0.1, ...daily.map(d => +d.distance_km || 0));
    const labels = ['一', '二', '三', '四', '五', '六', '日'];
    $('#ovBars').innerHTML = daily.map((d, i) => {
      const km = +d.distance_km || 0;
      const h = km ? Math.max(8, Math.round(78 * km / max)) : 2;
      return `<div class="bar"><b>${km ? km.toFixed(1) : '–'}</b><i class="${d.date === bp.iso ? 'hot' : ''}" style="height:${h}%"></i><span>${labels[i] || ''}</span></div>`;
    }).join('');
  }else{
    $('#ovKm').innerHTML = '—<small>km</small>';
    $('#ovCount').textContent = '—'; $('#ovPace').textContent = '—'; $('#ovDays').textContent = '—';
    $('#ovWeekRange').textContent = '—';
    $('#ovBars').innerHTML = LIVE.weekError
      ? `<div class="hint">周汇总加载失败：${esc(LIVE.weekError)}</div>`
      : '<div class="hint">本周还没有记录。</div>';
  }

  renderWeekTrend(weeks || []);
  renderSemesterCard(semester);
  renderPlanGrid();

  $('#ovRecords').innerHTML = recordsFailed
    ? `<div class="empty-state">记录加载失败：${esc(LIVE.recordsError)}</div>`
    : (records.length ? records.slice(0, 4).map(overviewRecordRow).join('')
                      : '<div class="empty-state">暂无跑步记录。</div>');

  const slot = nextPlanSlot(LIVE.plan && (LIVE.plan.slots || []));
  if(slot){
    $('#ovPlan').innerHTML = `${esc(slot.day || '')}<br>${esc(slot.distance_km)} km · ${esc(slot.pace_hhmmss || '')} · ${esc(slot.route || '')}`;
  }else{
    $('#ovPlan').textContent = LIVE.planError
      ? ('计划生成失败：' + LIVE.planError) : '暂时没有可用计划。';
  }
  refreshOverviewState();
  renderSession();
}

/* ---------------- records ---------------- */
async function loadRecords(){
  if(!hasSession()){
    _recordsSeq++;
    LIVE.records = []; LIVE.recordsLoaded = false; LIVE.recordsLoading = false;
    LIVE.recordsError = null;
    renderRecords(); refreshChecks(); return;
  }
  const seq = ++_recordsSeq;
  LIVE.recordsLoading = true;
  renderRecords();
  const j = await liveApi('real_recent_records', {limit: 20});
  if(seq !== _recordsSeq) return;
  LIVE.recordsLoading = false;
  if(!liveOk(j)){
    liveLog('记录加载失败', j.message || '', 'err');
    LIVE.records = []; LIVE.recordsLoaded = false;
    LIVE.recordsError = j.message || '记录加载失败';
    renderRecords();
    if(LIVE.plan) renderPlanGrid();
    refreshChecks(); return;
  }
  LIVE.records = (j.data && j.data.records) || [];
  LIVE.recordsLoaded = true;
  LIVE.recordsError = null;
  renderRecords();
  if(LIVE.plan) renderPlanGrid();
  refreshChecks();
  const key = LIVE.runDate || beijingTodayKey();
  if(!LIVE.daily || LIVE.daily.key !== key ||
     LIVE.daily.runs === null || LIVE.daily.runs === undefined){
    refreshDailyCount();
  }else if(typeof scheduleModelPreview === 'function') scheduleModelPreview();
  loadReceipts();
}
let rdMap = null, rdTrackLine = null, rdTrackGlow = null, rdPtLayer = null;
let rdStart = null, rdEnd = null;
function ensureRecordMap(){
  if(rdMap || !$('#rdMap')) return rdMap;
  rdMap = L.map('rdMap', {zoomControl: true, attributionControl: false,
                          scrollWheelZoom: false}).setView(CENTER, CENTER_ZOOM);
  L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
    {subdomains: '1234', maxNativeZoom: 18, maxZoom: 19,
     attribution: '高德卫星'}).addTo(rdMap);
  rdTrackGlow = L.polyline([], {color: themeColor('--highlighter', '#F2D25C'),
                                weight: 9, opacity: .45,
                                lineCap: 'round'}).addTo(rdMap);
  rdTrackLine = L.polyline([], {color: themeColor('--ink-blue', '#2E4A7D'),
                                weight: 2.6, opacity: .95,
                                lineCap: 'round'}).addTo(rdMap);
  rdPtLayer = L.layerGroup().addTo(rdMap);
  return rdMap;
}
function clearRecordMap(){
  if(rdTrackLine) rdTrackLine.setLatLngs([]);
  if(rdTrackGlow) rdTrackGlow.setLatLngs([]);
  if(rdPtLayer) rdPtLayer.clearLayers();
  if(rdStart && rdMap && rdMap.hasLayer(rdStart)) rdMap.removeLayer(rdStart);
  if(rdEnd && rdMap && rdMap.hasLayer(rdEnd)) rdMap.removeLayer(rdEnd);
  rdStart = rdEnd = null;
  const hint = $('#rdMapHint');
  if(hint) hint.textContent = '选择记录后显示轨迹与打卡点。';
  if($('#rdGpx')) $('#rdGpx').disabled = true;
  if($('#rdSaveRoute')) $('#rdSaveRoute').disabled = true;
  if($('#rdCharts')) $('#rdCharts').disabled = true;
  if($('#rdFit')) $('#rdFit').disabled = true;
  if($('#rdReplay')) $('#rdReplay').disabled = true;
  if($('#rdVerify')) $('#rdVerify').disabled = true;
  if($('#rdVerifyBox')) $('#rdVerifyBox').hidden = true;
  if($('#recFit')) $('#recFit').disabled = true;
}
function renderRecordMap(detail){
  const track = Array.isArray(detail && detail.track) ? detail.track : [];
  const five = Array.isArray(detail && detail.five_point) ? detail.five_point : [];
  const meta = (detail && detail.detail) || {};
  const hint = $('#rdMapHint');
  if(!track.length){
    clearRecordMap();
    if(hint){
      if(meta.obs_error && /404|不存在/.test(meta.obs_error))
        hint.textContent = '这条记录的服务器轨迹已不存在（OBS 404），只能查看摘要。';
      else if(meta.obs_error)
        hint.textContent = 'OBS 轨迹读取失败：' + meta.obs_error;
      else if(meta.json_from_obs)
        hint.textContent = '这条记录的轨迹在 OBS，但服务端没有返回可读对象。';
      else
        hint.textContent = '这条记录没有读到轨迹（服务端未返回 allLocJson）。';
    }
    return;
  }
  ensureRecordMap();
  if(!rdMap) return;
  const line = track.map(p => [p.lat, p.lng]).filter(p => isFinite(p[0]) && isFinite(p[1]));
  if(line.length < 2){ clearRecordMap(); return; }
  rdTrackGlow.setLatLngs(line);
  rdTrackLine.setLatLngs(line);
  rdPtLayer.clearLayers();
  if(rdStart && rdMap.hasLayer(rdStart)) rdMap.removeLayer(rdStart);
  if(rdEnd && rdMap.hasLayer(rdEnd)) rdMap.removeLayer(rdEnd);
  rdStart = L.circleMarker(line[0], {
    radius: 5, color: themeColor('--paper-2', '#FBF8F0'), weight: 2,
    fillColor: themeColor('--ink-blue', '#2E4A7D'), fillOpacity: 1
  }).addTo(rdMap).bindTooltip('起点');
  rdEnd = L.circleMarker(line[line.length - 1], {
    radius: 5, color: themeColor('--paper-2', '#FBF8F0'), weight: 2,
    fillColor: themeColor('--stamp', '#B6483C'), fillOpacity: 1
  }).addTo(rdMap).bindTooltip('终点');
  five.forEach((p, i) => {
    const lat = +(p.glat ?? p.gLat ?? p.lat);
    const lng = +(p.glon ?? p.gLng ?? p.lng ?? p.lon);
    if(!isFinite(lat) || !isFinite(lng)) return;
    const ok = !!(p.isPass ?? p.is_pass ?? p.pass);
    const marker = L.circleMarker([lat, lng], {
      radius: 6, color: themeColor('--paper-2', '#FBF8F0'), weight: 2,
      fillColor: ok ? themeColor('--green', '#3F7A4B')
                    : themeColor('--stamp', '#B6483C'), fillOpacity: .95
    });
    marker.__pass = ok;
    marker.addTo(rdPtLayer)
      .bindTooltip(esc(p.pointName || p.name || ('点位 ' + (i + 1))) + (ok ? ' · 已通过' : ' · 未通过'));
  });
  rdMap.fitBounds(L.latLngBounds(line).pad(.18));
  setTimeout(() => { try{ rdMap.invalidateSize(); }catch(e){} }, 80);
  if(hint) hint.textContent = `${line.length} 个轨迹点 · ${five.length} 个打卡点`;
  if($('#rdGpx')) $('#rdGpx').disabled = false;
  if($('#rdSaveRoute')) $('#rdSaveRoute').disabled = false;
  if($('#rdCharts')) $('#rdCharts').disabled = false;
  if($('#rdFit')) $('#rdFit').disabled = false;
  if($('#rdReplay')) $('#rdReplay').disabled = false;
  if($('#rdVerify')) $('#rdVerify').disabled = false;
  if($('#recFit')) $('#recFit').disabled = false;
}
function clearRecordDetail(){
  ['rdPace','rdTime','rdCadence','rdSteps','rdTrack','rdSource','rdCheat','rdRulePolicy','rdPaceBand','rdRrid'].forEach(id => {
    const el = $('#' + id); if(el) el.textContent = '—';
  });
  ['rdCheat','rdRulePolicy','rdPaceBand','rdSource'].forEach(id => {
    const el = $('#' + id); if(el){ el.className = ''; el.title = ''; }
  });
  const appealRow = $('#rdAppealRow');
  if(appealRow) appealRow.hidden = true;
  if($('#rdAppeal')) $('#rdAppeal').textContent = '—';
  const tag = $('#rdTag'); if(tag){ tag.textContent = '未选择'; tag.className = 'tag'; }
  if($('#rdTitle')) $('#rdTitle').textContent = '点左侧记录查看详情';
  if($('#rdReasons')) $('#rdReasons').innerHTML = '';
  if($('#rdReasonSummary')) $('#rdReasonSummary').textContent = '';
  if($('#rdVerdict')){ $('#rdVerdict').hidden = true; $('#rdVerdict').textContent = ''; }
  if($('#rdCopy')){ $('#rdCopy').disabled = true; delete $('#rdCopy').dataset.rrid; }
  LIVE.lastDetail = null;
  clearRecordMap();
}
function updateTodayNav(){
  const dot = $('#runNavDot'), count = $('#navCount');
  const known = !!LIVE.recordsLoaded;
  const today = beijingTodayKey();
  const n = (LIVE.records || []).filter(r => r.date === today).length;
  if(dot){
    dot.hidden = !known || n > 0;
    dot.title = !known ? '今日记录未加载'
      : (n ? ('今天已跑 ' + n + ' 次') : '今天还没跑');
  }
  if(count){
    count.hidden = !known || n <= 0;
    count.textContent = n > 0 ? String(n) : '';
    count.title = n > 0 ? ('今天已跑 ' + n + ' 次') : '';
  }
}
function renderRecords(){
  updateTodayNav();
  const stateOf = recordState;
  const list = LIVE.records.filter(r => {
    const s = stateOf(r);
    return LIVE.recordFilter === 'all' ? true : s === LIVE.recordFilter;
  });
  const okN = LIVE.records.filter(r => stateOf(r) === 'ok').length;
  const warnN = LIVE.records.filter(r => stateOf(r) === 'warn').length;
  const issueN = LIVE.records.filter(r => stateOf(r) === 'issue').length;
  $('#recCount').textContent = LIVE.records.length + ' 条' + (issueN ? (' · ' + issueN + ' 条有问题') : '');
  $('#recChips').innerHTML = `<span class="chip ok"><i></i>${okN} 条已通过</span>` +
    `<span class="chip warn"><i></i>${warnN} 条待核对</span>` +
    (issueN ? `<span class="chip red"><i></i>${issueN} 条有问题</span>` : '');
  const head = '<div class="thead"><span>日期</span><span>距离</span><span>配速</span><span>状态</span><span>rrid</span></div>';
  $('#recordTable').innerHTML = head + list.map(r => {
    const idx = LIVE.records.indexOf(r);
    const rr = r.rrid ? (String(r.rrid).slice(0, 4) + '…' + String(r.rrid).slice(-4)) : '—';
    return `<div class="trow" data-rec-index="${idx}" role="button" tabindex="0"><span class="mono">${esc((r.date || '').slice(5).replace('-', '/'))}</span><span>${(+r.distance_km || 0).toFixed(2)} km</span><span class="mono">${paceText(r.pace_min_per_km)}</span><span>${recordTag(r)}</span><span class="mono">${esc(rr)}</span></div>`;
  }).join('');
  // FIT export acts on the selected record only; show why when there is none.
  if($('#recFit')){
    // A stale detail from another account/page must not be exportable.
    const sel = LIVE.lastDetail && LIVE.lastDetail.detail;
    const inList = !!(sel && LIVE.records.some(r => r.rrid && r.rrid === sel.rrid));
    $('#recFit').disabled = !inList;
    $('#recFit').title = inList ? '导出选中记录的 FIT' : '先在右侧选一条有轨迹的记录';
  }
  if(!list.length){
    $('#recHint').textContent = LIVE.recordsError
      ? ('记录加载失败：' + LIVE.recordsError)
      : (LIVE.records.length ? '当前筛选没有记录。'
        : (LIVE.recordsLoading ? '正在读取记录…' : '暂无记录。'));
    if(!LIVE.records.length) clearRecordDetail();
  }else{
    $('#recHint').textContent = LIVE.recordsError
      ? ('记录加载失败：' + LIVE.recordsError)
      : '点一行看详情。';
  }
}
const RECEIPT_LABEL = {pending: '待确认', unknown: '结果未知',
                       accepted: '已受理', rejected: '已拒绝'};
async function loadReceipts(){
  const seq = ++_receiptsSeq;
  if(!hasSession()){
    LIVE.receipts = []; LIVE.receiptsLoaded = false;
    LIVE.receiptsError = null;
    renderReceipts(); return;
  }
  const j = await liveApi('real_receipts', {limit: 50});
  if(seq !== _receiptsSeq) return;
  LIVE.receiptsLoaded = liveOk(j);
  LIVE.receiptsError = liveOk(j) ? null : (j.message || '回执加载失败');
  LIVE.receipts = liveOk(j) ? (((j.data && j.data.receipts) || [])) : [];
  renderReceipts();
}
function renderReceipts(){
  const wrap = $('#receiptList');
  if(!wrap) return;
  const rows = LIVE.receipts || [];
  const pending = rows.filter(r => r.state === 'pending' || r.state === 'unknown').length;
  if($('#receiptSummary')){
    $('#receiptSummary').textContent = LIVE.receiptsLoaded
      ? (rows.length ? (pending + ' 条未决 / 共 ' + rows.length + ' 条') : '还没有回执')
      : '回执未加载';
  }
  if(!rows.length){
    const msg = LIVE.receiptsError
      ? ('回执加载失败：' + LIVE.receiptsError)
      : (LIVE.receiptsLoaded ? '还没有提交回执。' : '登录后显示提交回执。');
    wrap.innerHTML = `<div class="receipt-empty">${esc(msg)}</div>`;
    return;
  }
  wrap.innerHTML = rows.slice(0, 8).map(r => {
    const st = RECEIPT_LABEL[r.state] || r.state || '未知';
    const rrid = r.rrid ? (' · rrid ' + String(r.rrid).slice(-8)) : '';
    return `<div class="receipt-row">
      <span class="receipt-state ${esc(r.state || 'unknown')}">${esc(st)}</span>
      <span>${esc(r.start_text || '—')} · ${(+r.distance_km || 0).toFixed(2)} km${esc(rrid)}<br><span class="receipt-meta">${esc(r.uuid || '')}${r.note ? (' · ' + esc(r.note)) : ''}</span></span>
      <span class="receipt-meta">${r.state === 'unknown' || r.state === 'pending' ? '可只读核对' : ''}</span>
    </div>`;
  }).join('');
}
async function reconcileReceipts(){
  if(LIVE.submitting){
    toast('提交进行中，等结果出来后再核对回执');
    return null;
  }
  if(accountBusy){ toast('账号操作进行中，请稍候'); return null; }
  const btn = $('#receiptReconcile');
  if(btn){ btn.disabled = true; btn.textContent = '核对中…'; }
  try{
    const j = await liveApi('real_reconcile_receipts', {limit: 80}, 120000);
    if(!liveOk(j)){
      toast('核对失败：' + (j.message || ''));
      liveLog('回执核对失败', j.message || '', 'warn');
      return null;
    }
    const d = j.data || {};
    const matchedRows = d.matched || [];
    const matched = matchedRows.length;
    const unmatched = (d.unmatched || []).length;
    toast(`核对完成：${matched} 条已匹配${unmatched ? '，' + unmatched + ' 条仍未决' : ''}`);
    liveLog('回执只读核对', `匹配 ${matched} · 未决 ${unmatched} · 查了 ${d.records || 0} 条记录`,
            matched ? 'ok' : 'warn');
    await loadReceipts();
    // A completed read-only reconciliation is what unlocks an "unknown"
    // submit result.  It does not re-submit and does not pretend an
    // unmatched receipt was rejected: the server may still have accepted it.
    if(LIVE.submitResult && LIVE.submitResult.state === 'unknown' &&
       !LIVE.submitResult.checked && LIVE.submitResult.key === submitResultKey()){
      if(matched){
        LIVE.submitResult.state = 'ok';
        LIVE.submitResult.checked = true;
        LIVE.submitResult.warn = false;
        LIVE.submitResult.rrid = matchedRows[0].rrid || '';
        LIVE.submitResult.title = '只读核对完成：已找到服务器记录';
        LIVE.submitResult.detail = '服务器最近记录中有 ' + matched +
          ' 条匹配' + (matchedRows[0].rrid ? ('（rrid ' + matchedRows[0].rrid + '）') : '') +
          '；这次提交已入库，不要重复提交。';
      }else{
        LIVE.submitResult.checked = true;
        LIVE.submitResult.warn = true;
        LIVE.submitResult.title = '只读核对完成：最近记录没有匹配项';
        LIVE.submitResult.detail = d.checked
          ? '没有找到匹配记录。该时段仍可能被服务器受理；后端回执会拦截重复提交。已解除页面锁定，换时段前请先确认。'
          : '没有待核对的回执。已解除页面锁定；如果刚提交过，请先刷新记录确认。';
      }
      renderSubmitResult();
      refreshChecks();
    }
    if(matched){ await loadRecords(); if(typeof loadOverview === 'function') loadOverview(); }
    return d;
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '只读核对未决'; }
  }
}

async function selectRecord(i){
  const r = LIVE.records[i];
  if(!r) return;
  if(!r.rrid){
    toast('这条记录没有 rrid，无法读取详情');
    return;
  }
  const seq = ++_detailSeq;
  // Do not leave the previous record's numbers visible while this one loads
  // (or after it fails); that made a failed detail look like real data.
  clearRecordDetail();
  document.querySelectorAll('[data-rec-index]').forEach(x => x.classList.toggle('active', +x.dataset.recIndex === i));
  $('#rdTitle').textContent = '读取 rrid=' + (r.rrid || '…');
  $('#rdTag').textContent = '查询中'; $('#rdTag').className = 'tag warn';
  const j = await liveApi('real_record_detail', {rrid: r.rrid, uuid: r.uuid || null});
  if(seq !== _detailSeq) return;
  if(!liveOk(j)){
    $('#rdTitle').textContent = '详情查询失败';
    $('#rdTag').textContent = '失败'; $('#rdTag').className = 'tag warn';
    $('#rdReasons').innerHTML = `<li class="miss">${esc(j.message || '')}</li>`;
    liveLog('记录详情失败', j.message || '', 'err');
    recordErrorForExplain(j.message || '记录详情读取失败', 'err');
    return;
  }
  const d = j.data || {}, x = d.detail || {}, s = d.stats || {};
  LIVE.lastDetail = d;
  const km = (+x.distance_m || 0) / 1000;
  const state = x.state || recordState(x);
  const risky = !!(+x.is_cheat || +x.step_freq_status);
  const tagText = risky || state === 'issue' ? '有问题'
    : state === 'ok' ? '已通过' : '待核对';
  $('#rdTag').textContent = tagText;
  $('#rdTag').className = 'tag ' +
    ((risky || state === 'issue') ? 'red' : state === 'ok' ? 'ok' : 'warn');
  $('#rdTitle').textContent = `${shortDate(x.start_ms)} · ${km.toFixed(2)} km`;
  $('#rdPace').textContent = paceText(paceFromSec(x.duration_s, km));
  $('#rdTime').textContent = hms(x.duration_s);
  $('#rdCadence').textContent = x.avg_step_freq ? (x.avg_step_freq + ' spm') : '—';
  $('#rdSteps').textContent = x.total_steps ? String(x.total_steps) : '—';
  $('#rdTrack').textContent = (s.points || 0) + ' 点' +
    (s.has_altitude ? ` · 海拔 ${s.alt_min}–${s.alt_max} m` : '') +
    (s.segments ? ` · ${s.segments} 段` : '');
  const fromObs = (s.json_from_obs != null ? s.json_from_obs : x.json_from_obs);
  const obsErr = x.obs_error || '';
  const pts = s.points || 0;
  const srcKind = s.track_source || (pts ? (fromObs ? 'obs' : 'inline') : 'none');
  let srcText = '', srcCls = '', srcTitle = '';
  if(srcKind === 'obs'){
    srcText = 'OBS' + (pts ? ` · ${pts} 点` : '');
  }else if(srcKind === 'inline'){
    srcText = '记录内联' + (pts ? ` · ${pts} 点` : '');
    if(obsErr) srcTitle = 'OBS 未读取，已使用记录内联轨迹：' + obsErr;
  }else if(obsErr && /404|不存在/.test(obsErr)){
    // Historical rows can point at an OBS object the server no longer keeps.
    // That is not a read bug: only the summary survives.
    srcText = '轨迹未保留（OBS 404）'; srcCls = 'risk-warn';
    srcTitle = '记录标记为 jsonFromObs=1，但服务器上的 OBS 对象不存在（HTTP 404）；只能查看摘要，无法还原轨迹。';
  }else if(obsErr){
    srcText = 'OBS 读取失败'; srcCls = 'risk-bad'; srcTitle = obsErr;
  }else{
    srcText = '无轨迹';
  }
  $('#rdSource').textContent = srcText;
  $('#rdSource').className = srcCls;
  $('#rdSource').title = srcTitle;
  // The server's own anti-cheat fields.  `policy` is the rule-set id echoed
  // back by the detail endpoint (1 = 学分跑), not a risk flag; treating it as
  // one painted every normal 学分跑 record red starting with the 11/05
  // records (issue #14).
  const risk = [];
  if(+x.is_cheat) risk.push('isCheat=1');
  if(+x.step_freq_status) risk.push('步频异常');
  $('#rdCheat').textContent = risk.length ? risk.join(' · ') : '正常';
  $('#rdCheat').className = risk.length ? 'risk-bad' : 'risk-ok';
  $('#rdRulePolicy').textContent = x.policy ? ('policy=' + x.policy) : '—';
  const curPace = paceFromSec(x.duration_s, km);
  const pf = +x.pace_fast, ps = +x.pace_slow;
  $('#rdPaceBand').textContent = (pf > 0 && ps > 0)
    ? (paceText(pf) + '–' + paceText(ps) +
       (curPace ? ' · 本次 ' + paceText(curPace) : ''))
    : (curPace ? paceText(curPace) : '—');
  // Only show the appeal row when the server sent text.  `appealStatus=1`
  // appears on ordinary records with no appeal, so a raw status number would
  // add a meaningless row to every valid run.
  const appeal = x.appeal_info || '';
  const appealRow = $('#rdAppealRow');
  if(appealRow) appealRow.hidden = !appeal;
  if($('#rdAppeal')) $('#rdAppeal').textContent = appeal || '—';
  $('#rdRrid').textContent = x.rrid || r.rrid || '—';
  const reasons = x.reasons || [];
  const badReasons = reasons.filter(y => !y.complete);
  const verdict = $('#rdVerdict');
  if(verdict){
    if(state === 'issue'){
      verdict.hidden = false;
      verdict.textContent = '平台判定：这条记录有问题' +
        (badReasons.length ? (' · ' + badReasons.map(y => y.reason).join('；')) : '') +
        (x.normal_run === false ? ' · normalRun=false' : '');
    }else{
      verdict.hidden = true;
      verdict.textContent = '';
    }
  }
  if($('#rdReasonSummary')){
    const passed = reasons.filter(y => y.complete).length;
    $('#rdReasonSummary').textContent = reasons.length
      ? (passed + '/' + reasons.length + ' 通过') : '';
    $('#rdReasonSummary').className = 'mini ' +
      (badReasons.length ? 'warn' : (reasons.length ? 'ok' : ''));
  }
  $('#rdReasons').innerHTML = reasons.length
    ? reasons.map(y => `<li class="${y.complete ? '' : 'miss'}">${esc(y.reason)}${y.complete ? '' : ' · 未通过'}</li>`).join('')
    : '<li>服务端没有返回 reasonList。</li>';
  $('#rdCopy').disabled = !(x.rrid || r.rrid);
  $('#rdCopy').dataset.rrid = x.rrid || r.rrid || '';
  renderRecordMap(d);
}
function exportRecordsCsv(){
  if(!LIVE.records.length){ toast('没有可导出的记录'); return; }
  const rows = [['date','distance_km','duration_s','pace_min_per_km','state','complete','normal_run','status_code','rrid']];
  LIVE.records.forEach(r => rows.push([r.date || '', (+r.distance_km || 0).toFixed(3), r.duration_s || 0, (+r.pace_min_per_km || 0).toFixed(2), recordState(r), r.complete ? 1 : 0, r.normal_run === null || r.normal_run === undefined ? '' : r.normal_run, r.status_code === null || r.status_code === undefined ? '' : r.status_code, r.rrid || '']));
  const csv = rows.map(x => x.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join(String.fromCharCode(13,10));
  const blob = new Blob([csv], {type: 'text/csv;charset=utf-8'});
  saveBlob(blob, 'haoxiangpao-records.csv');
}

/* ---------------- route library ---------------- */
const ROUTE_KEY = 'jhp_routes_v1';
function routeTypeLabel(type){
  return type === 'stadium' ? '田径场' : type === 'file' ? '导入'
    : type === 'record' ? '记录' : type === 'auto' ? '自动' : '手绘';
}
function validRouteLine(line){
  if(!Array.isArray(line)) return [];
  return line.map(p => (Array.isArray(p) ? [+p[0], +p[1]] : null))
    .filter(p => p && isFinite(p[0]) && isFinite(p[1]));
}
function routeLine(r){
  const line = validRouteLine(r && r.line);
  // A saved closed route is previewed/covered with its final segment too.
  if(r && r.closed && line.length >= 2) return line.concat([line[0]]);
  return line;
}
function normaliseRoutes(parsed){
  return (Array.isArray(parsed) ? parsed : [])
    .filter(r => r && typeof r === 'object')
    .map(r => ({
      name: String(r.name || '未命名路线'),
      type: ['draw', 'auto', 'stadium', 'file', 'record'].includes(r.type) ? r.type : 'draw',
      line: validRouteLine(r.line),
      closed: !!r.closed,
      saved_at: r.saved_at || ''
    }))
    .filter(r => r.line.length >= 2);
}
function routeSaveToast(ok, successText, failText){
  toast(ok ? successText
           : (failText || '路线只写入了浏览器缓存，data 写入失败；请重试'));
}
let _routeWriteChain = Promise.resolve();
async function writeRoutesNow(next){
  try{
    const j = await liveApi('real_save_routes', {routes: next});
    if(!liveOk(j)) throw new Error(j.message || '保存失败');
    try{ localStorage.setItem(ROUTE_KEY, JSON.stringify(next)); }catch(e){}
    return true;
  }catch(e){
    try{ localStorage.setItem(ROUTE_KEY, JSON.stringify(next)); }catch(_e){}
    liveLog('路线写入 data 失败，已退回浏览器本地', String(e.message || e), 'warn');
    return false;
  }
}
function persistRoutes(next){
  LIVE.routes = next;
  // Serialise writes: two quick saves used to race in the bridge and the
  // older snapshot could land last, silently dropping the newer route.
  const job = _routeWriteChain.then(
    () => writeRoutesNow(next),
    () => writeRoutesNow(next));
  _routeWriteChain = job.then(() => undefined, () => undefined);
  return job;
}
async function loadRoutes(){
  let parsed = [];
  try{
    const j = await liveApi('real_get_routes');
    if(liveOk(j) && j.data && Array.isArray(j.data.routes) && j.data.routes.length){
      parsed = j.data.routes;
    }
  }catch(e){}
  if(!parsed.length){
    try{ parsed = JSON.parse(localStorage.getItem(ROUTE_KEY) || '[]'); }
    catch(e){ parsed = []; }
    if(parsed.length) await persistRoutes(normaliseRoutes(parsed));
  }
  LIVE.routes = normaliseRoutes(parsed);
  if(LIVE.selectedRoute >= LIVE.routes.length) LIVE.selectedRoute = -1;
  renderRouteList();
  selectRoute(LIVE.selectedRoute);
}
function routeCoverage(line){
  if(!livePoints.length || !line || line.length < 2) return null;
  const rad = hitRadiusM();
  let passed = 0;
  livePoints.forEach(p => {
    if(line.some(q => distM(q, [p.lat, p.lng]) <= rad)) passed++;
  });
  return {passed: passed, total: livePoints.length, radius: rad};
}
function renderRouteList(){
  const wrap = $('#routeList');
  if(!wrap) return;
  const list = LIVE.routes.map((r, i) => ({r, i})).filter(x =>
    LIVE.routeFilter === 'all' ? true : (x.r.type || 'draw') === LIVE.routeFilter);
  $('#routeCount').textContent = LIVE.routes.length + ' 条';
  $('#routeChipCount').textContent = LIVE.routes.length ? (LIVE.routes.length + ' 条本机路线') : '本机数据';
  if(!list.length){
    wrap.innerHTML = `<div class="empty-state">${LIVE.routes.length ? '当前筛选没有路线。' : '还没有保存的路线。画好一条后点「保存当前路线」。'}</div>`;
    return;
  }
  wrap.innerHTML = list.map(x => {
    const r = x.r;
    const line = routeLine(r);
    const d = pathLength(line) / 1000;
    const cov = routeCoverage(line);
    return `<button class="route-item ${x.i === LIVE.selectedRoute ? 'active' : ''}" data-route-index="${x.i}">
      <div class="route-name">${esc(r.name || '未命名路线')} <span class="route-tag">${esc(routeTypeLabel(r.type))}</span></div>
      <div class="route-meta">${d.toFixed(2)} km · ${cov ? cov.passed + '/' + cov.total + ' 打卡点' : '未加载点位'}</div>
    </button>`;
  }).join('');
  wrap.querySelectorAll('[data-route-index]').forEach(btn => btn.addEventListener('click', () => selectRoute(+btn.dataset.routeIndex)));
}
function selectRoute(i){
  const r = LIVE.routes[i];
  if(!r){
    LIVE.selectedRoute = -1;
    routeHi.setLatLngs([]); routeInk.setLatLngs([]);
    if(routeMap.hasLayer(routeStart)) routeMap.removeLayer(routeStart);
    $('#routeName').textContent = '未选择路线';
    $('#routeCoverage').textContent = '—'; $('#routeCoverage').className = 'tag';
    $('#routeMeta').textContent = '路线保存在本机 data/routes.json。';
    $('#routeUse').disabled = true; $('#routeDelete').disabled = true;
    if($('#routeExportOne')) $('#routeExportOne').disabled = true;
    return;
  }
  LIVE.selectedRoute = i;
  const line = routeLine(r);
  routeHi.setLatLngs(line); routeInk.setLatLngs(line);
  if(line.length){
    routeStart.setLatLng(line[0]);
    if(!routeMap.hasLayer(routeStart)) routeStart.addTo(routeMap);
    routeMap.fitBounds(L.latLngBounds(line).pad(.35));
  }
  $('#routeName').textContent = r.name || '未命名路线';
  const cov = routeCoverage(line);
  $('#routeCoverage').textContent = cov ? (cov.passed + '/' + cov.total) : '—';
  $('#routeCoverage').className = 'tag ' + (cov && cov.passed === cov.total && cov.total ? 'ok' : 'warn');
  const d = pathLength(line) / 1000;
  $('#routeMeta').textContent = `${d.toFixed(2)} km · ${routeTypeLabel(r.type)} · 保存于 ${r.saved_at || '—'}`;
  $('#routeUse').disabled = false; $('#routeDelete').disabled = false;
  if($('#routeExportOne')) $('#routeExportOne').disabled = false;
  renderRouteList();
}
async function saveCurrentRoute(){
  if(!route || route.length < 2){ toast('先在地图上画一条路线'); return; }
  const name = prompt('路线名称', '手绘路线 ' + beijingTodayKey());
  if(!name) return;
  const line = capRoutePoints(validRouteLine(route));
  if(line.length < 2){ toast('路线没有有效坐标'); return; }
  const item = {
    name: name, type: (typeof currentMode !== 'undefined' ? currentMode : 'draw'),
    line: line, closed: !!routeClosed,
    saved_at: beijingTodayKey()
  };
  const next = LIVE.routes.concat([item]);
  const ok = await persistRoutes(next);
  LIVE.selectedRoute = LIVE.routes.length - 1;
  renderRouteList(); selectRoute(LIVE.selectedRoute);
  routeSaveToast(ok, '路线已保存');
}
async function deleteSelectedRoute(){
  const i = LIVE.selectedRoute;
  if(i < 0 || !LIVE.routes[i]) return;
  if(!confirm('删除路线「' + (LIVE.routes[i].name || '') + '」？')) return;
  const next = LIVE.routes.filter((_, j) => j !== i);
  const ok = await persistRoutes(next);
  LIVE.selectedRoute = -1;
  renderRouteList(); selectRoute(-1);
  routeSaveToast(ok, '路线已删除',
    '已从列表移除，但 data 写入失败；重启后这条路线可能恢复');
}

async function saveDetailAsRoute(){
  const d = LIVE.lastDetail;
  const track = (d && d.track) || [];
  if(track.length < 2){ toast('当前记录没有轨迹'); return; }
  const name = prompt('路线名称', '记录路线 ' + shortDate(d.detail && d.detail.start_ms));
  if(!name) return;
  const line = capRoutePoints(validRouteLine(track.map(p => [p.lat, p.lng])));
  if(line.length < 2){ toast('轨迹没有有效坐标'); return; }
  if(line.length < track.length){
    liveLog('记录轨迹已抽稀', `${track.length} → ${line.length} 点`, 'warn');
  }
  const item = {name: name, type: 'record',
                line: line,
                closed: false, saved_at: beijingTodayKey()};
  const ok = await persistRoutes(LIVE.routes.concat([item]));
  LIVE.selectedRoute = LIVE.routes.length - 1;
  renderRouteList(); selectRoute(LIVE.selectedRoute);
  routeSaveToast(ok, '已存为路线');
}

function openRecordDataPreview(){
  const d = LIVE.lastDetail;
  const analysis = d && d.analysis;
  if(!analysis || !analysis.series || !analysis.series.length){
    toast('这条记录没有可用的步频/海拔明细');
    return;
  }
  PENDING_SUBMIT = null;
  renderDataPreview(analysis, 'record', null, '');
}

async function copyDiagnostics(){
  const j = await liveApi('real_diagnostics', {}, 60000);
  if(!liveOk(j)){ toast('诊断包生成失败：' + (j.message || '')); return; }
  const text = (j.data && j.data.text) || '';
  const ok = await copyPlainText(text);
  toast(ok ? '诊断包已复制（已脱敏）' : '复制失败，请手动选择');
  if(ok) liveLog('诊断包已复制', (j.data && j.data.at) || '', 'ok');
}
/* ---------------- rules / model ---------------- */
const RULE_FIELDS = [
  ['rMinKm', 'min_km'], ['rMaxKm', 'max_km'],
  ['rPaceMin', 'pace_min'], ['rPaceMax', 'pace_max'],
  ['rMinMin', 'min_minutes'], ['rMaxMin', 'max_minutes'],
  ['rMaxDay', 'max_per_day'],
  ['rStepMin', 'step_min'], ['rStepMax', 'step_max']
];
function ruleCount(r){
  r = r || {};
  const fields = RULE_FIELDS.filter(([, key]) => {
    const v = r[key];
    return v !== null && v !== undefined && String(v).trim() !== '';
  }).length;
  return fields + readWindowRowsFrom(r).length;
}
function normaliseWindow(raw){
  if(!raw) return null;
  let start = '', end = '';
  if(typeof raw === 'string'){
    const text = raw.replace(/[–—]/g, '-').trim();
    if(text.indexOf('-') < 0) return null;
    const parts = text.split('-', 2);
    start = parts[0].trim(); end = parts[1].trim();
  }else if(Array.isArray(raw) && raw.length >= 2){
    start = String(raw[0] || '').trim(); end = String(raw[1] || '').trim();
  }else if(typeof raw === 'object'){
    start = String(raw.start != null ? raw.start : (raw.startTime || '')).trim();
    end = String(raw.end != null ? raw.end : (raw.endTime || '')).trim();
  }
  const toMin = s => {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(s || '').trim());
    if(!m) return null;
    const h = +m[1], min = +m[2];
    if(min > 59 || h > 24 || (h === 24 && min !== 0)) return null;
    return h * 60 + min;
  };
  const a = toMin(start), b = toMin(end);
  if(a === null || b === null || b <= a) return null;
  return {start: start.padStart(5, '0'), end: end.padStart(5, '0')};
}
function windowRowsInvalid(){
  const wrap = $('#windowRows');
  if(!wrap) return false;
  for(const row of wrap.querySelectorAll('.window-row')){
    const start = (row.querySelector('.window-start') || {}).value || '';
    const end = (row.querySelector('.window-end') || {}).value || '';
    if(!start.trim() && !end.trim()) continue;   // an unused blank row
    if(!normaliseWindow({start: start, end: end})) return true;
  }
  return false;
}
function rawWindowRowsFrom(r){
  r = r || {};
  let list = Array.isArray(r.windows) ? r.windows : [];
  if(!list.length && (r.window_start || r.window_end)){
    list = [{start: r.window_start, end: r.window_end}];
  }
  return list.map(raw => {
    let start = '', end = '';
    if(typeof raw === 'string'){
      const text = raw.replace(/[–—－]/g, '-').replace(/：/g, ':').trim();
      if(text.indexOf('-') >= 0){
        const parts = text.split('-', 2);
        start = parts[0].trim(); end = parts[1].trim();
      }else{
        start = text;
      }
    }else if(Array.isArray(raw) && raw.length >= 2){
      start = String(raw[0] || '').trim();
      end = String(raw[1] || '').trim();
    }else if(raw && typeof raw === 'object'){
      start = String(raw.start != null ? raw.start : (raw.startTime || '')).trim();
      end = String(raw.end != null ? raw.end : (raw.endTime || '')).trim();
    }
    return {start, end};
  }).filter(row => row.start || row.end);
}
function readWindowRowsFrom(r){
  return rawWindowRowsFrom(r).map(normaliseWindow).filter(Boolean);
}
function readWindowRows(){
  const wrap = $('#windowRows');
  if(!wrap) return [];
  return [...wrap.querySelectorAll('.window-row')].map(row => ({
    start: (row.querySelector('.window-start') || {}).value || '',
    end: (row.querySelector('.window-end') || {}).value || ''
  })).map(normaliseWindow).filter(Boolean);
}
function addWindowRow(start, end){
  const wrap = $('#windowRows');
  if(!wrap) return;
  const row = document.createElement('div');
  row.className = 'window-row';
  row.innerHTML = '<input class="window-start" inputmode="numeric" maxlength="5" ' +
    'placeholder="06:00" aria-label="时段开始"><span class="window-sep">–</span>' +
    '<input class="window-end" inputmode="numeric" maxlength="5" placeholder="07:59" ' +
    'aria-label="时段结束"><button type="button" class="window-del" aria-label="删除这个时段">×</button>';
  row.querySelector('.window-start').value = start || '';
  row.querySelector('.window-end').value = end || '';
  row.querySelector('.window-del').addEventListener('click', () => {
    row.remove();
    if(!wrap.querySelector('.window-row')) renderWindowRows([]);
  });
  wrap.appendChild(row);
}
function renderWindowRows(list, opts){
  const wrap = $('#windowRows');
  if(!wrap) return;
  const rows = (list || []).map(w => {
    if(opts && opts.raw) return {start: String(w.start || ''), end: String(w.end || '')};
    return normaliseWindow(w);
  }).filter(w => w && (w.start || w.end));
  wrap.innerHTML = '';
  if(!rows.length){
    wrap.innerHTML = '<div class="window-empty">未填写 = 学校没有时间窗要求；也可以点下面添加。</div>';
    return;
  }
  rows.forEach(w => addWindowRow(w.start, w.end));
}
function windowsText(list){
  const rows = (list || []).map(normaliseWindow).filter(Boolean);
  return rows.map(w => w.start + '–' + w.end).join('、');
}
async function loadNetworkConfig(){
  if(!$('#netStatus')) return;
  const j = await liveApi('get_network_config');
  if(!liveOk(j)){
    $('#netStatus').textContent = '读取失败';
    $('#netStatus').className = 'mini warn';
    $('#netHint').textContent = j.message || '网络配置读取失败';
    return;
  }
  const d = j.data || {};
  if($('#netProxy')) $('#netProxy').value = d.proxy || '';
  if($('#netAmap')) $('#netAmap').value = '';
  const bits = [];
  if(d.proxy_present) bits.push('代理已设置');
  if(d.amap_key_present) bits.push('高德 Key 已设置');
  $('#netStatus').textContent = bits.length ? bits.join(' · ') : '未配置';
  $('#netStatus').className = 'mini ' + (bits.length ? 'ok' : '');
  $('#netAmap').placeholder = d.amap_key_present
    ? ('已配置 ' + (d.amap_key_masked || '') + '；留空=不修改')
    : '粘贴 Key；留空=不修改';
  $('#netHint').textContent = d.amap_key_present
    ? '当前 Key：' + (d.amap_key_masked || '') + '。'
    : '';
}
async function saveNetworkConfig(){
  if(accountBusy){ toast('正在登录，请稍候'); return; }
  if(LIVE.submitting){
    toast('提交进行中，完成后再修改网络配置');
    return;
  }
  const proxy = ($('#netProxy') ? $('#netProxy').value : '').trim();
  const key = ($('#netAmap') ? $('#netAmap').value : '').trim();
  const args = {proxy};
  if(key) args.amap_key = key;
  const j = await liveApi('set_network_config', args);
  if(!liveOk(j)){ toast('应用失败：' + (j.message || '')); return; }
  if($('#netAmap')) $('#netAmap').value = '';
  await loadNetworkConfig();
  toast(key ? '代理/高德 Key 已应用到本次运行' : '代理已应用到本次运行');
  liveLog('网络配置已应用', key ? '高德 Key 仅保存在内存' : '未修改高德 Key', 'ok');
}
async function clearNetworkConfig(){
  if(accountBusy){ toast('正在登录，请稍候'); return; }
  if(LIVE.submitting){
    toast('提交进行中，完成后再修改网络配置');
    return;
  }
  if(!confirm('清除本次运行中设置的代理和高德 Key？')) return;
  const j = await liveApi('set_network_config', {clear_proxy: true, clear_amap_key: true});
  if(!liveOk(j)){ toast('清除失败：' + (j.message || '')); return; }
  await loadNetworkConfig();
  toast('已清除本次运行中的网络配置');
}
const RULE_LABELS = {
  min_km: '最低距离', max_km: '最高距离',
  pace_min: '配速下限', pace_max: '配速上限',
  min_minutes: '最短时长', max_minutes: '最长时长',
  max_per_day: '每日次数',
  step_min: '步频下限', step_max: '步频上限',
  window_start: '时间窗开始', window_end: '时间窗结束',
  windows: '有效时段'
};
function applyRulesToForm(r){
  r = r || {};
  RULE_FIELDS.forEach(([id, key]) => {
    const el = $('#' + id);
    if(el) el.value = (r[key] === null || r[key] === undefined) ? '' : r[key];
  });
  // Keep malformed old rows visible instead of silently deleting them; the
  // save button will then refuse with a concrete message.
  renderWindowRows(rawWindowRowsFrom(r), {raw: true});
}
function collectRulesForm(){
  const rules = {};
  RULE_FIELDS.forEach(([id, key]) => {
    const el = $('#' + id);
    if(el) rules[key] = el.value.trim();
  });
  rules.windows = readWindowRows();
  return rules;
}
function rulesFormHasValues(){
  if(readWindowRows().length) return true;
  return RULE_FIELDS.some(([id]) => {
    const el = $('#' + id);
    return !!(el && el.value.trim());
  });
}
function renderRulesStatus(d, rules, hint){
  d = d || {};
  const configured = !!d.configured;
  const n = ruleCount(d.rules || rules);
  LIVE.rules = {rules: d.rules || rules, configured};
  modelConfigured = configured;
  $('#rulesStatus').textContent = configured ? ('已保存 · ' + n + ' 项') : '未配置';
  $('#rulesStatus').className = 'mini ' + (configured ? 'ok' : '');
  const autoOff = !!(d.rules && d.rules.auto_fetch === false);
  $('#rulesHint').textContent = hint || (configured
    ? '已保存；只检查填写过的项目，留空项显示“未要求”。'
    : (autoOff
      ? '模型已关闭；自动读取已停止，点「从服务器读取」可恢复。'
      : '任意填写一项即可启用模型；留空项不检查。'));
  refreshParams();
  refreshModelPreview();
  refreshChecks();
  if(typeof updateRunDateLabel === 'function') updateRunDateLabel();
  if($('#datePop') && !$('#datePop').hidden) renderDatePop();
}
async function persistRules(rules, opts){
  opts = opts || {};
  const j = await liveApi('save_school_rules', {rules: rules});
  if(!liveOk(j)){
    if(!opts.silent) toast('保存失败：' + (j.message || ''));
    liveLog('学校要求保存失败', j.message || '', 'err');
    return null;
  }
  const d = j.data || {};
  renderRulesStatus(d, rules, opts.hint);
  if(!opts.silent){
    toast(opts.toast || '学校要求已保存');
    liveLog('学校要求已保存', d.configured
      ? ('模型已启用 · ' + ruleCount(d.rules || rules) + ' 项')
      : '模型关闭', 'ok');
  }
  return d;
}
async function loadSemesterGoal(){
  const input = $('#rSemesterKm'), status = $('#semesterGoalStatus');
  try{
    const sid = LIVE.semester && LIVE.semester.view ? LIVE.semester.view.sid : null;
    const j = await liveApi('get_semester_goal', sid ? {sid: sid} : {});
    if(!liveOk(j) || !input) return;
    const t = (j.data || {}).target_km;
    input.value = t == null ? '' : String(t);
    if(status){
      status.textContent = t == null ? '未设置' : ('目标 ' + t + ' km');
      status.className = 'mini' + (t == null ? '' : ' ok');
    }
  }catch(e){ /* the target is optional */ }
}
async function saveSemesterGoal(){
  const input = $('#rSemesterKm'), btn = $('#saveSemesterGoal');
  const hint = $('#semesterGoalHint'), status = $('#semesterGoalStatus');
  const raw = input ? String(input.value || '').trim() : '';
  const sid = LIVE.semester && LIVE.semester.view ? LIVE.semester.view.sid : null;
  if(btn){ btn.disabled = true; btn.textContent = '保存中…'; }
  try{
    const j = await liveApi('save_semester_goal',
      sid ? {target_km: raw, sid: sid} : {target_km: raw});
    if(!liveOk(j)){
      toast('保存失败：' + (j.message || ''));
      if(hint) hint.textContent = j.message || '保存失败';
      return;
    }
    const t = (j.data || {}).target_km;
    if(status){
      status.textContent = t == null ? '未设置' : ('目标 ' + t + ' km');
      status.className = 'mini' + (t == null ? '' : ' ok');
    }
    if(hint){
      hint.textContent = t == null
        ? '已清除；学期卡会显示“户外未设目标”。'
        : ('已保存：按 ' + t + ' km 计算学期还差。');
    }
    toast(t == null ? '已清除户外目标' : '户外目标已保存');
    liveLog('本学期目标已保存', t == null ? '已清除' : (t + ' km'), 'ok');
    // Both cards are built from real_semester, so refresh them right away.
    if(hasSession()){
      await Promise.allSettled([loadOverview(), loadProfile({silent: true})]);
    }
  }catch(e){
    toast('保存失败：' + (e && e.message ? e.message : e));
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '保存目标'; }
  }
}
async function loadRules(){
  await loadSemesterGoal();
  const j = await liveApi('get_school_rules');
  if(!liveOk(j)){
    const hasSaved = !!(LIVE.rules && LIVE.rules.configured);
    $('#rulesStatus').textContent = '读取失败';
    $('#rulesStatus').className = 'mini warn';
    $('#rulesHint').textContent = hasSaved
      ? '本次读取失败，仍沿用当前已保存的规则；可点「保存要求」重试。'
      : (j.message || '本地学校要求读取失败；可手动填写后保存。');
    liveLog('学校要求读取失败', j.message || '', 'err');
    return;
  }
  const d = j.data || {}, r = d.rules || {};
  applyRulesToForm(r);
  renderRulesStatus(d, r);
}
async function saveRules(){
  if(accountBusy){ toast('正在登录，请稍候'); return; }
  if(LIVE.submitting){ toast('提交进行中，完成后再修改学校要求'); return; }
  const rowCount = $('#windowRows')
    ? $('#windowRows').querySelectorAll('.window-row').length : 0;
  if(rowCount > 12){
    toast('最多支持 12 个有效时段');
    return;
  }
  if(windowRowsInvalid()){
    toast('有效时段格式不对：要写成 HH:MM，结束要晚于开始');
    return;
  }
  const rules = collectRulesForm();
  const filled = RULE_FIELDS.filter(([id]) => $('#' + id) && $('#' + id).value.trim()).length +
    (rules.windows || []).length;
  if(!filled && !confirm('所有项都留空，保存后会关闭模型并停止自动读取。继续？')) return;
  // A manual save is the operator's source of truth.  Stop the background
  // server refresh so it cannot silently overwrite hand-edited bands; the
  // explicit「从服务器读取」button turns auto_fetch back on.
  rules.auto_fetch = false;
  const btn = $('#saveRules');
  if(btn){ btn.disabled = true; btn.textContent = '保存中…'; }
  try{
    await persistRules(rules, {
      hint: filled
        ? '已保存；不再自动覆盖你填的规则。点「从服务器读取」可恢复自动刷新。'
        : '模型已关闭；自动读取已停止。'
    });
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '保存要求'; }
  }
}
async function fetchSchoolRules(opts){
  opts = opts || {};
  if(LIVE.submitting){
    if(!opts.silent) toast('提交进行中，完成后再读取学校要求');
    return false;
  }
  if(!hasSession()){ if(!opts.silent) toast('先登录'); return false; }
  if(!opts.force && LIVE.rules && LIVE.rules.configured) return false;
  if(!opts.force && LIVE.rules && LIVE.rules.rules &&
     LIVE.rules.rules.auto_fetch === false) return false;   // explicit model off
  // Do not overwrite values the operator has typed but not saved yet.
  if(!opts.force && rulesFormHasValues()) return false;
  if(opts.force && ((LIVE.rules && LIVE.rules.configured) || rulesFormHasValues()) &&
     !confirm('用服务器返回的规则覆盖当前填写？服务器没返回的项会保留。')) return false;
  const btn = $('#rulesFetch');
  if(btn){ btn.disabled = true; btn.textContent = '读取中…'; }
  try{
    const j = await liveApi('real_fetch_school_rules', {run_mode: 1}, 60000);
    if(!liveOk(j)){
      liveLog('服务器学校规则读取失败', j.message || '', 'warn');
      if(!opts.silent) toast('读取失败：' + (j.message || ''));
      return false;
    }
    const d = j.data || {};
    const mapped = d.rules || {};
    const keys = Object.keys(mapped);
    if(!keys.length){
      liveLog('服务器学校规则为空', 'runModePolicy 没有可映射字段', 'warn');
      if(!opts.silent) toast('服务器没有返回可填写的规则');
      return false;
    }
    // Preserve fields the server does not provide (for example a manually
    // entered daily cap); only overwrite the fields we actually mapped.
    const merged = collectRulesForm();
    keys.forEach(k => { merged[k] = mapped[k]; });
    merged.auto_fetch = true;
    applyRulesToForm(merged);
    const meta = d.meta || {};
    const notes = [];
    const mappedWindows = readWindowRowsFrom(mapped);
    if(mappedWindows.length > 1){
      notes.push('服务器返回 ' + mappedWindows.length + ' 个时段，已全部填入');
    }
    if(meta.day_goal_m) notes.push('服务器每日目标 ' + (meta.day_goal_m / 1000).toFixed(1) + ' km（未映射）');
    const labels = keys.map(k => RULE_LABELS[k] || k);
    const hint = '已从服务器自动填入并保存 ' + labels.join('、') +
      '；可修改后再次保存。' +
      (notes.length ? ' ' + notes.join('；') + '。' : '');
    const saved = await persistRules(merged, {silent: true, hint});
    if(!saved){
      $('#rulesHint').textContent =
        '服务器规则已填入，但保存失败；请点「保存要求」重试。';
      if(!opts.silent) toast('规则已填入但保存失败');
      return false;
    }
    LIVE.rulesSyncedAt = Date.now();
    liveLog('学校规则已从服务器填入',
      'runMode ' + d.run_mode + ' · ' + labels.join('、') +
      (notes.length ? ' · ' + notes.join('；') : ''), 'ok');
    if(!opts.silent) toast('已填入服务器规则并保存');
    return true;
  } finally {
    if(btn){ btn.disabled = false; btn.textContent = '从服务器读取'; }
  }
}

/* Refresh the cached rules without the settings form's "already configured"
   early return.  This is what makes an upgraded v0.1.x profile (single old
   window) pick up the server's real multi-band validTime before the run page
   decides whether 18:00 is allowed. */
async function syncSchoolRulesQuiet(){
  if(!hasSession() || LIVE.submitting) return false;
  const current = LIVE.rules && LIVE.rules.rules;
  if(current && current.auto_fetch === false) return false;
  if(document.querySelector('#page-settings.active') && rulesFormHasValues())
    return false;      // never overwrite an unsaved edit
  if(!(LIVE.rules && LIVE.rules.configured)){
    LIVE.rulesSyncedAt = Date.now();
    return fetchSchoolRules({silent: true});
  }
  LIVE.rulesSyncedAt = Date.now();
  const j = await liveApi('real_fetch_school_rules', {run_mode: 1}, 60000);
  if(!liveOk(j)) return false;
  const mapped = (j.data && j.data.rules) || {};
  if(!Object.keys(mapped).length) return false;
  const merged = Object.assign({}, current || {}, mapped);
  merged.auto_fetch = true;
  const saved = await liveApi('save_school_rules', {rules: merged});
  if(!liveOk(saved)) return false;
  LIVE.rules = {
    rules: (saved.data && saved.data.rules) || merged,
    configured: !!(saved.data && saved.data.configured)
  };
  LIVE.rulesSyncedAt = Date.now();
  try{ await loadRules(); }catch(e){ /* keep the merged copy */ }
  return true;
}

/* ---------------- backfill date / time ---------------- */
function beijingNowDate(){
  return new Date(Date.now() + (new Date().getTimezoneOffset() + 480) * 60000);
}
const pad2 = n => String(n).padStart(2, '0');
const DATE_WD = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

function fmtDateKey(d){
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}
function beijingTodayKey(){ return fmtDateKey(beijingNowDate()); }
function parseDateKey(key){
  const m = String(key || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if(!m) return null;
  const d = new Date(+m[1], +m[2] - 1, +m[3]);
  return (d.getFullYear() === +m[1] && d.getMonth() === +m[2] - 1 &&
          d.getDate() === +m[3]) ? d : null;
}
function dateToDaysAgo(key){
  const d = parseDateKey(key);
  const t = parseDateKey(beijingTodayKey());
  if(!d || !t) return null;
  return Math.round((Date.UTC(t.getFullYear(), t.getMonth(), t.getDate())
                   - Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
}
function dateDisplay(key){
  const d = parseDateKey(key);
  return d ? (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日 · ' + DATE_WD[d.getDay()]
           : '日期未选';
}
function dateRange(){
  const t = parseDateKey(beijingTodayKey());
  return {today: t,
          min: new Date(t.getFullYear(), t.getMonth(), t.getDate() - 3),
          max: t};
}
function defaultRunDateKey(){
  const now = beijingNowDate();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const dur = (+$('#inKm').value || 0) * (+$('#inPace').value || 6);
  if(Math.min(22 * 60, nowMin) - dur < 9 * 60){
    return fmtDateKey(new Date(now.getFullYear(), now.getMonth(),
                               now.getDate() - 1));
  }
  return fmtDateKey(now);
}
function syncDateButton(){
  const key = LIVE.runDate || beijingTodayKey();
  const d = parseDateKey(key);
  if($('#dateMain')){
    $('#dateMain').textContent = d ? (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日'
                                   : '选择日期';
  }
  if($('#dateSub')) $('#dateSub').textContent = d ? DATE_WD[d.getDay()] : '';
  if($('#dateBtn')){
    $('#dateBtn').setAttribute('aria-expanded',
      ($('#datePop') && !$('#datePop').hidden) ? 'true' : 'false');
    $('#dateBtn').title = d ? dateDisplay(key) : '';
  }
}
function initRunDate(){
  if(LIVE.runDate){
    const n = dateToDaysAgo(LIVE.runDate);
    if(n !== null && n >= 0 && n <= 3) return;
  }
  LIVE.runDate = defaultRunDateKey();
  syncDateButton();
}
function schoolWindows(){
  const rules = (LIVE.rules && LIVE.rules.configured) ? LIVE.rules.rules : null;
  return readWindowRowsFrom(rules);
}
function manualSplitMinutes(){
  if(!splitsManual || !Array.isArray(splitRows) || !splitRows.length) return null;
  const total = splitRows.reduce((sum, row) =>
    sum + Math.max(0, +row.seconds || 0), 0);
  return total > 0 ? total / 60 : null;
}
function selectedTimeInfo(){
  const key = LIVE.runDate || beijingTodayKey();
  const daysAgo = dateToDaysAgo(key);
  const raw = ($('#inStart') ? $('#inStart').value : '').trim();
  // Match the backend defaults in submitRun(), not 0: an empty input must not
  // make the window check believe the run lasts zero minutes.
  const km = +$('#inKm').value || 0;
  const pace = +$('#inPace').value || 6;
  // Manual per-km splits are authoritative for the generated record; the
  // average pace box must not make the slot check shorter than the submit.
  const manualMin = manualSplitMinutes();
  const durMin = manualMin != null ? manualMin : km * pace;
  // Match the submit path: auto pause reserves up to 3 minutes (or 1.5 min for
  // runs under 10 minutes).  The slot check must include that reserve.
  let pauseReserveMin = 0;
  try{
    if(realismNow().pause_profile === 'auto'){
      pauseReserveMin = (durMin * 60 >= 600) ? 3 : 1.5;
    }
  }catch(e){}
  const slotMin = durMin + pauseReserveMin;
  const configured = schoolWindows();
  const usingSchool = configured.length > 0;
  const windows = usingSchool ? configured
    : [{start: '09:00', end: '22:00'}];
  const label = windowsText(windows) || '09:00–22:00';
  const out = {
    date: key, daysAgo, raw, time: null, durMin, slotMin, windows,
    dateOk: true, dateNote: dateDisplay(key),
    timeOk: true, timeNote: '', ok: true, note: ''
  };
  if(daysAgo === null || daysAgo < 0 || daysAgo > 3){
    out.dateOk = false;
    out.dateNote = '只能选最近 3 天（含今天）';
    out.timeOk = false;
    out.timeNote = '先选有效日期';
    out.ok = false;
    out.note = out.dateNote;
    return out;
  }
  const clock = m => {
    const t = Math.max(0, Math.round(m));
    return pad2(Math.floor(t / 60)) + ':' + pad2(t % 60);
  };
  const toMin = t => {
    const p = String(t || '').split(':');
    return (+p[0] || 0) * 60 + (+p[1] || 0);
  };
  const now = beijingNowDate();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const fail = note => {
    out.timeOk = false; out.ok = false; out.timeNote = note; out.note = note;
    return out;
  };
  // A slot is usable only when the whole run fits and (for today) ends by now.
  const slots = [];
  windows.forEach(w => {
    const a = toMin(w.start), b = toMin(w.end);
    if(b <= a) return;
    const latestEnd = daysAgo === 0 ? Math.min(b, nowMin) : b;
    const latestStart = latestEnd - slotMin;
    if(latestStart >= a) slots.push({start: a, end: b, latestStart});
  });
  if(!slots.length){
    if(daysAgo === 0){
      return fail('今天剩余时间放不下这次跑步（有效时段：' + label +
                  '），换一天或缩短距离');
    }
    return fail('这次跑步太长，放不进有效时段：' + label);
  }
  if(!raw){
    out.timeNote = '留空 · 在 ' + label + ' 内随机' +
      (daysAgo === 0 ? '（今天不能晚于现在）' : '');
    out.note = out.timeNote;
    return out;
  }
  const m = raw.match(/^(\d{1,2}):(\d{2})$/);
  if(!m) return fail('要写成 HH:MM');
  const hh = +m[1], mm = +m[2];
  if(hh > 23 || mm > 59) return fail('时间不存在');
  const mins = hh * 60 + mm;
  if(usingSchool && !windows.some(w => mins >= toMin(w.start) && mins <= toMin(w.end))){
    return fail('不在学校有效时段内；可用：' + label);
  }
  if(!usingSchool){
    if(mins < 9 * 60) return fail('不能早于 09:00');
    if(mins > 22 * 60) return fail('不能晚于 22:00');
  }
  const slot = slots.find(s => mins >= s.start && mins <= s.latestStart);
  if(!slot){
    if(daysAgo === 0 && mins > nowMin){
      return fail('今天这个时间还没到（现在 ' + clock(nowMin) + '）');
    }
    if(daysAgo === 0){
      return fail('这个时间结束时已经超过现在（现在 ' + clock(nowMin) + '）');
    }
    if(usingSchool){
      return fail('按当前距离/配速，结束会超过有效时段（' + label + '），请提前开始');
    }
    return fail('按当前距离/配速，结束会超过 22:00，请提前开始');
  }
  out.time = pad2(hh) + ':' + pad2(mm);
  out.timeNote = dateDisplay(key) + ' ' + out.time + ' 开始 · 约 ' +
    Math.round(durMin) + ' 分钟' +
    (pauseReserveMin ? (' · 预留暂停 ' + pauseReserveMin + ' 分钟') : '');
  out.note = out.timeNote;
  return out;
}
function dailyCountInfo(){
  const key = LIVE.runDate || beijingTodayKey();
  const rules = (LIVE.rules && LIVE.rules.configured) ? LIVE.rules.rules : null;
  const cap = rules ? (+rules.max_per_day || 0) : 0;
  let count = null, source = null;
  const dailyForDate = !!(LIVE.daily && LIVE.daily.key === key);
  if(dailyForDate && LIVE.daily.runs !== null && LIVE.daily.runs !== undefined){
    count = +LIVE.daily.runs;
    source = LIVE.daily.source || 'server';
  }else if(!dailyForDate && LIVE.recordsLoaded){
    // The recent list is a fallback only when we have not asked for this
    // date.  If the dedicated query failed, do not relabel a 20-row tail as
    // an authoritative count.
    count = (LIVE.records || []).filter(r => (r.date || '') === key).length;
    source = 'server';
  }
  // A local receipt count is only a lower bound; it cannot prove the server
  // has fewer runs.  Treat it (and a failed query) as unknown for the gate.
  const loaded = count !== null && source !== 'local' && source !== 'unknown';
  return {key, cap, count, source, loaded, daysAgo: dateToDaysAgo(key),
          ok: loaded ? (cap <= 0 || count < cap) : false};
}
async function refreshDailyCount(){
  if(!hasSession()){ LIVE.daily = null; refreshChecks(); return; }
  const key = LIVE.runDate || beijingTodayKey();
  const uid = LIVE.session && (LIVE.session.uid || LIVE.session.unid);
  const j = await liveApi('real_daily_count', {date: key});
  const current = LIVE.runDate || beijingTodayKey();
  const currentUid = LIVE.session && (LIVE.session.uid || LIVE.session.unid);
  if(key !== current || !hasSession() || (uid && currentUid !== uid)) return;
  if(!liveOk(j)){
    LIVE.daily = {key, runs: null, source: 'unknown'};
    refreshChecks();
    return;
  }
  const d = j.data || {};
  LIVE.daily = {key: d.date || key, runs: d.runs, source: d.source || 'unknown'};
  refreshChecks();
  if(typeof scheduleModelPreview === 'function') scheduleModelPreview();
}
function updateRunDateLabel(){
  syncDateButton();
  const key = LIVE.runDate || beijingTodayKey();
  const d = parseDateKey(key);
  if($('#runDate')){
    $('#runDate').textContent = d ? dateDisplay(key) : '选择日期';
  }
  if($('#runCrumbDay')){
    $('#runCrumbDay').textContent = d
      ? (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日' : '日期';
  }
  if($('#runTitle')){
    const n = dateToDaysAgo(key);
    $('#runTitle').textContent = !d ? '跑步记录'
      : (n === 0 ? '今日跑步'
                 : '补跑 · ' + (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日');
  }
  const ti = selectedTimeInfo();
  if($('#timeHint')){
    if(ti.ok){
      const r = dateRange();
      const range = (r.min.getMonth() + 1) + ' 月 ' + r.min.getDate() + ' 日 – ' +
                    (r.max.getMonth() + 1) + ' 月 ' + r.max.getDate() + ' 日';
      const windowNote = ti.time && ti.windows && ti.windows.length
        ? ('；有效时段：' + windowsText(ti.windows)) : '';
      $('#timeHint').textContent = '可选 ' + range + '（北京时间）；' +
        (ti.time ? ti.time + ' 开始，约 ' + Math.round(ti.durMin) + ' 分钟'
                 : ti.timeNote) + windowNote + '。';
    }else{
      $('#timeHint').textContent = '⚠ ' + (ti.dateOk ? '开始时间：' : '') + ti.note;
    }
    $('#timeHint').style.color = ti.ok ? '' : '#b3261e';
  }
}

/* ---------------- date popover ---------------- */
const dateView = {y: 0, m: 0};
function monthIntersectsAllowed(y, m){
  const r = dateRange();
  return new Date(y, m + 1, 0) >= r.min && new Date(y, m, 1) <= r.max;
}
function positionDatePop(){
  const pop = $('#datePop'), btn = $('#dateBtn');
  if(!pop || !btn || pop.hidden) return;
  const r = btn.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  const left = Math.max(10, Math.min(r.left, window.innerWidth - w - 10));
  let top = r.bottom + 8;
  if(top + h > window.innerHeight - 10) top = Math.max(10, r.top - h - 8);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}
async function loadDateCounts(force){
  if(!hasSession()) return;
  const uid = LIVE.session && (LIVE.session.uid || LIVE.session.unid);
  if(!force && LIVE.dailyCountsUid === uid &&
     Date.now() - LIVE.dailyCountsAt < 60000 &&
     Object.keys(LIVE.dailyCounts || {}).length) return;
  const seq = ++_dateCountsSeq;
  const t = parseDateKey(beijingTodayKey());
  const dates = [0, 1, 2, 3].map(i =>
    fmtDateKey(new Date(t.getFullYear(), t.getMonth(), t.getDate() - i)));
  const j = await liveApi('real_daily_counts', {dates: dates}, 30000);
  const currentUid = LIVE.session && (LIVE.session.uid || LIVE.session.unid);
  if(seq !== _dateCountsSeq || !hasSession() || (uid && currentUid !== uid)) return;
  if(!liveOk(j)){
    LIVE.dailyCounts = {};
    LIVE.dailyCountsAt = 0;
    renderDatePop();
    return;
  }
  const map = {};
  ((j.data && j.data.dates) || []).forEach(d => { if(d && d.date) map[d.date] = d; });
  LIVE.dailyCounts = map;
  LIVE.dailyCountsUid = uid;
  LIVE.dailyCountsAt = Date.now();
  renderDatePop();
  positionDatePop();
}
function renderDatePop(){
  const pop = $('#datePop');
  if(!pop) return;
  const y = dateView.y, m = dateView.m;
  if($('#dateMonth')) $('#dateMonth').textContent = y + ' 年 ' + (m + 1) + ' 月';
  if($('#datePrev')) $('#datePrev').disabled = !monthIntersectsAllowed(y, m - 1);
  if($('#dateNext')) $('#dateNext').disabled = !monthIntersectsAllowed(y, m + 1);
  const grid = $('#dateGrid');
  if(!grid) return;
  const firstDow = (new Date(y, m, 1).getDay() + 6) % 7;   // Monday = 0
  const days = new Date(y, m + 1, 0).getDate();
  const todayKey = beijingTodayKey();
  const r = dateRange();
  let html = '';
  for(let i = 0; i < firstDow; i++){
    html += '<span class="date-day out" aria-hidden="true"></span>';
  }
  for(let day = 1; day <= days; day++){
    const dt = new Date(y, m, day);
    const key = fmtDateKey(dt);
    const allowed = dt >= r.min && dt <= r.max;
    const cls = ['date-day'];
    if(key === todayKey) cls.push('today');
    if(key === LIVE.runDate) cls.push('selected');
    const c = LIVE.dailyCounts && LIVE.dailyCounts[key];
    const cap = LIVE.rules && LIVE.rules.configured && LIVE.rules.rules
      ? +LIVE.rules.rules.max_per_day || 0 : 0;
    let badge = '';
    if(c){
      const runs = c.runs;
      const over = runs != null && cap > 0 && runs >= cap;
      const txt = runs == null ? '?' : (cap > 0 ? (runs + '/' + cap) : (runs + '次'));
      const bcls = runs == null ? 'unknown' : (over ? 'over' : '');
      badge = '<span class="date-badge ' + bcls + '">' + txt + '</span>';
    }
    html += '<button type="button" class="' + cls.join(' ') + '" data-date="' + key + '"' +
      (allowed ? '' : ' disabled') +
      ' aria-pressed="' + (key === LIVE.runDate) + '"' +
      ' aria-label="' + key + ' ' + DATE_WD[dt.getDay()] + (c && c.runs != null ? (' 已跑 ' + c.runs + ' 次') : '') + '">' +
      '<span>' + day + '</span>' + badge + '</button>';
  }
  grid.innerHTML = html;
  if($('#dateFoot')){
    $('#dateFoot').textContent = '可选 ' +
      (r.min.getMonth() + 1) + '.' + r.min.getDate() + ' – ' +
      (r.max.getMonth() + 1) + '.' + r.max.getDate() + ' · 北京时间';
  }
}
function positionAndRenderDatePop(){
  renderDatePop();
  positionDatePop();
}
function openDatePop(){
  const pop = $('#datePop'), btn = $('#dateBtn');
  if(!pop || !btn) return;
  const box = btn.getBoundingClientRect();
  if(!box.width || !box.height) return;   // params tab is hidden
  const d = parseDateKey(LIVE.runDate) || dateRange().today;
  dateView.y = d.getFullYear();
  dateView.m = d.getMonth();
  pop.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  // The hint sits directly under the field; leaving it visible on the right of
  // the popover reads as a collision.  It comes back as soon as the picker
  // closes, and the popover footer repeats the range.
  if($('#timeHint')) $('#timeHint').style.visibility = 'hidden';
  positionAndRenderDatePop();
  loadDateCounts(false);
  const sel = pop.querySelector('.date-day.selected') ||
              pop.querySelector('.date-day:not(:disabled)');
  if(sel) sel.focus({preventScroll: true});
}
function closeDatePop(){
  const pop = $('#datePop');
  if(!pop || pop.hidden) return;
  const hadFocus = pop.contains(document.activeElement);
  pop.hidden = true;
  if($('#dateBtn')) $('#dateBtn').setAttribute('aria-expanded', 'false');
  if($('#timeHint')) $('#timeHint').style.visibility = '';
  if(hadFocus && $('#dateBtn')) $('#dateBtn').focus({preventScroll: true});
}
function shiftDateMonth(delta){
  const d = new Date(dateView.y, dateView.m + delta, 1);
  if(!monthIntersectsAllowed(d.getFullYear(), d.getMonth())) return;
  dateView.y = d.getFullYear();
  dateView.m = d.getMonth();
  positionAndRenderDatePop();
}
function chooseRunDate(key){
  const n = dateToDaysAgo(key);
  if(n === null || n < 0 || n > 3) return;
  LIVE.runDate = key;
  LIVE.runDateTouched = true;
  LIVE.daily = null;
  closeDatePop();
  updateRunDateLabel();
  refreshChecks();
  refreshModelPreview();
  refreshDailyCount();
  if($('#dateBtn')) $('#dateBtn').focus({preventScroll: true});
}
function wireDatePicker(){
  const btn = $('#dateBtn'), pop = $('#datePop');
  if(!btn || !pop) return;
  btn.addEventListener('click', e => {
    e.preventDefault(); e.stopPropagation();
    if(pop.hidden) openDatePop(); else closeDatePop();
  });
  if($('#datePrev')) $('#datePrev').addEventListener('click', () => shiftDateMonth(-1));
  if($('#dateNext')) $('#dateNext').addEventListener('click', () => shiftDateMonth(1));
  if($('#dateGrid')){
    $('#dateGrid').addEventListener('click', e => {
      const b = e.target.closest('button[data-date]');
      if(!b || b.disabled) return;
      chooseRunDate(b.dataset.date);
    });
  }
  document.addEventListener('click', e => {
    if(pop.hidden) return;
    if(pop.contains(e.target) || btn.contains(e.target)) return;
    closeDatePop();
  });
  document.addEventListener('keydown', e => {
    if(e.key === 'Escape') closeDatePop();
  });
  window.addEventListener('resize', closeDatePop);
  document.addEventListener('scroll', closeDatePop, true);
}

function scheduleModelPreview(){ clearTimeout(_modelTimer); _modelTimer = setTimeout(refreshModelPreview, 300); scheduleSplitsPreview(); }
function livePointsPayload(){
  // The exact set drawn on the map, in the bridge's checkpoint shape.  Submit
  // must use it: the endpoint returns a different random five-point draw per
  // request, so a fresh fetch would route through points nobody saw.
  return livePoints.map(p => ({lat: p.lat, lon: p.lng, name: p.n,
                               isMustPoint: p.must, isFixed: p.isFixed,
                               position: p.position, seq: p.seq}));
}
function previewArgs(params, ti){
  const di = dailyCountInfo();
  return {
    km: params.km, pace: params.pace,
    cadence: params.cadence, altitude: params.altitude,
    waypoints: route && route.length >= 2 ? route : null,
    checkpoints: freeRunOn() ? null : livePointsPayload(),
    sequential: !!LIVE.sequential,
    date: ti.date,
    start_time: ti.time,
    closed: !!routeClosed,
    // The preview must show the same laps that will be submitted; otherwise
    // a manual split edit is invisible until after the upload.
    km_times: splitsManual && splitRows
      ? splitRows.map(r => Math.round(+r.seconds || 0)) : null,
    daily_runs: di.loaded ? di.count : null,
    daily_source: di.loaded ? di.source : null,
    ...altitudeArgs(params),
    // v0.1.8 realism switches.  Defaults ("none"/"urban"/false) reproduce the
    // v0.1.7 output exactly, so an untouched install submits the same track.
    ...realismArgs()
  };
}
async function refreshModelPreview(){
  const seq = ++_modelSeq;
  if(!hasSession()){
    LIVE.model = null; LIVE.modelWarnings = []; renderModel(); refreshChecks(); return;
  }
  const ti = selectedTimeInfo();
  if(!ti.ok){ LIVE.model = null; LIVE.modelWarnings = []; renderModel(); refreshChecks(); return; }
  if(!LIVE.rules || !LIVE.rules.configured){ LIVE.model = null; LIVE.modelWarnings = []; renderModel(); refreshChecks(); return; }
  const params = readRunParams();
  if(validateRunParams(params)){
    LIVE.model = null; LIVE.modelWarnings = []; renderModel(); refreshChecks(); return;
  }
  const j = await liveApi('real_preview_model', {
    ...previewArgs(params, ti),
    with_series: false,
    seed: LIVE.previewSeed || null
  });
  if(seq !== _modelSeq) return;
  LIVE.model = liveOk(j) ? ((j.data && j.data.model) || null) : null;
  LIVE.modelWarnings = liveOk(j) && j.data ? (j.data.param_warnings || []) : [];
  if(liveOk(j) && j.data){
    LIVE.previewSeed = j.data.seed || null;
    LIVE.previewData = j.data.preview || null;
  }
  if(!liveOk(j)) liveLog('模型预览失败', j.message || '', 'warn');
  renderModel();
  refreshChecks();
}
/* Product-level plausibility band.  It is deliberately wider than any one
   school's rules: a soft yellow reminder, never a submit block. */
const PLAUSIBLE_PACE_MIN = 4.5, PLAUSIBLE_PACE_MAX = 8.5;
const PLAUSIBLE_CADENCE_MIN = 150, PLAUSIBLE_CADENCE_MAX = 180;
function paramWarnings(pace, cadence){
  const out = [], p = +pace, c = +cadence;
  if(p && (p < PLAUSIBLE_PACE_MIN || p > PLAUSIBLE_PACE_MAX))
    out.push(`配速 ${paceText(p)} 超出常见 4'30"–8'30"`);
  if(c && (c < PLAUSIBLE_CADENCE_MIN || c > PLAUSIBLE_CADENCE_MAX))
    out.push(`步频 ${Math.round(c)} spm 超出常见 150–180`);
  return out;
}

function renderModel(){
  const m = LIVE.model;
  const score = $('#modelScore'), sub = $('#modelSub'), tag = $('#modelTag');
  const checks = $('#modelChecks'), empty = $('#modelEmpty'), bar = $('#modelBar');
  const configured = !!(m && m.configured);
  const rulesConfigured = !!(LIVE.rules && LIVE.rules.configured);
  modelConfigured = configured;
  if(!configured){
    score.textContent = '—';
    sub.textContent = rulesConfigured ? '规则已配置 · 预览失败/等待' : '未配置学校要求';
    tag.textContent = rulesConfigured ? '待预览' : '待配置';
    tag.className = 'tag warn'; checks.hidden = true; empty.hidden = false;
    empty.textContent = rulesConfigured
      ? '规则已保存；预览失败时，提交仍会由后端重新检查。'
      : '先在「设置 → 学校要求」里填写并保存；模型不会用猜测的学校规则。';
    bar.style.width = '0%';
    $('#runModelTag').textContent = rulesConfigured ? '模型待预览' : '模型待配置';
    $('#runModelTag').className = 'tag warn';
    $('#runModelLink').textContent = rulesConfigured ? '检查规则' : '填写学校要求';
    $('#genState').textContent = rulesConfigured ? '待预览' : '待配置';
    $('#genState').className = '';
    $('#runModelChip').textContent = rulesConfigured ? '模型待预览' : '模型未配置';
    $('#runModelChip').className = 'chip warn';
    $('#runModelChip').innerHTML = '<i></i>' + $('#runModelChip').textContent;
    if($('#logModel')) $('#logModel').textContent = '—';
    return;
  }
  score.textContent = String(m.score);
  const pw = LIVE.modelWarnings || [];
  const effective = m.verdict === 'fail' ? 'fail'
    : ((m.verdict === 'warn' || pw.length) ? 'warn' : 'pass');
  const baseSub = m.summary || ((m.verdict === 'pass' ? '合格' : m.verdict === 'warn' ? '有提醒' : '不合格') +
    (m.warnings && m.warnings.length ? (' · ' + m.warnings.length + ' 项注意') : ''));
  sub.textContent = baseSub + (pw.length ? (' · 参数 ' + pw.length + ' 项提醒') : '');
  tag.textContent = effective === 'pass' ? '合格' : effective === 'warn' ? '提醒' : '不合格';
  tag.className = 'tag ' + (effective === 'pass' ? 'ok' : 'warn');
  bar.style.width = Math.max(0, Math.min(100, +m.score || 0)) + '%';
  empty.hidden = true; checks.hidden = false;
  checks.innerHTML = (m.checks || []).map(c => {
    const cls = c.status === 'pass' ? 'ok' : (c.status === 'skip' ? 'skip' : 'warn');
    const txt = c.status === 'pass' ? '合格' : c.status === 'skip' ? '未要求' : c.status === 'warn' ? '注意' : '不合格';
    return `<li><span>${esc(c.label)}</span><b>${esc(c.value)}</b><i class="${cls}">${txt}</i></li>`;
  }).join('') + pw.map(w =>
    `<li><span>${esc(w)}</span><b>参数</b><i class="warn">提醒</i></li>`).join('');
  $('#runModelTag').textContent = '模型 ' + m.score + (pw.length ? ' · 参数提醒' : '');
  $('#runModelTag').className = 'tag ' + (effective === 'pass' ? 'ok' : 'warn');
  $('#runModelLink').textContent = '查看';
  $('#genState').textContent = '模型就绪' + (pw.length ? ' · 参数提醒' : '');
  $('#genState').className = effective === 'pass' ? 'ok' : 'warn';
  $('#runModelChip').textContent = '模型 ' + m.score + (pw.length ? ' · 参数提醒' : '');
  $('#runModelChip').className = 'chip ' + (effective === 'pass' ? 'ok' : 'warn');
  $('#runModelChip').innerHTML = '<i></i>' + $('#runModelChip').textContent;
  if($('#logModel')) $('#logModel').textContent = m.score + ' 分';
  if($('#ovModelChip')){
    $('#ovModelChip').textContent = '模型 ' + m.score + (pw.length ? ' · 参数提醒' : '');
    $('#ovModelChip').className = 'chip ' + (effective === 'pass' ? 'ok' : 'warn');
    $('#ovModelChip').innerHTML = '<i></i>' + $('#ovModelChip').textContent;
  }
}

/* The old refreshModel used a fixed score; the live one only renders
   the backend's rule evaluation (or the empty/not-configured state). */
function refreshModel(){ renderModel(); }

/* ---------------- pre-submit checks ---------------- */
function pointInPolygon(p, polygon){
  const x = +p[0], y = +p[1];
  let inside = false;
  for(let i = 0, j = polygon.length - 1; i < polygon.length; j = i++){
    const a = polygon[i], b = polygon[j];
    const ay = +(a.lat ?? a[0] ?? a.glat), ax = +(a.lon ?? a[1] ?? a.glon);
    const by = +(b.lat ?? b[0] ?? b.glat), bx = +(b.lon ?? b[1] ?? b.glon);
    if(((ay > x) !== (by > x)) && (y < (bx - ax) * (x - ay) / (by - ay) + ax)) inside = !inside;
  }
  return inside;
}
function fenceOutside(routePoints, polygons){
  const out = {points: 0, segments: 0};
  const rings = (Array.isArray(polygons) ? polygons : [])
    .filter(r => Array.isArray(r) && r.length >= 3);
  if(!rings.length || !routePoints || routePoints.length < 2) return out;
  const inside = p => rings.some(ring => pointInPolygon(p, ring));
  for(const p of routePoints){
    if(!inside(p)) out.points++;
  }
  // Sample each segment at <=3 m.  This catches a short notch that both
  // endpoints (and the dense display points) can miss; the backend does the
  // analytic version before submitting.
  for(let i = 0; i < routePoints.length - 1; i++){
    const a = routePoints[i], b = routePoints[i + 1];
    const n = Math.max(1, Math.ceil(distM(a, b) / 3));
    for(let k = 1; k < n; k++){
      const t = k / n;
      const p = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
      if(!inside(p)){ out.segments++; break; }
    }
  }
  return out;
}
function hitRadiusM(){
  const v = +LIVE.hitRadiusM;
  return (isFinite(v) && v > 0) ? v : 25;
}
function computeCoverage(){
  if(!livePoints.length || !route || route.length < 2) return {passed: 0, total: livePoints.length, missed: livePoints.length, radius: hitRadiusM()};
  const path = displayPath();
  const rad = hitRadiusM();
  let passed = 0;
  livePoints.forEach(p => { if(path.some(q => distM(q, [p.lat, p.lng]) <= rad)) passed++; });
  return {passed, total: livePoints.length, missed: livePoints.length - passed, radius: rad};
}
function beijingEpoch(dateKey, hhmm){
  const d = parseDateKey(dateKey);
  if(!d) return Date.now();
  const m = String(hhmm || '09:00').match(/^(\d{1,2}):(\d{2})$/);
  const hh = m ? +m[1] : 9, mm = m ? +m[2] : 0;
  return Date.UTC(d.getFullYear(), d.getMonth(), d.getDate(), hh - 8, mm);
}
function clockSkewText(ms){
  const sec = Math.abs(+ms || 0) / 1000;
  if(sec < 1.5) return '已对齐';
  if(sec < 90) return Math.round(sec) + ' 秒';
  if(sec < 5400) return Math.round(sec / 60) + ' 分钟';
  return (sec / 3600).toFixed(1) + ' 小时';
}
async function runPreflight(){
  const btn = $('#preflightBtn');
  if(btn){ btn.disabled = true; btn.textContent = '查询中…'; }
  const ti = selectedTimeInfo();
  const city = $('#preflightCity'), grid = $('#preflightGrid'), text = $('#preflightText');
  try{
    const j = await liveApi('real_preflight', {
      start_ms: beijingEpoch(ti.date, ti.time || '09:00'),
      campus_name: LIVE.session && LIVE.session.campusName || '',
      school_name: LIVE.session && LIVE.session.schoolName || ''
    }, 30000);
    if(!liveOk(j)){
      if(city) city.textContent = '查询失败（仅提醒）';
      if(grid) grid.hidden = true;
      if(text){
        text.className = 'preflight-warn';
        text.textContent = (j.message || '网络查询失败') + '（仅提醒，不影响提交）';
      }
      liveLog('网络诊断失败', j.message || '', 'warn');
      LIVE.preflight = {ok: false, match: 'error',
        error: j.message || '网络查询失败', at: Date.now()};
      refreshChecks();
      return;
    }
    const d = j.data || {};
    const info = d.info || {};
    const ip = info.ip || null;
    const want = info.expected_city || '';
    const got = ip && ip.city ? ip.city : '';
    const match = info.ip_city_matches;
    const clock = info.server_time || null;
    if(city) city.textContent = match === true
      ? ('同城 · ' + (want || '校区'))
      : (match === false
        ? ('不同城 · ' + (got || '未知') + ' / ' + (want || '校区未知'))
        : '无法确认');
    if(grid){
      grid.hidden = false;
      const clockText = clock && clock.offset_ms != null
        ? (clock.applied === false ? '未校正'
           : (Math.abs(clock.offset_ms) < 1500 ? '已对齐'
              : ((clock.offset_ms > 0 ? '慢 ' : '快 ') +
                 clockSkewText(clock.offset_ms) + ' · 已自动校正')))
        : (clock && (clock.error || clock.reason) ? '读取失败' : '未校准');
      const serverText = clock && clock.server_time
        ? beijingDateTime(clock.server_time) : '—';
      grid.innerHTML = `<div>校区城市 <b>${esc(want || '未知')}</b></div>` +
        `<div>出口 IP <b>${esc(got || '未知')}</b></div>` +
        `<div>本机时钟 <b>${esc(clockText)}</b></div>` +
        `<div>服务器时间 <b>${esc(serverText)}</b></div>` +
        `<div>开始时间 <b>${esc(d.start_text || ti.date + ' ' + (ti.time || '09:00'))}</b></div>`;
    }
    const warns = (d.warnings || []).map(String);
    if(text){
      // Diagnostic only: even a mismatch is a note here, never a gate.
      text.className = 'preflight-' + (match === false ? 'warn' : 'ok');
      const lines = [];
      if(match === false) lines.push('出口 IP 与校区城市不同：运营商省级 NAT 很常见，仅提醒，不影响提交。');
      else if(match !== true) lines.push('无法确认出口城市；仅提醒，不影响提交。');
      if(warns.length) lines.push(...warns.map(w => esc(w)));
      if(!lines.length) lines.push('没有发现明显问题；仅提醒，不影响提交。');
      text.innerHTML = lines.join('<br>');
    }
    liveLog('网络诊断', `${got || '未知'} / 校区 ${want || '未知'}` +
      (warns.length ? (' · ' + warns[0]) : ''), match === false ? 'warn' : 'ok');
    LIVE.preflight = {
      ok: match === true,
      match: match === true ? 'same' : (match === false ? 'diff' : 'unknown'),
      city: got,
      want: want,
      clock: clock,
      clockApplied: !!(clock && clock.applied),
      warnings: warns,
      at: Date.now()
    };
    refreshChecks();
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '开始自检'; }
  }
}

function submitResultKey(){
  return pointsSessionKey();
}
function submitLocked(){
  const r = LIVE.submitResult;
  return !!(r && r.state === 'unknown' && !r.checked &&
            r.key === submitResultKey());
}
function renderSubmitResult(){
  const box = $('#submitResult');
  if(!box) return;
  const r = LIVE.submitResult;
  if(!r || r.key !== submitResultKey()){
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.className = 'submit-result ' + (r.state || 'unknown') +
    (r.warn ? ' warn' : '');
  const badge = $('#submitResultBadge');
  const title = $('#submitResultTitle');
  const text = $('#submitResultText');
  const time = $('#submitResultTime');
  const rec = $('#submitResultRecords');
  const dismiss = $('#submitResultDismiss');
  const labels = {ok: '已提交', fail: '未提交', unknown: '结果未知'};
  if(badge) badge.textContent = (r.state === 'unknown' && r.checked)
    ? '已核对' : (labels[r.state] || '—');
  if(title) title.textContent = r.title || '';
  if(text) text.textContent = r.detail || '';
  if(r.state !== 'ok' && (r.detail || r.title)) recordErrorForExplain(r.detail || r.title, r.state);
  if($('#submitResultVerify')){
    const v = r.rrid || r.uuid;
    $('#submitResultVerify').disabled = !v;
    $('#submitResultVerify').title = v ? '只读核验这条记录（不重发、不 PUT）' : '提交结果里没有 rrid，去记录页核验';
    if(!v && $('#submitVerify')) $('#submitVerify').hidden = true;
  }
  if(time) time.textContent = r.at
    ? new Date(r.at).toLocaleTimeString('zh-CN',
        {hour12: false, hour: '2-digit', minute: '2-digit'})
    : '';
  if(rec){
    rec.hidden = false;
    rec.textContent = (r.state === 'unknown' && !r.checked)
      ? '去记录页只读核对' : '查看提交记录';
  }
  if(dismiss){
    // While the result is unknown and unreconciled this card is the only
    // durable warning; do not let it be dismissed before the read-only check.
    dismiss.hidden = !(r.state !== 'unknown' || r.checked);
  }
}
function setSubmitResult(state, title, detail, extra){
  LIVE.submitResult = Object.assign({
    state: state,
    title: title || '',
    detail: detail || '',
    at: Date.now(),
    key: submitResultKey(),
    checked: state !== 'unknown',
    warn: false,
    rrid: ''
  }, extra || {});
  renderSubmitResult();
  refreshChecks();
}
function clearSubmitResult(){
  if(!LIVE.submitResult) return;
  LIVE.submitResult = null;
  renderSubmitResult();
  refreshChecks();
}

function refreshChecks(){
  const checks = [];
  const verified = !!(LIVE.checked && liveOk(LIVE.checked));
  checks.push({ok: verified, label: 'token 有效',
    note: verified ? '服务端已验证' : (hasSession() ? '未验证/失效' : '未登录'),
    goto: 'accounts'});
  // Network egress stays a diagnostic card on the submit page, never a
  // checklist gate.  IP geolocation is advisory and can be wrong (carrier
  // NAT, stale databases), so it must not change `done`, the dock, or the
  // submit button.  The card is queried only when the operator clicks it.
  const free = freeRunOn();
  const noPoints = !!LIVE.pointsEmpty;
  checks.push({ok: free || livePoints.length > 0,
    warn: free || noPoints,
    label: free ? '跳过打卡点' : (noPoints ? '无打卡点（需确认）' : '点位已加载'),
    note: free ? '不拉取打卡点；官方自由跑（serviceType=3）尚未支持'
      : (livePoints.length ? (livePoints.length + ' 个 · ' + livePoints.filter(p => p.must).length + ' 必经')
        : (noPoints ? '服务器未返回打卡点，提交默认阻止'
          : (LIVE.pointsError ? ('读取失败：' + LIVE.pointsError) : '未加载'))),
    goto: 'run', rtab: 'route'});
  const cov = computeCoverage();
  checks.push({ok: free || (cov.total > 0 && cov.missed === 0),
    warn: free || noPoints,
    label: '经过全部打卡点',
    note: free ? '已勾选跳过打卡点'
      : (noPoints ? '无法确认该校区允许自由跑'
        : (cov.total ? (cov.passed + '/' + cov.total + ' 在 ' + hitRadiusM() + 'm 内') : '未加载点位')),
    goto: 'run', rtab: 'route'});
  if(livePoints.length && pointsDistanceStale()){
    // Not a hard gate: the operator can still submit the displayed set.  But
    // the card must say that the set belongs to an older line length.
    checks.push({ok: true, warn: true, label: '点位与目标距离',
      note: pointsDistanceText() + '；提交会复用旧点位，点此按当前距离刷新',
      goto: 'run', rtab: 'route', action: 'refresh-points'});
  }
  let fenceBad = {points: 0, segments: 0};
  const fenceReady = LIVE.fences.length > 0 && route && route.length >= 2;
  if(fenceReady){
    fenceBad = fenceOutside(displayPath(), LIVE.fences);
  }
  checks.push({ok: !!LIVE.fenceMissing || (fenceReady && fenceBad.points === 0 && fenceBad.segments === 0),
    label: '在跑步围栏内',
    note: LIVE.fenceMissing ? '该校区未配置围栏，不限制位置'
      : (LIVE.fenceError ? ('围栏读取失败：' + LIVE.fenceError)
        : (!LIVE.fences.length ? '围栏未加载'
          : (!route || route.length < 2 ? '待画线'
            : (fenceBad.points || fenceBad.segments
              ? ('越界 ' + fenceBad.points + ' 点 / ' + fenceBad.segments + ' 段')
              : ('整段检查通过' + (LIVE.fences.length > 1
                ? '（' + LIVE.fences.length + ' 个围栏任一）' : '')))))),
    goto: 'run', rtab: 'route'});
  const configured = !!(LIVE.rules && LIVE.rules.configured);
  checks.push({ok: configured, label: '学校要求已配置',
    note: configured ? '模型已启用' : '设置 → 学校要求',
    goto: 'settings'});

  // Freeze / face / distance belong in the same checklist as the points:
  // they used to exist only as log lines, so the dock could read "可以提交"
  // while the server would refuse the run (or the record would not count).
  if(LIVE.freezeSeconds > 0){
    const fm = Math.ceil(LIVE.freezeSeconds / 60);
    // Per product decision the freeze is a warning, not a gate: keep `ok`
    // true so the dock still says "可以提交", and colour the note amber.
    checks.push({ok: true, warn: true, label: '跑步冻结期',
      note: '剩余约 ' + fm + ' 分钟；官方此刻不能开跑，本工具只提醒不拦截'});
  }else{
    checks.push({ok: true, label: '跑步冻结期', note: '无冻结'});
  }
  checks.push({ok: !LIVE.faceRequired, label: '人脸抽检',
    note: LIVE.faceRequired
      ? ((LIVE.faceReason || 'faceVerify') + '；本工具无法完成核验，提交会被阻止')
      : '学校未要求'});
  const ruleSet = (configured && LIVE.rules.rules) ? LIVE.rules.rules : {};
  const minKm = +ruleSet.min_km > 0 ? +ruleSet.min_km : 0;
  const maxKm = +ruleSet.max_km > 0 ? +ruleSet.max_km : 0;
  const kmInput = $('#inKm');
  const plannedKm = kmInput ? (+kmInput.value || 0) : 0;
  const hasTarget = plannedKm > 0;
  if(minKm || maxKm){
    const distOk = hasTarget &&
      (!minKm || plannedKm >= minKm) && (!maxKm || plannedKm <= maxKm);
    checks.push({ok: distOk, label: hasTarget ? '距离达标' : '目标距离',
      note: hasTarget
        ? (plannedKm.toFixed(2) + ' km / 要求 ' +
           (minKm ? ('≥ ' + minKm) : '') + (minKm && maxKm ? '、' : '') +
           (maxKm ? ('≤ ' + maxKm) : '') + ' km')
        : '先手绘一条路线，或在参数页填写目标距离',
      goto: 'run', rtab: 'params', focus: 'inKm'});
  }else{
    checks.push({ok: hasTarget, label: '目标距离',
      note: hasTarget ? (plannedKm.toFixed(2) + ' km（学校未配置范围）')
        : '先手绘一条路线，或在参数页填写目标距离',
      goto: 'run', rtab: 'params', focus: 'inKm'});
  }

  const ti = selectedTimeInfo();
  checks.push({ok: ti.dateOk, label: '提交日期', note: ti.dateNote,
    goto: 'run', rtab: 'params', focus: 'dateBtn'});
  checks.push({ok: ti.timeOk, label: '开始时间', note: ti.timeNote || ti.note,
    goto: 'run', rtab: 'params', focus: 'inStart'});

  const di = dailyCountInfo();
  let dWarn = false, dNote = '';
  if(!di.loaded){
    dWarn = true;
    dNote = '次数未知（仅提醒，提交时后端会再查）';
  }else{
    // Use the direct date counter, not a possibly stale model preview for a
    // previously selected date.  The school cap is advisory: show it in amber,
    // but never stop the operator from submitting.
    dWarn = !di.ok;
    dNote = dateDisplay(di.key) + ' 已跑 ' + di.count + ' 次' +
      (di.cap > 0 ? ' / 上限 ' + di.cap : '（未设置每日上限）') +
      (di.ok ? '' : '（已达上限，仅提醒）') +
      (di.source === 'local' ? '（本地回执）' : '');
  }
  checks.push({ok: true, warn: dWarn, label: '当日次数', note: dNote,
    goto: 'records'});

  const wrap = $('#checks');
  if(wrap){
    wrap.innerHTML = checks.map(c => {
      const attrs = [];
      if(c.goto) attrs.push(`data-goto="${esc(c.goto)}"`);
      if(c.rtab) attrs.push(`data-rtab="${esc(c.rtab)}"`);
      if(c.focus) attrs.push(`data-focus="${esc(c.focus)}"`);
      if(c.action) attrs.push(`data-check-action="${esc(c.action)}"`);
      if(c.goto) attrs.push('role="button" tabindex="0"');
      return `<div class="check-row ${c.ok ? 'checked' : 'warn'}${c.warn ? ' note-warn' : ''}" ${attrs.join(' ')}>` +
        `<span class="cbox"></span><span class="clabel">${esc(c.label)}</span>` +
        `<span class="cnote">${esc(c.note)}</span></div>`;
    }).join('');
  }
  const done = checks.filter(c => c.ok).length;
  const all = done === checks.length;
  if($('#checkSummary')) $('#checkSummary').textContent = done + '/' + checks.length + ' 已核对';
  if($('#readyCount')) $('#readyCount').textContent = done + '/' + checks.length;
  if($('#readyBar')) $('#readyBar').style.width = (checks.length ? (done / checks.length * 100) : 0) + '%';
  const dock = $('#dock'), dockText = $('#dockText'), dockBtn = $('#dockBtn'), stamp = $('#stamp');
  if(dock) dock.classList.toggle('ready', all);
  if(dockText){
    const missing = checks.filter(c => !c.ok).map(c => c.label);
    dockText.textContent = all ? '检查通过 · 可以提交' : ('还差：' + missing.slice(0, 2).join('、') + (missing.length > 2 ? '…' : ''));
  }
  if(dockBtn){
    dockBtn.textContent = all ? '去提交' : '去核对';
    dockBtn.classList.toggle('ghost', !all);
  }
  if(stamp){ stamp.textContent = all ? '已核对' : '未核对'; stamp.classList.toggle('show', all); }
  const dot = $('#submitDot'); if(dot) dot.classList.toggle('ok', all);
  const locked = submitLocked();
  if($('#submitRun')){
    $('#submitRun').disabled = !hasSession() || !verified ||
      LIVE.submitting || locked || !hasTarget;
    if(!LIVE.submitting){
      $('#submitRun').textContent = locked
        ? '先核对回执再提交'
        : (!hasTarget ? '先画路线' : '提交这次跑步');
    }
  }
  if($('#submitHint')){
    $('#submitHint').textContent = locked
      ? '上次提交结果未知：先到「记录」页点「只读核对未决」，核对完成后才能再次提交。'
      : (!hasTarget
        ? '目标距离当前为 0：先在「路线」页手绘，或手动填写目标距离。'
        : (all ? '检查通过。提交后该时段会被回执锁定，不能重复提交。'
               : '先补齐上面的检查项；提交仍会由后端再次校验。'));
  }
  refreshOverviewState();
}

function liveDockClick(){
  switchRunTab('submit');
  if(!$('#dock').classList.contains('ready')){
    const missing = [...document.querySelectorAll('#checks .check-row.warn .clabel')].map(x => x.textContent.trim());
    toast('还差：' + (missing.length ? missing.slice(0, 3).join('、') : '提交前检查'));
    return;
  }
  toast('检查通过，点「提交这次跑步」');
}

/* ---------------- submit ---------------- */
function previewPendingFrom(params, ti, allReady){
  let di = dailyCountInfo();
  const dayText = dateDisplay(ti.date);
  const timeText = ti.time || (ti.windows && ti.windows.length
    ? ('留空 · 在 ' + windowsText(ti.windows) + ' 内随机')
    : '09:00–22:00 内随机');
  const dailyText = di.loaded
    ? (di.count + ' / ' + (di.cap || '—') +
       (di.ok ? '' : '（已达上限，仅提醒）'))
    : '未知（后端会再查）';
  const missing = allReady ? [] :
    [...document.querySelectorAll('#checks .check-row.warn .clabel')]
      .map(x => x.textContent.trim());
  return {
    params, ti, allReady, dayText, timeText, dailyText, missing,
    band: paramWarnings(params.pace, params.cadence),
    seed: LIVE.previewSeed || null
  };
}
async function submitRun(){
  if(LIVE.submitting) return;
  if(PENDING_SUBMIT) return;
  if(submitLocked()){
    toast('上次提交结果未知；先到「记录」页完成只读核对');
    go('records');
    return;
  }
  if(accountBusy){ toast('账号操作进行中，请稍候'); return; }
  if(!hasSession()){ toast('先登录'); switchRunTab('submit'); return; }
  if(!LIVE.checked || !liveOk(LIVE.checked)){
    toast('token 未验证或已失效；先到「账号」页测试 token');
    liveLog('提交已阻止', 'token 未验证，不能提交', 'warn');
    switchRunTab('submit');
    return;
  }
  const params = readRunParams();
  const invalid = validateRunParams(params);
  if(invalid){ toast(invalid); switchRunTab('params'); return; }
  const ti = selectedTimeInfo();
  if(!ti.ok){ toast('开始时间无效：' + ti.note); switchRunTab('params'); return; }
  refreshChecks();
  const allReady = !!(document.querySelector('#dock') &&
    document.querySelector('#dock').classList.contains('ready'));
  let di = dailyCountInfo();
  if(!di.loaded){
    toast('当日次数还没核对完，正在重试…');
    if(!LIVE.recordsLoaded) await loadRecords();
    di = dailyCountInfo();
    if(!di.loaded){ await refreshDailyCount(); di = dailyCountInfo(); }
  }
  await openDataPreviewForSubmit(previewPendingFrom(params, ti, allReady));
}

async function openDataPreviewForSubmit(pending){
  if(!pending) return;
  PENDING_SUBMIT = pending;
  if($('#dataPreview')){
    $('#dataPreview').hidden = false;
    setTimeout(() => {
      const close = $('#dpClose');
      if(close) close.focus();
    }, 30);
  }
  if($('#dpKicker')) $('#dpKicker').textContent = '提交前预览';
  if($('#dpTitle')) $('#dpTitle').textContent = '正在生成本次数据…';
  if($('#dpSummary')) $('#dpSummary').innerHTML = '<div class="mini">按当前参数生成配速、步频、步幅和海拔…</div>';
  ['#dpPace','#dpCadence','#dpStride','#dpAltitude'].forEach(sel => {
    const el = $(sel); if(el) el.innerHTML = '';
  });
  if($('#dpTable')) $('#dpTable').innerHTML = '';
  if($('#dpConfirm')){ $('#dpConfirm').hidden = false; $('#dpConfirm').disabled = true; $('#dpConfirm').textContent = '生成中…'; }
  try{
    const j = await liveApi('real_preview_model', {
      ...previewArgs(pending.params, pending.ti),
      with_series: true,
      seed: pending.seed || null
    }, 45000);
    if(!PENDING_SUBMIT) return; // closed while waiting
    if(liveOk(j) && j.data && j.data.preview){
      PENDING_SUBMIT.seed = j.data.seed || pending.seed || null;
      renderDataPreview(j.data.preview, 'submit', PENDING_SUBMIT);
    }else{
      renderDataPreview(null, 'submit', PENDING_SUBMIT, j.message || '预览生成失败');
    }
  }catch(e){
    if(PENDING_SUBMIT) renderDataPreview(null, 'submit', PENDING_SUBMIT, String(e.message || e));
  }
}

function renderDataPreview(data, mode, pending, errorText){
  const isRecord = mode === 'record';
  const rules = (LIVE.rules && LIVE.rules.rules) || {};
  const p = pending || {};
  const d = data || {};
  const km = d.distance_m ? d.distance_m / 1000 : (+p.params?.km || 0);
  const duration = d.duration_s || (p.params ? Math.round(+p.params.km * +p.params.pace * 60) : 0);
  const avgPace = d.avg_pace || (p.params ? +p.params.pace : null);
  const avgCad = d.avg_cadence || (p.params ? +p.params.cadence : null);
  const avgStride = d.avg_stride || null;
  const altText = (d.alt_min != null && d.alt_max != null) ? (d.alt_min + '–' + d.alt_max) : '—';
  const sum = [
    ['距离', km ? km.toFixed(2) : '—', 'km'],
    ['总时长', duration ? hms(duration) : '—', ''],
    ['平均配速', HXPCharts.fmtPace(avgPace), '/km'],
    ['平均步频', avgCad ? Math.round(avgCad) : '—', 'spm'],
    ['平均步幅', avgStride ? Math.round(avgStride) : '—', 'cm'],
    ['海拔', altText, 'm'],
    ['累计爬升', d.ascent != null ? d.ascent : '—', 'm'],
    ['热量', d.kcal != null ? d.kcal : '—', 'kcal']
  ];
  if($('#dpSummary')){
    $('#dpSummary').innerHTML = sum.map(([k,v,u]) =>
      '<div><span>' + k + '</span><b>' + esc(v) + '</b>' +
      (u ? '<small>' + u + '</small>' : '') + '</div>').join('');
  }
  if($('#dpKicker')) $('#dpKicker').textContent = isRecord ? '记录回看' : '提交前预览';
  if($('#dpTitle')) $('#dpTitle').textContent = isRecord
    ? ('记录数据 · ' + (km ? km.toFixed(2) + ' km' : '—'))
    : ('本次数据 · ' + (km ? km.toFixed(2) + ' km' : '—'));
  const series = Array.isArray(d.series) ? d.series : [];
  const paceBand = (rules.pace_min != null && rules.pace_max != null)
    ? [+rules.pace_min, +rules.pace_max] : null;
  const cadBand = (rules.step_min != null && rules.step_max != null)
    ? [+rules.step_min, +rules.step_max] : null;
  if($('#dpPaceNote')){
    $('#dpPaceNote').textContent = series.length
      ? ('平均 ' + HXPCharts.fmtPace(avgPace) +
         (d.min_pace ? (' · 最快 10 秒 ' + HXPCharts.fmtPace(d.min_pace)) : ''))
      : '分/公里';
  }
  if($('#dpCadNote')) $('#dpCadNote').textContent = d.avg_cadence
    ? ('平均 ' + Math.round(d.avg_cadence) + ' · 最大 ' + Math.round(d.max_cadence) +
       ' · 最小 ' + Math.round(d.min_cadence))
    : '步/分钟';
  if($('#dpStrideNote')) $('#dpStrideNote').textContent = d.avg_stride
    ? ('平均 ' + Math.round(d.avg_stride) + ' 厘米 · 尖峰已截断')
    : '厘米';
  if($('#dpAltNote')) $('#dpAltNote').textContent = (d.alt_min != null)
    ? (d.alt_min + '–' + d.alt_max + ' 米') : '米';
  if(errorText){
    ['#dpPace','#dpCadence','#dpStride','#dpAltitude'].forEach(sel => {
      const el = $(sel);
      if(el) el.innerHTML = '<div class="mini" style="padding:10px 0">' + esc(errorText) + '</div>';
    });
  }else{
    HXPCharts.lineChart($('#dpPace'), series, {
      key:'pace', invert:true, formatY:HXPCharts.fmtPace,
      band:paceBand, bandNote:paceBand ? ('学校 ' + HXPCharts.fmtPace(paceBand[0]) +
        '–' + HXPCharts.fmtPace(paceBand[1])) : ''
    });
    HXPCharts.barChart($('#dpCadence'), series, {
      key:'cadence', formatY:v=>Math.round(v),
      band:cadBand, bandNote:cadBand ? ('下限 ' + Math.round(cadBand[0])) : ''
    });
    HXPCharts.barChart($('#dpStride'), series, {key:'stride', clamp:[40,200], formatY:v=>Math.round(v)});
    // Altitude must auto-fit the run's own band.  A zero-based axis turned
    // every 10-20 m trace into a flat line at the top of the chart.
    HXPCharts.lineChart($('#dpAltitude'), series, {
      key:'alt', area:true, lineClass:'dp-alt-line', tight:true, minSpan:4,
      formatY:v=>Math.round(v)
    });
  }
  const rows = Array.isArray(d.splits) ? d.splits : [];
  let best = -1;
  rows.forEach((r, i) => {
    if(+r.km < 0.999) return;            // only full kilometres compete
    if(!(+r.pace > 0)) return;
    if(best < 0 || +r.pace < +rows[best].pace) best = i;
  });
  if($('#dpTable')){
    if(!rows.length){
      $('#dpTable').innerHTML = '<div class="mini" style="grid-column:1/-1;padding:8px 10px">' +
        (isRecord ? '这条记录没有可用的每公里数据。' : '还没有分段数据；返回参数页重试。') + '</div>';
    }else{
      $('#dpTable').innerHTML =
        '<div class="dp-th">#</div><div class="dp-th">公里</div>' +
        '<div class="dp-th">分段用时</div><div class="dp-th">累计用时</div><div class="dp-th">配速</div>' +
        rows.map((r,i) => {
          const isBest = i === best;
          return '<div class="dp-row' + (isBest ? ' best' : '') + '">' +
            '<div class="dp-cell dp-num">' + (i + 1) + '</div>' +
            '<div class="dp-cell dp-num">' + (+r.km).toFixed(2) + '</div>' +
            '<div class="dp-cell dp-num">' + splitFmt(r.seconds) + '</div>' +
            '<div class="dp-cell dp-num">' + splitFmt(r.cumulative || r.seconds) + '</div>' +
            '<div class="dp-cell dp-num dp-pace">' + HXPCharts.fmtPace(+r.pace / 60) + '</div>' +
            '</div>';
        }).join('');
    }
  }
  if($('#dpTableNote')) $('#dpTableNote').textContent = rows.length
    ? (rows.length + ' 段' + (best >= 0 ? ' · 最快一公里已标出' : '')) : '';
  if($('#dpFootNote')){
    if(isRecord){
      $('#dpFootNote').textContent = '数据从服务器回读轨迹按 10 秒重采样；没有步频字段时显示 —。';
    }else{
      const missing = (p.missing || []).length ? (' · 未通过：' + p.missing.join('、')) : '';
      $('#dpFootNote').textContent = (p.dayText || '') + ' ' + (p.timeText || '') +
        ' · 当日 ' + (p.dailyText || '未知') + missing +
        ' · 预览按当前参数生成，最终以服务器回读为准。';
    }
  }
  if($('#dpConfirm')){
    $('#dpConfirm').hidden = isRecord;
    $('#dpConfirm').disabled = false;
    $('#dpConfirm').textContent = errorText ? '仍然提交' : '确认提交';
  }
  if($('#dpCancel')) $('#dpCancel').textContent = isRecord ? '关闭' : '返回修改';
  if($('#altPreview')){
    if(series.length && series.some(s => s.alt != null)){
      HXPCharts.spark($('#altPreview'), series, 'alt');
    }else{
      $('#altPreview').hidden = true;
    }
  }
  if($('#dataPreview')) $('#dataPreview').hidden = false;
}

function closeDataPreview(){
  if($('#dataPreview')) $('#dataPreview').hidden = true;
}
function cancelDataPreview(){
  PENDING_SUBMIT = null;
  closeDataPreview();
}
async function doSubmitRun(pending){
  if(!pending || LIVE.submitting) return;
  const {params, ti, band, dayText, timeText} = pending;
  const {km, pace, cadence, altitude} = params;
  PENDING_SUBMIT = null;
  LIVE.submitting = true;
  const btn = $('#submitRun');
  if(btn){ btn.disabled = true; btn.textContent = '提交中…'; }
  if(band && band.length) liveLog('参数提醒', band.join('；'), 'warn');
  const altModeText = params.altitudeMode === 'fixed' ? ('固定 ' + altitude + ' m')
    : params.altitudeMode === 'range' ? ('区间 ' + (params.altitudeRange || []).join('–') + ' m')
    : params.altitudeMode === 'template' ? ('模板 ' + (altTemplateLabel || '已载入'))
    : ('自动 · 基准 ' + altitude + ' m');
  liveLog('开始提交', `${km} km · ${pace.toFixed(2)} /km · ${cadence} spm · 海拔 ${altModeText} · ${dayText} ${timeText}`, 'info');
  let j;
  try{
    j = await liveApi('real_submit_run', {
      km: km, pace: pace, cadence: cadence, altitude: altitude,
      waypoints: route && route.length ? route : null,
      snap_route: true, use_points: !freeRunOn(), geeToken: '',
      date: ti.date, start_time: ti.time, closed: !!routeClosed,
      km_times: splitRows ? splitRows.map(r => r.seconds) : null,
      seed: pending.seed || null,
      run_area_id: chosenRunAreaId(),
      // Submit the set the operator saw, not a fresh random draw for the
      // current line length.
      points_distance_m: LIVE.pointsDistance || null,
      points: (!freeRunOn() && livePoints.length) ? livePointsPayload() : null,
      ...altitudeArgs(params),
      ...realismArgs()
    }, 180000);
  }finally{
    LIVE.submitting = false;
    if(btn){
      btn.disabled = submitLocked();
      btn.textContent = submitLocked()
        ? '先核对回执再提交' : '提交这次跑步';
    }
  }
  if(!liveOk(j)){
    const d = j.data || {};
    if(d.model){ _modelSeq++; LIVE.model = d.model; renderModel(); }
    const msg = String(j.message || '');
    const unknown = /超时|未知|已锁定|可能仍在执行|bridge 不可用|bridge 返回非 JSON/.test(msg);
    if(unknown){
      setSubmitResult('unknown', '提交结果未知',
        (msg || '没有拿到服务器结果。') +
        ' 后台可能已经受理；已禁用重复提交。请到「记录」页点「只读核对未决」，确认结果后再操作。',
        {warn: true});
      // A write-ahead receipt is the durable proof of an in-flight submit;
      // surface its count on the card without asking the operator to hunt.
      await loadReceipts();
      const pending = (LIVE.receipts || [])
        .filter(r => r.state === 'pending' || r.state === 'unknown');
      if(pending.length && LIVE.submitResult){
        LIVE.submitResult.detail += ' 已记录 ' + pending.length + ' 条未决回执。';
        renderSubmitResult();
      }
    }else{
      setSubmitResult('fail', '提交失败', msg || '服务器拒绝了这次提交。');
      loadReceipts();
    }
    liveLog(unknown ? '提交结果未知' : '提交失败', msg, unknown ? 'warn' : 'err');
    toast((unknown ? '提交结果未知（后台可能仍在执行，请勿重复提交）：'
                   : '提交失败：') + msg);
    // Stay on the submit tab: the persistent card explains the lock.  The
    // old code jumped to the log and the toast vanished after 2.6 s.
    switchRunTab('submit');
    refreshChecks();
    return;
  }
  const d = j.data || {};
  LIVE.lastSubmit = d;
  if(d.model){ _modelSeq++; LIVE.model = d.model; renderModel(); }
  const warnings = [];
  if(d.readback_error) warnings.push('回读失败：' + d.readback_error);
  if(!d.readback && !d.readback_error) warnings.push('没有回读结果');
  if(d.readback && (d.readback.complete === false || d.readback.complete === 0)) warnings.push('回读未完成');
  if(d.readback && d.readback.isCheat) warnings.push('回读标记 isCheat=' + d.readback.isCheat);
  if(d.obs_error) warnings.push('轨迹上传失败：' + d.obs_error);
  if(d.obs && d.obs.of && ((d.obs.uploaded || 0) < d.obs.of || (d.obs.verified || 0) < d.obs.of)){
    const obsErr = (d.obs.errors && d.obs.errors.length) ? ('（' + d.obs.errors[0] + '）') : '';
    warnings.push('轨迹上传不完整：' + (d.obs.uploaded || 0) + '/' + d.obs.of +
      ' 上传，' + (d.obs.verified || 0) + '/' + d.obs.of + ' 校验' + obsErr);
  }
  if(d.receipt_error) warnings.push('回执记录失败：' + d.receipt_error);
  if(d.missed_points && d.missed_points.length) warnings.push('仍有点位未经过');
  if(d.daily && d.daily.source === 'unknown') warnings.push('当日次数未知');
  (d.param_warnings || []).forEach(w => warnings.push(w));
  const obsText = (d.obs && d.obs.of)
    ? ' · OBS ' + (d.obs.uploaded || 0) + '/' + d.obs.of + ' 上传，' +
      (d.obs.verified || 0) + '/' + d.obs.of + ' 校验'
    : '';
  const summary = 'rrid=' + (d.rrid || '') + ' · ' + (d.km || km) + ' km' +
    (d.start ? ' · ' + d.start.text : '') + obsText;
  if(warnings.length){
    liveLog('提交已受理（有提醒）', summary + ' · ' + warnings.join('；'), 'warn');
    toast('已提交，但有提醒：' + warnings[0]);
  }else{
    liveLog('提交成功', summary, 'ok');
    toast('提交成功：rrid ' + (d.rrid || ''));
  }
  setSubmitResult(
    'ok',
    (warnings.length ? '提交已受理（有提醒）' : '提交成功') +
      (d.rrid ? (' · rrid ' + d.rrid) : ''),
    summary + (warnings.length ? (' · ' + warnings.join('；')) : ''),
    {warn: warnings.length > 0, rrid: d.rrid || ''});
  refreshChecks();
  switchRunTab('log');
  openPostSubmit(d, pending, warnings);
  await loadOverview();
  LIVE.daily = null;
  await loadRecords();
  LIVE.dailyCountsAt = 0;
  loadDateCounts(true);
  loadReceipts();
}

/* ---------------- post-submit route cleanup ---------------- */
function routeLineKey(line){
  return (line || []).map(p => (+p[0]).toFixed(5) + ',' + (+p[1]).toFixed(5)).join('|');
}
function clearCurrentRouteAfterSubmit(message){
  clearTimeout(_routeFinalTimer);
  route = [];
  // Do not let undo resurrect the route that was just submitted: reusing it
  // with a different time window is a deliberate act, not an undo slip.
  resetRouteHistory();
  redraw();
  if($('#ptCount')) $('#ptCount').textContent = '0 个控制点';
  toast(message || '已清空路线，可以画下一条');
}
function openPostSubmit(d, pending, warnings){
  if(!route || route.length < 2) return;
  POST_SUBMIT = {
    data: d || {},
    params: pending.params,
    rrid: (d && d.rrid) || '',
    route: route.map(p => [+p[0], +p[1]]),
    closed: !!routeClosed,
    warnings: warnings || []
  };
  if($('#postSummary')){
    const km = pending.params.km;
    const warn = (warnings || []).length
      ? '<div class="tag warn" style="margin-top:6px">' + esc(warnings[0]) + '</div>' : '';
    $('#postSummary').innerHTML = 'rrid <b>' + esc(POST_SUBMIT.rrid || '—') + '</b> · ' +
      km.toFixed(2) + ' km · ' + esc(pending.dayText || '') + ' ' +
      esc(pending.timeText || '') + warn;
  }
  if($('#postSubmit')){
    $('#postSubmit').hidden = false;
    setTimeout(() => {
      const primary = $('#postSaveClear');
      if(primary) primary.focus();
    }, 30);
  }
}
function closePostSubmit(){
  if($('#postSubmit')) $('#postSubmit').hidden = true;
}
async function postSaveAndClear(){
  const p = POST_SUBMIT;
  if(!p) return;
  const line = capRoutePoints(validRouteLine(p.route));
  if(line.length < 2){ postClearRoute(); return; }
  const key = routeLineKey(line);
  const exists = (LIVE.routes || []).some(r => routeLineKey(validRouteLine(r.line)) === key);
  if(!exists){
    const item = {
      name: '提交路线 ' + beijingTodayKey().slice(5) + ' · ' + (+p.params.km).toFixed(2) + 'km',
      type: 'record',
      line: line,
      closed: !!p.closed,
      saved_at: beijingTodayKey()
    };
    const ok = await persistRoutes((LIVE.routes || []).concat([item]));
    if(!ok){
      if($('#postSummary')) $('#postSummary').insertAdjacentHTML('beforeend',
        '<div class="tag red" style="margin-top:6px">路线库保存失败，路线先保留，可重试</div>');
      return;
    }
    LIVE.selectedRoute = LIVE.routes.length - 1;
    renderRouteList();
    selectRoute(LIVE.selectedRoute);
  }
  clearCurrentRouteAfterSubmit(exists ? '路线库已有，已清空当前路线' : '已加入路线库并清空当前路线');
  closePostSubmit();
  POST_SUBMIT = null;
}
function postClearRoute(){
  clearCurrentRouteAfterSubmit('已清空当前路线');
  closePostSubmit();
  POST_SUBMIT = null;
}
function postKeepRoute(){
  closePostSubmit();
  POST_SUBMIT = null;
}


/* ---------------- wire live controls ---------------- */
let snapping = false;
/* ---------------- first-run guide ---------------- */
const ONBOARD_KEY = 'hxp.onboard.v2';
const ONBOARD_STEPS = [
  {
    title: '已登录：工具已经知道你在哪个校区',
    body: '<p>登录后，工具会从服务器读取你所在校区的<b>跑步围栏</b>和<b>打卡点</b>。</p>' +
      '<ul><li>围栏、点位都来自真实接口；没有 token 不会生成假点位。</li>' +
      '<li>多校区学校可在跑步页切换「跑区」，每个跑区用自己的围栏和打卡点。</li>' +
      '<li>有些学校不配置围栏/打卡点，也能正常提交；<b>没有围栏</b>时才需要把地图拖到学校后点「设为校区」。</li>' +
      '<li>token 失效时，到「账号」页重新验证或登录。</li></ul>' +
      '<div class="onboard-legal"><b>使用边界</b><span>仅限本人使用。禁止倒卖、分发安装包、代跑收费或共享账号；不要把 data 目录、token、回执发给他人。</span></div>',
    tip: '第一次登录要在本地解极验，会慢一点；之后一般几秒内完成。'
  },
  {
    title: '路线：手绘最稳，自动还在开发中',
    body: '<p>跑步页有四种路线：<b>手绘</b>、<b>导入</b>（GPX/GeoJSON/CSV）、<b>田径场</b>，以及仍在开发中的<b>自动</b>。</p>' +
      '<ul><li><b>优先手绘</b>：点地图加点，右键删点，最可控。</li>' +
      '<li><b>不建议自动</b>：目前只是按点位连一圈，路线质量不稳定。</li>' +
      '<li>围栏校验会检查整段路线；提交前看到「越界 0 点 / 0 段」才稳。</li></ul>',
    tip: '从「手绘」开始；需要绕圈时再打开「闭合」。',
    actions: [{label:'去手绘', goto:'run', tab:'route', mode:'draw'}]
  },
  {
    title: '参数：每公里和海拔默认自动',
    body: '<p>填目标距离、配速和步频即可。</p>' +
      '<ul><li><b>每公里用时</b>默认按配速自动分配；想改再点「手动微调」。</li>' +
      '<li><b>海拔</b>默认自动，以基准海拔轻微起伏，一般不用改。</li>' +
      '<li>配速/步频超出常见范围只是黄色提醒，不会硬拦。</li></ul>',
    tip: '模板海拔只在本机解析，不会上传，也不会写进记录。',
    actions: [{label:'去填参数', goto:'run', tab:'params'}]
  },
  {
    title: '提交前先看一遍数据',
    body: '<p>点「提交这次跑步」会先弹出<b>官方同款数据预览</b>：配速、步频、步幅、海拔四张图，加上每公里用时表。</p>' +
      '<ul><li>先核对距离、时长、配速和最高/最低值。</li>' +
      '<li>确认无误再提交；返回修改不会提交任何数据。</li></ul>',
    tip: '预览按当前参数生成，最终以服务器回读为准。'
  },
  {
    title: '提交后回执锁定，不能重复提交',
    body: '<p>提交成功后，回执会锁住这个时段；同一时段不能重复提交。</p>' +
      '<ul><li>「记录」页可以回看轨迹、爬升和平台检查项。</li>' +
      '<li>刚上传的路线可以选择<b>存进路线库并清空</b>，下一条从空白开始。</li></ul>',
    tip: '出问题在「设置 → 关于」复制诊断包；里面带版本和构建号，不含 token/坐标/IP。'
  }
];
let onboardIndex = 0;
let onboardFocus = null;
let onboardWait = null;

function renderOnboard(){
  const s = ONBOARD_STEPS[onboardIndex];
  if(!s) return;
  $('#onboardKicker').textContent = '新手指引 · ' + (onboardIndex + 1) + '/' + ONBOARD_STEPS.length;
  $('#onboardDots').innerHTML = ONBOARD_STEPS.map((_, i) =>
    '<i class="' + (i <= onboardIndex ? 'on' : '') + '"></i>').join('');
  const actions = (s.actions || []).map(a =>
    `<button type="button" class="btn small ghost" data-onboard-goto="${a.goto}"` +
    (a.tab ? ` data-onboard-tab="${a.tab}"` : '') +
    (a.mode ? ` data-onboard-mode="${a.mode}"` : '') + `>${a.label}</button>`).join('');
  $('#onboardBody').innerHTML = '<h2 id="onboardTitle">' + s.title + '</h2>' + s.body +
    (s.tip ? '<div class="onboard-tip">' + s.tip + '</div>' : '') +
    (actions ? '<div class="onboard-actions-inline">' + actions + '</div>' : '');
  $('#onboardPrev').disabled = onboardIndex === 0;
  $('#onboardNext').textContent =
    onboardIndex === ONBOARD_STEPS.length - 1 ? '开始使用' : '下一步';
  $('#onboardBody').querySelectorAll('[data-onboard-goto]').forEach(btn => {
    btn.addEventListener('click', () => {
      const page = btn.dataset.onboardGoto;
      if(btn.dataset.onboardMode && typeof setMode === 'function') setMode(btn.dataset.onboardMode);
      if(btn.dataset.onboardTab && typeof switchRunTab === 'function') switchRunTab(btn.dataset.onboardTab);
      closeOnboard(true);
      if(page && typeof go === 'function') go(page);
    });
  });
}
function openOnboard(){
  if(!$('#onboard')) return;
  // A saved session is restored during boot, so this can fire while the boot
  // splash (z 1000) is still on screen.  Two aria-modal dialogs stacked on top
  // of each other break the intro; wait until splash.js has removed it.
  if($('#bootSplash')){
    if(!onboardWait){
      onboardWait = setTimeout(() => { onboardWait = null; openOnboard(); }, 400);
    }
    return;
  }
  // Never stack two aria-modal dialogs.  If an announcement won the race,
  // let it close first; this timer retries after it is hidden.
  const announcement = $('#announcementModal');
  if(announcement && !announcement.hidden){
    if(!onboardWait){
      onboardWait = setTimeout(() => { onboardWait = null; openOnboard(); }, 400);
    }
    return;
  }
  onboardFocus = document.activeElement;
  onboardIndex = 0;
  renderOnboard();
  $('#onboard').hidden = false;
  setTimeout(() => { if($('#onboardClose')) $('#onboardClose').focus(); }, 30);
}
function closeOnboard(remember){
  const box = $('#onboard');
  if(!box || box.hidden) return;
  box.hidden = true;
  // null = "skip this time": leave the stored preference untouched, so a
  // reopened guide does not reset onboard_done and a first-run skip still
  // shows again next launch unless the operator ticks the opt-out box.
  const keep = remember === true
    ? !!( $('#onboardNever') && $('#onboardNever').checked)
    : (remember === false ? false : null);
  if(remember !== null){
    try{
      if(keep) localStorage.setItem(ONBOARD_KEY, 'done');
      else localStorage.removeItem(ONBOARD_KEY);
    }catch(e){}
    // localStorage is scoped to a random localhost port; the backend copy is
    // the one that actually survives a restart.
    liveApi('real_set_ui_state', {onboard_done: keep}, 8000).catch(() => {});
  }
  if(keep) toast('已关闭自动显示；可在「设置 → 关于」重看');
  // The announcement waits while the first-run guide is open.
  setTimeout(() => {
    if(window.Announcements) Announcements.maybeShow();
  }, 500);
  if(onboardFocus && onboardFocus.focus){
    try{ onboardFocus.focus(); }catch(e){}
  }
}
function onboardNext(){
  if(onboardIndex >= ONBOARD_STEPS.length - 1){ closeOnboard(true); return; }
  onboardIndex++;
  renderOnboard();
}
function onboardPrev(){
  if(onboardIndex > 0){ onboardIndex--; renderOnboard(); }
}
async function maybeShowOnboard(){
  if(!hasSession()) return false;
  let done = false;
  try{
    const j = await liveApi('real_get_ui_state', {}, 8000);
    if(liveOk(j) && j.data && j.data.onboard_done) done = true;
  }catch(e){}
  if(!done){
    try{ done = localStorage.getItem(ONBOARD_KEY) === 'done'; }catch(e){}
  }
  if(done) return false;
  setTimeout(openOnboard, 650);
  return true;
}

/* ---------------- version / build ---------------- */
async function loadAppVersion(){
  const j = await liveApi('app_version', {}, 10000);
  if(!liveOk(j)) return;
  const d = j.data || {};
  const short = 'v' + (d.version || '?');
  ['#sideVersion', '#aboutVersion', '#loginVersion'].forEach(sel => {
    const el = $(sel);
    if(el) el.textContent = short;
  });
  const full = d.label || short;
  if($('#sideVersion')) $('#sideVersion').title = full;
  const build = $('#aboutBuild');
  if(build){
    build.textContent = d.commit
      ? (d.commit + (d.dirty ? '-dirty' : '') +
         (d.built_at ? (' · ' + d.built_at) : '') +
         (d.frozen ? ' · 打包版' : ' · 源码'))
      : (d.frozen ? '打包版（无 git 信息）' : '源码运行（无 git 信息）');
    build.title = full;
  }
}

/* ---------------- auto update ---------------- */
const UPDATE = {data: null, busy: false, downloading: false, applying: false,
                autoNotified: false, package: '', packageVersion: '',
                timer: null};

function bytesLabel(n){
  n = +n || 0;
  if(n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  if(n >= 1024) return Math.round(n / 1024) + ' KB';
  return n + ' B';
}

function updateLabel(d){
  d = d || UPDATE.data || {};
  if(!d.state) return '未检查';
  if(d.state === 'available') return '发现 v' + (d.latest || '新版本');
  if(d.state === 'current') return '已是最新 v' + (d.current || '');
  if(d.state === 'ahead') return '本地版本更新';
  return '检查失败';
}

function renderUpdate(){
  const d = UPDATE.data || {};
  const avail = d.state === 'available';
  const btn = $('#updateBtn');
  if(btn){
    btn.hidden = !avail;
    btn.disabled = UPDATE.busy || UPDATE.downloading || UPDATE.applying;
    btn.classList.toggle('busy', UPDATE.busy || UPDATE.downloading ||
      UPDATE.applying);
    if(avail){
      const tip = '发现新版本 v' + (d.latest || '') + '，点击查看';
      btn.title = tip;
      btn.setAttribute('aria-label', tip);
    }
  }
  const st = $('#updateState');
  if(st){
    st.textContent = updateLabel(d);
    st.title = d.message || '';
    st.style.color = (d.state === 'failed' || d.state === 'ahead')
      ? 'var(--stamp)' : '';
  }
  const chk = $('#updateCheckBtn');
  if(chk){
    chk.disabled = UPDATE.busy;
    chk.textContent = UPDATE.busy ? '检查中…' : '检查更新';
  }
}

async function checkUpdate(opts){
  if(UPDATE.busy) return;
  UPDATE.busy = true;
  renderUpdate();
  try{
    const j = await liveApi('real_check_update', {}, 45000);
    UPDATE.data = liveOk(j)
      ? (j.data || {state: 'failed', message: j.message || ''})
      : {state: 'failed', message: (j && j.message) || '本地服务不可用'};
  }finally{
    UPDATE.busy = false;
    renderUpdate();
  }
  const d = UPDATE.data || {};
  const silent = !!(opts && opts.silent);
  if(d.state === 'available'){
    const line = '发现新版本 v' + d.latest;
    if(silent){
      if(!UPDATE.autoNotified){
        UPDATE.autoNotified = true;
        toast(line + '，点左下角下载图标');
        liveLog(line, (d.notes || []).slice(0, 3).join('；'), 'ok');
      }
    }else{
      toast(line);
      openUpdateDialog();
    }
  }else if(!silent){
    toast(d.state === 'current'
      ? '已是最新版本'
      : (d.message || '检查更新失败'));
  }
}

function openUpdateDialog(){
  const d = UPDATE.data;
  if(!d || d.state !== 'available'){
    checkUpdate({silent: false});
    return;
  }
  $('#updateTitle').textContent = '发现新版本 v' + (d.latest || '');
  const size = d.size ? (' · ' + bytesLabel(d.size)) : '';
  $('#updateSummary').textContent = '当前 v' + (d.current || '?')
    + ' → 最新 v' + (d.latest || '?') + size;
  const ul = $('#updateNotes');
  ul.innerHTML = '';
  const notes = (d.notes || []).slice(0, 12);
  if(notes.length){
    notes.forEach(n => {
      const li = document.createElement('li');
      li.textContent = n;
      ul.appendChild(li);
    });
  }else{
    const li = document.createElement('li');
    li.textContent = '这个版本没有写更新说明。';
    ul.appendChild(li);
  }
  const go = $('#updateGo');
  const downloaded = !!UPDATE.package &&
    UPDATE.packageVersion === (d.latest || '');
  go.textContent = UPDATE.downloading ? '下载中…' : (downloaded ? '立即更新' : '下载');
  go.dataset.done = downloaded ? '1' : '';
  go.disabled = !!UPDATE.downloading;
  if($('#updateCancel')){
    $('#updateCancel').hidden = !UPDATE.downloading;
    $('#updateCancel').disabled = false;
    $('#updateCancel').textContent = '取消下载';
  }
  if($('#updateProgress')) $('#updateProgress').hidden = !UPDATE.downloading;
  if(UPDATE.downloading){
    $('#updateHint').textContent = '正在下载并校验，请保持窗口打开';
    startUpdatePoll();
  }else{
    $('#updateHint').textContent = downloaded
      ? ('已下载并校验：' + UPDATE.package) : '';
  }
  const tip = $('#updateTip');
  tip.hidden = !(downloaded || d.required);
  tip.textContent = downloaded
    ? '更新会关闭程序、自动安装并重启；data 文件夹保留。'
    : (d.required
      ? ('当前 v' + (d.current || '?') + ' 低于最低支持版本 v' + d.min_version +
         '，建议先更新；更新失败不会清空 data。')
      : '');
  if($('#updateLater')) $('#updateLater').textContent = d.required ? '暂不更新' : '稍后';
  $('#updateDlg').hidden = false;
  if(!go.disabled) go.focus();
}

function closeUpdateDialog(){
  const dlg = $('#updateDlg');
  if(dlg) dlg.hidden = true;
}

function stopUpdatePoll(){
  if(UPDATE.timer){ clearInterval(UPDATE.timer); UPDATE.timer = null; }
}
async function checkLastUpdateResult(){
  let j;
  try{ j = await liveApi('real_update_last_result', {}, 10000); }catch(e){ return; }
  if(!liveOk(j) || !j.data || !j.data.state) return;
  const st = j.data;
  if(st.state === 'ok'){
    toast('已更新到 v' + (st.version || '新版本'));
    liveLog('上次自动更新完成', 'v' + (st.version || '') + ' · ' + (st.at || ''), 'ok');
  }else if(st.state === 'failed'){
    toast('上次自动更新失败，已保留旧版本');
    liveLog('上次自动更新失败', st.message || '', 'warn');
  }
  liveApi('real_clear_update_last_result', {}, 8000).catch(() => {});
}
function renderUpdateProgress(st){
  const box = $('#updateProgress');
  if(!box) return;
  const received = +st.received || 0;
  const total = +st.total || 0;
  const pct = total ? Math.min(100, Math.max(0, Math.round(received / total * 100))) : 0;
  box.hidden = false;
  if($('#updateBar')) $('#updateBar').style.width = pct + '%';
  if($('#updatePct')) $('#updatePct').textContent = total
    ? (pct + '% · ' + bytesLabel(received) + ' / ' + bytesLabel(total))
    : (bytesLabel(received));
  if($('#updateSpeed')) $('#updateSpeed').textContent = st.speed
    ? (bytesLabel(st.speed) + '/s') : '';
}
function finishUpdateDownload(st){
  const d = UPDATE.data || {};
  stopUpdatePoll();
  UPDATE.downloading = false;
  UPDATE.package = st.file || '';
  UPDATE.packageVersion = d.latest || '';
  const go = $('#updateGo');
  if(go){
    go.disabled = false;
    go.textContent = '立即更新';
    go.dataset.done = '1';
  }
  if($('#updateCancel')) $('#updateCancel').hidden = true;
  if($('#updateProgress')) $('#updateProgress').hidden = true;
  if($('#updateHint')) $('#updateHint').textContent = '已下载并校验：' + (st.file || '');
  if($('#updateTip')){
    $('#updateTip').hidden = false;
    $('#updateTip').textContent = '更新会关闭程序、自动安装并重启；data 文件夹保留。';
  }
  toast('更新包已下载');
  let via = '';
  if(st.mirror){
    try{ via = new URL(st.mirror).host; }catch(e){ via = st.mirror; }
  }
  liveLog('更新包已下载', (st.file || '') +
    (via ? ' · 线路 ' + via : '') + ' · sha256 已校验', 'ok');
}
async function pollUpdateDownload(){
  const d = UPDATE.data || {};
  let j;
  try{ j = await liveApi('real_update_download_status', {}, 12000); }
  catch(e){ return; }
  if(!liveOk(j)) return;
  const st = j.data || {};
  if(st.state === 'done'){ finishUpdateDownload(st); return; }
  if(st.state === 'failed'){
    stopUpdatePoll();
    UPDATE.downloading = false;
    if($('#updateGo')){ $('#updateGo').disabled = false; $('#updateGo').textContent = '重试下载'; }
    if($('#updateCancel')) $('#updateCancel').hidden = true;
    if($('#updateProgress')) $('#updateProgress').hidden = true;
    if($('#updateHint')) $('#updateHint').textContent = st.message || st.error || '下载失败';
    toast('更新包下载失败');
    return;
  }
  if(st.state === 'cancelled'){
    stopUpdatePoll();
    UPDATE.downloading = false;
    if($('#updateGo')){ $('#updateGo').disabled = false; $('#updateGo').textContent = '继续下载'; }
    if($('#updateCancel')) $('#updateCancel').hidden = true;
    if($('#updateProgress')) $('#updateProgress').hidden = true;
    if($('#updateHint')) $('#updateHint').textContent = '已取消；已下载的部分会保留，可继续';
    return;
  }
  if(st.state === 'cancelling'){
    if($('#updateCancel')){ $('#updateCancel').disabled = true; $('#updateCancel').textContent = '取消中…'; }
    if($('#updateHint')) $('#updateHint').textContent = '正在取消下载…';
    return;
  }
  if(st.state === 'downloading' || st.state === 'probing'){
    if($('#updateHint')) $('#updateHint').textContent = st.state === 'probing'
      ? '正在测速并选择线路…' : '正在下载并校验，请保持窗口打开';
    renderUpdateProgress(st);
  }
}
function startUpdatePoll(){
  stopUpdatePoll();
  UPDATE.timer = setInterval(pollUpdateDownload, 800);
  pollUpdateDownload();
}
async function downloadUpdate(){
  const d = UPDATE.data || {};
  const go = $('#updateGo');
  if(!go) return;
  if(go.dataset.done === '1') return applyUpdate();
  if(!d.url){ toast('清单里没有下载地址'); return; }
  if(UPDATE.downloading) return;
  UPDATE.downloading = true;
  go.disabled = true;
  go.textContent = '下载中…';
  if($('#updateCancel')){
    $('#updateCancel').hidden = false;
    $('#updateCancel').disabled = false;
    $('#updateCancel').textContent = '取消下载';
  }
  if($('#updateHint')) $('#updateHint').textContent = '正在启动下载…';
  try{
    const j = await liveApi('real_start_download_update', {
      url: d.url,
      urls: (d.urls && d.urls.length) ? d.urls : [],
      sha256: d.sha256 || '',
      size: d.size || 0,
      file: d.file || ''
    }, 30000);
    if(!liveOk(j)){
      UPDATE.downloading = false;
      go.disabled = false;
      go.textContent = '重试下载';
      if($('#updateCancel')) $('#updateCancel').hidden = true;
      if($('#updateHint')) $('#updateHint').textContent = (j && j.message) || '下载失败';
      toast('更新包下载失败');
      return;
    }
    startUpdatePoll();
  }catch(e){
    UPDATE.downloading = false;
    go.disabled = false;
    go.textContent = '重试下载';
    if($('#updateCancel')) $('#updateCancel').hidden = true;
    if($('#updateHint')) $('#updateHint').textContent = String(e.message || e);
  }
}
async function cancelUpdateDownload(){
  if(!UPDATE.downloading) return;
  if($('#updateCancel')){ $('#updateCancel').disabled = true; $('#updateCancel').textContent = '取消中…'; }
  try{ await liveApi('real_cancel_update_download', {}, 10000); }catch(e){}
}

async function applyUpdate(){
  if(UPDATE.applying) return;
  if(!UPDATE.package){ toast('先下载更新包'); return; }
  if(UPDATE.packageVersion !== ((UPDATE.data || {}).latest || '')){
    UPDATE.package = '';
    UPDATE.packageVersion = '';
    toast('版本已经变了，请重新下载');
    renderUpdate();
    openUpdateDialog();
    return;
  }
  if(LIVE.submitting){ toast('提交进行中，等结果出来后再更新'); return; }
  if(!confirm('更新会关闭程序并自动重启，确定现在更新？')) return;
  UPDATE.applying = true;
  const go = $('#updateGo');
  go.disabled = true;
  go.textContent = '正在更新…';
  $('#updateHint').textContent = '正在准备安装并重启，请不要关闭窗口';
  try{
    const j = await liveApi('real_apply_update', {
      file: UPDATE.package,
      version: (UPDATE.data || {}).latest || ''
    }, 180000);
    if(!liveOk(j)){
      go.disabled = false;
      go.textContent = '重试更新';
      $('#updateHint').textContent = (j && j.message) || '自动更新失败';
      toast('自动更新失败');
      return;
    }
    $('#updateHint').textContent = '程序即将重启…';
    toast('正在重启，稍等一下');
    liveLog('自动更新已准备', (j.data && j.data.install) || '', 'ok');
    await liveApi('real_quit_app', {}, 8000);
  }finally{
    UPDATE.applying = false;
    if(go.textContent === '正在更新…'){
      go.disabled = false;
      go.textContent = '立即更新';
    }
  }
}

function liveWire(){
  // Closing the window mid-submit can turn a known result into an unknown one.
  window.addEventListener('beforeunload', e => {
    if(!LIVE.submitting) return;
    e.preventDefault();
    e.returnValue = '';
  });
  const loginForm = $('#loginForm');
  if(loginForm){
    loginForm.addEventListener('submit', ev => { ev.preventDefault(); gateLogin(); });
  }
  function setLoginCollapsed(collapsed){
    const card = document.querySelector('.login-card');
    const tape = $('#loginTape'), label = $('#loginTapeLabel');
    if(!card || !tape) return;
    card.classList.toggle('collapsed', !!collapsed);
    tape.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    tape.setAttribute('aria-label', collapsed ? '展开登录框' : '收起登录框');
    if(label) label.textContent = collapsed ? '登录' : '收起';
    if(!collapsed){
      setTimeout(() => { const u = $('#loginUser'); if(u) u.focus(); }, 120);
    }
  }
  if($('#loginTape')) $('#loginTape').addEventListener('click', () => {
    const card = document.querySelector('.login-card');
    setLoginCollapsed(!(card && card.classList.contains('collapsed')));
  });
  const manualBtn = $('#loginManual');
  if(manualBtn) manualBtn.addEventListener('click', openManualCaptcha);
  const gt4Close = $('#gt4Close');
  if(gt4Close) gt4Close.addEventListener('click', closeManualCaptcha);
  const gt4Retry = $('#gt4Retry');
  if(gt4Retry) gt4Retry.addEventListener('click', () => {
    if(GT4.busy) return;
    gt4Reset();
    setGt4Status('正在重新读取题目…', 'busy');
    gt4LoadChallenge();
  });
  const gt4Track = $('#gt4Track');
  if(gt4Track){
    gt4Track.addEventListener('pointerdown', gt4PointerDown);
    gt4Track.addEventListener('pointermove', gt4PointerMove);
    gt4Track.addEventListener('pointerup', gt4PointerUp);
    gt4Track.addEventListener('pointercancel', gt4PointerUp);
    gt4Track.addEventListener('keydown', gt4KeyDown);
  }
  window.addEventListener('resize', () => {
    const modal = $('#gt4Modal');
    if(modal && !modal.hidden) gt4Layout();
  });
  const gt4Modal = $('#gt4Modal');
  if(gt4Modal){
    gt4Modal.addEventListener('click', ev => {
      if(ev.target === gt4Modal) closeManualCaptcha();
    });
  }
  document.addEventListener('keydown', ev => {
    if(ev.key === 'Escape' && gt4Modal && !gt4Modal.hidden) closeManualCaptcha();
  });
  const loginUser = $('#loginUser');
  if(loginUser){
    loginUser.addEventListener('keydown', e => {
      // Enter in the phone field means “next”, not “submit with no password”.
      if(e.key === 'Enter'){
        e.preventDefault();
        const pass = $('#loginPass');
        if(pass) pass.focus();
      }
    });
  }
  const loginEye = $('#loginEye');
  if(loginEye){
    loginEye.addEventListener('click', () => {
      const pass = $('#loginPass');
      if(!pass) return;
      const show = pass.type === 'password';
      const start = pass.selectionStart, end = pass.selectionEnd;
      pass.type = show ? 'text' : 'password';
      loginEye.textContent = show ? '隐藏' : '显示';
      loginEye.setAttribute('aria-pressed', show ? 'true' : 'false');
      loginEye.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
      pass.focus();
      try{ pass.setSelectionRange(start, end); }catch(e){}
    });
  }
  ['#loginUser', '#loginPass'].forEach(sel => {
    const el = $(sel);
    if(!el) return;
    el.addEventListener('input', () => {
      const err = $('#loginError');
      if(err){ err.hidden = true; err.textContent = ''; }
      if($('#loginStatus') && $('#loginStatus').classList.contains('error')){
        setLoginStatus('请输入手机号和密码登录。', '');
      }
    });
  });
  const rememberBox = $('#loginRemember');
  if(rememberBox){
    rememberBox.addEventListener('change', () => {
      // Unchecking is an explicit "forget it now", not just a preference for
      // the next successful login.
      if(!rememberBox.checked) forgetSavedLogin();
    });
  }
  const loginAgree = $('#loginAgree');
  // `click` only: autofill/restoration can fire `change` without a human.
  if(loginAgree) loginAgree.addEventListener('click', toggleDisclaimerAgree);
  ['#disclaimerOpen', '#disclaimerOpenAbout'].forEach(sel => {
    const el = $(sel);
    if(el) el.addEventListener('click', openDisclaimer);
  });
  if($('#disclaimerClose'))
    $('#disclaimerClose').addEventListener('click', closeDisclaimer);
  if($('#disclaimerVeil'))
    $('#disclaimerVeil').addEventListener('click', closeDisclaimer);
  if($('#disclaimerAgree'))
    $('#disclaimerAgree').addEventListener('click', agreeDisclaimerFromModal);
  document.addEventListener('keydown', ev => {
    const modal = $('#disclaimerModal');
    if(!modal || modal.hidden) return;
    if(ev.key === 'Escape'){
      ev.preventDefault();
      closeDisclaimer();
      return;
    }
    if(ev.key !== 'Tab') return;
    const focusables = modal.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    if(!focusables.length) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if(ev.shiftKey && document.activeElement === first){
      ev.preventDefault(); last.focus();
    }else if(!ev.shiftKey && document.activeElement === last){
      ev.preventDefault(); first.focus();
    }
  });
  $('#ovRefresh').addEventListener('click', async () => {
    const btn = $('#ovRefresh');
    if(btn.disabled) return;
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '刷新中…';
    try{ await loadOverview(); }
    finally{ btn.disabled = false; btn.textContent = old; }
  });
  $('#ovPlanUse').addEventListener('click', () => {
    const slots = (LIVE.plan && LIVE.plan.slots) || [];
    const slot = nextPlanSlot(slots);
    if(!slot){ toast('暂时没有可用计划'); return; }
    applyPlanSlot(slot, {tab: 'params'});
  });
  $('#accLogin').addEventListener('click', liveLogin);
  ['#accUser', '#accPass'].forEach(sel => {
    const el = $(sel);
    if(!el) return;
    el.addEventListener('keydown', e => {
      if(e.key !== 'Enter') return;
      e.preventDefault();
      if(sel === '#accUser'){
        // Enter in the phone field moves to the password unless the browser
        // already filled it (password managers do).
        const pass = $('#accPass');
        if(pass && !pass.value){ pass.focus(); return; }
      }
      liveLogin();
    });
  });
  $('#accTest').addEventListener('click', liveTestSession);
  $('#accLogout').addEventListener('click', liveLogout);
  if($('#deviceRotateBtn')) $('#deviceRotateBtn').addEventListener('click', rotateDeviceProfile);
  if($('#customDevicePlatform')) $('#customDevicePlatform').addEventListener('change', fillCustomDeviceModels);
  if($('#customDeviceModel')) $('#customDeviceModel').addEventListener('change', fillCustomDeviceOs);
  ['#customDeviceConfirm', '#customDeviceId'].forEach(sel => {
    const el = $(sel);
    if(el) el.addEventListener('input', updateCustomSaveState);
  });
  if($('#customDeviceSave')) $('#customDeviceSave').addEventListener('click', saveCustomDevice);
  $('#kaStart').addEventListener('click', liveKeepaliveStart);
  $('#kaCheck').addEventListener('click', liveKeepaliveCheck);
  $('#kaStop').addEventListener('click', liveKeepaliveStop);
  $('#fetchPoints').addEventListener('click', () => loadPoints(true));
  if($('#setCampusBtn')) $('#setCampusBtn').addEventListener('click', setCampusFromMap);
  if($('#runAreaSel')) $('#runAreaSel').addEventListener('change', e => {
    const raw = e.target.value;
    if(raw === ''){
      // Back to the server's automatic match by the account's campus name.
      // Persist the removal so the next login does not restore the manual id.
      LIVE.runAreaChoice = null;
      LIVE.runAreaChoiceKey = '';
      const key = runAreaSessionKey();
      liveApi('real_set_ui_state', {
        run_area_choice: {[key]: null}
      }, 5000).catch(() => {});
      renderRunAreas();
      reloadRunArea('已恢复自动匹配：按当前账号校区选择围栏和打卡点');
      return;
    }
    const id = +raw;
    if(!isFinite(id)) return;
    LIVE.runAreaChoice = id;
    LIVE.runAreaChoiceKey = runAreaSessionKey();
    liveApi('real_set_ui_state', {
      run_area_choice: {[LIVE.runAreaChoiceKey]: id}
    }, 5000).catch(() => {});
    reloadRunArea('正在重新读取该跑区的围栏和打卡点');
  });
  $('#freeRun').addEventListener('change', () => {
    if(freeRunOn()) liveLog('已勾选跳过打卡点',
      '提交时不拉取打卡点；学校要求必经点会被后端拒绝。注意：这不是官方自由跑（serviceType=3）', 'warn');
    renderPoints(livePoints, {fit: false});
    updateFenceChip();
    refreshChecks(); scheduleModelPreview();
  });
  $('#preflightBtn').addEventListener('click', runPreflight);
  $('#receiptRefresh').addEventListener('click', async () => {
    const btn = $('#receiptRefresh');
    if(btn.disabled) return;
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '刷新中…';
    try{ await loadReceipts(); }
    finally{ btn.disabled = false; btn.textContent = old; }
  });
  $('#receiptReconcile').addEventListener('click', reconcileReceipts);
  if($('#submitResultRecords')){
    $('#submitResultRecords').addEventListener('click', async () => {
      go('records');
      if(LIVE.submitResult && LIVE.submitResult.state === 'unknown' &&
         !LIVE.submitResult.checked){
        await reconcileReceipts();
      }
    });
  }
  if($('#submitResultDismiss')){
    $('#submitResultDismiss').addEventListener('click', clearSubmitResult);
  }
  $('#routeImport').addEventListener('click', importRouteToLibrary);
  $('#routeExport').addEventListener('click', () => exportRouteGpx(LIVE.routes[LIVE.selectedRoute]));
  $('#routeExportOne').addEventListener('click', () => exportRouteGpx(LIVE.routes[LIVE.selectedRoute]));
  $('#rdGpx').addEventListener('click', () => {
    const d = LIVE.lastDetail;
    if(d) downloadGpx('记录 ' + shortDate(d.detail && d.detail.start_ms), d.track || []);
  });
  $('#rdSaveRoute').addEventListener('click', saveDetailAsRoute);
  if($('#rdCharts')) $('#rdCharts').addEventListener('click', openRecordDataPreview);
  $('#dpClose').addEventListener('click', cancelDataPreview);
  $('#dpCancel').addEventListener('click', cancelDataPreview);
  $('#dataPreviewVeil').addEventListener('click', cancelDataPreview);
  $('#dpConfirm').addEventListener('click', () => {
    const pending = PENDING_SUBMIT;
    if(!pending) return;
    closeDataPreview();
    doSubmitRun(pending);
  });
  $('#postClose').addEventListener('click', postKeepRoute);
  $('#postKeep').addEventListener('click', postKeepRoute);
  $('#postClear').addEventListener('click', postClearRoute);
  $('#postSaveClear').addEventListener('click', postSaveAndClear);
  $('#postSubmitVeil').addEventListener('click', postKeepRoute);
  $('#diagCopy').addEventListener('click', copyDiagnostics);
  document.addEventListener('keydown', ev => {
    if(ev.key !== 'Escape') return;
    if($('#dataPreview') && !$('#dataPreview').hidden){ ev.preventDefault(); cancelDataPreview(); }
    else if($('#postSubmit') && !$('#postSubmit').hidden){ ev.preventDefault(); postKeepRoute(); }
  });
  if($('#onboard')){
    $('#onboardClose').addEventListener('click', () => closeOnboard(true));
    $('#onboardSkip').addEventListener('click', () => closeOnboard(null));
    $('#onboardNext').addEventListener('click', onboardNext);
    $('#onboardPrev').addEventListener('click', onboardPrev);
    $('#onboardVeil').addEventListener('click', () => closeOnboard(true));
    $('#onboardOpen').addEventListener('click', openOnboard);
    if($('#coverPick')) $('#coverPick').addEventListener('click', () => $('#coverFile').click());
    if($('#coverFile')) $('#coverFile').addEventListener('change', ev => {
      const f = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if(f) pickLoginCover(f);
    });
    if($('#coverReset')) $('#coverReset').addEventListener('click', resetLoginCover);
    document.addEventListener('keydown', ev => {
      if($('#onboard').hidden) return;
      if(ev.key === 'Escape'){ ev.preventDefault(); closeOnboard(true); }
      else if(ev.key === 'ArrowRight'){ ev.preventDefault(); onboardNext(); }
      else if(ev.key === 'ArrowLeft'){ ev.preventDefault(); onboardPrev(); }
      else if(ev.key === 'Tab'){
        const focusables = [...$('#onboard').querySelectorAll(
          'button:not([disabled]), input:not([disabled]), select, a[href]')];
        if(!focusables.length) return;
        const first = focusables[0], last = focusables[focusables.length - 1];
        if(ev.shiftKey && document.activeElement === first){
          ev.preventDefault(); last.focus();
        }else if(!ev.shiftKey && document.activeElement === last){
          ev.preventDefault(); first.focus();
        }
      }
    });
  }
  if(window.Announcements) Announcements.wire();
  const updateBtn = $('#updateBtn');
  if(updateBtn){
    updateBtn.addEventListener('click', () => {
      if(UPDATE.data && UPDATE.data.state === 'available') openUpdateDialog();
      else checkUpdate({silent: false});
    });
  }
  ['#updateClose', '#updateLater'].forEach(sel => {
    const el = $(sel);
    if(el) el.addEventListener('click', closeUpdateDialog);
  });
  if($('#updateVeil')) $('#updateVeil').addEventListener('click', closeUpdateDialog);
  if($('#updateCancel')) $('#updateCancel').addEventListener('click', cancelUpdateDownload);
  if($('#updateGo')) $('#updateGo').addEventListener('click', downloadUpdate);
  if($('#updateCheckBtn')){
    $('#updateCheckBtn').addEventListener('click', () => checkUpdate({silent: false}));
  }
  document.addEventListener('keydown', ev => {
    const dlg = $('#updateDlg');
    if(!dlg || dlg.hidden) return;
    if(ev.key === 'Escape'){ ev.preventDefault(); closeUpdateDialog(); }
  });
  $('#ovPlanGrid').addEventListener('click', ev => {
    const btn = ev.target.closest('[data-plan-day]');
    if(!btn || btn.disabled) return;
    const day = btn.dataset.planDay;
    const slot = ((LIVE.plan && LIVE.plan.slots) || []).find(s => s && s.day === day);
    if(!slot){ toast('这个日期没有可用档位'); return; }
    applyPlanSlot(slot, {tab: 'params'});
  });
  $('#snapBtn').addEventListener('click', async () => {
    if(snapping) return;
    if(!route || route.length < 2){ toast('先画至少 2 个点'); return; }
    const btn = $('#snapBtn'), tool = $('#snapTool');
    const oldText = btn.textContent;
    snapping = true;
    btn.disabled = true; btn.textContent = '吸附中…';
    if(tool) tool.disabled = true;
    liveLog('道路吸附开始', route.length + ' 个控制点，请稍候', 'info');
    try{
      const j = await liveApi('real_snap_route', {points: route, key: null, max_legs: 24});
      if(!liveOk(j)){ toast(j.message || '吸附失败'); liveLog('吸附失败', j.message || '', 'warn'); return; }
      const d = j.data || {};
      if(d.route && d.route.length){
        pushRouteHistory('道路吸附');
        selRoutePoint = -1;
        route = d.route.map(p => [p[0], p[1]]);
        redraw();
        scheduleRouteFinalize(500, 'snap');
      }
      const failed = (d.failed || []).length;
      liveLog('道路吸附完成', `${d.legs_ok || 0}/${d.legs_total || 0} 段` +
        (failed ? ` · ${failed} 段失败` : ''), failed ? 'warn' : 'ok');
      toast(failed ? `吸附完成，${failed} 段失败` : '吸附完成');
    }finally{
      snapping = false;
      btn.disabled = false; btn.textContent = oldText || '吸附';
      if(tool) tool.disabled = false;
    }
  });
  $('#planFill').addEventListener('click', async () => {
    // Same horizon as the overview grid: never suggest a day past the
    // semester deadline.
    const days = planHorizonDays(LIVE.semester && LIVE.semester.view);
    if(days === 0){ toast('本学期已结束，不再生成计划'); return; }
    const j = await liveApi('real_plan', {days: days});
    if(!liveOk(j)){ toast(j.message || '计划生成失败'); return; }
    const slot = nextPlanSlot((j.data && j.data.slots) || []);
    if(!slot){ toast('计划为空'); return; }
    applyPlanSlot(slot, {tab: 'params'});
  });
  $('#fillRouteDistance').addEventListener('click', () => {
    distanceManual = false;
    const m = route && route.length >= 2 ? pathLength(displayPath()) : 0;
    if(!(m > 0)){ toast('先画一条路线；之后会自动按线路长度填写'); return; }
    redraw();
    scheduleRouteFinalize(300, 'fill-route');
    toast('已按线路长度填写；继续画线会同步更新');
  });
  $('#runCheck').addEventListener('click', () => { refreshModelPreview(); scheduleSplitsPreview(); refreshChecks(); toast('已重新检查'); });
  if($('#submitRun')) $('#submitRun').addEventListener('click', submitRun);
  $('#splitsToggle').addEventListener('click', () => {
    setSplitsManual(!splitsManual);
    if(splitsManual) toast('已进入手动分段；改数字或点 ± 调整');
  });
  $('#saveRules').addEventListener('click', saveRules);
  $('#saveSemesterGoal').addEventListener('click', saveSemesterGoal);
  ['rSemesterKm'].forEach(id => $('#' + id).addEventListener('keydown', e => {
    if(e.key === 'Enter'){ e.preventDefault(); saveSemesterGoal(); }
  }));
  $('#rulesFetch').addEventListener('click', () => fetchSchoolRules({force: true}));
  if($('#addWindow')) $('#addWindow').addEventListener('click', () => {
    const wrap = $('#windowRows');
    if(wrap && wrap.querySelector('.window-empty')) wrap.innerHTML = '';
    addWindowRow('', '');
    const rows = wrap ? wrap.querySelectorAll('.window-row') : [];
    const last = rows[rows.length - 1];
    if(last) last.querySelector('.window-start').focus();
  });
  $('#netSave').addEventListener('click', saveNetworkConfig);
  $('#netClear').addEventListener('click', clearNetworkConfig);
  ['inKm', 'inPace', 'inCad', 'inAlt'].forEach(id => $('#' + id).addEventListener('input', () => {
    // A new distance or average pace invalidates the split table; the next
    // preview regenerates it from the new target.
    if(id === 'inKm' || id === 'inPace'){
      splitsManual = false; splitRows = null; splitAutoRows = []; renderSplits();
    }
    updateRunDateLabel();
    refreshChecks();
    scheduleModelPreview();
  }));
  if($('#splitsList')){
    $('#splitsList').addEventListener('change', ev => {
      const inp = ev.target.closest('.split-input');
      if(!inp) return;
      const i = +inp.dataset.split;
      const sec = splitParse(inp.value);
      if(sec === null || sec <= 0){
        toast('用时格式：6:03 或 6.05（分钟）');
        renderSplits();
        return;
      }
      splitRows = (splitRows || []).map(r => Object.assign({}, r));
      splitRows[i].seconds = sec;
      applySplitsToPace();
    });
    $('#splitsList').addEventListener('click', ev => {
      const btn = ev.target.closest('.split-step');
      if(!btn) return;
      const i = +btn.dataset.split;
      const step = +btn.dataset.splitStep || 0;
      if(!splitRows || !splitRows[i]) return;
      const next = Math.max(30, Math.min(3600, (+splitRows[i].seconds || 0) + step));
      splitRows = splitRows.map(r => Object.assign({}, r));
      splitRows[i].seconds = next;
      applySplitsToPace();
    });
    if($('#splitsReset')) $('#splitsReset').addEventListener('click', () => {
      setSplitsManual(false);
      toast('已恢复自动分配');
    });
  }
  if($('#altMode')){
    $('#altMode').addEventListener('change', altModeChanged);
    document.querySelectorAll('[data-alt-mode]').forEach(btn => {
      btn.addEventListener('click', () => {
        $('#altMode').value = btn.dataset.altMode;
        altAdjustOpen = false;
        altModeChanged();
      });
    });
    if($('#altAdjustToggle')) $('#altAdjustToggle').addEventListener('click', () => {
      altAdjustOpen = !altAdjustOpen;
      altModeChanged();
    });
    ['altMin', 'altMax'].forEach(id => $('#' + id).addEventListener('input', () => {
      updateAltSummary(); scheduleModelPreview(); refreshChecks();
    }));
    document.querySelectorAll('[data-alt-range]').forEach(btn => {
      btn.addEventListener('click', () => {
        const [lo, hi] = String(btn.dataset.altRange).split(',').map(Number);
        if($('#altMin')) $('#altMin').value = lo;
        if($('#altMax')) $('#altMax').value = hi;
        updateAltSummary(); scheduleModelPreview(); refreshChecks();
      });
    });
    $('#inAlt').addEventListener('input', () => {
      updateAltSummary(); scheduleModelPreview(); refreshChecks();
    });
    $('#altTemplateBtn').addEventListener('click', () => $('#altTemplateFile').click());
    $('#altTemplateFile').addEventListener('change', ev => {
      const f = ev.target.files && ev.target.files[0];
      loadAltitudeTemplate(f);
      ev.target.value = '';
    });
    altModeChanged();
    window.addEventListener('resize', () => requestAnimationFrame(syncAltThumb));
  }
  ['inStart'].forEach(id => $('#' + id).addEventListener('input', () => { updateRunDateLabel(); scheduleModelPreview(); refreshChecks(); }));
  // The six-page UI uses the date popover (#dateBtn/#datePop), not the old
  // #inDay select.  Calling wireDatePicker() here is required; otherwise the
  // button is inert and the boot code below used to throw on a null #inDay.
  wireDatePicker();
  $('#routeSaveCurrent').addEventListener('click', saveCurrentRoute);
  $('#routeUse').addEventListener('click', () => {
    const r = LIVE.routes[LIVE.selectedRoute];
    if(!r) return;
    const line = validRouteLine(r.line);
    if(line.length < 2){ toast('这条路线没有有效坐标'); return; }
    pushRouteHistory('载入路线库：' + (r.name || '未命名'));
    selRoutePoint = -1;
    route = line;
    setCloseUI(!!r.closed);
    redraw();
    scheduleRouteFinalize(300, 'library');
    go('run');
    switchRunTab('route');
    toast('已设为当前路线');
  });
  $('#routeDelete').addEventListener('click', deleteSelectedRoute);
  document.querySelectorAll('[data-route-filter]').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('[data-route-filter]').forEach(x => x.classList.remove('active'));
    b.classList.add('active'); LIVE.routeFilter = b.dataset.routeFilter; renderRouteList();
  }));
  $('#recRefresh').addEventListener('click', async () => {
    const btn = $('#recRefresh');
    if(btn.disabled) return;
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '刷新中…';
    try{ await loadRecords(); }
    finally{ btn.disabled = false; btn.textContent = old; }
  });
  $('#profRefresh').addEventListener('click', () => loadProfile());
  $('#violCheck').addEventListener('click', loadViolations);
  $('#recExport').addEventListener('click', exportRecordsCsv);
  $('#recordTable').addEventListener('click', ev => {
    const row = ev.target.closest('[data-rec-index]');
    if(row) selectRecord(+row.dataset.recIndex);
  });
  $('#recordTable').addEventListener('keydown', ev => {
    const row = ev.target.closest('[data-rec-index]');
    if(!row) return;
    if(ev.key === 'Enter' || ev.key === ' '){
      ev.preventDefault();
      selectRecord(+row.dataset.recIndex);
    }
  });
  document.querySelectorAll('[data-rec-filter]').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('[data-rec-filter]').forEach(x => x.classList.remove('active'));
    b.classList.add('active'); LIVE.recordFilter = b.dataset.recFilter; renderRecords();
  }));
  $('#rdCopy').addEventListener('click', async () => {
    const v = $('#rdCopy').dataset.rrid || '';
    if(!v) return;
    const ok = await copyPlainText(String(v));
    toast(ok ? '已复制 rrid' : '复制失败，请手动选择');
  });
  document.querySelectorAll('.log-filter').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('.log-filter').forEach(x => x.classList.remove('active'));
    b.classList.add('active'); renderLog();
  }));
  $('#logCopy').addEventListener('click', async () => {
    if(!LIVE.log.length){ toast('日志为空'); return; }
    const txt = LIVE.log.map(e => `[${e.time}] ${e.level.toUpperCase()} ${e.msg} ${e.detail}`).join(String.fromCharCode(10));
    const ok = await copyPlainText(txt);
    toast(ok ? '日志已复制' : '复制失败，请手动选择');
  });
  $('#logExport').addEventListener('click', () => {
    if(!LIVE.log.length){ toast('日志为空'); return; }
    const txt = LIVE.log.map(e => `[${e.time}] ${e.level.toUpperCase()} ${e.msg} ${e.detail}`).join(String.fromCharCode(10));
    saveBlob(new Blob([txt], {type: 'text/plain;charset=utf-8'}), 'haoxiangpao-log.txt');
  });
  $('#logClear').addEventListener('click', () => {
    if(!LIVE.log.length) return;
    if(!confirm('清空本次运行日志？')) return;
    LIVE.log = []; LIVE.logSel = -1;
    if($('#logExplainBox')) $('#logExplainBox').hidden = true;
    if($('#logExplain')) $('#logExplain').disabled = true;
    renderLog();
  });
}

/* v0.1.8: every new control is bound here, next to liveWire's own handlers.
   Each one only acts on a click; nothing below runs on page load or a timer. */
function featureWire(){
  // settings: appearance skins
  wireSkinCards();
  // If /theme-boot.js was blocked, fall back to the last local choice.  When
  // the server route worked it marks the source, so a stale localStorage value
  // never overrides the authoritative data/ui_state.json.
  if(document.documentElement.getAttribute('data-skin-source') !== 'server'){
    try{
      const cached = normalizeSkin(localStorage.getItem(SKIN_KEY));
      if(cached && cached !== currentSkin()) applySkin(cached, {save: false, refresh: false});
    }catch(e){}
  }
  renderSkinCards();
  if($('#windowRows') && !$('#windowRows').children.length) renderWindowRows([]);

  // settings: OBS key + realism
  if($('#setRealismSave')) $('#setRealismSave').addEventListener('click', saveRealism);
  if($('#setRealismReset')) $('#setRealismReset').addEventListener('click', resetRealismForm);
  if($('#diagZip')) $('#diagZip').addEventListener('click', ev => downloadDiagZip(ev.currentTarget));
  if($('#recDiagZip')) $('#recDiagZip').addEventListener('click', ev => downloadDiagZip(ev.currentTarget));

  // overview: inspection + error note
  if($('#inspectRefresh')) $('#inspectRefresh').addEventListener('click', runInspection);
  if($('#ovErrorNote')){
    $('#ovErrorNote').addEventListener('click', () => { $('#ovErrorNote').hidden = true; });
  }

  // run page: presets
  if($('#presetSel')) $('#presetSel').addEventListener('change', renderPresets);
  if($('#presetApply')) $('#presetApply').addEventListener('click', applyPreset);
  if($('#presetSave')) $('#presetSave').addEventListener('click', savePreset);
  if($('#presetDelete')) $('#presetDelete').addEventListener('click', deletePreset);
  if($('#presetName')) $('#presetName').addEventListener('keydown', ev => {
    if(ev.key === 'Enter'){ ev.preventDefault(); savePreset(); }
  });

  // submit page: audit + verify
  if($('#auditBtn')) $('#auditBtn').addEventListener('click', runAudit);
  if($('#auditCopy')) $('#auditCopy').addEventListener('click', copyAuditJson);
  if($('#auditToggle')) $('#auditToggle').addEventListener('click', () => {
    LIVE.auditShowAll = !LIVE.auditShowAll;
    renderAudit();
  });
  if($('#submitResultVerify')) $('#submitResultVerify').addEventListener('click', () => {
    const r = LIVE.submitResult;
    if(!r) return;
    verifyRecord({rrid: r.rrid || null, uuid: r.uuid || null, via: 'submit'});
  });

  // log: explain the selected entry
  if($('#logEvents')){
    $('#logEvents').addEventListener('click', ev => {
      const li = ev.target.closest('[data-log-index]');
      selectLogEntry(li ? +li.dataset.logIndex : -1);
    });
  }
  if($('#logExplain')) $('#logExplain').addEventListener('click', explainSelectedLog);

  // records: stats, FIT, verify
  if($('#recStats')) $('#recStats').addEventListener('click', () => {
    const panel = $('#statsPanel');
    const open = panel && panel.hidden;
    if(panel) panel.hidden = !open;
    if(open) loadStats();
  });
  if($('#statsClose')) $('#statsClose').addEventListener('click', () => {
    if($('#statsPanel')) $('#statsPanel').hidden = true;
  });
  if($('#statsRun')) $('#statsRun').addEventListener('click', loadStats);
  if($('#statsWeeks')) $('#statsWeeks').addEventListener('change', () => {
    if($('#statsPanel') && !$('#statsPanel').hidden) loadStats();
  });
  if($('#recFit')) $('#recFit').addEventListener('click', ev => {
    const d = LIVE.lastDetail;
    if(!d || !d.detail){ toast('先在右侧选一条记录'); return; }
    exportFit(d.detail, ev.currentTarget);
  });
  if($('#rdFit')) $('#rdFit').addEventListener('click', ev => {
    const d = LIVE.lastDetail;
    if(!d || !d.detail){ toast('先选一条记录'); return; }
    exportFit(d.detail, ev.currentTarget);
  });
  if($('#rdVerify')) $('#rdVerify').addEventListener('click', () => {
    const x = (LIVE.lastDetail && LIVE.lastDetail.detail) || {};
    if(!x.rrid && !x.uuid){ toast('这条记录没有 rrid，无法核验'); return; }
    verifyRecord({rrid: x.rrid || null, uuid: x.uuid || null, via: 'detail'});
  });
  if($('#rdReplay')) $('#rdReplay').addEventListener('click', openRecordReplay);

  // replay modal
  if($('#replayClose')) $('#replayClose').addEventListener('click', replayClose);
  if($('#replayVeil')) $('#replayVeil').addEventListener('click', replayClose);
  if($('#replayPlay')) $('#replayPlay').addEventListener('click', replayToggle);
  if($('#replayRestart')) $('#replayRestart').addEventListener('click', () => {
    REPLAY.playing = false;
    if(REPLAY.raf) cancelAnimationFrame(REPLAY.raf);
    REPLAY.raf = 0;
    REPLAY.t = 0;
    if($('#replayPlay')) $('#replayPlay').textContent = '播放';
    replayDraw();
  });
  if($('#replayScrub')) $('#replayScrub').addEventListener('input', ev => {
    if(!LIVE.replay) return;
    // Scrubbing pauses: a drag fighting the animation jumps backwards.
    REPLAY.playing = false;
    if(REPLAY.raf) cancelAnimationFrame(REPLAY.raf);
    REPLAY.raf = 0;
    if($('#replayPlay')) $('#replayPlay').textContent = '继续';
    REPLAY.t = LIVE.replay.duration * (+ev.target.value || 0) / 1000;
    replayDraw();
  });
  if($('#replaySpeed')) $('#replaySpeed').addEventListener('change', ev => {
    REPLAY.speed = +ev.target.value || 4;
  });
  window.addEventListener('resize', () => {
    if(LIVE.replay && $('#replayModal') && !$('#replayModal').hidden) replayDraw();
  });
  document.addEventListener('keydown', ev => {
    if(!$('#replayModal') || $('#replayModal').hidden) return;
    if(ev.key === 'Escape'){ ev.preventDefault(); replayClose(); }
    else if(ev.key === ' ' && !(eventTargetEl(ev) && eventTargetEl(ev).closest('input,select'))){
      ev.preventDefault(); replayToggle();
    }
  });
  renderAudit();
  renderPresets();
}

/* Boot: login gate -> local data -> real session. */
initRunDate();
liveWire();
featureWire();
// Realism is local state, not per-page: load it before the first preview so a
// saved pause/GPS/terrain profile is actually used after a restart.
loadRealism();
loadAppVersion();
loadLoginCover();
setTimeout(() => checkUpdate({silent: true}), 1500);
updateRunDateLabel();
renderLog();
loadRoutes();
go(PAGES.includes(startPage) ? startPage : 'overview');
refreshDateHeader();
bootAuth();

/* splash.js dispatches this after removing the splash.  Release the queued
   boot toasts (the window is usable now) and hand focus to the login form if
   it is the visible surface. */
document.addEventListener('hxp:boot-done', () => {
  flushBootToasts();
  if(LOGIN.visible && !LOGIN.busy){
    const u = $('#loginUser');
    if(u) u.focus();
  }
});

/* The onboarding already traps Tab itself; the preview, post-submit and
   update dialogs need the same treatment so Tab cannot reach the workbench
   behind them. */
document.addEventListener('keydown', ev => {
  if(ev.key !== 'Tab') return;
  for(const [dlgSel, cardSel] of [
    ['#dataPreview', '.data-preview-card'],
    ['#postSubmit', '.post-card'],
    ['#replayModal', '.replay-card'],
    ['#updateDlg', '.onboard-card']
  ]){
    const dlg = $(dlgSel);
    if(!dlg || dlg.hidden) continue;
    const card = dlg.querySelector(cardSel);
    if(card) trapTabIn(card, ev);
    return;
  }
});

// Local file status only; this does not probe the real server.  It keeps the
// account card in sync when the background keeper auto-relogins.
setInterval(() => {
  if(document.visibilityState !== 'hidden') loadKeepalive();
  refreshDateHeader();
  // Keep the "today, before now" slot valid while the page stays open.
  const active = document.querySelector('.page.active');
  if(active && active.id === 'page-run' && !LIVE.submitting){
    updateRunDateLabel();
    refreshChecks();
  }
}, 60000);
