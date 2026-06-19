from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from config import Settings, load_settings
from core.indexer import MusicLibrary, Song
from core.player import EndOfQueuePolicy, GuildAudioPlayer, PlaybackError, RepeatMode


_LOGGER = logging.getLogger(__name__)
_VOTE_SKIP_TIMEOUT = 30  # seconds
_PENDING_CHOICES_TTL = 300.0  # 5 minutes — pending song choices expire after this


def _song_label(song: Song) -> str:
    title = song.display_title.strip()
    if title.lower().startswith("nightcore - "):
        title = title[12:].strip() or song.display_title
    return title


class VoteSkipView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int, needed: int, song_label: str) -> None:
        super().__init__(timeout=_VOTE_SKIP_TIMEOUT)
        self.cog = cog
        self.guild_id = guild_id
        self.needed = needed
        self.song_label = song_label
        self._resolved = False

    async def on_timeout(self) -> None:
        if not self._resolved:
            msg = self.cog._vote_skip_message.get(self.guild_id)
            self.cog._vote_skip_votes.pop(self.guild_id, None)
            self.cog._vote_skip_message.pop(self.guild_id, None)
            for item in self.children:
                item.disabled = True  # type: ignore
            if msg:
                try:
                    await msg.edit(
                        content=f"Vote skip expired — not enough votes to skip **{self.song_label}**.",
                        view=self,
                    )
                except Exception:
                    pass

    @discord.ui.button(label="Vote Skip", style=discord.ButtonStyle.danger, emoji="⏭️")
    async def vote_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        guild_id = self.guild_id
        votes: set[int] = self.cog._vote_skip_votes.get(guild_id, set())

        if interaction.user.id in votes:
            await interaction.response.send_message("You already voted to skip.", ephemeral=True)
            return

        # Must be in the same voice channel
        player = self.cog._get_player(guild_id)
        user_channel = MusicCog._user_voice_channel(interaction)
        if not user_channel or not player.voice_client or user_channel.id != player.voice_client.channel.id:
            await interaction.response.send_message("Join the voice channel to vote.", ephemeral=True)
            return

        votes.add(interaction.user.id)
        self.cog._vote_skip_votes[guild_id] = votes
        current = len(votes)

        # Server owner or configured bot owner always forces an instant skip
        is_owner = await self.cog._is_skip_owner(interaction)
        if is_owner or current >= self.needed:
            self._resolved = True
            self.cog._vote_skip_votes.pop(guild_id, None)
            self.cog._vote_skip_message.pop(guild_id, None)
            for item in self.children:
                item.disabled = True  # type: ignore
            await player.skip()
            asyncio.create_task(self.cog._refresh_now_playing_after_skip(guild_id))
            if is_owner:
                msg = f"Owner override — skipped **{self.song_label}**."
            else:
                msg = f"Vote passed ({current}/{self.needed}) — skipped **{self.song_label}**."
            await interaction.response.edit_message(
                content=f"⏭️ {msg}",
                view=self,
            )
        else:
            remaining = self.needed - current
            await interaction.response.edit_message(
                content=self.cog._msg(
                    "Vote Skip",
                    f"**{self.song_label}**\n{current}/{self.needed} votes — need {remaining} more. ({_VOTE_SKIP_TIMEOUT}s window)",
                ),
                view=self,
            )


class PickSelect(discord.ui.Select):
    def __init__(self, cog: "MusicCog", guild_id: int, songs: list[Song]) -> None:
        self.cog = cog
        self.guild_id = guild_id
        self.songs = songs
        options = [
            discord.SelectOption(label=_song_label(s)[:100], description=(s.artist or s.relative_path)[:100], value=str(i))
            for i, s in enumerate(songs)
        ]
        super().__init__(placeholder="Choose a song...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("This picker is no longer valid.", ephemeral=True)
            return

        index = int(self.values[0])
        song = self.songs[index]
        self.cog.pending_choices[self.guild_id] = self.songs
        self.cog._pending_times[self.guild_id] = time.monotonic()
        if not interaction.response.is_done():
            await interaction.response.defer()

        await self.cog._play_selected_song(interaction, song)

        # Remove the large picker message after selection to keep chat clean.
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass


class PickView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int, songs: list[Song]) -> None:
        super().__init__(timeout=120)
        self.add_item(PickSelect(cog, guild_id, songs))


