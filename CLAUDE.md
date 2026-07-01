# media-mcp

MCP-сервер для управления медиа-стеком (Jellyseerr, Radarr, Sonarr, qBit, Prowlarr, Jellyfin).
Shared: Sebastian (full), Max (safe), Claude Code (full).

## Где работает

- **Host:** the bot host, path `~/projects/media-mcp/`
- **MCP транспорт:** stdio через uvx (запускается агентом при каждом вызове)
- **Webhook receiver:** systemd user service `media-mcp-webhook.service`, порт 8097
- **Медиа-стек:** media host (Jellyseerr/Radarr/Sonarr/Prowlarr/qBittorrent/Jellyfin)
- **CI:** on push → lint → git pull + restart webhook (если изменён)

## Структура

- `server.py` — FastMCP сервер, 7 SAFE + 16 FULL tools. Режим через `MEDIA_MCP_MODE` env.
- `webhook_receiver.py` — HTTP :8097, принимает Jellyseerr webhooks, шлёт в Telegram.
- `media-mcp-webhook.service` — systemd unit для webhook receiver.

## Режимы

- `safe` — только поиск, заказ, статус, прогресс, рекомендации. Для второго пользователя (Max).
- `full` — всё из safe + прямой контроль Radarr/Sonarr/qBit/Prowlarr/Jellyfin. Деструктив через `confirm=True`.

## Потребители

| Агент | Конфиг | Режим |
|-------|--------|-------|
| Sebastian | `mcp-config.json.template` → `media` | full |
| Max (Hermes) | `~/.hermes/config.yaml` → `mcp_servers.media` | safe |
| Claude Code | `.mcp.json` (gitignored) | full |

## Env

Токены в `.env` (gitignored) на хосте бота.

## Деплой

Push → CI lint → git pull на хосте бота → restart webhook (если изменён).
