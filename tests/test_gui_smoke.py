import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ytdj.gui.main_window import MainWindow
from ytdj.models import DownloadStatus


def test_main_window_can_queue_url() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.url_input.setText("https://music.youtube.com/watch?v=abc")
        window._add_url()

        assert window.table.rowCount() == 1
        job = next(iter(window.jobs.values()))
        assert job.status == DownloadStatus.PENDING
        assert window.table.item(0, 2).text() == "YouTube Music"
        assert window.table.item(0, 3).text() == "https://music.youtube.com/watch?v=abc"
    finally:
        window.close()
        app.processEvents()


def test_main_window_marks_soundcloud_source() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.url_input.setText("https://soundcloud.com/artist/track")
        window._add_url()

        assert window.table.rowCount() == 1
        assert window.table.item(0, 2).text() == "SoundCloud"
        assert window.table.item(0, 3).text() == "https://soundcloud.com/artist/track"
    finally:
        window.close()
        app.processEvents()