class NowPlayingCardView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.message_id: int | None = None

    async def on_timeout(self) -> None:
        if self.message_id is None:
            return
        for item in self.children:
            item.disabled = True  # type: ignore
        msg = self.cog._now_playing_messages.get(self.guild_id)
        if msg and msg.id == self.message_id:
            try:
                await msg.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, emoji="⏸️")
    async def pause_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog._get_player(self.guild_id)
        if not player.current_song:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if player.voice_client and player.voice_client.is_paused():
            await interaction.response.send_message("Already paused.", ephemeral=True)
            return
        if await player.pause():
            await interaction.response.send_message("⏸️ Paused", ephemeral=True)
        else:
            await interaction.response.send_message("Could not pause.", ephemeral=True)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.success, emoji="▶️")
    async def resume_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog._get_player(self.guild_id)
        if not player.current_song:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if player.voice_client and player.voice_client.is_playing():
            await interaction.response.send_message("Already playing.", ephemeral=True)
            return
        if await player.resume():
            await interaction.response.send_message("▶️ Resumed", ephemeral=True)
        else:
            await interaction.response.send_message("Could not resume.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog._get_player(self.guild_id)
        if not player.current_song:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if await player.skip():
            asyncio.create_task(self.cog._refresh_now_playing_after_skip(self.guild_id))
            await interaction.response.send_message("⏭️ Skipped", ephemeral=True)
        else:
            await interaction.response.send_message("Could not skip.", ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.primary, emoji="🔀")
    async def shuffle_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog._get_player(self.guild_id)
        count = player.shuffle_queue()
        await interaction.response.send_message(f"🔀 Queue shuffled ({count} songs)", ephemeral=True)

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.secondary, emoji="📋")
    async def queue_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog._get_player(self.guild_id)
        snapshot = player.queue_snapshot()
        if not snapshot:
            await interaction.response.send_message("No upcoming songs.", ephemeral=True)
            return

        per_page = 10
        total_pages = max(1, (len(snapshot) + per_page - 1) // per_page)

        def make_queue_page(page: int) -> str:
            lines = [f"**Queue** — {len(snapshot)} songs"]
            start = page * per_page
            chunk = snapshot[start:start + per_page]
            for idx, queued_song in enumerate(chunk, start=start + 1):
                lines.append(f"{idx}. {_song_label(queued_song)}")
            lines.append(f"\nPage {page + 1}/{total_pages}")
            return "\n".join(lines)

        view = QueuePager(make_queue_page, total_pages)
        view._toggle()
        await interaction.response.send_message(content=make_queue_page(0), view=view, ephemeral=True)


class QueuePager(discord.ui.View):
    def __init__(self, page_factory: Any, total_pages: int) -> None:
        super().__init__(timeout=180)
        self.page = 0
        self.total_pages = total_pages
        self.page_factory = page_factory
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    def _toggle(self) -> None:
        prev_btn: discord.ui.Button = self.children[0]  # type: ignore
        next_btn: discord.ui.Button = self.children[1]  # type: ignore
        prev_btn.disabled = self.page <= 0
        next_btn.disabled = self.page >= self.total_pages - 1

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.page > 0:
            self.page -= 1
        self._toggle()
        await interaction.response.edit_message(content=self.page_factory(self.page), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.page < self.total_pages - 1:
            self.page += 1
        self._toggle()
        await interaction.response.edit_message(content=self.page_factory(self.page), view=self)


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings: Settings = getattr(bot, "settings", None) or load_settings()
        self.library = MusicLibrary(self.settings.music_library_path)
        self.players: dict[int, GuildAudioPlayer] = {}
        self.pending_choices: dict[int, list[Song]] = {}
        self._vote_skip_votes: dict[int, set[int]] = {}
        self._vote_skip_message: dict[int, discord.Message] = {}
        self._now_playing_messages: dict[int, discord.Message] = {}
        self._pending_times: dict[int, float] = {}

    async def cog_load(self) -> None:
        total = self.library.scan()
        _LOGGER.info("Music library indexed: %s song(s) from %s", total, self.settings.music_library_path)

    def _msg(self, title: str, description: str) -> str:
        if description:
            return f"**{title}**\n{description}"
        return f"**{title}**"

    def _build_now_playing_card(self, player: GuildAudioPlayer) -> str | None:
        if not player.current_song:
            return None

        song = player.current_song
        state = "⏸️ Paused" if player.is_paused else "▶️ Playing"
        volume = int(player.volume * 100)
        return (
            "**Now Playing**\n"
            f"Song: **{_song_label(song)}**\n"
            f"Artist: {song.artist or 'Unknown'}\n"
            f"Album: {song.album or 'Unknown'}\n"
            f"Volume: {volume}%\n"
            f"Status: {state}"
        )

    async def _upsert_now_playing_message(self, guild_id: int, player: GuildAudioPlayer) -> None:
        existing = self._now_playing_messages.get(guild_id)
        card = self._build_now_playing_card(player)
        if not card:
            return

        view = NowPlayingCardView(self, guild_id)
        if existing:
            try:
                view.message_id = existing.id
                await existing.edit(content=card, view=view)
                return
            except Exception:
                self._now_playing_messages.pop(guild_id, None)

        if not player.last_text_channel:
            return

        try:
            sent = await player.last_text_channel.send(content=card, view=view)
            view.message_id = sent.id
            self._now_playing_messages[guild_id] = sent
        except Exception:
            _LOGGER.warning("Could not send now-playing message to text channel.", exc_info=True)

    async def _refresh_now_playing_after_skip(self, guild_id: int) -> None:
        # Poll briefly after a skip so the card updates as soon as the next track starts,
        # rather than relying on a fixed delay.
        for _ in range(10):
            await asyncio.sleep(0.2)
            player = self.players.get(guild_id)
            if player and player.current_song:
                await self._upsert_now_playing_message(guild_id, player)
                return

    def _get_player(self, guild_id: int) -> GuildAudioPlayer:
        player = self.players.get(guild_id)
        if player:
            return player

        player = GuildAudioPlayer(
            guild_id=guild_id,
            loop=self.bot.loop,
            ffmpeg_executable=self.settings.ffmpeg_path,
            ffmpeg_before_options=self.settings.ffmpeg_before_options,
            ffmpeg_options=self.settings.ffmpeg_options,
        )
        player.on_playback_error = self._make_error_callback(player)
        player.on_song_start = self._make_song_start_callback(player)
        self.players[guild_id] = player
        return player

    def _make_song_start_callback(self, player: GuildAudioPlayer):
        async def _on_start(song: Song) -> None:
            await self._upsert_now_playing_message(player.guild_id, player)
        return _on_start

    def _make_error_callback(self, player: GuildAudioPlayer):
        async def _on_error(song: Song, error: Exception) -> None:
            if isinstance(error, FileNotFoundError):
                msg = f"Could not play **{_song_label(song)}**. File not found."
            else:
                msg = f"Playback failed for **{_song_label(song)}**. Check logs."

            if player.last_text_channel:
                try:
                    await player.last_text_channel.send(content=self._msg("Playback Error", msg))
                except Exception:
                    _LOGGER.warning("Could not send playback error to text channel.", exc_info=True)
        return _on_error

    @staticmethod
    def _user_voice_channel(interaction: discord.Interaction) -> discord.VoiceChannel | None:
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return None
        if not interaction.user.voice or not interaction.user.voice.channel:
            return None
        if not isinstance(interaction.user.voice.channel, discord.VoiceChannel):
            return None
        return interaction.user.voice.channel

    @staticmethod
    def _has_human_listeners(voice_client: discord.VoiceClient | None) -> bool:
        if not voice_client or not voice_client.channel:
            return False
        return any(not member.bot for member in voice_client.channel.members)

    async def _cleanup_if_empty(self, player: GuildAudioPlayer) -> bool:
        if not player.voice_client:
            return False
        if self._has_human_listeners(player.voice_client):
            return False

        player.begin_idle_countdown()
        return True

    def _control_error_message(self, interaction: discord.Interaction, player: GuildAudioPlayer) -> str | None:
        voice_client = player.voice_client
        if not voice_client or not voice_client.channel:
            return "Bot is not connected to a voice channel. Use /play first."

        user_channel = self._user_voice_channel(interaction)
        if not user_channel or user_channel.id != voice_client.channel.id:
            return "Join the same voice channel as the bot to control playback."

        return None

    async def _is_skip_owner(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        if self.settings.bot_owner_id is not None and interaction.user.id == self.settings.bot_owner_id:
            return True
        try:
            return await self.bot.is_owner(interaction.user)
        except Exception:
            return False

    async def _play_selected_song(self, interaction: discord.Interaction, song: Song) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        voice_channel = self._user_voice_channel(interaction)
        if not voice_channel:
            await interaction.response.send_message("Join a voice channel first, then run /play.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        if isinstance(interaction.channel, discord.TextChannel):
            player.last_text_channel = interaction.channel

        if not interaction.response.is_done():
            await interaction.response.defer()

        try:
            await player.connect(voice_channel)
            _prev_callback = player.on_song_start
            player.on_song_start = None
            now_playing = await player.enqueue(song)
            player.on_song_start = _prev_callback
        except PlaybackError as exc:
            await interaction.followup.send(content=self._msg("Playback Error", str(exc)), ephemeral=True)
            return
        except discord.ClientException as exc:
            await interaction.followup.send(content=self._msg("Voice Error", f"{exc}"), ephemeral=True)
            return

        if now_playing:
            card = self._build_now_playing_card(player)
            if card:
                view = NowPlayingCardView(self, interaction.guild.id)
                sent = await interaction.followup.send(content=card, view=view, wait=True)
                view.message_id = sent.id
                self._now_playing_messages[interaction.guild.id] = sent
                return

        status = f"Queued at position {player.queue_length}"
        desc = f"**{_song_label(song)}**"
        await interaction.followup.send(content=self._msg(status, desc))

    async def _play_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not current.strip():
            return []
        matches = self.library.search(current, limit=20)
        seen: set[str] = set()
        choices: list[app_commands.Choice[str]] = []
        for song in matches:
            name = _song_label(song)[:100]
            value = song.display_title[:100]
            if value in seen:
                continue
            seen.add(value)
            choices.append(app_commands.Choice(name=name, value=value))
            if len(choices) >= 20:
                break
        return choices

    async def _album_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        needle = current.strip().lower()
        albums = sorted({s.album for s in self.library.songs if s.album})
        filtered = [a for a in albums if needle in a.lower()][:20]
        return [app_commands.Choice(name=a[:100], value=a[:100]) for a in filtered]

    async def _playlist_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        needle = current.strip().lower()
        folders = [f for f in self.library.list_folders() if needle in f.lower()][:20]
        return [app_commands.Choice(name=f[:100], value=f[:100]) for f in folders]

    @app_commands.command(name="help", description="Show music bot command guide")
    async def help(self, interaction: discord.Interaction) -> None:
        lines = [
            "**Playback**: /play /playnext /pick /pause /resume /skip /skipto /stop /nowplaying /queue /shuffle /clearqueue /remove /volume /loop /endpolicy",
            "**Library**: /playall /album /search /rescan",
            "**Diagnostics**: /diag",
            "**Admin**: /reload /clearhistory (owner only)",
            "Tips: /play supports autocomplete. If multiple songs match, use the dropdown or /pick <number>.",
        ]
        await interaction.response.send_message(content=self._msg("Music Bot Help", "\n".join(lines)), ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @app_commands.command(name="reload", description="Hot-reload music commands without restarting the bot")
    async def reload(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        if not await self._is_skip_owner(interaction):
            await interaction.response.send_message("Only the server/bot owner can use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.reload_extension("cogs.music")
            synced = await self.bot.tree.sync()
            await interaction.followup.send(
                content=self._msg("Reloaded", f"Music cog reloaded. Synced {len(synced)} global command(s)."),
                ephemeral=True,
            )
        except Exception as exc:
            _LOGGER.exception("Hot reload failed")
            await interaction.followup.send(
                content=self._msg("Reload Failed", f"{type(exc).__name__}: {exc}"),
                ephemeral=True,
            )

    @app_commands.command(name="play", description="Play a song from your local music library")
    @app_commands.describe(song="Song title or filename to search")
    @app_commands.autocomplete(song=_play_autocomplete)
    async def play(self, interaction: discord.Interaction, song: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        selected_song, candidates = self.library.resolve_play_query(song, limit=25)
        if not selected_song and not candidates:
            await interaction.response.send_message(content=self._msg("No Match", f"No song found for '{song}'."), ephemeral=True)
            return

        if not selected_song and candidates:
            self.pending_choices[interaction.guild.id] = candidates
            self._pending_times[interaction.guild.id] = time.monotonic()
            lines = [f"Multiple matches for **{song}**. Pick from the menu below or run /pick <number>."]
            if len(candidates) >= 25:
                lines.append("Showing first 25 matches. Use a more specific query for better results.")
            for i, item in enumerate(candidates, start=1):
                lines.append(f"{i}. {_song_label(item)}")
            view = PickView(self, interaction.guild.id, candidates)
            await interaction.response.send_message(content=self._msg("Choose a Song", "\n".join(lines)), view=view, ephemeral=True)
            return

        if selected_song is None:
            await interaction.response.send_message("Could not resolve a unique song for this request.", ephemeral=True)
            return

        await self._play_selected_song(interaction, selected_song)

    @app_commands.command(name="pick", description="Choose a song from the last ambiguous /play result")
    @app_commands.describe(number="Result number from the last /play suggestions")
    async def pick(self, interaction: discord.Interaction, number: int) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        choices = self.pending_choices.get(interaction.guild.id)
        stored_at = self._pending_times.get(interaction.guild.id, 0.0)
        if not choices or (time.monotonic() - stored_at) > _PENDING_CHOICES_TTL:
            self.pending_choices.pop(interaction.guild.id, None)
            self._pending_times.pop(interaction.guild.id, None)
            await interaction.response.send_message("No pending choices (or they expired). Run /play first.", ephemeral=True)
            return

        if number < 1 or number > len(choices):
            await interaction.response.send_message(
                f"Please choose a number between 1 and {len(choices)}.",
                ephemeral=True,
            )
            return

        song = choices[number - 1]
        del self.pending_choices[interaction.guild.id]
        self._pending_times.pop(interaction.guild.id, None)
        await self._play_selected_song(interaction, song)

    @app_commands.command(name="playnext", description="Queue a song to play immediately after current track")
    @app_commands.describe(song="Song title or filename to search")
    @app_commands.autocomplete(song=_play_autocomplete)
    async def playnext(self, interaction: discord.Interaction, song: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        voice_channel = self._user_voice_channel(interaction)
        if not voice_channel:
            await interaction.response.send_message("Join a voice channel first, then run /playnext.", ephemeral=True)
            return

        selected_song, candidates = self.library.resolve_play_query(song, limit=10)
        if not selected_song and candidates:
            lines = [f"Multiple matches for **{song}**. Use a more specific query:"]
            for i, item in enumerate(candidates, start=1):
                lines.append(f"{i}. {_song_label(item)}")
            await interaction.response.send_message(content=self._msg("Choose a Song", "\n".join(lines)), ephemeral=True)
            return
        if selected_song is None:
            await interaction.response.send_message(content=self._msg("No Match", f"No song found for '{song}'."), ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        if isinstance(interaction.channel, discord.TextChannel):
            player.last_text_channel = interaction.channel

        try:
            await player.connect(voice_channel)
        except PlaybackError as exc:
            await interaction.response.send_message(content=self._msg("Playback Error", str(exc)), ephemeral=True)
            return
        except discord.ClientException as exc:
            await interaction.response.send_message(content=self._msg("Voice Error", f"{exc}"), ephemeral=True)
            return

        _prev_callback = player.on_song_start
        player.on_song_start = None
        now_playing = await player.enqueue_next(selected_song)
        player.on_song_start = _prev_callback

        if now_playing:
            card = self._build_now_playing_card(player)
            if card:
                view = NowPlayingCardView(self, interaction.guild.id)
                await interaction.response.send_message(content=card, view=view)
                try:
                    sent = await interaction.original_response()
                    view.message_id = sent.id
                    self._now_playing_messages[interaction.guild.id] = sent
                except Exception:
                    pass
                return

        await interaction.response.send_message(
            f"⏭️ Queued next: **{_song_label(selected_song)}**", ephemeral=True
        )

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        if isinstance(interaction.channel, discord.TextChannel):
            player.last_text_channel = interaction.channel
        control_error = self._control_error_message(interaction, player)
        if control_error:
            await interaction.response.send_message(control_error, ephemeral=True)
            return

        if await self._cleanup_if_empty(player):
            await interaction.response.send_message(
                "No human listeners in voice. I will disconnect after 10 minutes of inactivity.",
                ephemeral=True,
            )
            return

        paused = await player.pause()
        if not paused:
            await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
            return

        await interaction.response.send_message("⏸️ Paused.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume paused playback")
    async def resume(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        if isinstance(interaction.channel, discord.TextChannel):
            player.last_text_channel = interaction.channel
        control_error = self._control_error_message(interaction, player)
        if control_error:
            await interaction.response.send_message(control_error, ephemeral=True)
            return

        if await self._cleanup_if_empty(player):
            await interaction.response.send_message(
                "No human listeners in voice. I will disconnect after 10 minutes of inactivity.",
                ephemeral=True,
            )
            return

        resumed = await player.resume()
        if not resumed:
            await interaction.response.send_message("Nothing is paused right now.", ephemeral=True)
            return

        await interaction.response.send_message("▶️ Resumed.", ephemeral=True)

    @app_commands.command(name="nowplaying", description="Show the currently playing song with quick controls")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        if not player.current_song:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return

        card = self._build_now_playing_card(player)
        if not card:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return

        view = NowPlayingCardView(self, interaction.guild.id)
        await interaction.response.send_message(content=card, view=view)
        try:
            sent = await interaction.original_response()
            view.message_id = sent.id
            self._now_playing_messages[interaction.guild.id] = sent
        except Exception:
            pass

    @app_commands.command(name="queue", description="Show the upcoming song queue")
    async def queue(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        snapshot = player.queue_snapshot()

        if not player.current_song and not snapshot:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return

        per_page = 10
        total_pages = max(1, (len(snapshot) + per_page - 1) // per_page)

        def make_page(page: int) -> str:
            start = page * per_page
            end = start + per_page
            chunk = snapshot[start:end]
            lines: list[str] = []

            if page == 0 and player.current_song:
                state = "Paused" if player.is_paused else "Playing"
                lines.append(f"**[{state}]** {_song_label(player.current_song)}")
                lines.append("")

            if not chunk:
                lines.append("No upcoming songs.")
            else:
                for idx, song in enumerate(chunk, start=start + 1):
                    lines.append(f"{idx}. {_song_label(song)}")

            lines.append(f"\nPage {page + 1}/{total_pages}")
            return self._msg("Queue", "\n".join(lines))

        view = QueuePager(make_page, total_pages)
        view._toggle()
        await interaction.response.send_message(content=make_page(0), view=view)
        try:
            view.message = await interaction.original_response()
        except Exception:
            pass

    @app_commands.command(name="shuffle", description="Shuffle the current queue")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        control_error = self._control_error_message(interaction, player)
        if control_error:
            await interaction.response.send_message(control_error, ephemeral=True)
            return

        count = player.shuffle_queue()
        if count == 0:
            await interaction.response.send_message("Nothing in queue to shuffle.", ephemeral=True)
            return

        await interaction.response.send_message(f"🔀 Shuffled {count} song(s).", ephemeral=True)

    @app_commands.command(name="clearqueue", description="Clear all upcoming songs in the queue")
    async def clearqueue(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        control_error = self._control_error_message(interaction, player)
        if control_error:
            await interaction.response.send_message(control_error, ephemeral=True)
            return

        cleared = player.clear_queue()
        if cleared == 0:
            await interaction.response.send_message("Queue is already empty.", ephemeral=True)
            return

        await interaction.response.send_message(f"🗑️ Cleared {cleared} upcoming song(s).", ephemeral=True)

    @app_commands.command(name="remove", description="Remove one queued song by number")
    @app_commands.describe(number="Queue position number to remove")
    async def remove(self, interaction: discord.Interaction, number: int) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        control_error = self._control_error_message(interaction, player)
        if control_error:
            await interaction.response.send_message(control_error, ephemeral=True)
            return

        removed = player.remove_from_queue(number)
        if not removed:
            await interaction.response.send_message(f"Queue position {number} does not exist.", ephemeral=True)
            return

        await interaction.response.send_message(f"Removed **{_song_label(removed)}** from the queue.", ephemeral=True)

    @app_commands.command(name="loop", description="Set repeat mode for playback")
    @app_commands.describe(mode="Repeat mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Track", value="track"),
        app_commands.Choice(name="Queue", value="queue"),
    ])
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        player = self._get_player(interaction.guild.id)
        current = player.set_repeat_mode(cast(RepeatMode, mode.value))
        await interaction.response.send_message(f"🔁 Repeat set to **{current}**.", ephemeral=True)

    @app_commands.command(name="endpolicy", description="Set behavior when queue reaches the end")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(mode="What to do after the last song")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Announce + Wait", value="announce_wait"),
        app_commands.Choice(name="Silent Wait", value="silent_wait"),
        app_commands.Choice(name="Leave Immediately", value="leave_now"),
    ])
    async def endpolicy(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        player = self._get_player(interaction.guild.id)
        current = player.set_end_of_queue_policy(cast(EndOfQueuePolicy, mode.value))
        await interaction.response.send_message(f"End-of-queue set to **{current}**.", ephemeral=True)

    @app_commands.default_permissions(manage_guild=True)
    @app_commands.command(name="clearhistory", description="Delete recent bot messages in this channel")
    @app_commands.describe(amount="How many recent bot messages to delete (1-100)")
    async def clearhistory(self, interaction: discord.Interaction, amount: int = 25) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This command can only be used in a text channel.", ephemeral=True)
            return
        if amount < 1 or amount > 100:
            await interaction.response.send_message("Please provide a number between 1 and 100.", ephemeral=True)
            return
        if not await self._is_skip_owner(interaction):
            await interaction.response.send_message("Only the server/bot owner can use this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        bot_user = self.bot.user
        if bot_user is None:
            await interaction.followup.send("Bot user is not available yet. Try again in a moment.", ephemeral=True)
            return

        scanned_limit = min(1000, max(100, amount * 12))
        cutoff = discord.utils.utcnow() - timedelta(days=14)
        deleted = 0
        scanned = 0

        async for msg in interaction.channel.history(limit=scanned_limit):
            scanned += 1
            if msg.author.id != bot_user.id:
                continue
            if msg.created_at < cutoff:
                continue
            try:
                await msg.delete()
                deleted += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
            if deleted >= amount:
                break

        await interaction.followup.send(
            content=self._msg(
                "History Cleared",
                f"Deleted **{deleted}** bot message(s) from this channel. Scanned {scanned} recent message(s).",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="volume", description="Show or set playback volume for this server")
    @app_commands.describe(percent="Volume percent (5-150). Leave empty to view current volume")
    async def volume(self, interaction: discord.Interaction, percent: int | None = None) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)

        if percent is None:
            scope = "All upcoming tracks" if not player.is_playing and not player.is_paused else "Next track"
            await interaction.response.send_message(
                content=self._msg(
                    "Volume",
                    f"Current server volume: **{int(player.volume * 100)}%**\nApplies to: **{scope}**",
                ),
                ephemeral=True,
            )
            return

        if percent < 5 or percent > 150:
            await interaction.response.send_message("Please provide a volume between 5 and 150.", ephemeral=True)
            return

        new_volume = player.set_volume(percent / 100.0)
        quality_note = ""
        if percent > 100:
            quality_note = "\nNote: above 100% can introduce clipping on some tracks."

        apply_note = ""
        if player.is_playing or player.is_paused:
            apply_note = "\nApplies on the next started track (quality-first Opus mode)."

        await interaction.response.send_message(
            content=self._msg(
                "Volume Updated",
                f"Server playback volume is now **{int(new_volume * 100)}%**.{apply_note}{quality_note}",
            )
        )

    @app_commands.command(name="playall", description="Queue all songs from a folder in your music library")
    @app_commands.describe(folder="Folder name inside your music library")
    @app_commands.autocomplete(folder=_playlist_autocomplete)
    async def playlist(self, interaction: discord.Interaction, folder: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        voice_channel = self._user_voice_channel(interaction)
        if not voice_channel:
            await interaction.response.send_message("Join a voice channel first, then run /playall.", ephemeral=True)
            return

        resolved_folder, folder_candidates = self.library.resolve_folder_query(folder)
        songs = self.library.songs_in_folder(resolved_folder or folder)
        if not songs:
            available = self.library.list_folders()
            if folder_candidates:
                suggestion_list = "\n".join(f"- {f}" for f in folder_candidates)
                await interaction.response.send_message(
                    content=self._msg(
                        "Folder Not Found",
                        f"Could not uniquely match '{folder}'. Did you mean:\n{suggestion_list}",
                    ),
                    ephemeral=True,
                )
                return
            if available:
                folder_list = "\n".join(f"- {f}" for f in available)
                await interaction.response.send_message(
                    content=self._msg("Folder Not Found", f"No folder named '{folder}'.\n{folder_list}"),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "No subfolders found in your music library.",
                    ephemeral=True,
                )
            return

        player = self._get_player(interaction.guild.id)
        if isinstance(interaction.channel, discord.TextChannel):
            player.last_text_channel = interaction.channel

        await interaction.response.defer()

        try:
            await player.connect(voice_channel)
        except PlaybackError as exc:
            await interaction.followup.send(content=self._msg("Playback Error", str(exc)), ephemeral=True)
            return
        except discord.ClientException as exc:
            await interaction.followup.send(content=self._msg("Voice Error", str(exc)), ephemeral=True)
            return

        first_started = False
        _prev_callback = player.on_song_start
        player.on_song_start = None
        for song in songs:
            started = await player.enqueue(song)
            if started and not first_started:
                first_started = True
        player.on_song_start = _prev_callback

        if first_started:
            card = self._build_now_playing_card(player)
            if card:
                card_view = NowPlayingCardView(self, interaction.guild.id)
                sent = await interaction.followup.send(content=card, view=card_view, wait=True)
                card_view.message_id = sent.id
                self._now_playing_messages[interaction.guild.id] = sent

        status = "Now Playing" if first_started else "Queued"
        per_page = 10
        total_pages = max(1, (len(songs) + per_page - 1) // per_page)

        def make_playlist_page(page: int) -> str:
            start = page * per_page
            chunk = songs[start:start + per_page]
            lines = [f"**{resolved_folder or folder}** — {len(songs)} song(s)\n"]
            for idx, s in enumerate(chunk, start=start + 1):
                prefix = "▶" if idx == 1 and first_started else f"{idx}."
                lines.append(f"{prefix} {_song_label(s)}")
            lines.append(f"\nPage {page + 1}/{total_pages}")
            return self._msg(status, "\n".join(lines))

        view = QueuePager(make_playlist_page, total_pages)
        view._toggle()
        sent_page = await interaction.followup.send(content=make_playlist_page(0), view=view, wait=True)
        view.message = sent_page

    @app_commands.command(name="skip", description="Skip the current song (owner: instant, others: vote)")
    async def skip(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        if isinstance(interaction.channel, discord.TextChannel):
            player.last_text_channel = interaction.channel
        control_error = self._control_error_message(interaction, player)
        if control_error:
            await interaction.response.send_message(control_error, ephemeral=True)
            return

        if await self._cleanup_if_empty(player):
            await interaction.response.send_message(
                "No human listeners in voice. I will disconnect after 10 minutes of inactivity.",
                ephemeral=True,
            )
            return

        if not player.current_song:
            await interaction.response.send_message("Nothing to skip.", ephemeral=True)
            return

        # Server owner or configured bot owner skips instantly
        is_owner = await self._is_skip_owner(interaction)
        if is_owner:
            song_label = _song_label(player.current_song)
            await player.skip()
            asyncio.create_task(self._refresh_now_playing_after_skip(interaction.guild.id))
            await interaction.response.send_message(f"⏭️ Skipped **{song_label}**.")
            return

        # Everyone else: vote-skip
        guild_id = interaction.guild.id
        voice_channel = player.voice_client.channel if player.voice_client else None
        if not voice_channel:
            await interaction.response.send_message("Bot is not in a voice channel.", ephemeral=True)
            return

        listeners = [m for m in voice_channel.members if not m.bot]
        needed = max(1, (len(listeners) // 2) + 1)

        # If a vote is already active, add this user's vote
        if guild_id in self._vote_skip_votes:
            votes = self._vote_skip_votes[guild_id]
            if interaction.user.id in votes:
                await interaction.response.send_message("You already voted to skip.", ephemeral=True)
                return
            votes.add(interaction.user.id)
            current = len(votes)
            # Vote threshold check
            if current >= needed:
                self._vote_skip_votes.pop(guild_id, None)
                self._vote_skip_message.pop(guild_id, None)
                song_label = _song_label(player.current_song)
                await player.skip()
                asyncio.create_task(self._refresh_now_playing_after_skip(interaction.guild.id))
                await interaction.response.send_message(
                    f"⏭️ Vote passed ({current}/{needed}) — skipped **{song_label}**."
                )
            else:
                remaining = needed - current
                song_label = _song_label(player.current_song)
                await interaction.response.send_message(
                    f"**{song_label}** — {current}/{needed} votes, need {remaining} more.",
                    ephemeral=True,
                )
            return

        # Start a new vote
        self._vote_skip_votes[guild_id] = {interaction.user.id}
        song_label = _song_label(player.current_song)
        view = VoteSkipView(self, guild_id, needed, song_label)
        await interaction.response.send_message(
            content=self._msg(
                "Vote Skip",
                f"**{song_label}**\n1/{needed} votes — need {needed - 1} more. ({_VOTE_SKIP_TIMEOUT}s window)",
            ),
            view=view,
        )
        msg = await interaction.original_response()
        self._vote_skip_message[guild_id] = msg

    @app_commands.command(name="skipto", description="Jump to a specific song in the queue by number or name")
    @app_commands.describe(song="Queue position number (e.g. 3) or part of a song title")
    async def skipto(self, interaction: discord.Interaction, song: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        control_error = self._control_error_message(interaction, player)
        if control_error:
            await interaction.response.send_message(control_error, ephemeral=True)
            return

        snapshot = player.queue_snapshot()
        if not snapshot:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return

        # Resolve by number first, then by title match
        target_index: int | None = None
        try:
            n = int(song.strip())
            if 1 <= n <= len(snapshot):
                target_index = n
            else:
                await interaction.response.send_message(
                    f"Number out of range. Queue has {len(snapshot)} song(s).", ephemeral=True
                )
                return
        except ValueError:
            needle = song.strip().lower()
            for i, s in enumerate(snapshot, start=1):
                if needle in s.display_title.lower() or (s.artist and needle in s.artist.lower()):
                    target_index = i
                    break
            if target_index is None:
                await interaction.response.send_message(
                    f"No song in the queue matching **{song}**.", ephemeral=True
                )
                return

        target_song = player.skipto(target_index)
        if not target_song:
            await interaction.response.send_message("Could not find that position in the queue.", ephemeral=True)
            return

        # Stop current song — player will automatically start the target next
        await player.skip()
        asyncio.create_task(self._refresh_now_playing_after_skip(interaction.guild.id))
        await interaction.response.send_message(f"⏭️ Skipping to **{_song_label(target_song)}**.")

    @app_commands.command(name="stop", description="Stop playback and leave the voice channel")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        if isinstance(interaction.channel, discord.TextChannel):
            player.last_text_channel = interaction.channel
        control_error = self._control_error_message(interaction, player)
        if control_error:
            await interaction.response.send_message(control_error, ephemeral=True)
            return

        await player.stop(disconnect=True)
        await interaction.response.send_message("⏹️ Stopped playback and disconnected.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if self.bot.user and member.id == self.bot.user.id:
            if before.channel is not None and after.channel is None:
                player = self.players.get(member.guild.id)
                if player:
                    if player.consume_expected_disconnect():
                        player.handle_disconnect()
                        return
                    _LOGGER.warning(
                        "Guild %s: bot was force-disconnected from voice channel '%s'.",
                        member.guild.id,
                        before.channel.name,
                    )
                    player.handle_disconnect()
                    if player.last_text_channel:
                        try:
                            await player.last_text_channel.send(
                                content=self._msg("Disconnected", "I was disconnected from voice. Use /play to resume.")
                            )
                        except Exception:
                            pass
            return

        if member.bot:
            return

        guild = member.guild
        if not guild:
            return

        player = self.players.get(guild.id)
        if not player or not player.voice_client:
            return

        bot_channel = player.voice_client.channel
        if not bot_channel:
            return

        user_left_bot_channel = before.channel and before.channel.id == bot_channel.id
        user_joined_bot_channel = after.channel and after.channel.id == bot_channel.id
        if not user_left_bot_channel and not user_joined_bot_channel:
            return

        if not self._has_human_listeners(player.voice_client):
            started = player.begin_idle_countdown()
            if started:
                _LOGGER.info("No human listeners in guild %s. Starting idle disconnect countdown.", guild.id)
                if player.last_text_channel:
                    try:
                        mins = int(player.idle_disconnect_seconds // 60)
                        await player.last_text_channel.send(
                            content=self._msg("Idle Countdown", f"Voice channel is empty. Disconnecting in {mins} minutes if still idle.")
                        )
                    except Exception:
                        pass
        else:
            player.cancel_idle_countdown()

    @app_commands.command(name="rescan", description="Rescan the music library for new or removed files")
    async def rescan(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        total = self.library.scan()
        self.pending_choices.clear()
        self._pending_times.clear()
        dupes = len(self.library.duplicates)
        dupe_note = f"{dupes} duplicate title(s) detected" if dupes else "No duplicates detected"
        await interaction.followup.send(
            content=self._msg("Library Rescanned", f"{total} song(s) indexed. {dupe_note}."),
            ephemeral=True,
        )
        _LOGGER.info("Manual rescan completed: %s songs, %s duplicates.", total, dupes)

    @app_commands.command(name="search", description="Search the library by title, artist, or album")
    @app_commands.describe(text="What to search for (title, artist, or album name)")
    async def search(self, interaction: discord.Interaction, text: str) -> None:
        results = self.library.search(text, limit=10)
        if not results:
            await interaction.response.send_message(content=self._msg("Search", f"No results for '{text}'."), ephemeral=True)
            return

        lines = []
        for i, song in enumerate(results, 1):
            parts = [_song_label(song)]
            if song.album:
                parts.append(f"album: {song.album}")
            lines.append(f"{i}. " + " | ".join(parts))
        await interaction.response.send_message(content=self._msg("Search Results", "\n".join(lines)), ephemeral=True)

    @app_commands.command(name="album", description="Queue all songs from an album")
    @app_commands.describe(album="Album name to queue")
    @app_commands.autocomplete(album=_album_autocomplete)
    async def album(self, interaction: discord.Interaction, album: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        voice_channel = self._user_voice_channel(interaction)
        if not voice_channel:
            await interaction.response.send_message("Join a voice channel first, then run /album.", ephemeral=True)
            return

        songs = self.library.search_by_album(album)
        if not songs:
            await interaction.response.send_message(
                content=self._msg("Album Not Found", f"No album matching '{album}'."),
                ephemeral=True,
            )
            return

        player = self._get_player(interaction.guild.id)
        if isinstance(interaction.channel, discord.TextChannel):
            player.last_text_channel = interaction.channel

        await interaction.response.defer()

        try:
            await player.connect(voice_channel)
        except PlaybackError as exc:
            await interaction.followup.send(content=self._msg("Playback Error", str(exc)), ephemeral=True)
            return
        except discord.ClientException as exc:
            await interaction.followup.send(content=self._msg("Voice Error", str(exc)), ephemeral=True)
            return

        first_started = False
        _prev_callback = player.on_song_start
        player.on_song_start = None
        for song in songs:
            started = await player.enqueue(song)
            if started and not first_started:
                first_started = True
        player.on_song_start = _prev_callback

        if first_started:
            card = self._build_now_playing_card(player)
            if card:
                card_view = NowPlayingCardView(self, interaction.guild.id)
                sent = await interaction.followup.send(content=card, view=card_view, wait=True)
                card_view.message_id = sent.id
                self._now_playing_messages[interaction.guild.id] = sent

        album_display = songs[0].album or album
        artist_display = f" by {songs[0].artist}" if songs[0].artist else ""
        status = "Now Playing" if first_started else "Queued"
        await interaction.followup.send(
            content=self._msg(status, f"{album_display}{artist_display} — {len(songs)} song(s)")
        )

    @app_commands.default_permissions(manage_guild=True)
    @app_commands.command(name="diag", description="Show bot diagnostics")
    async def diag(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        lines = [
            f"Connected: {player.is_connected}",
            f"Playing: {player.is_playing}",
            f"Paused: {player.is_paused}",
            f"Queue length: {player.queue_length}",
            f"Repeat mode: {player.repeat_mode}",
            f"End policy: {player.end_of_queue_policy}",
            f"Volume: {int(player.volume * 100)}%",
            f"Track elapsed: {player.playback_elapsed_seconds}s",
            f"Idle countdown active: {player.idle_countdown_active}",
            f"Idle timeout: {int(player.idle_disconnect_seconds)}s",
            f"Connect timeout: {int(player.connect_timeout_seconds)}s",
            f"FFmpeg: {self.settings.ffmpeg_path}",
            f"FFmpeg before options: {self.settings.ffmpeg_before_options}",
            f"FFmpeg options: {self.settings.ffmpeg_options}",
        ]
        await interaction.response.send_message(content=self._msg("Diagnostics", "\n".join(lines)), ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        _LOGGER.exception("Slash command failed: %s", error)
        message = "Playback command failed. Check bot logs for details."

        if interaction.response.is_done():
            await interaction.followup.send(content=self._msg("Error", message), ephemeral=True)
            return

        await interaction.response.send_message(content=self._msg("Error", message), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
