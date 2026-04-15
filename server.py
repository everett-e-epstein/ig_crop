from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8000
EXPORT_PRESETS = {
    "square": {"width": 1080, "height": 1080, "suffix": "square-1x1"},
    "feed": {"width": 1080, "height": 1350, "suffix": "feed-4x5"},
    "story": {"width": 1080, "height": 1920, "suffix": "story-9x16"},
}


def build_cover_crop_filter(width: int, height: int) -> str:
    # Scale to cover, then center-crop (plain crop=w:h defaults to top-left).
    # setsar=1 avoids odd SAR; fps=30 forces CFR (QuickTime often breaks on VFR screen captures).
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2,setsar=1,fps=30"
    )


_FF_ENCODERS: str | None = None


def _ffmpeg_encoder_list() -> str:
    global _FF_ENCODERS
    if _FF_ENCODERS is None:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        _FF_ENCODERS = (r.stdout or "") + (r.stderr or "")
    return _FF_ENCODERS


def _ffmpeg_has_encoder(name: str) -> bool:
    return name in _ffmpeg_encoder_list()


def _build_convert_commands(
    input_path: Path, output_path: Path, width: int, height: int
) -> list[list[str]]:
    """Try Apple VideoToolbox first on macOS, then libx264.

    QuickTime Player is picky: video-only VFR MP4s from WebM often fail. We force CFR
    (fps=30), add silent AAC stereo, -shortest, explicit -f mp4, +faststart.
    """
    vf = build_cover_crop_filter(width, height)
    head = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        vf,
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-shortest",
    ]
    audio = [
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
    ]
    mux = [
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output_path),
    ]

    cmds: list[list[str]] = []

    if platform.system() == "Darwin" and _ffmpeg_has_encoder("h264_videotoolbox"):
        # Constant bitrate tends to produce streams QuickTime accepts more reliably than -q:v.
        cmds.append(
            head
            + [
                "-c:v",
                "h264_videotoolbox",
                "-allow_sw",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                "12M",
                "-maxrate",
                "14M",
                "-bufsize",
                "28M",
                "-realtime",
                "0",
            ]
            + audio
            + mux
        )

    # Baseline-like compatibility: no B-frames, avc1 tag, AUD; silent AAC included.
    cmds.append(
        head
        + [
            "-c:v",
            "libx264",
            "-profile:v",
            "baseline",
            "-level",
            "5.1",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "20",
            "-preset",
            "medium",
            "-bf",
            "0",
            "-refs",
            "1",
            "-x264-params",
            "aud=1",
            "-tag:v",
            "avc1",
        ]
        + audio
        + mux
    )

    return cmds


class SiteHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/__video_export_server__":
            body = b'{"videoExportServer":true}'
            self.send_response(HTTPStatus.OK)
            self._cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Content-Length")

    def do_OPTIONS(self) -> None:
        if urlparse(self.path).path != "/convert":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def end_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parsed_url = urlparse(self.path)
        if parsed_url.path != "/convert":
            self.end_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        preset_name = parse_qs(parsed_url.query).get("preset", ["square"])[0]
        preset = EXPORT_PRESETS.get(preset_name, EXPORT_PRESETS["square"])

        content_length = self.headers.get("Content-Length")
        if not content_length:
            self.end_json(HTTPStatus.BAD_REQUEST, {"error": "Missing Content-Length"})
            return

        try:
            body_length = int(content_length)
        except ValueError:
            self.end_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return

        blob = self.rfile.read(body_length)
        if not blob:
            self.end_json(HTTPStatus.BAD_REQUEST, {"error": "Empty request body"})
            return

        with tempfile.TemporaryDirectory(prefix="ttalks-") as tmpdir:
            temp_dir = Path(tmpdir)
            input_path = temp_dir / "input.webm"
            output_path = temp_dir / "output.mp4"
            input_path.write_bytes(blob)

            w, h = preset["width"], preset["height"]
            commands = _build_convert_commands(input_path, output_path, w, h)

            last_stderr = ""
            ok = False
            for command in commands:
                if output_path.exists():
                    output_path.unlink()
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    cwd=str(ROOT),
                )
                last_stderr = result.stderr or result.stdout or ""
                if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                    enc = (
                        "h264_videotoolbox"
                        if "h264_videotoolbox" in command
                        else "libx264"
                    )
                    print(f"[convert] ok preset={preset_name} {w}x{h} encoder={enc}")
                    ok = True
                    break

            if not ok:
                err_tail = (last_stderr or "no stderr")[-6000:]
                print(f"[convert] ffmpeg failed (preset={preset_name} {w}x{h}):\n{err_tail}")
                self.end_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "error": "ffmpeg conversion failed",
                        "details": err_tail,
                    },
                )
                return

            payload = output_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._cors_headers()
            self.send_header("Content-Type", "video/mp4")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="video-export-{preset["suffix"]}-hq.mp4"',
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


def main() -> None:
    os.chdir(ROOT)
    server = ThreadingHTTPServer((HOST, PORT), SiteHandler)
    print()
    print("=" * 62)
    print("  Video export server — POST /convert → H.264 .mp4 (QuickTime-friendly, ffmpeg)")
    print(f"  Open: http://{HOST}:{PORT}/")
    print("  Do not use: python -m http.server (POST is not supported there).")
    print("=" * 62)
    print()
    server.serve_forever()


if __name__ == "__main__":
    main()
