"""Server logging: purge logs, deleted-message logs and deleted-image uploads."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.cogs import welcomelogs
from bot.cogs.welcomelogs import WelcomeLogs, get_guild_config, note_purge
from bot.services import attachment_cache

from helpers import GUILD_ID, TEXT_CHANNEL_ID, USER_IDS, make_channel, make_guild, make_member, make_message

LOGS_CHANNEL_ID = 9001


@pytest.fixture(autouse=True)
def clean_cache():
    attachment_cache.clear()
    yield
    attachment_cache.clear()


@pytest.fixture
def logged(bot, tmp_path, monkeypatch):
    """A guild with message logging switched on, storage in a tmp dir."""
    monkeypatch.setattr(welcomelogs, "CONFIG_FILE", str(tmp_path / "config.json"))
    source = make_channel(TEXT_CHANNEL_ID, name="general")
    logs = make_channel(LOGS_CHANNEL_ID, name="server-logs")
    guild = make_guild(channels=[source, logs])

    config = get_guild_config(GUILD_ID)
    config.update({"logs_channel": logs.id, "logs_enabled": True})
    welcomelogs.save_guild_config(GUILD_ID, config)

    return SimpleNamespace(cog=WelcomeLogs(bot), guild=guild, source=source, logs=logs)


@pytest.fixture
def env(bot):
    """Just enough of a guild to hang messages off, with no logging configured."""
    source = make_channel(TEXT_CHANNEL_ID, name="general")
    return SimpleNamespace(guild=make_guild(channels=[source]), source=source)


def _config(**overrides) -> dict:
    config = get_guild_config(GUILD_ID)
    config.update(overrides)
    welcomelogs.save_guild_config(GUILD_ID, config)
    return config


def _attachment(name="pic.png", data=b"\x89PNG\r\n", content_type="image/png", size=None, with_to_bytes=True):
    attachment = MagicMock()
    attachment.filename = name
    attachment.content_type = content_type
    attachment.size = len(data) if size is None else size
    if with_to_bytes:
        attachment.to_bytes = AsyncMock(return_value=data)
    else:
        del attachment.to_bytes
    attachment.read = AsyncMock(return_value=data)
    return attachment


def _message(env, author, *, content="", attachments=(), message_id=1, minutes_ago=0):
    message = make_message(
        content, author=author, channel=env.source, guild=env.guild, message_id=message_id
    )
    message.attachments = list(attachments)
    message.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return message


def _sent_embeds(logs) -> list[discord.Embed]:
    """Every embed handed to the log channel, across all send calls."""
    embeds = []
    for call in logs.send.call_args_list:
        embeds.extend(call.kwargs.get("embeds") or [])
        if call.kwargs.get("embed"):
            embeds.append(call.kwargs["embed"])
    return embeds


def _sent_transcripts(logs) -> list[str]:
    """Text of every .txt transcript attached to the log channel."""
    texts = []
    for call in logs.send.call_args_list:
        for handle in call.kwargs.get("files") or []:
            if str(getattr(handle, "filename", "")).endswith(".txt"):
                fp = handle.fp
                fp.seek(0)
                texts.append(fp.read().decode("utf-8"))
    return texts


def _sent_filenames(logs) -> list[str]:
    names = []
    for call in logs.send.call_args_list:
        for handle in call.kwargs.get("files") or []:
            names.append(str(getattr(handle, "filename", "")))
    return names


def _field(embed: discord.Embed, name: str) -> str | None:
    for field in embed.fields:
        if field.name == name:
            return field.value
    return None


# ── Config ────────────────────────────────────────────────────────────────────
def test_new_toggles_reach_guilds_configured_before_them(tmp_path, monkeypatch):
    """A server configured before log_purges existed still gets the default."""
    monkeypatch.setattr(welcomelogs, "CONFIG_FILE", str(tmp_path / "config.json"))
    with open(welcomelogs.CONFIG_FILE, "w") as handle:
        json.dump({str(GUILD_ID): {"logs_channel": LOGS_CHANNEL_ID, "logs_enabled": True}}, handle)

    config = get_guild_config(GUILD_ID)

    assert config["log_purges"] is True
    assert config["log_attachments"] is True
    assert config["logs_channel"] == LOGS_CHANNEL_ID


def test_config_defaults_are_not_mutated_by_callers():
    first = get_guild_config(GUILD_ID)
    first["log_purges"] = False
    assert get_guild_config(GUILD_ID)["log_purges"] is True


# ── Purge logging ─────────────────────────────────────────────────────────────
async def test_purge_logs_every_deleted_message(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    messages = [
        _message(logged, author, content="first", message_id=1, minutes_ago=3),
        _message(logged, author, content="second", message_id=2, minutes_ago=2),
        _message(logged, author, content="third", message_id=3, minutes_ago=1),
    ]

    await logged.cog.on_bulk_message_delete(messages)

    embeds = _sent_embeds(logged.logs)
    assert embeds[0].author.name == "Messages Purged"
    assert "3 message(s) removed" in embeds[0].description

    transcripts = _sent_transcripts(logged.logs)
    assert len(transcripts) == 1
    text = transcripts[0]
    for body in ("first", "second", "third"):
        assert body in text
    # one embed and one message, not one per purged message
    assert logged.logs.send.await_count == 1


async def test_purge_log_names_the_moderator(logged):
    moderator = make_member(USER_IDS["admin"], guild=logged.guild)
    author = make_member(USER_IDS["member"], guild=logged.guild)
    note_purge(logged.guild, logged.source, moderator)

    await logged.cog.on_bulk_message_delete([_message(logged, author, content="bye", message_id=1)])

    assert _field(_sent_embeds(logged.logs)[0], "Purged by") == str(moderator)


async def test_purge_without_a_note_is_reported_as_client_side(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)

    await logged.cog.on_bulk_message_delete([_message(logged, author, content="bye", message_id=1)])

    assert "client-side" in _field(_sent_embeds(logged.logs)[0], "Purged by")


async def test_purge_note_is_single_use(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    note_purge(logged.guild, logged.source, make_member(USER_IDS["admin"], guild=logged.guild))
    message = _message(logged, author, content="bye", message_id=1)

    await logged.cog.on_bulk_message_delete([message])
    logged.logs.send.reset_mock()
    await logged.cog.on_bulk_message_delete([message])

    assert "client-side" in _field(_sent_embeds(logged.logs)[0], "Purged by")


async def test_purge_logging_can_be_switched_off(logged):
    _config(log_purges=False)
    author = make_member(USER_IDS["member"], guild=logged.guild)

    await logged.cog.on_bulk_message_delete([_message(logged, author, content="bye", message_id=1)])

    logged.logs.send.assert_not_called()


async def test_purge_logging_needs_logs_enabled(logged):
    _config(logs_enabled=False)
    author = make_member(USER_IDS["member"], guild=logged.guild)

    await logged.cog.on_bulk_message_delete([_message(logged, author, content="bye", message_id=1)])

    logged.logs.send.assert_not_called()


async def test_purge_logging_ignores_dm_messages(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    message = _message(logged, author, content="bye", message_id=1)
    message.guild = None

    await logged.cog.on_bulk_message_delete([message])

    logged.logs.send.assert_not_called()


async def test_purge_logs_every_message_not_just_the_first_few(logged):
    """A file has no embed cap, so nothing gets silently dropped."""
    author = make_member(USER_IDS["member"], guild=logged.guild)
    messages = [
        _message(logged, author, content=f"msg {i}", message_id=i, minutes_ago=100 - i)
        for i in range(40)
    ]

    await logged.cog.on_bulk_message_delete(messages)

    embeds = _sent_embeds(logged.logs)
    assert "40 message(s) removed" in embeds[0].description
    assert _field(embeds[0], "Note") is None
    text = _sent_transcripts(logged.logs)[0]
    for i in range(40):
        assert f"msg {i}" in text


async def test_purge_releases_cached_images_it_never_logs(logged):
    """Bytes for pictures that don't fit in one log message must not linger."""
    author = make_member(USER_IDS["member"], guild=logged.guild)
    messages = [
        _message(
            logged, author, content=f"msg {i}", message_id=i, minutes_ago=100 - i,
            attachments=[_attachment(f"p{i}.png")],
        )
        for i in range(40)
    ]
    for message in messages:
        await logged.cog.on_message(message)
    assert attachment_cache.stats()["messages"] == 40

    await logged.cog.on_bulk_message_delete(messages)

    # Images that fit are claimed and re-uploaded; the rest are discarded.
    # Either way the purge must not leave bytes in the cache.
    assert attachment_cache.stats() == {"messages": 0, "bytes": 0}


