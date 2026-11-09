/* In-app announcement renderer.
 *
 * Content lives in `replica/application/announcements.py` and is returned by
 * the `real_get_announcements` bridge.  Publishing a new notice therefore
 * only needs a catalog entry (plus an image under `frontend/assets/`), not a
 * change here or in `index.html`.
 *
 * Seen versions are stored per id in `data/ui_state.json`:
 *     "announcement_seen": {"qq-group-2026-11": 1}
 * Bump `version` in the catalog to show an edited notice again.
 */
(function(){
  'use strict';

  const state = {
    items: [],
    seen: {},
    current: null,
    focus: null,
    retry: null,
    wired: false
  };

  const $ = sel => document.querySelector(sel);
  const api = (name, args, timeout) =>
    (typeof liveApi === 'function'
      ? liveApi(name, args, timeout)
      : Promise.resolve({code: 500, message: 'bridge missing'}));

  function isBlocked(){
    for(const sel of ['#bootSplash', '#onboard', '#disclaimerModal',
                      '#updateDlg', '#dataPreview', '#postSubmit',
                      '#replayModal']){
      const el = $(sel);
      if(el && !el.hidden) return true;
    }
    return false;
  }

  function ensureModal(){
    let modal = $('#announcementModal');
    if(modal) return modal;
    modal = document.createElement('div');
    modal.className = 'onboard';
    modal.id = 'announcementModal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'announcementTitle');
    modal.hidden = true;
    modal.innerHTML = `
      <div class="onboard-veil" data-ann-close></div>
      <section class="onboard-card announcement-card">
        <header class="onboard-head">
          <span class="tag ok" id="announcementKicker">公告</span>
          <button type="button" class="onboard-x" id="announcementClose" aria-label="关闭公告">×</button>
        </header>
        <div class="onboard-body announcement-body">
          <h2 id="announcementTitle"></h2>
          <p id="announcementText"></p>
          <div class="announcement-image-wrap" id="announcementImageWrap" hidden>
            <img id="announcementImage" alt="">
          </div>
          <div class="announcement-meta-row" id="announcementMetaRow" hidden>
            <span id="announcementMeta"></span>
            <button type="button" class="mini-link" id="announcementCopy" hidden></button>
          </div>
        </div>
        <footer class="onboard-foot">
          <label class="onboard-never">
            <input type="checkbox" id="announcementNever" checked>
            <span id="announcementDismiss">下次不再自动显示</span>
          </label>
          <div class="onboard-actions">
            <button type="button" class="btn small primary" id="announcementOk">知道了</button>
          </div>
        </footer>
      </section>`;
    document.body.appendChild(modal);

    $('#announcementClose').addEventListener('click', () => close(true));
    $('#announcementOk').addEventListener('click', () => close(true));
    modal.querySelector('[data-ann-close]').addEventListener(
      'click', () => close(true));
    $('#announcementCopy').addEventListener('click', async () => {
      const value = state.current && state.current.copy;
      if(!value) return;
      const ok = (typeof copyPlainText === 'function')
        ? await copyPlainText(value) : false;
      if(typeof toast === 'function')
        toast(ok ? '已复制' : '复制失败，请手动选择');
    });
    modal.addEventListener('keydown', ev => {
      if(ev.key === 'Escape'){ ev.preventDefault(); close(true); }
      else if(ev.key === 'Tab') trapTab(modal, ev);
    });
    return modal;
  }

  function trapTab(modal, ev){
    const nodes = [...modal.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), a[href]')];
    if(!nodes.length) return;
    const first = nodes[0], last = nodes[nodes.length - 1];
    if(ev.shiftKey && document.activeElement === first){
      ev.preventDefault(); last.focus();
    }else if(!ev.shiftKey && document.activeElement === last){
      ev.preventDefault(); first.focus();
    }
  }

  function safeImage(value){
    return /^assets\/[A-Za-z0-9._-]+$/.test(String(value || ''));
  }

  function render(item){
    $('#announcementKicker').textContent = item.kicker || '公告';
    $('#announcementTitle').textContent = item.title || '公告';
    const text = $('#announcementText');
    text.textContent = item.body || '';
    text.hidden = !item.body;

    const wrap = $('#announcementImageWrap');
    const image = $('#announcementImage');
    if(item.image && safeImage(item.image)){
      image.src = item.image;
      image.alt = item.image_alt || item.title || '公告图片';
      wrap.hidden = false;
    }else{
      image.removeAttribute('src');
      wrap.hidden = true;
    }

    const row = $('#announcementMetaRow');
    const meta = $('#announcementMeta');
    const copy = $('#announcementCopy');
    meta.textContent = item.meta || '';
    copy.textContent = item.copy_label || '复制';
    copy.hidden = !item.copy;
    row.hidden = !(item.meta || item.copy);

    $('#announcementDismiss').textContent =
      item.dismiss || '下次不再自动显示';
    $('#announcementOk').textContent = item.ok || '知道了';
  }

  function open(item){
    if(!item) return;
    state.current = item;
    const modal = ensureModal();
    if(!modal.hidden) return;
    render(item);
    state.focus = document.activeElement;
    modal.hidden = false;
    setTimeout(() => {
      const ok = $('#announcementOk');
      if(ok) ok.focus();
    }, 30);
  }

  function markSeen(item, version){
    if(!item) return;
    if(version > 0) state.seen[item.id] = version;
    else delete state.seen[item.id];
    const patch = {};
    patch[item.id] = version;
    api('real_set_ui_state', {announcement_seen: patch}, 8000)
      .catch(() => {});
  }

  function close(remember){
    const modal = $('#announcementModal');
    if(!modal || modal.hidden) return;
    modal.hidden = true;
    const never = $('#announcementNever');
    const keep = remember === true
      ? !!(never && never.checked)
      : (remember === false ? false : null);
    if(remember !== null && state.current){
      // 0 removes the id, so "don't show again" unchecked really shows next
      // launch; keeping the version stops the popup for this content.
      markSeen(state.current, keep ? (state.current.version || 1) : 0);
    }
    if(keep && typeof toast === 'function')
      toast('已关闭自动显示；可在「设置 → 关于」重新打开');
    if(state.focus && state.focus.focus){
      try{ state.focus.focus(); }catch(e){}
    }
  }

  async function load(){
    const j = await api('real_get_announcements', {}, 8000);
    if(!j || j.code !== 200 || !j.data) return null;
    state.items = Array.isArray(j.data.items) ? j.data.items : [];
    state.seen = (j.data.seen && typeof j.data.seen === 'object')
      ? j.data.seen : {};
    return state.items;
  }

  function unseen(item){
    return (Number(state.seen[item.id]) || 0) <
           (Number(item.version) || 0);
  }

  async function maybeShow(){
    if(typeof hasSession !== 'function' || !hasSession()) return;
    if(isBlocked()){
      clearTimeout(state.retry);
      state.retry = setTimeout(maybeShow, 800);
      return;
    }
    const items = await load();
    if(!items) return;
    // Re-check after the bridge await: onboarding or the legal gate may have
    // opened while this request was in flight.
    if(isBlocked()){
      clearTimeout(state.retry);
      state.retry = setTimeout(maybeShow, 800);
      return;
    }
    const item = items.find(unseen);
    if(item) open(item);
  }

  async function openById(id){
    if(!state.items.length) await load();
    let item = state.items.find(x => x.id === id);
    if(!item){
      const items = await load();
      item = (items || []).find(x => x.id === id);
    }
    if(item) open(item);
    else if(typeof toast === 'function') toast('公告不存在或已下线');
  }

  function wire(){
    document.querySelectorAll('[data-announcement-id]').forEach(btn => {
      if(btn.dataset.announcementBound === '1') return;
      btn.dataset.announcementBound = '1';
      btn.addEventListener('click', () => openById(btn.dataset.announcementId));
    });
    state.wired = true;
  }

  window.Announcements = { maybeShow, openById, wire };
})();
