import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional, Union, cast

import interactions

BASE_DIR: str = os.path.abspath(os.path.dirname(__file__))
LOG_FILE: str = os.path.join(BASE_DIR, "replica-lib.log")


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


webhook_name: str = "Dyad Webhook"
webhook_avatar: interactions.Absent[interactions.UPLOADABLE_TYPE] = interactions.MISSING


ChannelMapping = Dict[int, Any]
RoleMapping = Dict[int, Any]
Mappings = Dict[str, Union[ChannelMapping, RoleMapping]]
MESSAGE_LEN_LIMIT: int = 2000


async def flatten_history_iterator(
    history: interactions.ChannelHistory, reverse: bool = False
) -> list[interactions.Message]:
    ret_list: list[interactions.Message] = []
    error_fmt: str = "Channel {0.channel.name} ({0.channel.id})"
    error_map: dict[int, tuple[str, str]] = {
        50083: ("error", "archived thread"),
        10003: ("error", "unknown channel"),
        50001: ("error", "no access"),
        50013: ("error", "lacks permission"),
        10008: ("warning", "Unknown message in"),
        50021: ("warning", "System message in"),
        160005: (
            "warning",
            "Channel {0.channel.name} ({0.channel.id}) is a locked thread",
        ),
    }
    try:
        async for msg in history:
            ret_list.append(msg)
    except interactions.errors.HTTPException as e:
        try:
            if e.code in error_map:
                level, msg = error_map[e.code]
                getattr(logger, level)(
                    f"{msg} {error_fmt.format(history)}"
                    if msg != error_map[160005][1]
                    else msg.format(history)
                )
            else:
                logger.warning(f"{error_fmt.format(history)} has unknown code {e.code}")
        except ValueError:
            logger.warning(
                f"Unknown HTTP exception {e.code} {e.errors} {e.route} {e.response} {e.text}",
                stack_info=True,
            )
    except Exception as e:
        logger.warning(
            f"Unknown exception {e.__class__.__name__}: {str(e)}", exc_info=True
        )
    return ret_list[::-1] if reverse else ret_list


async def fetch_create_webhook(
    dest_chan: interactions.WebhookMixin,
) -> interactions.Webhook:
    webhooks: list[interactions.Webhook] = await dest_chan.fetch_webhooks()
    webhook: interactions.Webhook = next(
        (wh for wh in webhooks if wh.name == webhook_name), None
    ) or await dest_chan.create_webhook(name=webhook_name, avatar=webhook_avatar)
    return webhook


def convert_poll_to_message(poll: interactions.Poll) -> str:
    def poll_media_to_str(poll_media: interactions.PollMedia) -> str:
        if isinstance(poll_media, interactions.PollMedia):
            poll_media = poll_media.to_dict()
        emoji_data = poll_media.get("emoji")
        text = poll_media.get("text", "")
        if not emoji_data:
            return text
        emoji_str = (
            f"<:{emoji_data['name']}:{emoji_data['id']}>"
            if "id" in emoji_data
            else emoji_data["name"]
        )
        return f"{emoji_str} {text}".rstrip()

    question = poll_media_to_str(poll.question)
    answers = [poll_media_to_str(pa.poll_media) for pa in poll.answers]

    if poll.results:
        results = [ac.count for ac in poll.results.answer_counts]
        answers = list(map(lambda x: f"{x[0]:04d} - {x[1]}", zip(results, answers)))
        return f"(Poll finished) {question}\n{chr(10).join(answers)}"

    return f"{question}\n{chr(10).join(answers)}"


