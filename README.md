# ZBL Playlist QA

Статический сайт (GitHub Pages) с поиском и ответами по субтитрам YouTube-плейлиста.

## Локальный запуск

```bash
python3 -m http.server 8000
```

## Сборка индекса субтитров

Зависимости:
- Python 3.10+
- `yt-dlp`
- опционально: `youtube-transcript-api`
- опционально: `openai` + `OPENAI_API_KEY` для fallback-транскриба

```bash
python3 scripts/build_playlist_transcripts.py
```

Скрипт обновляет `playlist-transcripts.json` инкрементально:
- уже скачанные видео и чанки сохраняются;
- новые видео в плейлисте добавляются;
- мусорные реплики (`[смех]`, `[музыка]` и т.п.) очищаются.

## GitHub Actions

Workflow: `.github/workflows/update-transcripts.yml`
- ручной запуск: Actions → **Update playlist transcripts** → Run workflow
- запуск по расписанию: каждый понедельник
- авто-коммит нового `playlist-transcripts.json`, если есть изменения


## Новый инкрементальный процесс (1 ролик за запуск)

- Реестр статусов: `playlist-video-status.json`.
- Отдельные immutable-файлы: `playlist-video-transcripts/<videoId>.json`.
- Один запуск `scripts/build_playlist_transcripts.py` обрабатывает максимум один `pending` ролик.
- Ежедневная автоматизация: `.github/workflows/update-transcripts.yml`.

Опциональный тяжёлый fallback (WhisperX + diarization) для конкретного видео:

```bash
python3 scripts/transcribe_youtube_whisperx.py "https://www.youtube.com/watch?v=<ID>" --video-id <ID> --hf-token "$HF_TOKEN"
```
