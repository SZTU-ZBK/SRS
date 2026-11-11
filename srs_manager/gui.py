"""PySide6 desktop UI. Background channels are polled; frames never queue as signals."""

from __future__ import annotations

import json
from pathlib import Path
import queue
import socket
import sys
import threading
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont, QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox,
    QSplitter, QVBoxLayout, QWidget)

from .backend import Channel, Manager
from .media import ROOT, RESULT_PATH, SOURCE_PATH, PublishOptions, find_tool, parse_resolution, stream_url, validate_address


def local_ips() -> list[str]:
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        return sorted((ip for ip in addresses if not ip.startswith("127.")),
                      key=lambda ip: (ip.startswith("169.254."), ip)) or ["127.0.0.1"]
    except OSError:
        return ["127.0.0.1"]


class PreviewPane(QGroupBox):
    def __init__(self, title: str, channel: Channel) -> None:
        super().__init__(title)
        self.channel = channel
        self.image = QLabel("等待服务启动")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(280, 180)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.image.setStyleSheet("background:#111827; color:#a8b6cc; border-radius:6px;")
        self.status = QLabel("未启动 · 源尺寸 — · 显示 0.0 fps")
        self.status.setWordWrap(True)
        self.pixmap: QPixmap | None = None
        self.displayed = 0
        self.display_fps = 0.0
        self.fps_since = time.monotonic()
        layout = QVBoxLayout(self)
        layout.addWidget(self.image, 1)
        layout.addWidget(self.status)

    def refresh(self) -> None:
        status = self.channel.snapshot()
        frame = self.channel.take()
        if status.ready and frame:
            width, height, pixels = frame
            image = QImage(pixels, width, height, width * 3, QImage.Format.Format_RGB888).copy()
            self.pixmap = QPixmap.fromImage(image)
            self._render()
            self.displayed += 1
        elif not status.ready:
            self.pixmap = None
            self.image.setText(status.text)
        now = time.monotonic()
        if now - self.fps_since >= 1:
            self.display_fps = self.displayed / (now - self.fps_since)
            self.displayed = 0
            self.fps_since = now
        self.status.setText(f"{status.text} · 源尺寸 {status.size} · 显示 {self.display_fps:.1f} fps")

    def _render(self) -> None:
        if self.pixmap:
            self.image.setPixmap(self.pixmap.scaled(self.image.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._render()


class MainWindow(QMainWindow):
    def __init__(self, settings_path: Path | None = None) -> None:
        super().__init__()
        self.manager = Manager()
        self._closing = False
        self._closed = threading.Event()
        self._publisher_stopping = False
        self.settings_path = settings_path or ROOT / "runtime" / "settings.json"
        self.setWindowTitle("SRS 视频链路管理")
        self.resize(1220, 940)
        self.setMinimumSize(900, 740)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel("SRS 视频链路管理")
        title.setFont(QFont("Microsoft YaHei UI", 17, QFont.Weight.Bold))
        layout.addWidget(title)
        hint = QLabel("先启动服务并发布视频，确认原始画面后，再启动板端程序。")
        hint.setStyleSheet("color:#475569;")
        layout.addWidget(hint)

        config = QGroupBox("服务与输入发布")
        grid = QGridLayout(config)
        self.ip = QComboBox()
        self.ip.setEditable(True)
        self.ip.addItems(local_ips())
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(1935)
        self.file = QLineEdit()
        self.file.setPlaceholderText("选择本地视频，按正常播放速度循环发布（不含音频）")
        self.choose_file = QPushButton("选择视频…")
        self.choose_file.clicked.connect(self._choose_video)
        grid.addWidget(QLabel("电脑局域网 IP"), 0, 0)
        grid.addWidget(self.ip, 0, 1)
        grid.addWidget(QLabel("服务端口"), 0, 2)
        grid.addWidget(self.port, 0, 3)
        grid.addWidget(QLabel("本地视频"), 1, 0)
        grid.addWidget(self.file, 1, 1, 1, 3)
        grid.addWidget(self.choose_file, 1, 4)

        self.resolution = QComboBox()
        self.resolution.setEditable(True)
        self.resolution.addItems(["原始分辨率", "640x360", "1280x720", "1920x1080", "2560x1440", "3840x2160"])
        self.resolution.setToolTip("可直接填写宽x高。改变尺寸时转码，保持比例并补黑边。")
        self.fps = QDoubleSpinBox()
        self.fps.setRange(1, 120)
        self.fps.setDecimals(3)
        self.fps.setValue(30)
        self.fps.setSuffix(" fps")
        self.source_fps = QCheckBox("保持源帧率")
        self.source_fps.toggled.connect(lambda checked: self.fps.setEnabled(not checked))
        self.bitrate = QDoubleSpinBox()
        self.bitrate.setRange(0.1, 100)
        self.bitrate.setValue(18)
        self.bitrate.setSuffix(" Mbps")
        self.bitrate.setToolTip("仅转码模式生效；直接转发保留视频原码率。")
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.fps)
        rate_row.addWidget(self.source_fps)
        grid.addWidget(QLabel("推流分辨率"), 2, 0)
        grid.addWidget(self.resolution, 2, 1)
        grid.addWidget(QLabel("推流帧率"), 2, 2)
        grid.addLayout(rate_row, 2, 3)
        bitrate_row = QHBoxLayout()
        bitrate_row.addWidget(QLabel("转码码率"))
        bitrate_row.addWidget(self.bitrate)
        grid.addLayout(bitrate_row, 2, 4)
        layout.addWidget(config)

        paths = QGroupBox("程序路径（留空自动查找 Conda 环境和 tools/srs）")
        paths_layout = QGridLayout(paths)
        self.tool_paths: dict[str, QLineEdit] = {}
        self.tool_buttons: list[QPushButton] = []
        for row, (name, label) in enumerate((("srs", "SRS"), ("ffmpeg", "FFmpeg"), ("ffprobe", "ffprobe"))):
            edit = QLineEdit()
            edit.setPlaceholderText(f"自动查找 {name}.exe")
            self.tool_paths[name] = edit
            button = QPushButton("浏览…")
            self.tool_buttons.append(button)
            button.clicked.connect(lambda checked=False, key=name: self._choose_tool(key))
            paths_layout.addWidget(QLabel(label), row, 0)
            paths_layout.addWidget(edit, row, 1)
            paths_layout.addWidget(button, row, 2)
        layout.addWidget(paths)

        buttons = QHBoxLayout()
        self.start_server = QPushButton("启动服务")
        self.stop_server = QPushButton("停止服务")
        self.start_publisher = QPushButton("开始推流")
        self.stop_publisher = QPushButton("停止推流")
        self.start_server.clicked.connect(self._start_service)
        self.stop_server.clicked.connect(self.manager.stop_service)
        self.start_publisher.clicked.connect(self._start_publish)
        self.stop_publisher.clicked.connect(self._stop_publish)
        for button in (self.start_server, self.stop_server, self.start_publisher, self.stop_publisher):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.service_status = QLabel()
        self.publish_status = QLabel()
        self.publish_status.setWordWrap(True)
        layout.addWidget(self.service_status)
        layout.addWidget(self.publish_status)

        addresses = QGroupBox("板端配置地址")
        address_grid = QGridLayout(addresses)
        self.source_url = QLineEdit()
        self.result_url = QLineEdit()
        for row, (label, edit) in enumerate((("板端输入", self.source_url), ("板端输出", self.result_url))):
            edit.setReadOnly(True)
            button = QPushButton("复制")
            button.clicked.connect(lambda checked=False, field=edit: QApplication.clipboard().setText(field.text()))
            address_grid.addWidget(QLabel(label), row, 0)
            address_grid.addWidget(edit, row, 1)
            address_grid.addWidget(button, row, 2)
        layout.addWidget(addresses)
        self.ip.currentTextChanged.connect(self._update_addresses)
        self.port.valueChanged.connect(self._update_addresses)

        splitter = QSplitter(Qt.Orientation.Vertical)
        previews = QWidget()
        preview_layout = QHBoxLayout(previews)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.source_pane = PreviewPane("原始视频 · /live/testsrc2", self.manager.source)
        self.result_pane = PreviewPane("检测结果 · /live/alertgateway", self.manager.result)
        preview_layout.addWidget(self.source_pane)
        preview_layout.addWidget(self.result_pane)
        splitter.addWidget(previews)
        logs_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(logs_group)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(2000)
        self.logs.setMinimumHeight(80)
        self.logs.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.logs)
        log_buttons = QHBoxLayout()
        clear = QPushButton("清空显示")
        clear.clicked.connect(self.logs.clear)
        open_logs = QPushButton("打开日志目录")
        open_logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ROOT / "runtime" / "logs"))))
        log_buttons.addStretch()
        log_buttons.addWidget(clear)
        log_buttons.addWidget(open_logs)
        log_layout.addLayout(log_buttons)
        splitter.addWidget(logs_group)
        splitter.setSizes([360, 150])
        layout.addWidget(splitter, 1)
        self.setCentralWidget(root)
        self._load_settings()
        self._update_addresses()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(50)
        self._refresh()
        self.manager.logs.write("管理界面", "预览独立解码，显示上限 15 fps，每路仅保留最新 1 帧；推流帧率由上方配置决定。")
        for tool in ("srs", "ffmpeg", "ffprobe"):
            try:
                self.manager.logs.write("依赖检查", f"{tool}: {self._tool(tool)}")
            except ValueError as exc:
                self.manager.logs.write("依赖检查", str(exc))

    def _tool(self, name: str) -> str:
        return find_tool(name, self.tool_paths[name].text())

    def _choose_video(self) -> None:
        file, _ = QFileDialog.getOpenFileName(self, "选择本地视频", str(ROOT),
            "视频文件 (*.mp4 *.mkv *.mov *.avi *.flv *.ts *.m4v *.webm);;所有文件 (*)")
        if file:
            self.file.setText(file)

    def _choose_tool(self, name: str) -> None:
        file, _ = QFileDialog.getOpenFileName(self, f"选择 {name}", str(ROOT), "程序 (*.exe);;所有文件 (*)")
        if file:
            self.tool_paths[name].setText(file)

    def _error(self, error: Exception) -> None:
        self.manager.logs.write("操作失败", str(error))
        QMessageBox.warning(self, "无法执行", str(error))

    def _start_service(self) -> None:
        try:
            validate_address(self.ip.currentText(), self.port.value())
            executable, ffmpeg = self._tool("srs"), self._tool("ffmpeg")
            self._tool("ffprobe")
            self._save_settings()
            self.manager.start_service(executable, ffmpeg, self.port.value())
            self._refresh()
        except (ValueError, OSError) as exc:
            self._error(exc)

    def _start_publish(self) -> None:
        try:
            file = Path(self.file.text().strip()).expanduser()
            if not self.file.text().strip() or not file.is_file():
                raise ValueError("请先选择存在的本地视频。")
            options = PublishOptions(str(file.resolve()), stream_url("127.0.0.1", self.manager.port, SOURCE_PATH),
                parse_resolution(self.resolution.currentText()), None if self.source_fps.isChecked() else self.fps.value(),
                self.bitrate.value())
            self._save_settings()
            self._publisher_stopping = False
            self.manager.start_publish(self._tool("ffmpeg"), self._tool("ffprobe"), options)
            self._refresh()
        except (ValueError, OSError) as exc:
            self._error(exc)

    def _stop_publish(self) -> None:
        self._publisher_stopping = True
        self.manager.stop_publish()
        self._refresh()

    def _update_addresses(self, *args) -> None:
        try:
            self.source_url.setText(stream_url(self.ip.currentText(), self.port.value(), SOURCE_PATH))
            self.result_url.setText(stream_url(self.ip.currentText(), self.port.value(), RESULT_PATH))
        except ValueError:
            self.source_url.clear()
            self.result_url.clear()

    def _refresh(self) -> None:
        service = self.manager.service.snapshot()
        publisher = self.manager.publisher.snapshot()
        server_alive = bool(self.manager.server_worker and self.manager.server_worker.is_alive())
        publisher_alive = bool(self.manager.publisher_worker and self.manager.publisher_worker.is_alive())
        busy = self.manager.stopping.is_set() or self._closing
        self.start_server.setEnabled(not server_alive and not busy)
        self.stop_server.setEnabled(server_alive and not busy)
        self.start_publisher.setEnabled(service.ready and not publisher_alive and not busy)
        self.stop_publisher.setEnabled(publisher_alive and not self._publisher_stopping and not busy)
        self.ip.setEnabled(not server_alive and not busy)
        self.port.setEnabled(not server_alive and not busy)
        for field in (self.file, self.choose_file, self.resolution, self.source_fps, self.bitrate):
            field.setEnabled(not publisher_alive and not busy)
        self.fps.setEnabled(not publisher_alive and not busy and not self.source_fps.isChecked())
        for field in self.tool_paths.values():
            field.setEnabled(not server_alive and not busy)
        for button in self.tool_buttons:
            button.setEnabled(not server_alive and not busy)
        self.service_status.setText(f"RTMP 服务：{service.text}")
        self.publish_status.setText(f"输入发布：{publisher.text}  {publisher.size}  {publisher.mode}")
        self.source_pane.refresh()
        self.result_pane.refresh()
        lines = []
        for _ in range(150):
            try:
                lines.append(self.manager.logs.queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.logs.appendPlainText("\n".join(lines))
        if self._closing and self._closed.is_set():
            self.close()

    def _load_settings(self) -> None:
        if not self.settings_path.exists():
            return
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
            self.ip.setCurrentText(settings.get("ip", self.ip.currentText()))
            self.port.setValue(int(settings.get("port", 1935)))
            self.file.setText(str(settings.get("file", "")))
            self.resolution.setCurrentText(str(settings.get("resolution", "原始分辨率")))
            self.fps.setValue(float(settings.get("fps", 30)))
            self.source_fps.setChecked(bool(settings.get("source_fps", False)))
            self.bitrate.setValue(float(settings.get("bitrate_mbps", 18)))
            for name, field in self.tool_paths.items():
                field.setText(str(settings.get(name, "")))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.manager.logs.write("配置", f"上次配置无法读取，使用默认值：{exc}")

    def _save_settings(self) -> None:
        settings = {"ip": self.ip.currentText(), "port": self.port.value(), "file": self.file.text(),
                    "resolution": self.resolution.currentText(), "fps": self.fps.value(),
                    "source_fps": self.source_fps.isChecked(), "bitrate_mbps": self.bitrate.value(),
                    **{name: field.text() for name, field in self.tool_paths.items()}}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.settings_path)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closed.is_set():
            self.timer.stop()
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        try:
            self._save_settings()
        except OSError as exc:
            self.manager.logs.write("配置", f"保存失败：{exc}")
        self.manager.logs.write("管理界面", "正在停止本程序创建的进程并释放预览线程…")
        self.setWindowTitle("SRS 视频链路管理 · 正在清理进程")

        def cleanup() -> None:
            try:
                self.manager.close()
            finally:
                self._closed.set()

        threading.Thread(target=cleanup, daemon=True).start()
        self._refresh()


def main() -> int:
    application = QApplication(sys.argv)
    application.setStyle("Fusion")
    application.setFont(QFont("Microsoft YaHei UI", 9))
    window = MainWindow()
    application.aboutToQuit.connect(window.manager.close)
    window.show()
    return application.exec()