async def migrate_message(
    orig_msg: interactions.Message,
    dest_chan: interactions.GuildChannel,
    thread_id: Optional[int] = None,
    mappings: Optional[Mappings] = None,
    orig_guild_id: Optional[int] = None,
    new_guild_id: Optional[int] = None,
) -> tuple[bool, Optional[int], Optional[interactions.Message]]:

    if not isinstance(dest_chan, (interactions.GuildText, interactions.GuildForum)):
        return False, None, None

    webhook: interactions.Webhook = await fetch_create_webhook(dest_chan=dest_chan)

    msg_text: str = orig_msg.content
    msg_author: interactions.User = orig_msg.author

    thread: interactions.Snowflake_Type = (
        thread_id if thread_id and thread_id != 0 else None
    )
    thread_name: Optional[str] = orig_msg.channel.name if thread_id == 0 else None
    output_thread_id: Optional[int] = None

    if (reply_to := orig_msg.get_referenced_message()) and reply_to.type in {
        interactions.MessageType.DEFAULT,
        interactions.MessageType.REPLY,
        interactions.MessageType.THREAD_STARTER_MESSAGE,
    }:
        reply_lines = (
            convert_poll_to_message(reply_to.poll)
            if reply_to.poll
            else reply_to.content
        ).splitlines()
        msg_text = f"> {reply_to.author.display_name} at {reply_to.created_at.strftime('%d/%m/%Y %H:%M')} said:\n{chr(10).join('> ' + line for line in reply_lines)}\n{msg_text}"

    if mappings and orig_guild_id and new_guild_id:
        replacements = {
            **{
                f"<#{old_id}>": f"<#{new.id if new and hasattr(new, 'id') else ''}>"
                for old_id, new in mappings.get("channels", {}).items()
            },
            **{
                f"<@&{old_id}>": f"<@&{new.id if new and hasattr(new, 'id') else ''}>"
                for old_id, new in mappings.get("roles", {}).items()
            },
            **{
                f"https://discord.com/channels/{orig_guild_id}/{old_id}": f"https://discord.com/channels/{new_guild_id}/{new.id if new and hasattr(new, 'id') else old_id}"
                for old_id, new in mappings.get("channels", {}).items()
            },
        }

        for old_text, new_text in replacements.items():
            msg_text = msg_text.replace(old_text, new_text)

    msg_text = (
        f"{chr(10).join(a.url for a in orig_msg.attachments)}\n{msg_text}"
        if orig_msg.attachments
        else msg_text
    )

    if orig_msg.sticker_items:
        available_stickers = [
            s
            for s in await dest_chan.guild.fetch_all_custom_stickers()
            if any(s.id == i.id or s.name == i.name for i in orig_msg.sticker_items)
        ]

        if len(available_stickers) < len(orig_msg.sticker_items):
            missing = (
                i.name
                for i in orig_msg.sticker_items
                if not any(i.id == s.id or i.name == s.name for s in available_stickers)
            )
            msg_text = f"Sticker {','.join(missing)} not available\n{msg_text}"

        msg_text = f"{chr(10).join(s.url for s in available_stickers)}\n{msg_text}"

    msg_text = (
        f"{convert_poll_to_message(orig_msg.poll)}\n{msg_text}"
        if orig_msg.poll
        else msg_text
    )

    sent_msg: Optional[interactions.Message] = None
    for text in (
        msg_text[i : i + MESSAGE_LEN_LIMIT]
        for i in range(0, len(msg_text), MESSAGE_LEN_LIMIT)
    ):
        try:
            sent_msg = await webhook.send(
                content=text,
                embeds=orig_msg.embeds,
                username=f"{msg_author.display_name} at {orig_msg.created_at.strftime('%d/%m/%Y %H:%M')}",
                avatar_url=msg_author.display_avatar.url,
                reply_to=sent_msg.id if sent_msg else None,
                allowed_mentions=interactions.AllowedMentions.none(),
                wait=True,
                thread=thread,
                thread_name=thread_name,
            )
        except interactions.errors.HTTPException as e:
            error_code = int(e.code) if e.code else None
            match error_code:
                case None:
                    reason = "unknown error code"
                case 50083:
                    reason = "This thread is archived"
                case 10003 | 10008 | 50001 | 50013:
                    return True, output_thread_id, sent_msg
                case 50006:
                    reason = "Cannot send an empty message"
                case 50021 | 160005:
                    reason = "This thread is locked"
                case _:
                    reason = f"Unknown error {error_code}"

            sent_msg = await webhook.send(
                content=f"Message {orig_msg.jump_url} {orig_msg.id} cannot be migrated because {reason}",
                username=f"{msg_author.display_name} at {orig_msg.created_at.strftime('%d/%m/%Y %H:%M')}",
                avatar_url=msg_author.display_avatar.url,
                wait=True,
                thread=thread,
                thread_name=thread_name,
            )

        if (
            sent_msg
            and isinstance(sent_msg, interactions.Message)
            and isinstance(sent_msg.channel, interactions.ThreadChannel)
        ):
            output_thread_id = sent_msg.channel.id

    return True, output_thread_id, sent_msg


def is_empty_message(msg: interactions.Message) -> bool:
    return all(
        map(
            lambda x: not bool(x),
            (msg.content, msg.embeds, msg.poll, msg.reactions, msg.sticker_items),
        )
    )


