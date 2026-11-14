"""Threaded process supervision. No GUI objects or unbounded frame events here."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import re
import socket
import subprocess
import threading
import time
from typing import Callable

from .media import (ROOT, RESULT_PATH, SOURCE_PATH, PublishOptions, VideoInfo,
                    preview_command, probe_command, publish_command, read_ppm,
                    srs_config, stream_url)
from .processes import ProcessRegistry


class LogBus:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.queue: queue.Queue[str] = queue.Queue(maxsize=1000)
        self.logger = logging.Logger(f"srs-manager-{id(self)}", logging.INFO)
        handler = RotatingFileHandler(directory / "manager.log", maxBytes=5_000_000,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.logger.addHandler(handler)

    def write(self, source: str, message: str) -> None:
        message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message).strip()
        if not message:
            return
        text = f"[{source}] {message}"
        self.logger.info(text)
        line = f"{time.strftime('%H:%M:%S')} {text}"
        try:
            self.queue.put_nowait(line)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(line)
            except queue.Full:
                pass

    def close(self) -> None:
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)


@dataclass
class Status:
    text: str = "未启动"
    ready: bool = False
    size: str = "—"
    frames: int = 0
    mode: str = ""
    last_frame: float = 0


class Channel:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status = Status()
        self.frames: deque[tuple[int, int, bytes]] = deque(maxlen=1)

    def update(self, **changes) -> None:
        with self.lock:
            for name, value in changes.items():
                setattr(self.status, name, value)

    def snapshot(self) -> Status:
        with self.lock:
            return replace(self.status)

    def put(self, frame: tuple[int, int, bytes]) -> None:
        with self.lock:
            self.frames.append(frame)
            self.status.frames += 1
            self.status.last_frame = time.monotonic()
            self.status.ready = True
            self.status.text = "播放中"

    def take(self) -> tuple[int, int, bytes] | None:
        with self.lock:
            return self.frames.pop() if self.frames else None

    def clear(self) -> None:
        with self.lock:
            self.frames.clear()
            self.status.ready = False


def read_lines(pipe, consumer: Callable[[str], None]) -> None:
    try:
        while raw := pipe.readline(8192):
            consumer(raw.decode("utf-8", errors="replace").strip())
    except (OSError, ValueError):
        pass  # Closing an owned process also closes its pipes.


class Worker(threading.Thread):
    def __init__(self, registry: ProcessRegistry, logs: LogBus, name: str, channel: Channel) -> None:
        super().__init__(name=name, daemon=True)
        self.registry, self.logs, self.channel = registry, logs, channel
        self.stop_event = threading.Event()
        self.process: subprocess.Popen | None = None

    def log(self, message: str) -> None:
        self.logs.write(self.name, message)

    def request_stop(self) -> None:
        self.stop_event.set()

    def drain(self, pipe, consumer: Callable[[str], None] | None = None) -> threading.Thread:
        thread = threading.Thread(target=read_lines, args=(pipe, consumer or self.log), daemon=True)
        thread.start()
        return thread

    def dispose(self, readers: list[threading.Thread], graceful: bool = False) -> None:
        if self.process:
            self.registry.stop(self.process, graceful=graceful)
            for reader in readers:
                reader.join(timeout=2)
            self.registry.release(self.process)
            self.process = None


def check_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as exc:
            raise ValueError(f"端口 {port} 被占用或无法绑定，请先处理端口冲突。") from exc


class ServiceWorker(Worker):
    def __init__(self, registry, logs, channel, executable: str, port: int,
                 on_ready: Callable[[], None], on_exit: Callable[[], None],
                 bind_host: str = "0.0.0.0") -> None:
        super().__init__(registry, logs, "RTMP服务", channel)
        self.executable, self.port = executable, port
        self.bind_host = bind_host
        self.on_ready, self.on_exit = on_ready, on_exit

    def run(self) -> None:
        readers = []
        try:
            self.channel.update(text="启动中", ready=False)
            check_port_available(self.port)
            runtime = ROOT / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            config = runtime / "srs.conf"
            config.write_text(srs_config(self.port, self.bind_host), encoding="utf-8")
            executable = Path(self.executable)
            cwd = executable.parent.parent if executable.parent.name == "objs" else executable.parent
            if self.stop_event.is_set():
                return
            self.process = self.registry.spawn([str(executable), "-c", str(config)], cwd=cwd)
            readers = [self.drain(self.process.stdout), self.drain(self.process.stderr)]
            deadline = time.monotonic() + 12
            while not self.stop_event.is_set():
                if self.process.poll() is not None:
                    raise RuntimeError(f"服务退出，退出码 {self.process.returncode}，请查看服务日志。")
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                        pass
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("服务启动超时，未检测到 RTMP 监听端口。")
                    self.stop_event.wait(0.1)
            if not self.stop_event.is_set():
                self.channel.update(text=f"运行中 · {self.bind_host}:{self.port}", ready=True)
                self.log(f"服务已就绪，PID={self.process.pid}，监听 {self.bind_host}:{self.port}")
                self.on_ready()
                while not self.stop_event.wait(0.2):
                    if self.process.poll() is not None:
                        raise RuntimeError(f"服务意外退出，退出码 {self.process.returncode}")
        except Exception as exc:
            self.channel.update(text=f"启动/运行失败：{exc}", ready=False)
            self.log(str(exc))
        finally:
            self.channel.update(ready=False)
            self.on_exit()
            self.dispose(readers)
            if self.stop_event.is_set():
                self.channel.update(text="已停止", ready=False)


class PublisherWorker(Worker):
    def __init__(self, registry, logs, channel, ffmpeg: str, ffprobe: str,
                 options: PublishOptions) -> None:
        super().__init__(registry, logs, "输入发布", channel)
        self.ffmpeg, self.ffprobe, self.options = ffmpeg, ffprobe, options

    def run(self) -> None:
        readers = []
        try:
            self.channel.update(text="检查视频中", ready=False, mode="")
            if not Path(self.options.file).is_file():
                raise ValueError("请选择存在的本地视频文件。")
            if self.stop_event.is_set():
                return
            self.process = self.registry.spawn(probe_command(self.ffprobe, self.options.file))
            try:
                output, error = self.process.communicate(timeout=20)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("读取本地视频信息超时。") from exc
            if self.stop_event.is_set():
                return
            if self.process.returncode:
                raise ValueError(error.decode("utf-8", errors="replace")[-2000:])
            info = VideoInfo.from_probe(output.decode("utf-8"))
            self.registry.release(self.process)
            self.process = None
            command, mode = publish_command(self.ffmpeg, self.options, info)
            self.log(f"源视频：{info.codec}，{info.width}x{info.height}，{info.fps:g} fps；{mode}")
            size = self.options.resolution or ((info.height, info.width) if info.rotation in (90, 270)
                                                else (info.width, info.height))
            self.channel.update(text="正在发布", mode=mode, size=f"{size[0]}x{size[1]}")
            if self.stop_event.is_set():
                return
            self.process = self.registry.spawn(command, stdin=True)

            def progress(line: str) -> None:
                if line.startswith("out_time_us="):
                    try:
                        if int(line.split("=", 1)[1]) > 0:
                            self.channel.update(text="推流中", ready=True)
                    except ValueError:
                        pass
                if line.startswith("speed="):
                    self.channel.update(mode=f"{mode} · speed={line.split('=', 1)[1].strip()}")

            readers = [self.drain(self.process.stdout, progress), self.drain(self.process.stderr)]
            self.log(f"正常速度循环发布到 {self.options.url}；恢复推流时从文件开头播放。")
            while not self.stop_event.wait(0.2):
                if self.process.poll() is not None:
                    raise RuntimeError(f"发布进程退出，退出码 {self.process.returncode}；可点击开始推流重试。")
        except Exception as exc:
            self.channel.update(text=f"发布失败：{exc}", ready=False)
            self.log(str(exc))
        finally:
            self.dispose(readers, graceful=True)
            self.channel.update(ready=False)
            if self.stop_event.is_set():
                self.channel.update(text="已停止")


class PreviewWorker(Worker):
    def __init__(self, registry, logs, channel, name: str, ffmpeg: str, url: str) -> None:
        super().__init__(registry, logs, name, channel)
        self.ffmpeg, self.url = ffmpeg, url

    def run(self) -> None:
        retry = 1.0
        try:
            while not self.stop_event.is_set():
                readers = []
                watchdog_done = threading.Event()
                got_frame = False
                try:
                    self.channel.clear()
                    self.channel.update(text="连接中 / 等待发布", size="—")
                    self.process = self.registry.spawn(preview_command(self.ffmpeg, self.url))
                    process = self.process
                    activity = [time.monotonic()]
                    input_section = [True]

                    def stderr(line: str) -> None:
                        if line.startswith("Output #"):
                            input_section[0] = False
                        if input_section[0] and "Video:" in line:
                            match = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
                            if match:
                                self.channel.update(size=f"{match[1]}x{match[2]}")
                        self.log(line)

                    def watchdog() -> None:
                        while not watchdog_done.wait(0.25):
                            if self.stop_event.is_set() or time.monotonic() - activity[0] > 10:
                                if not self.stop_event.is_set():
                                    self.log("10 秒未收到可显示图像，重新连接。")
                                self.registry.stop(process, timeout=0.5)
                                return

                    readers = [self.drain(process.stderr, stderr)]
                    watcher = threading.Thread(target=watchdog, daemon=True)
                    watcher.start()
                    while not self.stop_event.is_set():
                        frame = read_ppm(process.stdout)
                        if self.stop_event.is_set():
                            break
                        activity[0] = time.monotonic()
                        self.channel.put(frame)
                        if not got_frame:
                            self.log(f"已收到画面：{self.url}")
                        got_frame = True
                except (EOFError, OSError, ValueError, RuntimeError) as exc:
                    if not self.stop_event.is_set():
                        self.log(f"拉流中断/尚未发布：{exc}")
                finally:
                    watchdog_done.set()
                    if "watcher" in locals():
                        watcher.join(timeout=2)
                    self.dispose(readers)
                    self.channel.clear()
                if self.stop_event.is_set():
                    break
                retry = 1.0 if got_frame else min(retry * 2, 5.0)
                self.channel.update(text=f"断线 / 等待发布 · {retry:g} 秒后重连", ready=False)
                self.stop_event.wait(retry)
        finally:
            self.channel.clear()
            self.channel.update(text="已停止", ready=False)


class Manager:
    def __init__(self) -> None:
        self.logs = LogBus(ROOT / "runtime" / "logs")
        self.registry = ProcessRegistry()
        self.service = Channel()
        self.publisher = Channel()
        self.source = Channel()
        self.result = Channel()
        self.server_worker: ServiceWorker | None = None
        self.publisher_worker: PublisherWorker | None = None
        self.previews: list[PreviewWorker] = []
        self.port = 1935
        self.ffmpeg = ""
        self._lock = threading.RLock()
        self.stopping = threading.Event()
        self.closing = threading.Event()
        self.cleanup_thread: threading.Thread | None = None

    def start_service(self, executable: str, ffmpeg: str, port: int, bind_host: str = "0.0.0.0") -> None:
        with self._lock:
            if self.closing.is_set() or self.stopping.is_set():
                raise ValueError("正在停止，请稍候。")
            if self.server_worker and self.server_worker.is_alive():
                raise ValueError("服务已启动。")
            if any(worker.is_alive() for worker in self.previews):
                raise ValueError("旧预览尚未结束，请稍后重新启动。")
            self.port, self.ffmpeg = port, ffmpeg
            self.server_worker = ServiceWorker(self.registry, self.logs, self.service,
                                               executable, port, self._ready, self._server_exit, bind_host)
            self.server_worker.start()

    def _ready(self) -> None:
        with self._lock:
            if self.stopping.is_set() or self.closing.is_set():
                return
            self.previews = [
                PreviewWorker(self.registry, self.logs, self.source, "原始预览", self.ffmpeg,
                              stream_url("127.0.0.1", self.port, SOURCE_PATH)),
                PreviewWorker(self.registry, self.logs, self.result, "结果预览", self.ffmpeg,
                              stream_url("127.0.0.1", self.port, RESULT_PATH)),
            ]
            for worker in self.previews:
                worker.start()

    def _server_exit(self) -> None:
        # A server crash must not leave publishers/decoders running against a dead server.
        with self._lock:
            for worker in [self.publisher_worker, *self.previews]:
                if worker:
                    worker.request_stop()

    def start_publish(self, ffmpeg: str, ffprobe: str, options: PublishOptions) -> None:
        with self._lock:
            if self.closing.is_set() or self.stopping.is_set() or not self.service.snapshot().ready:
                raise ValueError("请先启动 RTMP 服务并等待就绪。")
            if self.publisher_worker and self.publisher_worker.is_alive():
                raise ValueError("输入正在发布，请先停止推流。")
            self.publisher_worker = PublisherWorker(self.registry, self.logs, self.publisher,
                                                     ffmpeg, ffprobe, options)
            self.publisher_worker.start()

    def stop_publish(self) -> None:
        # This request is nonblocking. The publisher sends 'q' in its own finally block.
        with self._lock:
            if self.publisher_worker:
                self.publisher_worker.request_stop()
                self.publisher.update(text="正在停止", ready=False)
                # ffprobe may be blocked in communicate(); terminate it off the UI thread.
                worker = self.publisher_worker
                if worker.process and "ffprobe" in str(worker.process.args[0]).lower():
                    threading.Thread(target=self.registry.stop, args=(worker.process,), daemon=True).start()

    def stop_service(self) -> None:
        with self._lock:
            if self.stopping.is_set():
                return
            self.stopping.set()
            self.service.update(text="正在停止", ready=False)
            self.cleanup_thread = threading.Thread(target=self._stop_all, name="进程清理", daemon=True)
            self.cleanup_thread.start()

    def _stop_all(self) -> None:
        try:
            with self._lock:
                workers = [self.publisher_worker, *self.previews]
                server = self.server_worker
                for worker in workers:
                    if worker:
                        worker.request_stop()
            for worker in workers:
                if worker:
                    worker.join(timeout=3)
                    if worker.is_alive() and worker.process:
                        self.registry.stop(worker.process, timeout=0.5)
                        worker.join(timeout=2)
            if server:
                server.request_stop()
                server.join(timeout=4)
                if server.is_alive() and server.process:
                    self.registry.stop(server.process, timeout=0.5)
                    server.join(timeout=2)
            self.service.update(text="已停止", ready=False)
        finally:
            self.stopping.clear()

    def close(self) -> None:
        self.closing.set()
        self.stop_service()
        if self.cleanup_thread:
            self.cleanup_thread.join(timeout=15)
        self.registry.close()
        self.logs.close()
