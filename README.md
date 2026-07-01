# media-mcp

MCP server for media stack management (Jellyseerr, Radarr, Sonarr, qBit, Jellyfin) — shared by Sebastian, Max, and Claude Code.

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
| `safe` | Max (secondary user bot) | search, availability, request, status, recently_added, progress, similar |
| `full` | Sebastian, Claude Code | all safe + radarr_*, sonarr_*, qbit_*, prowlarr_*, jellyfin_* |

Set via `MEDIA_MCP_MODE` env variable.

## Tools

### SAFE (7 tools)

| Tool | Description |
|------|-------------|
| `media_search` | Search movies/TV by name (Jellyseerr/TMDb) |
| `media_availability` | Check if available in Jellyfin |
| `media_request` | Order a movie/show (HD-1080p, Russian audio) |
| `media_request_status` | Show request statuses |
| `library_recently_added` | What's new in Jellyfin |
| `media_progress` | Download progress (Radarr/Sonarr queue) |
| `media_similar` | Recommendations based on a title |

### FULL (additional 16 tools)

Radarr: `radarr_queue`, `radarr_search_releases`, `radarr_grab_release`, `radarr_delete_movie`
Sonarr: `sonarr_queue`, `sonarr_search_releases`, `sonarr_grab_release`, `sonarr_delete_series`
qBit: `qbit_list_torrents`, `qbit_pause`, `qbit_resume`, `qbit_delete`
Prowlarr: `prowlarr_indexer_status`, `prowlarr_test_indexer`
Jellyfin: `jellyfin_scan_library`, `jellyfin_refresh_item`

Destructive tools (`*_delete`, `*_grab`) require `confirm=True`. Default is dry-run preview.

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

## Stack

- Python 3.11+
- FastMCP (`mcp.server.fastmcp`)
- requests
- No Docker — runs directly via uvx

## Status

MVP — SAFE + FULL tools implemented. Webhook receiver pending.
