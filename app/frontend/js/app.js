/* ---------- tiny helpers ---------- */
const $ = s => document.querySelector(s);
const toastEl = $('#toast');
let toastTimer = null;
function toast(msg){
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2600);
}
/* one delegated handler, so links rebuilt by the checklist keep working */
document.addEventListener('click', ev => {
  const el = ev.target.closest('[data-toast]');
  if(!el) return;
  if(el.tagName === 'A') ev.preventDefault();
  toast(el.dataset.toast);
});

/* Request/data sequence counters.  Declared before the first top-level call
   (redraw -> scheduleModelPreview) so no let/const is read in its TDZ. */
let _pointsSeq = 0, _recordsSeq = 0, _overviewSeq = 0;
let _receiptsSeq = 0, _dateCountsSeq = 0, _detailSeq = 0, _modelSeq = 0;
let _profileSeq = 0, _violSeq = 0;
let _headerDay = '', _modelTimer = null, accountBusy = false;
let distanceManual = false, lastRouteLength = 0;
function invalidateLiveRequests(){
  // Bump every request sequence so replies that belong to a previous account
  // (or a previous logout) cannot repopulate the UI after the switch.
  _pointsSeq++; _recordsSeq++; _overviewSeq++;
  _receiptsSeq++; _dateCountsSeq++; _detailSeq++; _modelSeq++;
  _profileSeq++; _violSeq++;
}

/* ---------- map ---------- */
const CENTER = [22.4108, 112.9408];
const map = L.map('map', {zoomControl: false, attributionControl: false, doubleClickZoom: false})
  .setView(CENTER, 16);

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
  color: '#B6483C', weight: 1.8, dashArray: '6 5',
  fillColor: '#B6483C', fillOpacity: .035
}).addTo(map).bindTooltip('跑步围栏', {sticky: true, className: 'pt-tip'});
let fenceBounds = L.latLngBounds([]);
const pointLayer = L.layerGroup().addTo(map);
/* kept name for the existing route code; it contains only real checkpoints */
let livePoints = [];

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
      badge.textContent = '自由跑 · 不查点位';
      badge.className = 'tag warn';
    }else if(livePoints.length){
      badge.textContent = livePoints.length + ' 个 · ' + must + ' 必经';
      badge.className = 'tag ok';
    }else{
      badge.textContent = '未加载点位';
      badge.className = 'tag warn';
    }
  }
  if(list && !livePoints.length){
    list.innerHTML = '<div class="empty-state">' + (freeRunOn()
      ? '自由跑模式：提交时不拉取打卡点。'
      : (hasSession()
        ? '服务器没有返回打卡点；点「刷新」重试。'
        : '登录后从服务器拉取打卡点。')) + '</div>';
  }
  if(livePoints.length && opts.fit !== false){
    const b = L.latLngBounds(livePoints.map(p => [p.lat, p.lng]));
    map.fitBounds(b.pad(.18), runMapPadding());
  }
}
function setFence(polygon, name, opts){
  const pts = (Array.isArray(polygon) ? polygon : [])
    .map(p => [p && (p.lat ?? p.glat), p && (p.lon ?? p.glon)])
    .filter(p => isFinite(p[0]) && isFinite(p[1]));
  fenceLayer.setLatLngs(pts);
  fenceLayer.setTooltipContent(esc(name || '跑步围栏') + (pts.length ? '' : '（未加载）'));
  fenceBounds = L.latLngBounds(pts);
  try{ routeFenceLayer.setLatLngs(pts); }catch(e){}
  if(pts.length && (!opts || opts.fit !== false)) map.fitBounds(fenceBounds.pad(.18), runMapPadding());
}

let route = [];

const hi = L.polyline([], {color: '#F2D25C', weight: 15, opacity: .5, lineCap: 'round', lineJoin: 'round'}).addTo(map);
const ink = L.polyline([], {color: '#2E4A7D', weight: 3.1, opacity: .96, lineCap: 'round', lineJoin: 'round'}).addTo(map);

const start = L.circleMarker([0, 0], {radius: 5, color: '#FBF8F0', weight: 2, fillColor: '#2E4A7D', fillOpacity: 1})
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
    map.setView(CENTER, 16);
  }
}

/* ---------- route library map ---------- */
const routeMap = L.map('routeMap', {zoomControl: true, attributionControl: true,
  scrollWheelZoom: false})
  .setView(CENTER, 16);
L.tileLayer(
  'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
  {subdomains: '1234', maxNativeZoom: 18, maxZoom: 19, attribution: '高德卫星'}).addTo(routeMap);
let routeFenceLayer = L.polygon([], {
  color: '#B6483C', weight: 1.6, dashArray: '6 5',
  fillColor: '#B6483C', fillOpacity: .03
}).addTo(routeMap);

const routeHi = L.polyline([], {color: '#F2D25C', weight: 13, opacity: .45, lineCap: 'round'}).addTo(routeMap);
const routeInk = L.polyline([], {color: '#2E4A7D', weight: 3, opacity: .95, lineCap: 'round'}).addTo(routeMap);
const routeStart = L.circleMarker([0, 0], {radius: 5, color: '#FBF8F0', weight: 2, fillColor: '#2E4A7D', fillOpacity: 1});

/* ---------- draw: click to add, smooth curve ---------- */
let pen = false, smoothOn = true, routeClosed = true;
let ctrlDots = [];

function setPen(on, silent){
  pen = on;
  const btn = $('#penBtn');
  btn.classList.toggle('active', on);
  btn.setAttribute('aria-pressed', String(on));
  map.getContainer().style.cursor = on ? 'crosshair' : '';
  if(on && !silent) toast('画笔已打开：单击加点，右键删点；地图仍可拖动');
}
$('#penBtn').addEventListener('click', () => setPen(!pen));
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
    state.textContent = '闭合：' + (on ? '开' : '关');
    state.classList.toggle('on', on);
  }
}

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
function drawDots(){
  ctrlDots.forEach(d => map.removeLayer(d));
  ctrlDots = [];
  route.forEach((p, i) => {
    const d = L.circleMarker(p, {
      radius: i === 0 ? 5 : 3,
      color: '#2E4A7D', weight: 1.3,
      fillColor: i === 0 ? '#2E4A7D' : '#FBF8F0', fillOpacity: 1
    }).addTo(map);
    if(i === 0) d.bindTooltip('起点', {direction: 'top', offset: [0, -5], className: 'pt-tip'});
    ctrlDots.push(d);
  });
}
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
    if(map.hasLayer(start)) map.removeLayer(start);
    $('#routeKm').innerHTML = '0.00<small>km</small>';
    $('#routeNote').innerHTML = '<b>·</b> 点击地图开始';
    $('#ptCount').textContent = '0 个控制点';
    $('#runSummary').textContent = '单圈 0.00 km · 等待画线';
    $('#mapNoteKm').textContent = '0.00 km';
    $('#mapNoteMeta').textContent = '等待画线';
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
  const target = (+$('#inKm').value || 2.5) * 1000;
  const closed = routeClosed && route.length >= 3;
  // 0.5% tolerance: a target equal to one lap must stay one lap after the
  // route length was rounded to centimetres for the input field.
  const laps = closed && m > 5
    ? Math.max(1, Math.ceil(target / m - 0.005)) : 0;
  const cov = typeof computeCoverage === 'function' ? computeCoverage() : null;
  let status;
  if(!routeClosed) status = '开放路线 · 再点可以继续';
  else if(route.length < 3) status = '再点一个点形成闭合';
  else if(m < 80) status = '路线太短（<80m），画大一点';
  else status = '闭合路线 · 打卡点 ' + (cov && cov.total ? (cov.passed + '/' + cov.total) : '未加载') + ' · ' + laps + ' 圈';
  $('#routeNote').innerHTML = closed && m >= 80
    ? '<b>✓</b> ' + status
    : '<b>·</b> ' + status;
  $('#ptCount').textContent = route.length + ' 个控制点';
  const pace = (+$('#inPace').value || 6);
  const totalMin = (target / 1000) * pace;
  $('#runSummary').textContent = '单圈 ' + km.toFixed(2) + ' km · ' + _paceText(pace) +
    ' · ' + (closed ? ('闭合 · 打卡 ' + (cov && cov.total ? (cov.passed + '/' + cov.total) : '未加载')) : '开放路线');

  $('#mapNoteKm').textContent = km.toFixed(2) + ' km';
  $('#mapNoteMeta').textContent = closed && m >= 80
    ? '闭合 · ×' + laps + ' 圈 · ' + durationText(totalMin)
    : status;
  if(typeof scheduleModelPreview === 'function') scheduleModelPreview();
}

