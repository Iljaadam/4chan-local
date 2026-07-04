// Click a quotelink -> toggle an inline preview of the referenced post.
// Event delegation so it also works inside fetched previews (nested quotes).
document.addEventListener('click', function (e) {
  const a = e.target.closest('a.quotelink[data-no]');
  if (!a) return;
  e.preventDefault();
  const next = a.nextElementSibling;
  if (next && next.classList.contains('qpreview')) { next.remove(); return; }
  // board/no come from data-* attributes; encode before building the URL
  const board = encodeURIComponent(a.dataset.board);
  const no = encodeURIComponent(a.dataset.no);
  fetch('/post/' + board + '/' + no).then(function (r) {
    if (!r.ok) return null;
    return r.text();
  }).then(function (html) {
    if (html === null) return;
    const div = document.createElement('div');
    div.className = 'qpreview';
    div.innerHTML = html;   // server output is escaped + sanitized server-side
    a.after(div);
  }).catch(function () {});
});

// Click an archived file/thumb -> toggle the full media inline in the current post.
// Videos use native controls, which include fullscreen in modern browsers.
document.addEventListener('click', function (e) {
  const a = e.target.closest('a.mediaToggle[data-media-url]');
  if (!a) return;
  e.preventDefault();
  const file = a.closest('.file') || a.closest('.mediaTile');
  if (!file) return;
  const existing = file.querySelector('.mediaInline');
  if (existing) {
    existing.remove();
    file.classList.remove('mediaExpanded');
    return;
  }
  const url = a.dataset.mediaUrl;
  const kind = a.dataset.mediaKind || 'image';
  const wrap = document.createElement('div');
  wrap.className = 'mediaInline';
  if (kind === 'video') {
    const v = document.createElement('video');
    v.controls = true;
    v.playsInline = true;
    v.preload = 'metadata';
    v.src = url;
    wrap.appendChild(v);
  } else {
    const img = document.createElement('img');
    img.src = url;
    img.loading = 'eager';
    img.alt = '';
    wrap.appendChild(img);
  }
  file.classList.add('mediaExpanded');
  file.appendChild(wrap);
});

// Media thread view: stable thumbnail grid + one viewing window navigated by
// buttons or ArrowLeft/ArrowRight. The grid does not resize when media opens.
(function () {
  let overlay = null;
  let items = [];
  let idx = 0;

  function buildOverlay() {
    const root = document.createElement('div');
    root.className = 'mediaViewer';
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.innerHTML = [
      '<div class="mediaViewerChrome">',
      '  <button type="button" class="mediaViewerClose" title="Close">x</button>',
      '  <a class="mediaViewerPost" href="#">Post</a>',
      '  <span class="mediaViewerCount"></span>',
      '</div>',
      '<button type="button" class="mediaViewerNav mediaViewerPrev" title="Previous">Prev</button>',
      '<div class="mediaViewerStage"></div>',
      '<button type="button" class="mediaViewerNav mediaViewerNext" title="Next">Next</button>'
    ].join('');
    document.body.appendChild(root);
    root.addEventListener('click', function (e) {
      if (e.target === root || e.target.closest('.mediaViewerClose')) closeViewer();
      if (e.target.closest('.mediaViewerPrev')) show(idx - 1);
      if (e.target.closest('.mediaViewerNext')) show(idx + 1);
    });
    return root;
  }

  function show(nextIdx) {
    if (!items.length) return;
    idx = (nextIdx + items.length) % items.length;
    const item = items[idx];
    const stage = overlay.querySelector('.mediaViewerStage');
    stage.replaceChildren();
    if (item.kind === 'video') {
      const v = document.createElement('video');
      v.controls = true;
      v.autoplay = true;
      v.playsInline = true;
      v.preload = 'metadata';
      v.src = item.url;
      stage.appendChild(v);
    } else {
      const img = document.createElement('img');
      img.src = item.url;
      img.alt = '';
      stage.appendChild(img);
    }
    overlay.querySelector('.mediaViewerCount').textContent =
      (idx + 1) + ' / ' + items.length;
    const post = overlay.querySelector('.mediaViewerPost');
    post.href = item.postUrl || '#';
    post.textContent = item.label || 'Post';
  }

  function openViewer(grid, startLink) {
    const links = Array.prototype.slice.call(
      grid.querySelectorAll('a.mediaViewerOpen[data-media-url]')
    );
    items = links.map(function (a) {
      return {
        url: a.dataset.mediaUrl,
        kind: a.dataset.mediaKind || 'image',
        postUrl: a.dataset.postUrl,
        label: a.dataset.label
      };
    });
    idx = Math.max(0, links.indexOf(startLink));
    overlay = overlay || buildOverlay();
    overlay.classList.add('open');
    document.body.classList.add('mediaViewerActive');
    show(idx);
  }

  function closeViewer() {
    if (!overlay) return;
    overlay.classList.remove('open');
    const stage = overlay.querySelector('.mediaViewerStage');
    if (stage) stage.replaceChildren();
    document.body.classList.remove('mediaViewerActive');
  }

  document.addEventListener('click', function (e) {
    const a = e.target.closest('a.mediaViewerOpen[data-media-url]');
    if (!a) return;
    const grid = a.closest('.mediaThreadGrid');
    if (!grid) return;
    e.preventDefault();
    openViewer(grid, a);
  });

  document.addEventListener('keydown', function (e) {
    if (!overlay || !overlay.classList.contains('open')) return;
    if (e.key === 'Escape') closeViewer();
    if (e.key === 'ArrowLeft') show(idx - 1);
    if (e.key === 'ArrowRight') show(idx + 1);
  });
})();

