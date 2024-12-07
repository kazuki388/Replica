# Replica

The **Replica** module is designed to replicate and migrate server configurations, role hierarchies, channel structures, custom assets, and message content between Discord guilds.

> [!NOTE]
> `lib.py`: derived from [libdiscord-ipy-migrate](https://github.com/retr0-init/libdiscord-ipy-migrate) with cross-server migration capabilities

## Features

- Full Server Replication
  - Guild configurations (name, icon, banner, verification level, etc.)
  - Channel hierarchy (categories, text, voice, forum, stage)
  - Role structure and permission mappings
  - Custom assets (emojis, stickers)
  - Message content and thread structures
  - Webhook configurations

- Cross-Server Channel Migration
  - Text channel to text channel transfer
  - Forum channel replication
  - Thread to text channel conversion
  - Forum post migration

## Commands

### Base Command

- `/replica`: Root command for all replica operations

### Initialization Command

- `/replica initialize`: Bootstrap bot configuration and state
  - Loads parameters from config.json
  - Establishes webhook and process delays
  - Sets critical admin and guild identifiers

### Server Commands

- `/replica create`: Initialize new target server
  - Establishes base server configuration
  - Generates invite URL
  - Configures administrative role

- `/replica delete`: Remove managed server
  - `server` (choice, required): Target server identifier

- `/replica invite`: Generate server invitation
  - `server` (choice, required): Target server identifier
  - `duration` (integer, optional): Invitation validity period in hours (1-168, default: 24)

### Replication Commands

- `/replica settings`: Replicate server configurations
  - Duplicates guild-wide settings
  - Includes verification and notification parameters

- `/replica icon`: Replicate server icon
  - Transfers guild icon asset

- `/replica banner`: Replicate server banner
  - Transfers guild banner asset

- `/replica roles`: Replicate role hierarchy
  - Replicates role structure
  - Preserves permission bitfields and color codes
  - Maintains role assets where applicable

- `/replica categories`: Replicate channel categories
  - Establishes category structure
  - Maintains permission overwrites

- `/replica c-channels`: Replicate community channels
  - Duplicates forum structures
  - Replicates stage instances
  - Preserves channel configurations and permissions

- `/replica nc-channels`: Replicate standard channels
  - Duplicates text channels
  - Replicates voice channels
  - Transfers announcement channels
  - Maintains channel-specific configurations

- `/replica emojis`: Replicate custom emojis
  - Transfers emoji assets
  - Preserves metadata and role restrictions

- `/replica stickers`: Replicate custom stickers
  - Transfers sticker assets
  - Maintains associated metadata

### Migration Command

- `/replica migrate`: Execute channel content migration (adapted from [Discord-Utilities](https://github.com/retr0-init/DIscord-Utilities/blob/master/main.py#L583) with cross-server support)
  - `origin` (string, required): Source channel identifier
  - `server` (string, required): Destination server identifier
  - `channel` (string, required): Destination channel identifier

### Configuration Command

- `/replica config`: Modify runtime parameters
  - `webhook` (float, optional): Webhook execution delay (0.1-5.0 seconds)
  - `process` (float, optional): Process execution delay (0.1-5.0 seconds)
  - `admin_user_id` (string, optional): Administrative user identifier
  - `source_guild_id` (string, optional): Source guild identifier
  - `target_guild_id` (string, optional): Target guild identifier

### Debug Command

- `/replica export`: Export extension assets
  - `type` (choice, required): Asset export scope
    - Complete extension directory
    - Specific extension components
  - Internationalization support (English, Chinese)

## Configuration

Key configuration options in `config.json`:

```json
{
  "webhook_delay": 0.2,
  "process_delay": 0.2,
  "admin_user_id": "YOUR_ADMIN_ID",
  "source_guild_id": "SOURCE_SERVER_ID",
  "target_guild_id": "TARGET_SERVER_ID"
}
```

### Files

The module uses several files for operation:

- `config.json`: Bot configuration settings
- `state.json`: Current state and mappings
- `replica.log`: Operation logs
