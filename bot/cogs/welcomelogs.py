"""
cogs/welcomelogs.py — Welcome messages and server logging.
"""

import io
import json
import os
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.checks import perms_or_developer
from bot.services import attachment_cache


DATA_DIR = "data/welcomelogs"
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
os.makedirs(DATA_DIR, exist_ok=True)

#: Discord's per-message limits for log batches.
EMBEDS_PER_LOG_MESSAGE = 5
FILES_PER_LOG_MESSAGE = 10

#: A purge can remove up to 500 messages. Writing each one as its own embed spammed
#: the log channel with dozens of messages, so a whole purge now goes out as a
#: single text file instead.
#: One slot of every log message is taken by the transcript itself.
MAX_PURGE_IMAGES = FILES_PER_LOG_MESSAGE - 1
#: Per-message text kept in the transcript, so one wall of text can't blow the
#: attachment limit on its own.
PURGE_TEXT_CHUNK = 1000

#: How long a purge note stays valid, so a client-side bulk delete happening
#: seconds later is not attributed to the wrong moderator.
PURGE_NOTE_TTL = 30.0

_DEFAULT_CONFIG = {
    "welcome_channel": None,
    "welcome_message": "Welcome {user} to {server}!",
    "welcome_enabled": False,
    "logs_channel": None,
    "logs_enabled": False,
    "log_messages": True,
    "log_joins": True,
    "log_leaves": True,
    "log_roles": True,
    "log_bans": True,
    "log_edits": True,
    "log_purges": True,
    "log_attachments": True,
}

#: (guild_id, channel_id) -> (moderator, expiry) for the purge in flight.
_recent_purges: dict[tuple[int, int], tuple[object, float]] = {}


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_guild_config(guild_id: int) -> dict:
    """Saved config for a guild, filled in from the defaults.

    The saved blob is merged over the defaults rather than only being defaulted
    for unknown guilds, so toggles added later (purges, attachments) start
    working on servers that were configured before they existed.
    """
    config = load_config()
    return {**_DEFAULT_CONFIG, **config.get(str(guild_id), {})}


def note_purge(guild, channel, moderator) -> None:
    """Record who is about to purge *channel* so the log can name them.

    Called by the purge commands just before they delete. Client-side bulk
    deletes never get here, which is how the log tells the two apart.
    """
    now = time.monotonic()
    for key, (_, expiry) in list(_recent_purges.items()):
        if expiry < now:
            del _recent_purges[key]
    _recent_purges[(guild.id, channel.id)] = (moderator, now + PURGE_NOTE_TTL)


def _consume_purge_note(guild_id: int, channel_id: int):
    """Pop the moderator who triggered a purge here, if it was one of ours."""
    entry = _recent_purges.pop((guild_id, channel_id), None)
    if entry is None:
        return None
    moderator, expiry = entry
    return moderator if time.monotonic() <= expiry else None


def save_guild_config(guild_id: int, guild_config: dict) -> None:
    config = load_config()
    config[str(guild_id)] = guild_config
    save_config(config)


