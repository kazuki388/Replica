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
import aiohttp
import aioshutil
import interactions
import orjson
from interactions.api.events import MessageCreate
from interactions.client.errors import Forbidden, HTTPException, NotFound

BASE_DIR: str = os.path.abspath(os.path.dirname(__file__))
LOG_FILE: str = os.path.join(BASE_DIR, "clone.log")

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
            logger.error(f"Error loading state: {e}")

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
            logger.error(f"Error saving state: {e}")
            raise

    async def load_config(self, file_path: str) -> None:
        try:
            async with aiofiles.open(file_path) as file:
                raw_data = await file.read()
                self.mappings = orjson.loads(memoryview(raw_data.encode()))
                logger.info("Successfully loaded config")
        except FileNotFoundError:
            logger.warning("Config file not found")
        except Exception as e:
            logger.error(f"Error loading state: {e}")

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
            logger.error(f"Error saving config: {e}")
            raise


class Clone(interactions.Extension):
    def __init__(self, bot: interactions.Client) -> None:
        self.bot: interactions.Client = bot
        self.model: Model = Model()

        self.CONFIG_FILE: str = os.path.join(BASE_DIR, "config.json")
        self.STATE_FILE: str = os.path.join(BASE_DIR, "state.json")

        self.guild: interactions.Guild = None
        self.new_guild: interactions.Guild = None
        self.enabled_community: bool = False
        self.delay: float = 0
        self.disable_fetch_channels: bool = False
        self.clone_messages_toggled: bool = False
        self.webhook_delay: float = 0
        self.live_update: bool = False
        self.new_messages_enabled: bool = False

        self.message_queue: deque[tuple] = deque(maxlen=10000)
        self.new_messages_queue: deque[tuple] = deque(maxlen=1000)
        self.processed_channels: list[int] = []
        self.mappings: dict[str, dict] = {}

    async def initialize_data(self) -> None:
        await self.model.load_state(self.STATE_FILE)
        try:
            await self.model.load_config(self.CONFIG_FILE)
            config = self.model.mappings
            self.clone_messages_toggled = config.get("clone_messages", False)
            self.webhook_delay = config.get("webhook_delay", 0.85)
            self.delay = config.get("process_delay", 0.85)
            self.live_update = config.get("live_update", False)
            self.disable_fetch_channels = config.get("disable_fetch_channels", False)
        except Exception as e:
            logger.warning(f"Error loading config file, using default settings: {e}")
            self.clone_messages_toggled = False
            self.webhook_delay = int(0.85 * 1000)
            self.delay = int(0.85 * 1000)
            self.live_update = False
            self.disable_fetch_channels = False

    async def find_webhook(self, channel_id: int) -> interactions.Webhook | None:
        return self.mappings["webhooks"].get(str(channel_id))

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

    async def populate_queue(self, limit: int = 512) -> None:
        for channel_id, new_channel in self.mappings["channels"].items():
            try:
                original_channel: interactions.GuildText = (
                    await self.guild.fetch_channel(channel_id)
                )

                if isinstance(
                    original_channel,
                    (interactions.GuildForum, interactions.GuildStageVoice),
                ):
                    continue

                async for message in original_channel.history(limit=limit):
                    self.message_queue.append((new_channel, message))
            except Forbidden:
                logger.debug(
                    f"Can't fetch channel message history (no permissions): {channel_id}"
                )

    async def prepare_server(self) -> None:
        cleanup_methods = {
            "roles": lambda: self.bot.http.get_roles(self.new_guild.id),
            "channels": self.new_guild.fetch_channels,
            "emojis": lambda: self.bot.http.get_all_guild_emoji(self.new_guild.id),
            "stickers": lambda: self.bot.http.list_guild_stickers(self.new_guild.id),
        }

        if "COMMUNITY" in self.guild.features:
            self.enabled_community = True
            logger.warning(
                "Community mode is toggled. Will be set up after channel processing (if enabled)."
            )

        for method_name, method in cleanup_methods.items():
            logger.debug(f"Processing cleaning method: {method_name}...")
            await self.cleanup_items(await method())

        self.last_executed_method = "prepare_server"

    async def cleanup_items(self, items) -> None:
        for item in items:
            try:
                await item.delete()
            except HTTPException:
                continue
            await asyncio.sleep(self.delay)

        await self.new_guild.edit(icon=None, banner=None, description=None)

    async def clone_icon(self) -> None:
        if self.guild.icon:
            await self.new_guild.edit(icon=BytesIO(await self.guild.icon.fetch()))
        await asyncio.sleep(self.delay)
        self.last_executed_method = "clone_icon"

    async def clone_banner(self) -> None:
        if self.guild.banner:
            await self.new_guild.edit(banner=BytesIO(await self.guild.banner.fetch()))
            await asyncio.sleep(self.delay)
        self.last_executed_method = "clone_banner"

    async def clone_roles(self):
        roles_create = [role for role in self.mappings["fetched_data"]["roles"]]
        self.mappings["roles"].update(
            {
                role.id: await self.new_guild.fetch_role(role.id)
                for role in roles_create
                if role.name == "@everyone"
            }
        )

        for role in reversed(roles_create):
            if role.name == "@everyone":
                await (await self.new_guild.fetch_role(role.id)).edit(
                    name=role.name,
                    color=role.color,
                    hoist=role.hoist,
                    mentionable=role.mentionable,
                    permissions=role.permissions,
                )
                await asyncio.sleep(self.delay)
                continue

            self.mappings["roles"][role.id] = new_role = (
                await self.new_guild.create_role(
                    name=role.name,
                    color=role.color,
                    hoist=role.hoist,
                    mentionable=role.mentionable,
                    permissions=role.permissions,
                )
            )
            await self.create_object_log(
                object_type="role", object_name=new_role.name, object_id=new_role.id
            )
            await asyncio.sleep(self.delay)
        self.last_executed_method = "clone_roles"

    async def clone_categories(self, perms: bool = True) -> None:
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
            await asyncio.sleep(self.delay)
        self.last_executed_method = "clone_categories"

    async def clone_channels(self, perms: bool = True) -> None:
        for channel in self.mappings["fetched_data"]["channels"]:
            if not self.disable_fetch_channels:
                try:
                    channel = await self.guild.fetch_channel(channel.id)
                except Forbidden:
                    logger.debug(f"Can't fetch channel {channel.name} | {channel.id}")
                    continue

            category = self.mappings["categories"].get(channel.category_id)
            overwrites = {}
            for overwrite in channel.permission_overwrites:
                if perms and isinstance(overwrite.id, interactions.Role):
                    perm = interactions.PermissionOverwrite.for_target(overwrite.id)
                    if overwrite.allow:
                        perm.add_allows(overwrite.allow)
                    if overwrite.deny:
                        perm.add_denies(overwrite.deny)
                    overwrites[self.mappings["roles"][overwrite.id]] = perm

            if overwrites:
                logger.debug(f"Got overwrites mapping for channel #{channel.name}")

            if isinstance(channel, interactions.GuildText):
                self.mappings["channels"][channel.id] = new_channel = (
                    await self.new_guild.create_text_channel(
                        name=channel.name,
                        position=channel.position,
                        topic=channel.topic,
                        rate_limit_per_user=channel.rate_limit_per_user,
                        nsfw=channel.nsfw,
                        category=category,
                        permission_overwrites=overwrites,
                    )
                )
                await self.create_channel_log(
                    channel_type="text",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )
            elif isinstance(channel, interactions.GuildVoice):
                self.mappings["channels"][channel.id] = new_channel = (
                    await self.new_guild.create_voice_channel(
                        name=channel.name,
                        position=channel.position,
                        bitrate=min(channel.bitrate, self.new_guild.bitrate_limit),
                        user_limit=channel.user_limit,
                        category=category,
                        permission_overwrites=overwrites,
                    )
                )
                await self.create_channel_log(
                    channel_type="voice",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )
            await asyncio.sleep(self.delay)

        if self.enabled_community:
            logger.info("Processing community settings")
            if await self.process_community():
                logger.info("Processing community channels")
                await self.add_community_channels(perms=perms)

        self.last_executed_method = "clone_channels"

    async def process_community(self) -> bool:
        if not self.enabled_community:
            return False

        channels = {
            "afk": self.get_channel_from_mapping(self.guild.afk_channel_id),
            "system": self.get_channel_from_mapping(self.guild.system_channel),
            "public_updates": self.get_channel_from_mapping(
                self.guild.public_updates_channel
            ),
            "rules": self.get_channel_from_mapping(self.guild.rules_channel),
        }

        if not channels["public_updates"]:
            logger.error(
                "Can't create community: missing access to public updates channel"
            )
            return False

        await self.new_guild.edit(
            features=["COMMUNITY"],
            verification_level=self.guild.verification_level,
            default_message_notifications=self.guild.default_message_notifications,
            afk_channel=channels["afk"],
            afk_timeout=self.guild.afk_timeout,
            system_channel=channels["system"],
            system_channel_flags=self.guild.system_channel_flags,
            rules_channel=channels["rules"],
            public_updates_channel=channels["public_updates"],
            explicit_content_filter=self.guild.explicit_content_filter,
            preferred_locale=self.guild.preferred_locale,
            premium_progress_bar_enabled=self.guild.premium_progress_bar_enabled,
        )
        logger.debug("Updated guild community settings")
        await asyncio.sleep(self.delay)
        return True

    async def add_community_channels(self, perms: bool = True) -> None:
        if not self.enabled_community:
            return

        channels = [
            c
            for c in self.mappings["fetched_data"]["channels"]
            if isinstance(c, (interactions.GuildForum, interactions.GuildStageVoice))
        ]

        for channel in channels:
            category = (
                self.mappings["categories"].get(channel.parent_id)
                if channel.parent_id
                else None
            )
            overwrites = {}

            if perms and channel.permission_overwrites:
                overwrites = {
                    self.mappings["roles"][o.id]: (
                        (perm := interactions.PermissionOverwrite.for_target(o.id))
                        and [
                            perm.add_allows(o.allow) if o.allow else None,
                            perm.add_denies(o.deny) if o.deny else None,
                        ]
                        and perm
                    )
                    for o in channel.permission_overwrites
                    if isinstance(o.id, interactions.Role)
                }

            if isinstance(channel, interactions.GuildForum):
                tags = channel.available_tags
                for tag in tags:
                    if tag.emoji.id:
                        tag.emoji = self.mappings["emojis"].get(tag.emoji.id)

                new_channel = await self.new_guild.create_forum_channel(
                    name=channel.name,
                    topic=channel.topic,
                    position=channel.position,
                    category=category,
                    nsfw=channel.nsfw,
                    permission_overwrites=overwrites,
                    layout=channel.default_forum_layout,
                    rate_limit_per_user=channel.rate_limit_per_user,
                    sort_order=channel.default_sort_order,
                    available_tags=tags,
                )
                self.mappings["channels"][channel.id] = new_channel
                await self.create_channel_log("forum", new_channel.name, new_channel.id)

            elif isinstance(channel, interactions.GuildStageVoice):
                new_channel = await self.new_guild.create_stage_channel(
                    name=channel.name,
                    category=category,
                    position=channel.position,
                    bitrate=min(channel.bitrate, self.new_guild.bitrate_limit),
                    user_limit=channel.user_limit,
                    permission_overwrites=overwrites,
                )
                self.mappings["channels"][channel.id] = new_channel
                await self.create_channel_log("stage", new_channel.name, new_channel.id)

            await asyncio.sleep(self.delay)

        self.last_executed_method = "add_community_channels"

    async def clone_emojis(self) -> None:
        emoji_limit = min(
            self.new_guild.emoji_limit - 5,
            (
                current_emojis := await self.new_guild.fetch_all_custom_emojis()
            ).__len__(),
        )
        emoji_data = [
            (emoji.name, emoji.roles, await emoji.read())
            for emoji in self.mappings["fetched_data"]["emojis"][:emoji_limit]
            if len(current_emojis) < emoji_limit
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
            await asyncio.sleep(self.delay)

        self.last_executed_method = "clone_emojis"

    async def clone_stickers(self) -> None:
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

            await asyncio.sleep(self.delay)

        self.last_executed_method = "clone_stickers"

    async def send_webhook(
        self,
        webhook: interactions.Webhook,
        message: interactions.Message,
        delay: float = 0.85,
    ) -> None:
        author: interactions.User = message.author
        files = []

        if message.attachments:
            async with aiohttp.ClientSession() as session:
                files.extend(
                    [
                        interactions.File(
                            file=BytesIO(await (await session.get(a.url)).read()),
                            file_name=a.filename,
                        )
                        for a in message.attachments
                        if a.url and (await session.get(a.url)).status == 200
                    ]
                )

        name = (
            f"{author.display_name} at {message.created_at.strftime('%d/%m/%Y %H:%M')}"
        )
        content = message.content

        replacements = {
            **{
                f"<#{old_id}>": f"<#{new.id}>"
                for old_id, new in self.mappings.get("channels", {}).items()
            },
            **{
                f"<@&{old_id}>": f"<@&{new.id}>"
                for old_id, new in self.mappings.get("roles", {}).items()
            },
            **{
                f"https://discord.com/channels/{self.guild.id}/{old_id}": f"https://discord.com/channels/{self.new_guild.id}/{new.id}"
                for old_id, new in self.mappings.get("channels", {}).items()
            },
        }

        for old, new in replacements.items():
            content = content.replace(old, new)

        try:
            await webhook.send(
                content=content,
                avatar_url=author.display_avatar.url,
                username=name,
                embeds=message.embeds,
                files=files,
            )

            if message.content:
                truncated = self.truncate_string(
                    string=message.content, length=32, replace_newline_with=""
                ).rstrip()

                logger.debug(
                    f"Cloned message from {author.display_name}"
                    + (f": {truncated}" if truncated else "")
                )

        except (HTTPException, Forbidden):
            logger.debug(
                f"Can't send, skipping message in #{message.channel.name if message.channel else ''}"
            )

        await asyncio.sleep(delay)

    async def clone_messages(
        self,
        messages_limit: int = 100,
        clear_webhooks: bool = False,
    ) -> None:
        if not self.clone_messages_toggled:
            return

        self.processing_messages = True
        await self.populate_queue(messages_limit)

        queue_len = len(self.message_queue)
        logger.debug(f"Collected {queue_len} messages")

        total_latency = queue_len * (self.webhook_delay + self.bot.latency)
        logger.info(
            f"Calculated message cloning ETA: {self.format_time(timedelta(seconds=total_latency))}"
        )

        await self.clone_messages_from_queue(clear_webhooks=clear_webhooks)
        self.last_executed_method = "clone_messages"

    async def cleanup_after_cloning(self, clear: bool = False) -> None:
        self.message_queue.clear()

        if clear:
            webhooks = self.mappings["webhooks"]
            for webhook in webhooks.values():
                await webhook.delete()
                await asyncio.sleep(self.webhook_delay)
            webhooks.clear()

        self.processing_messages = False
        self.last_executed_method = "cleanup_after_cloning"

    async def clone_messages_from_queue(self, clear_webhooks: bool = True) -> None:
        try:
            if not self.message_queue:
                logger.warning("Message queue is empty")
                return

            if channel_msgs := self.split_messages_by_channel(self.message_queue):
                await self.process_messages_channel_map(channel_msgs)

            if self.new_messages_queue:
                await asyncio.sleep(self.webhook_delay)
                if new_msgs := self.split_messages_by_channel(self.new_messages_queue):
                    await self.process_messages_channel_map(new_msgs)
        except Exception as e:
            logger.error(f"Error processing message queue: {e}")
        finally:
            await self.cleanup_after_cloning(clear=clear_webhooks)

    def get_channel_from_mapping(self, channel) -> interactions.GuildText | None:
        return self.mappings["channels"].get(channel.id) if channel else None

    async def process_messages_channel_map(self, channel_messages_map: Dict) -> None:
        while channel_messages_map:
            for channel, messages in list(channel_messages_map.items()):
                if messages:
                    await self.clone_message_with_delay(channel, messages.pop(0))
                    await asyncio.sleep(self.webhook_delay)
                else:
                    self.processed_channels.append(channel.id)
                    del channel_messages_map[channel]

    async def clone_message_with_delay(
        self, channel: interactions.GuildText, message: interactions.Message
    ) -> None:
        webhook = self.find_webhook(channel.id)
        if not webhook:
            try:
                webhook = await channel.create_webhook(name="bot by itskekoff")
                await asyncio.sleep(self.webhook_delay)
                await self.create_webhook_log(channel_name=channel.name)
                self.mappings["webhooks"][channel.id] = webhook
            except (NotFound, Forbidden) as e:
                logger.debug(
                    f"Can't create webhook: {'unknown channel' if isinstance(e, NotFound) else 'missing permissions'}"
                )
                return

        try:
            await self.send_webhook(webhook, message)
        except Forbidden:
            channel_name = getattr(message.channel, "name", "unknown")
            logger.debug(f"Missing access for channel: #{channel_name}")

    @interactions.listen(MessageCreate)
    async def on_message_create(self, event: MessageCreate) -> None:
        message = event.message
        guild = message.guild
        if not (guild and guild.id == self.guild.id):
            return

        try:
            if not self.live_update:
                return

            channel_id = message.channel.id
            new_channel = self.mappings["channels"].get(channel_id)

            if new_channel is None:
                logger.warning(
                    "Can't clone message from channel that doesn't exists in new guild"
                )
                return

            if (
                self.processing_messages
                and new_channel.id not in self.processed_channels
            ):
                if self.new_messages_enabled:
                    self.new_messages_queue.append((new_channel, message))
                return

            await self.clone_message_with_delay(new_channel, message)
            await asyncio.sleep(self.webhook_delay)

        except KeyError:
            return

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
    async def process(
        self,
        ctx: interactions.SlashContext,
        start: bool = True,
    ) -> None:
        await ctx.defer(ephemeral=True)

        model_ref = proxy(self.model)

        async def auto_save(model=model_ref, file=self.STATE_FILE):
            await asyncio.sleep(300)
            await model.save_state(file)
            logger.info("Auto saved clone state")

        asyncio.create_task(auto_save())

        if start:
            last_method = self.last_executed_method
            conditions_to_functions = defaultdict(lambda: [] * 10)

            def append_if_different(
                condition: bool, msg: str, func: Callable[..., Coroutine]
            ) -> None:
                if condition and last_method != func.__name__:
                    conditions_to_functions[True].append((msg, func))

            function_map = (
                ("clear_guild", (self.prepare_server, "Preparing guild to process...")),
                ("clone_icon", (self.clone_icon, "Processing server icon...")),
                ("clone_banner", (self.clone_banner, "Processing server banner...")),
                ("clone_roles", (self.clone_roles, "Processing server roles...")),
                (
                    "clone_channels",
                    [
                        (self.clone_categories, "Processing server categories..."),
                        (self.clone_channels, "Processing server channels..."),
                    ],
                ),
                ("clone_emojis", (self.clone_emojis, "Processing server emojis...")),
                ("clone_stickers", (self.clone_stickers, "Processing stickers...")),
                (
                    "clone_messages",
                    (self.clone_messages, "Processing server messages..."),
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

            funcs = conditions_to_functions[True]
            for msg, func in funcs:
                logger.info(msg)
                await func()
                await self.model.save_state(self.STATE_FILE)

        await ctx.send("Process completed.", ephemeral=True)

    @module_base.subcommand(
        sub_cmd_name="config", sub_cmd_description="Configure clone settings"
    )
    @interactions.slash_option(
        name="clone_messages",
        description="Enable/disable message cloning",
        opt_type=interactions.OptionType.BOOLEAN,
        required=False,
    )
    @interactions.slash_option(
        name="webhook_delay",
        description="Set delay between webhook messages (in seconds)",
        opt_type=interactions.OptionType.NUMBER,
        required=False,
        min_value=0.1,
        max_value=5.0,
    )
    @interactions.slash_option(
        name="process_delay",
        description="Set delay between clone operations (in seconds)",
        opt_type=interactions.OptionType.NUMBER,
        required=False,
        min_value=0.1,
        max_value=5.0,
    )
    @interactions.slash_option(
        name="live_update",
        description="Enable/disable live message updates",
        opt_type=interactions.OptionType.BOOLEAN,
        required=False,
    )
    @interactions.slash_option(
        name="disable_fetch_channels",
        description="Disable channel fetching (faster but less accurate)",
        opt_type=interactions.OptionType.BOOLEAN,
        required=False,
    )
    @interactions.slash_default_member_permission(
        interactions.Permissions.ADMINISTRATOR
    )
    async def config(
        self,
        ctx: interactions.SlashContext,
        clone_messages: Optional[bool] = None,
        webhook_delay: Optional[float] = None,
        process_delay: Optional[float] = None,
        live_update: Optional[bool] = None,
        disable_fetch_channels: Optional[bool] = None,
    ) -> None:
        await ctx.defer(ephemeral=True)

        try:
            await self.model.load_config(self.CONFIG_FILE)
            current_config = self.model.mappings
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            current_config = {}

        if clone_messages is not None:
            current_config["clone_messages"] = clone_messages
            self.clone_messages_toggled = clone_messages

        if webhook_delay is not None:
            current_config["webhook_delay"] = webhook_delay
            self.webhook_delay = webhook_delay

        if process_delay is not None:
            current_config["process_delay"] = process_delay
            self.delay = process_delay

        if live_update is not None:
            current_config["live_update"] = live_update
            self.live_update = live_update

        if disable_fetch_channels is not None:
            current_config["disable_fetch_channels"] = disable_fetch_channels
            self.disable_fetch_channels = disable_fetch_channels

        self.model.mappings = current_config
        try:
            await self.model.save_config(self.CONFIG_FILE)

            settings = [
                f"Clone Messages: {current_config.get('clone_messages', False)}",
                f"Webhook Delay: {current_config.get('webhook_delay', 0.85)}s",
                f"Process Delay: {current_config.get('process_delay', 0.85)}s",
                f"Live Update: {current_config.get('live_update', False)}",
                f"Disable Channel Fetching: {current_config.get('disable_fetch_channels', False)}",
            ]

            await ctx.send(
                "Configuration updated successfully!\n\n**Current Settings:**\n"
                + "\n".join(settings),
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error saving config: {e}")
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
                    logger.error(f"Error cleaning up temp file: {e}")

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
