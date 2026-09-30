/* Fotos (tela do trabalho e tela do cliente): "Tirar foto" e "Da galeria", várias de uma vez.
   - O próprio celular reduz cada foto antes de enviar (até 1600 px, JPEG): no 4G sobe bem mais rápido.
   - Vai uma foto por vez, e cada uma aparece na grade assim que chega. Dá pra continuar tirando
     fotos enquanto as outras sobem: elas entram na fila.
   - Se a internet cair no meio, aparece "Tentar de novo" só com as que não foram.
   - Apagar também é sem recarregar a página, então o que já estava marcado nas tarefas não se perde.
   - Nas cotações, o texto embaixo de cada foto salva sozinho enquanto a pessoa digita.
   Se este arquivo não carregar, os formulários continuam funcionando do jeito normal. */
(function () {
  'use strict';
  if (!window.fetch || !window.FormData || !window.Promise || !document.querySelector('[data-photo-set]')) return;
  document.documentElement.classList.add('js-photos');

  var MAX_SIDE = 1600, QUALITY = 0.85;
  var TEXT = {};
  try { TEXT = JSON.parse(document.getElementById('photo-text').textContent); } catch (e) { /* fica sem os textos */ }
  var ERRORS = TEXT.errors || {};

  function fill(text, values) {
    return String(text || '').replace(/\{(\w+)\}/g, function (all, key) {
      return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : all;
    });
  }

  /* Reduz a foto no celular. Se o navegador não conseguir abrir (ex.: foto HEIC num Android),
     manda a original mesmo: o servidor sabe converter. */
  function shrink(file) {
    return new Promise(function (resolve) {
      if (!/^image\/(jpeg|png|webp|heic|heif)$/i.test(file.type || '')) { resolve(file); return; }
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () {
        var w = img.naturalWidth, h = img.naturalHeight, scale = Math.min(1, MAX_SIDE / Math.max(w, h));
        if (!w || !h || (scale === 1 && file.type === 'image/jpeg' && file.size < 1500000)) {
          URL.revokeObjectURL(url); resolve(file); return;  // já é pequena
        }
        var canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(w * scale));
        canvas.height = Math.max(1, Math.round(h * scale));
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#fff';  // PNG com fundo transparente fica com fundo branco, não preto
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        if (!canvas.toBlob) { resolve(file); return; }
        canvas.toBlob(function (blob) {
          resolve(blob && (scale < 1 || blob.size < file.size) ? blob : file);
        }, 'image/jpeg', QUALITY);
      };
      img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }

  /* O que o servidor respondeu, ou o motivo de não ter chegado lá. */
  function answer(res) {
    var type = res.headers.get('Content-Type') || '';
    if (res.ok && !res.redirected && type.indexOf('application/json') === 0) return res.json();
    if (res.status === 400 || res.redirected) return { error: TEXT.expired, stop: true };  // sessão expirou
    return { error: ERRORS[res.status] || TEXT.network, retry: res.status >= 500 };
  }

  function offline() { return { error: TEXT.network, retry: true }; }

  function post(url, data) {
    return fetch(url, { method: 'POST', body: data, credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(answer).catch(offline);  // sem internet, ou a resposta veio cortada
  }

  function send(form, file) {
    return shrink(file).catch(function () { return file; }).then(function (blob) {
      var data = new FormData();
      data.append('_csrf', form.querySelector('input[name="_csrf"]').value);
      data.append('photo', blob, blob === file ? (file.name || 'foto') : (file.name || 'foto').replace(/\.[^.]*$/, '') + '.jpg');
      return post(form.action, data);
    });
  }

  /* ---------- Cada seção (Antes, Depois, galeria do cliente) tem a sua fila ---------- */

  function stateOf(box) {
    if (!box._photos) box._photos = { queue: [], busy: false, done: 0, total: 0, added: 0, left: 0, bad: 0, errors: [], retry: [] };
    return box._photos;
  }

  function maxOf(box) { return parseInt(box.getAttribute('data-max'), 10) || 12; }
  function tiles(box) { return box.querySelectorAll('[data-grid] .photo').length; }

  function refresh(box) {
    var s = box._photos, n = tiles(box), pending = s ? s.queue.length + (s.busy ? 1 : 0) : 0, max = maxOf(box);
    box.querySelector('[data-grid]').hidden = !n;
    box.querySelector('[data-empty]').hidden = n > 0 || pending > 0;
    box.querySelector('[data-upload]').hidden = n + pending >= max;
    box.querySelector('[data-full]').hidden = n < max;
    var count = box.querySelector('[data-count]');
    if (count) count.textContent = fill(TEXT.count, { done: n, total: max });
  }

  function show(box, text, kind, fraction) {
    var status = box.querySelector('[data-status]'), bar = box.querySelector('[data-bar]');
    status.hidden = !text;
    status.className = 'upload-status ' + kind;
    box.querySelector('[data-status-text]').textContent = text;
    bar.hidden = kind !== 'busy';
    bar.style.setProperty('--p', Math.max(8, Math.round((fraction || 0) * 100)) + '%');
    if (kind === 'busy') box.querySelector('[data-retry]').hidden = true;
  }

  function addTile(box, photo) {
    var tile = box.querySelector('template[data-tile]').content.firstElementChild.cloneNode(true);
    tile.querySelector('a').href = photo.full;
    tile.querySelector('img').src = photo.mini;
    [['form[data-delete]', photo['delete']], ['form[data-caption]', photo.caption]].forEach(function (pair) {
      var form = tile.querySelector(pair[0]);
      if (form && pair[1]) form.action = pair[1];
      else if (form) form.parentNode.removeChild(form);
    });
    var text = tile.querySelector('form[data-caption] textarea');
    if (text) text.value = photo.text || '';
    box.querySelector('[data-grid]').appendChild(tile);
  }

  function note(s, message) {
    if (message && s.errors.indexOf(message) < 0) s.errors.push(message);
  }

  function enqueue(box, form, files) {
    var s = stateOf(box);
    var room = Math.max(0, maxOf(box) - tiles(box) - s.queue.length - (s.busy ? 1 : 0));
    var take = files.slice(0, room);
    s.left += files.length - take.length;  // passaram do limite de fotos: nem chegam a subir
    s.queue = s.queue.concat(take);
    s.total += take.length;
    refresh(box);
    if (!s.busy) next(box, form);
  }

  function next(box, form) {
    var s = stateOf(box);
    if (!s.queue.length) { finish(box); return; }
    var file = s.queue.shift();
    s.busy = true;
    box.classList.add('busy');
    show(box, fill(TEXT.uploading, { i: s.done + 1, n: s.total }), 'busy', s.done / s.total);
    send(form, file).then(function (r) {
      try {
        s.done += 1;
        (r.photos || []).forEach(function (photo) { addTile(box, photo); s.added += 1; });
        s.left += r.over_limit || 0;
        s.bad += r.bad || 0;
        if (r.error) {
          note(s, r.error);
          if (r.retry) s.retry.push(file);
        } else if (!r.ok && !r.over_limit && !r.bad) {
          note(s, r.message);  // ex.: acabou o espaço reservado para as fotos
        }
        if (r.stop) s.queue = [];  // não adianta mandar o resto
      } finally {  // aconteça o que acontecer, a fila anda
        s.busy = false;
        refresh(box);
        next(box, form);
      }
    });
  }

  function finish(box) {
    var s = stateOf(box), max = maxOf(box), parts = [];
    if (s.added) parts.push(s.added === 1 ? TEXT.added_one : fill(TEXT.added_many, { n: s.added }));
    if (s.left) parts.push(s.left === 1 ? fill(TEXT.left_one, { max: max }) : fill(TEXT.left_many, { n: s.left, max: max }));
    if (s.bad) parts.push(s.bad === 1 ? TEXT.bad_one : fill(TEXT.bad_many, { n: s.bad }));
    parts = parts.concat(s.errors);
    var problems = s.left || s.bad || s.errors.length;
    show(box, parts.join(' '), problems ? (s.added ? 'info' : 'error') : 'ok');
    var retry = box.querySelector('[data-retry]'), again = s.retry.slice();
    retry.hidden = !again.length;
    retry.onclick = again.length ? function () {
      retry.hidden = true;
      enqueue(box, box.querySelector('form[data-upload]'), again);
    } : null;
    box._photos = null;  // a próxima leva começa do zero
    box.classList.remove('busy');
    refresh(box);
  }

  /* Escolheu foto (câmera ou galeria): já começa a enviar, sem precisar de outro botão. */
  document.addEventListener('change', function (e) {
    var input = e.target;
    if (!input.matches || !input.matches('form[data-upload] input[type="file"]')) return;
    var box = input.closest('[data-photo-set]'), files = Array.prototype.slice.call(input.files || []);
    input.value = '';  // deixa escolher a mesma foto de novo, se precisar
    if (box && files.length) enqueue(box, input.form, files);
  });

  /* Apagar sem recarregar. A pergunta "Apagar esta foto?" vem antes (no próprio formulário). */
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.matches || !form.matches('form[data-delete]') || e.defaultPrevented) return;
    var box = form.closest('[data-photo-set]'), tile = form.closest('.photo');
    if (!box || !tile) return;
    e.preventDefault();
    tile.classList.add('removing');
    post(form.action, new FormData(form)).then(function (r) {
      if (r.ok) tile.parentNode.removeChild(tile);
      else tile.classList.remove('removing');
      refresh(box);
      if (!(box._photos && box._photos.busy)) show(box, r.message || r.error, r.ok ? 'ok' : 'error');
    });
  });

  /* Texto embaixo da foto (cotações): salva sozinho um pouco depois de parar de digitar, e ao sair do campo. */
  function saveCaption(form) {
    var status = form.querySelector('[data-caption-status]');
    clearTimeout(form._timer);
    if (!form._dirty) return;
    form._dirty = false;
    form.classList.add('saving');
    status.textContent = TEXT.caption_saving || '…';
    post(form.action, new FormData(form)).then(function (r) {
      form.classList.remove('saving');
      if (r.ok) { status.textContent = TEXT.caption_saved || '✓'; }
      else { form._dirty = true; status.textContent = r.message || r.error || TEXT.network; }
    });
  }
  document.addEventListener('input', function (e) {
    var form = e.target.closest && e.target.closest('form[data-caption]');
    if (!form) return;
    form._dirty = true;
    clearTimeout(form._timer);
    form._timer = setTimeout(function () { saveCaption(form); }, 900);
  });
  document.addEventListener('focusout', function (e) {
    var form = e.target.closest && e.target.closest('form[data-caption]');
    if (form) saveCaption(form);
  });
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.matches || !form.matches('form[data-caption]')) return;
    e.preventDefault();
    form._dirty = true;
    saveCaption(form);
  });

  /* Sair da página no meio do envio cancelaria as fotos que ainda estão na fila (ou um texto por salvar). */
  window.addEventListener('beforeunload', function (e) {
    var pending = Array.prototype.some.call(document.querySelectorAll('form[data-caption]'), function (f) { return f._dirty || f.classList.contains('saving'); });
    if (pending) Array.prototype.forEach.call(document.querySelectorAll('form[data-caption]'), saveCaption);
    if (pending || document.querySelector('[data-photo-set].busy')) { e.preventDefault(); e.returnValue = TEXT.uploading || '…'; }
  });
})();
