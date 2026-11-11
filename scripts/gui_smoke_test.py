"""Offscreen GUI integration: button actions, actual displayed frames, close while playing."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

os.environ["QT_QPA_PLATFORM"] = "windows" if os.name == "nt" else "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from srs_manager.backend import read_lines
from srs_manager.gui import MainWindow
from srs_manager.media import ROOT, find_tool, parse_resolution


def main() -> int:
    source = next(ROOT.glob("*.mp4"), None)
    if source is None:
        raise ValueError("Place a test MP4 in the project root.")
    with socket.socket() as temporary:
        temporary.bind(("127.0.0.1", 0))
        port = temporary.getsockname()[1]
    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    with tempfile.TemporaryDirectory(prefix="srs-gui-") as directory:
        window = MainWindow(settings_path=Path(directory) / "settings.json")
        # Use native Windows fonts while preventing any visible desktop window.
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        assert window.fps.value() == 30
        assert parse_resolution(window.resolution.currentText()) is None
        assert not window.start_publisher.isEnabled()
        window.ip.setCurrentText("127.0.0.1")
        window.port.setValue(port)
        window.file.setText(str(source))
        window.resolution.setCurrentText("1280x720")
        window.fps.setValue(30)
        window.bitrate.setValue(4)
        window.show()
        phase = [0]
        errors = []
        deadline = time.monotonic() + 60
        relay = [None]
        readers = []

        def step() -> None:
            try:
                if time.monotonic() > deadline:
                    raise AssertionError(f"GUI validation timeout, phase={phase[0]}")
                manager = window.manager
                if phase[0] == 0:
                    window.start_server.click()
                    phase[0] = 1
                elif phase[0] == 1 and manager.service.snapshot().ready:
                    window._refresh()
                    window.start_publisher.click()
                    phase[0] = 2
                elif phase[0] == 2 and window.source_pane.pixmap is not None:
                    ffmpeg = find_tool("ffmpeg")
                    relay[0] = manager.registry.spawn([ffmpeg, "-hide_banner", "-nostats", "-nostdin",
                        "-rw_timeout", "5000000", "-i", f"rtmp://127.0.0.1:{port}/live/testsrc2",
                        "-an", "-c:v", "copy", "-f", "flv", f"rtmp://127.0.0.1:{port}/live/alertgateway"])
                    for pipe in (relay[0].stdout, relay[0].stderr):
                        thread = threading.Thread(target=read_lines, args=(pipe, lambda line: manager.logs.write("GUI模拟板端", line)), daemon=True)
                        thread.start()
                        readers.append(thread)
                    phase[0] = 3
                elif phase[0] == 3 and window.result_pane.pixmap is not None and window.result_pane.display_fps > 0:
                    assert manager.source.snapshot().size == "1280x720"
                    assert manager.result.snapshot().size == "1280x720"
                    assert not window.port.isEnabled()
                    assert not window.resolution.isEnabled()
                    assert window.stop_publisher.isEnabled()
                    assert window.source_pane.display_fps > 0
                    screenshot = ROOT / "runtime" / "gui-preview.png"
                    assert window.grab().save(str(screenshot))
                    print(f"PASS: GUI buttons, independent displayed frames, dimensions and FPS; screenshot={screenshot}", flush=True)
                    manager.registry.stop(relay[0])
                    for reader in readers:
                        reader.join(timeout=2)
                    manager.registry.release(relay[0])
                    relay[0] = None
                    phase[0] = 4
                    timer.stop()
                    window.close()
            except Exception as exc:
                errors.append(exc)
                timer.stop()
                window.close()

        timer = QTimer()
        timer.timeout.connect(step)
        timer.start(100)
        app.exec()
        assert window._closed.is_set()
        assert window.manager.registry.active_pids == []
        assert not window.manager.server_worker.is_alive()
        assert not window.manager.publisher_worker.is_alive()
        assert not any(worker.is_alive() for worker in window.manager.previews)
        if errors:
            raise errors[0]
        assert phase[0] == 4
        print("PASS: closing the playing GUI cleans up its processes and worker threads", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
