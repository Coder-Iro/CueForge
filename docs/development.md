# Development Notes

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ytdj
```

`ffmpeg` must be on PATH or selected in the Settings tab.

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

For YouTube Music metadata calls, create a `ytmusicapi` browser auth JSON outside git and select it in Settings. Do not commit auth JSON or copied request headers.

## SoundCloud Metadata

SoundCloud is primarily supported for DJ-focused remix, bootleg, edit, mashup, and free download workflows. The app trusts SoundCloud native metadata by default and preserves the original title instead of normalizing it against canonical release databases.

MusicBrainz or other external matches may be shown as reference candidates later, but they should not automatically overwrite SoundCloud title, uploader/creator, genre, artwork, or source URL.

## Packaging

The packaging extra installs PyInstaller:

```powershell
.\.venv\Scripts\python -m pip install -e ".[packaging]"
.\.venv\Scripts\pyinstaller --noconfirm --windowed --name YT-DJ --collect-all PySide6 src\ytdj\app.py
```

For releases, either bundle a reviewed ffmpeg build or document that users must install ffmpeg separately. If ffmpeg is bundled, verify the build license and include the required notices.
