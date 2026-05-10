(() => {
  const statusEl = document.getElementById('index-status');
  const queryEl = document.getElementById('qa-query');
  const askBtn = document.getElementById('qa-ask');
  const answerEl = document.getElementById('qa-answer');
  const fragmentsEl = document.getElementById('qa-fragments');
  const sourcesEl = document.getElementById('qa-sources');
  const STOP = new Set(['и','в','во','не','что','он','на','я','с','со','как','а','то','все','она','так','его','но','да','ты','к','у','же','вы','за','бы','по','или','ли','это','от','для']);
  let idx = { videos: [], chunks: [], error: null, updatedAt: null };
  const norm = (s) => (s || '').toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim();
  const toks = (s) => norm(s).split(' ').filter((t) => t.length > 2 && !STOP.has(t));
  const setStatus = (type, text) => { statusEl.className = `card status status-${type}`; statusEl.textContent = text; };
  const score = (c, q) => {
    const txt = norm(c.text), ttl = norm(c.title); let value = 0;
    q.forEach((t) => { if (txt.includes(t)) value += 2; if (ttl.includes(t)) value += 3; });
    return value;
  };
  function render(results) {
    sourcesEl.innerHTML = '';
    if (!results.length) {
      answerEl.textContent = 'По текущему индексу недостаточно данных для ответа.';
      fragmentsEl.textContent = 'Совпадений не найдено.';
      return;
    }
    answerEl.textContent = results.slice(0, 2).map((r) => r.text).join('\n\n');
    fragmentsEl.textContent = results.slice(0, 5).map((r, i) => `${i + 1}) ${r.text}`).join('\n\n');
    results.slice(0, 8).forEach((r) => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = Number.isFinite(r.start) ? `${r.url}&t=${r.start}s` : r.url;
      a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.textContent = `${r.title}${Number.isFinite(r.start) ? ` (${r.start}s)` : ''}`;
      li.appendChild(a); sourcesEl.appendChild(li);
    });
  }
  function ask() {
    const q = queryEl.value.trim(); if (!q) return;
    const ts = toks(q);
    const res = idx.chunks.map((c) => ({ ...c, value: score(c, ts) })).filter((x) => x.value > 0).sort((a,b) => b.value-a.value).slice(0,8);
    render(res);
  }
  askBtn.addEventListener('click', ask);
  queryEl.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } });
  fetch('./playlist-transcripts.json').then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }).then((p) => {
    if (!p || !Array.isArray(p.videos) || !Array.isArray(p.chunks)) throw new Error('Некорректная структура JSON');
    idx = p;
    if (p.error) setStatus('error', `Ошибка индекса: ${p.error}`);
    else if (!p.chunks.length) setStatus('empty', 'Индекс пуст: субтитры пока не загружены.');
    else setStatus('ok', `Индекс загружен: видео ${p.videos.length}, чанков ${p.chunks.length}, обновлён: ${p.updatedAt || 'неизвестно'}`);
    askBtn.disabled = false;
  }).catch((e) => { setStatus('error', `Ошибка загрузки индекса: ${e.message}`); answerEl.textContent = 'Невозможно ответить без индекса.'; });
})();
