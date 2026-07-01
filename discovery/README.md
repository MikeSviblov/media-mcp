# discovery — taste-based пополнение музыки

Разовый (раз в ~полгода) заход: изучить фонотеку Navidrome → составить список
пробелов → пакетно скачать через музыкальный пайплайн media-mcp. Дедуп против
библиотеки, приоритет FLAC, троттлинг, respects disk guard.

Отдельно от постоянной инфры (`server.py`, `music_importer.py`): это ручные
драйверы, не сервис. Дёргают функции `server.py` (`music_search_releases`,
`music_grab`), поэтому запускаются из корня репо с тем же окружением, что и бот.

## Где бежит

- `music_discovery.py`, `music_wave.py`, `fverify.py` — на **хосте бота** (там
  креды Prowlarr/qB и `server.py`).
- `split_cue.py` — на **медиа-хосте** (там смонтирована фонотека, напр. `/mnt/nas/disk2/Music`).

## Запуск

Системный `python3` не имеет `mcp` — только через `uv run` с теми же зависимостями,
что у бота. Музыкальные тулы `server.py` видны лишь при `MEDIA_MCP_MODE=full`.

```bash
cd ~/projects/media-mcp
set -a; . .env; set +a          # креды Prowlarr/qB/Navidrome
export MEDIA_MCP_MODE=full

# 1. отредактировать список целей
$EDITOR discovery/music_targets.json          # [{artist, album, cluster}, ...]

# 2. основной проход
~/.local/bin/uv run --with mcp --with requests python discovery/music_discovery.py

# 3. догон остатка (ждёт слива MUSIC_MAX_INFLIGHT-кэпа)
~/.local/bin/uv run --with mcp --with requests python discovery/music_wave.py

# 4. (опц.) верификация: сколько целей реально легло в Navidrome
~/.local/bin/uv run --with requests python discovery/fverify.py
```

Результаты пишутся в `discovery/music_results.json` (gitignored). Пути к
targets/results переопределяются env `MUSIC_TARGETS` / `MUSIC_RESULTS`.

## split_cue (пост-обработка, на медиа-хосте)

Метал/неофолк на RuTracker часто приходит как image+`.cue` (1 FLAC/APE на альбом) —
в Navidrome это осиротевшие треки, не браузятся по артисту. Резалка парсит `.cue` →
ffmpeg по таймкодам → потрековые FLAC с тегами. Оригиналы уезжают в
`/mnt/nas/disk2/_music_orig/`. Нужен только ffmpeg.

Запускается из git-чекаута репо на медиа-хосте (`~/projects/media-mcp`):

```bash
# на медиа-хосте
cd ~/projects/media-mcp && git pull --ff-only
python3 discovery/split_cue.py "Nytt Land" "Другой Артист" ...
```

True iso (образ диска) — не режется, только перекачка. APE+cue режется так же.

## Грабли (наступали)

- **qB queue cap** `max_active_downloads=3 / max_active_torrents=5` (ОБЩИЙ qB) →
  серийная закачка, большой заход тянется полдня. Временно поднимать через
  `setPreferences`, потом вернуть 3/5.
- **media-mcp inflight guard** `MUSIC_MAX_INFLIGHT=20` блокирует >20 грэбов сразу.
  Для большого захода: `export MUSIC_MAX_INFLIGHT=80` (на живого бота не влияет).
- **pick не того артиста**: ловил Numenor под «Draconian», Mrs. Piss под «Chelsea
  Wolfe». `norm()` стрипает кириллицу → для русских артистов фильтр по имени
  бесполезен, спасает pick по сидам.
- Точный альбом часто не находится → берётся лучший релиз артиста по сидам (для
  discovery ок). Дискографии >2500MB отсекаются по размеру.

## Формат music_targets.json

```json
[
  {"artist": "Nytt Land", "album": "Fimbulvintr", "cluster": "neofolk"},
  {"artist": "...", "album": "...", "kind": "discography"}
]
```

`kind`: `album` (дефолт) или `discography`. `cluster` — произвольная метка для
группировки в отчёте `fverify.py`.
