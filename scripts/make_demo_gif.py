#!/usr/bin/env python3
"""Generate the README demo GIF from the project health report screenshot."""

from __future__ import annotations

from pathlib import Path

import imageio.v2 as imageio
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "project-health-report-demo.png"
TARGET = ROOT / "assets" / "project-health-report-demo.gif"
FONT_PATH = Path("/System/Library/Fonts/Helvetica.ttc")
WIDTH = 1100
HEIGHT = 366
FPS = 8
SECONDS_PER_SCENE = 6

SCENES = [
    "1. Pull live project data\n2. Generate workload and health views",
    "3. Review assignee workload, due-date buckets,\n   and status distribution in one pass",
    "4. Export dashboards as HTML or PNG\n   for stakeholders and project tracking",
    "5. Use bulk workflows for teams, tasks,\n   boards, wiki pages, meetings, and attachments",
    "Open source: github.com/varaprasadreddy9676/\nopenproject-codex-plugin",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if FONT_PATH.exists():
        return ImageFont.truetype(str(FONT_PATH), size=size)
    return ImageFont.load_default()


def _wrap_frame(base: Image.Image, scene_text: str) -> Image.Image:
    frame = base.copy().convert("RGBA")
    draw = ImageDraw.Draw(frame)
    title_font = _load_font(32)
    subtitle_font = _load_font(18)
    body_font = _load_font(28)

    draw.rectangle((0, 0, WIDTH, 86), fill=(0, 0, 0, 155))
    draw.rectangle((0, HEIGHT - 118, WIDTH, HEIGHT), fill=(0, 0, 0, 150))

    draw.text((34, 16), "OpenProject Codex", fill="white", font=title_font)
    draw.text((36, 52), "Operate OpenProject directly from Codex", fill="white", font=subtitle_font)
    draw.multiline_text((52, HEIGHT - 100), scene_text, fill="white", font=body_font, spacing=6)
    return frame.convert("P", palette=Image.Palette.ADAPTIVE)


def main() -> None:
    base = Image.open(SOURCE).convert("RGB")
    base.thumbnail((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (WIDTH, HEIGHT), "white")
    offset_x = (WIDTH - base.width) // 2
    offset_y = (HEIGHT - base.height) // 2
    canvas.paste(base, (offset_x, offset_y))

    frames = []
    for scene in SCENES:
        frame = _wrap_frame(canvas, scene)
        frames.extend([frame] * (SECONDS_PER_SCENE * FPS))

    imageio.mimsave(TARGET, frames, duration=1 / FPS, loop=0)
    print(TARGET)


if __name__ == "__main__":
    main()
