(() => {
  const el = {
    status: document.getElementById('index-status'), query: document.getElementById('qa-query'), ask: document.getElementById('qa-ask'),
    answer: document.getElementById('qa-answer'), fragments: document.getElementById('qa-fragments'), sources: document.getElementById('qa-sources'),
  };
  const STOP = new Set(['и','в','во','не','что','он','на','я','с','со','как','а','то','все','она','так','его','но','да','ты','к','у','же','вы','за','бы','по','или','ли','это','от','для','мы','они']);
  let idx = { videos: [], chunks: [], error: null, updatedAt: null };

  const norm = (s) => (s || '').toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim();
  const tokens = (s) => norm(s).split(' ').filter((x) => x.length > 2 && !STOP.has(x));
  const withTime = (url, start) => Number.isFinite(start) ? `${url}${url.includes('?') ? '&' : '?'}t=${start}s` : url;
  const setStatus = (kind, text) => { el.status.className = `card status status-${kind}`; el.status.textContent = text; };

  function scoreChunk(chunk, qTokens) {
    const txt = norm(chunk.text), title = norm(chunk.title);
    let score = 0;
    for (const t of qTokens) {
      if (txt.includes(t)) score += 2;
      if (title.includes(t)) score += 3;
    }
    return score;
  }

  function rerankDiverse(ranked) {
    const out = [];
    const perVideo = new Map();
    for (const r of ranked) {
      const n = perVideo.get(r.videoId) || 0;
      if (n >= 1) continue;
      perVideo.set(r.videoId, n + 1);
      out.push(r);
      if (out.length >= 12) break;
    }
    return out;
  }

  function snippet(text, qTokens, maxLen = 260) {
    if (!text) return '';
    const low = text.toLowerCase();
    let pos = -1;
    for (const t of qTokens) {
      pos = low.indexOf(t.toLowerCase());
      if (pos >= 0) break;
    }
    if (pos < 0) return text.slice(0, maxLen).trim();
    return text.slice(Math.max(0, pos - 90), Math.min(text.length, pos + 170)).trim();
  }

  function buildShortAnswer(results, qTokens) {
    if (!results.length) return 'По текущему индексу не найдено достаточно данных для ответа. Уточните вопрос.';
    const askNumber = qTokens.some((t) => ['сколько', 'лет', 'возраст'].includes(t));
    if (askNumber) {
      for (const r of results.slice(0, 6)) {
        const m = (r.text || '').match(/\b(\d{1,3})\b/);
        if (m) return `Короткий ответ: вероятное значение — ${m[1]}.`;
      }
    }

    const first = snippet(results[0].text, qTokens, 220).split(/[.!?]/)[0].trim();
    const second = (results[1] ? snippet(results[1].text, qTokens, 180).split(/[.!?]/)[0].trim() : '');
    const primary = first.length >= 40 ? first : (results[0].text || '').split(/[.!?]/)[0].trim();
    if (second && second !== primary) {
      return `Короткий ответ: ${primary}. Дополнительно: ${second}.`;
    }
    return `Короткий ответ: ${primary}.`;
  }

  function renderResults(results, qTokens) {
    el.sources.innerHTML = '';
    el.answer.textContent = buildShortAnswer(results, qTokens);
    if (!results.length) return void (el.fragments.textContent = 'Совпадений не найдено.');

    el.fragments.textContent = results.slice(0, 6).map((r, i) => `${i + 1}) ${Number.isFinite(r.start) ? `[${r.start}s] ` : ''}${snippet(r.text, qTokens)}`).join('\n\n');

    const seen = new Set();
    for (const r of results) {
      if (seen.has(r.videoId)) continue;
      seen.add(r.videoId);
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = withTime(r.url, r.start);
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = `${r.title}${Number.isFinite(r.start) ? ` (${r.start}s)` : ''}`;
      li.appendChild(a);
      el.sources.appendChild(li);
      if (seen.size >= 8) break;
    }
  }

  function askQuestion() {
    const query = el.query.value.trim();
    if (!query) return void (el.answer.textContent = 'Введите вопрос.');
    const qTokens = tokens(query);
    const ranked = idx.chunks.map((c) => ({ ...c, _score: scoreChunk(c, qTokens) })).filter((x) => x._score > 0).sort((a, b) => b._score - a._score);
    renderResults(rerankDiverse(ranked), qTokens);
  }

  el.ask.addEventListener('click', askQuestion);
  el.query.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); askQuestion(); } });

  fetch('./playlist-transcripts.json').then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }).then((payload) => {
    if (!payload || !Array.isArray(payload.videos) || !Array.isArray(payload.chunks)) throw new Error('Некорректная структура playlist-transcripts.json');
    idx = payload;
    setStatus(payload.error ? 'error' : 'ok', payload.error ? 'Индекс временно недоступен. Попробуйте позже.' : `Индекс готов: видео ${payload.videos.length}, чанков ${payload.chunks.length}, обновлён ${payload.updatedAt ?? 'неизвестно'}.`);
    el.ask.disabled = false;
  }).catch((err) => { setStatus('error', `Ошибка загрузки индекса: ${err.message}`); el.answer.textContent = 'Невозможно ответить без индекса.'; });
})();
