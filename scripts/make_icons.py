"""生成 src-tauri 图标集（Windows 打包必需，缺文件 tauri build 直接失败）。

用法：python scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

MASTER = 1024
OUT_DIR = Path(__file__).resolve().parent.parent / "src-tauri" / "icons"

_TOP = (22, 119, 255)
_BOTTOM = (9, 191, 214)
_RADIUS = 200


def _gradient_rounded_square(size: int) -> Image.Image:
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)
    for y in range(size):
        ratio = y / max(size - 1, 1)
        color = tuple(round(a + (b - a) * ratio) for a, b in zip(_TOP, _BOTTOM, strict=True))
        draw.line([(0, y), (size, y)], fill=(*color, 255))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(_RADIUS * size / MASTER), fill=255
    )
    base.putalpha(mask)
    return base


def _draw_mark(img: Image.Image) -> None:
    """白色播放三角 + 两条剪辑标记，示意「剪掉静音段」。"""
    draw = ImageDraw.Draw(img)
    s = img.width

    def px(v: float) -> float:
        return v * s / MASTER

    triangle = [(px(330), px(250)), (px(330), px(770)), (px(700), px(510))]
    draw.polygon(triangle, fill=(255, 255, 255, 235))

    bar_w, bar_h = px(48), px(300)
    for cx in (px(760), px(840)):
        draw.rounded_rectangle(
            [cx, px(510) - bar_h / 2, cx + bar_w, px(510) + bar_h / 2],
            radius=px(20),
            fill=(255, 255, 255, 235),
        )


def build() -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    master = _gradient_rounded_square(MASTER)
    _draw_mark(master)

    written: list[Path] = []
    for name, size in (
        ("32x32.png", 32),
        ("128x128.png", 128),
        ("128x128@2x.png", 256),
        ("icon.png", 512),
    ):
        path = OUT_DIR / name
        master.resize((size, size), Image.Resampling.LANCZOS).save(path)
        written.append(path)

    ico = OUT_DIR / "icon.ico"
    master.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    written.append(ico)
    return written


if __name__ == "__main__":
    for item in build():
        print(f"wrote {item.relative_to(OUT_DIR.parent.parent)}")
