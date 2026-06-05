"""Application entry point."""

from __future__ import annotations

import json
import sys

from ytdj.runtime import configure_dependency_path, format_diagnostics


def main() -> int:
    configure_dependency_path()
    diagnose_file = _diagnose_file_arg(sys.argv)
    smoke_metadata_url = _arg_value(sys.argv, "--smoke-metadata-url")
    if smoke_metadata_url:
        payload = _smoke_metadata(smoke_metadata_url)
        output = json.dumps(payload, ensure_ascii=False, indent=2)
        if diagnose_file:
            diagnose_file.write_text(output + "\n", encoding="utf-8")
        print(output)
        return 0
    if "--diagnose" in sys.argv or diagnose_file:
        diagnostics = format_diagnostics()
        if "--smoke-gui" in sys.argv:
            _smoke_gui()
            diagnostics += "\ngui: ok"
        if diagnose_file:
            diagnose_file.write_text(diagnostics + "\n", encoding="utf-8")
        print(diagnostics)
        return 0
    if "--smoke-gui" in sys.argv:
        _smoke_gui()
        return 0
    from ytdj.gui.main_window import run_app

    return run_app()


def _diagnose_file_arg(argv: list[str]) -> "Path | None":
    from pathlib import Path

    value = _arg_value(argv, "--diagnose-file")
    if value is None:
        return None
    return Path(value)


def _arg_value(argv: list[str], flag: str) -> str | None:
    if flag not in argv:
        return None
    index = argv.index(flag)
    try:
        return argv[index + 1]
    except IndexError as exc:
        raise SystemExit(f"{flag} requires a value") from exc


def _smoke_metadata(
    url: str,
    *,
    downloader_factory: object | None = None,
    resolver_factory: object | None = None,
) -> dict:
    from pathlib import Path

    from ytdj.download import DownloadConfig, YTDLPDownloader
    from ytdj.metadata.resolver import MetadataResolver

    downloader_cls = downloader_factory or YTDLPDownloader
    resolver_cls = resolver_factory or MetadataResolver
    downloader = downloader_cls(DownloadConfig(output_dir=Path("downloads"), quiet=True))
    info = downloader.fetch_info(url)
    logs: list[str] = []
    resolution = resolver_cls().resolve(url=url, info=info, log=logs.append)
    metadata = resolution.metadata
    return {
        "url": url,
        "state": resolution.state.value,
        "platform": resolution.platform.value,
        "metadata": {field: getattr(metadata, field) for field in metadata.field_names()},
        "candidates": [
            {
                "provider": candidate.provider,
                "score": candidate.score,
                "matched_fields": list(candidate.matched_fields),
                "title": candidate.metadata.title,
                "artist": candidate.metadata.artist,
                "album": candidate.metadata.album,
                "release_date": candidate.metadata.release_date,
                "isrc": candidate.metadata.isrc,
                "musicbrainz_release_id": candidate.metadata.musicbrainz_release_id,
            }
            for candidate in resolution.candidates[:5]
        ],
        "logs": logs,
        "diagnostics": format_diagnostics(),
    }


def _smoke_gui() -> None:
    from PySide6.QtWidgets import QApplication

    from ytdj.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("YT-DJ")
    window = MainWindow()
    window.close()
    window.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    raise SystemExit(main())
