#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J-MUSIC TOP10 動画生成
outputs/日付/ranking.json から、カウントダウン形式の動画を作ります。
  - reel.mp4 : 1080x1920(9:16)TikTok / Instagramリール / YouTubeショート用
  - feed.mp4 : 1080x1350(4:5)Instagram・Xのフィード用

権利面の配慮から、MVの映像・音源・ジャケット画像は一切使わず、文字とグラフィックだけで構成します。
音声は無音です。BGMは各SNSアプリの楽曲ライブラリから公式音源を選んで付けてください。

使い方:
  python make_video.py outputs/2026-10-06            # 両方作る
  python make_video.py outputs/2026-10-06 --only reel
必要なもの: Pillow, ffmpeg, 日本語フォント(Noto Sans CJK など。FONT_PATH 環境変数で指定も可)
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FPS = 30
SIZES = {"reel": (1080, 1920), "feed": (1080, 1350)}

# 尺(秒)。tiktok.txt の台本と同じ構成
DUR_INTRO, DUR_QUICK, DUR_PODIUM, DUR_OUTRO = 3.0, 1.5, 6.0, 3.0

COL = {
    "bg_top": (14, 11, 43), "bg_bottom": (52, 16, 84),
    "text": (255, 255, 255), "sub": (190, 182, 225), "accent": (255, 77, 141),
    "gold": (245, 197, 66), "silver": (205, 213, 223), "bronze": (214, 140, 80),
    "panel": (255, 255, 255, 28), "new": (64, 220, 160), "up": (255, 120, 120), "down": (120, 170, 255),
}

