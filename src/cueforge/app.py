"""Application entry point."""

from __future__ import annotations

import json
import os
import sys
import traceback

from cueforge.runtime import configure_dependency_path, format_diagnostics


def main() -> int:
    configure_dependency_path()
    diagnose_file = _diagnose_file_arg(sys.argv)
    smoke_metadata_url = _arg_value(sys.argv, "--smoke-metadata-url")
    if smoke_metadata_url:
        exit_code = 0
        try:
            payload = _smoke_metadata(smoke_metadata_url)
        except Exception as exc:
            exit_code = 2
            payload = {
                "url": smoke_metadata_url,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "diagnostics": format_diagnostics(),
            }
        output = json.dumps(payload, ensure_ascii=False, indent=2)
        _write_cli_output(output, diagnose_file)
        return _finish_cli(exit_code)
    if "--diagnose" in sys.argv or diagnose_file:
        diagnostics = format_diagnostics()
        if "--smoke-gui" in sys.argv:
            _smoke_gui()
            diagnostics += "\ngui: ok"
        _write_cli_output(diagnostics, diagnose_file)
        return _finish_cli(0)
    if "--smoke-gui" in sys.argv:
        _smoke_gui()
        return _finish_cli(0)
    from cueforge.gui.main_window import run_app

    return run_app()


def run() -> None:
    exit_code = main()
    if _is_cli_utility_mode(sys.argv):
        _force_process_exit(exit_code)
    raise SystemExit(exit_code)


def _finish_cli(exit_code: int) -> int:
    if getattr(sys, "frozen", False):
        _force_process_exit(exit_code)
    return exit_code


def _force_process_exit(exit_code: int) -> None:
    if not getattr(sys, "frozen", False):
        sys.stdout.flush()
        sys.stderr.flush()
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), exit_code)
    os._exit(exit_code)


def _write_cli_output(text: str, diagnose_file: object | None) -> None:
    if diagnose_file is not None:
        diagnose_file.write_text(text + "\n", encoding="utf-8")
    if getattr(sys, "frozen", False):
        return
    _print_cli_output(text)


def _print_cli_output(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            buffer.write((text + "\n").encode("utf-8"))
            buffer.flush()
            return
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def _is_cli_utility_mode(argv: list[str]) -> bool:
    return any(
        flag in argv
        for flag in (
            "--diagnose",
            "--diagnose-file",
            "--smoke-gui",
            "--smoke-metadata-url",
        )
    )


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

    from cueforge.download import DownloadConfig, YTDLPDownloader
    from cueforge.metadata.resolver import MetadataResolver

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
                "bpm": candidate.metadata.bpm,
                "bpm_source": candidate.metadata.bpm_source,
                "bpm_confidence": candidate.metadata.bpm_confidence,
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

    from cueforge.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("CueForge")
    window = MainWindow()
    window.close()
    window.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    run()
