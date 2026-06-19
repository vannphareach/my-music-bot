from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from config import Settings, load_settings
from core.indexer import MusicLibrary, Song
from core.player import GuildAudioPlayer, PlaybackError


_LOGGER = logging.getLogger(__name__)
_VOTE_SKIP_TIMEOUT = 30  # seconds


def _song_label(song: Song) -> str:
    if song.artist:
        return f"{song.display_title} — {song.artist}"
    return song.display_title


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
                        embed=self.cog._embed("Vote Skip Expired", f"Not enough votes to skip **{self.song_label}**.", 0xED4245),
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
            if is_owner:
                msg = f"Owner override — skipped **{self.song_label}**."
            else:
                msg = f"Vote passed ({current}/{self.needed}) — skipped **{self.song_label}**."
            await interaction.response.edit_message(
                embed=self.cog._embed("Skipped", msg, 0x57F287),
                view=self,
            )
        else:
            remaining = self.needed - current
            await interaction.response.edit_message(
                embed=self.cog._embed(
                    "Vote Skip",
                    f"**{self.song_label}**\n{current}/{self.needed} votes — need {remaining} more. ({_VOTE_SKIP_TIMEOUT}s window)",
                    0xFEE75C,
                ),
                view=self,
            )


class PickSelect(discord.ui.Select):
    def __init__(self, cog: "MusicCog", guild_id: int, songs: list[Song]) -> None:
        self.cog = cog
        self.guild_id = guild_id
        self.songs = songs
        options = [
            discord.SelectOption(label=s.display_title[:100], description=(s.artist or s.relative_path)[:100], value=str(i))
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
        await self.cog._play_selected_song(interaction, song)


class PickView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int, songs: list[Song]) -> None:
        super().__init__(timeout=120)
        self.add_item(PickSelect(cog, guild_id, songs))