FONT_CANDIDATES = [
    os.environ.get("FONT_PATH", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",          # Ubuntu / GitHub Actions
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/meiryob.ttc",  # Windows
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",                 # macOS
]


def find_font() -> str:
    for p in FONT_CANDIDATES:
        if p and Path(p).exists():
            return p
    sys.exit("日本語フォントが見つかりません。FONT_PATH 環境変数でフォントファイルを指定してください")


FONT_PATH = None
_font_cache: dict = {}


def font(size: int) -> ImageFont.FreeTypeFont:
    size = max(int(size), 8)
    if size not in _font_cache:
        _font_cache[size] = ImageFont.truetype(FONT_PATH, size, index=0)  # .ttc の0番 = JP
    return _font_cache[size]

# ------------------------------------------------------------
# テキスト折り返し(日本語は1文字単位、英単語はまとめて扱う)
# ------------------------------------------------------------

NO_HEAD = set("、。,.!?！？」』)）]】〕ー…・ゃゅょっャュョッ")


def tokenize(text: str) -> list[str]:
    toks, buf = [], ""
    for ch in text:
        if ch.isascii() and not ch.isspace():
            buf += ch
            continue
        if buf:
            toks.append(buf)
            buf = ""
        toks.append(ch)
    if buf:
        toks.append(buf)
    return toks


def wrap(text: str, f: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for tok in tokenize(text):
        if f.getlength(cur + tok) <= max_w or not cur:
            cur += tok
            continue
        if tok[0] in NO_HEAD:  # 行頭禁則: 句読点などは前の行にぶら下げる
            cur += tok
            continue
        lines.append(cur.rstrip())
        cur = tok.lstrip()
    if cur:
        lines.append(cur.rstrip())
    return lines


def fit(text: str, max_w: int, size: int, min_size: int, max_lines: int):
    """max_lines 行に収まるまで文字を小さくする。収まらなければ最終行を…で切る"""
    s = size
    while True:
        f = font(s)
        lines = wrap(text, f, max_w)
        if len(lines) <= max_lines or s <= min_size:
            break
        s = int(s * 0.92)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and f.getlength(lines[-1] + "…") > max_w:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return f, lines


def draw_lines(d: ImageDraw.ImageDraw, lines, f, cx: int, y: int, fill, gap: float = 1.25,
               anchor_center: bool = True) -> int:
    lh = int(f.size * gap)
    for ln in lines:
        if anchor_center:
            d.text((cx, y), ln, font=f, fill=fill, anchor="ma")
        else:
            d.text((cx, y), ln, font=f, fill=fill)
        y += lh
    return y

# ------------------------------------------------------------
# パーツ
# ------------------------------------------------------------

def background(w: int, h: int) -> Image.Image:
    top, bot = COL["bg_top"], COL["bg_bottom"]
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / (h - 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    img = grad.resize((w, h)).convert("RGBA")
    # 装飾: ぼんやり光る円
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r, a in ((int(w * 0.55), 18), (int(w * 0.38), 22), (int(w * 0.22), 26)):
        gd.ellipse((w * 0.85 - r, h * 0.12 - r, w * 0.85 + r, h * 0.12 + r), fill=(255, 77, 141, a))
        gd.ellipse((w * 0.1 - r, h * 0.9 - r, w * 0.1 + r, h * 0.9 + r), fill=(90, 120, 255, a))
    return Image.alpha_composite(img, glow)


def move_badge(d: ImageDraw.ImageDraw, move: str | None, cx: int, y: int, u: float):
    if not move:
        return
    label, col = {"NEW": ("NEW", COL["new"]), "再": ("再ランクイン", COL["gold"])}.get(
        move, (f"前回比 {move}", COL["up"] if move.startswith("↑") else
               COL["down"] if move.startswith("↓") else COL["sub"]))
    f = font(38 * u)
    tw = f.getlength(label)
    pad_x, pad_y = 26 * u, 12 * u
    box = (cx - tw / 2 - pad_x, y, cx + tw / 2 + pad_x, y + f.size + pad_y * 2)
    d.rounded_rectangle(box, radius=int((f.size + pad_y * 2) / 2), outline=col, width=max(int(4 * u), 2))
    d.text((cx, y + pad_y), label, font=f, fill=col, anchor="ma")


def footer(d: ImageDraw.ImageDraw, meta: dict, w: int, h: int, u: float):
    f = font(30 * u)
    d.text((w / 2, h - 90 * u), meta.get("brand", "J-MUSIC TOP10"), font=f, fill=COL["sub"], anchor="ma")


def new_layer(w: int, h: int):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)

# ------------------------------------------------------------
# シーン(各シーン = 背景 + 時間差で出てくる文字レイヤーのリスト)
# ------------------------------------------------------------

def scene_intro(meta, w, h, u):
    a, d = new_layer(w, h)
    d.text((w / 2, h * 0.30), "最新", font=font(90 * u), fill=COL["accent"], anchor="ma")
    d.text((w / 2, h * 0.30 + 120 * u), "邦楽", font=font(210 * u), fill=COL["text"], anchor="ma")
    d.text((w / 2, h * 0.30 + 360 * u), f"TOP{meta['n']}", font=font(170 * u), fill=COL["gold"], anchor="ma")
    b, d2 = new_layer(w, h)
    d2.text((w / 2, h * 0.30 + 600 * u), meta["date_s"], font=font(54 * u), fill=COL["sub"], anchor="ma")
    d2.text((w / 2, h * 0.30 + 690 * u), "1位はあの曲…!", font=font(64 * u), fill=COL["text"], anchor="ma")
    footer(d2, meta, w, h, u)
    return [(a, 0.0), (b, 0.5)]


def scene_quick(item, i, meta, w, h, u):
    a, d = new_layer(w, h)
    cy = h * 0.40
    d.text((w / 2, cy - 260 * u), f"{i + 1}", font=font(240 * u), fill=COL["accent"], anchor="ma")
    d.text((w / 2, cy + 10 * u), "位", font=font(60 * u), fill=COL["accent"], anchor="ma")
    f, lines = fit(item["title"], int(w * 0.84), int(96 * u), int(56 * u), 2)
    y = draw_lines(d, lines, f, w // 2, int(cy + 110 * u), COL["text"])
    f2, l2 = fit(item.get("artist", ""), int(w * 0.84), int(54 * u), int(36 * u), 1)
    y = draw_lines(d, l2, f2, w // 2, y + int(16 * u), COL["sub"])
    move_badge(d, item.get("move"), w // 2, y + int(30 * u), u)
    footer(d, meta, w, h, u)
    return [(a, 0.0)]


def scene_podium(item, i, meta, w, h, u):
    medal = [COL["gold"], COL["silver"], COL["bronze"]][i]
    a, d = new_layer(w, h)
    top = h * (0.20 if h > w * 1.6 else 0.07)  # 縦型は画面中央寄せ(UIに被りにくい位置)
    r = 130 * u
    cx = w / 2
    d.ellipse((cx - r, top, cx + r, top + 2 * r), fill=medal)
    d.text((cx, top + r - 95 * u), f"{i + 1}", font=font(150 * u), fill=COL["bg_top"], anchor="ma")
    d.text((cx, top + 2 * r + 24 * u), "位", font=font(56 * u), fill=medal, anchor="ma")
    f, lines = fit(item["title"], int(w * 0.86), int(112 * u), int(60 * u), 2)
    y = draw_lines(d, lines, f, w // 2, int(top + 2 * r + 120 * u), COL["text"])
    f2, l2 = fit(item.get("artist", ""), int(w * 0.86), int(60 * u), int(38 * u), 1)
    y = draw_lines(d, l2, f2, w // 2, y + int(14 * u), COL["sub"])
    move_badge(d, item.get("move"), w // 2, y + int(28 * u), u)
    y_panel = y + int(140 * u)

    b, d2 = new_layer(w, h)
    hit = item.get("hitokoto") or ""
    fh, lh = fit(hit, int(w * 0.76), int(52 * u), int(36 * u), 4)
    info = []
    if item.get("ranks") and meta.get("n_sources"):
        info.append(f"{meta['n_sources']}チャート中 {len(item['ranks'])}つにランクイン")
    if item.get("tieup"):
        info.append(item["tieup"])
    fi = font(36 * u)
    info_lines = [ln for t in info for ln in wrap(t, fi, int(w * 0.76))][:3]
    panel_h = int(len(lh) * fh.size * 1.35 + len(info_lines) * fi.size * 1.4 + 110 * u)
    if hit or info_lines:
        d2.rounded_rectangle((w * 0.07, y_panel, w * 0.93, y_panel + panel_h),
                             radius=int(36 * u), fill=COL["panel"])
        d2.rectangle((w * 0.07, y_panel + 30 * u, w * 0.07 + 10 * u, y_panel + panel_h - 30 * u),
                     fill=COL["accent"])
        yy = draw_lines(d2, lh, fh, w // 2, int(y_panel + 45 * u), COL["text"], gap=1.35)
        draw_lines(d2, info_lines, fi, w // 2, yy + int(14 * u), COL["sub"], gap=1.4)
    footer(d2, meta, w, h, u)
    return [(a, 0.0), (b, 0.9)]


def scene_outro(meta, w, h, u):
    a, d = new_layer(w, h)
    y = h * 0.33
    for text, size, col in (("全曲のMVは", 72, COL["text"]), ("プロフィールから", 72, COL["text"]),
                            ("フォローで", 60, COL["accent"]), ("最新ランキングをチェック", 60, COL["accent"])):
        f = font(size * u)
        d.text((w / 2, y), text, font=f, fill=col, anchor="ma")
        y += f.size * 1.5
    if meta.get("credit"):
        fc, lc = fit(meta["credit"], int(w * 0.84), int(30 * u), int(22 * u), 4)
        draw_lines(d, lc, fc, w // 2, int(y + 80 * u), COL["sub"], gap=1.4)
    footer(d, meta, w, h, u)
    return [(a, 0.0)]


def build_scenes(items, meta, w, h):
    u = w / 1080
    scenes = [(DUR_INTRO, scene_intro(meta, w, h, u))]
    for i in range(len(items) - 1, 2, -1):
        scenes.append((DUR_QUICK, scene_quick(items[i], i, meta, w, h, u)))
    for i in (2, 1, 0):
        if i < len(items):
            scenes.append((DUR_PODIUM, scene_podium(items[i], i, meta, w, h, u)))
    scenes.append((DUR_OUTRO, scene_outro(meta, w, h, u)))
    return scenes

# ------------------------------------------------------------
# エンコード
# ------------------------------------------------------------

def ease(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def render(items, meta, out_path: Path, size):
    w, h = size
    bg = background(w, h)
    scenes = build_scenes(items, meta, w, h)
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg が見つかりません")
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
           "-shortest", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    slide = 60 * w / 1080
    total = 0
    for dur, layers in scenes:
        nf = int(round(dur * FPS))
        alphas = [lay.getchannel("A") for lay, _ in layers]
        for fi in range(nf):
            t = fi / FPS
            frame = bg.copy()
            for (lay, delay), alpha in zip(layers, alphas):
                e = ease((t - delay) / 0.35)
                if e <= 0:
                    continue
                # シーン終わり0.2秒で全体をフェードアウト
                out_e = ease((dur - t) / 0.2) if dur - t < 0.2 else 1.0
                k = e * out_e
                moved = lay if k >= 1 else lay.copy()
                if k < 1:
                    moved.putalpha(alpha.point(lambda v, k=k: int(v * k)))
                dy = int((1 - e) * slide)
                frame.alpha_composite(moved, (0, dy)) if dy == 0 else \
                    frame.alpha_composite(moved.crop((0, 0, w, h - dy)), (0, dy))
            proc.stdin.write(frame.convert("RGB").tobytes())
        total += nf
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg でのエンコードに失敗しました")
    print(f"[info] 動画出力: {out_path}({total / FPS:.1f}秒)")
    return out_path


def make_videos(out_dir: Path, only: str | None = None) -> list[Path]:
    global FONT_PATH
    FONT_PATH = FONT_PATH or find_font()
    items = json.loads((out_dir / "ranking.json").read_text(encoding="utf-8"))
    meta_file = out_dir / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    meta.setdefault("date_s", out_dir.name.replace("-", "/"))
    meta["n"] = len(items)
    outs = []
    for kind, size in SIZES.items():
        if only and kind != only:
            continue
        outs.append(render(items, meta, out_dir / f"{kind}.mp4", size))
    return outs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", help="ranking.json があるフォルダ(例: outputs/2026-10-06)")
    ap.add_argument("--only", choices=list(SIZES))
    a = ap.parse_args()
    make_videos(Path(a.out_dir), a.only)
