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

/* ------------------------------------------------ uploads: smaller photos, one file per request */
(function () {
  'use strict';
  var MAX_SIDE = 2400, MIN_BYTES = 1200000;

  // Phone photos are 3-8 MB; 2400 px JPEG is plenty to read an invoice and uploads much faster.
  function shrink(file) {
    if (!/^image\/(jpeg|png|webp)$/.test(file.type) || file.size < MIN_BYTES || !window.createImageBitmap) {
      return Promise.resolve(file);
    }
    return createImageBitmap(file, { imageOrientation: 'from-image' }).then(function (bmp) {
      var scale = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
      var canvas = document.createElement('canvas');
      canvas.width = Math.round(bmp.width * scale); canvas.height = Math.round(bmp.height * scale);
      canvas.getContext('2d').drawImage(bmp, 0, 0, canvas.width, canvas.height);
      return new Promise(function (resolve) {
        canvas.toBlob(function (blob) {
          if (!blob || blob.size >= file.size) return resolve(file);
          resolve(new File([blob], file.name.replace(/\.[^.]+$/, '') + '.jpg', { type: 'image/jpeg' }));
        }, 'image/jpeg', 0.85);
      });
    }).catch(function () { return file; });
  }

  function replaceFiles(input, files) {
    try {
      var dt = new DataTransfer();
      files.forEach(function (f) { dt.items.add(f); });
      input.files = dt.files;
      return true;
    } catch (e) { return false; }
  }

  function busy(text, sub) {
    var b = document.getElementById('busy');
    if (!b) return;
    document.getElementById('busy-text').textContent = text;
    document.getElementById('busy-sub').textContent = sub || '';
    b.hidden = false;
  }

  // Shrink the photos in a file input, then submit its form normally.
  window.daftarSubmitWithFiles = function (form, input, submitButton) {
    var files = Array.prototype.slice.call(input.files);
    busy('بيجهّز الصور…');
    return Promise.all(files.map(shrink)).then(function (small) {
      replaceFiles(input, small);
      submitButton.click();
    });
  };

  // Serverless hosts cap one request at a few MB: send the files one by one, then reload.
  window.daftarUploadEach = function (form, input) {
    var files = Array.prototype.slice.call(input.files);
    var batch = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    var messages = [], sent = 0, queued = 0;
    function next(i) {
      if (i >= files.length) return Promise.resolve();
      busy('بيرفع ' + (i + 1) + ' من ' + files.length + '…', 'القراية بتبدأ أول ما الرفع يخلص، وبتكمل في الخلفية.');
      return shrink(files[i]).then(function (f) {
        var data = new FormData();
        data.append('csrf', form.elements.csrf.value);
        data.append('period', form.elements.period.value);
        data.append('batch', batch);
        if (form.elements.split_pages && form.elements.split_pages.checked) data.append('split_pages', '1');
        data.append('files', f, f.name);
        return fetch(form.action, { method: 'POST', body: data, headers: { Accept: 'application/json' }, credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : { failed: [f.name + ': الرفع فشل (' + r.status + ')'] }; })
          .catch(function () { return { failed: [f.name + ': الرفع فشل — اتأكد من النت'] }; })
          .then(function (res) {
            sent += 1; queued += res.queued || 0;
            ['notes', 'skipped', 'failed'].forEach(function (k) {
              (res[k] || []).forEach(function (m) { messages.push({ kind: k === 'failed' ? 'error' : 'info', text: m }); });
            });
          });
      }).then(function () { return next(i + 1); });
    }
    return next(0).then(function () {
      if (queued) {
        messages.unshift({ kind: 'success', text: 'اترفع ' + queued + ' فاتورة، وبيتقروا دلوقتي في الخلفية — تقدر تكمّل شغلك، والفواتير بتظهر هنا أول بأول.' });
        fetch('/jobs/kick', { method: 'POST', credentials: 'same-origin', keepalive: true }).catch(function () {});
      }
      try { sessionStorage.setItem('daftar-flash', JSON.stringify(messages)); } catch (e) {}
      location.reload();
    });
  };

  // Messages carried over a reload by the uploader.
  try {
    var saved = JSON.parse(sessionStorage.getItem('daftar-flash') || '[]');
    sessionStorage.removeItem('daftar-flash');
    if (saved.length) {
      var main = document.querySelector('main.main');
      var box = document.createElement('div'); box.className = 'flashes';
      saved.forEach(function (m) {
        var d = document.createElement('div'); d.className = 'flash flash-' + m.kind; d.setAttribute('role', 'status');
        var span = document.createElement('span'); span.textContent = m.text; d.appendChild(span);
        var x = document.createElement('button'); x.type = 'button'; x.setAttribute('data-dismiss', ''); x.textContent = '×';
        d.appendChild(x); box.appendChild(d);
      });
      if (main) main.insertBefore(box, main.firstChild);
    }
  } catch (e) {}
})();

// Phones: tables marked .cards turn into one card per row; each cell borrows its column title.
(function () {
  document.querySelectorAll('table.cards').forEach(function (table) {
    var heads = Array.prototype.map.call(table.querySelectorAll('thead th'), function (th) { return th.textContent.trim(); });
    if (!heads.length) return;
    table.querySelectorAll('tbody tr').forEach(function (tr) {
      Array.prototype.forEach.call(tr.children, function (td, i) {
        if (heads[i] && !td.hasAttribute('data-label')) td.setAttribute('data-label', heads[i]);
        var text = td.textContent.trim();
        if ((text === '' || text === '—') && !td.querySelector('input')) td.classList.add('c-empty');
      });
    });
  });
  // close the account menu when tapping elsewhere
  document.addEventListener('click', function (e) {
    document.querySelectorAll('details.account[open]').forEach(function (d) { if (!d.contains(e.target)) d.open = false; });
  });
})();
