from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str
    music_library_path: Path
    ffmpeg_path: str
    ffmpeg_before_options: str
    ffmpeg_options: str
    command_sync_guild_id: int | None
    bot_owner_id: int | None
    log_level: str


def _read_optional_int(raw_value: str | None) -> int | None:
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        return None


def load_settings() -> Settings:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is missing in .env")

    raw_music_path = os.getenv("MUSIC_LIBRARY_PATH", "music").strip() or "music"
    music_path = Path(raw_music_path)
    if not music_path.is_absolute():
        music_path = (Path(__file__).resolve().parent / music_path).resolve()

    ffmpeg_path = os.getenv("FFMPEG_PATH", "ffmpeg").strip() or "ffmpeg"
    ffmpeg_before_options = os.getenv(
        "FFMPEG_BEFORE_OPTIONS",
        "-nostdin -hide_banner -loglevel warning -rtbufsize 150M -thread_queue_size 4096",
    ).strip()
    ffmpeg_options = os.getenv("FFMPEG_OPTIONS", "-vn -ac 2 -ar 48000 -bufsize 2000k -threads 2").strip()
    guild_id = _read_optional_int(os.getenv("COMMAND_SYNC_GUILD_ID"))
    bot_owner_id = _read_optional_int(os.getenv("BOT_OWNER_ID"))
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"

    return Settings(
        discord_bot_token=token,
        music_library_path=music_path,
        ffmpeg_path=ffmpeg_path,
        ffmpeg_before_options=ffmpeg_before_options,
        ffmpeg_options=ffmpeg_options,
        command_sync_guild_id=guild_id,
        bot_owner_id=bot_owner_id,
        log_level=log_level,
    )