async def migrate_thread(
    orig_thread: interactions.ThreadChannel,
    dest_chan: Union[interactions.GuildText, interactions.GuildForum],
    client: interactions.Client,
    mappings: Optional[Mappings] = None,
    orig_guild_id: Optional[int] = None,
    new_guild_id: Optional[int] = None,
) -> None:
    thread_type_valid = (
        isinstance(orig_thread, interactions.GuildForumPost)
        and isinstance(dest_chan, interactions.GuildForum)
    ) or (
        (
            isinstance(
                orig_thread,
                (interactions.GuildPublicThread, interactions.GuildPrivateThread),
            )
            and not isinstance(orig_thread, interactions.GuildForumPost)
        )
        and isinstance(dest_chan, interactions.GuildText)
    )

    if not thread_type_valid:
        logger.warning(
            f"Thread type mismatch: orig_thread={orig_thread.__class__.__name__}, dest_chan={dest_chan.__class__.__name__}"
        )
        return

    history_list: list[interactions.Message] = await flatten_history_iterator(
        orig_thread.history(0), reverse=True
    )

    parent_msg = (
        getattr(orig_thread, "initial_post", None)
        if isinstance(orig_thread, interactions.GuildForumPost)
        else (
            getattr(orig_thread, "parent_message", None)
            if isinstance(orig_thread, interactions.GuildPublicThread)
            else None
        )
    )

    thread_id: Optional[int] = None

    if parent_msg is not None and parent_msg in history_list:
        if isinstance(orig_thread, interactions.GuildForumPost):
            ok, thread_id, _ = await migrate_message(
                parent_msg, dest_chan, None, mappings, orig_guild_id, new_guild_id
            )

    webhook = await fetch_create_webhook(dest_chan=dest_chan)

    if thread_id is None:
        thread_id = 0

    if parent_msg is None:
        sent_msg = await webhook.send(
            content="This message has been deleted by original author",
            thread=None if isinstance(dest_chan, interactions.GuildForum) else None,
            thread_name=(
                orig_thread.name
                if isinstance(dest_chan, interactions.GuildForum)
                else None
            ),
            wait=True,
        )
        thread_id = (
            sent_msg.channel.id
            if isinstance(dest_chan, interactions.GuildForum)
            else (
                await sent_msg.create_thread(
                    name=orig_thread.name,
                    reason="Message migration",
                )
            ).id
        )

    for i, msg in enumerate(history_list):
        if i == 0 and parent_msg is not None and msg != parent_msg:
            if isinstance(orig_thread, interactions.GuildForumPost):
                ok, thread_id, _ = await migrate_message(
                    parent_msg,
                    dest_chan,
                    thread_id,
                    mappings,
                    orig_guild_id,
                    new_guild_id,
                )
            elif isinstance(orig_thread, interactions.GuildPublicThread):
                _, _, sent_msg = await migrate_message(
                    parent_msg, dest_chan, None, mappings, orig_guild_id, new_guild_id
                )
                thread_id = (
                    await sent_msg.create_thread(
                        name=orig_thread.name, reason="Message migration"
                    )
                ).id

        if not is_empty_message(msg):
            ok, new_thread_id, _ = await migrate_message(
                msg, dest_chan, thread_id, mappings, orig_guild_id, new_guild_id
            )
            if new_thread_id:
                thread_id = new_thread_id

    try:
        if thread_id and isinstance(dest_chan, interactions.GuildText):
            thread = await client.fetch_thread(thread_id)
            if thread:
                if orig_thread.archived:
                    await thread.archive()
                if orig_thread.locked:
                    await thread.lock()
                if orig_thread.auto_archive_duration:
                    await thread.edit(
                        auto_archive_duration=orig_thread.auto_archive_duration
                    )
    except Exception as e:
        logger.warning(f"Failed to update thread metadata: {str(e)}")


async def migrate_channel(
    orig_chan: Union[interactions.GuildText, interactions.GuildForum],
    dest_chan: Union[interactions.GuildText, interactions.GuildForum],
    client: interactions.Client,
    mappings: Optional[Mappings] = None,
    orig_guild_id: Optional[int] = None,
    new_guild_id: Optional[int] = None,
) -> None:
    match (
        isinstance(orig_chan, interactions.GuildForum),
        isinstance(dest_chan, interactions.GuildForum),
    ):
        case (True, True):
            orig_chan = cast(interactions.GuildForum, orig_chan)
            archived_posts_id: list[int] = [
                int(thread["id"])
                for thread in (
                    await client.http.list_public_archived_threads(orig_chan.id)
                )["threads"]
            ][::-1]

            posts = [
                *[
                    await orig_chan.fetch_post(id=post_id)
                    for post_id in archived_posts_id
                ],
                *(await orig_chan.fetch_posts())[::-1],
            ]

            for post in posts:
                await migrate_thread(
                    post, dest_chan, client, mappings, orig_guild_id, new_guild_id
                )

        case (False, False) if isinstance(orig_chan, interactions.GuildText):
            text_chan = cast(interactions.GuildText, orig_chan)
            messages = await flatten_history_iterator(
                text_chan.history(0), reverse=True
            )

            for msg in messages:
                if msg.thread:
                    await migrate_thread(
                        msg.thread,
                        dest_chan,
                        client,
                        mappings,
                        orig_guild_id,
                        new_guild_id,
                    )
                else:
                    await migrate_message(
                        msg, dest_chan, None, mappings, orig_guild_id, new_guild_id
                    )

        case _:
            return
