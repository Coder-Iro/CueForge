# Third-Party Notices

CueForge depends on Python packages and external command-line tools. This file is an alpha-stage notice list for Windows builds and must be reviewed before public release.

## Python Packages

- PySide6: LGPL/commercial licensing, used for the desktop UI.
- yt-dlp: Unlicense, used for supported media extraction.
- mutagen: GPL-2.0-or-later, used for ID3 tag writing.
- requests: Apache-2.0, used for HTTP requests.
- ytmusicapi: MIT, used for YouTube Music metadata.
- platformdirs: MIT, used for app data path resolution.

## Online Installer Dependencies

The Windows online installer downloads external command-line tools during setup and verifies SHA256 before extraction. Exact versions, URLs, and hashes are resolved from `microsoft/winget-pkgs` at release build time and recorded in the generated release dependency report.

- Deno (`DenoLand.Deno`)
  - License: MIT
  - Purpose: JavaScript runtime for yt-dlp YouTube challenge solving.
- FFmpeg full shared build by Gyan (`Gyan.FFmpeg.Shared`)
  - License: GPL-3.0 for this selected build
  - Purpose: Audio extraction and MP3 conversion through yt-dlp.

## External Services

- OpenAI API: optional ChatGPT metadata parsing and web search when the user configures an OpenAI API key.
- YouTube, YouTube Music, and SoundCloud: URLs are processed through yt-dlp. Users are responsible for using the app only with content they are authorized to download.
