from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import discord
from discord.ext import commands

from config import Settings, load_settings


class MyDMBBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.settings: Settings = settings

    async def setup_hook(self) -> None:
        await self.load_extension("cogs.music")

        # Always sync global commands so they are available in every server.
        global_synced = await self.tree.sync()
        logging.info("Synced %s global command(s)", len(global_synced))

        # One-time cleanup: remove old guild-scoped duplicates if configured.
        if self.settings.command_sync_guild_id:
            guild = discord.Object(id=self.settings.command_sync_guild_id)
            self.tree.clear_commands(guild=guild)
            cleared = await self.tree.sync(guild=guild)
            logging.info("Cleared %s guild-scoped command(s) in dev guild %s", len(cleared), guild.id)

    async def on_ready(self) -> None:
        logging.info("Bot is online as %s", self.user)


if __name__ == "__main__":
    settings = load_settings()
    project_root = Path(__file__).resolve().parent
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    log_file = logs_dir / "bot.log"

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                log_file,
                encoding="utf-8",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
            ),
        ],
    )

    bot = MyDMBBot(settings)
    bot.run(settings.discord_bot_token)
