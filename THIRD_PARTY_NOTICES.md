# Third-Party Notices

YT-DJ depends on Python packages and external command-line tools. This file is an alpha-stage notice list for Windows builds and must be reviewed before public release.

## Python Packages

- PySide6: LGPL/commercial licensing, used for the desktop UI.
- yt-dlp: Unlicense, used for supported media extraction.
- mutagen: GPL-2.0-or-later, used for ID3 tag writing.
- requests: Apache-2.0, used for metadata and cover-art HTTP calls.
- ytmusicapi: MIT, used for YouTube Music metadata.
- platformdirs: MIT, used for cache path resolution.

## Online Installer Dependencies

The Windows online installer downloads these archives during setup and verifies SHA256 before extraction:

- Deno 2.8.2
  - URL: https://github.com/denoland/deno/releases/download/v2.8.2/deno-x86_64-pc-windows-msvc.zip
  - License: MIT
  - Purpose: JavaScript runtime for yt-dlp YouTube challenge solving.
- Chromaprint fpcalc 1.6.0
  - URL: https://github.com/acoustid/chromaprint/releases/download/v1.6.0/chromaprint-fpcalc-1.6.0-windows-x86_64.zip
  - License: LGPL-2.1-or-later
  - Purpose: Acoustic fingerprint extraction for AcoustID lookup.
- FFmpeg 8.1.1 full shared build by Gyan
  - URL: https://github.com/GyanD/codexffmpeg/releases/download/8.1.1/ffmpeg-8.1.1-full_build-shared.zip
  - License: GPL-3.0 for this selected build
  - Purpose: Audio extraction and MP3 conversion through yt-dlp.

## External Services

- MusicBrainz: metadata lookup and MusicBrainz identifiers.
- AcoustID: optional acoustic fingerprint metadata lookup; users provide their own client key.
- YouTube, YouTube Music, and SoundCloud: URLs are processed through yt-dlp. Users are responsible for using the app only with content they are authorized to download.
