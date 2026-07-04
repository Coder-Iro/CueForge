# Development Notes

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m cueforge
```

`ffmpeg` must be on PATH or selected in the Settings tab.
Install Deno on PATH for YouTube JavaScript challenge solving. The downloader enables yt-dlp's `ejs:github` remote component by default so Deno can run the current solver script.
Use `python -m cueforge --smoke-metadata-url <url>` to validate yt-dlp metadata extraction, resolver hints, cover fallback, and diagnostics without downloading audio.

## Git Workflow

Keep commits small and functional:

- scaffold and project configuration
- core metadata models and merge policy
- provider integrations
- download pipeline
- tag writer
- GUI workflow
- tests and documentation

Before each commit, run the most relevant test command and check `git status --short`.

## YouTube Music Authentication

The app can run without auth for public URLs. For account-scoped YouTube Music metadata and playlist expansion, place the distributor-owned Google OAuth client JSON at `config/google_oauth_client.json` before packaging. Packaged users can then press Settings > Google Account > Connect, complete the browser approval, and CueForge stores the user's OAuth token under the app data directory.

Google OAuth is used for account-scoped YouTube Music playlist expansion. `ytmusicapi` metadata lookup runs unauthenticated unless the playlist path uses the YouTube Data API.

Direct browser-cookie extraction, cookies.txt, and manual `ytmusicapi` browser auth JSON are not supported. Do not commit the real OAuth client JSON or OAuth tokens.

## Metadata Resolution

YouTube and YouTube Music analysis uses YouTube Music metadata when available, then title and description hints as review candidates. Explicit anime theme hints and cover hints can become the initial review metadata, but they still require review unless a future trusted provider marks them higher confidence.

The ChatGPT/OpenAI parser is configured by connecting a ChatGPT account in Settings. It uses CueForge's own Codex OAuth connection, stores the resulting token under the app data directory, refreshes the connected account's Codex model catalog from `/backend-api/codex/models`, shows current model/usage in the status bar using `/backend-api/wham/usage` or response rate-limit data when present, calls the ChatGPT Codex Responses endpoint through `requests`, uses structured JSON output, and always enables the built-in `web_search` tool for BPM and official metadata lookups. It caps candidate confidence below the auto-approval threshold so LLM output is review-first.

Do not read local Codex CLI credentials for this parser. The Settings > ChatGPT Metadata > Connect flow creates the app-owned OAuth profile used by CueForge.

BPM is stored on `TrackMetadata.bpm`, appears in the review UI, and is written to MP3 files as ID3 `TBPM`. The parser should return `null` for BPM when the exact recording, remix, edit, or cover version is uncertain.

## SoundCloud Metadata

SoundCloud is primarily supported for DJ-focused remix, bootleg, edit, mashup, and free download workflows. The app trusts SoundCloud native metadata by default and preserves the original title instead of normalizing it against canonical release databases.

## Cover Art and Review

Cover art is resolved in this order: SoundCloud native artwork for SoundCloud tracks, YouTube Music artwork when provided, then platform thumbnails as fallback.

The queue flow is split into `Analyze Metadata`, review approval, and `Download Approved`. Analysis turns auto-approved tracks into `approved` jobs and sends lower-confidence tracks to the review queue; downloading only runs for approved jobs.

The review tab shows a review queue, candidate metadata rows, matched fields, candidate scores, confidence explanations, cover source, and a cover preview before tagging. Selecting a candidate shows a current-vs-candidate preview; the editable fields are changed only after pressing `후보 적용`.

## Beta Diagnostics

The Queue tab shows a compact settings status banner for ffmpeg, ChatGPT parser status, Google OAuth status, YouTube Music auth, and worker concurrency. The Settings tab has a Copy Diagnostics action that copies Python, PySide6, yt-dlp, and external dependency status to the clipboard. Job logs include best candidate, selected metadata, cover source, and written/skipped tag fields.

For packaged or local metadata smoke checks without audio download:

```powershell
.\dist\CueForge\CueForge.exe --smoke-metadata-url https://youtu.be/VEb3rctB3dc
.\dist\CueForge\CueForge.exe --smoke-metadata-url https://youtu.be/VEb3rctB3dc --diagnose-file build\metadata-smoke.json
```

The smoke output is JSON with resolver state, platform, selected metadata, top candidates, resolver logs, and dependency diagnostics.

## Packaging

Windows releases are built as a PyInstaller application plus an Inno Setup online installer. The installer downloads external binary dependencies during setup instead of bundling them into git or the PyInstaller tree.

```powershell
.\.venv\Scripts\python -m pip install -e ".[packaging]"
.\scripts\package_windows.ps1
```

The package script runs verification in this fixed order: full `pytest`, local GUI smoke, metadata regression fixture suite, packaged `--diagnose-file`, and packaged `--smoke-gui`. It then resolves external dependency URLs from `microsoft/winget-pkgs` and invokes Inno Setup if `ISCC.exe` is available. Use `-SkipInstaller` when only the PyInstaller app is needed, or `-SkipTests` for a repeat build after tests have already passed.

If `config\google_oauth_client.json` exists when PyInstaller runs, the spec copies it into the packaged app under `config`. Keep the real file local and untracked; use `config\google_oauth_client.example.json` as the committed template.

External dependency package IDs are configured in `packaging\dependencies.windows-x64.json`:

- Deno for yt-dlp's JavaScript challenge solver
- ffmpeg and ffprobe for audio conversion and probing

For installer builds, `scripts\resolve_winget_dependencies.py` reads the latest stable x64 ZIP installer manifest for each package and generates `build\dependencies.windows-x64.resolved.json` plus `build\dependencies.windows-x64.iss`. The Inno script includes the generated `.iss`, downloads those ZIP archives, checks SHA-256 hashes, and extracts them to `{app}\bin`. The runtime prepends discovered dependency directories to `PATH`, so the bundled app can find `ffmpeg`, `ffprobe`, and `deno` without system-wide installs.

The resolved dependency report is copied to `release\CueForge-<version>-windows-x64-dependencies.json`, and the full release report is written to `release\CueForge-<version>-windows-x64-release-report.json`. The release report includes external dependency versions and SHA-256 values, packaged diagnostics, packaged test results, installer SHA-256 when an installer is built, and the third-party notice path. When changing package IDs, install subdirectories, expected executables, or public notices, update the config and notices together, then run the packaging tests before cutting a release.
