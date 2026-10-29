#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J-MUSIC TOP10 動画生成(音楽カウントダウン番組風)

outputs/日付/ranking.json から、オリジナルキャラクター3人が進行するカウントダウン動画を作ります。
  - reel.mp4 : 1080x1920(9:16)TikTok / Instagramリール / YouTubeショート用
  - feed.mp4 : 1080x1350(4:5)Instagram・Xのフィード用

構成(10曲の場合 約55秒):
  オープニング → 10位〜4位(テンポよく)→「いよいよTOP3!」→ 3位 → 2位
  → ドラムロール「第1位は…」→ 1位(紙吹雪)→ エンディング

キャラクター(すべてオリジナル):
  マイクン … マイク型。元気な司会役
  レコ     … レコード型。データに詳しい解説役
  オンプ   … 音符型。リアクションで盛り上げる役

権利面の配慮:
  - MVの映像・楽曲音源・ジャケット画像は一切使いません
  - 効果音(ワイプ音・ドラムロール・ファンファーレ等)はこのスクリプトがその場で合成したオリジナルです
  - BGMは各SNSアプリの楽曲ライブラリから公式音源を付けてください(効果音は控えめな音量です)

使い方:
  python make_video.py outputs/2026-10-06              # reel と feed を両方作る
  python make_video.py outputs/2026-10-06 --only reel
  python make_video.py outputs/2026-10-06 --no-sfx     # 効果音なし(無音)
