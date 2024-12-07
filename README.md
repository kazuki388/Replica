# Replica

The **Replica** module is a powerful Discord bot designed to clone and migrate servers, channels, and content across Discord servers. It provides comprehensive functionality for duplicating server settings, roles, channels, emojis, stickers, and messages.

## Features

- Complete server cloning
  - Server settings (name, icon, banner, verification level, etc.)
  - Categories and channels (text, voice, forum, stage)
  - Roles and permissions
  - Emojis and stickers
  - Messages and threads
  - Webhooks

- Channel migration
  - Text channel to text channel
  - Forum to forum
  - Public thread to text channel
  - Forum post to forum

- Customizable delays
  - Webhook delay (0.1-5.0 seconds)
  - Process delay (0.1-5.0 seconds)

- Rate limiting
  - 5 concurrent webhooks
  - 10 concurrent member operations
  - 2 concurrent channel operations

## Commands

### Base Command

- `/replica`: Base command for all replica operations

### Server Commands

- `/replica create`: Create a new server for cloning
  - Creates a new server with basic settings
  - Automatically generates an invite link
  - Sets up admin role

- `/replica delete`: Delete a managed server
  - `server` (choice, required): Server to delete

- `/replica invite`: Generate server invite
  - `server` (choice, required): Target server
  - `duration` (integer, optional): Invite duration in hours (1-168, default: 24)

### Cloning Commands

- `/replica settings`: Clone server settings
  - Copies server-wide configurations
  - Includes verification levels and notification settings

- `/replica icon`: Clone server icon
  - Transfers server icon to the target server

- `/replica banner`: Clone server banner
  - Transfers server banner to the target server

- `/replica roles`: Clone server roles
  - Duplicates role hierarchy
  - Preserves permissions and colors
  - Maintains role icons (if available)

- `/replica categories`: Clone channel categories
  - Creates category structure
  - Preserves category permissions

- `/replica c-channels`: Clone community channels
  - Clones forum channels
  - Duplicates stage channels
  - Preserves channel settings and permissions

- `/replica nc-channels`: Clone non-community channels
  - Clones text channels
  - Duplicates voice channels
  - Copies announcement channels
  - Maintains channel permissions and settings

- `/replica emojis`: Clone server emojis
  - Transfers custom emojis
  - Preserves emoji names and roles

- `/replica stickers`: Clone server stickers
  - Copies custom stickers
  - Maintains sticker metadata

### Migration Command

- `/replica migrate`: Migrate channel content
  - `origin` (string, required): Source channel ID
  - `server` (string, required): Destination server ID
  - `channel` (string, required): Destination channel ID

### Configuration Command

- `/replica config`: Configure bot settings
  - `webhook` (float, optional): Set webhook delay
  - `process` (float, optional): Set process delay
  - `admin_user_id` (string, optional): Set admin user ID
  - `source_guild_id` (string, optional): Set source guild ID
  - `target_guild_id` (string, optional): Set target guild ID

### Debug Command

- `/replica export`: Export extension files
  - `type` (choice, required): Type of files to export
    - All Files
    - Individual files from extension directory

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
