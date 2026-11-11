from __future__ import annotations

import io
import json
import unittest

from srs_manager.backend import Channel
from srs_manager.media import (PublishOptions, VideoInfo, parse_resolution, publish_command,
                               read_ppm, stream_url)


class MediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.info = VideoInfo("h264", 1920, 1080, 30, "yuv420p")

    def command(self, **changes):
        options = PublishOptions("E:/带空格/video clip.mp4", "rtmp://127.0.0.1:1935/live/testsrc2", **changes)
        return publish_command("ffmpeg", options, self.info)[0]

    def test_compatible_h264_can_copy_but_changed_size_or_rate_must_encode(self):
        self.assertIn("copy", self.command())
        for change in ({"resolution": (1280, 720)}, {"fps": 25}, {"fps": 29.97}):
            with self.subTest(change=change):
                self.assertIn("libx264", self.command(**change))
                self.assertNotIn("copy", self.command(**change))

    def test_rotation_and_incompatible_pixel_format_must_encode(self):
        for info in (VideoInfo("hevc", 3840, 2160, 30, "yuv420p"),
                     VideoInfo("h264", 1920, 1080, 30, "yuv420p10le"),
                     VideoInfo("h264", 1920, 1080, 30, "yuv420p", 90)):
            with self.subTest(info=info):
                self.info = info
                self.assertIn("libx264", self.command())

    def test_preserve_source_rate_is_explicit(self):
        self.info = VideoInfo("h264", 1920, 1080, 23.976, "yuv420p")
        self.assertIn("copy", self.command(fps=None))
        self.assertIn("libx264", self.command())

    def test_parse_probe_fractional_rate_and_rotation(self):
        info = VideoInfo.from_probe(json.dumps({"streams": [{"codec_name": "h264", "width": 1920,
            "height": 1080, "avg_frame_rate": "30000/1001", "pix_fmt": "yuv420p",
            "side_data_list": [{"rotation": -90}]}]}))
        self.assertAlmostEqual(info.fps, 29.97002997)
        self.assertEqual(info.rotation, 270)

    def test_invalid_dimensions_and_advertised_address_are_rejected(self):
        for value in ("1921x1080", "0x0", "3840", "9000x1080", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_resolution(value)
        self.assertEqual(parse_resolution("3840×2160"), (3840, 2160))
        for ip in ("0.0.0.0", "224.1.1.1", "255.255.255.255", "bad"):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                stream_url(ip, 1935, "live/testsrc2")

    def test_ppm_partial_reads_and_binary_whitespace_do_not_lose_pixels(self):
        class Fragmented(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 2))
        pixels = b"\n \t\x00\xff\x01"
        stream = Fragmented(b"P6\n# comment\n2 1\n255\n" + pixels + b"P6\n1 1\n255\nabc")
        self.assertEqual(read_ppm(stream), (2, 1, pixels))
        self.assertEqual(read_ppm(stream), (1, 1, b"abc"))

    def test_ppm_truncation_and_oversize_fail_without_allocating_large_frame(self):
        with self.assertRaises(EOFError):
            read_ppm(io.BytesIO(b"P6\n2 2\n255\na"))
        with self.assertRaises(ValueError):
            read_ppm(io.BytesIO(b"P6\n40000 40000\n255\n"))

    def test_slow_display_receives_only_newest_frame(self):
        channel = Channel()
        for value in range(1000):
            channel.put((1, 1, str(value).encode()))
        self.assertEqual(len(channel.frames), 1)
        self.assertEqual(channel.take(), (1, 1, b"999"))
        self.assertIsNone(channel.take())


if __name__ == "__main__":
    unittest.main()