必要なもの: Pillow, ffmpeg, 日本語フォント(Noto Sans CJK など。FONT_PATH 環境変数で指定も可)
"""

import argparse
import array
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

FPS = 30
SIZES = {"reel": (1080, 1920), "feed": (1080, 1350)}
SHOW_TITLE = ("J-MUSIC", "COUNTDOWN")

# 尺(秒)
T_OPEN, T_QUICK, T_TOP3, T_PODIUM, T_DRUM, T_FIRST, T_END = 5.5, 2.4, 2.2, 7.5, 2.6, 9.0, 4.0
WIPE = 0.18          # 画面切り替えワイプの片側の長さ
TYPE_CPS = 20        # 吹き出しの文字送り(文字/秒)

COL = {
    "bg_top": (24, 8, 58), "bg_bottom": (6, 3, 22), "out": (28, 18, 52),
    "text": (255, 255, 255), "sub": (200, 192, 235), "pink": (255, 70, 150), "cyan": (60, 210, 255),
    "gold": (255, 205, 60), "panel": (18, 10, 44), "dark": (12, 6, 30),
}

CHARS = [  # 並び順 = 画面の左から
    {"id": "mic", "name": "マイクン", "color": (255, 84, 160), "x": 0.20},
    {"id": "reco", "name": "レコ", "color": (50, 190, 255), "x": 0.50},
    {"id": "onpu", "name": "オンプ", "color": (255, 196, 40), "x": 0.80},
]
MIC, RECO, ONPU = 0, 1, 2

# 画面の割り付け(高さに対する比率)。reel は下部がSNSのUIに隠れるため余白を多めに
LAYOUTS = {
    "reel": {"header": 0.030, "num_bottom": 0.262, "card_top": 0.250, "card_max": 0.165,
             "bubble_top": 0.435, "bubble_max": 0.140, "feet": 0.800, "char": 0.27,
             "ts": 1.0, "num": 270},
    "feed": {"header": 0.025, "num_bottom": 0.240, "card_top": 0.225, "card_max": 0.170,
             "bubble_top": 0.420, "bubble_max": 0.165, "feet": 0.945, "char": 0.255,
             "ts": 0.84, "num": 215},
}

FONT_CANDIDATES = [
    os.environ.get("FONT_PATH", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",         # Ubuntu / GitHub Actions
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/meiryob.ttc",  # Windows
    "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc",                 # macOS
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
]
FONT_PATH = None
_fonts: dict = {}


def find_font() -> str:
    for p in FONT_CANDIDATES:
        if p and Path(p).exists():
            return p
    sys.exit("日本語フォントが見つかりません。FONT_PATH 環境変数でフォントファイルを指定してください")


def font(size) -> ImageFont.FreeTypeFont:
    size = max(int(size), 8)
    if size not in _fonts:
        _fonts[size] = ImageFont.truetype(FONT_PATH, size, index=0)  # .ttc の0番 = 日本語
    return _fonts[size]

# ============================================================
# 小道具
# ============================================================

def clamp(t, a=0.0, b=1.0):
    return max(a, min(b, t))


def lerp(a, b, t):
    return a + (b - a) * t


def ease_out(t):
    t = clamp(t)
    return 1 - (1 - t) ** 3


def ease_back(t):
    t = clamp(t)
    c1 = 1.70158
    return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2


def ease_bounce(t):
    t = clamp(t)
    n, d = 7.5625, 2.75
    if t < 1 / d:
        return n * t * t
    if t < 2 / d:
        t -= 1.5 / d
        return n * t * t + 0.75
    if t < 2.5 / d:
        t -= 2.25 / d
        return n * t * t + 0.9375
    t -= 2.625 / d
    return n * t * t + 0.984375


def clean(s) -> str:
    """フォントにない絵文字などを除く"""
    return "".join(ch for ch in str(s or "") if ord(ch) <= 0xFFFF
                   and not 0x2600 <= ord(ch) <= 0x27BF and not 0xFE00 <= ord(ch) <= 0xFE0F).strip()


def vgrad(w, h, c1, c2) -> Image.Image:
    g = Image.new("RGB", (1, max(h, 2)))
    for y in range(g.height):
        t = y / (g.height - 1)
        g.putpixel((0, y), tuple(int(lerp(c1[i], c2[i], t)) for i in range(3)))
    return g.resize((max(w, 1), max(h, 1)))


def paste(frame: Image.Image, spr: Image.Image, x, y, alpha: float = 1.0, scale: float = 1.0):
    """frame(RGB)に RGBA スプライトを (x,y)=左上 で合成。scale は中心基準"""
    if alpha <= 0.01 or spr is None:
        return
    if abs(scale - 1) > 0.01:
        nw, nh = max(int(spr.width * scale), 1), max(int(spr.height * scale), 1)
        x, y = x + (spr.width - nw) / 2, y + (spr.height - nh) / 2
        spr = spr.resize((nw, nh), Image.BILINEAR)
    if alpha < 0.99:
        spr = spr.copy()
        spr.putalpha(spr.getchannel("A").point(lambda v, a=alpha: int(v * a)))
    frame.paste(spr, (int(round(x)), int(round(y))), spr)


def trim(img: Image.Image) -> Image.Image:
    bb = img.getbbox()
    return img.crop(bb) if bb else img

# ---------- 文字組み ----------

NO_HEAD = set("、。,.!?！？」』)）]】〕ー…・ゃゅょっャュョッぁぃぅぇぉ〜")


def tokenize(text):
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


def wrap(text, f, max_w):
    lines, cur = [], ""
    for tok in tokenize(text):
        if f.getlength(cur + tok) <= max_w or not cur or tok[0] in NO_HEAD:
            cur += tok
            continue
        lines.append(cur.rstrip())
        cur = tok.lstrip()
    if cur:
        lines.append(cur.rstrip())
    return lines


def fit(text, max_w, size, min_size, max_lines):
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


def deco_text(text, size, top, bottom, stroke=COL["out"], shadow=COL["pink"], shear=0.0) -> Image.Image:
    """番組テロップ風: グラデーション文字 + 太フチ + ずらし影"""
    f = font(size)
    sw = max(int(size * 0.07), 3)
    off = max(int(size * 0.06), 3)
    tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
    l, t, r, b = tmp.textbbox((0, 0), text, font=f, stroke_width=sw)
    pad = sw + off + 4
    W, H = r - l + pad * 2, b - t + pad * 2
    org = (pad - l, pad - t)
    smask, fmask = Image.new("L", (W, H)), Image.new("L", (W, H))
    ImageDraw.Draw(smask).text(org, text, font=f, fill=255, stroke_width=sw, stroke_fill=255)
    ImageDraw.Draw(fmask).text(org, text, font=f, fill=255)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.paste(shadow + (255,), (off, off), smask)
    img.paste(stroke + (255,), (0, 0), smask)
    img.paste(vgrad(W, H, top, bottom), (0, 0), fmask)
    if shear:
        ext = int(H * abs(shear))
        img2 = Image.new("RGBA", (W + ext, H), (0, 0, 0, 0))
        img2.paste(img, (ext, 0))
        img = img2.transform(img2.size, Image.AFFINE, (1, shear, 0, 0, 1, 0), Image.BICUBIC)
    return trim(img)


def pill(text, size, bg, fg=(255, 255, 255), outline=None, pad=(0.55, 0.22)) -> Image.Image:
    f = font(size)
    tw = f.getlength(text)
    px, py = int(size * pad[0]), int(size * pad[1])
    W, H = int(tw + px * 2), int(size * 1.25 + py * 2)
    S = 2
    big = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    ImageDraw.Draw(big).rounded_rectangle((0, 0, W * S - 1, H * S - 1), radius=H * S // 2, fill=bg + (255,),
                                          outline=(outline + (255,)) if outline else None,
                                          width=max(int(size * 0.12 * S), 2) if outline else 0)
    img = big.resize((W, H), Image.LANCZOS)
    ImageDraw.Draw(img).text((W / 2, H / 2), text, font=f, fill=fg, anchor="mm")
    return img

# ============================================================
# キャラクター(オリジナル。2倍で描いて縮小しアンチエイリアス)
# ============================================================

def _heart(d, cx, cy, size, fill, outline=None, width=1):
    pts = []
    for k in range(48):
        t = 2 * math.pi * k / 48
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x / 17 * size, cy - y / 17 * size))
    d.polygon(pts, fill=fill, outline=outline, width=width)


def _face_gekiga(d, cx, cy, e, mouth_open, ow):
    """劇画顔: 太い眉・三白眼・おでこと頬の縦線・汗・食いしばった歯"""
    o = COL["out"]
    lw = max(int(e * 0.09), 2)       # 輪郭線(顔の大きさに合わせる)
    hw = max(int(e * 0.055), 1)      # 影の線
    # おでこの縦線(青ざめる影)
    for k in range(-3, 4):
        x = cx + k * 0.28 * e
        d.line([(x, cy - 1.55 * e), (x, cy - 1.05 * e - abs(k) * 0.04 * e)], fill=o, width=hw)
    for sx in (-1, 1):
        ex = cx + sx * e
        # 眉(内側が下がった険しい眉)
        d.polygon([(ex - sx * 0.58 * e, cy - 0.48 * e), (ex + sx * 0.60 * e, cy - 0.98 * e),
                   (ex + sx * 0.52 * e, cy - 1.18 * e), (ex - sx * 0.62 * e, cy - 0.72 * e)], fill=o)
        # 鋭い目(つり目のアーモンド形 + 小さな黒目 = 三白眼)
        eye = [(ex - sx * 0.55 * e, cy - 0.08 * e), (ex + sx * 0.05 * e, cy - 0.40 * e),
               (ex + sx * 0.58 * e, cy - 0.30 * e), (ex + sx * 0.02 * e, cy + 0.24 * e)]
        d.polygon(eye, fill=(255, 255, 255))
        d.line(eye[:3], fill=o, width=lw * 2, joint="curve")   # 上まぶたは太く
        d.line(eye[2:] + [eye[0]], fill=o, width=lw)
        pr = 0.10 * e
        px, py = ex + sx * 0.02 * e, cy - 0.10 * e
        d.ellipse((px - pr, py - pr, px + pr, py + pr), fill=o)
        # 頬の縦線
        for k in range(3):
            x = ex + sx * (0.02 + k * 0.18) * e
            d.line([(x, cy + 0.42 * e), (x, cy + 0.80 * e)], fill=o, width=hw)
    # 汗
    sx_, sy_ = cx + 1.60 * e, cy - 0.75 * e
    sr = 0.26 * e
    d.polygon([(sx_, sy_ - sr * 2.2), (sx_ - sr * 0.92, sy_ - sr * 0.3), (sx_ + sr * 0.92, sy_ - sr * 0.3)],
              fill=(150, 215, 255))
    d.ellipse((sx_ - sr, sy_ - sr, sx_ + sr, sy_ + sr), fill=(150, 215, 255))
    d.line([(sx_ - sr * 0.95, sy_ - sr * 0.2), (sx_, sy_ - sr * 2.2), (sx_ + sr * 0.95, sy_ - sr * 0.2)],
           fill=o, width=hw)
    d.arc((sx_ - sr, sy_ - sr, sx_ + sr, sy_ + sr), -10, 190, fill=o, width=hw)
    d.ellipse((sx_ - sr * 0.45, sy_ - sr * 0.45, sx_ - sr * 0.05, sy_ - sr * 0.05), fill=(255, 255, 255))
    # 口: 食いしばった歯(開けると叫ぶ口)
    my = cy + 1.02 * e
    mw, mh = (0.66 * e, 0.36 * e) if mouth_open else (0.56 * e, 0.24 * e)
    if mouth_open:
        d.rounded_rectangle((cx - mw, my - mh, cx + mw, my + mh), radius=int(0.1 * e), fill=(110, 20, 50),
                            outline=o, width=lw)
        ix = mw * 0.10
        d.rectangle((cx - mw + ix, my - mh * 0.78, cx + mw - ix, my - mh * 0.22), fill=(255, 255, 255))
        d.rectangle((cx - mw + ix, my + mh * 0.30, cx + mw - ix, my + mh * 0.78), fill=(255, 255, 255))
    else:
        d.rounded_rectangle((cx - mw, my - mh, cx + mw, my + mh), radius=int(0.08 * e), fill=(255, 255, 255),
                            outline=o, width=lw)
        d.line([(cx - mw, my), (cx + mw, my)], fill=o, width=hw)
    for k in (-2, -1, 0, 1, 2):
        x = cx + k * mw * 0.36
        d.line([(x, my - mh), (x, my + mh)], fill=o, width=hw)


def _face_dere(d, cx, cy, e, mouth_open, ow):
    """デレ顔: ハートの目・斜線入りの赤面・ふにゃっとした口・ハートマーク"""
    o = COL["out"]
    for sx in (-1, 1):
        ex = cx + sx * e
        _heart(d, ex, cy, 0.52 * e, (255, 60, 120), o, max(int(e * 0.08), 2))
        d.ellipse((ex - 0.22 * e, cy - 0.26 * e, ex - 0.06 * e, cy - 0.10 * e), fill=(255, 220, 235))
        bx, by = ex + sx * 0.40 * e, cy + 0.66 * e
        d.ellipse((bx - 0.42 * e, by - 0.20 * e, bx + 0.42 * e, by + 0.20 * e), fill=(255, 120, 155))
        for k in (-1, 0, 1):
            x = bx + k * 0.22 * e
            d.line([(x - 0.06 * e, by + 0.12 * e), (x + 0.08 * e, by - 0.12 * e)], fill=(225, 50, 100),
                   width=max(ow // 2, 2))
    my = cy + 0.95 * e
    if mouth_open:
        d.chord((cx - 0.45 * e, my - 0.30 * e, cx + 0.45 * e, my + 0.45 * e), 0, 180, fill=(110, 20, 50),
                outline=o, width=ow)
        d.chord((cx - 0.26 * e, my + 0.08 * e, cx + 0.26 * e, my + 0.40 * e), 0, 180, fill=(255, 110, 140))
    else:  # 「ω」の口
        lw = max(int(e * 0.09), 2)
        d.arc((cx - 0.50 * e, my - 0.30 * e, cx, my + 0.22 * e), 0, 180, fill=o, width=lw)
        d.arc((cx, my - 0.30 * e, cx + 0.50 * e, my + 0.22 * e), 0, 180, fill=o, width=lw)
    _heart(d, cx + 1.55 * e, cy - 1.05 * e, 0.36 * e, (255, 80, 140), o, max(ow // 2, 2))
    _heart(d, cx + 1.15 * e, cy - 1.55 * e, 0.22 * e, (255, 140, 180), o, max(ow // 2, 2))


def _face(d, cx, cy, e, blink, mouth_open, ow, face="normal"):
    if face == "gekiga":
        return _face_gekiga(d, cx, cy, e, mouth_open, ow)
    if face == "dere":
        return _face_dere(d, cx, cy, e, mouth_open, ow)
    o = COL["out"]
    rx, ry = 0.42 * e, 0.55 * e
    for sx in (-1, 1):
        ex = cx + sx * e
        if blink:
            d.arc((ex - rx, cy - ry * 0.6, ex + rx, cy + ry * 0.9), 200, 340, fill=o, width=ow)
        else:
            d.ellipse((ex - rx, cy - ry, ex + rx, cy + ry), fill=(255, 255, 255), outline=o, width=ow)
            pr = 0.30 * e
            d.ellipse((ex - pr, cy - pr + 0.12 * e, ex + pr, cy + pr + 0.12 * e), fill=o)
            hr = 0.11 * e
            d.ellipse((ex - 0.12 * e - hr, cy - 0.08 * e - hr, ex - 0.12 * e + hr, cy - 0.08 * e + hr),
                      fill=(255, 255, 255))
        d.ellipse((ex + sx * 0.45 * e - 0.30 * e, cy + 0.62 * e - 0.17 * e,
                   ex + sx * 0.45 * e + 0.30 * e, cy + 0.62 * e + 0.17 * e), fill=(255, 140, 170))
    my = cy + 0.95 * e
    if mouth_open:
        d.ellipse((cx - 0.42 * e, my - 0.25 * e, cx + 0.42 * e, my + 0.45 * e), fill=(110, 20, 50),
                  outline=o, width=ow)
        d.chord((cx - 0.30 * e, my + 0.05 * e, cx + 0.30 * e, my + 0.42 * e), 0, 180, fill=(255, 110, 140))
    else:
        d.arc((cx - 0.42 * e, my - 0.35 * e, cx + 0.42 * e, my + 0.25 * e), 20, 160, fill=o, width=ow)


def _limb(d, pts, ow, col):
    d.line(pts, fill=COL["out"], width=int(ow * 3.0), joint="curve")
    d.line(pts, fill=col, width=int(ow * 1.5), joint="curve")


def _arms(d, shoulders, hands, ow, hand_r, col=(255, 255, 255)):
    o = COL["out"]
    for (sx, sy), (hx, hy) in zip(shoulders, hands):
        mx, my = (sx + hx) / 2, (sy + hy) / 2 + (hand_r if hy > sy else -hand_r * 0.3)
        _limb(d, [(sx, sy), (mx, my), (hx, hy)], ow, col)
    for hx, hy in hands:
        d.ellipse((hx - hand_r, hy - hand_r, hx + hand_r, hy + hand_r), fill=(255, 255, 255), outline=o, width=ow)


def _feet(d, cx, y, gap, r, ow, col=(255, 255, 255)):
    for sx in (-1, 1):
        d.ellipse((cx + sx * gap - r * 1.3, y - r * 0.8, cx + sx * gap + r * 1.3, y + r * 0.8),
                  fill=col, outline=COL["out"], width=ow)


def draw_character(cid: str, S: int, mouth: bool, blink: bool, arms: str, face: str = "normal") -> Image.Image:
    W, H = S * 2, int(S * 2 * 1.3)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    o, ow = COL["out"], max(int(W * 0.018), 2)
    up = arms == "up"
    hr = W * 0.055

    if cid == "mic":
        pink, dpink = (255, 84, 160), (200, 50, 120)
        sh = [(W * 0.36, H * 0.66), (W * 0.64, H * 0.66)]
        hands = [(W * 0.12, H * 0.38), (W * 0.88, H * 0.38)] if up else [(W * 0.15, H * 0.80), (W * 0.85, H * 0.80)]
        _arms(d, sh, hands, ow, hr, (255, 150, 200))
        _feet(d, W * 0.5, H * 0.93, W * 0.11, W * 0.06, ow, (255, 220, 235))
        d.polygon([(W * 0.35, H * 0.57), (W * 0.65, H * 0.57), (W * 0.58, H * 0.90), (W * 0.42, H * 0.90)],
                  fill=pink, outline=o)
        d.line([(W * 0.40, H * 0.62), (W * 0.45, H * 0.86)], fill=(255, 170, 210), width=int(ow * 1.6))
        d.rounded_rectangle((W * 0.29, H * 0.53, W * 0.71, H * 0.61), radius=int(W * 0.03), fill=dpink,
                            outline=o, width=ow)
        cx, cy, r = W * 0.5, H * 0.31, W * 0.29
        head = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        hd = ImageDraw.Draw(head)
        hd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(214, 220, 232))
        step = W * 0.045
        k = -2 * r
        while k < 2 * r:
            hd.line([(cx - r + k, cy - r), (cx + r + k, cy + r)], fill=(176, 184, 204), width=max(ow // 2, 2))
            hd.line([(cx + r - k, cy - r), (cx - r - k, cy + r)], fill=(176, 184, 204), width=max(ow // 2, 2))
            k += step
        mask = Image.new("L", (W, H))
        ImageDraw.Draw(mask).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
        img.paste(head, (0, 0), mask)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=o, width=ow)
        d.arc((cx - r * 0.8, cy - r * 0.8, cx + r * 0.8, cy + r * 0.8), 200, 250, fill=(255, 255, 255),
              width=int(ow * 1.5))
        # 顔の下地(読みやすくするための明るい楕円)
        d.ellipse((cx - r * 0.72, cy - r * 0.42, cx + r * 0.72, cy + r * 0.72), fill=(236, 240, 248))
        _face(d, cx, cy, W * 0.10, blink, mouth, ow, face)

    elif cid == "reco":
        cx, cy, r = W * 0.5, H * 0.42, W * 0.31
        sh = [(cx - r * 0.92, cy + r * 0.25), (cx + r * 0.92, cy + r * 0.25)]
        hands = [(W * 0.09, H * 0.17), (W * 0.91, H * 0.17)] if up else [(W * 0.08, H * 0.66), (W * 0.92, H * 0.66)]
        _arms(d, sh, hands, ow, hr, (130, 220, 255))
        for sx in (-1, 1):
            _limb(d, [(cx + sx * W * 0.09, cy + r * 0.9), (cx + sx * W * 0.11, H * 0.92)], ow, (130, 220, 255))
        _feet(d, cx, H * 0.93, W * 0.12, W * 0.06, ow, (210, 240, 255))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(30, 28, 40), outline=o, width=ow)
        rr = r * 0.93
        while rr > r * 0.55:
            d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=(62, 60, 78), width=max(ow // 2, 2))
            rr -= W * 0.022
        d.arc((cx - r * 0.85, cy - r * 0.85, cx + r * 0.85, cy + r * 0.85), 200, 255, fill=(150, 150, 175),
              width=int(ow * 1.8))
        d.arc((cx - r * 0.85, cy - r * 0.85, cx + r * 0.85, cy + r * 0.85), 20, 60, fill=(110, 110, 135),
              width=int(ow * 1.4))
        lr = r * 0.56
        d.ellipse((cx - lr, cy - lr, cx + lr, cy + lr), fill=(50, 190, 255), outline=o, width=ow)
        d.ellipse((cx - W * 0.012, cy - lr * 0.72 - W * 0.012, cx + W * 0.012, cy - lr * 0.72 + W * 0.012), fill=o)
        _face(d, cx, cy - lr * 0.12, W * 0.072, blink, mouth, ow, face)

    else:  # onpu
        yel, dyel = (255, 200, 40), (225, 150, 20)
        cx, cy, rx, ry = W * 0.44, H * 0.66, W * 0.28, W * 0.21
        sh = [(cx - rx * 0.85, cy + ry * 0.2), (cx + rx * 0.85, cy + ry * 0.2)]
        hands = [(W * 0.10, H * 0.40), (W * 0.80, H * 0.40)] if up else [(W * 0.08, H * 0.82), (W * 0.82, H * 0.82)]
        _arms(d, sh, hands, ow, hr, (255, 215, 90))
        _feet(d, cx, H * 0.94, W * 0.11, W * 0.06, ow, (255, 240, 200))
        sx0, sx1 = W * 0.66, W * 0.72
        d.rectangle((sx0, H * 0.07, sx1, cy), fill=dyel, outline=o, width=ow)
        pts = []
        for i in range(13):
            t = i / 12
            pts.append((sx1 + W * 0.22 * math.sin(t * math.pi * 0.75), H * 0.07 + H * 0.24 * t))
        for i in range(12, -1, -1):
            t = i / 12
            pts.append((sx1 + W * 0.12 * math.sin(t * math.pi * 0.75), H * 0.13 + H * 0.16 * t))
        d.polygon(pts, fill=dyel, outline=o)
        d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=yel, outline=o, width=ow)
        d.arc((cx - rx * 0.8, cy - ry * 0.8, cx + rx * 0.8, cy + ry * 0.8), 200, 250, fill=(255, 245, 200),
              width=int(ow * 1.6))
        _face(d, cx, cy - ry * 0.15, W * 0.085, blink, mouth, ow, face)

    return img.resize((S, int(S * 1.3)), Image.LANCZOS)

# ============================================================
# ステージ背景(スポットライトの角度違いを先に作っておく)
# ============================================================

N_LIGHT = 24
LIGHT_CYCLE = 3.2  # 秒


def make_backgrounds(w, h, L, meta):
    u = w / 1080
    base = vgrad(w, h, COL["bg_top"], COL["bg_bottom"]).convert("RGBA")
    d = ImageDraw.Draw(base)
    feet = h * L["feet"]
    horizon = feet - h * 0.06

    # LEDウォール(背面の点々)
    rnd = random.Random(7)
    step = int(36 * u)
    for y in range(int(h * 0.09), int(horizon), step):
        for x in range(step // 2, w, step):
            wave_ = 0.5 + 0.5 * math.sin(x / w * 6 + y / h * 9)
            c = COL["pink"] if (x // step + y // step) % 5 else COL["cyan"]
            a = 0.10 + 0.22 * wave_ * rnd.random()
            col = tuple(int(lerp(COL["bg_top"][i], c[i], a)) for i in range(3))
            r = 4 * u
            d.ellipse((x - r, y - r, x + r, y + r), fill=col)

    # 床(パースのついたネオングリッド)
    d.polygon([(0, horizon), (w, horizon), (w, h), (0, h)], fill=(14, 6, 34))
    vx = w / 2
    for i in range(-12, 13):
        x_bot = vx + i * w * 0.16
        d.line([(vx + i * w * 0.035, horizon), (x_bot, h)], fill=(120, 30, 110), width=max(int(2 * u), 1))
    yy, gap = horizon, h * 0.008
    while yy < h:
        d.line([(0, yy), (w, yy)], fill=(120, 30, 110), width=max(int(2 * u), 1))
        gap *= 1.35
        yy += gap
    d.line([(0, horizon), (w, horizon)], fill=COL["pink"], width=max(int(4 * u), 2))

    # キャラクターの影と名札
    S = int(w * L["char"])
    for c in CHARS:
        cx = c["x"] * w
        d.ellipse((cx - S * 0.36, feet - S * 0.06, cx + S * 0.36, feet + S * 0.06), fill=(8, 3, 20))
        tag = pill(c["name"], 26 * u * L["ts"], c["color"], outline=(255, 255, 255))
        base.alpha_composite(tag, (int(cx - tag.width / 2), int(feet + S * 0.10)))

    # 番組ロゴ(左上)と日付(右上)
    ts = L["ts"]
    y0 = int(h * L["header"])
    logo1 = deco_text(SHOW_TITLE[0], 54 * u * ts, (255, 255, 255), (255, 180, 220), shear=-0.18)
    logo2 = deco_text(SHOW_TITLE[1], 40 * u * ts, COL["gold"], (255, 140, 40), shadow=COL["cyan"], shear=-0.18)
    base.alpha_composite(logo1, (int(40 * u), y0))
    base.alpha_composite(logo2, (int(40 * u + logo1.width * 0.08), y0 + logo1.height - int(4 * u)))
    date_p = pill(meta.get("date_s", ""), 30 * u * ts, COL["dark"], COL["text"], outline=COL["cyan"])
    base.alpha_composite(date_p, (int(w - date_p.width - 40 * u), y0 + int(10 * u)))

    # スポットライト(半分の解像度で描いてぼかす)
    hw, hh = w // 2, h // 2
    out = []
    for k in range(N_LIGHT):
        ph = k / N_LIGHT * 2 * math.pi
        lay = Image.new("RGBA", (hw, hh), (0, 0, 0, 0))
        ld = ImageDraw.Draw(lay)
        for (ox, base_ang, col, spd) in ((0.08, 62, COL["pink"], 1), (0.92, 118, COL["cyan"], -1),
                                         (0.5, 90, (255, 240, 200), 2)):
            ang = math.radians(base_ang + spd * 16 * math.sin(ph))
            L_ = hh * 1.15
            spread = math.radians(9 if spd != 2 else 6)
            p0 = (ox * hw, -10)
            p1 = (p0[0] + L_ * math.cos(ang - spread), p0[1] + L_ * math.sin(ang - spread))
            p2 = (p0[0] + L_ * math.cos(ang + spread), p0[1] + L_ * math.sin(ang + spread))
            ld.polygon([p0, p1, p2], fill=col + (34 if spd != 2 else 22,))
        lay = lay.filter(ImageFilter.GaussianBlur(6)).resize((w, h), Image.BILINEAR)
        out.append(Image.alpha_composite(base, lay).convert("RGB"))
    return out


def make_starburst(D, c1, c2, n=16, alpha=150) -> list[Image.Image]:
    """回転する放射線。対称なので 360/n 度ぶんの角度違いを用意すれば一周する"""
    half = D // 2
    lay = Image.new("RGBA", (half, half), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    c = half / 2
    for i in range(n):
        a0 = 2 * math.pi * i / n
        a1 = a0 + math.pi / n
        d.polygon([(c, c), (c + half * math.cos(a0), c + half * math.sin(a0)),
                   (c + half * math.cos(a1), c + half * math.sin(a1))],
                  fill=(c1 if i % 2 else c2) + (alpha,))
    lay = lay.resize((D, D), Image.BILINEAR)
    rad = ImageChops.invert(Image.radial_gradient("L").resize((D, D)))
    lay.putalpha(ImageChops.multiply(lay.getchannel("A"), rad))
    step = 360 / n
    return [lay.rotate(step * k / 10, resample=Image.BICUBIC) for k in range(10)]

# ============================================================
# 吹き出し・カード
# ============================================================

class Bubble:
    def __init__(self, w, h, L):
        self.w, self.h, self.L = w, h, L
        self.u = w / 1080 * L["ts"]
        self.cache = {}

    def layout(self, text):
        u = self.u
        maxw = int(self.w * 0.80)
        maxh = self.h * self.L["bubble_max"] - 90 * u
        f, lines = fit(text, maxw, 50 * u, 34 * u, 3)
        while len(lines) * f.size * 1.32 > maxh and f.size > 30 * u:
            f, lines = fit(text, maxw, f.size * 0.9, 30 * u, 3)
        return f, lines

    def render(self, text, n, ci) -> Image.Image:
        key = (text, n, ci)
        if key in self.cache:
            return self.cache[key]
        if len(self.cache) > 400:
            self.cache.clear()
        u, w = self.u, self.w
        f, lines = self.layout(text)
        lh = f.size * 1.32
        bw = int(w * 0.90)
        tag_h = int(46 * u)
        body_h = int(len(lines) * lh + 54 * u)
        tail = int(34 * u)
        H = tag_h // 2 + body_h + tail
        col = CHARS[ci]["color"]
        S = 2
        big = Image.new("RGBA", (bw * S, H * S), (0, 0, 0, 0))
        bd = ImageDraw.Draw(big)
        top = tag_h // 2
        ow = int(6 * u * S)
        sx = clamp(CHARS[ci]["x"] * w - (w - bw) / 2, 70 * u, bw - 70 * u)
        tri = [((sx - 30 * u) * S, (top + body_h - 4) * S), ((sx + 30 * u) * S, (top + body_h - 4) * S),
               (sx * S, (H - 2) * S)]
        bd.polygon(tri, fill=col + (255,))
        bd.rounded_rectangle((0, top * S, bw * S - 1, (top + body_h) * S), radius=int(34 * u * S),
                             fill=(255, 255, 255, 255), outline=col + (255,), width=ow)
        img = big.resize((bw, H), Image.LANCZOS)
        d = ImageDraw.Draw(img)
        tagimg = pill(CHARS[ci]["name"], 28 * u, col, outline=(255, 255, 255))
        img.alpha_composite(tagimg, (int(28 * u), 0))
        # 文字送り
        y = top + 32 * u
        left = n
        for ln in lines:
            if left <= 0:
                break
            part = ln[:left]
            left -= len(ln)
            d.text((bw / 2 - f.getlength(ln) / 2, y), part, font=f, fill=COL["out"])
            y += lh
        self.cache[key] = img
        return img


def make_card(item, w, u, n_sources, big=False, border=None) -> Image.Image:
    cw = int(w * 0.88)
    pad = int(34 * u)
    title_f, tlines = fit(clean(item.get("title", "")), cw - pad * 2, (86 if big else 76) * u, 44 * u, 2)
    art_f, alines = fit(clean(item.get("artist", "")), cw - pad * 2, (48 if big else 44) * u, 30 * u, 1)
    chips = []
    if big:
        if item.get("genre"):
            chips.append(pill(clean(item["genre"]), 30 * u, (70, 40, 130)))
        if item.get("ranks") and n_sources:
            chips.append(pill(f"{len(item['ranks'])}/{n_sources}チャートにランクイン", 30 * u, (70, 40, 130)))
        if item.get("tieup"):
            t_ = clean(item["tieup"])
            if len(t_) > 22:
                t_ = t_[:21] + "…"
            chips.append(pill(t_, 30 * u, (150, 40, 100)))
    # チップの折り返し
    rows, cur, cur_w = [], [], 0
    for c in chips:
        if cur and cur_w + c.width + 12 * u > cw - pad * 2:
            rows.append(cur)
            cur, cur_w = [], 0
        cur.append(c)
        cur_w += c.width + 12 * u
    if cur:
        rows.append(cur)
    th = len(tlines) * title_f.size * 1.22
    ah = art_f.size * 1.3
    chip_h = sum(r[0].height + 10 * u for r in rows)
    H = int(pad * 1.6 + th + ah + chip_h + (10 * u if rows else 0))
    S = 2
    img = Image.new("RGBA", (cw * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bcol = border or COL["pink"]
    d.rounded_rectangle((0, 0, cw * S - 1, H * S - 1), radius=int(30 * u * S), fill=COL["panel"] + (235,),
                        outline=bcol + (255,), width=int(6 * u * S))
    d.rounded_rectangle((int(18 * u * S), int(26 * u * S), int(30 * u * S), int((H - 26 * u) * S)),
                        radius=int(6 * u * S), fill=bcol + (255,))
    img = img.resize((cw, H), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    y = pad * 0.8
    for ln in tlines:
        d.text((cw / 2, y), ln, font=title_f, fill=COL["text"], anchor="ma",
               stroke_width=max(int(2 * u), 1), stroke_fill=COL["out"])
        y += title_f.size * 1.22
    d.text((cw / 2, y + 4 * u), alines[0] if alines else "", font=art_f, fill=COL["sub"], anchor="ma")
    y += ah + 10 * u
    for r in rows:
        tot = sum(c.width for c in r) + 12 * u * (len(r) - 1)
        x = cw / 2 - tot / 2
        for c in r:
            img.alpha_composite(c, (int(x), int(y)))
            x += c.width + 12 * u
        y += r[0].height + 10 * u
    return img

# ============================================================
# セリフ
# ============================================================

def quick_line(item) -> str:
    if item.get("quick_line"):
        return clean(item["quick_line"])
    m = item.get("move") or ""
    if m == "NEW":
        return "初登場でいきなりランクイン！"
    if m == "再":
        return "再ランクイン！返り咲きだ！"
    if m.startswith("↑"):
        return f"{m[1:]}ランクアップ！勢いがすごい！"
    if m.startswith("↓"):
        return "まだまだ上位で粘ってる！"
    if m == "→":
        return "前回と同じ順位をキープ！"
    return "要チェックの1曲！"


def reaction_line(item, n_sources) -> str:
    if item.get("tieup"):
        t = clean(item["tieup"])
        return (t[:18] + "…" if len(t) > 19 else t) + "としても話題！"
    if item.get("ranks") and n_sources:
        return f"{len(item['ranks'])}つのチャートにランクイン！"
    return quick_line(item)

# ============================================================
# 効果音(その場で合成するオリジナル。著作物は使わない)
# ============================================================

class SFX:
    SR = 44100

    def __init__(self, seconds):
        self.buf = array.array("f", bytes(4 * int(seconds * self.SR + self.SR)))
        self.rnd = random.Random(11)

    def _add(self, t0, samples):
        i0 = int(t0 * self.SR)
        b = self.buf
        for k, v in enumerate(samples):
            j = i0 + k
            if 0 <= j < len(b):
                b[j] += v

    def whoosh(self, t0, dur=0.32, amp=0.16):
        n, y, out = int(dur * self.SR), 0.0, []
        for k in range(n):
            x = k / n
            a = 0.02 + 0.35 * x
            y += a * (self.rnd.uniform(-1, 1) - y)
            out.append(y * amp * 2.2 * math.sin(math.pi * x))
        self._add(t0, out)

    def pop(self, t0, f=880, dur=0.16, amp=0.22):
        n, ph, out = int(dur * self.SR), 0.0, []
        for k in range(n):
            x = k / n
            ph += 2 * math.pi * f * (1 + 0.6 * x) / self.SR
            out.append(math.sin(ph) * amp * math.exp(-x * 6))
        self._add(t0, out)

    def chime(self, t0, amp=0.16):
        for i, f in enumerate((1047, 1319, 1568)):
            n, out = int(0.9 * self.SR), []
            for k in range(n):
                x = k / self.SR
                out.append(amp * math.exp(-x * 4) * (math.sin(2 * math.pi * f * x) + 0.3 * math.sin(4 * math.pi * f * x)))
            self._add(t0 + i * 0.07, out)

    def drumroll(self, t0, dur, amp=0.30):
        t = 0.0
        while t < dur:
            g = amp * (0.25 + 0.75 * t / dur)
            n = int(0.035 * self.SR)
            y, out = 0.0, []
            for k in range(n):
                y += 0.5 * (self.rnd.uniform(-1, 1) - y)
                out.append(y * g * math.exp(-k / n * 5))
            self._add(t0 + t, out)
            t += 1 / 24
        # 最後の「ドン」
        n, out = int(0.25 * self.SR), []
        for k in range(n):
            x = k / self.SR
            out.append(amp * 1.6 * math.sin(2 * math.pi * 70 * x) * math.exp(-x * 14))
        self._add(t0 + dur, out)

    def crash(self, t0, dur=1.8, amp=0.20):
        n, prev, out = int(dur * self.SR), 0.0, []
        for k in range(n):
            x = self.rnd.uniform(-1, 1)
            out.append((x - prev) * amp * math.exp(-k / self.SR * 2.6))
            prev = x
        self._add(t0, out)

    def fanfare(self, t0, amp=0.13):
        notes = [(0.00, 523, 0.14), (0.14, 659, 0.14), (0.28, 784, 0.14), (0.42, 1047, 1.1)]
        for st, f, du in notes:
            n, out = int(du * self.SR), []
            for k in range(n):
                x = k / self.SR
                env = min(1, x / 0.01) * math.exp(-x * (1.8 if du > 0.5 else 5))
                v = sum(math.sin(2 * math.pi * f * h * x) / h for h in (1, 2, 3, 4))
                out.append(amp * env * v)
            self._add(t0 + st, out)

    def write(self, path, seconds):
        n = int(seconds * self.SR)
        peak = max(1e-6, max(abs(v) for v in self.buf[:n]))
        g = min(1.0, 0.7 / peak)
        pcm = array.array("h", (int(clamp(v * g, -1, 1) * 32767) for v in self.buf[:n]))
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.SR)
            wf.writeframes(pcm.tobytes())

# ============================================================
# シーン構成
# ============================================================

class Scene:
    def __init__(self, dur, draw=None, lines=(), mode=lambda t: "idle", sfx=(), light_speed=1.0,
                 confetti=False, post=None, faces=None):
        self.dur, self.draw, self.lines, self.mode = dur, draw, list(lines), mode
        self.faces = faces  # (t, キャラ番号) -> "normal" / "gekiga"(劇画顔) / "dere"(デレ顔)
        self.sfx, self.light_speed, self.confetti, self.post = list(sfx), light_speed, confetti, post


def build_scenes(items, meta, w, h, L):
    u = w / 1080 * L["ts"]
    n = len(items)
    ns = meta.get("n_sources") or 0
    num_bottom = h * L["num_bottom"]
    card_top = h * L["card_top"]
    card_max = h * L["card_max"]
    zone_mid = (h * L["header"] + 120 * u + h * L["bubble_top"]) / 2
    burst = make_starburst(int(w * 1.3), (255, 210, 80), (255, 80, 160))
    scenes = []

    def fit_card(card):
        if card.height > card_max:
            k = card_max / card.height
            card = card.resize((int(card.width * k), int(card.height * k)), Image.LANCZOS)
        return card

    def num_sprite(i, medal=None):
        tops = {None: ((255, 255, 255), (160, 225, 255)), "gold": ((255, 246, 170), (240, 160, 20)),
                "silver": ((255, 255, 255), (165, 178, 200)), "bronze": ((255, 210, 170), (200, 115, 55))}
        t, b = tops[medal]
        num = deco_text(str(i + 1), L["num"] * w / 1080 * (1.12 if medal else 1), t, b)
        unit = deco_text("位", L["num"] * w / 1080 * 0.36, t, b)
        img = Image.new("RGBA", (num.width + unit.width + int(10 * u), num.height), (0, 0, 0, 0))
        img.alpha_composite(num, (0, 0))
        img.alpha_composite(unit, (num.width + int(6 * u), num.height - unit.height - int(8 * u)))
        return img

    def badge(item):
        m = item.get("move")
        if not m:
            return None
        if m == "NEW":
            return pill("NEW!", 44 * u, (40, 200, 140), outline=(255, 255, 255))
        if m == "再":
            return pill("再ランクイン", 40 * u, (240, 160, 30), outline=(255, 255, 255))
        col = (240, 70, 90) if m.startswith("↑") else (70, 120, 240) if m.startswith("↓") else (120, 110, 150)
        return pill(f"前回比 {m}", 40 * u, col, outline=(255, 255, 255))

    def rank_board(i, item, medal=None, card_border=None, big=False, t_card=0.12):
        nspr = num_sprite(i, medal)
        card = fit_card(make_card(item, w, u, ns, big=big, border=card_border))
        bspr = badge(item)
        nx = 46 * u
        ny = num_bottom - nspr.height

        def draw(frame, t):
            if medal == "gold":
                paste(frame, burst[int(t * 12) % len(burst)], nx + nspr.width * 0.4 - burst[0].width / 2,
                      ny + nspr.height / 2 - burst[0].height / 2, alpha=ease_out(t / 0.4))
            sc = lerp(1.9, 1.0, ease_out(t / 0.22)) if t < 0.22 else 1.0
            paste(frame, nspr, nx, ny, alpha=ease_out(t / 0.12), scale=sc)
            if bspr is not None and t > 0.30:
                bs = ease_back((t - 0.30) / 0.25)
                paste(frame, bspr, w - bspr.width - 46 * u, ny + nspr.height * 0.42, scale=max(bs, 0.05))
            if t > t_card:
                e = ease_out((t - t_card) / 0.30)
                paste(frame, card, (w - card.width) / 2 + (1 - e) * w, card_top)
        return draw

    def all_cheer_until(t_end, after="idle"):
        return lambda t: "cheer" if t < t_end else after

    # ---- オープニング ----
    logo_a = deco_text(SHOW_TITLE[0], 150 * u, (255, 255, 255), (255, 170, 215), shear=-0.18)
    logo_b = deco_text(SHOW_TITLE[1], 104 * u, COL["gold"], (255, 130, 40), shadow=COL["cyan"], shear=-0.18)
    logo_c = deco_text(f"TOP{n}", 130 * u, (160, 235, 255), (60, 160, 255), shadow=COL["pink"])

    def draw_open(frame, t):
        paste(frame, burst[int(t * 10) % len(burst)], (w - burst[0].width) / 2, zone_mid - burst[0].height / 2,
              alpha=0.7 * ease_out((t - 0.2) / 0.5))
        y = zone_mid - (logo_a.height + logo_b.height + logo_c.height) / 2
        for spr, st in ((logo_a, 0.20), (logo_b, 0.45), (logo_c, 0.70)):
            if t > st:
                sc = lerp(2.0, 1.0, ease_out((t - st) / 0.22))
                paste(frame, spr, (w - spr.width) / 2, y, alpha=ease_out((t - st) / 0.1), scale=sc)
            y += spr.height + 6 * u

    scenes.append(Scene(
        T_OPEN, draw_open, mode=lambda t: "enter" if t < 1.2 else "idle",
        lines=[(1.1, 2.5, MIC, "さあ始まりました！"),
               (2.5, 4.3, RECO, f"{ns}つのチャートを集計した最新の邦楽TOP{n}！" if ns else f"最新の邦楽TOP{n}！"),
               (4.3, T_OPEN, ONPU, "1位はだれだ〜！？")],
        sfx=[("whoosh", 0.0), ("pop", 0.20), ("pop", 0.45), ("pop", 0.70), ("chime", 1.0)]))

    # ---- 10位〜4位 ----
    rot = [MIC, ONPU, RECO]
    for k, i in enumerate(range(n - 1, 2, -1)):
        it = items[i]
        scenes.append(Scene(T_QUICK, rank_board(i, it), lines=[(0.45, T_QUICK, rot[k % 3], quick_line(it))],
                            sfx=[("whoosh", 0.0), ("pop", 0.03)]))

    # ---- いよいよTOP3 ----
    if n >= 3:
        top3 = deco_text("TOP 3", 210 * u, (255, 246, 170), (240, 150, 20))
        sub = deco_text("いよいよ発表！", 70 * u, (255, 255, 255), (255, 190, 225))

        def draw_top3(frame, t):
            paste(frame, burst[int(t * 14) % len(burst)], (w - burst[0].width) / 2, zone_mid - burst[0].height / 2)
            sc = lerp(2.2, 1.0, ease_back(t / 0.35))
            paste(frame, top3, (w - top3.width) / 2, zone_mid - top3.height * 0.75, scale=sc)
            if t > 0.5:
                paste(frame, sub, (w - sub.width) / 2, zone_mid + top3.height * 0.35, alpha=ease_out((t - 0.5) / 0.2))
        scenes.append(Scene(T_TOP3, draw_top3, mode=all_cheer_until(1.2),
                            faces=lambda t, ci: "gekiga" if ci == ONPU and t >= 1.3 else "normal",
                            lines=[(0.3, 1.3, MIC, "いよいよTOP3の発表！"), (1.3, T_TOP3, ONPU, "ドキドキ…！")],
                            sfx=[("chime", 0.05), ("whoosh", 0.0)]))

    # ---- 3位・2位 ----
    for i, medal, border in ((2, "bronze", (214, 140, 80)), (1, "silver", (205, 213, 223))):
        if i >= n:
            continue
        it = items[i]
        hit = clean(it.get("hitokoto")) or quick_line(it)
        scenes.append(Scene(
            T_PODIUM, rank_board(i, it, medal, border, big=True, t_card=0.5),
            lines=[(0.25, 1.4, MIC, f"第{i + 1}位！"), (1.4, 5.3, RECO, hit),
                   (5.3, T_PODIUM, ONPU, reaction_line(it, ns))],
            sfx=[("whoosh", 0.0), ("pop", 0.03), ("chime", 0.5)]))

    # ---- ドラムロール → 1位 ----
    if n >= 1:
        drum = deco_text("第1位は…", 150 * u, (255, 246, 170), (240, 150, 20))
        # 劇画顔のアップ(コマ割り風カットイン)
        cut_y = zone_mid + drum.height * 0.15
        pw = int(w * 0.29)
        ph = int(min(h * 0.135, h * L["bubble_top"] - cut_y - 40 * u))  # 吹き出しに重ならない高さ
        face_box = {"mic": (0.50, 0.25, 0.66), "reco": (0.50, 0.36, 0.52), "onpu": (0.46, 0.62, 0.72)}
        panels = []
        for c in CHARS:
            big = draw_character(c["id"], int(pw * 1.9), True, False, "down", "gekiga")
            fx, fy, fw = face_box[c["id"]]
            cw_ = big.width * fw
            ch_ = cw_ * ph / pw
            crop = big.crop((int(big.width * fx - cw_ / 2), int(big.height * fy - ch_ / 2),
                             int(big.width * fx + cw_ / 2), int(big.height * fy + ch_ / 2))).resize((pw, ph), Image.LANCZOS)
            pan = Image.new("RGBA", (pw, ph), c["color"] + (255,))
            pd = ImageDraw.Draw(pan)
            for k in range(0, pw + ph, int(14 * u)):  # 背景の斜線トーン
                pd.line([(k, 0), (k - ph, ph)], fill=tuple(int(v * 0.8) for v in c["color"]), width=max(int(4 * u), 2))
            pan.alpha_composite(crop)
            pd.rectangle((0, 0, pw - 1, ph - 1), outline=(255, 255, 255), width=max(int(7 * u), 3))
            pd.rectangle((0, 0, pw - 1, ph - 1), outline=COL["out"], width=max(int(3 * u), 2))
            panels.append(pan.rotate(-3 if len(panels) != 1 else 2, expand=True, resample=Image.BICUBIC))

        def draw_drum(frame, t):
            # 劇画風の集中線(2フレームごとにランダムに描き直してチラつかせる)
            r = random.Random(int(t * FPS) // 2)
            d = ImageDraw.Draw(frame)
            cx, cy = w / 2, (zone_mid + h * L["feet"]) / 2
            R = math.hypot(w, h)
            for _ in range(70):
                a = r.uniform(0, 2 * math.pi)
                r0 = r.uniform(0.32, 0.55) * min(w, h)
                wd = r.uniform(0.004, 0.012)
                p0 = (cx + r0 * math.cos(a), cy + r0 * math.sin(a))
                p1 = (cx + R * math.cos(a - wd), cy + R * math.sin(a - wd))
                p2 = (cx + R * math.cos(a + wd), cy + R * math.sin(a + wd))
                d.polygon([p0, p1, p2], fill=(205, 195, 240))
            sc = 1 + 0.05 * math.sin(t * 18)
            jx = math.sin(t * 47) * 4 * u
            lift = ease_out((t - 0.9) / 0.25) * drum.height * 0.55  # カットインの前に文字が上へ
            paste(frame, drum, (w - drum.width) / 2 + jx, zone_mid - drum.height / 2 - lift, scale=sc)
            for k, pan in enumerate(panels):
                st = 1.0 + k * 0.22
                if t < st:
                    continue
                x = w * (0.035 + k * 0.322) + math.sin(t * 40 + k) * 3 * u
                paste(frame, pan, x, cut_y, scale=lerp(1.6, 1.0, ease_out((t - st) / 0.15)))
        scenes.append(Scene(T_DRUM, draw_drum, mode=lambda t: "shake", faces=lambda t, ci: "gekiga",
                            lines=[(0.2, T_DRUM, ONPU, "ドゥルルルルル…！")], light_speed=4.0,
                            sfx=[("drumroll", 0.05), ("pop", 1.0), ("pop", 1.22), ("pop", 1.44)]))

        it = items[0]
        hit = clean(it.get("hitokoto")) or quick_line(it)
        white = Image.new("RGB", (w, h), (255, 255, 255))

        def flash(frame, t):
            return Image.blend(frame, white, 0.85 * (1 - t / 0.35)) if t < 0.35 else frame
        scenes.append(Scene(
            T_FIRST, rank_board(0, it, "gold", COL["gold"], big=True, t_card=0.45),
            mode=all_cheer_until(2.6, "idle"), confetti=True, post=flash,
            # 1位発表でみんなデレ顔 → オンプは「おめでとう」でもう一度デレる
            faces=lambda t, ci: "dere" if t < 1.6 or (ci == ONPU and t >= 7.0) else "normal",
            lines=[(0.5, 3.2, MIC, f"第1位は{clean(it.get('artist'))}！"), (3.2, 7.0, RECO, hit),
                   (7.0, T_FIRST, ONPU, "おめでとう〜！！")],
            sfx=[("crash", 0.0), ("fanfare", 0.05)]))

    # ---- エンディング ----
    end1 = deco_text("フォローして", 92 * u, (255, 255, 255), (255, 190, 225))
    end2 = deco_text("次回もチェック！", 92 * u, COL["gold"], (255, 130, 40), shadow=COL["cyan"])
    end3 = pill("全曲のMVはプロフィールから", 38 * u, COL["dark"], outline=COL["cyan"])
    credit = clean(meta.get("credit"))
    cf, clines = fit(credit, int(w * 0.84), 24 * u, 18 * u, 3) if credit else (None, [])

    def draw_end(frame, t):
        y = zone_mid - (end1.height + end2.height + end3.height + 30 * u) / 2
        for spr, st in ((end1, 0.1), (end2, 0.3), (end3, 0.6)):
            if t > st:
                paste(frame, spr, (w - spr.width) / 2, y, scale=lerp(1.6, 1, ease_out((t - st) / 0.2)),
                      alpha=ease_out((t - st) / 0.1))
            y += spr.height + 15 * u
        if clines and t > 0.8:
            d = ImageDraw.Draw(frame)
            yy = h * L["bubble_top"] - len(clines) * cf.size * 1.35 - 20 * u
            for ln in clines:
                d.text((w / 2, yy), ln, font=cf, fill=COL["sub"], anchor="ma")
                yy += cf.size * 1.35
    scenes.append(Scene(T_END, draw_end, mode=lambda t: "wave",
                        lines=[(0.2, 2.3, MIC, f"以上、最新の邦楽TOP{n}でした！"), (2.3, T_END, ONPU, "またね〜！")],
                        sfx=[("chime", 0.15)]))
    return scenes

# ============================================================
# 描画ループ
# ============================================================

class Cast:
    def __init__(self, w, h, L):
        self.w, self.h = w, h
        self.S = int(w * L["char"])
        self.feet = h * L["feet"]
        self.cache = {}

    def sprite(self, ci, mouth, blink, arms, face="normal"):
        if face != "normal":
            blink = False
        k = (ci, mouth, blink, arms, face)
        if k not in self.cache:
            self.cache[k] = draw_character(CHARS[ci]["id"], self.S, mouth, blink, arms, face)
        return self.cache[k]

    def draw(self, frame, t, gt, mode, speaker, line_t, faces=None):
        for ci, c in enumerate(CHARS):
            face = (faces(t, ci) if faces else None) or "normal"
            ph = ci * 1.7
            talking = speaker == ci and line_t is not None
            mouth = talking and (int(gt * 9 + ci) % 2 == 0) and line_t < 2.5
            blink = (gt * 1000 + ci * 900) % 3300 < 110
            arms = "down"
            dx, dy = 0.0, math.sin(gt * 2 * math.pi * 0.9 + ph) * 6 * self.S / 300
            if mode == "cheer":
                arms = "up"
                dy -= abs(math.sin(gt * 7 + ph)) * self.S * 0.16
                mouth = True
            elif mode == "wave":
                arms = "up" if int(gt * 3.5 + ci) % 2 == 0 else "down"
            elif mode == "shake":
                dx = math.sin(gt * 50 + ph) * self.S * 0.02
            elif mode == "enter":
                p = (t - ci * 0.18) / 0.7
                dy -= (1 - ease_bounce(p)) * self.h * 0.6
            if talking and line_t < 0.18:  # 話し始めにぴょこっと跳ねる
                dy -= math.sin(line_t / 0.18 * math.pi) * self.S * 0.08
                arms = "up" if mode != "shake" else arms
            spr = self.sprite(ci, mouth, blink, arms, face)
            paste(frame, spr, c["x"] * self.w - spr.width / 2 + dx, self.feet - spr.height + dy + self.S * 0.03)


def make_confetti(w, h, n=170, seed=3):
    r = random.Random(seed)
    cols = [COL["pink"], COL["cyan"], COL["gold"], (255, 255, 255), (140, 255, 140), (190, 120, 255)]
    return [(r.uniform(0, w), r.uniform(-h * 1.0, -20), r.uniform(h * 0.18, h * 0.34), r.uniform(1.5, 4),
             r.uniform(0, 6.28), r.uniform(10, 26) * w / 1080, r.choice(cols), r.uniform(20, 60) * w / 1080)
            for _ in range(n)]


def draw_confetti(frame, parts, t, h):
    d = ImageDraw.Draw(frame)
    for x0, y0, vy, fr, ph, sz, col, sway in parts:
        y = y0 + vy * t
        if y > h + 40 or y < -40:
            continue
        x = x0 + math.sin(t * fr + ph) * sway
        a = t * fr * 1.3 + ph
        cw, ch = sz * abs(math.cos(a)) + 2, sz * 0.55
        ca, sa = math.cos(a * 0.6), math.sin(a * 0.6)
        pts = [(x + px * ca - py * sa, y + px * sa + py * ca) for px, py in
               ((-cw, -ch), (cw, -ch), (cw, ch), (-cw, ch))]
        d.polygon(pts, fill=col)


def draw_wipe(frame, p, w, h, entering):
    """entering=False: 左から覆う / True: 右へ抜けて新しい画面を見せる"""
    d = ImageDraw.Draw(frame)
    slant = h * 0.35
    xe = lerp(0, w + slant, ease_out(p) if entering else p ** 1.5)
    band = 34 * w / 1080
    if entering:
        d.polygon([(xe, 0), (w, 0), (w, h), (xe - slant, h)], fill=COL["pink"])
        d.polygon([(xe - band, 0), (xe, 0), (xe - slant, h), (xe - slant - band, h)], fill=COL["gold"])
    else:
        d.polygon([(0, 0), (xe, 0), (xe - slant, h), (0, h)], fill=COL["pink"])
        d.polygon([(xe, 0), (xe + band, 0), (xe + band - slant, h), (xe - slant, h)], fill=COL["cyan"])


def current_line(lines, t):
    for st, en, ci, text in lines:
        if st <= t < en:
            return st, ci, clean(text)
    return None


def render(items, meta, out_path: Path, size, kind, sfx_on=True):
    w, h = size
    L = LAYOUTS[kind]
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg が見つかりません")
    print(f"[info] {kind}: 背景・キャラクターを準備中...")
    bgs = make_backgrounds(w, h, L, meta)
    scenes = build_scenes(items, meta, w, h, L)
    cast = Cast(w, h, L)
    bubble = Bubble(w, h, L)
    confetti = make_confetti(w, h)
    total = sum(round(s.dur * FPS) for s in scenes) / FPS

    with tempfile.TemporaryDirectory() as td:
        audio_in = ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=44100:cl=mono"]
        if sfx_on:
            sfx = SFX(total)
            gt0 = 0.0
            for s in scenes:
                for name, at in s.sfx:
                    if name == "drumroll":
                        sfx.drumroll(gt0 + at, s.dur - at - 0.1)
                    else:
                        getattr(sfx, name)(gt0 + at)
                gt0 += round(s.dur * FPS) / FPS
            wav = Path(td) / "sfx.wav"
            sfx.write(wav, total)
            audio_in = ["-i", str(wav)]
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
               *audio_in, "-shortest", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        frames = 0
        light_pos = 0.0
        for si, sc in enumerate(scenes):
            nf = round(sc.dur * FPS)
            for fi in range(nf):
                t = fi / FPS
                gt = frames / FPS
                light_pos += sc.light_speed / FPS
                frame = bgs[int(light_pos / LIGHT_CYCLE * N_LIGHT) % N_LIGHT].copy()
                if sc.confetti:  # 文字が読めるよう、紙吹雪はテロップより奥に
                    draw_confetti(frame, confetti, t, h)
                if sc.draw:
                    sc.draw(frame, t)
                ln = current_line(sc.lines, t)
                speaker, line_t = (ln[1], t - ln[0]) if ln else (None, None)
                cast.draw(frame, t, gt, sc.mode(t), speaker, line_t, sc.faces)
                if ln:
                    nvis = int(line_t * TYPE_CPS) + 1
                    b = bubble.render(ln[2], min(nvis, len(ln[2])), ln[1])
                    pop = ease_back(line_t / 0.18) if line_t < 0.18 else 1.0
                    paste(frame, b, (w - b.width) / 2, h * L["bubble_top"], scale=max(0.6 + 0.4 * pop, 0.1))
                if sc.post:
                    frame = sc.post(frame, t)
                if si > 0 and t < WIPE:
                    draw_wipe(frame, t / WIPE, w, h, entering=True)
                if si < len(scenes) - 1 and sc.dur - t <= WIPE:
                    draw_wipe(frame, 1 - (sc.dur - t) / WIPE, w, h, entering=False)
                proc.stdin.write(frame.tobytes())
                frames += 1
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError("ffmpeg でのエンコードに失敗しました")
    print(f"[info] 動画出力: {out_path}({frames / FPS:.1f}秒)")
    return out_path


def make_videos(out_dir: Path, only: str | None = None, sfx_on: bool = True) -> list[Path]:
    global FONT_PATH
    FONT_PATH = FONT_PATH or find_font()
    items = json.loads((out_dir / "ranking.json").read_text(encoding="utf-8"))
    meta_file = out_dir / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    meta.setdefault("date_s", out_dir.name.replace("-", "/"))
    outs = []
    for kind, size in SIZES.items():
        if only and kind != only:
            continue
        outs.append(render(items, meta, out_dir / f"{kind}.mp4", size, kind, sfx_on))
    return outs


POSES = {  # ファイル名: (口を開ける, 目を閉じる, 腕, 顔)
    "normal": (False, False, "down", "normal"),
    "talk": (True, False, "down", "normal"),
    "cheer": (True, False, "up", "normal"),
    "smile": (False, True, "down", "normal"),
    "wave": (False, False, "up", "normal"),
    "gekiga": (False, False, "down", "gekiga"),      # 劇画顔(食いしばり)
    "gekiga_shout": (True, False, "up", "gekiga"),   # 劇画顔(叫ぶ)
    "dere": (False, False, "down", "dere"),          # デレ顔
    "dere_cheer": (True, False, "up", "dere"),       # デレ顔(バンザイ)
}


def export_characters(dest: Path, size: int = 1200) -> list[Path]:
    """キャラクターを1体ずつ背景透過PNGで書き出す(アイコン・サムネ・グッズ用)。
    size = 横幅(px)。高さは横幅の1.3倍。余白は自動でトリミング"""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for c in CHARS:
        for pose, (mouth, blink, arms, face) in POSES.items():
            img = trim(draw_character(c["id"], size, mouth, blink, arms, face))
            pad = int(size * 0.04)
            canvas = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
            canvas.alpha_composite(img, (pad, pad))
            p = dest / f"{c['id']}_{pose}.png"
            canvas.save(p, optimize=True)
            out.append(p)
    print(f"[info] キャラクター画像 {len(out)} 枚を書き出しました: {dest}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", help="ranking.json があるフォルダ(例: outputs/2026-10-06)。"
                                    "--characters のときは画像の保存先")
    ap.add_argument("--only", choices=list(SIZES))
    ap.add_argument("--no-sfx", action="store_true", help="効果音を入れない(無音)")
    ap.add_argument("--characters", action="store_true",
                    help="動画は作らず、キャラクターを1体ずつ背景透過PNGで書き出す")
    ap.add_argument("--char-size", type=int, default=1200, help="キャラクター画像の横幅(px)")
    a = ap.parse_args()
    if a.characters:
        export_characters(Path(a.out_dir), a.char_size)
    else:
        make_videos(Path(a.out_dir), a.only, not a.no_sfx)
