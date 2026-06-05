"""Application entry point."""

from __future__ import annotations

import sys

from ytdj.runtime import configure_dependency_path, format_diagnostics


def main() -> int:
    configure_dependency_path()
    diagnose_file = _diagnose_file_arg(sys.argv)
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

    if "--diagnose-file" not in argv:
        return None
    index = argv.index("--diagnose-file")
    try:
        return Path(argv[index + 1])
    except IndexError as exc:
        raise SystemExit("--diagnose-file requires a path") from exc


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
