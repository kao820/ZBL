---
title: YouTube Playlist QA
---

# YouTube Playlist QA

Это альтернатива NotebookLM для плейлиста:  
`https://www.youtube.com/playlist?list=PLQ0wmPbdvhzJl6lFMAVzbneqAPtBvCrsg`

<div class="playlist-qa">
  <p id="qa-status">Индекс субтитров ещё не загружен.</p>
  <textarea id="qa-query" placeholder="Например: Кто такой Карнелл?"></textarea>
  <button id="qa-ask">Спросить</button>
  <h3>Ответ</h3>
  <div id="qa-answer">Введите вопрос и нажмите «Спросить».</div>
  <h3>Источники</h3>
  <ol id="qa-sources"></ol>
</div>

<link rel="stylesheet" href="/playlist-qa.css" />
<script src="/playlist-qa.js"></script>

## Как наполнить индекс субтитрами

1. Установите `yt-dlp` локально.
2. Запустите:

```bash
python scripts/build_playlist_transcripts.py
```

3. Скрипт обновит файл `quartz/static/playlist-transcripts.json`.
4. Соберите сайт и задеплойте на GitHub Pages.

> Важно: качество ответов зависит от авто-субтитров YouTube.
