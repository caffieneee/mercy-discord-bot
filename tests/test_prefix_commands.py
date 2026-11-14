"""Regression tests for prefix-command dispatch.

Prefix commands used to be processed only through ``cogs/thread.py``'s
``on_message`` listener, which made them depend on an unrelated feature cog
loading successfully (and pushed every message through the command pipeline
twice). ``DiscordBot`` now owns its own ``on_message``.
"""

import asyncio
from unittest.mock import MagicMock

import discord
from discord.ext import commands

import main as mainmod


def _make_message(bot, content, *, author_id=999, is_bot=False, message_id=4242):
    """Build a Message stub good enough for ``get_context``/``process_commands``."""
    msg = MagicMock()
    msg.content = content
    msg.author.bot = is_bot
    msg.author.id = author_id
    msg.id = message_id
    msg.channel.id = 7
    msg.guild.id = 3
    msg.guild.me = MagicMock()
    msg.attachments = []
    msg.embeds = []
    msg.stickers = []
    return msg


def test_discordbot_defines_its_own_on_message_handler():
    """The bot must not depend on a cog to wire up prefix commands."""

    async def scenario():
        bot = mainmod.DiscordBot()
        try:
            assert "on_message" in type(bot).__dict__, (
                "DiscordBot must define on_message itself"
            )
            # It must actually be DiscordBot's handler, not the library default.
            assert type(bot).on_message is mainmod.DiscordBot.on_message
        finally:
            await bot.close()

    asyncio.run(scenario())


def test_prefix_command_runs_through_on_message():
    """A prefixed message must reach the command callback."""

    async def scenario():
        bot = mainmod.DiscordBot()
        bot._connection.user = MagicMock(id=12345)
        hits = []

        @bot.command(name="hello")
        async def hello(ctx):
            hits.append(ctx.message.content)

        try:
            msg = _make_message(bot, ".hello")
            await bot.on_message(msg)
            assert hits == [".hello"]
        finally:
            await bot.close()

    asyncio.run(scenario())


def test_prefix_command_runs_through_dispatch_with_cogs_absent():
    """Full gateway-style dispatch works even when no cog is loaded.

    This is the exact failure mode the old design had: ``thread.py`` was the
    only thing calling ``process_commands``. The bot must work without it.
    """

    async def scenario():
        bot = mainmod.DiscordBot()
        bot.loop = asyncio.get_running_loop()
        bot._connection.user = MagicMock(id=12345)
        hits = []

        @bot.command(name="hello")
        async def hello(ctx):
            hits.append(True)

        try:
            bot.dispatch("message", _make_message(bot, ".hello"))
            await asyncio.sleep(0.2)
            assert hits == [True]
            assert not bot.extensions, "no cogs should be loaded for this test"
        finally:
            await bot.close()

    asyncio.run(scenario())


def test_bot_messages_are_ignored():
    async def scenario():
        bot = mainmod.DiscordBot()
        bot._connection.user = MagicMock(id=12345)
        hits = []

        @bot.command(name="hello")
        async def hello(ctx):
            hits.append(True)

        try:
            await bot.on_message(
                _make_message(bot, ".hello", author_id=555, is_bot=True)
            )
            assert hits == []
        finally:
            await bot.close()

    asyncio.run(scenario())


def test_unprefixed_message_does_not_invoke_a_command():
    async def scenario():
        bot = mainmod.DiscordBot()
        bot._connection.user = MagicMock(id=12345)
        hits = []

        @bot.command(name="hello")
        async def hello(ctx):
            hits.append(True)

        try:
            await bot.on_message(_make_message(bot, "hello there"))
            assert hits == []
        finally:
            await bot.close()

    asyncio.run(scenario())


def test_thread_cog_no_longer_calls_process_commands():
    """The autothread listener must not re-dispatch prefix commands."""
    import inspect

    import cogs.thread as thread_module

    source = inspect.getsource(thread_module.ThreadCreatorCog.on_message)
    assert "await self.bot.process_commands" not in source