// Pin toggle: POST/DELETE /api/pin to keep a target past 4chan's 404 (or release
// it back to the retention GC). The button's data-kind picks the granularity —
// thread (data-no), post (data-post), or file (data-md5). Flip UI only on server OK.
const PIN_TITLES = {
  thread: ['Pin — keep this thread past 404', 'Unpin thread'],
  post: ['Pin — keep this post past 404', 'Unpin post'],
  file: ['Pin — keep this file past 404', 'Unpin file'],
};
document.addEventListener('click', function (e) {
  const btn = e.target.closest('.pinBtn');
  if (!btn) return;
  e.preventDefault();
  if (btn.disabled) return;
  const kind = btn.dataset.kind || 'thread';
  const payload = { kind: kind };
  if (kind === 'thread') {
    payload.board = btn.dataset.board;
    payload.thread_no = parseInt(btn.dataset.no, 10);
  } else if (kind === 'post') {
    payload.board = btn.dataset.board;
    payload.post_no = parseInt(btn.dataset.post, 10);
  } else {
    payload.file_md5 = btn.dataset.md5;
  }
  const pinned = btn.classList.contains('pinned');
  btn.disabled = true;
  fetch('/api/pin', {
    method: pinned ? 'DELETE' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(function (r) {
    if (!r.ok) throw new Error('pin failed');
    return r.json();
  }).then(function (d) {
    btn.classList.toggle('pinned', d.pinned);
    btn.setAttribute('aria-pressed', d.pinned ? 'true' : 'false');
    const t = PIN_TITLES[kind] || PIN_TITLES.thread;
    btn.title = d.pinned ? t[1] : t[0];
  }).catch(function () {
    btn.classList.add('pinerr');
  }).finally(function () {
    btn.disabled = false;
  });
});

// Catalog: client-side sort + filter of the already-rendered thread cards,
// mirroring 4chan's catalog controls. Only runs on the catalog page.
(function () {
  const grid = document.getElementById('threads');
  const sortSel = document.getElementById('catalogSort');
  const filterBox = document.getElementById('catalogFilter');
  if (!grid || !sortSel || !filterBox) return;
  const cards = Array.prototype.slice.call(grid.querySelectorAll('.threadcardwrap'));
  const bump = cards.slice();  // server order == bump order

  function apply() {
    const mode = sortSel.value;
    let order = bump;
    if (mode !== 'bump') {
      const key = mode === 'replies' ? 'replies' : mode === 'images' ? 'images' : 'age';
      order = bump.slice().sort(function (a, b) {
        return (+b.dataset[key]) - (+a.dataset[key]);  // desc
      });
    }
    order.forEach(function (c) { grid.appendChild(c); });

    const term = filterBox.value.trim().toLowerCase();
    cards.forEach(function (c) {
      c.style.display = (!term || c.dataset.text.indexOf(term) !== -1) ? '' : 'none';
    });
  }
  sortSel.addEventListener('change', apply);
  filterBox.addEventListener('input', apply);
})();
