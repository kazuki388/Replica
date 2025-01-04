from __future__ import annotations

import asyncio
import os
from collections import deque
from io import BytesIO
from typing import Any, Optional

import aiofiles
import aiofiles.os
import aiofiles.ospath
import aioshutil
import interactions
import orjson
from interactions.api.events import MemberAdd
from interactions.client.errors import Forbidden, NotFound

from .config import BASE_DIR, setup_logger
from .lib import migrate_channel, migrate_thread

logger = setup_logger(__name__)


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

    @staticmethod
    async def get_last_state(file_path: str) -> dict:
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

        self.webhook_delay: float = 0.2
        self.process_delay: float = 0.2
        self.admin_user_id: Optional[int] = None
        self.source_guild_id: Optional[int] = None
        self.source_guild: Optional[interactions.Guild] = None
        self.target_guild: Optional[interactions.Guild] = None

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

    # Log

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

    # Events
    @interactions.listen(MemberAdd)
    async def on_member_join(self, event: MemberAdd):
        if not self.target_guild:
            return

        if event.member.id == self.admin_user_id:
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

    # Base Command

    module_base: interactions.SlashCommand = interactions.SlashCommand(
        name="replica",
        description="Clone server",
        default_member_permissions=interactions.Permissions.ADMINISTRATOR,
    )

    # Initialize Command

    @module_base.subcommand(
        sub_cmd_name="initialize",
        sub_cmd_description="Initialize bot configuration and data",
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def initialize_data(self, ctx: interactions.SlashContext) -> None:
        try:
            await self.model.load_state(self.STATE_FILE)
            await self.model.load_config(self.CONFIG_FILE)
            config = self.model.mappings

            self.webhook_delay = config.get("webhook_delay", 0.2)
            self.process_delay = config.get("process_delay", 0.2)
            self.admin_user_id = (
                int(config["admin_user_id"]) if config.get("admin_user_id") else None
            )
            self.source_guild_id = (
                int(config["source_guild_id"])
                if config.get("source_guild_id")
                else None
            )

            if self.source_guild_id:
                self.source_guild = await self.bot.fetch_guild(self.source_guild_id)

            if config.get("target_guild_id"):
                self.target_guild = await self.bot.fetch_guild(
                    int(config["target_guild_id"])
                )

            await ctx.send("Successfully initialized bot configuration and data")

        except Exception as e:
            logger.warning(f"Error loading config file, using default settings: {e}")
            self.webhook_delay = self.process_delay = 0.2
            await ctx.send(f"Error initializing: {e}")

    # Create Command

    @module_base.subcommand(
        sub_cmd_name="create", sub_cmd_description="Create a new server for cloning"
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def create_guild_cmd(self, ctx: interactions.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            if not self.source_guild:
                await ctx.send("Source guild is not set.", ephemeral=True)
                return

            templates = await self.source_guild.fetch_guild_templates()
            if not templates:
                await ctx.send("No templates found in source guild.", ephemeral=True)
                return

            template = templates[0]
            guild_name = f"{self.source_guild.name} (Dyad)"

            self.target_guild = await self.bot.create_guild_from_template(
                template, guild_name
            )

            if not self.target_guild:
                await ctx.send("Failed to create guild from template.", ephemeral=True)
                return

            await ctx.send("Successfully created new server!", ephemeral=True)

        except Exception as e:
            logger.error(f"Error creating new guild: {e}", exc_info=True)
            await ctx.send(
                "An error occurred while creating new server.", ephemeral=True
            )

    async def create_new_guild(self) -> None:
        try:
            if not self.source_guild:
                raise ValueError("Source guild is not set")

            self.target_guild = await interactions.Guild.create(
                name=f"{self.source_guild.name} (Dyad)",
                client=self.bot,
                verification_level=(
                    self.source_guild.verification_level
                    if self.source_guild.verification_level
                    else interactions.VerificationLevel.NONE
                ),
                default_message_notifications=(
                    self.source_guild.default_message_notifications
                    if self.source_guild.default_message_notifications
                    else interactions.DefaultNotificationLevel.ALL_MESSAGES
                ),
                explicit_content_filter=(
                    self.source_guild.explicit_content_filter
                    if self.source_guild.explicit_content_filter
                    else interactions.ExplicitContentFilterLevel.DISABLED
                ),
            )

            try:
                await self.model.load_config(self.CONFIG_FILE)
                current_config = self.model.mappings
            except Exception:
                current_config = {}

            current_config["target_guild_id"] = str(self.target_guild.id)
            self.model.mappings = current_config

            try:
                await self.model.save_config(self.CONFIG_FILE)
                logger.info(
                    f"Created new guild: {self.target_guild.name} ({self.target_guild.id}) and saved ID to config"
                )

                await self.target_guild.create_role(
                    name="Admin",
                    permissions=interactions.Permissions.ADMINISTRATOR,
                    color=0xFF0000,
                    hoist=True,
                    mentionable=True,
                )

                system_channel = await self.target_guild.fetch_channel(
                    self.target_guild.system_channel_id
                )
                invite = await system_channel.create_invite()
                user = await self.bot.fetch_user(self.admin_user_id)
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

    # Clone Setting

    async def fetch_settings_data(self):
        try:
            if not self.source_guild:
                raise ValueError("Source guild is not set")
            channels = await self.source_guild.fetch_channels()
            self.mappings["fetched_data"]["channels"] = channels
            return True
        except Exception as e:
            logger.error(f"Error fetching settings data: {e}", exc_info=True)
            return False

    @module_base.subcommand(
        sub_cmd_name="settings", sub_cmd_description="Clone server settings"
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def clone_settings_cmd(self, ctx: interactions.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            if not await self.fetch_settings_data():
                await ctx.send(
                    "Failed to fetch source guild data. Please check bot permissions.",
                    ephemeral=True,
                )
                return

            success = await self.clone_settings()
            if success:
                await ctx.send("Successfully cloned server settings!", ephemeral=True)
            else:
                await ctx.send("Failed to clone server settings.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error cloning settings: {e}", exc_info=True)
            await ctx.send("An error occurred while cloning settings.", ephemeral=True)

    def get_channel_from_mapping(self, channel) -> interactions.GuildText | None:
        return self.mappings["channels"].get(channel.id) if channel else None

    async def clone_settings(self) -> bool:
        if not self.source_guild or not self.target_guild:
            logger.error("Guild or new guild is not initialized")
            return False

        try:
            channels = {
                "afk": (
                    self.get_channel_from_mapping(self.source_guild.afk_channel_id)
                    if hasattr(self.source_guild, "afk_channel_id")
                    else None
                ),
                "system": (
                    self.get_channel_from_mapping(self.source_guild.system_channel)
                    if hasattr(self.source_guild, "system_channel")
                    else None
                ),
                "public_updates": (
                    self.get_channel_from_mapping(
                        self.source_guild.public_updates_channel
                    )
                    if hasattr(self.source_guild, "public_updates_channel")
                    else None
                ),
                "rules": (
                    self.get_channel_from_mapping(self.source_guild.rules_channel)
                    if hasattr(self.source_guild, "rules_channel")
                    else None
                ),
                "safety_alerts": (
                    self.get_channel_from_mapping(
                        self.source_guild.safety_alerts_channel
                    )
                    if hasattr(self.source_guild, "safety_alerts_channel")
                    else None
                ),
            }

            try:
                await self.target_guild.edit(
                    features=["COMMUNITY"],
                    name=(
                        self.source_guild.name
                        if hasattr(self.source_guild, "name")
                        else None
                    ),
                    description=(
                        self.source_guild.description
                        if hasattr(self.source_guild, "description")
                        else None
                    ),
                    verification_level=(
                        self.source_guild.verification_level
                        if hasattr(self.source_guild, "verification_level")
                        else None
                    ),
                    default_message_notifications=(
                        self.source_guild.default_message_notifications
                        if hasattr(self.source_guild, "default_message_notifications")
                        else None
                    ),
                    explicit_content_filter=(
                        self.source_guild.explicit_content_filter
                        if hasattr(self.source_guild, "explicit_content_filter")
                        else None
                    ),
                    afk_channel=channels["afk"],
                    afk_timeout=(
                        self.source_guild.afk_timeout
                        if hasattr(self.source_guild, "afk_timeout")
                        else None
                    ),
                    system_channel=channels["system"],
                    system_channel_flags=(
                        self.source_guild.system_channel_flags
                        if hasattr(self.source_guild, "system_channel_flags")
                        else None
                    ),
                    rules_channel=channels["rules"],
                    public_updates_channel=channels["public_updates"],
                    safety_alerts_channel=channels["safety_alerts"],
                    preferred_locale=(
                        self.source_guild.preferred_locale
                        if hasattr(self.source_guild, "preferred_locale")
                        else None
                    ),
                    premium_progress_bar_enabled=(
                        self.source_guild.premium_progress_bar_enabled
                        if hasattr(self.source_guild, "premium_progress_bar_enabled")
                        else False
                    ),
                )
                logger.debug(
                    f"Updated settings for guild: {self.target_guild.name} ({self.target_guild.id})"
                )
                await asyncio.sleep(self.process_delay)
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

    # Clone Icon

    @module_base.subcommand(
        sub_cmd_name="icon", sub_cmd_description="Clone server icon"
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def clone_icon_cmd(self, ctx: interactions.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            await self.clone_icon()
            await ctx.send("Successfully cloned server icon!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error cloning icon: {e}", exc_info=True)
            await ctx.send("An error occurred while cloning icon.", ephemeral=True)

    async def clone_icon(self) -> None:
        if (
            self.source_guild is not None
            and self.source_guild.icon is not None
            and self.target_guild is not None
        ):
            await self.target_guild.edit(
                icon=BytesIO(await self.source_guild.icon.fetch())
            )
        await asyncio.sleep(self.process_delay)

    # Clone Banner

    @module_base.subcommand(
        sub_cmd_name="banner", sub_cmd_description="Clone server banner"
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def clone_banner_cmd(self, ctx: interactions.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            await self.clone_banner()
            await ctx.send("Successfully cloned server banner!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error cloning banner: {e}", exc_info=True)
            await ctx.send("An error occurred while cloning banner.", ephemeral=True)

    async def clone_banner(self) -> None:
        if (
            self.source_guild is not None
            and self.source_guild.banner is not None
            and self.target_guild is not None
        ):
            await self.target_guild.edit(
                banner=BytesIO(await self.source_guild.splash.fetch())
            )
            await asyncio.sleep(self.process_delay)

    # Clone Roles

    @module_base.subcommand(
        sub_cmd_name="roles", sub_cmd_description="Clone server roles"
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def clone_roles_cmd(self, ctx: interactions.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            if not await self.fetch_roles_data():
                await ctx.send(
                    "Failed to fetch source guild data. Please check bot permissions.",
                    ephemeral=True,
                )
                return

            await self.clone_roles()
            await ctx.send("Successfully cloned server roles!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error cloning roles: {e}", exc_info=True)
            await ctx.send("An error occurred while cloning roles.", ephemeral=True)

    async def fetch_roles_data(self):
        try:
            if not self.source_guild:
                raise ValueError("Source guild is not set")
            roles = []
            for role_id in self.source_guild._role_ids:
                role = await self.source_guild.fetch_role(role_id)
                if role:
                    roles.append(role)
            self.mappings["fetched_data"]["roles"] = roles
            return True
        except Exception as e:
            logger.error(f"Error fetching roles data: {e}", exc_info=True)
            return False

    async def clone_roles(self):
        try:
            roles_create = [role for role in self.mappings["fetched_data"]["roles"]]
            logger.info(f"Starting to clone {len(roles_create)} roles")

            self.mappings["roles"].update(
                {
                    role.name: await self.target_guild.fetch_role(role.id)
                    for role in roles_create
                    if role.id == self.target_guild.default_role.id
                }
            )

            for role in reversed(roles_create):
                if role.id == self.target_guild.default_role.id:
                    await (await self.target_guild.fetch_role(role.id)).edit(
                        name=role.name,
                        color=role.color,
                        hoist=role.hoist,
                        mentionable=role.mentionable,
                        permissions=role.permissions,
                        unicode_emoji=role._unicode_emoji,
                    )
                    await asyncio.sleep(self.process_delay)
                    continue

                try:
                    role_icon = None
                    if role._icon:
                        role_icon = BytesIO(await role._icon.fetch())

                    self.mappings["roles"][role.name] = new_role = (
                        await self.target_guild.create_role(
                            name=role.name,
                            color=role.color,
                            hoist=role.hoist,
                            mentionable=role.mentionable,
                            permissions=role.permissions,
                            icon=role_icon,
                        )
                    )

                    if role._unicode_emoji:
                        await new_role.edit(unicode_emoji=role._unicode_emoji)
                        await asyncio.sleep(self.process_delay)

                    await self.create_object_log(
                        object_type="role",
                        object_name=new_role.name,
                        object_id=new_role.id,
                    )
                except Forbidden as e:
                    if "This server needs more boosts" in str(e):
                        self.mappings["roles"][role.name] = new_role = (
                            await self.target_guild.create_role(
                                name=role.name,
                                color=role.color,
                                hoist=role.hoist,
                                mentionable=role.mentionable,
                                permissions=role.permissions,
                            )
                        )
                        await self.create_object_log(
                            object_type="role",
                            object_name=new_role.name,
                            object_id=new_role.id,
                        )
                    else:
                        raise

                await asyncio.sleep(self.process_delay)

            logger.info("Successfully cloned all roles")
        except Exception as e:
            logger.error(f"Failed to clone roles: {e}", exc_info=True)
            raise

    # Clone Categories

    async def fetch_channels_data(self):
        try:
            if not self.source_guild:
                raise ValueError("Source guild is not set")
            channels = await self.source_guild.fetch_channels()
            self.mappings["fetched_data"]["channels"] = channels
            return True
        except Exception as e:
            logger.error(f"Error fetching channels data: {e}", exc_info=True)
            return False

    @module_base.subcommand(
        sub_cmd_name="categories", sub_cmd_description="Clone channel categories"
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def clone_categories_cmd(self, ctx: interactions.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            if not await self.fetch_channels_data():
                await ctx.send(
                    "Failed to fetch source guild data. Please check bot permissions.",
                    ephemeral=True,
                )
                return

            await self.clone_categories()
            await ctx.send("Successfully cloned channel categories!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error cloning categories: {e}", exc_info=True)
            await ctx.send(
                "An error occurred while cloning categories.", ephemeral=True
            )

    async def clone_categories(self, perms: bool = True) -> None:
        if not self.target_guild:
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
                    role_name = overwrite.id.name
                    if role_name in self.mappings["roles"]:
                        perm = interactions.PermissionOverwrite.for_target(overwrite.id)
                        if overwrite.allow:
                            perm.add_allows(overwrite.allow)
                        if overwrite.deny:
                            perm.add_denies(overwrite.deny)
                        overwrites[self.mappings["roles"][role_name]] = perm

            self.mappings["categories"][category.name] = new_category = (
                await self.target_guild.create_category(
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

    # Clone Community Channels

    @module_base.subcommand(
        sub_cmd_name="c-channels", sub_cmd_description="Clone community channels"
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def clone_comm_channels_cmd(self, ctx: interactions.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            if not await self.fetch_channels_data():
                await ctx.send(
                    "Failed to fetch source guild data. Please check bot permissions.",
                    ephemeral=True,
                )
                return

            await self.clone_comm_channels()
            await ctx.send("Successfully cloned forum channels!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error cloning forums: {e}", exc_info=True)
            await ctx.send(
                "An error occurred while cloning forum channels.", ephemeral=True
            )

    async def clone_comm_channels(self, perms: bool = True) -> None:
        if not self.target_guild:
            logger.warning("New guild is not set")
            return

        channels = [
            c
            for c in self.mappings["fetched_data"]["channels"]
            if isinstance(
                c,
                (
                    interactions.GuildForum,
                    interactions.GuildStageVoice,
                ),
            )
        ]

        for channel in channels:
            try:
                if self.source_guild:
                    channel = await self.source_guild.fetch_channel(channel.id)
                else:
                    logger.warning("Guild is not set")
                    continue
            except Forbidden:
                logger.debug(f"Can't fetch channel {channel.name} | {channel.id}")
                continue

            category = None
            position = 0
            if channel.parent_id:
                category = self.mappings["categories"].get(channel.parent_id)
                if category:
                    category_channels = [
                        c
                        for c in self.target_guild.channels
                        if c.parent_id == category.id
                    ]
                    position = len(category_channels)

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
                "position": position,
                "category": category,
                "permission_overwrites": overwrites,
            }

            if isinstance(channel, interactions.GuildForum):
                tags = []
                for tag in channel.available_tags:
                    emoji_data = None
                    if tag.emoji_id:
                        emoji_name = tag.emoji_name
                        target_emoji = next(
                            (
                                emoji
                                for emoji in await self.target_guild.fetch_all_custom_emojis()
                                if emoji.name == emoji_name
                            ),
                            None,
                        )
                        if target_emoji:
                            emoji_data = {
                                "id": target_emoji.id,
                                "name": tag.emoji_name,
                            }
                    elif tag.emoji_name and not tag.emoji_id:
                        emoji_data = {"name": tag.emoji_name}

                    new_tag = interactions.ThreadTag.create(
                        name=tag.name, moderated=tag.moderated, emoji=emoji_data
                    )
                    tags.append(new_tag)

                new_channel = await self.target_guild.create_forum_channel(
                    **channel_args,
                    nsfw=channel.nsfw,
                    layout=channel.default_forum_layout,
                    rate_limit_per_user=channel.rate_limit_per_user,
                    sort_order=channel.default_sort_order,
                    available_tags=tags,
                )
                self.mappings["channels"][channel.name] = new_channel

                await self.create_channel_log(
                    channel_type="forum",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )

            elif isinstance(channel, interactions.GuildStageVoice):
                self.mappings["channels"][channel.id] = new_channel = (
                    await self.target_guild.create_stage_channel(
                        **channel_args,
                        bitrate=min(channel.bitrate, self.target_guild.bitrate_limit),
                        user_limit=channel.user_limit,
                    )
                )
                await self.create_channel_log(
                    channel_type="stage",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )

            await asyncio.sleep(self.process_delay)

    # Clone Non-Community Channels

    @module_base.subcommand(
        sub_cmd_name="nc-channels", sub_cmd_description="Clone non-community channels"
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def clone_non_comm_channels_cmd(self, ctx: interactions.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            if not await self.fetch_channels_data():
                await ctx.send(
                    "Failed to fetch source guild data. Please check bot permissions.",
                    ephemeral=True,
                )
                return

            await self.clone_non_comm_channels()
            await ctx.send("Successfully cloned channels!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error cloning channels: {e}", exc_info=True)
            await ctx.send("An error occurred while cloning channels.", ephemeral=True)

    async def clone_non_comm_channels(self, perms: bool = True) -> None:
        if not self.target_guild:
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
                    interactions.GuildNews,
                ),
            )
        ]

        for channel in channels:
            try:
                if self.source_guild:
                    channel = await self.source_guild.fetch_channel(channel.id)
                else:
                    logger.warning("Guild is not set")
                    continue
            except Forbidden:
                logger.debug(f"Can't fetch channel {channel.name} | {channel.id}")
                continue

            category = None
            position = 0
            if channel.parent_id:
                try:
                    parent_channel = await self.source_guild.fetch_channel(
                        channel.parent_id
                    )
                    parent_name = parent_channel.name if parent_channel else None
                    category = next(
                        (
                            cat
                            for cat in self.target_guild.channels
                            if isinstance(cat, interactions.GuildCategory)
                            and cat.name == parent_name
                        ),
                        None,
                    )
                    if category:
                        category_channels = [
                            c
                            for c in self.target_guild.channels
                            if c.parent_id == category.id
                        ]
                        position = len(category_channels)
                except Exception as e:
                    logger.warning(
                        f"Could not fetch parent category for channel {channel.name}: {e}"
                    )
                    continue

            overwrites = {}
            if perms and channel.permission_overwrites:
                for overwrite in channel.permission_overwrites:
                    if isinstance(overwrite.id, interactions.Role):
                        role_name = overwrite.id.name
                        target_role = next(
                            (
                                role
                                for role in self.target_guild.roles
                                if role.name == role_name
                            ),
                            None,
                        )
                        if target_role:
                            perm = interactions.PermissionOverwrite.for_target(
                                target_role
                            )
                            if overwrite.allow:
                                perm.add_allows(overwrite.allow)
                            if overwrite.deny:
                                perm.add_denies(overwrite.deny)
                            overwrites[target_role] = perm

            channel_args = {
                "name": channel.name,
                "position": position,
                "category": category,
                "permission_overwrites": overwrites,
            }

            if isinstance(channel, interactions.GuildText):
                new_channel = await self.target_guild.create_text_channel(
                    **channel_args,
                    topic=channel.topic,
                    rate_limit_per_user=channel.rate_limit_per_user,
                    nsfw=channel.nsfw,
                )
                self.mappings["channels"][channel.name] = new_channel
                await self.create_channel_log(
                    channel_type="text",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )

            elif isinstance(channel, interactions.GuildVoice):
                bitrate = (
                    min(channel.bitrate, self.target_guild.bitrate_limit)
                    if self.target_guild.bitrate_limit
                    else channel.bitrate
                )

                new_channel = await self.target_guild.create_voice_channel(
                    **channel_args,
                    bitrate=bitrate,
                    user_limit=channel.user_limit,
                )
                self.mappings["channels"][channel.name] = new_channel
                await self.create_channel_log(
                    channel_type="voice",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )

            elif isinstance(channel, interactions.GuildNews):
                new_channel = await self.target_guild.create_news_channel(
                    **channel_args,
                    topic=channel.topic,
                    nsfw=channel.nsfw,
                )
                self.mappings["channels"][channel.name] = new_channel
                await self.create_channel_log(
                    channel_type="news",
                    channel_name=new_channel.name,
                    channel_id=new_channel.id,
                )

            await asyncio.sleep(self.process_delay)

    # Clone Emojis

    async def fetch_emojis_data(self):
        try:
            if not self.source_guild:
                raise ValueError("Source guild is not set")
            emojis = await self.source_guild.fetch_all_custom_emojis()
            self.mappings["fetched_data"]["emojis"] = emojis
            return True
        except Exception as e:
            logger.error(f"Error fetching emojis data: {e}", exc_info=True)
            return False

    @module_base.subcommand(
        sub_cmd_name="emojis", sub_cmd_description="Clone server emojis"
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def clone_emojis_cmd(self, ctx: interactions.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            if not await self.fetch_emojis_data():
                await ctx.send(
                    "Failed to fetch source guild data. Please check bot permissions.",
                    ephemeral=True,
                )
                return

            await self.clone_emojis()
            await ctx.send("Successfully cloned server emojis!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error cloning emojis: {e}", exc_info=True)
            await ctx.send("An error occurred while cloning emojis.", ephemeral=True)

    async def clone_emojis(self) -> None:
        if not self.target_guild:
            return

        emoji_limit = min(
            self.target_guild.emoji_limit - 5,
            len(await self.target_guild.fetch_all_custom_emojis()),
        )
        emoji_data = [
            (emoji.name, emoji.roles, await emoji.read())
            for emoji in self.mappings["fetched_data"]["emojis"][:emoji_limit]
            if len(await self.target_guild.fetch_all_custom_emojis()) < emoji_limit
        ]

        for name, roles, imagefile in emoji_data:
            new_emoji = await self.target_guild.create_custom_emoji(
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

    # Clone Stickers

    async def fetch_stickers_data(self):
        try:
            if not self.source_guild:
                raise ValueError("Source guild is not set")
            stickers = await self.source_guild.fetch_all_custom_stickers()
            self.mappings["fetched_data"]["stickers"] = stickers
            return True
        except Exception as e:
            logger.error(f"Error fetching stickers data: {e}", exc_info=True)
            return False

    @module_base.subcommand(
        sub_cmd_name="stickers", sub_cmd_description="Clone server stickers"
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def clone_stickers_cmd(self, ctx: interactions.SlashContext):
        await ctx.defer(ephemeral=True)
        try:
            if not await self.fetch_stickers_data():
                await ctx.send(
                    "Failed to fetch source guild data. Please check bot permissions.",
                    ephemeral=True,
                )
                return

            await self.clone_stickers()
            await ctx.send("Successfully cloned server stickers!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error cloning stickers: {e}", exc_info=True)
            await ctx.send("An error occurred while cloning stickers.", ephemeral=True)

    async def clone_stickers(self) -> None:
        if not self.target_guild:
            return

        sticker_limit, created = self.target_guild.sticker_limit, 0
        sticker_data = [
            (s.name, s.description, await s.to_file(), s.tags, s.id, s.url)
            for s in self.mappings["fetched_data"]["stickers"][:sticker_limit]
        ]

        for name, description, file, tags, sticker_id, sticker_url in sticker_data:
            if created >= sticker_limit:
                break

            try:
                new_sticker = await self.target_guild.create_custom_sticker(
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

    # Migrate Command

    @module_base.subcommand(
        sub_cmd_name="migrate", sub_cmd_description="Migrate channel across servers"
    )
    @interactions.slash_option(
        name="origin",
        description="The origin channel to migrate from",
        opt_type=interactions.OptionType.STRING,
        required=True,
        argument_name="origin_channel",
    )
    @interactions.slash_option(
        name="server",
        description="The destination server ID to migrate to",
        opt_type=interactions.OptionType.STRING,
        required=True,
        argument_name="destination_server",
    )
    @interactions.slash_option(
        name="channel",
        description="The destination channel ID to migrate to",
        opt_type=interactions.OptionType.STRING,
        required=True,
        argument_name="destination_channel",
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def migrate(
        self,
        ctx: interactions.SlashContext,
        origin_channel: str,
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

            try:
                origin = await ctx.guild.fetch_channel(origin_channel)
            except Exception as e:
                logger.error(f"Error fetching channels: {e}", exc_info=True)
                await ctx.send("Error fetching channel information", ephemeral=True)
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
                f"Migrating {origin.name} to {destination.name} in server {destination_guild.name}...",
                ephemeral=True,
            )

            if origin_type in {interactions.GuildText, interactions.GuildForum}:
                await migrate_channel(
                    origin,
                    destination,
                    ctx.bot,
                )
            else:
                await migrate_thread(
                    origin,
                    destination,
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

    # Configure Command

    @module_base.subcommand(
        sub_cmd_name="config", sub_cmd_description="Configure clone settings"
    )
    @interactions.slash_option(
        name="webhook",
        description="Set webhook delay (in seconds)",
        opt_type=interactions.OptionType.NUMBER,
        argument_name="webhook_delay",
    )
    @interactions.slash_option(
        name="process",
        description="Set process delay (in seconds)",
        opt_type=interactions.OptionType.NUMBER,
        argument_name="process_delay",
    )
    @interactions.slash_option(
        name="admin_user_id",
        description="Set admin user ID",
        opt_type=interactions.OptionType.STRING,
    )
    @interactions.slash_option(
        name="source_guild_id",
        description="Set source guild ID",
        opt_type=interactions.OptionType.STRING,
    )
    @interactions.slash_option(
        name="target_guild_id",
        description="Set target guild ID",
        opt_type=interactions.OptionType.STRING,
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def config(
        self,
        ctx: interactions.SlashContext,
        webhook_delay: Optional[float] = None,
        process_delay: Optional[float] = None,
        admin_user_id: Optional[str] = None,
        source_guild_id: Optional[str] = None,
        target_guild_id: Optional[str] = None,
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

        if webhook_delay is not None:
            current_config["webhook_delay"] = webhook_delay
            self.webhook_delay = webhook_delay

        if process_delay is not None:
            current_config["process_delay"] = process_delay
            self.process_delay = process_delay

        if admin_user_id is not None:
            current_config["admin_user_id"] = admin_user_id
            self.admin_user_id = int(admin_user_id)

        if source_guild_id is not None:
            current_config["source_guild_id"] = source_guild_id
            self.source_guild_id = int(source_guild_id)
            self.source_guild = await self.bot.fetch_guild(self.source_guild_id)

        if target_guild_id is not None:
            current_config["target_guild_id"] = target_guild_id
            self.target_guild = await self.bot.fetch_guild(int(target_guild_id))

        current_config.update(
            {
                "webhook_delay": self.webhook_delay,
                "process_delay": self.process_delay,
                "target_guild_id": (
                    str(self.target_guild.id) if self.target_guild else None
                ),
                "source_guild_id": (
                    str(self.source_guild_id) if self.source_guild_id else None
                ),
                "admin_user_id": (
                    str(self.admin_user_id) if self.admin_user_id else None
                ),
            }
        )

        self.model.mappings = current_config
        try:
            await self.model.save_config(self.CONFIG_FILE)

            settings = [
                f"Webhook Delay: {current_config.get('webhook_delay', 0.2)}s",
                f"Process Delay: {current_config.get('process_delay', 0.2)}s",
                f"Source Guild ID: {current_config.get('source_guild_id', None)}",
                f"Target Guild ID: {current_config.get('target_guild_id', None)}",
                f"Admin User ID: {current_config.get('admin_user_id', None)}",
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

    # Export Command

    @module_base.subcommand(
        sub_cmd_name="export",
        sub_cmd_description="Export files from the extension directory",
    )
    @interactions.slash_option(
        name="type",
        description="Type of files to export",
        required=True,
        opt_type=interactions.OptionType.STRING,
        autocomplete=True,
        argument_name="file_type",
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def debug_export(
        self, ctx: interactions.SlashContext, file_type: str
    ) -> None:
        await ctx.defer(ephemeral=True)
        filename: str = ""

        if not os.path.exists(BASE_DIR):
            await ctx.send("Extension directory does not exist.")
            return None

        if file_type != "all" and not os.path.isfile(os.path.join(BASE_DIR, file_type)):
            await ctx.send(
                f"File `{file_type}` does not exist in the extension directory."
            )
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
                await ctx.send("Failed to create archive file.")
                return None

            file_size = os.path.getsize(filename)
            if file_size > 8_388_608:
                await ctx.send("Archive file is too large to send (>8MB).")
                return None

            await ctx.send(
                (
                    "All extension files attached."
                    if file_type == "all"
                    else f"File `{file_type}` attached."
                ),
                files=[interactions.File(filename)],
            )

        except PermissionError:
            logger.error(f"Permission denied while exporting {file_type}")
            await ctx.send("Permission denied while accessing files.")
        except Exception as e:
            logger.error(f"Error exporting {file_type}: {e}", exc_info=True)
            await ctx.send(f"An error occurred while exporting {file_type}: {str(e)}")
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

    # Invite Command

    @staticmethod
    def validate_duration(duration: int) -> bool:
        return 1 <= duration <= 168

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
    )
    @interactions.check(interactions.has_id(1268909926458064991))
    async def generate_invite(
        self, ctx: interactions.SlashContext, server: str, duration: int = 24
    ) -> None:
        if not server.isdigit():
            await ctx.send("Invalid server ID format", ephemeral=True)
            return

        if not self.validate_duration(duration):
            await ctx.send("Duration must be between 1 and 168 hours", ephemeral=True)
            return

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

        await ctx.send(choices[:25])

    # Delete Command

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
    @interactions.check(interactions.has_id(1268909926458064991))
    async def delete_server(self, ctx: interactions.SlashContext, server: str) -> None:
        await ctx.defer(ephemeral=True)

        try:
            target_guild = await self.bot.fetch_guild(server)

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

        await ctx.send(choices[:25])
