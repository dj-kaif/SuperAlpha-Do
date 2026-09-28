"""Moderation: the purge commands record who purged, so the log can name them."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.cogs import moderation
from bot.cogs.moderation import Moderation

from helpers import TEXT_CHANNEL_ID, USER_IDS, make_channel, make_ctx


@pytest.fixture
def notes(monkeypatch):
    """Capture ``note_purge`` calls instead of writing real purge state."""
    recorded = []
    monkeypatch.setattr(
        moderation, "note_purge", lambda guild, channel, who: recorded.append((guild, channel, who))
    )
    return recorded


@pytest.fixture
def purge_ctx(bot, monkeypatch, notes):
    """A ctx that can really be purged, without the 4s confirmation delay."""
    ctx = make_ctx(bot)
    ctx.channel.purge = AsyncMock(return_value=[])
    monkeypatch.setattr(moderation.asyncio, "sleep", AsyncMock())
    return ctx


async def test_purge_records_the_moderator(purge_ctx, notes):
    cog = Moderation(purge_ctx.bot)

    await cog.purge.callback(cog, purge_ctx, 5)

    assert notes == [(purge_ctx.guild, purge_ctx.channel, purge_ctx.author)]
    purge_ctx.channel.purge.assert_awaited_once_with(limit=6)


async def test_purge_rejects_out_of_range_without_noting(purge_ctx, notes):
    cog = Moderation(purge_ctx.bot)

    await cog.purge.callback(cog, purge_ctx, 0)

    assert notes == []
    purge_ctx.channel.purge.assert_not_called()
    assert "between 1 and 500" in purge_ctx.send.await_args.kwargs["embed"].description


async def test_slash_purge_records_the_moderator(bot, notes, monkeypatch):
    monkeypatch.setattr(moderation.asyncio, "sleep", AsyncMock())
    channel = make_channel(TEXT_CHANNEL_ID)
    channel.purge = AsyncMock(return_value=[])
    interaction = SimpleNamespace(
        channel=channel,
        guild=make_ctx(bot).guild,
        user=make_ctx(bot).author,
        response=SimpleNamespace(send_message=AsyncMock()),
        delete_original_response=AsyncMock(),
    )
    cog = Moderation(bot)

    await cog.slash_purge.callback(cog, interaction, 3)

    assert notes == [(interaction.guild, channel, interaction.user)]
    channel.purge.assert_awaited_once_with(limit=3)
