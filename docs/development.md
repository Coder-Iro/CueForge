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
Optional audio recognition requires `fpcalc` from Chromaprint. Put `fpcalc` on PATH or select the executable in Settings.
Optional external BPM tagging requires a GetSongBPM API key in Settings. BPM is never calculated locally.
Use `python -m cueforge --smoke-metadata-url <url>` to validate yt-dlp metadata extraction, resolver matching, Cover Art Archive lookup, and diagnostics without downloading audio.

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

The app can run without browser cookies for public URLs. For account-scoped access, select a browser in Settings so yt-dlp can use `cookiesfrombrowser`.

If Chrome or Edge cookies fail with a locked cookie database error, enable "Use Chrome/Edge cookie unlock if the browser database is locked" in Settings. This is an opt-in Windows-only fallback based on `seproDev/yt-dlp-ChromeCookieUnlock`; it asks Windows Restart Manager to release the lock and may close the browser process holding that cookie database.

The same browser cookie selection is also used for YouTube Music metadata. CueForge reads the `music.youtube.com` cookie header through yt-dlp's browser cookie extractor and, when `__Secure-3PAPISID` is present, builds the browser auth payload expected by `ytmusicapi`. Manual `ytmusicapi` browser auth JSON remains available as an advanced fallback; do not commit auth JSON, copied request headers, or browser cookies.

## Audio Recognition

AcoustID recognition is used only when YouTube or YouTube Music metadata is not auto-approved by the text-based resolver. The worker downloads a temporary MP3 under `.cueforge-temp`, runs `fpcalc`, queries AcoustID, and reuses that prepared file after review approval so the same URL is not downloaded twice.

For beta metadata validation, enable "Verify YouTube auto-approved metadata with AcoustID" in Settings. This uses the same temporary-audio flow for auto-approved YouTube metadata and lowers conflicting high-confidence fingerprint matches back to review. SoundCloud remains excluded by default.

Configure an AcoustID application client key in Settings. Do not commit keys or user credentials. The free AcoustID web service is intended for non-commercial use and rate-limits clients, so this app uses it only as a fallback recognition layer.

## External BPM

BPM source priority is manual Tag Editor input, then native yt-dlp metadata such as SoundCloud BPM/tempo, then GetSongBPM. Missing BPM is not a job failure; the app simply omits the `TBPM` tag.

GetSongBPM is queried only when an API key is configured. The resolver sends `type=both`, `lookup=song:<title> artist:<artist>`, and `limit=5` to `https://api.getsong.co/search/` with the key in the `X-API-KEY` header. Only strict title/artist-centered matches scoring 0.85 or higher are applied. Values are written as integer ID3 `TBPM` frames after decimal rounding, with no half/double tempo correction, so values like 64, 174, and 220 pass through.

GetSongBPM requires a mandatory backlink; include it in release notes, project docs, or the public distribution page when shipping builds. CueForge's public README links to https://getsongbpm.com/ for this requirement. Spotify Audio Features/Audio Analysis is intentionally excluded from v1 because access for new Spotify apps is restricted.

## SoundCloud Metadata

SoundCloud is primarily supported for DJ-focused remix, bootleg, edit, mashup, and free download workflows. The app trusts SoundCloud native metadata by default and preserves the original title instead of normalizing it against canonical release databases.

MusicBrainz or other external matches may be shown as reference candidates later, but they should not automatically overwrite SoundCloud title, uploader/creator, genre, artwork, or source URL.
Automatic AcoustID recognition is skipped for SoundCloud tracks for the same reason.

## Cover Art and Review

Cover art is resolved in this order: SoundCloud native artwork for SoundCloud tracks, Cover Art Archive 500px front artwork when a MusicBrainz release ID is available, then platform thumbnails as fallback.

The queue flow is split into `Analyze Metadata`, review approval, and `Download Approved`. Analysis turns auto-approved tracks into `approved` jobs and sends lower-confidence tracks to the review queue; downloading only runs for approved jobs.

The review tab shows a review queue, candidate metadata rows, matched fields, candidate scores, confidence explanations, cover source, and a cover preview before tagging. Selecting a candidate shows a current-vs-candidate preview; the editable fields are changed only after pressing `후보 적용`.

## Beta Diagnostics

The Queue tab shows a compact settings status banner for ffmpeg, fpcalc, AcoustID, GetSongBPM, browser cookies, cookie unlock state, and YouTube Music auth. The Settings tab has a Copy Diagnostics action that copies Python, PySide6, yt-dlp, and external dependency status to the clipboard. Job logs include best candidate, selected metadata, BPM resolution/skips, cover source, and written/skipped tag fields.

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

External dependency package IDs are configured in `packaging\dependencies.windows-x64.json`:

- Deno for yt-dlp's JavaScript challenge solver
- Chromaprint `fpcalc` for AcoustID fingerprinting
- ffmpeg and ffprobe for audio conversion and probing

For installer builds, `scripts\resolve_winget_dependencies.py` reads the latest stable x64 ZIP installer manifest for each package and generates `build\dependencies.windows-x64.resolved.json` plus `build\dependencies.windows-x64.iss`. The Inno script includes the generated `.iss`, downloads those ZIP archives, checks SHA-256 hashes, and extracts them to `{app}\bin`. The runtime prepends discovered dependency directories to `PATH`, so the bundled app can find `ffmpeg`, `ffprobe`, `fpcalc`, and `deno` without system-wide installs.

The resolved dependency report is copied to `release\CueForge-<version>-windows-x64-dependencies.json`, and the full release report is written to `release\CueForge-<version>-windows-x64-release-report.json`. The release report includes external dependency versions and SHA-256 values, packaged diagnostics, packaged test results, installer SHA-256 when an installer is built, and the GetSongBPM backlink notice flag. When changing package IDs, install subdirectories, expected executables, or public notices, update the config and notices together, then run the packaging tests before cutting a release.
