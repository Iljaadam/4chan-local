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