class WelcomeLogs(commands.Cog, name="welcomelogs"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _make_embed(self, title: str, color: int, description: str = "") -> discord.Embed:
        embed = discord.Embed(color=color)
        embed.set_author(name=title)
        if description:
            embed.description = description
        return embed

    def _logs_on(self, config: dict) -> bool:
        """True when this guild has somewhere to send logs."""
        return bool(config.get("logs_enabled") and config.get("logs_channel"))

    def _log_channel(self, guild, config: dict):
        return guild.get_channel(config["logs_channel"]) if self._logs_on(config) else None

    def _deleted_message_embed(self, message: discord.Message, title: str = "Message Deleted") -> discord.Embed:
        """Log entry for one removed message, shared by single and bulk deletes."""
        description = message.content[:1024] if message.content else "*No text content*"
        embed = discord.Embed(color=0xE74C3C, description=description, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=title, icon_url=message.author.display_avatar.url)
        embed.add_field(name="Author", value=f"{message.author} ({message.author.id})", inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Message ID", value=message.id, inline=True)
        if message.attachments:
            embed.add_field(name="Attachments", value=f"{len(message.attachments)} file(s)", inline=True)
        return embed

    def _claim_images(self, message: discord.Message, config: dict) -> list[discord.File]:
        """Take a deleted message's cached pictures, or drop them if disabled.

        Returns fresh ``discord.File`` objects; the caller points the embed's
        image at the first one with an ``attachment://`` URL.
        """
        if config.get("log_attachments"):
            return attachment_cache.to_files(attachment_cache.pop_message_images(message.id))
        attachment_cache.discard_message_images(message.id)
        return []

    async def _send_deleted_logs(self, channel, entries: list[tuple[discord.Embed, list[discord.File]]]) -> None:
        """Send log entries in batches that respect Discord's per-message limits."""
        batch_embeds: list[discord.Embed] = []
        batch_files: list[discord.File] = []

        for embed, files in entries:
            too_many_embeds = len(batch_embeds) >= EMBEDS_PER_LOG_MESSAGE
            too_many_files = batch_files and len(batch_files) + len(files) > FILES_PER_LOG_MESSAGE
            if batch_embeds and (too_many_embeds or too_many_files):
                await channel.send(embeds=batch_embeds, files=batch_files or None)
                batch_embeds, batch_files = [], []
            batch_embeds.append(embed)
            batch_files.extend(files)

        if batch_embeds:
            await channel.send(embeds=batch_embeds, files=batch_files or None)

    def _purge_transcript(
        self,
        guild,
        channel_name: str,
        messages: list[discord.Message],
        purged_count: int,
        moderator,
        bot_count: int,
        attached_images: int,
        total_images: int,
        stamp: datetime,
    ) -> str:
        """Render a whole purge as plain text.

        ``messages`` is the non-bot subset that gets logged (possibly empty) and
        ``purged_count`` is everything Discord removed, so the header can show
        both totals.
        """
        header = [
            "SuperAlpha Do - purged message transcript",
            f"Server    : {guild.name} ({guild.id})",
            f"Channel   : #{channel_name}",
            f"Purged by : {moderator if moderator else 'Unknown (client-side bulk delete)'}",
            f"Removed   : {purged_count} message(s)"
            + (f" ({bot_count} bot message(s) not logged)" if bot_count else ""),
            f"Logged    : {len(messages)} message(s)",
            f"Generated : {stamp:%Y-%m-%d %H:%M:%S} UTC",
        ]
        if total_images:
            header.append(
                f"Images    : {attached_images} of {total_images} attached to this message"
            )

        rows = ["", "=" * 72]
        for index, message in enumerate(messages, 1):
            attachments = [a.filename for a in message.attachments]
            rows.append(f"[{index}] {message.created_at:%Y-%m-%d %H:%M:%S} UTC")
            rows.append(f"    Author : {message.author} ({message.author.id})")
            rows.append(f"    ID     : {message.id}")
            if attachments:
                rows.append(f"    Files  : {', '.join(attachments)}")
            content = message.content or "*no text content*"
            if len(content) > PURGE_TEXT_CHUNK:
                content = content[:PURGE_TEXT_CHUNK] + " [...truncated]"
            for line in content.splitlines() or [""]:
                rows.append(f"    | {line}")

        return "\n".join(header + rows) + "\n"

    def cog_unload(self) -> None:
        attachment_cache.clear()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Stash image bytes while the attachments still exist on the CDN.

        Without this the pictures are unrecoverable by the time the message is
        deleted, so the log channel could only ever say "1 file(s)".
        """
        if message.guild is None or not message.attachments or getattr(message.author, "bot", False):
            return

        config = get_guild_config(message.guild.id)
        # Purges are logged under `log_purges`, not `log_messages`, so caching on
        # `log_messages` alone would leave a purge log unable to show the
        # picture it says it has.
        wants_deletions = config.get("log_messages") or config.get("log_purges")
        if not (self._logs_on(config) and wants_deletions and config.get("log_attachments")):
            return

        await attachment_cache.cache_message_images(message)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return

        config = get_guild_config(member.guild.id)

        if not config.get("welcome_enabled") or not config.get("welcome_channel"):
            return

        channel = member.guild.get_channel(config["welcome_channel"])
        if not channel:
            return

        message = config.get("welcome_message", "Welcome {user} to {server}!")
        message = message.replace("{user}", member.mention)
        message = message.replace("{user_name}", member.display_name)
        message = message.replace("{server}", member.guild.name)
        message = message.replace("{member_count}", str(member.guild.member_count))

        try:
            embed = discord.Embed(color=0x2ECC71, description=message)
            embed.set_author(name=f"Welcome {member.display_name}!", icon_url=member.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Member Count", value=f"#{member.guild.member_count}", inline=True)
            await channel.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return

        config = get_guild_config(member.guild.id)

        if not config.get("logs_enabled") or not config.get("logs_channel"):
            return
        if not config.get("log_leaves"):
            return

        channel = member.guild.get_channel(config["logs_channel"])
        if not channel:
            return

        try:
            embed = discord.Embed(color=0xE74C3C, timestamp=datetime.now(timezone.utc))
            embed.set_author(name="Member Left", icon_url=member.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="User", value=f"{member} ({member.id})", inline=False)
            embed.add_field(name="Joined At", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown", inline=True)
            embed.add_field(name="Member Count", value=f"#{member.guild.member_count}", inline=True)
            await channel.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.bot or after.bot:
            return

        config = get_guild_config(before.guild.id)

        if not config.get("logs_enabled") or not config.get("logs_channel"):
            return

        if before.roles != after.roles and config.get("log_roles"):
            channel = before.guild.get_channel(config["logs_channel"])
            if not channel:
                return

            before_roles = set(before.roles)
            after_roles = set(after.roles)

            added_roles = after_roles - before_roles
            removed_roles = before_roles - after_roles

            for role in added_roles:
                try:
                    embed = discord.Embed(color=0x2ECC71, timestamp=datetime.now(timezone.utc))
                    embed.set_author(name="Role Added", icon_url=after.display_avatar.url)
                    embed.add_field(name="User", value=str(after), inline=False)
                    embed.add_field(name="Role", value=role.mention, inline=True)
                    await channel.send(embed=embed)
                except Exception:
                    pass

            for role in removed_roles:
                try:
                    embed = discord.Embed(color=0xE74C3C, timestamp=datetime.now(timezone.utc))
                    embed.set_author(name="Role Removed", icon_url=after.display_avatar.url)
                    embed.add_field(name="User", value=str(after), inline=False)
                    embed.add_field(name="Role", value=role.mention, inline=True)
                    await channel.send(embed=embed)
                except Exception:
                    pass

        if before.nick != after.nick:
            channel = before.guild.get_channel(config["logs_channel"])
            if not channel:
                return
            try:
                embed = discord.Embed(color=0x3498DB, timestamp=datetime.now(timezone.utc))
                embed.set_author(name="Nickname Changed", icon_url=after.display_avatar.url)
                embed.add_field(name="User", value=str(after), inline=False)
                embed.add_field(name="Before", value=before.nick or before.display_name, inline=True)
                embed.add_field(name="After", value=after.nick or after.display_name, inline=True)
                await channel.send(embed=embed)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return

        config = get_guild_config(message.guild.id)

        if not self._logs_on(config):
            return
        if not config.get("log_messages"):
            return

        channel = self._log_channel(message.guild, config)
        if not channel:
            return

        try:
            embed = self._deleted_message_embed(message)
            files = self._claim_images(message, config)
            if files:
                embed.set_image(url=f"attachment://{files[0].filename}")
            await channel.send(embed=embed, files=files or None)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        """Log a whole purge (or client-side bulk delete) to the log channel.

        Discord fires this instead of one ``on_message_delete`` per message, so
        without it every purge — including moderators clearing messages from the
        Discord app itself — left no trace at all.
        """
        if not messages:
            return

        guild = messages[0].guild
        if guild is None:
            return

        config = get_guild_config(guild.id)
        if not self._logs_on(config) or not config.get("log_purges"):
            return

        channel = self._log_channel(guild, config)
        if not channel:
            return

        purged = sorted(messages, key=lambda message: message.created_at)
        moderator = _consume_purge_note(guild.id, purged[0].channel.id)
        logged = [m for m in purged if not getattr(m.author, "bot", False)]
        bot_count = len(purged) - len(logged)

        stamp = datetime.now(timezone.utc)
        filename = f"purge-{stamp:%Y%m%d-%H%M%S}.txt"

        summary = discord.Embed(
            color=0x95A5A6,
            timestamp=stamp,
            description=(
                f"{len(purged)} message(s) removed from {purged[0].channel.mention}.\n"
                f"Full contents attached as `{filename}`."
            ),
        )
        summary.set_author(name="Messages Purged")
        summary.add_field(name="Channel", value=purged[0].channel.mention, inline=True)
        summary.add_field(
            name="Purged by",
            value=str(moderator) if moderator else "Unknown (client-side bulk delete)",
            inline=True,
        )
        if bot_count:
            summary.add_field(name="Bot messages", value=f"{bot_count} skipped", inline=True)

        image_files: list[discord.File] = []
        image_slots = MAX_PURGE_IMAGES
        total_images = 0
        for message in logged:
            total_images += len(message.attachments)
            if image_slots > 0:
                claimed = self._claim_images(message, config)
                # One message can carry more pictures than slots are left, and
                # going over the file limit fails the whole send.
                image_files.extend(claimed[:image_slots])
                image_slots -= min(len(claimed), image_slots)
            else:
                # No room left in this message: release the bytes rather than
                # leaving them to sit in the cache until eviction.
                attachment_cache.discard_message_images(message.id)

        if total_images > len(image_files):
            summary.add_field(
                name="Note",
                value=(
                    f"{total_images - len(image_files)} image(s) not attached — Discord allows "
                    f"{MAX_PURGE_IMAGES} per log message. Their filenames are in the transcript."
                ),
                inline=False,
            )

        text = self._purge_transcript(
            guild, purged[0].channel.name, logged, len(purged), moderator, bot_count,
            len(image_files), total_images, stamp,
        )
        files: list[discord.File] = [discord.File(io.BytesIO(text.encode("utf-8")), filename=filename)]
        files.extend(image_files)

        try:
            await channel.send(embed=summary, files=files)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        # An edit can be the first time a picture appears on a message, so cache
        # before the "nothing changed" bail-out below.
        if after.attachments and after.guild and not getattr(after.author, "bot", False):
            config = get_guild_config(after.guild.id)
            wants_deletions = config.get("log_messages") or config.get("log_purges")
            if self._logs_on(config) and wants_deletions and config.get("log_attachments"):
                await attachment_cache.cache_message_images(after)

        if before.author.bot:
            return
        if not before.guild:
            return
        if before.content == after.content:
            return

        config = get_guild_config(before.guild.id)

        if not config.get("logs_enabled") or not config.get("logs_channel"):
            return
        if not config.get("log_edits"):
            return

        channel = before.guild.get_channel(config["logs_channel"])
        if not channel:
            return

        try:
            embed = discord.Embed(color=0xF39C12, timestamp=datetime.now(timezone.utc))
            embed.set_author(name="Message Edited", icon_url=before.author.display_avatar.url)
            embed.add_field(name="Author", value=f"{before.author} ({before.author.id})", inline=False)
            embed.add_field(name="Channel", value=before.channel.mention, inline=True)
            embed.add_field(name="Jump to", value=f"[Click Here]({after.jump_url})", inline=True)
            embed.add_field(name="Before", value=before.content[:1024] if before.content else "*No text*", inline=False)
            embed.add_field(name="After", value=after.content[:1024] if after.content else "*No text*", inline=False)
            await channel.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        config = get_guild_config(guild.id)

        if not config.get("logs_enabled") or not config.get("logs_channel"):
            return
        if not config.get("log_bans"):
            return

        channel = guild.get_channel(config["logs_channel"])
        if not channel:
            return

        try:
            embed = discord.Embed(color=0xE74C3C, timestamp=datetime.now(timezone.utc))
            embed.set_author(name="Member Banned", icon_url=user.display_avatar.url if hasattr(user, 'display_avatar') else None)
            embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
            await channel.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        config = get_guild_config(guild.id)

        if not config.get("logs_enabled") or not config.get("logs_channel"):
            return
        if not config.get("log_bans"):
            return

        channel = guild.get_channel(config["logs_channel"])
        if not channel:
            return

        try:
            embed = discord.Embed(color=0x2ECC71, timestamp=datetime.now(timezone.utc))
            embed.set_author(name="Member Unbanned", icon_url=user.display_avatar.url if hasattr(user, 'display_avatar') else None)
            embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
            await channel.send(embed=embed)
        except Exception:
            pass

    @commands.command(name="welcomesetup")
    @perms_or_developer(administrator=True)
    async def welcomesetup(self, ctx: commands.Context, channel: discord.TextChannel = None, *, message: str = None) -> None:
        """Setup welcome message. Usage: alpha welcomesetup #channel Welcome {user}!"""
        if not channel:
            channel = ctx.channel

        config = get_guild_config(ctx.guild.id)
        config["welcome_channel"] = channel.id
        config["welcome_enabled"] = True
        if message:
            config["welcome_message"] = message
        save_guild_config(ctx.guild.id, config)

        embed = discord.Embed(color=0x2ECC71)
        embed.set_author(name="✅ Welcome Setup Complete")
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Message", value=message or config.get("welcome_message"), inline=False)
        embed.add_field(name="Placeholders", value="{user}, {user_name}, {server}, {member_count}", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="welcomedisable")
    @perms_or_developer(administrator=True)
    async def welcomedisable(self, ctx: commands.Context) -> None:
        """Disable welcome messages. Usage: alpha welcomedisable"""
        config = get_guild_config(ctx.guild.id)
        config["welcome_enabled"] = False
        save_guild_config(ctx.guild.id, config)
        await ctx.send(embed=self._make_embed("❌ Welcome Disabled", 0xE74C3C, "Welcome messages have been disabled."))

    @commands.command(name="logsetup")
    @perms_or_developer(administrator=True)
    async def logsetup(self, ctx: commands.Context, channel: discord.TextChannel = None) -> None:
        """Setup logging channel. Usage: alpha logsetup #channel"""
        if not channel:
            await ctx.send(embed=self._make_embed("❌ No Channel", 0xE74C3C, "Please mention a channel: `alpha logsetup #channel`"))
            return

        config = get_guild_config(ctx.guild.id)
        config["logs_channel"] = channel.id
        config["logs_enabled"] = True
        save_guild_config(ctx.guild.id, config)

        embed = discord.Embed(color=0x2ECC71)
        embed.set_author(name="✅ Logging Setup Complete")
        embed.description = f"Logs will be sent to {channel.mention}"
        embed.add_field(name="Enabled Events", value="Messages, Joins, Leaves, Roles, Bans, Edits, Purges", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="logdisable")
    @perms_or_developer(administrator=True)
    async def logdisable(self, ctx: commands.Context) -> None:
        """Disable logging. Usage: alpha logdisable"""
        config = get_guild_config(ctx.guild.id)
        config["logs_enabled"] = False
        save_guild_config(ctx.guild.id, config)
        await ctx.send(embed=self._make_embed("❌ Logging Disabled", 0xE74C3C, "Logging has been disabled."))

    @commands.command(name="logconfig")
    @perms_or_developer(administrator=True)
    async def logconfig(self, ctx: commands.Context) -> None:
        """View current logging configuration. Usage: alpha logconfig"""
        config = get_guild_config(ctx.guild.id)

        welcome_status = "✅ Enabled" if config.get("welcome_enabled") else "❌ Disabled"
        welcome_channel = ctx.guild.get_channel(config.get("welcome_channel")) if config.get("welcome_channel") else None

        logs_status = "✅ Enabled" if config.get("logs_enabled") else "❌ Disabled"
        logs_channel = ctx.guild.get_channel(config.get("logs_channel")) if config.get("logs_channel") else None

        embed = discord.Embed(color=0x9B59B6)
        embed.set_author(name="⚙️ Configuration")

        embed.add_field(name="📝 Welcome", value=f"Status: {welcome_status}", inline=False)
        if welcome_channel:
            embed.add_field(name="", value=f"Channel: {welcome_channel.mention}", inline=False)

        embed.add_field(name="📋 Logging", value=f"Status: {logs_status}", inline=False)
        if logs_channel:
            embed.add_field(name="", value=f"Channel: {logs_channel.mention}", inline=False)

        await ctx.send(embed=embed)

    @welcomesetup.error
    async def welcomesetup_error(self, ctx: commands.Context, error) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=self._make_embed("❌ Permission Denied", 0xE74C3C, "You need **Administrator** permissions."))

    @logsetup.error
    async def logsetup_error(self, ctx: commands.Context, error) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=self._make_embed("❌ Permission Denied", 0xE74C3C, "You need **Administrator** permissions."))

    @logconfig.error
    async def logconfig_error(self, ctx: commands.Context, error) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=self._make_embed("❌ Permission Denied", 0xE74C3C, "You need **Administrator** permissions."))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeLogs(bot))
