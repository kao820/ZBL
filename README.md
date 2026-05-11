# ZBL Playlist QA

Статический сайт (GitHub Pages) с MVP-поиском по субтитрам YouTube-плейлиста:
https://www.youtube.com/playlist?list=PLQ0wmPbdvhzJl6lFMAVzbneqAPtBvCrsg

GitHub Pages URL:
https://kao820.github.io/ZBL/

## Что это

Мини-аналог NotebookLM для одного плейлиста: пользователь задаёт вопрос, фронтенд ищет релевантные фрагменты в заранее собранном JSON-индексе и показывает ответ + источники.

## Локальный запуск

Откройте `index.html` через статический сервер (рекомендуется), например:

```bash
python3 -m http.server 8000
```

Далее откройте http://localhost:8000/

## Сборка индекса субтитров

Зависимости:
- Python 3.10+
- `yt-dlp`

Запуск:

```bash
python3 scripts/build_playlist_transcripts.py
```

Скрипт создаёт/обновляет `playlist-transcripts.json`.
При ошибках YouTube/субтитров файл остаётся валидным JSON, а сообщение записывается в поле `error`.
<<<<<<< codex/check-repository-access-i175wd
Решение полностью автономное: API-ключи для сборки индекса не требуются.
=======
>>>>>>> main

## GitHub Actions

Workflow: `.github/workflows/update-transcripts.yml`
- ручной запуск: Actions → **Update playlist transcripts** → Run workflow
- плановый запуск: еженедельно по cron
- автоматический commit нового `playlist-transcripts.json`, если есть изменения
<<<<<<< codex/check-repository-access-i175wd
- для максимального покрытия подключается OpenAI fallback через `OPENAI_API_KEY` (GitHub Secret)

## Ограничения

- Это LLM-assisted QA: ответы формируются на основе индексированных субтитров и эвристик ранжирования.
=======

## Ограничения

- Это не LLM: качество зависит от полноты и качества авто-субтитров.
>>>>>>> main
- Если YouTube ограничивает доступ к субтитрам, ответы будут пустыми/частичными.
