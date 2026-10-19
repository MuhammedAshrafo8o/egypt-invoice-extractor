/* دفتر — small UI layer: command palette, loading bar, toasts, chart tooltips. No dependencies. */
(function () {
  'use strict';

  /* ------------------------------------------------ loading bar on navigation */
  var bar = document.createElement('div');
  bar.className = 'nav-progress';
  document.body.appendChild(bar);
  function startLoading() { bar.classList.remove('done'); bar.classList.add('active'); }
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[href]');
    if (!a || a.target === '_blank' || e.metaKey || e.ctrlKey || e.shiftKey) return;
    var href = a.getAttribute('href');
    if (!href || href.charAt(0) === '#' || href.indexOf('http') === 0 || href.indexOf('.xlsx') !== -1 || href.indexOf('.csv') !== -1) return;
    startLoading();
  });
  document.addEventListener('submit', function (e) {
    if (!e.defaultPrevented && !(e.target.dataset && e.target.dataset.busy)) startLoading();
  });
  window.addEventListener('pageshow', function () { bar.classList.remove('active'); });

  /* ----------------------------------------------- toasts dismiss themselves */
  document.querySelectorAll('.flash-success, .flash-info').forEach(function (el) {
    setTimeout(function () { el.classList.add('leaving'); setTimeout(function () { el.remove(); }, 300); }, 5000);
  });

  /* ------------------------------------------------------- chart tooltips */
  var tip = document.createElement('div');
  tip.className = 'chart-tip'; tip.hidden = true;
  document.body.appendChild(tip);
  function showTip(el, x, y) {
    tip.innerHTML = '';
    el.getAttribute('data-tip').split(' | ').forEach(function (line, i) {
      var d = document.createElement('div'); d.textContent = line; if (i === 0) d.className = 'tip-title'; tip.appendChild(d);
    });
    tip.hidden = false;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(Math.max(8, x - w / 2), window.innerWidth - w - 8) + 'px';
    tip.style.top = Math.max(8, y - h - 14) + 'px';
  }
  document.addEventListener('mousemove', function (e) {
    var el = e.target.closest && e.target.closest('[data-tip]');
    if (el) showTip(el, e.clientX, e.clientY); else tip.hidden = true;
  });
  document.addEventListener('focusin', function (e) {
    var el = e.target.closest && e.target.closest('[data-tip]');
    if (el) { var r = el.getBoundingClientRect(); showTip(el, r.left + r.width / 2, r.top); }
  });
  document.addEventListener('focusout', function () { tip.hidden = true; });

  /* ---------------------------------------------------- command palette */
  var palette = document.getElementById('palette');
  if (!palette) return;
  var input = palette.querySelector('input');
  var list = palette.querySelector('.palette-results');
  var actions = JSON.parse(palette.getAttribute('data-actions') || '[]');
  var items = [], active = 0, timer = null, lastQuery = null;

  function open() {
    palette.hidden = false; input.value = ''; render(actions.map(asAction), ''); input.focus();
  }
  function close() { palette.hidden = true; }
  function asAction(a) { return { href: a.href, title: a.title, sub: a.sub || '', kind: 'إجراء' }; }

  function render(results, q) {
    items = results; active = 0; list.innerHTML = '';
    if (!results.length) {
      var empty = document.createElement('li'); empty.className = 'palette-empty';
      empty.textContent = q ? 'مفيش نتايج لـ "' + q + '"' : 'اكتب اسم عميل أو مورد أو رقم فاتورة';
      list.appendChild(empty); return;
    }
    results.forEach(function (r, i) {
      var li = document.createElement('li');
      li.className = 'palette-item' + (i === 0 ? ' active' : '');
      li.setAttribute('role', 'option');
      var kind = document.createElement('span'); kind.className = 'palette-kind'; kind.textContent = r.kind;
      var main = document.createElement('span'); main.className = 'palette-main';
      var t = document.createElement('bdi'); t.className = 'palette-title'; t.textContent = r.title;
      var s = document.createElement('span'); s.className = 'palette-sub'; s.textContent = r.sub;
      main.appendChild(t); main.appendChild(s);
      li.appendChild(main); li.appendChild(kind);
      li.addEventListener('mousemove', function () { setActive(i); });
      li.addEventListener('click', function () { go(i); });
      list.appendChild(li);
    });
  }
  function setActive(i) {
    var lis = list.querySelectorAll('.palette-item');
    if (!lis.length) return;
    active = (i + lis.length) % lis.length;
    lis.forEach(function (li, k) { li.classList.toggle('active', k === active); });
    lis[active].scrollIntoView({ block: 'nearest' });
  }
  function go(i) { var r = items[i]; if (r) { close(); startLoading(); location.href = r.href; } }

  function search() {
    var q = input.value.trim();
    if (q === lastQuery) return; lastQuery = q;
    var local = actions.filter(function (a) { return !q || a.title.indexOf(q) !== -1; }).map(asAction);
    if (!q) { render(local, q); return; }
    fetch('/api/search?q=' + encodeURIComponent(q), { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (input.value.trim() !== q) return;
        var res = [];
        data.clients.forEach(function (c) { res.push({ href: '/clients/' + c.id, title: c.name, sub: c.tax_id || '', kind: 'عميل' }); });
        data.invoices.forEach(function (v) {
          res.push({ href: '/invoices/' + v.id, title: v.title, sub: v.client + ' — ' + v.date + ' — ' + v.total, kind: 'فاتورة' });
        });
        res.push({ href: '/invoices?q=' + encodeURIComponent(q), title: 'كل نتايج "' + q + '"', sub: 'في صفحة الفواتير', kind: 'بحث' });
        render(local.concat(res), q);
      });
  }

  document.addEventListener('keydown', function (e) {
    var typing = /INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName || '');
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); palette.hidden ? open() : close(); return; }
    if (e.key === '/' && !typing && palette.hidden) { e.preventDefault(); open(); return; }
    if (palette.hidden) return;
    if (e.key === 'Escape') { close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setActive(active + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(active - 1); }
    else if (e.key === 'Enter') { e.preventDefault(); go(active); }
  });
  input.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(search, 140); });
  palette.addEventListener('click', function (e) { if (e.target === palette) close(); });
  document.querySelectorAll('[data-open-palette]').forEach(function (b) { b.addEventListener('click', open); });
})();
