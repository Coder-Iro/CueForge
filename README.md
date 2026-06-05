# YT-DJ

YT-DJ is a Windows-first desktop app for downloading authorized YouTube/YouTube Music audio with rekordbox-friendly MP3 tags.

## v1 Scope

- PySide6 desktop UI
- Embedded `yt-dlp` download pipeline
- YouTube, YouTube Music, and SoundCloud URLs supported through yt-dlp
- ffmpeg conversion to MP3 320 kbps
- ID3v2.3 tagging for rekordbox compatibility
- YouTube description/YTMusic metadata first, MusicBrainz enrichment second
- AcoustID/Chromaprint audio recognition for low-confidence YouTube metadata
- Low-confidence metadata review before final tagging

SoundCloud tracks are treated differently from YouTube videos: native SoundCloud title, artist/uploader, genre, tags, artwork, and source URL are trusted by default so remix, bootleg, edit, and mashup titles are preserved.
Automatic audio recognition is skipped for SoundCloud by default to avoid replacing remix or bootleg metadata with the original commercial release.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ytdj
```

The app expects `ffmpeg` to be available on PATH during development. Optional AcoustID recognition also requires an AcoustID application client key and `fpcalc` from Chromaprint, either on PATH or selected in Settings.

See [docs/development.md](docs/development.md) for commit, authentication, and packaging notes.
