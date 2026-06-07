# YT-DJ

YT-DJ is a Windows-first desktop app for downloading authorized YouTube/YouTube Music audio with rekordbox-friendly MP3 tags.

## v1 Scope

- PySide6 desktop UI
- Embedded `yt-dlp` download pipeline
- YouTube, YouTube Music, and SoundCloud URLs supported through yt-dlp
- ffmpeg conversion to MP3 320 kbps
- ID3v2.3 tagging for rekordbox compatibility
- YouTube description/YTMusic metadata first, MusicBrainz enrichment second
- Cover Art Archive release artwork preferred over YouTube thumbnails when MusicBrainz release metadata is available
- AcoustID/Chromaprint audio recognition for low-confidence YouTube metadata
- External BPM tagging from native source metadata first, then GetSongBPM strict matches when an API key is configured
- Separate metadata analysis, review approval, and approved download/tagging steps
- Low-confidence metadata review queue before final tagging

SoundCloud tracks are treated differently from YouTube videos: native SoundCloud title, artist/uploader, genre, tags, artwork, and source URL are trusted by default so remix, bootleg, edit, and mashup titles are preserved.
Automatic audio recognition is skipped for SoundCloud by default to avoid replacing remix or bootleg metadata with the original commercial release.
The review screen shows a review queue, alternative metadata candidates, confidence details, cover-art source, and a cover preview so beta testers can catch bad matches before tagging. Use `Analyze Metadata` first, review or approve tracks as needed, then use `Download Approved` to download and tag the approved tracks. `Retry Failed` returns failed jobs to the analysis queue.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ytdj
```

The app expects `ffmpeg` and Deno to be available on PATH during development. Deno lets yt-dlp solve current YouTube JavaScript challenges through its `ejs:github` remote component. Optional AcoustID recognition also requires an AcoustID application client key and `fpcalc` from Chromaprint, either on PATH or selected in Settings. Optional BPM tagging can use a GetSongBPM API key in Settings; GetSongBPM requires a mandatory backlink in public releases or documentation.
For account-scoped YouTube access, select Chrome or Edge browser cookies in Settings. If yt-dlp cannot copy the locked Chromium cookie database, the optional Chrome/Edge cookie unlock setting applies bundled logic adapted from `seproDev/yt-dlp-ChromeCookieUnlock`; keep it off unless that specific cookie-copy failure occurs because it may close the browser process holding the database lock.
Use `python -m ytdj --smoke-metadata-url <url>` to validate yt-dlp metadata extraction, resolver matching, Cover Art Archive lookup, and diagnostics without downloading audio.

## Windows Packaging

The release flow builds a PyInstaller app and an Inno Setup online installer. At build time, the package script resolves the latest x64 ZIP manifests for Deno, Chromaprint `fpcalc`, and ffmpeg from `microsoft/winget-pkgs`. The installer then downloads those resolved URLs directly, verifies each archive by SHA-256, and installs them under the app's `bin` directory so end users do not need to install those tools manually.

```powershell
.\scripts\package_windows.ps1
```

Use `-SkipInstaller` to build only the PyInstaller app. External dependency package IDs are configured in `packaging/dependencies.windows-x64.json`; resolved versions, hashes, packaged diagnostics, installer SHA-256, and verification results are written to the release report during packaging.

See [docs/development.md](docs/development.md) for commit, authentication, and packaging notes.
