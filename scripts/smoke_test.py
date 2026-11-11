"""Real SRS/FFmpeg integration test. Simulated board relays video; no detection claims."""

from __future__ import annotations

import argparse
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from srs_manager.backend import Manager, read_lines
from srs_manager.media import PublishOptions, ROOT, find_tool, stream_url
import threading


def wait_for(predicate, description: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            print(f"PASS: {description}", flush=True)
            return
        time.sleep(0.1)
    raise AssertionError(f"Timeout: {description}; see runtime/logs/manager.log")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    source = args.file or next(ROOT.glob("*.mp4"), None)
    if source is None:
        parser.error("Supply --file VIDEO")
    port = args.port
    if not port:
        with socket.socket() as temporary:
            temporary.bind(("127.0.0.1", 0))
            port = temporary.getsockname()[1]
    manager = Manager()
    ffmpeg, ffprobe, srs = (find_tool(name) for name in ("ffmpeg", "ffprobe", "srs"))
    print(f"Testing with {source.name}; RTMP port {port}", flush=True)
    relay = None
    relay_readers = []
    try:
        manager.start_service(srs, ffmpeg, port)
        wait_for(lambda: manager.service.snapshot().ready, "SRS ready", 15)
        options = PublishOptions(str(source.resolve()), stream_url("127.0.0.1", port, "live/testsrc2"),
                                 (640, 360), 25, 2)
        manager.start_publish(ffmpeg, ffprobe, options)
        wait_for(lambda: manager.source.snapshot().frames > 10, "original preview receives decoded frames")
        assert manager.source.snapshot().size == "640x360", manager.source.snapshot()
        assert len(manager.source.frames) <= 1
        relay_command = [ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-rw_timeout", "5000000",
                         "-i", options.url, "-map", "0:v:0", "-an", "-c:v", "copy", "-f", "flv",
                         stream_url("127.0.0.1", port, "live/alertgateway")]

        def start_relay():
            process = manager.registry.spawn(relay_command)
            for pipe in (process.stdout, process.stderr):
                thread = threading.Thread(target=read_lines, args=(pipe, lambda line: manager.logs.write("模拟板端", line)), daemon=True)
                thread.start()
                relay_readers.append(thread)
            return process

        relay = start_relay()
        wait_for(lambda: manager.result.snapshot().frames > 10, "independent result preview receives frames")
        assert manager.result.snapshot().size == "640x360", manager.result.snapshot()
        manager.stop_publish()
        wait_for(lambda: not manager.publisher_worker.is_alive(), "publisher stops")
        assert manager.service.snapshot().ready, "Server must survive publisher disconnect"
        manager.registry.stop(relay)
        for reader in relay_readers:
            reader.join(timeout=2)
        manager.registry.release(relay)
        relay = None
        wait_for(lambda: not manager.source.snapshot().ready and not manager.result.snapshot().ready,
                 "both previews detect disconnected streams", 20)
        source_frames, result_frames = manager.source.snapshot().frames, manager.result.snapshot().frames
        manager.start_publish(ffmpeg, ffprobe, options)
        wait_for(lambda: manager.source.snapshot().frames > source_frames + 10, "original preview reconnects")
        relay = start_relay()
        wait_for(lambda: manager.result.snapshot().frames > result_frames + 10, "result preview reconnects")
        manager.registry.stop(relay)
        for reader in relay_readers:
            reader.join(timeout=2)
        manager.registry.release(relay)
        relay = None
        manager.stop_service()
        wait_for(lambda: not manager.stopping.is_set(), "stop service completes", 20)
        assert manager.registry.active_pids == [], manager.registry.active_pids
        print("PASS: no owned processes remain", flush=True)
        # Restarting the service must also recreate the independent preview workers.
        manager.start_service(srs, ffmpeg, port)
        wait_for(lambda: manager.service.snapshot().ready, "service restarts", 15)
    finally:
        if relay is not None:
            manager.registry.stop(relay)
            for reader in relay_readers:
                reader.join(timeout=2)
            manager.registry.release(relay)
        manager.close()
    assert manager.registry.active_pids == [], manager.registry.active_pids
    print("PASS: final cleanup; board decoding/detection still requires real-board acceptance", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
