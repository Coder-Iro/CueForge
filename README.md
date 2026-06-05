# YT-DJ

YT-DJ is a Windows-first desktop app for downloading authorized YouTube/YouTube Music audio with rekordbox-friendly MP3 tags.

## v1 Scope

- PySide6 desktop UI
- Embedded `yt-dlp` download pipeline
- ffmpeg conversion to MP3 320 kbps
- ID3v2.3 tagging for rekordbox compatibility
- YouTube Music metadata first, MusicBrainz enrichment second
- Low-confidence metadata review before final tagging

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ytdj
```

The app expects `ffmpeg` to be available on PATH during development.

See [docs/development.md](docs/development.md) for commit, authentication, and packaging notes.
