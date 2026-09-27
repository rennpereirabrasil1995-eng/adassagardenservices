/* Comentário com anexos (área da empresa e página do trabalho), como num chat:
   - "Anexar foto ou arquivo" junta o que foi escolhido numa fileira (miniatura da foto, nome do documento),
     com um × pra tirar antes de enviar; dá pra escolher mais de uma vez;
   - as fotos são reduzidas no próprio celular (até 1600 px) antes de subir: várias fotos grandes cabem num envio;
   - o texto e os anexos vão juntos, e a página recarrega na conversa.
   Se este arquivo não carregar, o formulário funciona do jeito normal (o servidor reduz as fotos). */
(function () {
  'use strict';
  if (!window.fetch || !window.FormData || !window.Promise || !window.URL) return;
  var MAX_SIDE = 1600, QUALITY = 0.85;
  var TEXT = {};
  try { TEXT = JSON.parse(document.getElementById('comment-text').textContent); } catch (e) { /* fica sem os textos */ }

  function isPhoto(file) { return /^image\//i.test(file.type || '') || /\.(heic|heif)$/i.test(file.name || ''); }

  function sizeText(n) { return n < 1048576 ? Math.max(1, Math.round(n / 1024)) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; }

  /* Reduz a foto no celular. Se o navegador não abrir (ex.: HEIC num Android), vai a original: o servidor converte. */
  function shrink(file) {
    return new Promise(function (resolve) {
      if (!/^image\/(jpeg|png|webp|heic|heif)$/i.test(file.type || '')) { resolve(file); return; }
      var url = URL.createObjectURL(file), img = new Image();
      img.onload = function () {
        var w = img.naturalWidth, h = img.naturalHeight, scale = Math.min(1, MAX_SIDE / Math.max(w, h));
        if (!w || !h || (scale === 1 && file.type === 'image/jpeg' && file.size < 1500000)) { URL.revokeObjectURL(url); resolve(file); return; }
        var canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(w * scale));
        canvas.height = Math.max(1, Math.round(h * scale));
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#fff';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        if (!canvas.toBlob) { resolve(file); return; }
        canvas.toBlob(function (blob) { resolve(blob && (scale < 1 || blob.size < file.size) ? blob : file); }, 'image/jpeg', QUALITY);
      };
      img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('form[data-comment-form]'), function (form) {
    var input = form.querySelector('input[type="file"]'), list = form.querySelector('[data-attach-list]');
    var status = form.querySelector('[data-attach-status]'), button = form.querySelector('button[type="submit"]');
    var text = form.querySelector('textarea'), max = parseInt(form.getAttribute('data-max'), 10) || 6;
    var picked = [];

    function say(message) { status.textContent = message || ''; status.hidden = !message; }

    function render() {
      Array.prototype.forEach.call(list.querySelectorAll('img'), function (img) { URL.revokeObjectURL(img.src); });
      list.innerHTML = '';
      list.hidden = !picked.length;
      picked.forEach(function (file, i) {
        var item = document.createElement('li');
        item.className = 'attach-item';
        if (isPhoto(file) && /^image\/(jpeg|png|webp|gif)$/i.test(file.type || '')) {
          var img = document.createElement('img');
          img.src = URL.createObjectURL(file);
          img.alt = file.name || '';
          item.classList.add('is-photo');
          item.appendChild(img);
        } else {
          var name = document.createElement('span');
          name.className = 'attach-name';
          name.textContent = file.name || '?';
          var size = document.createElement('small');
          size.textContent = sizeText(file.size || 0);
          item.appendChild(name);
          item.appendChild(size);
        }
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'attach-del';
        del.textContent = '×';
        del.setAttribute('aria-label', (TEXT.remove || '×') + ': ' + (file.name || ''));
        del.addEventListener('click', function () { picked.splice(i, 1); render(); say(''); });
        item.appendChild(del);
        list.appendChild(item);
      });
    }

    input.addEventListener('change', function () {
      var chosen = Array.prototype.slice.call(input.files || []);
      input.value = '';  // deixa escolher de novo (e o arquivo não vai em dobro no envio)
      var room = max - picked.length;
      picked = picked.concat(chosen.slice(0, Math.max(0, room)));
      say(chosen.length > room ? String(TEXT.too_many || '').replace('{n}', max) : '');
      render();
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (!picked.length && !text.value.trim()) { say(TEXT.empty); text.focus(); return; }
      button.disabled = true;
      say(TEXT.sending);
      Promise.all(picked.map(function (file) { return isPhoto(file) ? shrink(file).catch(function () { return file; }) : Promise.resolve(file); }))
        .then(function (blobs) {
          var data = new FormData();
          data.append('_csrf', form.querySelector('input[name="_csrf"]').value);
          data.append('body', text.value);
          blobs.forEach(function (blob, i) {
            var name = picked[i].name || 'foto.jpg';
            data.append('files', blob, blob === picked[i] ? name : name.replace(/\.[^.]*$/, '') + '.jpg');
          });
          /* redirect "manual": o aviso ("Comentário enviado") fica pra página que abre em seguida */
          return fetch(form.action, { method: 'POST', body: data, credentials: 'same-origin', redirect: 'manual' });
        })
        .then(function (res) {
          if (res.status === 413) throw new Error(TEXT.too_big);
          if (res.type !== 'opaqueredirect' && !res.ok) throw new Error(TEXT.failed);
          var done = form.getAttribute('data-done'), path = done.split('#')[0];
          if (location.pathname + location.search === path) {
            location.hash = done.split('#')[1] || '';
            location.reload();
          } else {
            location.href = done;
          }
        })
        .catch(function (err) {
          button.disabled = false;
          say(err && err.message && err.message !== 'Failed to fetch' && err.message.indexOf('NetworkError') < 0 ? err.message : TEXT.failed);
        });
    });
  });
})();