map.on('click', e => {
  if(!pen) return;
  const p = [e.latlng.lat, e.latlng.lng];
  if(route.length && distM(route[route.length - 1], p) < 6) return;
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
  if(best >= 0 && bd < 26){
    route.splice(best, 1);
    redraw();
    toast('已删除 1 个点');
  }
});

$('#undoTool').addEventListener('click', () => {
  if(route.length <= 1){ toast('没有可撤销的点了'); return; }
  route.pop();
  redraw();
});
$('#clearBtn').addEventListener('click', () => {
  if(route.length >= 3 && !confirm('清空当前路线？')) return;
  route = [];
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
  setCloseUI(!routeClosed);
  redraw();
  refreshChecks();
  if(typeof scheduleModelPreview === 'function') scheduleModelPreview();
  toast(routeClosed
    ? '闭合已开：3 个点也会首尾相连；关掉可画开放路线'
    : '闭合已关：保留起点到终点');
});
$('#snapTool').addEventListener('click', () => $('#snapBtn').click());

redraw();

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
const RUN_LIMITS = {
  km: {min: 0.1, max: 42.2, label: '目标距离', unit: 'km'},
  pace: {min: 2, max: 20, label: '配速', unit: 'min/km'},
  cadence: {min: 60, max: 260, label: '步频', unit: 'spm', integer: true},
  altitude: {min: -100, max: 5000, label: '海拔基准', unit: 'm'}
};
function readRunParams(){
  return {
    km: +$('#inKm').value,
    pace: +$('#inPace').value,
    cadence: +$('#inCad').value,
    altitude: +$('#inAlt').value
  };
}
function validateRunParams(p){
  p = p || readRunParams();
  for(const key of Object.keys(RUN_LIMITS)){
    const spec = RUN_LIMITS[key], v = p[key];
    if(!isFinite(v)) return '请填写有效的' + spec.label;
    if(spec.integer && Math.abs(v - Math.round(v)) > 1e-9){
      return spec.label + '需要是整数';
    }
    if(v < spec.min || v > spec.max){
      return spec.label + '需要在 ' + spec.min + '–' + spec.max + ' ' +
        spec.unit + ' 之间';
    }
  }
  return '';
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

/* ---------- run panel tabs / collapse ---------- */
function switchRunTab(name){
  if(name !== 'params') closeDatePop();
  document.querySelectorAll('.rtab').forEach(t => {
    const on = t.dataset.rtab === name;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.rsec').forEach(s =>
    s.classList.toggle('active', s.id === 'rsec-' + name));
  const body = document.querySelector('.run-body');
  if(body) body.scrollTop = 0;
}
document.querySelectorAll('.rtab').forEach(t => {
  t.addEventListener('click', () => switchRunTab(t.dataset.rtab));
});
['inKm', 'inPace', 'inCad', 'inAlt'].forEach(id => {
  $('#' + id).addEventListener('input', () => {
    if(id === 'inKm') distanceManual = true;
    refreshParams();
    if(id !== 'inAlt') redraw();
    if(modelConfigured) refreshModel();
    scheduleModelPreview();
  });
});
document.querySelectorAll('[data-km]').forEach(b => {
  b.addEventListener('click', () => {
    distanceManual = true;
    $('#inKm').value = b.dataset.km;
    refreshParams(); redraw(); updateRunDateLabel();
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
  const cads = [160, 168, 172, 176, 180];
  const pick = a => a[Math.floor(Math.random() * a.length)];
  distanceManual = true;
  $('#inKm').value = pick(kms);
  $('#inPace').value = pick(paces);
  $('#inCad').value = pick(cads);
  refreshParams(); redraw(); updateRunDateLabel();
  if(modelConfigured) refreshModel();
  scheduleModelPreview();
  toast('随机档位已填入');
});

let currentMode = 'draw';
const MODE_HELP = {
  draw: '手绘：点地图加点；第一个落点为起点；闭合开时首尾相连。',
  auto: '自动：按当前加载的打卡点生成一圈；提交时按目标距离绕圈。',
  file: '导入：读取 GPX / GeoJSON / JSON / CSV（手表或别的 App 导出的轨迹）。',
  stadium: '田径场：生成 200/300/400m 标准跑道；提交时按目标距离刷圈。'
};
const ROUTE_HINT = {
  draw: '点左上角画笔，在地图上<b>单击加点</b>；右键删点。第一个落点为起点；闭合开时首尾相连。<br>点位和围栏来自登录后的真实接口；没有 token 时不会生成假点位。',
  auto: '自动模式：按当前加载的打卡点生成一圈；点「重新生成」换一条。提交时按目标距离自动绕圈。<br>没有点位时请先登录并刷新点位。',
  file: '导入模式：读取 GPX / GeoJSON / JSON / CSV；手表或别的 App 导出的轨迹。文件只在本机解析。',
  stadium: '田径场模式：选 200/300/400m 生成标准跑道；提交时按目标距离刷圈。'
};
function autoGenerate(){
  const left = livePoints.map(p => [p.lat, p.lng]);
  if(left.length < 2){ toast('先登录并刷新点位'); return; }
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
  route = out;
  setCloseUI(true);
  redraw();
  toast('自动：按 ' + livePoints.length + ' 个点位生成一圈；提交时按目标距离绕圈');
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
  route = pts;
  setCloseUI(true);
  redraw();
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
    route = pts;
    setCloseUI(true);
    redraw();
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

/* ---------- page routing ---------- */
const PAGES = ['overview', 'accounts', 'run', 'routes', 'records', 'profile', 'settings'];
let runMapFitted = false;
function go(page){
  if(!PAGES.includes(page)) page = 'overview';
  document.querySelectorAll('.page').forEach(p =>
    p.classList.toggle('active', p.id === 'page-' + page));
  document.querySelectorAll('.nav-item').forEach(n =>
    n.classList.toggle('active', n.dataset.page === page));
  if(history.replaceState) history.replaceState(null, '', '#' + page);
  if(page === 'run'){
    setTimeout(() => {
      map.invalidateSize();
      if(!runMapFitted){ fitRunMap(); runMapFitted = true; }
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
}
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => go(btn.dataset.page));
});
document.addEventListener('click', ev => {
  const el = ev.target.closest('[data-goto]');
  if(!el) return;
  if(el.tagName === 'A') ev.preventDefault();
  if(el.dataset.modeTarget) setMode(el.dataset.modeTarget);
  if(el.dataset.goto === 'run') switchRunTab('route');
  go(el.dataset.goto);
});
document.addEventListener('keydown', ev => {
  const el = ev.target.closest('[data-goto]');
  if(!el) return;
  if(ev.key === 'Enter' || ev.key === ' '){
    ev.preventDefault();
    if(el.dataset.modeTarget) setMode(el.dataset.modeTarget);
    if(el.dataset.goto === 'run') switchRunTab('route');
    go(el.dataset.goto);
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
  session: null, checked: null, points: [], fence: [],
  records: [], recordsLoaded: false, recordsLoading: false, recordFilter: 'all',
  routes: [], selectedRoute: -1,
  routeFilter: 'all', rules: null, model: null, plan: null,
  runDate: null, daily: null, dailyCounts: {}, dailyCountsAt: 0,
  dailyCountsUid: null,
  recordsError: null, weekError: null, planError: null,
  semester: null, semesterError: null,
  profile: null, profileError: null, profileLoading: false, profileAt: 0,
  violations: null, violationsError: null, violationsAt: 0,
  receipts: [], receiptsLoaded: false, receiptsError: null,
  preflight: null, lastDetail: null,
  tokenUpdatedAt: null,
  log: [], submitting: false, lastSubmit: null
};

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
  if(/超时|timed out|timeout|bridge 超时/i.test(msg)) return '登录等待超时了。后台可能仍在处理，请稍等几秒再试。';
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
    if(LOGIN.visible && !LOGIN.busy){ const u = $('#loginUser'); if(u) u.focus(); }
  }, 80);
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
async function gateLogin(){
  if(LOGIN.busy) return;
  const user = ($('#loginUser') ? $('#loginUser').value : '').trim();
  const pass = $('#loginPass') ? $('#loginPass').value : '';
  if(!user){ loginFail('请输入手机号', '#loginUser'); return; }
  if(!pass){ loginFail('请输入密码', '#loginPass'); return; }
  setLoginBusy(true, '正在登录… 通常 5–15 秒', 'busy');
  startLoginClock();
  liveLog('登录中', '正在验证账号', 'info');
  try{
    const profile = await requestLogin(user, pass);
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
    fetchSchoolRules({silent: true});
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
  }catch(e){
    loginFail(prettyLoginError(e && e.message ? e.message : e));
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
  list.innerHTML = LIVE.log.map(e => {
    const show = filter === 'all' ||
      (filter === 'warn' && (e.level === 'warn' || e.level === 'wait')) ||
      (filter === 'err' && e.level === 'err');
    return `<li class="log-event ${esc(e.level)}" data-level="${esc(e.level)}" ${show ? '' : 'hidden'}>
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
  LIVE.plan = null;
  LIVE.daily = null;
  LIVE.model = null;
  LIVE.records = []; LIVE.recordsLoaded = false; LIVE.recordsLoading = false;
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
  renderLog();
  renderRecords();
  renderProfileEmpty();
}

/* ---------------- session / account ---------------- */
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
  $('#sideCampus').textContent = logged ? (s.campusName ? ('运动世界 · ' + s.campusName) : '本地工作台') : '本地工作台';

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
    session: '手动',
    error: '读取失败'
  }[s.device_source] || '已冻结';
  const osLabel = s.device_os
    ? ((s.device_platform === 'ios' ? 'iOS ' : 'Android ') + s.device_os)
    : '';
  const deviceLabel = [s.device_name, osLabel].filter(Boolean).join(' · ');
  $('#accDeviceChip').textContent = logged
    ? (s.device_source === 'error' ? '设备读取失败' : '设备 · ' + deviceSource)
    : '设备未加载';
  $('#accDeviceChip').className = 'chip' + (s.device_source === 'error' ? ' red' : '');
  $('#accDeviceChip').title = s.device_source === 'error'
    ? (s.device_error || '设备指纹读取失败')
    : '设备指纹在本机生成一次，登录和提交共用；不要频繁更换。';
  $('#accUidChip').textContent = s.uid ? ('uid ' + s.uid) : 'uid —';
  $('#accDisabledChip').hidden = !s.disabled;
  $('#accTokenMasked').textContent = logged
    ? (s.token_masked || maskToken(s.token)) : '—';
  $('#accDevice').textContent = logged ? (deviceLabel || '—') : '—';
  $('#accDeviceId').textContent = logged && hasDevice
    ? (s.device_id_masked || '—') : '—';
  $('#accCampus').textContent = logged ? (s.campusName || '—') : '—';
  $('#accUnid').textContent = logged ? (s.unid || '—') : '—';
  if($('#deviceRotateBtn')) $('#deviceRotateBtn').disabled = !logged || LIVE.submitting;
  if(!logged && $('#deviceRotateConfirm')) $('#deviceRotateConfirm').value = '';

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
  LIVE.dailyCounts = {}; LIVE.dailyCountsAt = 0; LIVE.dailyCountsUid = null;
  if(!liveOk(j)) liveLog('读取会话失败', j.message || '', 'err');
  renderSession();
  await loadKeepalive();
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
    fetchSchoolRules({silent: true});
  }else{
    showLoginGate((res && res.message) || '请输入手机号和密码登录。');
  }
  initRunDate();
  updateRunDateLabel();
  refreshChecks();
  refreshDailyCount();
  refreshModelPreview();
}

function renderEmptyData(){
  invalidateLiveRequests();
  renderPoints([]);
  setFence([], '跑步围栏');
  route = [];
  redraw();
  LIVE.points = [];
  LIVE.fence = [];
  LIVE.records = [];
  LIVE.recordsLoaded = false;
  LIVE.recordsLoading = false;
  LIVE.recordsError = null;
  LIVE.weekError = null;
  LIVE.planError = null;
  LIVE.plan = null;
  LIVE.daily = null;
  LIVE.lastSubmit = null;
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
    fetchSchoolRules({silent: true});
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
    LIVE.session = {}; LIVE.checked = null; LIVE.points = []; LIVE.fence = [];
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
async function loadPoints(force){
  if(!hasSession()){ toast('先登录后再刷新点位'); return; }
  const seq = ++_pointsSeq;
  const s = LIVE.session;
  const btn = $('#fetchPoints');
  if(btn){ btn.textContent = '刷新中…'; btn.disabled = true; }
  // Checkpoints belong to the campus, not to today's target distance.  The
  // bridge uses its fixed campus-query distance; changing 目标距离 must not
  // re-fetch or invalidate the points already on the map.
  const j = await liveApi('real_fetch_points', {
    school: s.schoolName || '', glat: +s.lat || 0, glon: +s.lon || 0,
    unid: s.unid || null, force: !!force
  });
  // A slower older request must not overwrite a newer campus/distance.
  if(seq !== _pointsSeq) return;
  if(btn){ btn.textContent = '刷新'; btn.disabled = false; }
  if(!liveOk(j)){
    liveLog('围栏/点位加载失败', j.message || '', 'err');
    setFence([], '跑步围栏');
    renderPoints([]);
    LIVE.points = []; LIVE.fence = [];
    updateFenceChip();
    refreshChecks();
    return;
  }
  const d = j.data || {};
  LIVE.fence = (Array.isArray(d.polygon) ? d.polygon : [])
    .filter(p => p && isFinite(+ (p.lat ?? p.glat)) && isFinite(+(p.lon ?? p.glon)));
  LIVE.points = (Array.isArray(d.checkpoints) ? d.checkpoints : [])
    .filter(p => p && isFinite(+(p.lat ?? p.glat)) && isFinite(+(p.lon ?? p.glon)));
  // Always redraw, even when the server returned an empty list: otherwise the
  // previous campus's fence/points stayed on the map after switching accounts.
  const hasPoints = LIVE.points.length > 0;
  setFence(LIVE.fence, (s.campusName || '') + '跑步围栏', {fit: !hasPoints});
  renderPoints(LIVE.points, {fit: hasPoints});
  if(Array.isArray(d.errors) && d.errors.length){
    liveLog('围栏/点位部分失败', d.errors.join('；'), 'warn');
  }
  liveLog('围栏/点位已加载', `${LIVE.fence.length} 个围栏顶点 · ${livePoints.length} 个打卡点 · 按校区固定范围拉取（与目标距离无关）`, 'ok');
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
  const ok = LIVE.fence.length && (livePoints.length || freeRunOn());
  const label = ok
    ? ((freeRunOn() && !livePoints.length) ? '围栏已加载 · 自由跑' : '围栏/点位已加载')
    : '围栏/点位未加载';
  if(chip){
    chip.textContent = label;
    chip.className = 'tag ' + (ok ? '' : 'warn');
  }
  if(top){
    top.textContent = ok ? '围栏已加载' : '围栏未加载';
    top.className = 'chip ' + (ok ? 'ok' : 'warn');
    top.innerHTML = '<i></i>' + top.textContent;
  }
  if($('#ovFenceChip')){
    $('#ovFenceChip').textContent = ok ? '围栏已加载' : '围栏未加载';
    $('#ovFenceChip').className = 'chip ' + (ok ? 'ok' : 'warn');
    $('#ovFenceChip').innerHTML = '<i></i>' + $('#ovFenceChip').textContent;
  }
}

/* ---------------- overview ---------------- */
function renderOverviewEmpty(){
  LIVE.recordsError = null;
  LIVE.weekError = null;
  LIVE.planError = null;
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
  if(r && (String(r.status_code) === '2' || String(r.status) === '2')) return 'issue';
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
  const bar = el('SemesterBar'), source = el('SemesterSource');
  if(!v){
    if(title) title.textContent = error ? '学期数据读取失败' : '登录后读取';
    if(pct) pct.textContent = '—';
    if(done) done.textContent = '—';
    if(left) left.textContent = '—';
    if(days) days.textContent = '—';
    if(bar) bar.style.width = '0%';
    if(source) source.textContent = error ? '失败' : '—';
    return;
  }
  const goal = v.goal_km, got = v.done_km;
  const percent = v.completed_pct != null ? +v.completed_pct
    : (goal && got != null ? Math.min(100, got / goal * 100) : 0);
  if(title) title.textContent = v.sname || '本学期';
  if(pct) pct.textContent = (percent != null ? percent.toFixed(1) + '%' : '—') +
    (v.goal_count ? (' · ' + (v.valid_count || 0) + '/' + v.goal_count + ' 次') : '');
  if(done) done.innerHTML = got != null ? (got.toFixed(1) + '<small> km</small>') : '—';
  if(left) left.innerHTML = v.remaining_km != null ? (v.remaining_km.toFixed(1) + '<small> km</small>') : '—';
  if(days) days.textContent = v.days_left != null
    ? (v.days_left <= 0 ? '已结束' : (v.days_left + ' 天')) : '—';
  if(bar) bar.style.width = Math.max(0, Math.min(100, percent)).toFixed(1) + '%';
  if(source) source.textContent = v.source === 'personal' ? '个人完成度' : (v.source === 'summary' ? '学期汇总' : '—');
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
  setText('profDevice', logged
    ? ([s.device_name, s.device_os ? ('Android ' + s.device_os) : '']
        .filter(Boolean).join(' · ') || '—') : '—');
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
    if(v == null || v === '') return '';
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
  setText('profDevice', logged
    ? ([s.device_name, s.device_os ? ('Android ' + s.device_os) : '']
        .filter(Boolean).join(' · ') || '—') : '—');
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
  const slots = (LIVE.plan && LIVE.plan.slots) || [];
  if(!slots.length){
    wrap.innerHTML = LIVE.planError
      ? `<div class="hint">计划生成失败：${esc(LIVE.planError)}</div>`
      : '<div class="hint">暂时没有可用计划。</div>';
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
    LIVE.weekError = null; LIVE.planError = null;
    renderRecords();
    renderOverviewEmpty();
    return;
  }
  const [rj, wj, pj, sj] = await Promise.all([
    liveApi('real_recent_records', {limit: 20}),
    liveApi('real_weekly_summary', {weeks: 4}),
    liveApi('real_plan', {days: 14}),
    liveApi('real_semester')
  ]);
  // Logout or an account switch may have happened while these four requests
  // were in flight.  Applying them now would show the previous account's
  // records and semester under the new session.
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
  LIVE.weekError = weekOk ? null : (wj.message || '周汇总加载失败');
  LIVE.planError = liveOk(pj) ? null : (pj.message || '计划生成失败');
  LIVE.plan = liveOk(pj) ? (pj.data || null) : null;
  LIVE.semester = liveOk(sj) ? (sj.data || null) : null;
  LIVE.semesterError = liveOk(sj) ? null : (sj.message || '学期数据加载失败');
  renderOverview(records, week, weeks, LIVE.semester);
  if(!recordsOk) liveLog('记录加载失败', LIVE.recordsError, 'err');
  if(!weekOk) liveLog('周汇总加载失败', LIVE.weekError, 'warn');
  if(!liveOk(pj)) liveLog('计划生成失败', LIVE.planError, 'warn');
  if(!liveOk(sj)) liveLog('学期数据加载失败', LIVE.semesterError, 'warn');
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
  const cap = LIVE.rules && LIVE.rules.configured && LIVE.rules.rules ? LIVE.rules.rules.max_per_day : null;
  $('#ovTodayLimit').textContent = cap ? ('/' + cap + ' 次') : '/— 次';

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

  const checklist = [
    {ok: !!(LIVE.checked && liveOk(LIVE.checked)), text: LIVE.checked && liveOk(LIVE.checked) ? 'token 有效' : 'token 未验证'},
    {ok: livePoints.length > 0, text: livePoints.length ? ('点位已加载 · ' + livePoints.length + ' 个') : '点位未加载'},
    {ok: !!(LIVE.rules && LIVE.rules.configured), text: LIVE.rules && LIVE.rules.configured ? '学校要求已配置' : '学校要求未配置'}
  ];
  $('#ovChecklist').innerHTML = checklist.map(x => `<div class="mini-check ${x.ok ? 'done' : 'warn'}">${esc(x.text)}</div>`).join('');

  const slot = nextPlanSlot(LIVE.plan && (LIVE.plan.slots || []));
  if(slot){
    $('#ovPlan').innerHTML = `${esc(slot.day || '')}<br>${esc(slot.distance_km)} km · ${esc(slot.pace_hhmmss || '')} · ${esc(slot.route || '')}`;
  }else{
    $('#ovPlan').textContent = LIVE.planError
      ? ('计划生成失败：' + LIVE.planError) : '暂时没有可用计划。';
  }
  const weekCount = week ? (week.count || 0) : null;
  $('#ovRhythm').textContent = weekCount === null
    ? (LIVE.weekError ? ('周汇总加载失败：' + LIVE.weekError) : '本周还没有记录。')
    : (`本周已跑 ${weekCount} 次` + (cap ? ` · 学校每日上限 ${cap} 次` : ' · 未配置学校每日上限'));
  const modelOk = !!(LIVE.model && LIVE.model.configured);
  const modelBad = modelOk && LIVE.model.verdict === 'fail';
  const rulesConfigured = !!(LIVE.rules && LIVE.rules.configured);
  $('#ovModelChip').textContent = modelOk ? ('模型 ' + LIVE.model.score) : (rulesConfigured ? '模型待预览' : '模型未配置');
  $('#ovModelChip').className = 'chip ' + (modelOk ? (modelBad ? 'warn' : 'ok') : 'warn');
  $('#ovModelChip').innerHTML = '<i></i>' + $('#ovModelChip').textContent;
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
                          scrollWheelZoom: false}).setView(CENTER, 16);
  L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
    {subdomains: '1234', maxNativeZoom: 18, maxZoom: 19,
     attribution: '高德卫星'}).addTo(rdMap);
  rdTrackGlow = L.polyline([], {color: '#F2D25C', weight: 9, opacity: .45,
                                lineCap: 'round'}).addTo(rdMap);
  rdTrackLine = L.polyline([], {color: '#2E4A7D', weight: 2.6, opacity: .95,
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
}
function renderRecordMap(detail){
  const track = Array.isArray(detail && detail.track) ? detail.track : [];
  const five = Array.isArray(detail && detail.five_point) ? detail.five_point : [];
  const meta = (detail && detail.detail) || {};
  const hint = $('#rdMapHint');
  if(!track.length){
    clearRecordMap();
    if(hint){
      hint.textContent = meta.obs_error
        ? ('轨迹在 OBS，但读取失败：' + meta.obs_error)
        : (meta.json_from_obs
          ? '这条记录的轨迹在 OBS，但服务端没有返回可读对象。'
          : '这条记录没有读到轨迹（服务端未返回 allLocJson）。');
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
  rdStart = L.circleMarker(line[0], {radius: 5, color: '#FBF8F0', weight: 2,
    fillColor: '#2E4A7D', fillOpacity: 1}).addTo(rdMap).bindTooltip('起点');
  rdEnd = L.circleMarker(line[line.length - 1], {radius: 5, color: '#FBF8F0', weight: 2,
    fillColor: '#B6483C', fillOpacity: 1}).addTo(rdMap).bindTooltip('终点');
  five.forEach((p, i) => {
    const lat = +(p.glat ?? p.gLat ?? p.lat);
    const lng = +(p.glon ?? p.gLng ?? p.lng ?? p.lon);
    if(!isFinite(lat) || !isFinite(lng)) return;
    const ok = !!(p.isPass ?? p.is_pass ?? p.pass);
    L.circleMarker([lat, lng], {radius: 6, color: '#FBF8F0', weight: 2,
      fillColor: ok ? '#3F7A4B' : '#B6483C', fillOpacity: .95})
      .addTo(rdPtLayer)
      .bindTooltip(esc(p.pointName || p.name || ('点位 ' + (i + 1))) + (ok ? ' · 已通过' : ' · 未通过'));
  });
  rdMap.fitBounds(L.latLngBounds(line).pad(.18));
  setTimeout(() => { try{ rdMap.invalidateSize(); }catch(e){} }, 80);
  if(hint) hint.textContent = `${line.length} 个轨迹点 · ${five.length} 个打卡点`;
  if($('#rdGpx')) $('#rdGpx').disabled = false;
  if($('#rdSaveRoute')) $('#rdSaveRoute').disabled = false;
}
function clearRecordDetail(){
  ['rdPace','rdTime','rdCadence','rdSteps','rdTrack','rdCheat','rdPolicy','rdRrid'].forEach(id => {
    const el = $('#' + id); if(el) el.textContent = '—';
  });
  const tag = $('#rdTag'); if(tag){ tag.textContent = '未选择'; tag.className = 'tag'; }
  if($('#rdTitle')) $('#rdTitle').textContent = '点左侧记录查看详情';
  if($('#rdReasons')) $('#rdReasons').innerHTML = '';
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
    return;
  }
  if(accountBusy){ toast('账号操作进行中，请稍候'); return; }
  const btn = $('#receiptReconcile');
  if(btn){ btn.disabled = true; btn.textContent = '核对中…'; }
  try{
    const j = await liveApi('real_reconcile_receipts', {limit: 80}, 120000);
    if(!liveOk(j)){
      toast('核对失败：' + (j.message || ''));
      liveLog('回执核对失败', j.message || '', 'warn');
      return;
    }
    const d = j.data || {};
    const matched = (d.matched || []).length;
    const unmatched = (d.unmatched || []).length;
    toast(`核对完成：${matched} 条已匹配${unmatched ? '，' + unmatched + ' 条仍未决' : ''}`);
    liveLog('回执只读核对', `匹配 ${matched} · 未决 ${unmatched} · 查了 ${d.records || 0} 条记录`,
            matched ? 'ok' : 'warn');
    await loadReceipts();
    if(matched){ await loadRecords(); if(typeof loadOverview === 'function') loadOverview(); }
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
    return;
  }
  const d = j.data || {}, x = d.detail || {}, s = d.stats || {};
  LIVE.lastDetail = d;
  const km = (+x.distance_m || 0) / 1000;
  const state = x.state || recordState(x);
  const tagText = state === 'issue' ? '有问题' : state === 'ok' ? '已通过' : '待核对';
  $('#rdTag').textContent = tagText;
  $('#rdTag').className = 'tag ' + (state === 'issue' ? 'red' : state === 'ok' ? 'ok' : 'warn');
  $('#rdTitle').textContent = `${shortDate(x.start_ms)} · ${km.toFixed(2)} km`;
  $('#rdPace').textContent = paceText(paceFromSec(x.duration_s, km));
  $('#rdTime').textContent = hms(x.duration_s);
  $('#rdCadence').textContent = x.avg_step_freq ? (x.avg_step_freq + ' spm') : '—';
  $('#rdSteps').textContent = x.total_steps ? String(x.total_steps) : '—';
  $('#rdTrack').textContent = (s.points || 0) + ' 点' + (s.has_altitude ? ` · 海拔 ${s.alt_min}–${s.alt_max} m` : '');
  $('#rdCheat').textContent = x.normal_run == null ? '—' : String(x.normal_run);
  $('#rdPolicy').textContent = (s.segments || 0) + ' 段';
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
  let passed = 0;
  livePoints.forEach(p => {
    if(line.some(q => distM(q, [p.lat, p.lng]) < 40)) passed++;
  });
  return {passed: passed, total: livePoints.length};
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
  ['rStepMin', 'step_min'], ['rStepMax', 'step_max'],
  ['rWinStart', 'window_start'], ['rWinEnd', 'window_end']
];
function ruleCount(r){
  r = r || {};
  return RULE_FIELDS.filter(([, key]) => {
    const v = r[key];
    return v !== null && v !== undefined && String(v).trim() !== '';
  }).length;
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
  window_start: '时间窗开始', window_end: '时间窗结束'
};
function applyRulesToForm(r){
  r = r || {};
  RULE_FIELDS.forEach(([id, key]) => {
    const el = $('#' + id);
    if(el) el.value = (r[key] === null || r[key] === undefined) ? '' : r[key];
  });
}
function collectRulesForm(){
  const rules = {};
  RULE_FIELDS.forEach(([id, key]) => {
    const el = $('#' + id);
    if(el) rules[key] = el.value.trim();
  });
  return rules;
}
function rulesFormHasValues(){
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
async function loadRules(){
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
  const rules = collectRulesForm();
  const filled = RULE_FIELDS.filter(([id]) => $('#' + id) && $('#' + id).value.trim()).length;
  if(!filled && !confirm('所有项都留空，保存后会关闭模型并停止自动读取。继续？')) return;
  // Empty save is an explicit "model off"; do not auto-fetch it back later.
  rules.auto_fetch = !!filled;
  const btn = $('#saveRules');
  if(btn){ btn.disabled = true; btn.textContent = '保存中…'; }
  try{
    await persistRules(rules);
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
    if(meta.window_count > 1){
      const ws = (meta.windows || []).join('、');
      notes.push('服务器时段 ' + (ws || meta.window_count + ' 个') + '，表单填第一个');
    }
    if(meta.day_goal_m) notes.push('服务器每日目标 ' + (meta.day_goal_m / 1000).toFixed(1) + ' km（未映射）');
    const labels = keys.map(k => RULE_LABELS[k] || k);
    const hint = '已从服务器自动填入 ' + labels.join('、') + '；可修改后保存。' +
      (notes.length ? ' ' + notes.join('；') + '。' : '');
    const saved = await persistRules(merged, {silent: true, hint});
    if(!saved){
      $('#rulesHint').textContent =
        '服务器规则已填入，但保存失败；请点「保存要求」重试。';
      if(!opts.silent) toast('规则已填入但保存失败');
      return false;
    }
    liveLog('学校规则已从服务器填入',
      'runMode ' + d.run_mode + ' · ' + labels.join('、') +
      (notes.length ? ' · ' + notes.join('；') : ''), 'ok');
    if(!opts.silent) toast('已填入服务器规则，请确认后保存');
    return true;
  } finally {
    if(btn){ btn.disabled = false; btn.textContent = '从服务器读取'; }
  }
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
  const dur = (+$('#inKm').value || 2.5) * (+$('#inPace').value || 6);
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
function selectedTimeInfo(){
  const key = LIVE.runDate || beijingTodayKey();
  const daysAgo = dateToDaysAgo(key);
  const raw = ($('#inStart') ? $('#inStart').value : '').trim();
  // Match the backend defaults in submitRun(), not 0: an empty input must not
  // make the window check believe the run lasts zero minutes.
  const km = +$('#inKm').value || 2.5;
  const pace = +$('#inPace').value || 6;
  const durMin = km * pace;
  const out = {
    date: key, daysAgo, raw, time: null, durMin,
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
  const now = beijingNowDate();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const latest = daysAgo === 0 ? Math.min(22 * 60, nowMin) : 22 * 60;
  const fail = note => {
    out.timeOk = false; out.ok = false; out.timeNote = note; out.note = note;
    return out;
  };
  if(daysAgo === 0 && !raw && nowMin < 9 * 60){
    return fail('今天还没到 09:00，换一天或等 09:00 后再试');
  }
  if(latest - durMin < 9 * 60){
    return fail(daysAgo === 0
      ? '今天剩余时间放不下这次跑步，换一天或缩短距离'
      : '这次跑步太长，放不进 09:00–22:00');
  }
  if(!raw){
    out.timeNote = daysAgo === 0
      ? '留空 · 今天 09:00–' + clock(latest - durMin) + ' 内随机'
      : '留空 · 09:00–22:00 内随机';
    out.note = out.timeNote;
    return out;
  }
  const m = raw.match(/^(\d{1,2}):(\d{2})$/);
  if(!m) return fail('要写成 HH:MM');
  const hh = +m[1], mm = +m[2];
  if(hh > 23 || mm > 59) return fail('时间不存在');
  const mins = hh * 60 + mm;
  if(mins < 9 * 60) return fail('不能早于 09:00');
  if(mins > 22 * 60) return fail('不能晚于 22:00');
  if(mins + durMin > latest){
    return fail(daysAgo === 0
      ? (mins > nowMin ? '今天这个时间还没到（现在 ' + clock(nowMin) + '）'
                       : '这个时间结束时已经超过现在（现在 ' + clock(nowMin) + '）')
      : '按当前距离/配速，结束会超过 22:00，请提前开始');
  }
  out.time = pad2(hh) + ':' + pad2(mm);
  out.timeNote = dateDisplay(key) + ' ' + out.time + ' 开始 · 约 ' +
    Math.round(durMin) + ' 分钟';
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
      let randomHint = '开始时间留空则在 09:00–22:00 内随机';
      if(ti.daysAgo === 0){
        // Today's random window stops at ``now - duration``, not at 22:00;
        // the old hint promised a slot the backend would refuse.
        const now = beijingNowDate();
        const nowMin = now.getHours() * 60 + now.getMinutes();
        const latestStart = Math.max(9 * 60,
          Math.min(22 * 60, nowMin) - ti.durMin);
        const hm = m => String(Math.floor(m / 60)).padStart(2, '0') + ':' +
                        String(Math.round(m) % 60).padStart(2, '0');
        randomHint = '开始时间留空则在 09:00–' + hm(latestStart) +
          ' 内随机（不能晚于现在）';
      }
      $('#timeHint').textContent = '可选 ' + range + '（北京时间）；' +
        (ti.time ? ti.time + ' 开始，约 ' + Math.round(ti.durMin) + ' 分钟'
                 : randomHint) + '；结束也要在 22:00 前。';
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

function scheduleModelPreview(){ clearTimeout(_modelTimer); _modelTimer = setTimeout(refreshModelPreview, 300); }
async function refreshModelPreview(){
  const seq = ++_modelSeq;
  if(!hasSession()){
    LIVE.model = null; renderModel(); refreshChecks(); return;
  }
  const ti = selectedTimeInfo();
  if(!ti.ok){ LIVE.model = null; renderModel(); refreshChecks(); return; }
  if(!LIVE.rules || !LIVE.rules.configured){ LIVE.model = null; renderModel(); refreshChecks(); return; }
  const di = dailyCountInfo();
  const params = readRunParams();
  if(validateRunParams(params)){
    LIVE.model = null; renderModel(); refreshChecks(); return;
  }
  const j = await liveApi('real_preview_model', {
    km: params.km, pace: params.pace,
    cadence: params.cadence, altitude: params.altitude,
    waypoints: route && route.length >= 2 ? route : null,
    checkpoints: freeRunOn() ? null :
      livePoints.map(p => ({lat: p.lat, lon: p.lng, name: p.n, isMustPoint: p.must})),
    date: ti.date,
    start_time: ti.time,
    closed: !!routeClosed,
    daily_runs: di.loaded ? di.count : null,
    daily_source: di.loaded ? di.source : null
  });
  if(seq !== _modelSeq) return;
  LIVE.model = liveOk(j) ? ((j.data && j.data.model) || null) : null;
  if(!liveOk(j)) liveLog('模型预览失败', j.message || '', 'warn');
  renderModel();
  refreshChecks();
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
  sub.textContent = m.summary || ((m.verdict === 'pass' ? '合格' : m.verdict === 'warn' ? '有提醒' : '不合格') +
    (m.warnings && m.warnings.length ? (' · ' + m.warnings.length + ' 项注意') : ''));
  tag.textContent = m.verdict === 'pass' ? '合格' : m.verdict === 'warn' ? '提醒' : '不合格';
  tag.className = 'tag ' + (m.verdict === 'pass' ? 'ok' : m.verdict === 'warn' ? 'warn' : 'warn');
  bar.style.width = Math.max(0, Math.min(100, +m.score || 0)) + '%';
  empty.hidden = true; checks.hidden = false;
  checks.innerHTML = (m.checks || []).map(c => {
    const cls = c.status === 'pass' ? 'ok' : (c.status === 'skip' ? 'skip' : 'warn');
    const txt = c.status === 'pass' ? '合格' : c.status === 'skip' ? '未要求' : c.status === 'warn' ? '注意' : '不合格';
    return `<li><span>${esc(c.label)}</span><b>${esc(c.value)}</b><i class="${cls}">${txt}</i></li>`;
  }).join('');
  $('#runModelTag').textContent = '模型 ' + m.score;
  $('#runModelTag').className = 'tag ' + (m.verdict === 'fail' ? 'warn' : 'ok');
  $('#runModelLink').textContent = '查看';
  $('#genState').textContent = '模型就绪'; $('#genState').className = 'ok';
  $('#runModelChip').textContent = '模型 ' + m.score; $('#runModelChip').className = 'chip ' + (m.verdict === 'fail' ? 'warn' : 'ok');
  $('#runModelChip').innerHTML = '<i></i>' + $('#runModelChip').textContent;
  if($('#logModel')) $('#logModel').textContent = m.score + ' 分';
  if($('#ovModelChip')){
    $('#ovModelChip').textContent = '模型 ' + m.score;
    $('#ovModelChip').className = 'chip ' + (m.verdict === 'fail' ? 'warn' : 'ok');
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
function computeCoverage(){
  if(!livePoints.length || !route || route.length < 2) return {passed: 0, total: livePoints.length, missed: livePoints.length};
  const path = displayPath();
  let passed = 0;
  livePoints.forEach(p => { if(path.some(q => distM(q, [p.lat, p.lng]) < 40)) passed++; });
  return {passed, total: livePoints.length, missed: livePoints.length - passed};
}
function beijingEpoch(dateKey, hhmm){
  const d = parseDateKey(dateKey);
  if(!d) return Date.now();
  const m = String(hhmm || '09:00').match(/^(\d{1,2}):(\d{2})$/);
  const hh = m ? +m[1] : 9, mm = m ? +m[2] : 0;
  return Date.UTC(d.getFullYear(), d.getMonth(), d.getDate(), hh - 8, mm);
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
      if(city) city.textContent = '自检失败';
      if(grid) grid.hidden = true;
      if(text){ text.className = 'preflight-warn'; text.textContent = j.message || '网络自检失败'; }
      liveLog('网络自检失败', j.message || '', 'warn');
      LIVE.preflight = {ok: false, error: j.message || '网络自检失败', at: Date.now()};
      refreshChecks();
      return;
    }
    const d = j.data || {};
    const ip = (d.info && d.info.ip) || null;
    const want = (d.info && d.info.expected_city) || d.campus || '—';
    const got = ip && ip.city ? ip.city : '';
    const match = ip && d.info && d.info.ip_city_matches;
    if(city) city.textContent = match ? ('同城 · ' + want) :
      (got ? ('不一致 · ' + got + ' / ' + want) : '无法确认');
    if(grid){
      grid.hidden = false;
      grid.innerHTML = `<div>校区城市 <b>${esc(want)}</b></div>` +
        `<div>出口 IP <b>${esc(got || '—')}</b></div>` +
        `<div>开始时间 <b>${esc(d.start_text || ti.date + ' ' + (ti.time || '09:00'))}</b></div>` +
        `<div>查询时间 <b>${esc((ip && ip.source) || '—')}</b></div>`;
    }
    const warns = d.warnings || [];
    if(text){
      text.className = warns.some(w => !w.startsWith('提醒')) ? 'preflight-warn' : 'preflight-ok';
      text.innerHTML = warns.length
        ? warns.map(w => esc(w)).join('<br>')
        : '没有发现明显问题；提交前仍建议人工确认手机与电脑在同一城市网络。';
    }
    liveLog('网络自检', `${got || '未知'} / 校区 ${want}` +
      (warns.length ? (' · ' + warns[0]) : ''), warns.length ? 'warn' : 'ok');
    LIVE.preflight = {
      ok: match === true,
      city: got,
      want: want,
      warnings: warns,
      at: Date.now()
    };
    refreshChecks();
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = '开始自检'; }
  }
}

function refreshChecks(){
  const checks = [];
  const verified = !!(LIVE.checked && liveOk(LIVE.checked));
  checks.push({ok: verified, label: 'token 有效', note: verified ? '服务端已验证' : (hasSession() ? '未验证/失效' : '未登录')});
  // Network egress is part of the pre-submit checklist: the run detail page
  // shows the IP's city, so a campus in Jiangmen on a Guangzhou egress is an
  // avoidable exposure.  The check is advisory; submission is never blocked.
  const pf = LIVE.preflight;
  if(!pf){
    checks.push({ok: false, warn: true, label: '网络出口',
      note: '未自检；点「开始自检」确认与校区同城（仅提醒）'});
  }else if(pf.error){
    checks.push({ok: false, warn: true, label: '网络出口',
      note: pf.error + '（仅提醒）'});
  }else{
    checks.push({
      ok: !!pf.ok,
      warn: !pf.ok,
      label: '网络出口',
      note: pf.ok
        ? ('同城 · ' + (pf.city || pf.want || '已确认'))
        : ('不一致 · ' + (pf.city || '未知') + ' / ' + (pf.want || '校区未知'))
    });
  }
  const free = freeRunOn();
  checks.push({ok: free || livePoints.length > 0, warn: free && !livePoints.length,
    label: free ? '自由跑' : '点位已加载',
    note: free ? '不检查打卡点' : (livePoints.length ? (livePoints.length + ' 个 · ' + livePoints.filter(p => p.must).length + ' 必经') : '未加载')});
  const cov = computeCoverage();
  checks.push({ok: free || (cov.total > 0 && cov.missed === 0), warn: free,
    label: '经过全部打卡点',
    note: free ? '自由跑已跳过' : (cov.total ? (cov.passed + '/' + cov.total + ' 在 40m 内') : '未加载点位')});
  let out = null;
  if(LIVE.fence.length && route && route.length >= 2){
    out = route.filter(p => !pointInPolygon(p, LIVE.fence)).length;
  }
  checks.push({ok: out === 0, label: '在跑步围栏内', note: !LIVE.fence.length ? '围栏未加载' : (!route || route.length < 2 ? '待画线' : ('越界 ' + out + ' 点'))});
  const configured = !!(LIVE.rules && LIVE.rules.configured);
  checks.push({ok: configured, label: '学校要求已配置', note: configured ? '模型已启用' : '设置 → 学校要求'});

  const ti = selectedTimeInfo();
  checks.push({ok: ti.dateOk, label: '提交日期', note: ti.dateNote});
  checks.push({ok: ti.timeOk, label: '开始时间', note: ti.timeNote || ti.note});

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
  checks.push({ok: true, warn: dWarn, label: '当日次数', note: dNote});

  const wrap = $('#checks');
  if(wrap) wrap.innerHTML = checks.map(c => `<div class="check-row ${c.ok ? 'checked' : 'warn'}${c.warn ? ' note-warn' : ''}"><span class="cbox"></span><span class="clabel">${esc(c.label)}</span><span class="cnote">${esc(c.note)}</span></div>`).join('');
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
  if($('#submitRun')) $('#submitRun').disabled = !hasSession() || !verified || LIVE.submitting;
  if($('#submitHint')) $('#submitHint').textContent = all ? '检查通过。提交后该时段会被回执锁定，不能重复提交。' : '先补齐上面的检查项；提交仍会由后端再次校验。';
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
async function submitRun(){
  if(LIVE.submitting) return;
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
  if(invalid){
    toast(invalid);
    switchRunTab('params');
    return;
  }
  const {km, pace, cadence, altitude} = params;
  const ti = selectedTimeInfo();
  if(!ti.ok){ toast('开始时间无效：' + ti.note); switchRunTab('params'); return; }
  refreshChecks();
  const allReady = !!(document.querySelector('#dock') && document.querySelector('#dock').classList.contains('ready'));
  let di = dailyCountInfo();
  if(!di.loaded){
    toast('当日次数还没核对完，正在重试…');
    if(!LIVE.recordsLoaded) await loadRecords();
    di = dailyCountInfo();
    if(!di.loaded){
      await refreshDailyCount();
      di = dailyCountInfo();
    }
  }
  const dayText = dateDisplay(ti.date);
  const timeText = ti.time || '09:00–22:00 内随机';
  const dailyText = di.loaded
    ? (di.count + ' / ' + (di.cap || '—') +
       (di.ok ? '' : '（已达上限，仅提醒）'))
    : '未知（后端会再查）';
  const missing = allReady ? [] :
    [...document.querySelectorAll('#checks .check-row.warn .clabel')]
      .map(x => x.textContent.trim());
  const warnLine = missing.length
    ? `\n未通过：${missing.join('、')}\n（后端仍会重新校验）`
    : '';
  if(!confirm(`确认提交 ${km} km @ ${pace.toFixed(2)} min/km？
提交日期：${dayText}
开始时间：${timeText}
当日次数：${dailyText}${warnLine}
提交后该时段会被回执锁定，不能重复提交。`)) return;
  LIVE.submitting = true;
  const btn = $('#submitRun');
  btn.disabled = true; btn.textContent = '提交中…';
  liveLog('开始提交', `${km} km · ${pace.toFixed(2)} /km · ${cadence} spm · ${dayText} ${timeText}`, 'info');
  const j = await liveApi('real_submit_run', {
    km: km, pace: pace, cadence: cadence, altitude: altitude,
    waypoints: route && route.length ? route : null,
    snap_route: true, use_points: !freeRunOn(), geeToken: '',
    date: ti.date, start_time: ti.time, closed: !!routeClosed
  }, 180000);
  LIVE.submitting = false;
  btn.textContent = '提交这次跑步';
  if(!liveOk(j)){
    const d = j.data || {};
    if(d.model){ _modelSeq++; LIVE.model = d.model; renderModel(); }
    const timeout = /超时/.test(String(j.message || ''));
    liveLog(timeout ? '提交结果未知' : '提交失败', j.message || '', timeout ? 'warn' : 'err');
    toast((timeout ? '提交结果未知（后台可能仍在执行，请勿重复提交）：'
                    : '提交失败：') + (j.message || ''));
    refreshChecks();
    loadReceipts();
    return;
  }
  const d = j.data || {};
  LIVE.lastSubmit = d;
  if(d.model){ _modelSeq++; LIVE.model = d.model; renderModel(); }
  // A 200 from save/record only means "accepted".  The record is not
  // guaranteed complete until get_one_record says so, and OBS upload can
  // fail independently.  Report those warnings instead of a flat success.
  const warnings = [];
  if(d.readback_error) warnings.push('回读失败：' + d.readback_error);
  if(!d.readback && !d.readback_error) warnings.push('没有回读结果');
  if(d.readback && (d.readback.complete === false || d.readback.complete === 0)) warnings.push('回读未完成');
  if(d.readback && d.readback.isCheat) warnings.push('回读标记 isCheat=' + d.readback.isCheat);
  if(d.obs_error) warnings.push('轨迹上传失败：' + d.obs_error);
  if(d.obs && d.obs.of && ((d.obs.uploaded || 0) < d.obs.of || (d.obs.verified || 0) < d.obs.of)){
    warnings.push('轨迹上传不完整：' + (d.obs.uploaded || 0) + '/' + d.obs.of +
      ' 上传，' + (d.obs.verified || 0) + '/' + d.obs.of + ' 校验');
  }
  if(d.receipt_error) warnings.push('回执记录失败：' + d.receipt_error);
  if(d.missed_points && d.missed_points.length) warnings.push('仍有点位未经过');
  if(d.daily && d.daily.source === 'unknown') warnings.push('当日次数未知');
  const summary = 'rrid=' + (d.rrid || '') + ' · ' + (d.km || km) + ' km' +
    (d.start ? ' · ' + d.start.text : '');
  if(warnings.length){
    liveLog('提交已受理（有提醒）', summary + ' · ' + warnings.join('；'), 'warn');
    toast('已提交，但有提醒：' + warnings[0]);
  }else{
    liveLog('提交成功', summary, 'ok');
    toast('提交成功：rrid ' + (d.rrid || ''));
  }
  refreshChecks();
  switchRunTab('log');
  await loadOverview();
  LIVE.daily = null;
  await loadRecords();
  LIVE.dailyCountsAt = 0;
  loadDateCounts(true);
  loadReceipts();
}

/* ---------------- wire live controls ---------------- */
let snapping = false;
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
  $('#kaStart').addEventListener('click', liveKeepaliveStart);
  $('#kaCheck').addEventListener('click', liveKeepaliveCheck);
  $('#kaStop').addEventListener('click', liveKeepaliveStop);
  $('#fetchPoints').addEventListener('click', () => loadPoints(true));
  $('#freeRun').addEventListener('change', () => {
    if(freeRunOn()) liveLog('已开启自由跑', '提交时不会拉取打卡点；学校要求必经点会被后端拒绝', 'warn');
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
  $('#routeImport').addEventListener('click', importRouteToLibrary);
  $('#routeExport').addEventListener('click', () => exportRouteGpx(LIVE.routes[LIVE.selectedRoute]));
  $('#routeExportOne').addEventListener('click', () => exportRouteGpx(LIVE.routes[LIVE.selectedRoute]));
  $('#rdGpx').addEventListener('click', () => {
    const d = LIVE.lastDetail;
    if(d) downloadGpx('记录 ' + shortDate(d.detail && d.detail.start_ms), d.track || []);
  });
  $('#rdSaveRoute').addEventListener('click', saveDetailAsRoute);
  $('#diagCopy').addEventListener('click', copyDiagnostics);
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
      if(d.route && d.route.length){ route = d.route.map(p => [p[0], p[1]]); redraw(); }
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
    const j = await liveApi('real_plan', {days: 14});
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
    toast('已按线路长度填写；继续画线会同步更新');
  });
  $('#runCheck').addEventListener('click', () => { refreshModelPreview(); refreshChecks(); toast('已重新检查'); });
  $('#submitRun').addEventListener('click', submitRun);
  $('#saveRules').addEventListener('click', saveRules);
  $('#rulesFetch').addEventListener('click', () => fetchSchoolRules({force: true}));
  $('#netSave').addEventListener('click', saveNetworkConfig);
  $('#netClear').addEventListener('click', clearNetworkConfig);
  ['inKm', 'inPace', 'inCad', 'inAlt'].forEach(id => $('#' + id).addEventListener('input', () => { updateRunDateLabel(); scheduleModelPreview(); }));
  ['inStart'].forEach(id => $('#' + id).addEventListener('input', () => { updateRunDateLabel(); scheduleModelPreview(); refreshChecks(); }));
  // The six-page UI uses the date popover (#dateBtn/#datePop), not the old
  // #inDay select.  Calling wireDatePicker() here is required; otherwise the
  // button is inert and the boot code below used to throw on a null #inDay.
  wireDatePicker();
  $('#routeSaveCurrent').addEventListener('click', saveCurrentRoute);
  $('#routeUse').addEventListener('click', () => {
    const r = LIVE.routes[LIVE.selectedRoute];
    if(!r) return;
    route = validRouteLine(r.line);
    if(route.length < 2){ toast('这条路线没有有效坐标'); return; }
    setCloseUI(!!r.closed);
    redraw();
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
    LIVE.log = []; renderLog();
  });
}

/* Boot: login gate -> local data -> real session. */
initRunDate();
liveWire();
updateRunDateLabel();
renderLog();
loadRoutes();
go(PAGES.includes(startPage) ? startPage : 'overview');
refreshDateHeader();
bootAuth();

// Local file status only; this does not probe the real server.  It keeps the
// account card in sync when the background keeper auto-relogins.
setInterval(() => {
  if(document.visibilityState !== 'hidden') loadKeepalive();
  refreshDateHeader();
}, 60000);
