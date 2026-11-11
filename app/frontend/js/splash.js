/* Boot splash controller.
 *
 * The construction sequence is pure CSS.  This file runs the intro screen:
 * one "进入" confirmation, then a few automatic read-out steps.  Click, Esc
 * or 跳过 gets out immediately; Enter confirms the visible button.  A saved
 * local session skips the whole intro after a short same-origin probe.  A
 * session marker stops it replaying after a soft reload.  If this file never
 * loads, the CSS failsafe hides the splash after sixty seconds.
 */
(function () {
  'use strict';

  var splash = document.getElementById('bootSplash');
  if (!splash) return;
  var loginGate = document.getElementById('loginGate');

  var KEY = 'hxp.boot.played';
  var force = /(?:^\?|&)boot=1(?:&|$)/.test(location.search);
  var off = /(?:^\?|&)boot=0(?:&|$)/.test(location.search);
  var reduced = false;
  try {
    reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (err) { /* older WebView: keep the animation */ }

  var played = false;
  try { played = sessionStorage.getItem(KEY) === '1'; } catch (err) { /* private mode */ }

  function drop() {
    splash.remove();
    /* The login gate was inert while the splash owned the window; restore it
       before handing focus back, then tell app.js the intro is over. */
    if (loginGate) loginGate.inert = false;
    try {
      document.dispatchEvent(new CustomEvent('hxp:boot-done'));
    } catch (err) { /* older WebView: the visibility poll in app.js still works */ }
  }

  function remember() {
    try { sessionStorage.setItem(KEY, '1'); } catch (err) { /* ignore */ }
  }

  if (off || reduced || (played && !force)) {
    remember();
    drop();
    return;
  }

  /* The login gate sits behind the splash; making it inert stops the login
     auto-focus from stealing the splash's focus and keeps Tab out of the
     hidden form. */
  if (loginGate) loginGate.inert = true;

  var access = document.getElementById('bootAccess');
  var action = document.getElementById('bootAction');
  var actionText = document.getElementById('bootActionText');
  var stamp = document.getElementById('bootStamp');
  var title = document.getElementById('bootTitle');
  var sub = document.getElementById('bootSub');
  var log = document.getElementById('bootLog');
  var logEn = document.getElementById('bootLogEn');
  var hint = document.getElementById('bootHint');
  var stepLabel = document.getElementById('bootStep');
  var progress = document.getElementById('bootProgress');
  var clock = document.getElementById('bootClock');
  var skip = document.getElementById('bootSkip');

  if (!access || !action || !actionText || !stamp || !title || !sub || !log ||
      !logEn || !hint || !stepLabel || !progress || !clock || !skip) {
    drop();
    return;
  }

  var STEPS = [
    {
      title: '好想跑 · 本地工作台',
      sub: 'LOCAL WORKBENCH · RUN OS',
      log: '准备画路线、核对并提交',
      logEn: 'READY WHEN YOU ARE',
      tag: '准备',
      btn: '进入',
      stamp: 'READY',
      progress: 10,
      wait: true
    },
    {
      title: '正在读取本地设置',
      sub: 'READING LOCAL PROFILE',
      log: '读取路线、参数和学校要求',
      logEn: 'INDEXING WORKBENCH',
      tag: '读取',
      progress: 40,
      hold: 700
    },
    {
      title: '正在检查登录状态',
      sub: 'CHECKING LOCAL SESSION',
      log: '检查登录凭证',
      logEn: 'CHECKING SESSION',
      tag: '检查',
      progress: 64,
      hold: 800
    },
    {
      title: '本地工作台已就绪',
      sub: 'LOCAL WORKBENCH IS READY',
      log: '准备完成',
      logEn: 'READY',
      tag: '就绪',
      progress: 88,
      hold: 700
    },
    {
      title: '欢迎使用好想跑',
      sub: 'WELCOME TO HAOXIANGPAO',
      log: '正在打开工作台',
      logEn: 'OPENING WORKBENCH',
      tag: '进入',
      progress: 100,
      hold: 900
    }
  ];

  var PANEL_AT = 1200;
  var index = -1;
  var done = false;
  var timer = null;
  var panelTimer = null;
  var clockTimer = null;

  function clearTimer() {
    if (timer) { clearTimeout(timer); timer = null; }
  }

  function setProgress(value) {
    progress.style.width = value + '%';
  }

  function hideStamp() {
    stamp.classList.remove('is-on');
    stamp.style.opacity = '0';
  }

  function showStamp(text) {
    stamp.textContent = text;
    stamp.classList.remove('is-on');
    /* force a reflow so the pop animation can run again */
    void stamp.offsetWidth;
    stamp.classList.add('is-on');
  }

  function render(i) {
    index = i;
    var step = STEPS[i];
    stepLabel.textContent = '[ 0' + (i + 1) + ' / 0' + STEPS.length + ' ] ' + step.tag;
    title.textContent = step.title;
    sub.textContent = step.sub;
    log.textContent = step.log;
    logEn.textContent = step.logEn;
    setProgress(step.progress);
    hideStamp();

    if (step.wait) {
      actionText.textContent = step.btn;
      action.hidden = false;
      hint.textContent = '按回车 / 空格，或点「进入」继续';
      try { action.focus({ preventScroll: true }); } catch (err) { action.focus(); }
    } else {
      action.hidden = true;
      hint.textContent = i === STEPS.length - 1 ? '正在打开工作台…' : '请稍候…';
    }
  }

  function later(fn, ms) {
    clearTimer();
    timer = setTimeout(fn, ms);
  }

  function next() {
    if (done) return;
    if (index + 1 >= STEPS.length) { finish(); return; }
    render(index + 1);
    var step = STEPS[index];
    if (step.wait) return;
    later(next, step.hold || 900);
  }

  function confirmStep() {
    if (done || index < 0) return;
    var step = STEPS[index];
    if (!step.wait) return;
    showStamp(step.stamp);
    later(next, 900);
  }

  function tick() {
    var d = new Date();
    clock.textContent = [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map(function (n) { return String(n).padStart(2, '0'); })
      .join(':');
  }

  /* A saved local session means there is nothing to authorise: skip the
     whole intro.  The bridge serves this page, so a same-origin probe is
     safe and does not need any new backend surface.  Any failure keeps the
     normal (already short) intro. */
  function hasLocalSession() {
    if (!window.fetch) return Promise.resolve(false);
    var timer = null;
    var opts = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"args":{}}'
    };
    try {
      var ctl = new AbortController();
      opts.signal = ctl.signal;
      timer = setTimeout(function () { ctl.abort(); }, 1500);
    } catch (err) { /* no AbortController: rely on the browser timeout */ }
    return fetch('/bridge/real_get_session', opts)
      .then(function (r) { return r.json(); })
      .then(function (j) {
        return !!(j && j.code === 200 && j.data &&
          (j.data.token_present || j.data.token) && !j.data.disabled);
      })
      .catch(function () { return false; })
      .then(function (found) {
        if (timer) clearTimeout(timer);
        return found;
      });
  }

  function showPanel() {
    if (done) return;
    tick();
    clockTimer = setInterval(tick, 1000);
    splash.classList.add('is-panel');
    render(0);
  }

  function finish() {
    if (done) return;
    done = true;
    clearTimer();
    if (panelTimer) clearTimeout(panelTimer);
    if (clockTimer) clearInterval(clockTimer);
    remember();
    splash.classList.add('is-out');
    document.removeEventListener('keydown', onKey, true);
    skip.removeEventListener('click', finish);
    action.removeEventListener('click', confirmStep);
    try { action.blur(); } catch (err) { /* ignore */ }
    setTimeout(drop, 360);
  }

  function onKey(event) {
    if (event.key === 'Tab') {
      // The splash is modal; keep Tab on its own controls instead of walking
      // into the inert login gate (or the browser chrome).
      var items = [];
      try {
        if (!action.hidden && getComputedStyle(action).visibility !== 'hidden') {
          items.push(action);
        }
      } catch (err) { /* keep going with the skip button */ }
      items.push(skip);
      if (!items.length) return;
      var at = items.indexOf(document.activeElement);
      var next = at < 0
        ? (event.shiftKey ? items.length - 1 : 0)
        : (at + (event.shiftKey ? -1 : 1) + items.length) % items.length;
      event.preventDefault();
      items[next].focus();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      finish();
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      if (document.activeElement === skip) return;
      event.preventDefault();
      if (index >= 0 && STEPS[index].wait && !action.hidden) {
        confirmStep();
      } else if (index < 0) {
        finish();
      }
    }
  }

  action.addEventListener('click', confirmStep);
  skip.addEventListener('click', finish);
  document.addEventListener('keydown', onKey, true);

  if (!force) {
    hasLocalSession().then(function (found) {
      if (found && !done) finish();
    });
  }

  panelTimer = setTimeout(showPanel, PANEL_AT);
})();