class QueuePager(discord.ui.View):
    def __init__(self, embed_factory: Any, total_pages: int) -> None:
        super().__init__(timeout=180)
        self.page = 0
        self.total_pages = total_pages
        self.embed_factory = embed_factory

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
        await interaction.response.edit_message(embed=self.embed_factory(self.page), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.page < self.total_pages - 1:
            self.page += 1
        self._toggle()
        await interaction.response.edit_message(embed=self.embed_factory(self.page), view=self)


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings: Settings = getattr(bot, "settings", None) or load_settings()
        self.library = MusicLibrary(self.settings.music_library_path)
        self.players: dict[int, GuildAudioPlayer] = {}
        self.pending_choices: dict[int, list[Song]] = {}
        self._vote_skip_votes: dict[int, set[int]] = {}
        self._vote_skip_message: dict[int, discord.Message] = {}

    async def cog_load(self) -> None:
        total = self.library.scan()
        _LOGGER.info("Music library indexed: %s song(s) from %s", total, self.settings.music_library_path)

    def _embed(self, title: str, description: str, color: int = 0x2F3136) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=color)

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
            if player.last_text_channel:
                try:
                    queue_len = player.queue_length
                    desc = f"**{_song_label(song)}**\nQueue: {queue_len} song(s)"
                    await player.last_text_channel.send(embed=self._embed("Now Playing", desc, 0x57F287))
                except Exception:
                    _LOGGER.warning("Could not send now-playing message to text channel.", exc_info=True)
        return _on_start

    def _make_error_callback(self, player: GuildAudioPlayer):
        async def _on_error(song: Song, error: Exception) -> None:
            if isinstance(error, FileNotFoundError):
                msg = f"Could not play **{_song_label(song)}**. File not found."
            else:
                msg = f"Playback failed for **{_song_label(song)}**. Check logs."

            if player.last_text_channel:
                try:
                    await player.last_text_channel.send(embed=self._embed("Playback Error", msg, 0xED4245))
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
            await interaction.followup.send(embed=self._embed("Playback Error", str(exc), 0xED4245), ephemeral=True)
            return
        except discord.ClientException as exc:
            await interaction.followup.send(embed=self._embed("Voice Error", f"{exc}", 0xED4245), ephemeral=True)
            return

        status = "Now Playing" if now_playing else f"Queued at position {player.queue_length}"
        desc = f"**{_song_label(song)}**"
        await interaction.followup.send(embed=self._embed(status, desc, 0x57F287))

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
            "**Playback**: /play /pick /pause /resume /skip /stop /nowplaying /queue /shuffle",
            "**Library**: /playlist /album /search /rescan",
            "**Diagnostics**: /diag",
            "Tips: /play supports autocomplete. If multiple songs match, use the dropdown or /pick <number>.",
        ]
        await interaction.response.send_message(embed=self._embed("Music Bot Help", "\n".join(lines), 0x5865F2), ephemeral=True)

    @app_commands.command(name="play", description="Play a song from your local music library")
    @app_commands.describe(song="Song title or filename to search")
    @app_commands.autocomplete(song=_play_autocomplete)
    async def play(self, interaction: discord.Interaction, song: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        selected_song, candidates = self.library.resolve_play_query(song, limit=8)
        if not selected_song and not candidates:
            await interaction.response.send_message(embed=self._embed("No Match", f"No song found for '{song}'.", 0xED4245), ephemeral=True)
            return

        if not selected_song and candidates:
            self.pending_choices[interaction.guild.id] = candidates
            lines = [f"Multiple matches for **{song}**. Pick from the menu below or run /pick <number>."]
            for i, item in enumerate(candidates, start=1):
                lines.append(f"{i}. {_song_label(item)}")
            view = PickView(self, interaction.guild.id, candidates)
            await interaction.response.send_message(embed=self._embed("Choose a Song", "\n".join(lines), 0xFEE75C), view=view, ephemeral=True)
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
        if not choices:
            await interaction.response.send_message("No pending choices. Run /play first.", ephemeral=True)
            return

        if number < 1 or number > len(choices):
            await interaction.response.send_message(
                f"Please choose a number between 1 and {len(choices)}.",
                ephemeral=True,
            )
            return

        song = choices[number - 1]
        del self.pending_choices[interaction.guild.id]
        await self._play_selected_song(interaction, song)

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

        await interaction.response.send_message(embed=self._embed("Paused", "Playback paused.", 0xFEE75C))

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

        await interaction.response.send_message(embed=self._embed("Resumed", "Playback resumed.", 0x57F287))

    @app_commands.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        player = self._get_player(interaction.guild.id)
        if not player.current_song:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return

        song = player.current_song
        state = "Paused" if player.is_paused else "Playing"
        fields = [
            f"**Title**: {song.display_title}",
            f"**Artist**: {song.artist or 'Unknown'}",
            f"**Album**: {song.album or 'Unknown'}",
            f"**Queue**: {player.queue_length} song(s)",
            f"**Volume**: {int(player.volume * 100)}%",
        ]
        await interaction.response.send_message(embed=self._embed(f"Now Playing [{state}]", "\n".join(fields), 0x57F287))

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

        def make_embed(page: int) -> discord.Embed:
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

            embed = self._embed("Queue", "\n".join(lines), 0x5865F2)
            embed.set_footer(text=f"Page {page + 1}/{total_pages}")
            return embed

        view = QueuePager(make_embed, total_pages)
        view._toggle()
        await interaction.response.send_message(embed=make_embed(0), view=view)

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

        await interaction.response.send_message(embed=self._embed("Queue Shuffled", f"Shuffled {count} song(s).", 0x57F287))

    @app_commands.command(name="playlist", description="Queue all songs from a folder in your music library")
    @app_commands.describe(folder="Folder name inside your music library")
    @app_commands.autocomplete(folder=_playlist_autocomplete)
    async def playlist(self, interaction: discord.Interaction, folder: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        voice_channel = self._user_voice_channel(interaction)
        if not voice_channel:
            await interaction.response.send_message("Join a voice channel first, then run /playlist.", ephemeral=True)
            return

        resolved_folder, folder_candidates = self.library.resolve_folder_query(folder)
        songs = self.library.songs_in_folder(resolved_folder or folder)
        if not songs:
            available = self.library.list_folders()
            if folder_candidates:
                suggestion_list = "\n".join(f"- {f}" for f in folder_candidates)
                await interaction.response.send_message(
                    embed=self._embed(
                        "Folder Not Found",
                        f"Could not uniquely match '{folder}'. Did you mean:\n{suggestion_list}",
                        0xED4245,
                    ),
                    ephemeral=True,
                )
                return
            if available:
                folder_list = "\n".join(f"- {f}" for f in available)
                await interaction.response.send_message(
                    embed=self._embed("Folder Not Found", f"No folder named '{folder}'.\n{folder_list}", 0xED4245),
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
            await interaction.followup.send(embed=self._embed("Playback Error", str(exc), 0xED4245), ephemeral=True)
            return
        except discord.ClientException as exc:
            await interaction.followup.send(embed=self._embed("Voice Error", str(exc), 0xED4245), ephemeral=True)
            return

        first_started = False
        _prev_callback = player.on_song_start
        player.on_song_start = None
        for song in songs:
            started = await player.enqueue(song)
            if started and not first_started:
                first_started = True
        player.on_song_start = _prev_callback

        status = "Now Playing" if first_started else "Queued"
        per_page = 10
        total_pages = max(1, (len(songs) + per_page - 1) // per_page)

        def make_playlist_embed(page: int) -> discord.Embed:
            start = page * per_page
            chunk = songs[start:start + per_page]
            lines = [f"**{resolved_folder or folder}** — {len(songs)} song(s)\n"]
            for idx, s in enumerate(chunk, start=start + 1):
                prefix = "▶" if idx == 1 and first_started else f"{idx}."
                lines.append(f"{prefix} {_song_label(s)}")
            embed = self._embed(status, "\n".join(lines), 0x57F287)
            embed.set_footer(text=f"Page {page + 1}/{total_pages}")
            return embed

        view = QueuePager(make_playlist_embed, total_pages)
        view._toggle()
        await interaction.followup.send(embed=make_playlist_embed(0), view=view)

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
            await interaction.response.send_message(
                embed=self._embed("Skipped", f"Owner override — skipped **{song_label}**.", 0xFEE75C)
            )
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
            # Owner joining an active vote forces an instant skip
            if is_owner or current >= needed:
                self._vote_skip_votes.pop(guild_id, None)
                self._vote_skip_message.pop(guild_id, None)
                song_label = _song_label(player.current_song)
                await player.skip()
                if is_owner:
                    msg = f"Owner override — skipped **{song_label}**."
                else:
                    msg = f"Vote passed ({current}/{needed}) — skipped **{song_label}**."
                await interaction.response.send_message(
                    embed=self._embed("Skipped", msg, 0x57F287)
                )
            else:
                remaining = needed - current
                song_label = _song_label(player.current_song)
                await interaction.response.send_message(
                    embed=self._embed("Vote Skip", f"**{song_label}**\n{current}/{needed} votes — need {remaining} more.", 0xFEE75C),
                    ephemeral=True,
                )
            return

        # Start a new vote
        self._vote_skip_votes[guild_id] = {interaction.user.id}
        song_label = _song_label(player.current_song)
        view = VoteSkipView(self, guild_id, needed, song_label)
        await interaction.response.send_message(
            embed=self._embed(
                "Vote Skip",
                f"**{song_label}**\n1/{needed} votes — need {needed - 1} more. ({_VOTE_SKIP_TIMEOUT}s window)",
                0xFEE75C,
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
        await interaction.response.send_message(
            embed=self._embed("Skipping To", f"**{_song_label(target_song)}**", 0xFEE75C)
        )

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
        await interaction.response.send_message(embed=self._embed("Stopped", "Stopped playback and disconnected.", 0xED4245))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if self.bot.user and member.id == self.bot.user.id:
            if before.channel is not None and after.channel is None:
                player = self.players.get(member.guild.id)
                if player:
                    _LOGGER.warning(
                        "Guild %s: bot was force-disconnected from voice channel '%s'.",
                        member.guild.id,
                        before.channel.name,
                    )
                    player.handle_disconnect()
                    if player.last_text_channel:
                        try:
                            await player.last_text_channel.send(
                                embed=self._embed("Disconnected", "I was disconnected from voice. Use /play to resume.", 0xED4245)
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
                            embed=self._embed("Idle Countdown", f"Voice channel is empty. Disconnecting in {mins} minutes if still idle.", 0xFEE75C)
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
        dupes = len(self.library.duplicates)
        dupe_note = f"{dupes} duplicate title(s) detected" if dupes else "No duplicates detected"
        await interaction.followup.send(
            embed=self._embed("Library Rescanned", f"{total} song(s) indexed. {dupe_note}.", 0x57F287),
            ephemeral=True,
        )
        _LOGGER.info("Manual rescan completed: %s songs, %s duplicates.", total, dupes)

    @app_commands.command(name="search", description="Search the library by title, artist, or album")
    @app_commands.describe(text="What to search for (title, artist, or album name)")
    async def search(self, interaction: discord.Interaction, text: str) -> None:
        results = self.library.search(text, limit=10)
        if not results:
            await interaction.response.send_message(embed=self._embed("Search", f"No results for '{text}'.", 0xED4245), ephemeral=True)
            return

        lines = []
        for i, song in enumerate(results, 1):
            parts = [_song_label(song)]
            if song.album:
                parts.append(f"album: {song.album}")
            lines.append(f"{i}. " + " | ".join(parts))
        await interaction.response.send_message(embed=self._embed("Search Results", "\n".join(lines), 0x5865F2), ephemeral=True)

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
                embed=self._embed("Album Not Found", f"No album matching '{album}'.", 0xED4245),
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
            await interaction.followup.send(embed=self._embed("Playback Error", str(exc), 0xED4245), ephemeral=True)
            return
        except discord.ClientException as exc:
            await interaction.followup.send(embed=self._embed("Voice Error", str(exc), 0xED4245), ephemeral=True)
            return

        first_started = False
        _prev_callback = player.on_song_start
        player.on_song_start = None
        for song in songs:
            started = await player.enqueue(song)
            if started and not first_started:
                first_started = True
        player.on_song_start = _prev_callback

        album_display = songs[0].album or album
        artist_display = f" by {songs[0].artist}" if songs[0].artist else ""
        status = "Now Playing" if first_started else "Queued"
        await interaction.followup.send(
            embed=self._embed(status, f"{album_display}{artist_display} — {len(songs)} song(s)", 0x57F287)
        )

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
            f"Volume: {int(player.volume * 100)}%",
            f"Idle countdown active: {player.idle_countdown_active}",
            f"Idle timeout: {int(player.idle_disconnect_seconds)}s",
            f"Connect timeout: {int(player.connect_timeout_seconds)}s",
            f"FFmpeg: {self.settings.ffmpeg_path}",
            f"FFmpeg before options: {self.settings.ffmpeg_before_options}",
            f"FFmpeg options: {self.settings.ffmpeg_options}",
        ]
        await interaction.response.send_message(embed=self._embed("Diagnostics", "\n".join(lines), 0x5865F2), ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        _LOGGER.exception("Slash command failed: %s", error)
        message = "Playback command failed. Check bot logs for details."

        if interaction.response.is_done():
            await interaction.followup.send(embed=self._embed("Error", message, 0xED4245), ephemeral=True)
            return

        await interaction.response.send_message(embed=self._embed("Error", message, 0xED4245), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
