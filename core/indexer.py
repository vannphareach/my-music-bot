from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from mutagen._file import File as MutagenFile

SUPPORTED_AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".ogg", ".m4a"}

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Song:
    title: str
    display_title: str
    path: Path
    relative_path: str
    search_text: str
    artist: str = ""
    album: str = ""
    year: str = ""


class MusicLibrary:
    def __init__(self, music_root: Path) -> None:
        self.music_root = music_root
        self.songs: list[Song] = []
        self.duplicates: list[tuple[Song, Song]] = []

    def scan(self) -> int:
        raw: list[Song] = []
        for path in _list_supported_audio_files(self.music_root):
            raw.append(_build_song(path, self.music_root))

        raw.sort(key=lambda s: s.title.lower())

        # Duplicate detection: same normalised title
        seen: dict[str, Song] = {}
        self.duplicates = []
        self.songs = []
        for song in raw:
            key = _normalize(song.title)
            if key in seen:
                self.duplicates.append((seen[key], song))
                _LOGGER.warning(
                    "Duplicate title '%s': '%s' vs '%s'",
                    song.title,
                    seen[key].relative_path,
                    song.relative_path,
                )
            else:
                seen[key] = song
            self.songs.append(song)

        return len(self.songs)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[Song]:
        term = _normalize(query)
        if not term:
            return []
        return [s for s in self.songs if term in s.search_text][:max(1, limit)]

    def resolve_play_query(self, query: str, limit: int = 8) -> tuple[Song | None, list[Song]]:
        """Resolve play query into a unique song or a list of candidates.

        Returns:
        - (song, []) when query resolves uniquely
        - (None, candidates) when multiple songs match and user should pick
        - (None, []) when nothing matches
        """
        term = _normalize(query)
        if not term:
            return None, []

        def _title_exact(song: Song) -> bool:
            return _normalize(song.title) == term or _normalize(song.display_title) == term

        def _title_starts(song: Song) -> bool:
            return _normalize(song.title).startswith(term) or _normalize(song.display_title).startswith(term)

        exact = [s for s in self.songs if _title_exact(s)]
        if len(exact) == 1:
            return exact[0], []
        if len(exact) > 1:
            return None, exact[: max(1, limit)]

        starts = [s for s in self.songs if _title_starts(s)]
        if len(starts) == 1:
            return starts[0], []
        if len(starts) > 1:
            return None, starts[: max(1, limit)]

        contains = [s for s in self.songs if term in s.search_text]
        if len(contains) == 1:
            return contains[0], []
        if len(contains) > 1:
            return None, contains[: max(1, limit)]

        return None, []

    def search_by_album(self, album: str) -> list[Song]:
        term = _normalize(album)
        if not term:
            return []
        return [s for s in self.songs if term in _normalize(s.album)]

    def resolve_folder_query(self, query: str, limit: int = 10) -> tuple[str | None, list[str]]:
        """Resolve folder query into one folder or candidate list.

        Returns:
        - (folder, []) when resolved uniquely
        - (None, candidates) when multiple folders match
        - (None, []) when nothing matches
        """
        term = _normalize(query)
        if not term:
            return None, []

        folders = self.list_folders()

        exact = [f for f in folders if _normalize(f) == term]
        if len(exact) == 1:
            return exact[0], []
        if len(exact) > 1:
            return None, exact[: max(1, limit)]

        starts = [f for f in folders if _normalize(f).startswith(term)]
        if len(starts) == 1:
            return starts[0], []
        if len(starts) > 1:
            return None, starts[: max(1, limit)]

        contains = [f for f in folders if term in _normalize(f)]
        if len(contains) == 1:
            return contains[0], []
        if len(contains) > 1:
            return None, contains[: max(1, limit)]

        return None, []

    def songs_in_folder(self, folder_name: str) -> list[Song]:
        resolved, _ = self.resolve_folder_query(folder_name)
        if not resolved:
            return []

        needle = resolved.strip().lower()
        return [
            s for s in self.songs
            if s.relative_path.lower().startswith(needle + "\\")
            or s.relative_path.lower().startswith(needle + "/")
        ]

    def list_folders(self) -> list[str]:
        folders: set[str] = set()
        for s in self.songs:
            parts = Path(s.relative_path).parts
            if len(parts) > 1:
                folders.add(parts[0])
        return sorted(folders)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _normalize(text: str) -> str:
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().lower().strip()


def _read_tags(path: Path) -> tuple[str, str, str]:
    """Return (artist, album, year) from file tags, or empty strings on failure."""
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return "", "", ""
        artist = (audio.get("artist") or audio.get("albumartist") or [""])[0]
        album = (audio.get("album") or [""])[0]
        year = (audio.get("date") or audio.get("year") or [""])[0]
        return str(artist).strip(), str(album).strip(), str(year)[:4].strip()
    except Exception:
        return "", "", ""


def _build_song(path: Path, music_root: Path) -> Song:
    title = path.stem
    display_title = _clean_display_title(title)
    try:
        rel = str(path.relative_to(music_root))
    except ValueError:
        rel = path.name

    artist, album, year = _read_tags(path)
    search_text = _normalize(f"{title} {rel} {artist} {album}")
    return Song(
        title=title,
        display_title=display_title,
        path=path,
        relative_path=rel,
        search_text=search_text,
        artist=artist,
        album=album,
        year=year,
    )


def _clean_display_title(title: str) -> str:
    """Hide leading track numbers in UI labels, e.g. '01. Starboy' -> 'Starboy'."""
    cleaned = re.sub(r"^\s*\d{1,3}\s*[.)_-]\s*", "", title)
    return cleaned.strip() or title


def _list_supported_audio_files(music_root: Path) -> list[Path]:
    if not music_root.exists() or not music_root.is_dir():
        return []
    files: list[Path] = []
    for path in music_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
            files.append(path)
    return files

