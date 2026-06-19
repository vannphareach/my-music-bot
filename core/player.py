from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

import discord

from core.indexer import Song


_LOGGER = logging.getLogger(__name__)
_CONNECT_TIMEOUT_SECONDS = 45.0
_CONNECT_RETRY_ATTEMPTS = 2
_CONNECT_RETRY_DELAY_SECONDS = 1.0
_IDLE_DISCONNECT_SECONDS = 600.0  # 10 minutes


class PlaybackError(Exception):
    pass


class GuildAudioPlayer:
    def __init__(
        self,
        guild_id: int,
        loop: asyncio.AbstractEventLoop,
        ffmpeg_executable: str = "ffmpeg",
        ffmpeg_before_options: str = "-nostdin",
        ffmpeg_options: str = "-vn",
    ) -> None:
        self.guild_id = guild_id
        self.loop = loop
        self.ffmpeg_executable = ffmpeg_executable
        self.ffmpeg_before_options = ffmpeg_before_options
        self.ffmpeg_options = ffmpeg_options

        self._queue: deque[Song] = deque()
        self.voice_client: discord.VoiceClient | None = None
        self.current_song: Song | None = None
        self._lock = asyncio.Lock()
        self.last_text_channel: discord.TextChannel | None = None
        self.on_playback_error: Callable[[Any, Exception], Coroutine[Any, Any, None]] | None = None
        self.on_song_start: Callable[[Any], Coroutine[Any, Any, None]] | None = None
        self.volume: float = 0.5
        self._idle_task: asyncio.Task | None = None

    async def connect(self, voice_channel: discord.VoiceChannel) -> None:
        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.channel and self.voice_client.channel.id != voice_channel.id:
                await asyncio.wait_for(self.voice_client.move_to(voice_channel), timeout=_CONNECT_TIMEOUT_SECONDS)
            return

        last_error: Exception | None = None
        for attempt in range(1, _CONNECT_RETRY_ATTEMPTS + 1):
            try:
                self.voice_client = await asyncio.wait_for(
                    voice_channel.connect(), timeout=_CONNECT_TIMEOUT_SECONDS
                )
                return
            except asyncio.TimeoutError as exc:
                self.voice_client = None
                last_error = exc
                _LOGGER.warning(
                    "Guild %s: voice connect timeout (attempt %s/%s).",
                    self.guild_id,
                    attempt,
                    _CONNECT_RETRY_ATTEMPTS,
                )
            except discord.ClientException as exc:
                self.voice_client = None
                last_error = exc
                _LOGGER.warning(
                    "Guild %s: voice connect failed (attempt %s/%s): %s",
                    self.guild_id,
                    attempt,
                    _CONNECT_RETRY_ATTEMPTS,
                    exc,
                )

            if attempt < _CONNECT_RETRY_ATTEMPTS:
                await asyncio.sleep(_CONNECT_RETRY_DELAY_SECONDS)

        raise PlaybackError(
            f"Could not connect to voice channel after {_CONNECT_RETRY_ATTEMPTS} attempt(s)."
        ) from last_error

    def handle_disconnect(self) -> None:
        """Call when the bot is force-kicked so stale state is cleared."""
        self._cancel_idle_timer()
        self.voice_client = None
        self.current_song = None
        self._queue.clear()
        _LOGGER.warning("Guild %s: voice client cleared after unexpected disconnect.", self.guild_id)

    def shuffle_queue(self) -> int:
        """Shuffle the pending queue in-place. Returns the new queue length."""
        items = list(self._queue)
        random.shuffle(items)
        self._queue = deque(items)
        return len(self._queue)

    def queue_snapshot(self) -> list[Song]:
        """Return a copy of the upcoming queue (not including current song)."""
        return list(self._queue)

    async def enqueue(self, song: Song) -> bool:
        """Add song to queue. Returns True if the song started playing immediately."""
        self._cancel_idle_timer()
        self._queue.append(song)
        await self._start_next_if_idle()
        return self.current_song is not None and self.current_song.path == song.path

    async def pause(self) -> bool:
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            return True
        return False

    async def resume(self) -> bool:
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            return True
        return False

    async def skip(self) -> bool:
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
            return True
        return False

    def skipto(self, index: int) -> Song | None:
        """Drop all songs before 1-based *index* in the queue so it plays next.

        Returns the target Song on success, or None if index is out of range.
        """
        if index < 1 or index > len(self._queue):
            return None
        # Remove the songs that come before the target (index is 1-based)
        for _ in range(index - 1):
            self._queue.popleft()
        return self._queue[0]

    async def stop(self, disconnect: bool) -> None:
        self._cancel_idle_timer()
        self._queue.clear()
        self.current_song = None

        if self.voice_client:
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()
            if disconnect and self.voice_client.is_connected():
                try:
                    await self.voice_client.disconnect(force=True)
                except Exception:
                    _LOGGER.warning("Guild %s: error during voice disconnect.", self.guild_id, exc_info=True)
                finally:
                    self.voice_client = None

    async def _start_next_if_idle(self) -> None:
        async with self._lock:
            if not self.voice_client or not self.voice_client.is_connected():
                return
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                return

            # Drain any missing files before picking the next song
            song: Song | None = None
            while self._queue:
                candidate = self._queue.popleft()
                if candidate.path.exists():
                    song = candidate
                    break
                _LOGGER.warning("Guild %s: file missing, skipping '%s'.", self.guild_id, candidate.title)
                if self.on_playback_error:
                    err = FileNotFoundError(f"File no longer exists: {candidate.path}")
                    asyncio.create_task(self.on_playback_error(candidate, err))

            if song is None:
                self.current_song = None
                self._start_idle_timer()
                return

            self.current_song = song

            source = discord.FFmpegPCMAudio(
                executable=self.ffmpeg_executable,
                source=str(song.path),
                before_options=self.ffmpeg_before_options,
                options=self.ffmpeg_options,
            )
            source = discord.PCMVolumeTransformer(source, volume=self.volume)

            def _after_playback(error: Exception | None) -> None:
                if error:
                    _LOGGER.error("Guild %s: playback error for '%s': %s", self.guild_id, song.title, error)
                    if self.on_playback_error:
                        asyncio.run_coroutine_threadsafe(
                            self.on_playback_error(song, error), self.loop
                        )
                asyncio.run_coroutine_threadsafe(self._playback_finished(), self.loop)

            try:
                self.voice_client.play(source, after=_after_playback)
                _LOGGER.info(
                    "Playing '%s' in guild %s using FFmpeg options: before='%s' options='%s'",
                    song.title,
                    self.guild_id,
                    self.ffmpeg_before_options,
                    self.ffmpeg_options,
                )
                if self.on_song_start:
                    asyncio.create_task(self.on_song_start(song))
            except FileNotFoundError as exc:
                self.current_song = None
                raise PlaybackError(
                    "FFmpeg executable was not found. Install FFmpeg and add it to PATH, or configure FFMPEG_PATH."
                ) from exc
            except discord.ClientException as exc:
                self.current_song = None
                raise PlaybackError(f"Failed to start playback: {exc}") from exc

    async def _playback_finished(self) -> None:
        self.current_song = None
        await self._start_next_if_idle()

    def begin_idle_countdown(self) -> bool:
        """Start idle disconnect countdown if not already running.

        Returns True when a new countdown was started, False if one was already active.
        """
        if self._idle_task and not self._idle_task.done():
            return False
        self._start_idle_timer()
        return True

    def cancel_idle_countdown(self) -> bool:
        """Cancel idle disconnect countdown.

        Returns True when an active countdown existed and was cancelled.
        """
        active = bool(self._idle_task and not self._idle_task.done())
        self._cancel_idle_timer()
        return active

    def _start_idle_timer(self) -> None:
        self._cancel_idle_timer()
        self._idle_task = asyncio.create_task(self._idle_disconnect_after_timeout())

    def _cancel_idle_timer(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _idle_disconnect_after_timeout(self) -> None:
        try:
            await asyncio.sleep(_IDLE_DISCONNECT_SECONDS)
        except asyncio.CancelledError:
            return

        if not self.voice_client or not self.voice_client.is_connected():
            return
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            return

        _LOGGER.info(
            "Guild %s: idle for %.0f seconds, disconnecting.",
            self.guild_id,
            _IDLE_DISCONNECT_SECONDS,
        )
        await self.stop(disconnect=True)

        if self.last_text_channel:
            try:
                mins = int(_IDLE_DISCONNECT_SECONDS // 60)
                await self.last_text_channel.send(
                    f"Left the voice channel after {mins} minutes of inactivity."
                )
            except Exception:
                pass

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    @property
    def is_connected(self) -> bool:
        return bool(self.voice_client and self.voice_client.is_connected())

    @property
    def is_playing(self) -> bool:
        return bool(self.voice_client and self.voice_client.is_playing())

    @property
    def is_paused(self) -> bool:
        return bool(self.voice_client and self.voice_client.is_paused())

    @property
    def idle_countdown_active(self) -> bool:
        return bool(self._idle_task and not self._idle_task.done())

    @property
    def idle_disconnect_seconds(self) -> float:
        return _IDLE_DISCONNECT_SECONDS

    @property
    def connect_timeout_seconds(self) -> float:
        return _CONNECT_TIMEOUT_SECONDS
