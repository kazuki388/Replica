from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict, deque
from datetime import timedelta
from io import BytesIO
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Coroutine, Dict, List, Optional
from weakref import proxy

import aiofiles
import aiofiles.os
import aiofiles.ospath
import aioshutil
import interactions
import orjson
from interactions.api.events import MemberAdd
from interactions.client.errors import Forbidden, HTTPException, NotFound

from .lib import migrate_channel, migrate_thread

BASE_DIR: str = os.path.abspath(os.path.dirname(__file__))
LOG_FILE: str = os.path.join(BASE_DIR, "replica.log")

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    "%(asctime)s | %(process)d:%(thread)d | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
    "%Y-%m-%d %H:%M:%S.%f %z",
)
file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=1024 * 1024, backupCount=1, encoding="utf-8"
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


class Model:
    def __init__(self) -> None:
        self.mappings: dict[str, Any] = {}

    async def load_state(self, file_path: str) -> None:
        try:
            async with aiofiles.open(file_path) as file:
                raw_data = await file.read()
                self.mappings = orjson.loads(memoryview(raw_data.encode()))
                logger.info("Successfully loaded state")
        except FileNotFoundError:
            logger.warning("State file not found")
        except Exception as e:
            logger.error(f"Error loading state: {e}", exc_info=True)

    async def save_state(self, file_path: str) -> None:
        try:
            encoded_data = orjson.dumps(
                self.mappings,
                option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_PASSTHROUGH_DATETIME,
            )
            async with aiofiles.open(file_path, mode="wb") as file:
                await file.write(encoded_data)
            logger.info("Successfully saved state")
        except Exception as e:
            logger.error(f"Error saving state: {e}", exc_info=True)
            raise

    async def get_last_state(self, file_path: str) -> dict:
        try:
            async with aiofiles.open(file_path) as file:
                raw_data = await file.read()
                return orjson.loads(memoryview(raw_data.encode()))
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"Error loading last state: {e}", exc_info=True)
            return {}

    async def load_config(self, file_path: str) -> None:
        try:
            async with aiofiles.open(file_path) as file:
                raw_data = await file.read()
                self.mappings = orjson.loads(memoryview(raw_data.encode()))
                logger.info("Successfully loaded config")
        except FileNotFoundError:
            logger.warning("Config file not found")
        except Exception as e:
            logger.error(f"Error loading config: {e}", exc_info=True)

    async def save_config(self, file_path: str) -> None:
        try:
            encoded_data = orjson.dumps(
                self.mappings,
                option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_PASSTHROUGH_DATETIME,
            )
            async with aiofiles.open(file_path, mode="wb") as file:
                await file.write(encoded_data)
            logger.info("Successfully saved config")
        except Exception as e:
            logger.error(f"Error saving config: {e}", exc_info=True)
            raise


