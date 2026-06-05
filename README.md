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

The app expects `ffmpeg` and Deno to be available on PATH during development. Deno lets yt-dlp solve current YouTube JavaScript challenges through its `ejs:github` remote component. Optional AcoustID recognition also requires an AcoustID application client key and `fpcalc` from Chromaprint, either on PATH or selected in Settings.

## Windows Packaging

The release flow builds a PyInstaller app and an Inno Setup online installer. The installer downloads locked Windows builds of Deno, Chromaprint `fpcalc`, and ffmpeg during setup, verifies each archive by SHA-256, and installs them under the app's `bin` directory so end users do not need to install those tools manually.

```powershell
.\scripts\package_windows.ps1
```

Use `-SkipInstaller` to build only the PyInstaller app. External dependency versions and hashes are pinned in `packaging/dependencies.windows-x64.json`; update `THIRD_PARTY_NOTICES.md` when changing that lock file.

See [docs/development.md](docs/development.md) for commit, authentication, and packaging notes.
