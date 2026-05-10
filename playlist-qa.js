(() => {
  const el = {
    status: document.getElementById('index-status'),
    query: document.getElementById('qa-query'),
    ask: document.getElementById('qa-ask'),
    answer: document.getElementById('qa-answer'),
    fragments: document.getElementById('qa-fragments'),
    sources: document.getElementById('qa-sources'),
  };
  const STOP = new Set(['и','в','во','не','что','он','на','я','с','со','как','а','то','все','она','так','его','но','да','ты','к','у','же','вы','за','бы','по','или','ли','это','от','для','мы','они']);
  let idx = { videos: [], chunks: [], error: null, updatedAt: null };

  const norm = (s) => (s || '').toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim();
  const tokens = (s) => norm(s).split(' ').filter((x) => x.length > 2 && !STOP.has(x));

  const setStatus = (kind, text) => { el.status.className = `card status status-${kind}`; el.status.textContent = text; };
  const scoreChunk = (chunk, qTokens) => {
    const txt = norm(chunk.text), title = norm(chunk.title);
    let score = 0, overlap = 0;
    for (const t of qTokens) { if (txt.includes(t)) { score += 2; overlap += 1; } if (title.includes(t)) score += 3; }
    if (qTokens.length) score += overlap / qTokens.length;
    return score;
  };
  const withTime = (url, start) => Number.isFinite(start) ? `${url}${url.includes('?') ? '&' : '?'}t=${start}s` : url;

  function renderResults(results) {
    el.sources.innerHTML = '';
    if (!results.length) {
      el.answer.textContent = 'По текущему индексу не найдено достаточно данных для ответа. Уточните вопрос.';
      el.fragments.textContent = 'Совпадений не найдено.';
      return;
    }
    el.answer.textContent = results.slice(0, 2).map((r) => r.text).join('\n\n');
    el.fragments.textContent = results.slice(0, 6).map((r, i) => `${i + 1}) ${r.text}`).join('\n\n');

    const seen = new Set();
    for (const r of results.slice(0, 8)) {
      const key = `${r.videoId}-${r.start ?? 'none'}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = withTime(r.url, r.start); a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.textContent = `${r.title}${Number.isFinite(r.start) ? ` (${r.start}s)` : ''}`;
      li.appendChild(a); el.sources.appendChild(li);
    }
  }

  function askQuestion() {
    const query = el.query.value.trim();
    if (!query) { el.answer.textContent = 'Введите вопрос.'; return; }
    const qTokens = tokens(query);
    const ranked = idx.chunks.map((c) => ({ ...c, _score: scoreChunk(c, qTokens) })).filter((x) => x._score > 0).sort((a, b) => b._score - a._score).slice(0, 8);
    renderResults(ranked);
  }

  el.ask.addEventListener('click', askQuestion);
  el.query.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); askQuestion(); } });

  fetch('./playlist-transcripts.json').then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }).then((payload) => {
    if (!payload || !Array.isArray(payload.videos) || !Array.isArray(payload.chunks)) throw new Error('Некорректная структура playlist-transcripts.json');
    idx = payload;
    if (payload.error) setStatus('error', `Ошибка индекса: ${payload.error}`);
    else if (!payload.chunks.length) setStatus('empty', 'Индекс пуст: субтитры ещё не собраны или недоступны.');
    else setStatus('ok', `Индекс готов: видео ${payload.videos.length}, чанков ${payload.chunks.length}, обновлён ${payload.updatedAt ?? 'неизвестно'}.`);
    el.ask.disabled = false;
  }).catch((err) => { setStatus('error', `Ошибка загрузки индекса: ${err.message}`); el.answer.textContent = 'Невозможно ответить без индекса.'; });
})();