class Replica(interactions.Extension):
    def __init__(self, bot: interactions.Client) -> None:
        self.bot: interactions.Client = bot
        self.model: Model = Model()

        self.CONFIG_FILE: str = os.path.join(BASE_DIR, "config.json")
        self.STATE_FILE: str = os.path.join(BASE_DIR, "state.json")

        self.guild: interactions.Guild | None = None
        self.new_guild: interactions.Guild | None = None
        self.live_update: bool = False

        self.process_delay: float = 0.2
        self.webhook_delay: float = 0.2
        self.webhook_semaphore = asyncio.Semaphore(5)
        self.member_semaphore = asyncio.Semaphore(10)
        self.channel_semaphore = asyncio.Semaphore(2)
        self.message_queue: deque[tuple] = deque(maxlen=10000)
        self.new_messages_queue: deque[tuple] = deque(maxlen=1000)
        self.processed_channels: list[int] = []

        self.mappings: dict[str, dict] = {
            "channels": {},
            "categories": {},
            "roles": {},
            "emojis": {},
            "fetched_data": {"channels": [], "roles": [], "emojis": [], "stickers": []},
        }
        self.last_executed_method: str = ""

        asyncio.create_task(self.initialize_data())

    async def initialize_data(self) -> None:
        await self.model.load_state(self.STATE_FILE)
        try:
            await self.model.load_config(self.CONFIG_FILE)
            config = self.model.mappings
            self.webhook_delay = config.get("webhook_delay", 0.2)
            self.process_delay = config.get("process_delay", 0.2)
            self.live_update = config.get("live_update", False)
            self.guild = await self.bot.fetch_guild(1150630510696075404)

            if saved_guild_id := config.get("new_guild_id"):
                try:
                    self.new_guild = await self.bot.fetch_guild(saved_guild_id)
                    logger.info(
                        f"Loaded saved guild: {self.new_guild.name} ({self.new_guild.id})"
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not load saved guild {saved_guild_id}: {e}",
                    )
                    self.new_guild = None

            config.update(
                {
                    "webhook_delay": self.webhook_delay,
                    "process_delay": self.process_delay,
                    "live_update": self.live_update,
                    "old_guild": self.guild.id,
                    "new_guild": self.new_guild.id if self.new_guild else None,
                }
            )
            self.model.mappings = config
            await self.model.save_config(self.CONFIG_FILE)

        except Exception as e:
            logger.warning(
                f"Error loading config file, using default settings: {e}",
            )
            self.webhook_delay = 0.2
            self.process_delay = 0.2
            self.live_update = False

    # Create

    @staticmethod
    async def create_channel_log(
        channel_type: str, channel_name: str, channel_id: int
    ) -> None:
        logger.debug(f"Created {channel_type} channel #{channel_name} | {channel_id}")

    @staticmethod
    async def create_object_log(
        object_type: str, object_name: str, object_id: int
    ) -> None:
        logger.debug(f"Created {object_type}: {object_name} | {object_id}")

    @staticmethod
    async def create_webhook_log(channel_name: str, deleted: bool = False) -> None:
        logger.debug(
            f"{'Deleted' if deleted else 'Created'} webhook in #{channel_name}"
        )

    async def create_new_guild(self) -> None:
        try:
            if not self.guild:
                raise ValueError("Source guild is not set")

            self.new_guild = await interactions.Guild.create(
                name=f"{self.guild.name} (Dyad)",
                client=self.bot,
                verification_level=(
                    self.guild.verification_level
                    if self.guild.verification_level
                    else interactions.VerificationLevel.NONE
                ),
                default_message_notifications=(
                    self.guild.default_message_notifications
                    if self.guild.default_message_notifications
                    else interactions.DefaultNotificationLevel.ALL_MESSAGES
                ),
                explicit_content_filter=(
                    self.guild.explicit_content_filter
                    if self.guild.explicit_content_filter
                    else interactions.ExplicitContentFilterLevel.DISABLED
                ),
            )

            try:
                await self.model.load_config(self.CONFIG_FILE)
                current_config = self.model.mappings
            except Exception:
                current_config = {}

            current_config["new_guild_id"] = str(self.new_guild.id)
            self.model.mappings = current_config

            try:
                await self.model.save_config(self.CONFIG_FILE)
                logger.info(
                    f"Created new guild: {self.new_guild.name} ({self.new_guild.id}) and saved ID to config"
                )

                await self.new_guild.create_role(
                    name="Admin",
                    permissions=interactions.Permissions.ADMINISTRATOR,
                    color=0xFF0000,
                    hoist=True,
                    mentionable=True,
                )

                system_channel = await self.new_guild.fetch_channel(
                    self.new_guild.system_channel_id
                )
                invite = await system_channel.create_invite(max_age=86400)
                user = await self.bot.fetch_user(1268909926458064991)
                await user.send(
                    f"Here's your invite link to the new server: {invite.link} ({invite.code})"
                )

            except Exception as e:
                logger.error(
                    f"Failed to save new guild ID to config: {e}", exc_info=True
                )

            await asyncio.sleep(self.process_delay)
        except Exception as e:
            logger.error(f"Failed to create new guild: {e}", exc_info=True)
            raise

    @interactions.listen(MemberAdd)
    async def on_member_join(self, event: MemberAdd):
        if not self.new_guild:
            return

        if event.member.id == 1268909926458064991:
            try:
                admin_role = next(
                    (role for role in event.member.guild.roles if role.name == "Admin"),
                    None,
                )
                if admin_role:
                    await event.member.add_role(admin_role)
                    logger.info(f"Added Admin role to user {event.member.id}")
                else:
                    logger.error("Admin role not found")
            except Exception as e:
                logger.error(f"Failed to add Admin role: {e}")

    # Serve

    async def fetch_guild_data(self) -> None:
        if not self.guild:
            raise ValueError("Source guild is not set")

        self.mappings = {
            "channels": {},
            "categories": {},
            "roles": {},
            "emojis": {},
            "fetched_data": {"channels": [], "roles": [], "emojis": [], "stickers": []},
        }

        try:
            self.mappings["fetched_data"][
                "channels"
            ] = await self.guild.fetch_channels()
            logger.info(
                f"Fetched {len(self.mappings['fetched_data']['channels'])} channels"
            )
        except Exception as e:
            logger.error(f"Failed to fetch channels: {e}")

        try:
            self.mappings["fetched_data"]["roles"] = self.guild.roles
            logger.info(f"Fetched {len(self.mappings['fetched_data']['roles'])} roles")
        except Exception as e:
            logger.error(f"Failed to fetch roles: {e}")

        try:
            self.mappings["fetched_data"][
                "emojis"
            ] = await self.guild.fetch_all_custom_emojis()
            logger.info(
                f"Fetched {len(self.mappings['fetched_data']['emojis'])} emojis"
            )
        except Exception as e:
            logger.error(f"Failed to fetch emojis: {e}")

        try:
            self.mappings["fetched_data"][
                "stickers"
            ] = await self.guild.fetch_all_custom_stickers()
            logger.info(
                f"Fetched {len(self.mappings['fetched_data']['stickers'])} stickers"
            )
        except Exception as e:
            logger.error(f"Failed to fetch stickers: {e}")

    async def prepare_server(self) -> None:
        if not self.new_guild:
            logger.error("New guild is not initialized")
            return

        cleanup_methods = {
            "roles": lambda: [
                role for role in self.new_guild.roles if role.id != self.new_guild.id
            ],
            "channels": self.new_guild.fetch_channels,
            "emojis": self.new_guild.fetch_all_custom_emojis,
            "stickers": self.new_guild.fetch_all_custom_stickers,
        }

        for method_name, method in cleanup_methods.items():
            logger.debug(f"Processing cleaning method: {method_name}...")
            items = method() if method_name == "roles" else await method()
            await self.cleanup_items(items)

        self.last_executed_method = "prepare_server"

    async def cleanup_items(self, items) -> None:
        if not items:
            return

        for item in items:
            try:
                if hasattr(item, "delete"):
                    await item.delete()
                    await asyncio.sleep(self.process_delay)
                else:
                    logger.warning(f"Item {item} does not have delete method")
            except HTTPException as e:
                logger.warning(f"Failed to delete item: {e}")
                continue

        if self.new_guild is not None:
            try:
                await self.new_guild.edit(icon=None, banner=None, description=None)
            except HTTPException as e:
                logger.warning(f"Failed to reset guild settings: {e}")

    async def clone_settings(self) -> bool:
        if not self.guild or not self.new_guild:
            logger.error("Guild or new guild is not initialized")
            return False

        try:
            channels = {
                "afk": (
                    self.get_channel_from_mapping(self.guild.afk_channel_id)
                    if hasattr(self.guild, "afk_channel_id")
                    else None
                ),
                "system": (
                    self.get_channel_from_mapping(self.guild.system_channel)
                    if hasattr(self.guild, "system_channel")
                    else None
                ),
                "public_updates": (
                    self.get_channel_from_mapping(self.guild.public_updates_channel)
                    if hasattr(self.guild, "public_updates_channel")
                    else None
                ),
                "rules": (
                    self.get_channel_from_mapping(self.guild.rules_channel)
                    if hasattr(self.guild, "rules_channel")
                    else None
                ),
                "safety_alerts": (
                    self.get_channel_from_mapping(self.guild.safety_alerts_channel)
                    if hasattr(self.guild, "safety_alerts_channel")
                    else None
                ),
            }

            if not channels["public_updates"]:
                logger.error(
                    "Can't create community: missing access to public updates channel"
                )
                return False

            try:
                await self.new_guild.edit(
                    features=["COMMUNITY"],
                    name=self.guild.name if hasattr(self.guild, "name") else None,
                    description=(
                        self.guild.description
                        if hasattr(self.guild, "description")
                        else None
                    ),
                    verification_level=(
                        self.guild.verification_level
                        if hasattr(self.guild, "verification_level")
                        else None
                    ),
                    default_message_notifications=(
                        self.guild.default_message_notifications
                        if hasattr(self.guild, "default_message_notifications")
                        else None
                    ),
                    explicit_content_filter=(
                        self.guild.explicit_content_filter
                        if hasattr(self.guild, "explicit_content_filter")
                        else None
                    ),
                    afk_channel=channels["afk"],
                    afk_timeout=(
                        self.guild.afk_timeout
                        if hasattr(self.guild, "afk_timeout")
                        else None
                    ),
                    system_channel=channels["system"],
                    system_channel_flags=(
                        self.guild.system_channel_flags
                        if hasattr(self.guild, "system_channel_flags")
                        else None
                    ),
                    rules_channel=channels["rules"],
                    public_updates_channel=channels["public_updates"],
                    safety_alerts_channel=channels["safety_alerts"],
                    preferred_locale=(
                        self.guild.preferred_locale
                        if hasattr(self.guild, "preferred_locale")
                        else None
                    ),
                    premium_progress_bar_enabled=(
                        self.guild.premium_progress_bar_enabled
                        if hasattr(self.guild, "premium_progress_bar_enabled")
                        else False
                    ),
                )
                logger.debug(
                    f"Updated settings for guild: {self.new_guild.name} ({self.new_guild.id})"
                )
                await asyncio.sleep(self.process_delay)
                self.last_executed_method = "clone_settings"
                return True
            except Exception as e:
                logger.error(
                    f"Settings that failed to update: {channels}, failed to update community settings: {e}",
                    exc_info=True,
                )
                return False
        except Exception as e:
            logger.error(f"Failed to clone settings: {e}", exc_info=True)
            return False

    async def clone_icon(self) -> None:
        if (
            self.guild is not None
            and self.guild.icon is not None
            and self.new_guild is not None
        ):
            await self.new_guild.edit(icon=BytesIO(await self.guild.icon.fetch()))
        await asyncio.sleep(self.process_delay)
        self.last_executed_method = "clone_icon"

    async def clone_banner(self) -> None:
        if (
            self.guild is not None
            and self.guild.banner is not None
            and self.new_guild is not None
        ):
            await self.new_guild.edit(banner=BytesIO(await self.guild.splash.fetch()))
            await asyncio.sleep(self.process_delay)
        self.last_executed_method = "clone_banner"

    async def clone_roles(self):
        roles_create = [role for role in self.mappings["fetched_data"]["roles"]]
        self.mappings["roles"].update(
            {
                role.id: await self.new_guild.fetch_role(role.id)
                for role in roles_create
                if role.id == self.new_guild.default_role.id
            }
        )

        for role in reversed(roles_create):
            if role.id == self.new_guild.default_role.id:
                await (await self.new_guild.fetch_role(role.id)).edit(
                    name=role.name,
                    color=role.color,
                    hoist=role.hoist,
                    mentionable=role.mentionable,
                    permissions=role.permissions,
                    icon=role.icon,
                    unicode_emoji=role.unicode_emoji,
                )
                await asyncio.sleep(self.process_delay)
                continue

            self.mappings["roles"][role.id] = new_role = (
                await self.new_guild.create_role(
                    name=role.name,
                    color=role.color,
                    hoist=role.hoist,
                    mentionable=role.mentionable,
                    permissions=role.permissions,
                    icon=role.icon,
                )
            )

            if role.unicode_emoji:
                await new_role.edit(unicode_emoji=role.unicode_emoji)
                await asyncio.sleep(self.process_delay)

            await self.create_object_log(
                object_type="role", object_name=new_role.name, object_id=new_role.id
            )
            await asyncio.sleep(self.process_delay)
        self.last_executed_method = "clone_roles"

    async def clone_categories(self, perms: bool = True) -> None:
        if not self.new_guild:
            logger.warning("New guild is not set")
            return

        categories = [
            channel
            for channel in self.mappings["fetched_data"]["channels"]
            if isinstance(channel, interactions.GuildCategory)
        ]

        for category in categories:
            overwrites = {}
            for overwrite in category.permission_overwrites:
                if perms and isinstance(overwrite.id, interactions.Role):
                    perm = interactions.PermissionOverwrite.for_target(overwrite.id)
                    if overwrite.allow:
                        perm.add_allows(overwrite.allow)
                    if overwrite.deny:
                        perm.add_denies(overwrite.deny)
                    overwrites[self.mappings["roles"][overwrite.id]] = perm

            self.mappings["categories"][category.id] = new_category = (
                await self.new_guild.create_category(
                    name=category.name,
                    position=category.position,
                    permission_overwrites=overwrites,
                )
            )
            await self.create_object_log(
                object_type="category",
                object_name=new_category.name,
                object_id=new_category.id,
            )
            await asyncio.sleep(self.process_delay)
        self.last_executed_method = "clone_categories"

    async def clone_channels(self, perms: bool = True) -> None:
        if not self.new_guild:
            logger.warning("New guild is not set")
            return

        channels = [
            c
            for c in self.mappings["fetched_data"]["channels"]
            if isinstance(
                c,
                (
                    interactions.GuildText,
                    interactions.GuildVoice,
                    interactions.GuildForum,
                    interactions.GuildStageVoice,
                    interactions.GuildNews,
                ),
            )
        ]

        for channel in channels:
            try:
                if self.guild:
                    channel = await self.guild.fetch_channel(channel.id)
                else:
                    logger.warning("Guild is not set")
                    continue
            except Forbidden:
                logger.debug(f"Can't fetch channel {channel.name} | {channel.id}")
                continue

            category = self.mappings["categories"].get(channel.parent_id)
            overwrites = {}

            if perms and channel.permission_overwrites:
                for overwrite in channel.permission_overwrites:
                    if isinstance(overwrite.id, interactions.Role):
                        perm = interactions.PermissionOverwrite.for_target(overwrite.id)
                        if overwrite.allow:
                            perm.add_allows(overwrite.allow)
                        if overwrite.deny:
                            perm.add_denies(overwrite.deny)
                        overwrites[self.mappings["roles"][overwrite.id]] = perm

            channel_args = {
                "name": channel.name,
                "position": channel.position,
                "category": category,
                "permission_overwrites": overwrites,
            }

            if isinstance(channel, interactions.GuildText):
                self.mappings["channels"][channel.id] = new_channel = (
                    await self.new_guild.create_text_channel(
                        **channel_args,
                        topic=channel.topic,
                        rate_limit_per_user=channel.rate_limit_per_user,
                        nsfw=channel.nsfw,
                    )
                )
                await self.create_channel_log(
                    channel_type="text",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )

            elif isinstance(channel, interactions.GuildVoice):
                bitrate = (
                    min(channel.bitrate, self.new_guild.bitrate_limit)
                    if self.new_guild.bitrate_limit
                    else channel.bitrate
                )

                self.mappings["channels"][channel.id] = new_channel = (
                    await self.new_guild.create_voice_channel(
                        **channel_args,
                        bitrate=bitrate,
                        user_limit=channel.user_limit,
                    )
                )
                await self.create_channel_log(
                    channel_type="voice",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )

            elif isinstance(channel, interactions.GuildForum):
                tags = channel.available_tags
                for tag in tags:
                    if tag.emoji and tag.emoji.id:
                        tag.emoji = self.mappings["emojis"].get(tag.emoji.id)

                self.mappings["channels"][channel.id] = new_channel = (
                    await self.new_guild.create_forum_channel(
                        **channel_args,
                        topic=channel.topic,
                        nsfw=channel.nsfw,
                        layout=channel.default_forum_layout,
                        rate_limit_per_user=channel.rate_limit_per_user,
                        sort_order=channel.default_sort_order,
                        available_tags=tags,
                    )
                )
                await self.create_channel_log(
                    channel_type="forum",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )

            elif isinstance(channel, interactions.GuildStageVoice):
                self.mappings["channels"][channel.id] = new_channel = (
                    await self.new_guild.create_stage_channel(
                        **channel_args,
                        bitrate=min(channel.bitrate, self.new_guild.bitrate_limit),
                        user_limit=channel.user_limit,
                    )
                )
                await self.create_channel_log(
                    channel_type="stage",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )

            elif isinstance(channel, interactions.GuildNews):
                self.mappings["channels"][channel.id] = new_channel = (
                    await self.new_guild.create_news_channel(
                        **channel_args,
                        topic=channel.topic,
                        nsfw=channel.nsfw,
                    )
                )
                await self.create_channel_log(
                    channel_type="news",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )

            await asyncio.sleep(self.process_delay)

        self.last_executed_method = "clone_channels"

    async def clone_emojis(self) -> None:
        if not self.new_guild:
            return

        emoji_limit = min(
            self.new_guild.emoji_limit - 5,
            len(await self.new_guild.fetch_all_custom_emojis()),
        )
        emoji_data = [
            (emoji.name, emoji.roles, await emoji.read())
            for emoji in self.mappings["fetched_data"]["emojis"][:emoji_limit]
            if len(await self.new_guild.fetch_all_custom_emojis()) < emoji_limit
        ]

        for name, roles, imagefile in emoji_data:
            new_emoji = await self.new_guild.create_custom_emoji(
                name=name, roles=roles, imagefile=imagefile
            )
            original_emoji = next(
                emoji
                for emoji in self.mappings["fetched_data"]["emojis"]
                if emoji.name == name
            )
            self.mappings["emojis"][original_emoji.id] = new_emoji
            await self.create_object_log(
                object_type="emoji", object_name=new_emoji.name, object_id=new_emoji.id
            )
            await asyncio.sleep(self.process_delay)

        self.last_executed_method = "clone_emojis"

    async def clone_stickers(self) -> None:
        if not self.new_guild:
            return

        sticker_limit, created = self.new_guild.sticker_limit, 0
        sticker_data = [
            (s.name, s.description, await s.to_file(), s.tags, s.id, s.url)
            for s in self.mappings["fetched_data"]["stickers"][:sticker_limit]
        ]

        for name, description, file, tags, sticker_id, sticker_url in sticker_data:
            if created >= sticker_limit:
                break

            try:
                new_sticker = await self.new_guild.create_custom_sticker(
                    name=name, description=description, file=file, tags=tags
                )
                created += 1
                await self.create_object_log(
                    object_type="sticker",
                    object_name=new_sticker.name,
                    object_id=new_sticker.id,
                )
            except NotFound:
                logger.warning(
                    f"Can't create sticker with id {sticker_id}, url: {sticker_url}"
                )

            await asyncio.sleep(self.process_delay)

        self.last_executed_method = "clone_stickers"

    def get_channel_from_mapping(self, channel) -> interactions.GuildText | None:
        return self.mappings["channels"].get(channel.id) if channel else None

    # Commands

    module_base: interactions.SlashCommand = interactions.SlashCommand(
        name=interactions.LocalisedName(
            default_locale="english_us",
            english_us="clone",
            chinese_china="複製",
            chinese_taiwan="複製",
        ),
        description=interactions.LocalisedDesc(
            default_locale="english_us",
            english_us="Clone server",
            chinese_china="複製伺服器",
            chinese_taiwan="複製伺服器",
        ),
    )

    @module_base.subcommand(
        sub_cmd_name="process", sub_cmd_description="Process server copy state"
    )
    @interactions.slash_option(
        name="start",
        description="Start copying process",
        opt_type=interactions.OptionType.BOOLEAN,
    )
    @interactions.slash_option(
        name="reset",
        description="Reset clone progress and start fresh",
        opt_type=interactions.OptionType.BOOLEAN,
        required=False,
    )
    async def process(
        self,
        ctx: interactions.SlashContext,
        start: bool = True,
        reset: bool = False,
    ) -> None:
        try:
            await ctx.defer(ephemeral=True)

            if reset:
                self.last_executed_method = ""
                self.mappings = {}
                await ctx.send("Clone progress has been reset.", ephemeral=True)
                return

            last_state = await self.model.get_last_state(self.STATE_FILE)
            if last_state:
                self.last_executed_method = last_state.get("last_method", "")
                self.mappings = last_state.get("mappings", {})
                if self.last_executed_method:
                    await ctx.send(
                        f"Resuming from last step: {self.last_executed_method}",
                        ephemeral=True,
                    )
            if start and not self.mappings.get("fetched_data"):
                await self.fetch_guild_data()

            model_ref = proxy(self.model)

            async def auto_save(model=model_ref, file=self.STATE_FILE):
                try:
                    while True:
                        await asyncio.sleep(300)
                        state = {
                            "last_method": self.last_executed_method,
                            "mappings": self.mappings,
                        }
                        model.mappings = state
                        await model.save_state(file)
                        logger.info("Auto saved clone state")
                except asyncio.CancelledError:
                    state = {
                        "last_method": self.last_executed_method,
                        "mappings": self.mappings,
                    }
                    model.mappings = state
                    await model.save_state(file)
                    logger.info("Final auto save completed")
                    raise

            save_task = asyncio.create_task(auto_save())

            try:

                if start:
                    last_method = self.last_executed_method
                    conditions_to_functions = defaultdict(lambda: [] * 10)

                    def append_if_different(
                        condition: bool, msg: str, func: Callable[..., Coroutine]
                    ) -> None:
                        if condition and last_method != func.__name__:
                            conditions_to_functions[True].append((msg, func))

                    conditions_to_functions[True].append(
                        ("Creating new guild...", self.create_new_guild)
                    )

                    function_map = (
                        (
                            "prepare_server",
                            (self.prepare_server, "Preparing guild to process..."),
                        ),
                        (
                            "clone_settings",
                            (self.clone_settings, "Processing settings..."),
                        ),
                        ("clone_icon", (self.clone_icon, "Processing icon...")),
                        ("clone_banner", (self.clone_banner, "Processing banner...")),
                        ("clone_roles", (self.clone_roles, "Processing roles...")),
                        (
                            "clone_channels",
                            [
                                (self.clone_categories, "Processing categories..."),
                                (self.clone_channels, "Processing channels..."),
                            ],
                        ),
                        ("clone_emojis", (self.clone_emojis, "Processing emojis...")),
                        (
                            "clone_stickers",
                            (self.clone_stickers, "Processing stickers..."),
                        ),
                    )

                    for key, value in function_map:
                        attr = getattr(self, key, False)
                        if isinstance(value, list):
                            for func, msg in value:
                                append_if_different(attr, msg, func)
                        else:
                            func, msg = value
                            append_if_different(attr, msg, func)

                    steps = conditions_to_functions[True]
                    for msg, func in steps:
                        logger.info(msg)
                        await func()
                        state = {
                            "last_method": func.__name__,
                            "mappings": self.mappings,
                        }
                        self.model.mappings = state
                        await self.model.save_state(self.STATE_FILE)

            finally:
                save_task.cancel()
                try:
                    await save_task
                except asyncio.CancelledError:
                    pass

            await ctx.send("Process completed.", ephemeral=True)
        except Exception as e:
            logger.error(f"Process error: {e}", exc_info=True)
            await ctx.send("An error occurred during processing.", ephemeral=True)

    @module_base.subcommand(
        sub_cmd_name="migrate", sub_cmd_description="Migrate channel across servers"
    )
    @interactions.slash_option(
        "origin",
        "The origin channel to migrate from",
        interactions.OptionType.CHANNEL,
        required=True,
    )
    @interactions.slash_option(
        "server",
        "The destination server ID to migrate to",
        interactions.OptionType.STRING,
        required=True,
        argument_name="destination_server",
    )
    @interactions.slash_option(
        "channel",
        "The destination channel ID to migrate to",
        interactions.OptionType.CHANNEL,
        required=True,
        argument_name="destination_channel",
    )
    @interactions.slash_default_member_permission(
        interactions.Permissions.ADMINISTRATOR
    )
    async def migrate(
        self,
        ctx: interactions.SlashContext,
        origin: interactions.GuildChannel | interactions.ThreadChannel,
        destination_server: str,
        destination_channel: str,
    ) -> None:
        try:
            destination_guild = await self.bot.fetch_guild(destination_server)
            if not destination_guild:
                await ctx.send("Could not find the destination server.", ephemeral=True)
                return

            destination = await destination_guild.fetch_channel(destination_channel)
            if not destination:
                await ctx.send(
                    "Could not find the destination channel.", ephemeral=True
                )
                return

            valid_pairs = {
                (interactions.GuildText, interactions.GuildText),
                (interactions.GuildForum, interactions.GuildForum),
                (interactions.GuildForumPost, interactions.GuildForum),
                (interactions.GuildPublicThread, interactions.GuildText),
            }

            origin_type = type(origin)
            dest_type = type(destination)

            if (origin_type, dest_type) not in valid_pairs:
                await ctx.send(
                    "\n".join(
                        [
                            "Only the following migrations are supported:",
                            "- Text Channel -> Text Channel",
                            "- Forum -> Forum",
                            "- Public Thread in Text Channel -> Text Channel",
                            "- Forum Post -> Forum",
                        ]
                    ),
                    ephemeral=True,
                )
                return

            await ctx.send(
                f"Migrating {origin.mention} to {destination.mention} in server {destination_guild.name}...",
                ephemeral=True,
            )

            if origin_type in {interactions.GuildText, interactions.GuildForum}:
                await migrate_channel(
                    origin,
                    destination,
                    ctx.bot,
                    self.mappings,
                    ctx.guild.id,
                    destination_guild.id,
                )
            else:
                await migrate_thread(
                    origin,
                    destination,
                    ctx.bot,
                    self.mappings,
                    ctx.guild.id,
                    destination_guild.id,
                )

            await ctx.channel.send("Migration completed!")

        except NotFound:
            await ctx.send(
                "Could not find the specified server or channel.", ephemeral=True
            )
        except Forbidden:
            await ctx.send(
                "Bot does not have permission to access the destination server/channel.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Migration error: {e}", exc_info=True)
            await ctx.send(
                "Something went wrong. Please contact the admin!", ephemeral=True
            )

    @module_base.subcommand(
        sub_cmd_name="config", sub_cmd_description="Configure clone settings"
    )
    @interactions.slash_option(
        name="live",
        description="Enable/disable live message updates",
        opt_type=interactions.OptionType.BOOLEAN,
        argument_name="live_update",
    )
    @interactions.slash_option(
        name="webhook_delay",
        description="Set webhook delay (in seconds)",
        opt_type=interactions.OptionType.NUMBER,
        required=False,
    )
    @interactions.slash_option(
        name="process_delay",
        description="Set process delay (in seconds)",
        opt_type=interactions.OptionType.NUMBER,
        required=False,
    )
    @interactions.slash_default_member_permission(
        interactions.Permissions.ADMINISTRATOR
    )
    async def config(
        self,
        ctx: interactions.SlashContext,
        live_update: Optional[bool] = None,
        webhook_delay: Optional[float] = None,
        process_delay: Optional[float] = None,
    ) -> None:
        await ctx.defer(ephemeral=True)

        if webhook_delay is not None and (webhook_delay < 0.1 or webhook_delay > 5.0):
            await ctx.send(
                "Webhook delay must be between 0.1 and 5.0 seconds", ephemeral=True
            )
            return

        if process_delay is not None and (process_delay < 0.1 or process_delay > 5.0):
            await ctx.send(
                "Process delay must be between 0.1 and 5.0 seconds", ephemeral=True
            )
            return

        try:
            await self.model.load_config(self.CONFIG_FILE)
            current_config = self.model.mappings
        except Exception as e:
            logger.error(f"Error loading config: {e}", exc_info=True)
            current_config = {}

        if live_update is not None:
            current_config["live_update"] = live_update
            self.live_update = live_update

        if webhook_delay is not None:
            current_config["webhook_delay"] = webhook_delay
            self.webhook_delay = webhook_delay

        if process_delay is not None:
            current_config["process_delay"] = process_delay
            self.process_delay = process_delay

        current_config.update(
            {
                "live_update": self.live_update,
                "webhook_delay": self.webhook_delay,
                "process_delay": self.process_delay,
                "new_guild_id": str(self.new_guild.id) if self.new_guild else None,
            }
        )

        self.model.mappings = current_config
        try:
            await self.model.save_config(self.CONFIG_FILE)

            settings = [
                f"Live Update: {current_config.get('live_update', False)}",
                f"Webhook Delay: {current_config.get('webhook_delay', 0.2)}",
                f"Process Delay: {current_config.get('process_delay', 0.2)}",
                f"New Messages: {current_config.get('new_messages_enabled', False)}",
                f"Fetch Channels: {current_config.get('fetch_channels', True)}",
                f"Old Guild: {current_config.get('guild', None)}",
                f"New Guild: {current_config.get('new_guild_id', None)}",
            ]

            await ctx.send(
                "Configuration updated successfully.\n\n**Current Settings:**\n"
                + "\n".join(settings),
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error saving config: {e}", exc_info=True)
            await ctx.send(
                "An error occurred while saving the configuration.", ephemeral=True
            )

    @module_base.subcommand(
        sub_cmd_name=interactions.LocalisedName(
            default_locale="english_us",
            english_us="export",
            chinese_china="导出",
            chinese_taiwan="匯出",
        ),
        sub_cmd_description=interactions.LocalisedDesc(
            default_locale="english_us",
            english_us="Export files from the extension directory",
            chinese_china="从扩展目录导出文件",
            chinese_taiwan="從擴充目錄匯出檔案",
        ),
    )
    @interactions.slash_option(
        name=interactions.LocalisedName(
            default_locale="english_us",
            english_us="type",
            chinese_china="类型",
            chinese_taiwan="類型",
        ),
        description=interactions.LocalisedDesc(
            default_locale="english_us",
            english_us="Type of files to export",
            chinese_china="要导出的文件类型",
            chinese_taiwan="要匯出的檔案類型",
        ),
        required=True,
        opt_type=interactions.OptionType.STRING,
        autocomplete=True,
        argument_name="file_type",
    )
    @interactions.slash_default_member_permission(
        interactions.Permissions.ADMINISTRATOR
    )
    async def debug_export(
        self, ctx: interactions.SlashContext, file_type: str
    ) -> None:
        await ctx.defer(ephemeral=True)
        filename: str = ""
        locale = ctx.locale or "default"

        if not os.path.exists(BASE_DIR):
            error_messages = {
                "default": "Extension directory does not exist.",
                "chinese_china": "扩展目录不存在。",
                "chinese_taiwan": "擴充目錄不存在。",
            }
            await ctx.send(error_messages.get(locale, error_messages["default"]))
            return None

        if file_type != "all" and not os.path.isfile(os.path.join(BASE_DIR, file_type)):
            error_messages = {
                "default": f"File `{file_type}` does not exist in the extension directory.",
                "chinese_china": f"文件 `{file_type}` 在扩展目录中不存在。",
                "chinese_taiwan": f"檔案 `{file_type}` 在擴充目錄中不存在。",
            }
            await ctx.send(error_messages.get(locale, error_messages["default"]))
            return None
        try:
            async with aiofiles.tempfile.NamedTemporaryFile(
                prefix="export_", suffix=".tar.gz", delete=False
            ) as afp:
                filename = afp.name
                base_name = filename[:-7]

                await aioshutil.make_archive(
                    base_name,
                    "gztar",
                    BASE_DIR,
                    "." if file_type == "all" else file_type,
                )

            if not os.path.exists(filename):
                error_messages = {
                    "default": "Failed to create archive file.",
                    "chinese_china": "创建归档文件失败。",
                    "chinese_taiwan": "建立壓縮檔案失敗。",
                }
                await ctx.send(error_messages.get(locale, error_messages["default"]))
                return None

            file_size = os.path.getsize(filename)
            if file_size > 8_388_608:
                error_messages = {
                    "default": "Archive file is too large to send (>8MB).",
                    "chinese_china": "归档文件太大，无法发送（>8MB）。",
                    "chinese_taiwan": "壓縮檔案太大，無法發送（>8MB）。",
                }
                await ctx.send(error_messages.get(locale, error_messages["default"]))
                return None
            success_messages = {
                "default": (
                    "All extension files attached."
                    if file_type == "all"
                    else f"File `{file_type}` attached."
                ),
                "chinese_china": (
                    "已附加所有扩展文件。"
                    if file_type == "all"
                    else f"已附加文件 `{file_type}`。"
                ),
                "chinese_taiwan": (
                    "已附加所有擴充檔案。"
                    if file_type == "all"
                    else f"已附加檔案 `{file_type}`。"
                ),
            }
            await ctx.send(
                success_messages.get(locale, success_messages["default"]),
                files=[interactions.File(filename)],
            )

        except PermissionError:
            logger.error(f"Permission denied while exporting {file_type}")
            error_messages = {
                "default": "Permission denied while accessing files.",
                "chinese_china": "访问文件时权限被拒绝。",
                "chinese_taiwan": "存取檔案時權限被拒絕。",
            }
            await ctx.send(error_messages.get(locale, error_messages["default"]))
        except Exception as e:
            logger.error(f"Error exporting {file_type}: {e}", exc_info=True)
            error_messages = {
                "default": f"An error occurred while exporting {file_type}: {str(e)}",
                "chinese_china": f"导出 {file_type} 时发生错误：{str(e)}",
                "chinese_taiwan": f"匯出 {file_type} 時發生錯誤：{str(e)}",
            }
            await ctx.send(error_messages.get(locale, error_messages["default"]))
        finally:
            if filename and os.path.exists(filename):
                try:
                    os.unlink(filename)
                except Exception as e:
                    logger.error(f"Error cleaning up temp file: {e}", exc_info=True)

    @debug_export.autocomplete("type")
    async def autocomplete_debug_export_type(
        self, ctx: interactions.AutocompleteContext
    ) -> None:
        choices: list[dict[str, str]] = [{"name": "All Files", "value": "all"}]

        try:
            if os.path.exists(BASE_DIR):
                files = [
                    f
                    for f in os.listdir(BASE_DIR)
                    if os.path.isfile(os.path.join(BASE_DIR, f))
                    and not f.startswith(".")
                ]

                choices.extend({"name": file, "value": file} for file in sorted(files))
        except PermissionError:
            logger.error("Permission denied while listing files")
            choices = [{"name": "Error: Permission denied", "value": "error"}]
        except Exception as e:
            logger.error(f"Error listing files: {e}", exc_info=True)
            choices = [{"name": f"Error: {str(e)}", "value": "error"}]

        await ctx.send(choices[:25])

    @module_base.subcommand(
        sub_cmd_name="invite",
        sub_cmd_description="Generate an invite link for the server",
    )
    @interactions.slash_option(
        name="server",
        description="Choose which server to generate invite for",
        opt_type=interactions.OptionType.STRING,
        required=True,
        autocomplete=True,
    )
    @interactions.slash_option(
        name="duration",
        description="Invite duration in hours (default: 24, max: 168)",
        opt_type=interactions.OptionType.INTEGER,
        required=False,
        min_value=1,
        max_value=168,
    )
    @interactions.slash_default_member_permission(
        interactions.Permissions.CREATE_INSTANT_INVITE
    )
    async def generate_invite(
        self, ctx: interactions.SlashContext, server: str, duration: int = 24
    ) -> None:
        await ctx.defer(ephemeral=True)

        try:
            max_age = duration * 3600

            channel = None
            target_guild = await self.bot.fetch_guild(server)
            if target_guild.system_channel_id:
                try:
                    channel = await target_guild.fetch_channel(
                        target_guild.system_channel_id
                    )
                except Exception as e:
                    logger.error(f"Error fetching system channel: {e}", exc_info=True)

            if not channel:
                try:
                    channels = [
                        c
                        for c in await target_guild.fetch_channels()
                        if isinstance(c, interactions.GuildText)
                    ]
                    if not channels:
                        channel = await target_guild.create_channel(
                            name="general",
                            channel_type=interactions.ChannelType.GUILD_TEXT,
                            reason="Created for invite generation",
                        )
                        logger.info(
                            f"Created new channel {channel.name} for invite generation"
                        )
                    else:
                        channel = channels[0]
                except Exception as e:
                    logger.error(
                        f"Error fetching/creating channels: {e}", exc_info=True
                    )
                    await ctx.send(
                        "Failed to fetch or create channels.", ephemeral=True
                    )
                    return

            invite = await channel.create_invite(
                max_age=max_age,
                max_uses=0,
                temporary=False,
                unique=True,
                reason=f"Invite generated by {ctx.author.display_name}",
            )

            await ctx.send(
                f"Here's your invite link for {target_guild.name}:\n"
                f"{invite.link} (Expires in {duration} hours)",
                ephemeral=True,
            )

        except Forbidden:
            await ctx.send(
                "I don't have permission to create invites in that server.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error generating invite: {e}", exc_info=True)
            await ctx.send(
                "An error occurred while generating the invite.", ephemeral=True
            )

    @generate_invite.autocomplete("server")
    async def autocomplete_server_choice(
        self, ctx: interactions.AutocompleteContext
    ) -> None:
        choices = []

        for guild in self.bot.guilds:
            choices.append(
                {"name": f"{guild.name} ({guild.id})", "value": str(guild.id)}
            )

        await ctx.send(choices)

    @module_base.subcommand(
        sub_cmd_name="delete",
        sub_cmd_description="Delete a server that the bot is managing",
    )
    @interactions.slash_option(
        name="server",
        description="Choose which server to delete",
        opt_type=interactions.OptionType.STRING,
        required=True,
        autocomplete=True,
    )
    @interactions.slash_default_member_permission(
        interactions.Permissions.ADMINISTRATOR
    )
    async def delete_server(self, ctx: interactions.SlashContext, server: str) -> None:
        await ctx.defer(ephemeral=True)

        try:
            target_guild = await self.bot.fetch_guild(server)
            member = await target_guild.fetch_member(ctx.author.id)
            if not member or not member.has_permission(
                interactions.Permissions.ADMINISTRATOR
            ):
                await ctx.send(
                    "You must have Administrator permission in the target server to delete it.",
                    ephemeral=True,
                )
                return

            await target_guild.delete()

            await ctx.send(
                f"Successfully deleted server: {target_guild.name}", ephemeral=True
            )

        except Forbidden:
            await ctx.send(
                "I don't have permission to delete that server.", ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error deleting server: {e}", exc_info=True)
            await ctx.send(
                "An error occurred while trying to delete the server.", ephemeral=True
            )

    @delete_server.autocomplete("server")
    async def autocomplete_server_delete(
        self, ctx: interactions.AutocompleteContext
    ) -> None:
        choices = []

        for guild in self.bot.guilds:
            choices.append(
                {"name": f"{guild.name} ({guild.id})", "value": str(guild.id)}
            )

        await ctx.send(choices)

    # Utility

    @staticmethod
    def truncate_string(
        string: str, length: int, replace_newline_with: str = " "
    ) -> str:
        return (
            (s := string.replace("\n", replace_newline_with))[: length - 3].strip()
            + "..."
            if len(s := string.replace("\n", replace_newline_with)) > length
            else s.strip()
        )

    @staticmethod
    def split_messages_by_channel(
        messages_queue: deque,
    ) -> Dict[interactions.GuildText, List[Any]]:
        channel_messages_map: Dict[interactions.GuildText, List[Any]] = defaultdict(
            list
        )
        while messages_queue:
            channel, message = messages_queue.popleft()
            channel_messages_map[channel].append(message)
        return dict(channel_messages_map)

    @staticmethod
    def format_time(delta: timedelta) -> str:
        time_parts = (
            ("year", delta.days // 365),
            ("day", delta.days % 365),
            ("hour", delta.seconds // 3600),
            ("minute", (delta.seconds % 3600) // 60),
            ("second", delta.seconds % 60),
        )
        return " ".join(f"{v} {n}{'s' if v != 1 else ''}" for n, v in time_parts if v)

    @staticmethod
    async def retry_with_backoff(coro, max_retries=3, base_delay=1, max_delay=60):
        for attempt in range(max_retries):
            try:
                return await coro
            except HTTPException as e:
                if e.status == 429:
                    retry_after = min(
                        float(e.response.headers.get("Retry-After", base_delay))
                        * (2**attempt),
                        max_delay,
                    )
                    logger.warning(f"Rate limited, waiting {retry_after}s before retry")
                    await asyncio.sleep(retry_after)
                    continue
                raise
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                logger.warning(f"Attempt {attempt + 1} failed: {e}", exc_info=True)
                await asyncio.sleep(base_delay * (2**attempt))
        return await coro
