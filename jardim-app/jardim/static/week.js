/* Grade da semana: arrastar um trabalho agendado pra outro dia ou horário.
   - mouse: segura e arrasta; celular: segura o bloco um instante (senão a página rola normal);
   - o bloco "fantasma" segue o dedo, e um retângulo tracejado mostra onde ele vai cair (de meia em meia hora);
   - ao soltar, o servidor confere (trabalho agendado, ninguém da equipe em dois lugares) e a página recarrega;
     se não puder, a mensagem aparece em cima da grade e o bloco volta pro lugar. */
(function () {
  'use strict';
  var grid = document.getElementById('week-grid'), status = document.getElementById('week-status');
  var csrf = document.querySelector('input[name="_csrf"]');
  if (!grid || !csrf || !window.fetch || !window.PointerEvent) return;
  var TEXT = {};
  try { TEXT = JSON.parse(document.getElementById('week-text').textContent); } catch (e) { /* fica sem os textos */ }
  var SLOT = 30, rows = parseInt(grid.getAttribute('data-rows'), 10) || 20;
  var first = grid.getAttribute('data-first').split(':');
  var firstMin = parseInt(first[0], 10) * 60 + parseInt(first[1], 10);

  function say(message) { status.textContent = message || ''; status.hidden = !message; }
  function hhmm(min) { return ('0' + Math.floor(min / 60)).slice(-2) + ':' + ('0' + (min % 60)).slice(-2); }

  var drag = null, holdTimer = null, suppressClick = false;

  function cancelHold() { if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; } }
  function blockTouch(e) { e.preventDefault(); }

  function start(block, e) {
    var rect = block.getBoundingClientRect();
    var ghost = block.cloneNode(true);
    ghost.classList.add('week-ghost');
    ghost.classList.remove('dragging');
    ghost.style.width = rect.width + 'px';
    ghost.style.height = rect.height + 'px';
    ghost.style.left = rect.left + 'px';
    ghost.style.top = rect.top + 'px';
    document.body.appendChild(ghost);
    block.classList.add('dragging');
    document.body.classList.add('week-dragging');
    document.addEventListener('touchmove', blockTouch, { passive: false });
    var cols = Array.prototype.map.call(grid.querySelectorAll('.week-col'), function (col) {
      return { el: col, date: col.getAttribute('data-date'), rect: col.getBoundingClientRect() };
    });
    drag = { block: block, ghost: ghost, dx: e.clientX - rect.left, dy: e.clientY - rect.top, cols: cols,
             span: parseInt(block.getAttribute('data-span'), 10) || 2, target: null, drop: null,
             fromDate: block.closest('.week-col').getAttribute('data-date'), fromTime: block.getAttribute('data-start') };
    say('');
  }

  function move(e) {
    var d = drag;
    d.ghost.style.left = (e.clientX - d.dx) + 'px';
    d.ghost.style.top = (e.clientY - d.dy) + 'px';
    var col = null;
    for (var i = 0; i < d.cols.length; i++) {
      var r = d.cols[i].rect;
      if (e.clientX >= r.left && e.clientX < r.right) { col = d.cols[i]; break; }
    }
    if (!col) { clearDrop(); return; }
    var rowH = col.rect.height / rows;
    var row = Math.floor((e.clientY - d.dy - col.rect.top + rowH / 2) / rowH);  /* o topo do bloco decide a linha */
    row = Math.max(0, Math.min(rows - d.span, row));
    var time = hhmm(firstMin + row * SLOT);
    if (d.target && d.target.date === col.date && d.target.time === time) return;
    clearDrop();
    var drop = document.createElement('span');
    drop.className = 'week-drop';
    drop.style.setProperty('--row', row + 1);
    drop.style.setProperty('--span', d.span);
    drop.textContent = time;
    col.el.appendChild(drop);
    d.target = { date: col.date, time: time };
    d.drop = drop;
  }

  function clearDrop() {
    if (drag && drag.drop) { drag.drop.remove(); drag.drop = null; }
    if (drag) drag.target = null;
  }

  function finish(commit) {
    var d = drag;
    if (!d) return;
    drag = null;
    document.removeEventListener('touchmove', blockTouch);
    document.body.classList.remove('week-dragging');
    d.ghost.remove();
    if (d.drop) d.drop.remove();
    var target = d.target;
    if (!commit || !target || (target.date === d.fromDate && target.time === d.fromTime)) {
      d.block.classList.remove('dragging');
      return;
    }
    say(TEXT.moving || '');
    var body = new FormData();
    body.append('_csrf', csrf.value);
    body.append('date', target.date);
    body.append('time', target.time);
    fetch(d.block.getAttribute('data-move'), { method: 'POST', body: body, credentials: 'same-origin',
                                                headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (res.ok && res.data.ok) { location.reload(); return; }
        d.block.classList.remove('dragging');
        say((res.data && res.data.message) || TEXT.failed);
      })
      .catch(function () { d.block.classList.remove('dragging'); say(TEXT.failed); });
  }

  grid.addEventListener('pointerdown', function (e) {
    var block = e.target.closest('.week-block[data-move]');
    if (!block || drag || (e.pointerType === 'mouse' && e.button !== 0)) return;
    var sx = e.clientX, sy = e.clientY, pointerId = e.pointerId, armed = false;
    function onMove(ev) {
      if (ev.pointerId !== pointerId) return;
      if (drag) { move(ev); return; }
      var far = Math.abs(ev.clientX - sx) > 6 || Math.abs(ev.clientY - sy) > 6;
      if (e.pointerType === 'mouse') {
        if (far) { armed = true; suppressClick = true; start(block, ev); move(ev); }
      } else if (far && !armed) {
        cancelHold(); cleanup();  /* o dedo deslizou antes de segurar: é rolagem, não arrasto */
      }
    }
    function onUp(ev) {
      if (ev.pointerId !== pointerId) return;
      cancelHold();
      cleanup();
      if (drag) finish(true);
    }
    function onCancel(ev) { if (ev.pointerId !== pointerId) return; cancelHold(); cleanup(); if (drag) finish(false); }
    function cleanup() {
      document.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerup', onUp);
      document.removeEventListener('pointercancel', onCancel);
    }
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
    document.addEventListener('pointercancel', onCancel);
    if (e.pointerType !== 'mouse') {
      holdTimer = setTimeout(function () {
        holdTimer = null; armed = true; suppressClick = true;
        start(block, e); move(e);
      }, 350);
    }
  });
  grid.addEventListener('click', function (e) {
    if (suppressClick && e.target.closest('.week-block')) { e.preventDefault(); suppressClick = false; }
  }, true);
  grid.addEventListener('dragstart', function (e) { if (e.target.closest('.week-block[data-move]')) e.preventDefault(); });
})();
