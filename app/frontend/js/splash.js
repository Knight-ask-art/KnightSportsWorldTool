/* Boot splash controller.
 *
 * The construction sequence is pure CSS.  This file runs the access screen:
 * two deliberate confirmations, then three automatic read-out steps.  Click,
 * Esc or SKIP gets out; Enter confirms the visible button.  A session marker
 * stops it replaying after a soft reload.  If this file never loads, the CSS
 * failsafe hides the splash after sixty seconds.
 */
(function () {
  'use strict';

  var splash = document.getElementById('bootSplash');
  if (!splash) return;

  var KEY = 'hxp.boot.played';
  var force = /(?:^\?|&)boot=1(?:&|$)/.test(location.search);
  var off = /(?:^\?|&)boot=0(?:&|$)/.test(location.search);
  var reduced = false;
  try {
    reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (err) { /* older WebView: keep the animation */ }

  var played = false;
  try { played = sessionStorage.getItem(KEY) === '1'; } catch (err) { /* private mode */ }

  function drop() { splash.remove(); }

  function remember() {
    try { sessionStorage.setItem(KEY, '1'); } catch (err) { /* ignore */ }
  }

  if (off || reduced || (played && !force)) {
    remember();
    drop();
    return;
  }

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
      title: 'ACCESS PERMISSION REQUIRED',
      sub: 'LOCAL WORKBENCH ACCESS',
      log: 'AWAITING OPERATOR CONSENT',
      logEn: 'GRANT ACCESS TO CONTINUE',
      tag: 'AUTHORIZATION',
      btn: 'GRANT ACCESS',
      stamp: 'AUTHORIZED',
      progress: 8,
      wait: true
    },
    {
      title: 'IDENTITY VERIFICATION',
      sub: 'LOCAL OPERATOR CONFIRMATION',
      log: 'MATCHING LOCAL SESSION',
      logEn: 'CONFIRM IDENTITY TO CONTINUE',
      tag: 'IDENTITY',
      btn: 'CONFIRM IDENTITY',
      stamp: 'VERIFIED',
      progress: 26,
      wait: true
    },
    {
      title: 'ACCESS REQUEST RECEIVED',
      sub: 'HANDSHAKE ESTABLISHED',
      log: 'SESSION CHANNEL OPEN',
      logEn: 'ACCESS REQUEST RECEIVED',
      tag: 'REQUEST',
      progress: 48,
      hold: 850
    },
    {
      title: 'READING LOCAL PROFILE',
      sub: 'INDEXING RUN DATA AND SETTINGS',
      log: 'READING LOCAL STORE',
      logEn: 'INDEXING WORKBENCH',
      tag: 'READ',
      progress: 70,
      hold: 950
    },
    {
      title: 'READ COMPLETE · VERIFIED',
      sub: 'LOCAL PROFILE VERIFIED',
      log: 'PERMISSION GRANTED',
      logEn: 'READ COMPLETE',
      tag: 'VERIFY',
      progress: 88,
      hold: 950
    },
    {
      title: 'WELCOME TO HAOXIANGPAO',
      sub: 'LOCAL WORKBENCH IS READY',
      log: 'STARTING WORKBENCH',
      logEn: 'WELCOME',
      tag: 'WELCOME',
      progress: 100,
      hold: 1500
    }
  ];

  var PANEL_AT = 4200;
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
    stepLabel.textContent = '[ 0' + (i + 1) + ' / 06 ] ' + step.tag;
    title.textContent = step.title;
    sub.textContent = step.sub;
    log.textContent = step.log;
    logEn.textContent = step.logEn;
    setProgress(step.progress);
    hideStamp();

    if (step.wait) {
      actionText.textContent = step.btn;
      action.hidden = false;
      hint.textContent = 'PRESS ENTER / SPACE, OR CLICK TO CONTINUE';
      try { action.focus({ preventScroll: true }); } catch (err) { action.focus(); }
    } else {
      action.hidden = true;
      hint.textContent = i === STEPS.length - 1 ? 'LAUNCHING' : 'PLEASE WAIT';
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

  panelTimer = setTimeout(showPanel, PANEL_AT);
})();
