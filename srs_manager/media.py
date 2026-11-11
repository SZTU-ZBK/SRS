"""Pure command construction and bounded image-pipe parsing."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO
import ipaddress
import json
import os
import shutil
import sys


ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = "live/testsrc2"
RESULT_PATH = "live/alertgateway"


def find_tool(name: str, override: str = "") -> str:
    if override.strip():
        path = Path(override).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"找不到 {name}：{path}")
        return str(path)
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [
        ROOT / "tools" / "srs" / "objs" / f"{name}{suffix}",
        ROOT / "tools" / "srs" / f"{name}{suffix}",
        Path(sys.prefix) / "Library" / "bin" / f"{name}{suffix}",
        Path(sys.prefix) / "bin" / name,
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    found = shutil.which(name)
    if found:
        return found
    raise ValueError(f"找不到 {name}，请激活 Conda 环境或在界面中指定可执行文件。")


def validate_address(ip: str, port: int) -> str:
    try:
        address = ipaddress.IPv4Address(ip.strip())
    except ipaddress.AddressValueError as exc:
        raise ValueError("请输入电脑的局域网 IPv4 地址。") from exc
    if address.is_unspecified or address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
        raise ValueError("板端地址需要可访问的电脑 IP，不能填写 0.0.0.0 或广播/组播地址。")
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1～65535 之间。")
    return str(address)


def stream_url(ip: str, port: int, path: str) -> str:
    return f"rtmp://{validate_address(ip, port)}:{port}/{path}"


def parse_resolution(text: str) -> tuple[int, int] | None:
    text = text.strip().lower().replace("×", "x")
    if text in ("原始分辨率", "原始", "source", ""):
        return None
    try:
        width, height = (int(part.strip()) for part in text.split("x"))
    except (ValueError, TypeError) as exc:
        raise ValueError("分辨率请填写 宽x高，例如 1920x1080。") from exc
    if not (16 <= width <= 8192 and 16 <= height <= 8192):
        raise ValueError("宽、高需在 16～8192 之间。")
    if width % 2 or height % 2:
        raise ValueError("H.264 推流的宽、高请使用偶数。")
    return width, height


@dataclass(frozen=True)
class VideoInfo:
    codec: str
    width: int
    height: int
    fps: float
    pixel_format: str
    rotation: int = 0

    @classmethod
    def from_probe(cls, output: str) -> "VideoInfo":
        streams = json.loads(output).get("streams", [])
        if not streams:
            raise ValueError("文件中未找到视频轨道。")
        stream = streams[0]
        try:
            fps = float(Fraction(stream.get("avg_frame_rate", "0/1")))
        except (ValueError, ZeroDivisionError):
            fps = 0
        if fps <= 0:
            try:
                fps = float(Fraction(stream.get("r_frame_rate", "0/1")))
            except (ValueError, ZeroDivisionError):
                fps = 0
        rotation = int(float(stream.get("tags", {}).get("rotate", 0)))
        for side_data in stream.get("side_data_list", []):
            if "rotation" in side_data:
                rotation = int(float(side_data["rotation"]))
        width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
        if width <= 0 or height <= 0:
            raise ValueError("无法读取视频尺寸。")
        return cls(stream.get("codec_name", "unknown"), width, height, fps,
                   stream.get("pix_fmt", "unknown"), rotation % 360)


@dataclass(frozen=True)
class PublishOptions:
    file: str
    url: str
    resolution: tuple[int, int] | None = None
    fps: float | None = 30.0
    bitrate_mbps: float = 18.0

    def __post_init__(self) -> None:
        if self.fps is not None and not 1 <= self.fps <= 120:
            raise ValueError("推流帧率必须在 1～120 fps 之间。")
        if not 0.1 <= self.bitrate_mbps <= 100:
            raise ValueError("转码码率必须在 0.1～100 Mbps 之间。")
        if self.resolution:
            parse_resolution(f"{self.resolution[0]}x{self.resolution[1]}")


def probe_command(ffprobe: str, source: str) -> list[str]:
    return [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", source]


def publish_command(ffmpeg: str, options: PublishOptions, info: VideoInfo) -> tuple[list[str], str]:
    copy = (
        info.codec == "h264" and info.pixel_format == "yuv420p" and info.rotation == 0
        and info.width % 2 == 0 and info.height % 2 == 0
        and (options.resolution is None or options.resolution == (info.width, info.height))
        and (options.fps is None or abs(info.fps - options.fps) < 0.001)
    )
    command = [ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats",
               "-stream_loop", "-1", "-re", "-i", options.file, "-map", "0:v:0", "-an", "-sn", "-dn"]
    if copy:
        command += ["-c:v", "copy"]
        mode = "H.264 直接转发（保留源码率）"
    else:
        filters = []
        if options.resolution:
            width, height = options.resolution
            filters += [f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]
        else:
            filters += ["scale=ceil(iw/2)*2:ceil(ih/2)*2"]
        if options.fps is not None:
            filters.append(f"fps={options.fps:g}")
        rate = options.fps or info.fps or 30
        bitrate = f"{options.bitrate_mbps:g}M"
        command += ["-vf", ",".join(filters), "-c:v", "libx264", "-preset", "veryfast",
                    "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-profile:v", "high",
                    "-g", str(max(1, round(rate * 2))), "-keyint_min", str(max(1, round(rate))),
                    "-sc_threshold", "0", "-b:v", bitrate, "-maxrate", bitrate,
                    "-bufsize", f"{options.bitrate_mbps * 2:g}M"]
        mode = "H.264 转码"
    command += ["-progress", "pipe:1", "-f", "flv", "-flvflags", "no_duration_filesize", options.url]
    return command, mode


def preview_command(ffmpeg: str, url: str) -> list[str]:
    # PPM has a size header per frame: reconnects/resolution changes cannot corrupt framing.
    return [ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats", "-nostdin",
            "-rw_timeout", "5000000", "-rtmp_live", "live", "-rtmp_buffer", "100",
            "-i", url, "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", "scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=15",
            "-c:v", "ppm", "-pix_fmt", "rgb24", "-f", "image2pipe", "pipe:1"]


def read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("视频图像管道已关闭")
        chunks.extend(chunk)
    return bytes(chunks)


def read_ppm(stream: BinaryIO) -> tuple[int, int, bytes]:
    def token() -> bytes:
        value = bytearray()
        while True:
            char = read_exact(stream, 1)
            if char == b"#" and not value:
                # Bound malformed headers; never readline() an unbounded binary pipe.
                for _ in range(1024):
                    if read_exact(stream, 1) == b"\n":
                        break
                else:
                    raise ValueError("PPM 注释过长")
            elif char.isspace():
                if value:
                    return bytes(value)
            else:
                value.extend(char)
                if len(value) > 32:
                    raise ValueError("无效的 PPM 头")

    if token() != b"P6":
        raise ValueError("FFmpeg 未输出预期的 PPM 图像")
    width, height, maximum = int(token()), int(token()), int(token())
    if not (0 < width <= 960 and 0 < height <= 540 and maximum == 255):
        raise ValueError(f"无效的预览尺寸：{width}x{height}")
    return width, height, read_exact(stream, width * height * 3)


def srs_config(port: int) -> str:
    if not 1 <= port <= 65535:
        raise ValueError("无效服务端口")
    return f"""# Generated by SRS Manager. The advertised LAN IP is configured in the UI.
listen              0.0.0.0:{port};
max_connections     1000;
daemon              off;
srs_log_tank        console;
http_api {{
    enabled off;
}}
http_server {{
    enabled off;
}}
vhost __defaultVhost__ {{
    tcp_nodelay on;
    min_latency on;
    play {{
        gop_cache on;
        queue_length 2;
        mw_latency 100;
    }}
}}
"""
