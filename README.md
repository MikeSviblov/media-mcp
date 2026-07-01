# media-mcp

MCP server for media stack management — movies/TV (Jellyseerr, Radarr, Sonarr,
Jellyfin), audiobooks/ebooks (Prowlarr → qBittorrent → Audiobookshelf), and music
(Prowlarr/Bandcamp → qBittorrent → Navidrome). Shared by Sebastian, Max, and Claude Code.

## Where it runs

- **Host:** the bot host
- **Path:** `~/projects/media-mcp/`
- **Transport:** stdio via `uvx` (MCP) + HTTP :8097 (webhook receiver)
- **Deploy:** CI on push (git pull + restart of the webhook service)
- **Media stack:** a media host running Jellyseerr/Radarr/Sonarr/Prowlarr/qBittorrent/Jellyfin

## Architecture

```
media host                      bot host                    dev machine
┌──────────────┐                ┌────────────────┐           ┌──────────────┐
│ Jellyseerr   │◄──HTTP──┐     │  server.py     │           │ media-mcp    │
│ Radarr       │◄────────┤     │  (FastMCP)     │           │ (full mode)  │
│ Sonarr       │◄────────┤     │  MODE=safe|full│           └──────────────┘
│ Prowlarr     │◄────────┤     └──┬──────────┬──┘
│ qBittorrent  │◄────────┤        │          │
│ Jellyfin     │◄────────┘     ┌──▼──┐  ┌───▼────┐
└──────────────┘               │ Max │  │Sebast. │
         │                     │safe │  │ full   │
         │  webhook            └─────┘  └────────┘
         └─────────────────►webhook_receiver.py :8097
                               → Telegram push
```

## Modes

| Mode | Consumer | Tools |
|------|----------|-------|
| `safe` | Max (secondary user bot) | movie/TV search+request+status, `book_*` (search/status/grab) |
| `full` | Sebastian, Claude Code | all safe + `radarr_*`, `sonarr_*`, `qbit_*`, `prowlarr_*`, `jellyfin_*`, `music_*`, `book_cancel` |

Set via `MEDIA_MCP_MODE` env variable.

## Tools

### SAFE (11 tools)

Movies / TV:

| Tool | Description |
|------|-------------|
| `media_search` | Search movies/TV by name (Jellyseerr/TMDb) |
| `media_availability` | Check if available in Jellyfin |
| `media_request` | Order a movie/show (HD-1080p, Russian audio) |
| `media_request_status` | Show request statuses |
| `library_recently_added` | What's new in Jellyfin |
| `media_progress` | Download progress (Radarr/Sonarr queue) |
| `media_similar` | Recommendations based on a title |

Audiobooks / ebooks (Prowlarr → qBittorrent → Audiobookshelf):

| Tool | Description |
|------|-------------|
| `book_search_releases` | Search audiobook/ebook releases via Prowlarr |
| `book_status` | Download status + whether imported into Audiobookshelf |
| `book_library_recent` | Recently added books in Audiobookshelf |
| `book_grab` | Grab a release, route to `[Abooks]`, tag for the importer (`confirm=True`) |

### FULL (additional 23 tools)

Radarr: `radarr_queue`, `radarr_search_releases`, `radarr_grab_release`, `radarr_delete_movie`
Sonarr: `sonarr_queue`, `sonarr_search_releases`, `sonarr_grab_release`, `sonarr_delete_series`
qBit: `qbit_list_torrents`, `qbit_pause`, `qbit_resume`, `qbit_delete`
Prowlarr: `prowlarr_indexer_status`, `prowlarr_test_indexer`
Jellyfin: `jellyfin_scan_library`, `jellyfin_refresh_item`
Books: `book_cancel`

Music (Prowlarr/Bandcamp → qBittorrent → Navidrome):

| Tool | Description |
|------|-------------|
| `music_search_releases` | Search music releases via Prowlarr |
| `music_status` | Download status + whether imported into Navidrome |
| `music_library_recent` | Recently added albums in Navidrome |
| `music_grab` | Grab a release, route to `[Music]`, tag for the importer (`confirm=True`) |
| `music_cancel` | Cancel a music download |
| `music_bandcamp` | Download a Bandcamp album/discography via yt-dlp (separate from torrents) |

See `discovery/` for the batch taste-based music-collection top-up that drives `music_*`.

Destructive tools (`*_delete`, `*_grab`, `*_cancel`) require `confirm=True`. Default is dry-run preview.

## Run

```bash
# SAFE mode
MEDIA_MCP_MODE=safe JELLYSEERR_URL=http://localhost:5055 JELLYSEERR_API_KEY=... \
  uvx --with mcp --with requests python server.py

# FULL mode
MEDIA_MCP_MODE=full JELLYSEERR_URL=... RADARR_URL=... RADARR_API_KEY=... \
  uvx --with mcp --with requests python server.py
```

## Environment Variables

| Variable | Required for | Description |
|----------|-------------|-------------|
| `MEDIA_MCP_MODE` | all | `safe` or `full` |
| `JELLYSEERR_URL` | all | Jellyseerr URL |
| `JELLYSEERR_API_KEY` | all | Jellyseerr API key |
| `RADARR_URL` | safe+full | Radarr URL (used for progress in safe) |
| `RADARR_API_KEY` | safe+full | Radarr API key |
| `SONARR_URL` | safe+full | Sonarr URL (used for progress in safe) |
| `SONARR_API_KEY` | safe+full | Sonarr API key |
| `JELLYFIN_URL` | safe+full | Jellyfin URL |
| `JELLYFIN_API_KEY` | safe+full | Jellyfin API key |
| `PROWLARR_URL` | full | Prowlarr URL |
| `PROWLARR_API_KEY` | full | Prowlarr API key |
| `QBIT_URL` | full | qBittorrent URL |
| `QBIT_USER` | full | qBittorrent username |
| `QBIT_PASS` | full | qBittorrent password |

Books add `ABS_URL` / `ABS_API_KEY` (Audiobookshelf); music adds `NAVIDROME_URL` /
`NAVIDROME_USER` / `NAVIDROME_PASS`. See [`.env.example`](.env.example) for the full
list (importer paths, Telegram notifications, webhook, discovery tuning).

## Stack

- Python 3.11+
- FastMCP (`mcp.server.fastmcp`)
- requests
- No Docker — runs directly via uvx

## Status

Movies/TV, audiobooks/ebooks, and music tools implemented (34 total). Webhook
receiver live. Importers (`importer.py`, `music_importer.py`) run on the media host.
