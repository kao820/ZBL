(function () {
  const statusEl = document.getElementById("qa-status");
  const queryEl = document.getElementById("qa-query");
  const askBtn = document.getElementById("qa-ask");
  const answerEl = document.getElementById("qa-answer");
  const sourcesEl = document.getElementById("qa-sources");

  if (!statusEl || !queryEl || !askBtn || !answerEl || !sourcesEl) {
    return;
  }

  const STOP_WORDS = new Set([
    "и","в","во","не","что","он","на","я","с","со","как","а","то","все","она","так","его","но","да",
    "ты","к","у","же","вы","за","бы","по","только","ее","мне","было","вот","от","меня","еще","нет",
    "о","из","ему","теперь","когда","даже","ну","вдруг","ли","если","уже","или","ни","быть","был","него",
    "до","вас","нибудь","опять","уж","вам","ведь","там","потом","себя","ничего","ей","может","они","тут",
    "где","есть","надо","ней","для","мы","тебя","их","чем","была","сам","чтоб","без","будто","чего","раз"
  ]);

  let chunks = [];
  let isIndexReady = false;

  function normalize(text) {
    return (text || "")
      .toLowerCase()
      .replace(/[^\p{L}\p{N}\s]/gu, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function tokenize(text) {
    return normalize(text)
      .split(" ")
      .filter((t) => t && !STOP_WORDS.has(t) && t.length > 1);
  }

  function scoreChunk(chunk, queryTokens) {
    const text = normalize(chunk.text || "");
    let score = 0;
    const matched = [];

    for (const token of queryTokens) {
      if (text.includes(token)) {
        score += 1;
        matched.push(token);
      }
    }

    if (score > 0 && queryTokens.length > 0) {
      score += matched.length / queryTokens.length;
    }

    return { score, matched };
  }

  function summarize(bestChunks, queryTokens) {
    if (bestChunks.length === 0) {
      return "Не нашла релевантных фрагментов в текущем индексе субтитров. Попробуйте уточнить вопрос.";
    }

    const lead = bestChunks
      .slice(0, 2)
      .map((c) => c.text)
      .join(" ");

    const mentions = queryTokens.length
      ? `Найденные ключевые слова: ${queryTokens.join(", ")}.`
      : "";

    return `По субтитрам плейлиста наиболее релевантны следующие фрагменты:\n\n${lead}\n\n${mentions}`.trim();
  }

  function renderSources(bestChunks) {
    sourcesEl.innerHTML = "";

    const seen = new Set();
    for (const chunk of bestChunks) {
      const key = `${chunk.videoId}::${chunk.start}`;
      if (seen.has(key)) continue;
      seen.add(key);

      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = chunk.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = chunk.title || chunk.url;
      li.appendChild(a);

      const meta = document.createElement("div");
      meta.textContent = `Фрагмент: ${chunk.start}`;
      meta.style.opacity = "0.8";
      meta.style.fontSize = "0.9em";
      li.appendChild(meta);

      sourcesEl.appendChild(li);
      if (sourcesEl.children.length >= 8) break;
    }
  }

  function answerQuestion() {
    const query = queryEl.value.trim();
    if (!query) {
      answerEl.textContent = "Введите вопрос.";
      return;
    }

    if (!isIndexReady) {
      answerEl.textContent = "Индекс ещё загружается. Попробуйте через пару секунд.";
      return;
    }

    if (!chunks.length) {
      answerEl.textContent = "Индекс пуст. Сначала сгенерируйте playlist-transcripts.json.";
      return;
    }

    const queryTokens = tokenize(query);
    const ranked = chunks
      .map((chunk) => {
        const { score, matched } = scoreChunk(chunk, queryTokens);
        return { ...chunk, score, matched };
      })
      .filter((c) => c.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 12);

    answerEl.textContent = summarize(ranked, queryTokens);
    renderSources(ranked);
  }

  askBtn.addEventListener("click", answerQuestion);
  queryEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      answerQuestion();
    }
  });

  fetch("/playlist-transcripts.json")
    .then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then((payload) => {
      chunks = Array.isArray(payload?.chunks) ? payload.chunks : [];
      isIndexReady = true;
      askBtn.disabled = false;
      statusEl.textContent = `Индекс загружен: ${chunks.length} фрагментов.`;
    })
    .catch((err) => {
      askBtn.disabled = true;
      statusEl.textContent = `Не удалось загрузить индекс: ${err.message}`;
    });

  askBtn.disabled = true;
})();
