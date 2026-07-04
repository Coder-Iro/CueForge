# CueForge

CueForge is a Windows-first desktop app for preparing authorized YouTube, YouTube Music, and SoundCloud audio with DJ-ready MP3 metadata.

## v1 Scope

- PySide6 desktop UI
- Embedded `yt-dlp` download pipeline
- YouTube, YouTube Music, and SoundCloud URLs supported through yt-dlp
- ffmpeg conversion to MP3 320 kbps
- ID3v2.3 tagging for rekordbox compatibility
- YouTube Music metadata plus title/description hints for YouTube sources
- ChatGPT/OpenAI metadata parser for YouTube sources when a ChatGPT account is connected, including review-only BPM candidates with always-on web search
- SoundCloud native title, uploader/creator, genre, artwork, and source URL preserved by default
- Platform artwork fallback from SoundCloud, YouTube Music, or YouTube thumbnails
- BPM tagging through ID3 `TBPM`
- Google OAuth support for account-scoped YouTube Music playlist access
- Separate metadata analysis, review approval, and approved download/tagging steps
- Low-confidence metadata review queue before final tagging

The ChatGPT metadata parser uses CueForge's own Codex OAuth connection. When a ChatGPT account is connected and a catalog model is selected, YouTube analysis automatically adds ChatGPT review candidates. CueForge fetches the account's Codex model list for selection, shows current model/usage in the status bar, uses structured output and always-on web search for BPM and official metadata, then returns review candidates rather than blindly tagging files.

SoundCloud tracks are treated differently from YouTube videos: native SoundCloud metadata is trusted by default so remix, bootleg, edit, and mashup titles are preserved.
The review screen shows a review queue, alternative metadata candidates, confidence details, cover-art source, and a cover preview so beta testers can catch bad matches before tagging. Use `Analyze Metadata` first, review or approve tracks as needed, then use `Download Approved` to download and tag the approved tracks. `Retry Failed` returns failed jobs to the analysis queue.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m cueforge
```

The app expects `ffmpeg` and Deno to be available on PATH during development. Deno lets yt-dlp solve current YouTube JavaScript challenges through its `ejs:github` remote component.
Use Settings > ChatGPT Metadata to connect a ChatGPT account for YouTube metadata parsing. CueForge stores that app-owned Codex OAuth token under the app data directory, does not read local Codex CLI credentials, and uses the connected account to refresh available models and Codex usage.
For account-scoped YouTube Music playlist expansion, packaged builds can include `config/google_oauth_client.json`; end users then use Settings > Google Account to connect their Google account through the browser. CueForge stores only the user's refresh token under the app data directory. cookies.txt, browser-cookie extraction, and manual YTMusic auth JSON are no longer supported.
Use `python -m cueforge --smoke-metadata-url <url>` to validate yt-dlp metadata extraction, resolver hints, cover fallback, and diagnostics without downloading audio.

## Windows Packaging

The release flow builds a PyInstaller app and an Inno Setup online installer. At build time, the package script resolves the latest x64 ZIP manifests for Deno and ffmpeg from `microsoft/winget-pkgs`. The installer then downloads those resolved URLs directly, verifies each archive by SHA-256, and installs them under the app's `bin` directory so end users do not need to install those tools manually.

```powershell
.\scripts\package_windows.ps1
```

Use `-SkipInstaller` to build only the PyInstaller app. External dependency package IDs are configured in `packaging/dependencies.windows-x64.json`; resolved versions, hashes, packaged diagnostics, installer SHA-256, and verification results are written to the release report during packaging.
Copy your Google OAuth desktop/client JSON to `config/google_oauth_client.json` before packaging if you want the distributed app to expose the one-click Google account connection flow. The real client file is ignored by git; `config/google_oauth_client.example.json` documents the expected shape.

See [docs/development.md](docs/development.md) for commit, authentication, and packaging notes.