async def test_purge_batches_embeds_instead_of_spamming(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    messages = [
        _message(logged, author, content=f"msg {i}", message_id=i, minutes_ago=100 - i)
        for i in range(12)
    ]

    await logged.cog.on_bulk_message_delete(messages)

    # A single message: the summary embed plus the transcript file.
    assert logged.logs.send.await_count == 1
    assert _sent_transcripts(logged.logs)


async def test_purge_counts_skipped_bot_messages(logged):
    human = make_member(USER_IDS["member"], guild=logged.guild)
    bot = make_member(USER_IDS["admin"], guild=logged.guild, bot=True)
    messages = [
        _message(logged, human, content="human", message_id=1),
        _message(logged, bot, content="beep", message_id=2),
    ]

    await logged.cog.on_bulk_message_delete(messages)

    embeds = _sent_embeds(logged.logs)
    assert _field(embeds[0], "Bot messages") == "1 skipped"
    text = _sent_transcripts(logged.logs)[0]
    assert "human" in text
    assert "beep" not in text


async def test_purge_keeps_oldest_message_first(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    older = _message(logged, author, content="older", message_id=1, minutes_ago=9)
    newer = _message(logged, author, content="newer", message_id=2, minutes_ago=1)

    await logged.cog.on_bulk_message_delete([newer, older])

    text = _sent_transcripts(logged.logs)[0]
    assert text.index("older") < text.index("newer")


async def test_purge_with_nothing_deleted_sends_nothing(logged):
    await logged.cog.on_bulk_message_delete([])

    logged.logs.send.assert_not_called()


# ── Deleted pictures ──────────────────────────────────────────────────────────
async def test_on_message_caches_pictures_while_they_still_exist(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    message = _message(logged, author, content="look", attachments=[_attachment()], message_id=7)

    await logged.cog.on_message(message)

    assert attachment_cache.has_message_images(7)


async def test_on_message_does_not_cache_when_logging_is_off(logged):
    _config(log_attachments=False)
    author = make_member(USER_IDS["member"], guild=logged.guild)
    message = _message(logged, author, attachments=[_attachment()], message_id=7)

    await logged.cog.on_message(message)

    assert not attachment_cache.has_message_images(7)


async def test_on_message_does_not_cache_bot_pictures(logged):
    author = make_member(USER_IDS["admin"], guild=logged.guild, bot=True)
    message = _message(logged, author, attachments=[_attachment()], message_id=7)

    await logged.cog.on_message(message)

    assert not attachment_cache.has_message_images(7)


async def test_single_delete_resends_the_picture(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    message = _message(logged, author, content="oops", attachments=[_attachment()], message_id=7)
    await logged.cog.on_message(message)

    await logged.cog.on_message_delete(message)

    kwargs = logged.logs.send.await_args.kwargs
    assert [f.filename for f in kwargs["files"]] == ["pic.png"]
    assert kwargs["embed"].image.url == "attachment://pic.png"
    # The bytes are handed over once, never re-sent.
    assert not attachment_cache.has_message_images(7)


async def test_purge_resends_deleted_pictures(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    first = _message(logged, author, content="one", attachments=[_attachment("a.png")], message_id=1, minutes_ago=2)
    second = _message(logged, author, content="two", attachments=[_attachment("b.png")], message_id=2, minutes_ago=1)
    for message in (first, second):
        await logged.cog.on_message(message)

    await logged.cog.on_bulk_message_delete([first, second])

    batch = logged.logs.send.await_args.kwargs
    names = [f.filename for f in batch["files"]]
    assert [n for n in names if not n.endswith(".txt")] == ["a.png", "b.png"]
    assert sum(n.endswith(".txt") for n in names) == 1
    # pictures are no longer one-embed-per-message, so they carry no embed image
    assert batch.get("embeds") is None and batch["embed"].image.url is None


async def test_picture_is_dropped_when_attachments_logging_is_off(logged):
    _config(log_attachments=False)
    author = make_member(USER_IDS["member"], guild=logged.guild)
    message = _message(logged, author, attachments=[_attachment()], message_id=7)
    await attachment_cache.cache_message_images(message)

    await logged.cog.on_bulk_message_delete([message])

    names = _sent_filenames(logged.logs)
    assert len(names) == 1 and names[0].endswith(".txt")
    assert not attachment_cache.has_message_images(7)


async def test_delete_without_cached_picture_still_logs_the_message(logged):
    """Pictures uploaded before the bot started (or evicted) must not break the log."""
    author = make_member(USER_IDS["member"], guild=logged.guild)
    message = _message(logged, author, content="no cache", attachments=[_attachment()], message_id=7)

    await logged.cog.on_message_delete(message)

    kwargs = logged.logs.send.await_args.kwargs
    assert kwargs["files"] is None
    assert kwargs["embed"].description == "no cache"
    assert _field(kwargs["embed"], "Attachments") == "1 file(s)"


async def test_edit_caches_a_newly_added_picture(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    before = _message(logged, author, content="same", message_id=7)
    after = _message(logged, author, content="same", attachments=[_attachment()], message_id=7)

    await logged.cog.on_message_edit(before, after)

    assert attachment_cache.has_message_images(7)


# ── attachment_cache unit tests ───────────────────────────────────────────────
async def test_cache_ignores_non_pictures(env):
    author = make_member(USER_IDS["member"])
    message = _message(env, author, attachments=[
        _attachment("report.pdf", b"%PDF", content_type="application/pdf"),
        _attachment("payload.exe", b"MZ", content_type=None),
    ])

    assert await attachment_cache.cache_message_images(message) == 0
    assert attachment_cache.stats()["messages"] == 0


async def test_cache_skips_files_over_the_size_limit(env):
    author = make_member(USER_IDS["member"])
    oversized = _attachment("huge.png", b"x" * 10, size=attachment_cache.MAX_IMAGE_BYTES + 1)
    message = _message(env, author, attachments=[oversized])

    assert await attachment_cache.cache_message_images(message) == 0


async def test_cache_bounds_memory_when_size_is_unreported(env):
    """`size` can be missing, so the downloaded length is what must bound us."""
    author = make_member(USER_IDS["member"])
    lying = _attachment(
        "sneaky.png",
        b"x" * (attachment_cache.MAX_IMAGE_BYTES + 1),
        size=1,  # claims to be tiny
    )
    message = _message(env, author, attachments=[lying])

    assert await attachment_cache.cache_message_images(message) == 0
    assert attachment_cache.stats()["bytes"] == 0


async def test_cache_falls_back_to_read_on_older_discord_py(env):
    author = make_member(USER_IDS["member"])
    attachment = _attachment("pic.png", b"data", with_to_bytes=False)
    message = _message(env, author, attachments=[attachment], message_id=3)

    assert await attachment_cache.cache_message_images(message) == 1
    assert attachment_cache.pop_message_images(3) == [("pic.png", b"data")]


async def test_cache_keeps_only_a_few_images_per_message(env):
    author = make_member(USER_IDS["member"])
    message = _message(env, author, attachments=[_attachment(f"p{i}.png") for i in range(6)], message_id=4)

    assert await attachment_cache.cache_message_images(message) == attachment_cache.MAX_IMAGES_PER_MESSAGE


async def test_cache_evicts_oldest_messages(monkeypatch, env):
    monkeypatch.setattr(attachment_cache, "MAX_MESSAGES", 3)
    author = make_member(USER_IDS["member"])
    for message_id in range(5):
        await attachment_cache.cache_message_images(
            _message(env, author, attachments=[_attachment()], message_id=message_id)
        )

    assert attachment_cache.stats()["messages"] == 3
    assert not attachment_cache.has_message_images(0)
    assert attachment_cache.has_message_images(4)


async def test_cache_evicts_to_stay_inside_the_memory_budget(monkeypatch, env):
    monkeypatch.setattr(attachment_cache, "MAX_TOTAL_BYTES", 10)
    author = make_member(USER_IDS["member"])
    for message_id in range(4):
        await attachment_cache.cache_message_images(
            _message(env, author, attachments=[_attachment(data=b"0123456789")], message_id=message_id)
        )

    assert attachment_cache.stats()["bytes"] <= 10
    assert attachment_cache.stats()["messages"] <= 1


async def test_clearing_the_cache_frees_everything(env):
    await attachment_cache.cache_message_images(
        _message(env, make_member(USER_IDS["member"]), attachments=[_attachment()], message_id=1)
    )
    attachment_cache.clear()
    assert attachment_cache.stats() == {"messages": 0, "bytes": 0}


def test_cog_unload_clears_the_cache(bot, env):
    cog = WelcomeLogs(bot)
    cache = attachment_cache
    cache._cache[1] = [("a.png", b"x" * 64)]
    cache._bytes_held = 64
    cog.cog_unload()
    assert cache.stats() == {"messages": 0, "bytes": 0}


async def test_discard_forgets_without_returning(env):
    message = _message(env, make_member(USER_IDS["member"]), attachments=[_attachment()], message_id=1)
    await attachment_cache.cache_message_images(message)

    attachment_cache.discard_message_images(1)

    assert attachment_cache.pop_message_images(1) == []
    assert attachment_cache.stats()["bytes"] == 0


# ── Purge transcript file ─────────────────────────────────────────────────────
async def test_purge_is_always_a_single_log_message(logged):
    """A 200-message purge must not become 200 messages in the log channel."""
    author = make_member(USER_IDS["member"], guild=logged.guild)
    messages = [
        _message(logged, author, content=f"msg {i}", message_id=i, minutes_ago=200 - i)
        for i in range(200)
    ]

    await logged.cog.on_bulk_message_delete(messages)

    assert logged.logs.send.await_count == 1
    assert len(_sent_transcripts(logged.logs)) == 1


async def test_purge_transcript_records_metadata(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    message = _message(
        logged, author, content="hello there", attachments=[_attachment("pic.png")], message_id=99
    )
    await logged.cog.on_message(message)

    await logged.cog.on_bulk_message_delete([message])

    text = _sent_transcripts(logged.logs)[0]
    assert "hello there" in text
    assert "pic.png" in text
    assert str(USER_IDS["member"]) in text          # author id
    assert str(message.id) in text                   # message id
    assert str(GUILD_ID) in text                     # server id
    assert "#" + logged.source.name in text          # channel name
    assert "Purged by" in text
    assert text.endswith("\n")


async def test_purge_transcript_marks_empty_messages(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    message = _message(logged, author, content="", message_id=5)

    await logged.cog.on_bulk_message_delete([message])

    assert "no text content" in _sent_transcripts(logged.logs)[0]


async def test_purge_never_exceeds_the_file_limit(logged):
    """One transcript + at most FILES_PER_LOG_MESSAGE-1 pictures."""
    author = make_member(USER_IDS["member"], guild=logged.guild)
    messages = [
        _message(
            logged, author, content=f"m{i}", message_id=i, minutes_ago=50 - i,
            attachments=[_attachment(f"p{i}.png")],
        )
        for i in range(20)
    ]
    for message in messages:
        await logged.cog.on_message(message)
    assert attachment_cache.stats()["messages"] == 20

    await logged.cog.on_bulk_message_delete(messages)

    assert logged.logs.send.await_count == 1
    files = logged.logs.send.await_args.kwargs["files"]
    assert len(files) <= welcomelogs.FILES_PER_LOG_MESSAGE
    # the surplus is reported rather than dropped silently
    note = _field(_sent_embeds(logged.logs)[0], "Note")
    assert "not attached" in note
    # and the cache is drained either way
    assert attachment_cache.stats() == {"messages": 0, "bytes": 0}


async def test_purge_transcript_truncates_a_wall_of_text(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    message = _message(logged, author, content="x" * (welcomelogs.PURGE_TEXT_CHUNK + 500), message_id=3)

    await logged.cog.on_bulk_message_delete([message])

    text = _sent_transcripts(logged.logs)[0]
    assert "[...truncated]" in text
    assert text.count("x") <= welcomelogs.PURGE_TEXT_CHUNK + 200


async def test_purge_transcript_filename_is_a_txt(logged):
    author = make_member(USER_IDS["member"], guild=logged.guild)
    message = _message(logged, author, content="hi", message_id=1)

    await logged.cog.on_bulk_message_delete([message])

    names = _sent_filenames(logged.logs)
    assert len(names) == 1
    assert names[0].startswith("purge-") and names[0].endswith(".txt")


async def test_purge_transcript_separates_removed_from_logged(logged):
    """Bot messages are excluded from the transcript but still counted as removed."""
    human = make_member(USER_IDS["member"], guild=logged.guild)
    bot = make_member(USER_IDS["admin"], guild=logged.guild, bot=True)
    messages = [
        _message(logged, human, content="kept", message_id=1),
        _message(logged, bot, content="dropped", message_id=2),
        _message(logged, bot, content="dropped too", message_id=3),
    ]

    await logged.cog.on_bulk_message_delete(messages)

    text = _sent_transcripts(logged.logs)[0]
    assert "Removed   : 3 message(s)" in text
    assert "2 bot message(s) not logged" in text
    assert "Logged    : 1 message(s)" in text
    assert "kept" in text and "dropped" not in text


async def test_purge_of_only_bot_messages_still_sends_a_summary(logged):
    """No logged messages means an empty transcript, never an IndexError."""
    bot = make_member(USER_IDS["admin"], guild=logged.guild, bot=True)
    messages = [_message(logged, bot, content="beep", message_id=i) for i in (1, 2)]

    await logged.cog.on_bulk_message_delete(messages)

    assert logged.logs.send.await_count == 1
    embeds = _sent_embeds(logged.logs)
    assert embeds[0].author.name == "Messages Purged"
    assert _field(embeds[0], "Bot messages") == "2 skipped"
    text = _sent_transcripts(logged.logs)[0]
    assert "Logged    : 0 message(s)" in text
    assert "2 bot message(s) not logged" in text


async def test_purge_caps_images_across_several_messages(logged):
    """3 messages x 4 cached pictures is 12, which is over the 9 available slots."""
    author = make_member(USER_IDS["member"], guild=logged.guild)
    messages = [
        _message(
            logged, author, content=f"m{i}", message_id=i, minutes_ago=10 - i,
            attachments=[_attachment(f"p{i}_{j}.png") for j in range(4)],
        )
        for i in range(3)
    ]
    for message in messages:
        await logged.cog.on_message(message)

    await logged.cog.on_bulk_message_delete(messages)

    files = logged.logs.send.await_args.kwargs["files"]
    assert len(files) == welcomelogs.MAX_PURGE_IMAGES + 1  # + transcript
    assert len(files) <= welcomelogs.FILES_PER_LOG_MESSAGE
    assert "3 image(s) not attached" in _field(_sent_embeds(logged.logs)[0], "Note")
    assert attachment_cache.stats() == {"messages": 0, "bytes": 0}
