#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J-MUSIC TOP10
「複数の公式チャートを独自に集計する、最新邦楽TOP10」

CM TREND WATCH と同じ「実行 → コピペ投稿文が出る」仕組みの邦楽版です。
好きなタイミングで手動実行します(毎朝の自動実行は前提にしていません)。

実行すると:
  1. 複数の確かなデータソースから最新の順位を集める
       - Billboard JAPAN Hot 100(販売・ストリーミング・DL・ラジオ・動画などの合算)
       - Billboard JAPAN Top User Generated Songs(SNS・UGCでの使用)
       - 週間 USEN HIT J-POPランキング(有線のオンエア・リクエスト)
         ↑ この3つは Claude API の web_fetch で公式ページを読み取り、順位を抽出
       - Apple Music 日本 Top Songs(Apple公式JSONフィードから直接取得)
       - manual_charts/ に置いたCSV(オリコン、TikTok、LINE MUSIC、Spotify など)
       - YouTube 新着MVの勢い(補助指標)
  2. 曲名・アーティスト名を正規化して同じ曲を突き合わせ、重み付きポイントで総合順位を算出
  3. Claude APIが邦楽かどうかを判定(海外アーティストは除外)
  4. 上位曲の公式MVをYouTubeで探し、Claudeがデータに基づく一言分析を作成
  5. 前回実行時からの順位変動(NEW / ↑ / ↓ / → / 再)を付けて
     X / Threads / Instagram / TikTok / note 用のコピペ投稿文を生成
  6. outputs/日付/ に保存し、Googleドライブにも保存(Discord Webhook設定時は通知)

使い方:
  必須: 環境変数 ANTHROPIC_API_KEY
  任意: YOUTUBE_API_KEY(MVリンク・再生数・新着MVの勢い。未設定ならYouTube検索リンクで代用)
        DISCORD_WEBHOOK_URL
        GDRIVE_SYNC_DIR(パソコン版Googleドライブの同期フォルダ。例: G:/マイドライブ)

    python music_trend_watch.py                 # 通常実行
    python music_trend_watch.py --video         # 動画(reel.mp4 / feed.mp4)も生成
    python music_trend_watch.py --upload-drive  # Google Drive API で直接アップロード
    python music_trend_watch.py --drive-login   # (初回だけ)Googleドライブ用トークンを作る
    python music_trend_watch.py --demo          # APIキーなしで出力フォーマットを確認

  GitHub Actions での実行方法は README.md を参照(Actionsタブの「Run workflow」で手動実行)。

手動CSV(manual_charts/ フォルダ):
  ファイル名がソース名になります(例: oricon.csv, tiktok.csv)。列は rank,title,artist。
  ブラウザで見た最新の順位を入力して保存してください。stale_days より古いファイルは集計から外します。
"""

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============================================================
# 設定(必要に応じてここを編集)
# ============================================================

CONFIG = {
    "brand": "J-MUSIC TOP10",
    "tagline": "複数の公式チャートを独自集計した、最新の邦楽TOP10",
    "hashtags": "#邦楽 #邦楽ランキング #新曲 #JPOP #音楽好きと繋がりたい",
    "tiktok_hashtags": "#邦楽 #邦楽ランキング #今週の邦楽 #音楽好きと繋がりたい #おすすめ曲",
    "top_n": 10,

    # Claudeモデル(コスト重視なら claude-haiku-4-5-20251001。web_fetchは対応モデルのみ)
    "claude_model": "claude-sonnet-5-5",
    "web_fetch_tool": "web_fetch_20250910",

    # 公開日がこれより古いチャートは集計しない(週間チャートの更新漏れ対策)
    "stale_days": 21,

    # ── Claude web_fetch で公式ページから読み取るチャート ──
    # weight: 総合順位への影響度。max_rank: 何位まで使うか
    "web_sources": [
        {"id": "billboard_hot100", "label": "Billboard JAPAN Hot 100",
         "url": "https://www.billboard-japan.com/charts/detail?a=hot100",
         "domains": ["billboard-japan.com"], "weight": 3.0, "max_rank": 50},
        {"id": "billboard_ugc", "label": "Billboard JAPAN Top User Generated Songs",
         "url": "https://www.billboard-japan.com/charts/detail?a=ugc",
         "domains": ["billboard-japan.com"], "weight": 1.5, "max_rank": 20},
        {"id": "usen_jpop", "label": "週間 USEN HIT J-POPランキング",
         "url": "https://music.usen.com/ch/A26/",
         "domains": ["music.usen.com"], "weight": 1.5, "max_rank": 25},
    ],

    # ── Apple Music 公式フィード(JSONで正確に取得できる) ──
    "apple_music": {"enabled": True, "id": "apple_music", "label": "Apple Music 日本 Top Songs",
                    "url": "https://rss.marketingtools.apple.com/api/v2/jp/music/most-played/50/songs.json",
                    "weight": 1.5, "max_rank": 50},

    # ── 手動CSV(manual_charts/ファイル名.csv)。ここにないファイル名は csv_default_weight ──
    "csv_sources": {
        "oricon": {"label": "オリコン週間合算シングル", "weight": 3.0, "max_rank": 50},
        "tiktok": {"label": "TikTok 人気楽曲", "weight": 1.5, "max_rank": 20},
        "line_music": {"label": "LINE MUSIC 週間", "weight": 1.0, "max_rank": 50},
        "spotify": {"label": "Spotify 日本 週間", "weight": 1.5, "max_rank": 50},
    },
    "csv_default_weight": 1.0,

    # ── YouTube 新着MVの勢い(補助指標。YOUTUBE_API_KEY がある場合のみ) ──
    "youtube_discovery": {
        "enabled": True, "id": "youtube_new_mv", "label": "YouTube 新着MVの勢い",
        "weight": 1.0, "max_rank": 20, "lookback_days": 14, "max_candidates": 30,
        "min_duration_sec": 60, "max_duration_sec": 600,
        "queries": ["MV", "Music Video", "Official Music Video", "ミュージックビデオ",
                    "新曲 MV", "Official Lyric Video"],
    },

    # 邦楽判定に回す総合上位の曲数(海外曲を除いてもTOP10が埋まるよう多めに)
    "classify_pool": 30,

    # 投稿文に各チャートの順位を載せるか。
    # 各社チャートの順位を一覧で転載すると利用規約に触れる場合があるため既定は False
    # (False でも digest.txt には確認用として表示します)
    "show_source_ranks": False,

    # Googleドライブ上の保存先フォルダ名
    "drive_folder_name": "J-MUSIC TOP10",
}

JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUT_DIR = BASE_DIR / "outputs"
CHARTS_DIR = BASE_DIR / "manual_charts"
RANKINGS_FILE = DATA_DIR / "rankings.json"  # 実行日ごとのTOP10(順位変動に使用)
ARTISTS_FILE = DATA_DIR / "artists.json"    # 邦楽判定のキャッシュ(API費用の節約)

# ============================================================
# ユーティリティ
# ============================================================

def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "music-trend-watch/2.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def http_post_json(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=300) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:600]
        except Exception:
            detail = "(詳細取得不可)"
        hint = ""
        if e.code == 400 and ("credit" in detail or "billing" in detail.lower()):
            hint = "\n→ Anthropic APIのクレジット残高切れの可能性が高いです。" \
                   "platform.claude.com のBillingで残高を確認・チャージしてください。"
        raise RuntimeError(f"APIエラー HTTP {e.code} ({url}):\n{detail}{hint}") from None


def parse_iso_duration(s: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + sec


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def jst_now() -> datetime:
    return datetime.now(JST)


def warn(msg: str):
    print(f"[warn] {msg}", file=sys.stderr)


def parse_json_lenient(text: str, opener: str):
    """Claudeの返答から最初のJSON(opener='{' or '[')を取り出す"""
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    closer = "}" if opener == "{" else "]"
    start, end = text.find(opener), text.rfind(closer)
    if start == -1 or end <= start:
        raise RuntimeError("Claudeの返答からJSONを取り出せませんでした:\n" + text[:500])
    return json.loads(text[start:end + 1])

# ============================================================
# 曲名・アーティスト名の正規化(ソース間の突き合わせ用)
# ============================================================

BRACKETS = re.compile(r"[\(\[（【〔「『<＜].*?[\)\]）】〕」』>＞]")
FEAT = re.compile(r"\s(feat\.?|ft\.?|with|×|x)\s.*$", re.IGNORECASE)


def norm_key(s: str) -> str:
    raw = unicodedata.normalize("NFKC", s or "").lower().strip()
    t = BRACKETS.sub("", raw)
    t = FEAT.sub("", " " + t).strip()
    t = re.sub(r"[\s\W_]+", "", t)
    if not t:  # 括弧だけの曲名などは括弧ごと使う
        t = re.sub(r"[\s\W_]+", "", raw)
    return t


def artist_match(a: str, b: str) -> bool:
    return not a or not b or a in b or b in a


def has_japanese(s: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", s or ""))

# ============================================================
# 1. データソース収集
#   各ソースは {"id","label","weight","max_rank","period","entries":[{rank,title,artist}]} を返す
# ============================================================

CHART_EXTRACT_SYSTEM = """あなたは音楽チャートのデータ抽出係です。
web_fetchツールで指定されたURLのページを取得し、掲載されているランキングをそのまま抽出します。

厳守事項:
- ページに書かれている内容だけを使う。推測・記憶による補完は禁止
- 読み取れなかった順位は出力しない(欠番のままでよい)
- 曲名とアーティスト名はページの表記のまま
- 取得に失敗した場合は entries を空配列にし、error に理由を書く

出力はJSONオブジェクトのみ。前置き・後書き・コードブロック記号は不要:
{"chart_name": "ページ上のチャート名", "period": "集計期間・公開日のページ表記そのまま",
 "published_date": "公開日(または集計期間の最終日)を YYYY-MM-DD で。不明なら null",
 "entries": [{"rank": 1, "title": "曲名", "artist": "アーティスト名"}], "error": null}"""


def claude_messages(payload: dict) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY が設定されていません")
    return http_post_json("https://api.anthropic.com/v1/messages", payload,
                          {"x-api-key": api_key, "anthropic-version": "2023-06-01"})


def fetch_chart_with_claude(src: dict) -> dict:
    messages = [{"role": "user",
                 "content": f"次のURLのページを取得し、{src['max_rank']}位までを抽出してください。\n"
                            f"URL: {src['url']}"}]
    tools = [{"type": CONFIG["web_fetch_tool"], "name": "web_fetch", "max_uses": 3,
              "allowed_domains": src["domains"], "max_content_tokens": 80000}]
    resp, blocks = None, []
    for _ in range(4):  # pause_turn が返った場合は続きを要求する
        resp = claude_messages({"model": CONFIG["claude_model"], "max_tokens": 8000,
                                "system": CHART_EXTRACT_SYSTEM, "messages": messages, "tools": tools})
        blocks += resp.get("content", [])
        if resp.get("stop_reason") != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp["content"]}]

    for b in blocks:
        c = b.get("content") if b.get("type") == "web_fetch_tool_result" else None
        if isinstance(c, dict) and c.get("type") == "web_fetch_tool_error":
            warn(f"{src['label']}: ページ取得エラー ({c.get('error_code')})")

    final_text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    data = parse_json_lenient(final_text, "{")
    if data.get("error"):
        warn(f"{src['label']}: {data['error']}")
    return {"period": data.get("period"), "published_date": data.get("published_date"),
            "chart_name": data.get("chart_name"), "entries": data.get("entries") or []}


def fetch_apple_music(src: dict) -> dict:
    feed = http_get_json(src["url"]).get("feed", {})
    entries = [{"rank": i + 1, "title": r.get("name", ""), "artist": r.get("artistName", "")}
               for i, r in enumerate(feed.get("results", []))]
    updated = (feed.get("updated") or "")[:10] or None
    return {"period": f"{updated} 更新" if updated else None, "published_date": updated,
            "chart_name": feed.get("title"), "entries": entries}


def file_updated_at(f: Path) -> datetime:
    """ファイルの最終更新日時。GitHub Actions ではチェックアウト時刻になってしまうため、
    gitの最終コミット日時を優先する(ワークフローは fetch-depth: 0 でチェックアウト)"""
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%cI", "--", f.name],
                           cwd=f.parent, capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain", "--", f.name],
                               cwd=f.parent, capture_output=True, text=True, timeout=10).stdout.strip()
        if r.returncode == 0 and r.stdout.strip() and not dirty:
            return datetime.fromisoformat(r.stdout.strip()).astimezone(JST)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return datetime.fromtimestamp(f.stat().st_mtime, JST)


def read_manual_csvs(today: datetime) -> list[dict]:
    """manual_charts/*.csv を読む。先頭が _ のファイル(テンプレート)は無視"""
    if not CHARTS_DIR.exists():
        CHARTS_DIR.mkdir(parents=True)
        (CHARTS_DIR / "_template.csv").write_text(
            "rank,title,artist\n1,曲名,アーティスト名\n", encoding="utf-8-sig")
        print(f"[info] {CHARTS_DIR} を作成しました(oricon.csv などを置くと集計に入ります)")
    out = []
    for f in sorted(CHARTS_DIR.glob("*.csv")):
        if f.name.startswith("_"):
            continue
        sid = f.stem
        conf = CONFIG["csv_sources"].get(sid, {})
        mtime = file_updated_at(f)
        age = (today - mtime).days
        if age > CONFIG["stale_days"]:
            warn(f"{f.name} は{age}日前の更新のため集計から外しました(最新の順位に更新してください)")
            continue
        entries = []
        with f.open(encoding="utf-8-sig", newline="") as fp:
            for row in csv.DictReader(fp):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                rank = row.get("rank") or row.get("順位")
                title = row.get("title") or row.get("曲名")
                artist = row.get("artist") or row.get("アーティスト") or row.get("アーティスト名")
                if rank and rank.isdigit() and title:
                    entries.append({"rank": int(rank), "title": title, "artist": artist or ""})
        out.append({"id": sid, "label": conf.get("label", sid),
                    "weight": conf.get("weight", CONFIG["csv_default_weight"]),
                    "max_rank": conf.get("max_rank", max([e["rank"] for e in entries] or [1])),
                    "period": f"手動入力({mtime.strftime('%Y-%m-%d')}更新)",
                    "published_date": mtime.strftime("%Y-%m-%d"), "entries": entries})
    return out

# ---------- YouTube ----------

def yt_search(api_key: str, query: str, published_after: str | None = None,
              order: str = "viewCount", max_results: int = 25) -> list[str]:
    params = {"part": "id", "q": query, "type": "video", "videoCategoryId": "10",
              "regionCode": "JP", "relevanceLanguage": "ja", "order": order,
              "maxResults": max_results, "key": api_key}
    if published_after:
        params["publishedAfter"] = published_after
    data = http_get_json("https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(params))
    return [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]


def yt_videos(api_key: str, ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 50):
        params = urllib.parse.urlencode({"part": "snippet,statistics,contentDetails",
                                         "id": ",".join(ids[i:i + 50]), "key": api_key})
        out.extend(http_get_json(f"https://www.googleapis.com/youtube/v3/videos?{params}").get("items", []))
    return out


MV_HINTS = re.compile(r"(MV|M/V|Music\s*Video|ミュージックビデオ|Lyric|リリック|Official|Visualizer)",
                      re.IGNORECASE)
NG_TITLE = re.compile(r"(歌ってみた|踊ってみた|弾いてみた|叩いてみた|reaction|リアクション|切り抜き|耳コピ|"
                      r"cover\s*by|カバー|カラオケ|メドレー|作業用|まとめ|1時間|耐久)", re.IGNORECASE)


def video_record(v: dict) -> dict:
    sn, st = v.get("snippet", {}), v.get("statistics", {})
    published = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00")).astimezone(JST)
    return {"video_id": v["id"], "video_title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
            "description": sn.get("description", "")[:500],
            "published_at": published.strftime("%Y-%m-%d"), "published_dt": published,
            "duration_sec": parse_iso_duration(v.get("contentDetails", {}).get("duration", "")),
            "views": int(st.get("viewCount", 0) or 0), "likes": int(st.get("likeCount", 0) or 0),
            "comments": int(st.get("commentCount", 0) or 0),
            "url": f"https://www.youtube.com/watch?v={v['id']}"}


YT_EXTRACT_SYSTEM = """YouTube動画のメタデータから、日本を主な活動拠点とするアーティストの
公式音楽動画(MV / リリックビデオ / ビジュアライザー / 公式ライブ映像)かを判定し、
アーティスト名と曲名を抽出してください。歌ってみた・カバー・転載・切り抜き・ティザーのみ・
メイキング・海外アーティストは false。推測で埋めないこと。
出力はJSON配列のみ: [{"video_id": "...", "is_official_jpop": true/false, "artist": "...", "song": "..."}]"""


def fetch_youtube_discovery(src: dict, api_key: str, now: datetime) -> dict:
    after = (now - timedelta(days=src["lookback_days"])).astimezone(timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for q in src["queries"]:
        try:
            ids += yt_search(api_key, q, after)
        except Exception as e:
            warn(f"YouTube検索失敗 '{q}': {e}")
    cands = []
    for v in yt_videos(api_key, list(dict.fromkeys(ids))):
        r = video_record(v)
        if not (src["min_duration_sec"] <= r["duration_sec"] <= src["max_duration_sec"]):
            continue
        if NG_TITLE.search(r["video_title"]) or not MV_HINTS.search(r["video_title"] + r["description"][:200]):
            continue
        days = max((now - r["published_dt"]).total_seconds() / 86400, 0.25)
        eng = (r["likes"] + r["comments"] * 2) / max(r["views"], 1)
        r["momentum"] = math.log10(r["views"] / days + 1) * (1 + min(eng * 20, 1.0))
        cands.append(r)
    cands.sort(key=lambda r: r["momentum"], reverse=True)
    cands = cands[:src["max_candidates"]]
    if not cands:
        return {"period": None, "published_date": now.strftime("%Y-%m-%d"), "entries": []}

    resp = claude_messages({
        "model": CONFIG["claude_model"], "max_tokens": 6000, "system": YT_EXTRACT_SYSTEM,
        "messages": [{"role": "user", "content": json.dumps(
            [{k: c[k] for k in ("video_id", "video_title", "channel", "description")} for c in cands],
            ensure_ascii=False)}]})
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    judged = {a["video_id"]: a for a in parse_json_lenient(text, "[") if isinstance(a, dict)}
    entries = []
    for c in cands:
        a = judged.get(c["video_id"])
        if a and a.get("is_official_jpop") and a.get("song"):
            entries.append({"rank": len(entries) + 1, "title": a["song"], "artist": a.get("artist", "")})
    return {"period": f"直近{src['lookback_days']}日に公開されたMV",
            "published_date": now.strftime("%Y-%m-%d"), "entries": entries[:src["max_rank"]]}


def gather_sources(now: datetime, yt_key: str | None) -> list[dict]:
    results = []

    def run(src: dict, fn):
        try:
            print(f"[info] 取得中: {src['label']}")
            r = {**{k: src[k] for k in ("id", "label", "weight", "max_rank")}, **fn()}
            r["entries"] = clean_entries(r["entries"], src["max_rank"])
            pub = r.get("published_date")
            if pub:
                try:
                    age = (now.date() - datetime.strptime(pub, "%Y-%m-%d").date()).days
                    if age > CONFIG["stale_days"]:
                        warn(f"{src['label']} は {pub} 公開で古いため集計から外しました")
                        return
                except ValueError:
                    pass
            if not r["entries"]:
                warn(f"{src['label']}: 順位を取得できませんでした(集計から除外)")
                return
            print(f"[info]   → {len(r['entries'])}曲 / {r.get('period') or '期間不明'}")
            results.append(r)
        except Exception as e:
            warn(f"{src['label']} の取得に失敗しました(集計から除外): {e}")

    for src in CONFIG["web_sources"]:
        run(src, lambda s=src: fetch_chart_with_claude(s))
    am = CONFIG["apple_music"]
    if am.get("enabled"):
        run(am, lambda: fetch_apple_music(am))
    yd = CONFIG["youtube_discovery"]
    if yd.get("enabled") and yt_key:
        run(yd, lambda: fetch_youtube_discovery(yd, yt_key, now))
    for csv_src in read_manual_csvs(now):
        run(csv_src, lambda s=csv_src: {k: s[k] for k in ("period", "published_date", "entries")})
    return results


def clean_entries(entries: list, max_rank: int) -> list[dict]:
    seen, out = set(), []
    for e in entries or []:
        try:
            rank = int(e.get("rank"))
        except (TypeError, ValueError):
            continue
        title = (e.get("title") or "").strip()
        if not title or rank < 1 or rank > max_rank or rank in seen:
            continue
        seen.add(rank)
        out.append({"rank": rank, "title": title, "artist": (e.get("artist") or "").strip()})
    return sorted(out, key=lambda e: e["rank"])

# ============================================================
# 2. 突き合わせ・総合ポイント
# ============================================================

def merge_sources(sources: list[dict]) -> list[dict]:
    """同じ曲をまとめ、ソースごとに weight × (max_rank+1-rank)/max_rank を加算"""
    songs: list[dict] = []
    for src in sorted(sources, key=lambda s: s["weight"], reverse=True):  # 表記は重いソースを優先
        for e in src["entries"]:
            tk, ak = norm_key(e["title"]), norm_key(e["artist"])
            hit = next((s for s in songs if s["tk"] == tk and artist_match(s["ak"], ak)), None)
            if hit is None:
                hit = {"tk": tk, "ak": ak, "title": e["title"], "artist": e["artist"],
                       "ranks": {}, "points": 0.0}
                songs.append(hit)
            if not hit["ak"] and ak:
                hit["ak"], hit["artist"] = ak, e["artist"]
            if src["id"] in hit["ranks"]:
                continue
            hit["ranks"][src["id"]] = e["rank"]
            hit["points"] += src["weight"] * (src["max_rank"] + 1 - e["rank"]) / src["max_rank"]
    for s in songs:
        s["key"] = f"{s['tk']}|{s['ak']}"
        s["points"] = round(s["points"], 3)
    # 同点はより多くのチャートに入っている曲を上に
    songs.sort(key=lambda s: (s["points"], len(s["ranks"])), reverse=True)
    return songs

# ============================================================
# 3. 邦楽判定(Claude、アーティスト単位でキャッシュ)
# ============================================================

CLASSIFY_SYSTEM = """日本の音楽チャートに入った曲のリストです。各アーティストが「邦楽」かを判定してください。
邦楽 = 日本を主な活動拠点とするアーティスト(J-POP、ロック、ヒップホップ、アニソン、ボカロP、
アイドル、演歌など。日本の事務所所属のグローバルグループも含む)。
韓国を拠点とするK-POPグループ(日本人メンバーがいても)・欧米などの海外アーティストは false。
判断できない場合は null。出力はJSON配列のみ:
[{"artist": "入力と同じ表記", "is_jpop": true/false/null}]"""


def classify_jpop(pool: list[dict], cache: dict) -> None:
    todo = sorted({s["artist"] for s in pool if norm_key(s["artist"]) not in cache})
    if todo:
        print(f"[info] 邦楽判定中({len(todo)}アーティスト)...")
        resp = claude_messages({"model": CONFIG["claude_model"], "max_tokens": 4000,
                                "system": CLASSIFY_SYSTEM,
                                "messages": [{"role": "user", "content": json.dumps(
                                    [{"artist": a, "songs": [s["title"] for s in pool if s["artist"] == a]}
                                     for a in todo], ensure_ascii=False)}]})
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        for a in parse_json_lenient(text, "["):
            if isinstance(a, dict) and a.get("artist") and isinstance(a.get("is_jpop"), bool):
                cache[norm_key(a["artist"])] = a["is_jpop"]  # 判断保留は保存せず次回また判定
    for s in pool:
        v = cache.get(norm_key(s["artist"]))
        # 判断不能(null)は日本のチャートに入っている以上、邦楽として残し digest で「要確認」と表示
        s["is_jpop"] = True if v is None else bool(v)
        s["jpop_unsure"] = v is None

# ============================================================
# 4. YouTube公式MVの特定 & Claude一言分析
# ============================================================

def find_mv(song: dict, api_key: str | None) -> None:
    q = f"{song['artist']} {song['title']}"
    song["url"] = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q)
    song["video_id"] = None
    if not api_key:
        return
    try:
        vids = [video_record(v) for v in yt_videos(api_key, yt_search(api_key, q, order="relevance",
                                                                       max_results=8))]
    except Exception as e:
        warn(f"MV検索失敗 {q}: {e}")
        return
    best, best_score = None, 0
    for v in vids:
        vt, ch = norm_key(v["video_title"]), norm_key(v["channel"])
        if NG_TITLE.search(v["video_title"]) or song["tk"] not in vt:
            continue
        score = 2
        if song["ak"] and (song["ak"] in ch or (ch and ch in song["ak"]) or song["ak"] in vt):
            score += 2
        if MV_HINTS.search(v["video_title"]):
            score += 1
        if v["channel"].endswith("- Topic"):
            score -= 1
        if score > best_score:
            best, best_score = v, score
    if best and best_score >= 3:
        song.update({k: best[k] for k in ("video_id", "url", "video_title", "channel",
                                            "description", "published_at", "views")})


ANALYSIS_SYSTEM = """あなたは邦楽メディア「J-MUSIC TOP10」の編集AIです。
各曲について、どのチャートで何位だったか(=実データ)と、公式MVのメタデータをもとに、
掲載情報を作ってください。

厳守:
- 一言分析は与えられたデータ(チャート順位の組み合わせ、再生数など)に基づいて書く。
  「ストリーミングと有線の両方で上位」「SNS発で浸透」のように数字の裏付けがある切り口にする
- 歌詞は著作物なので一切引用しない
- タイアップはMV説明文に明記がある場合のみ。なければ null
- 確認できないことは推測せず null

出力はJSON配列のみ:
[{"key": "入力と同じ", "genre": "J-POP / ロック / ヒップホップ / アニソン / ボカロ / アイドル など",
  "tieup": "タイアップ or null", "keywords": ["2〜4語"],
  "hitokoto": "15〜40文字の一言分析(データに基づく)"}]"""


def analyze_top(top: list[dict], source_labels: dict) -> None:
    payload = [{"key": s["key"], "artist": s["artist"], "title": s["title"],
                "charts": {source_labels[k]: f"{v}位" for k, v in s["ranks"].items()},
                "mv_title": s.get("video_title"), "mv_description": s.get("description"),
                "mv_views": s.get("views"), "mv_published": s.get("published_at")} for s in top]
    resp = claude_messages({"model": CONFIG["claude_model"], "max_tokens": 6000, "system": ANALYSIS_SYSTEM,
                            "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]})
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    got = {a["key"]: a for a in parse_json_lenient(text, "[") if isinstance(a, dict) and a.get("key")}
    for s in top:
        a = got.get(s["key"], {})
        s.update({"genre": a.get("genre"), "tieup": a.get("tieup"),
                  "keywords": a.get("keywords") or [], "hitokoto": a.get("hitokoto") or ""})

# ============================================================
# 5. 順位変動
# ============================================================

def attach_movement(top: list[dict], rankings: dict, today_s: str):
    """前回実行との比較: NEW / ↑3 / ↓2 / → / 再"""
    prev_dates = sorted(d for d in rankings if d < today_s)
    prev = rankings[prev_dates[-1]] if prev_dates else []
    ever = {k for d in prev_dates for k in rankings[d]}
    for i, c in enumerate(top):
        k = c["key"]
        if k in prev:
            diff = prev.index(k) - i
            c["move"] = f"↑{diff}" if diff > 0 else (f"↓{-diff}" if diff < 0 else "→")
        elif k in ever:
            c["move"] = "再"
        else:
            c["move"] = "NEW"
        c["times_in_chart"] = sum(1 for d in prev_dates if k in rankings[d]) + 1

# ============================================================
# 6. 投稿文生成
# ============================================================

def fmt_views(n) -> str:
    if n is None:
        return "—"
    return f"{n/10000:.1f}万" if n >= 10000 else f"{n:,}"


def playlist_url(top: list[dict]) -> str | None:
    ids = [c["video_id"] for c in top if c.get("video_id")]
    return f"https://www.youtube.com/watch_videos?video_ids={','.join(ids)}" if len(ids) >= 2 else None


def rank_label(i: int) -> str:
    return {0: "🥇第1位", 1: "🥈第2位", 2: "🥉第3位"}.get(i, f"{i + 1}位")


def podium_order(top: list[dict]) -> list[int]:
    return [i for i in (2, 1, 0) if i < len(top)]


def name(c: dict) -> str:
    return f"{c['artist']}「{c['title']}」" if c.get("artist") else f"「{c['title']}」"


def move_tag(c: dict) -> str:
    m = c.get("move")
    return {"NEW": "🆕NEW", "再": "🔁再"}.get(m, f"(前回比{m})") if m else ""


def chart_line(c: dict, ctx: dict, force: bool = False) -> str:
    """どのチャートに入っているか。show_source_ranks=False なら数だけ"""
    if CONFIG["show_source_ranks"] or force:
        return " / ".join(f"{ctx['labels'][k]} {v}位" for k, v in c["ranks"].items())
    return f"{len(c['ranks'])}/{ctx['n_sources']}チャートにランクイン"


def sources_line(ctx: dict) -> str:
    return "、".join(ctx["labels"].values())


def short_credit(ctx: dict) -> str:
    return f"{ctx['n_sources']}つのチャート({sources_line(ctx)})を独自集計"


def render_x(top, ctx) -> str:
    n, d = len(top), ctx["date_s"]
    lines = [f"◤ 最新 邦楽TOP{n} ◢ {d}", "",
             "複数の公式チャートを独自集計！3位からカウントダウン🧵", "",
             CONFIG["hashtags"], "\n--- ↓2ツイート目以降(スレッドに続ける) ---\n"]
    for i in podium_order(top):
        c = top[i]
        lines += [f"{rank_label(i)} {name(c)} {move_tag(c)}", f"📊 {chart_line(c, ctx)}"]
        if c.get("tieup"):
            lines.append(f"🎬 {c['tieup']}")
        lines += [f"💬 {c['hitokoto']}", c["url"], "\n---\n"]
    if top[3:]:
        lines += [f"続いて4位〜{n}位はこちら👇", ""]
        for i, c in enumerate(top[3:], start=4):
            lines += [f"{i}位 {name(c)} {move_tag(c)}", c["url"]]
        lines.append("\n(長い場合は2ツイートに分割してください)")
    return "\n".join(lines)


def render_threads(top, ctx) -> str:
    n = len(top)
    lines = [f"🎧 最新 邦楽TOP{n}({ctx['date_s']})", "3位からカウントダウン!", ""]
    for i in podium_order(top):
        c = top[i]
        lines += [f"{rank_label(i)} {name(c)} {move_tag(c)}", f"　{c['hitokoto']}", f"　{c['url']}", ""]
    if top[3:]:
        lines.append(f"— 4位〜{n}位 —")
        for i, c in enumerate(top[3:], start=4):
            lines.append(f"{i}位 {name(c)} {move_tag(c)} {c['url']}")
        lines.append("")
    pl = playlist_url(top)
    if pl:
        lines += [f"🎧 MVをまとめて見る→ {pl}", ""]
    lines.append(f"※{short_credit(ctx)}")
    return "\n".join(lines)


def render_instagram(top, ctx) -> str:
    n, d = len(top), ctx["date_s"]
    out = ["■ Instagramカルーセル原稿", "", "── スライド1(表紙) ──",
           f"最新 邦楽TOP{n}", d, CONFIG["brand"], ""]
    slide = 2
    for i in podium_order(top):
        c = top[i]
        out += [f"── スライド{slide}({rank_label(i)}) ──", f"{rank_label(i)} {move_tag(c)}",
                c["title"], c["artist"], chart_line(c, ctx)]
        if c.get("genre"):
            out.append(f"ジャンル: {c['genre']}")
        if c.get("tieup"):
            out.append(f"タイアップ: {c['tieup']}")
        if c.get("keywords"):
            out.append(f"キーワード: {' / '.join(c['keywords'])}")
        out += [f"注目ポイント: {c['hitokoto']}", ""]
        slide += 1
    if top[3:]:
        out.append(f"── スライド{slide}(4位〜{n}位一覧) ──")
        out += [f"{i}位 {name(c)} {move_tag(c)}" for i, c in enumerate(top[3:], start=4)]
        out.append("")
        slide += 1
    out += [f"── スライド{slide}(最終) ──", "MVはプロフィールのリンクから", "フォローして最新TOP10をチェック", "",
            "── キャプション ──", f"最新 邦楽TOP{n}({d})", "3位からカウントダウンで発表!", ""]
    out += [f"{i + 1}位 {name(c)}" for i, c in enumerate(top)]
    out += ["", f"※{short_credit(ctx)}", "",
            CONFIG["hashtags"] + " #今週の邦楽 #新曲紹介"]
    return "\n".join(out)


def render_tiktok(top, ctx) -> str:
    """縦型動画の台本(テロップ・尺)とキャプション"""
    n, d = len(top), ctx["date_s"]
    t = 0.0

    def span(sec: float) -> str:
        nonlocal t
        a, t = t, t + sec
        return f"[{int(a)//60}:{int(a)%60:02d}-{int(t)//60}:{int(t)%60:02d}]"

    out = ["■ TikTok投稿用(縦型 9:16 / カウントダウン形式)", "",
           "── 動画台本(テロップ案と尺の目安) ──",
           f"{span(3)} 冒頭フック",
           f"　テロップ: 「最新 邦楽TOP{n}」「1位はあの曲…!」 / {d}"]
    rest = list(enumerate(top[3:], start=4))
    for i, c in reversed(rest):
        out.append(f"{span(1.5)} {i}位 テロップ: {name(c)} {move_tag(c)}")
    for i in podium_order(top):
        c = top[i]
        out += [f"{span(6)} {rank_label(i)}",
                f"　テロップ1: {name(c)} {move_tag(c)}",
                f"　テロップ2: {c['hitokoto']}"]
    out += [f"{span(3)} 締め",
            "　テロップ: 「全曲のMVはプロフィールから」「フォローで最新ランキングをチェック」",
            f"(合計 約{int(t)}秒)", "",
            "── 音源について ──",
            "BGMはTikTokアプリの楽曲ライブラリから公式音源を選んで設定してください(1位の曲がおすすめ)。",
            "MVの映像や音声を切り出して使うのは権利上NGです。ジャケット画像も使わず、文字と背景で構成してください。",
            "※ビジネスアカウントでは商用利用可の音源しか使えない場合があります。", "",
            "── キャプション ──",
            f"最新 邦楽TOP{n}({d})🎧 あなたの推し曲は何位?コメントで教えて!", ""]
    out += [f"{i + 1}位 {name(c)}" for i, c in enumerate(top)]
    out += ["", f"※{short_credit(ctx)}", "", CONFIG["tiktok_hashtags"]]
    return "\n".join(out)


def render_note(top, ctx) -> str:
    n = len(top)
    lines = [f"# 【{ctx['date_s']}】最新 邦楽TOP{n}|{CONFIG['brand']}", "",
             f"{CONFIG['tagline']}です。",
             "複数の公式チャートの順位をポイント化して合算し、海外アーティストを除いた邦楽だけでランキングにしています。"
             "今回は3位からのカウントダウンで発表します。MVは各アーティストの公式チャンネルでお楽しみください。", ""]
    for i in podium_order(top):
        c = top[i]
        lines += [f"## {rank_label(i)} {name(c)} {move_tag(c)}", "",
                  f"- 集計: {chart_line(c, ctx)}(総合 {c['points']}pt)",
                  f"- ランクイン: {c.get('times_in_chart', 1)}回目"]
        if c.get("views") is not None:
            lines.append(f"- MV再生数: {fmt_views(c['views'])}回")
        for label, key in (("ジャンル", "genre"), ("タイアップ", "tieup")):
            if c.get(key):
                lines.append(f"- {label}: {c[key]}")
        if c.get("keywords"):
            lines.append(f"- キーワード: {' / '.join(c['keywords'])}")
        lines += ["", f"**一言分析**: {c['hitokoto']}", "", f"▶️ [公式MVを見る]({c['url']})", ""]
    if top[3:]:
        lines += [f"## 4位〜{n}位", ""]
        for i, c in enumerate(top[3:], start=4):
            lines += [f"**{i}位 {name(c)}** {move_tag(c)} — {c.get('hitokoto', '')}",
                      f"　▶️ [公式MVを見る]({c['url']})", ""]
    lines += ["---", ""]
    pl = playlist_url(top)
    if pl:
        lines += [f"🎧 [今回のTOP{n}のMVを連続再生でまとめて見る]({pl})", ""]
    lines += ["### 集計方法", "",
              "以下のチャートの順位を、チャートごとの重みを付けてポイント化し合算しました。", ""]
    lines += [f"- {s['label']}({s.get('period') or '期間不明'})" for s in ctx["sources"]]
    lines += ["",
              "※各チャートの権利は各運営者に帰属します。本ランキングはそれらを参照した独自集計です。",
              "※本メディアは音源・映像・歌詞を掲載せず、公式チャンネルへのリンクのみ掲載しています。",
              "※一言分析はチャートデータとMVの公開情報に基づくAI分析であり、楽曲の試聴評価ではありません。"]
    return "\n".join(lines)


def render_digest(top, ctx) -> str:
    """人間の確認用(各チャートの順位をすべて表示)"""
    lines = [f"📋 {CONFIG['brand']} {ctx['date_s']} 生成完了(TOP{len(top)})", "",
             "■ 集計に使ったチャート"]
    lines += [f"・{s['label']}: {len(s['entries'])}曲 / {s.get('period') or '期間不明'} (重み{s['weight']})"
              for s in ctx["sources"]]
    if ctx.get("excluded"):
        lines += ["", "■ 海外アーティストとして除外: " + "、".join(ctx["excluded"])]
    lines += ["", "■ TOP" + str(len(top))]
    for i, c in enumerate(top):
        unsure = "  ←邦楽か要確認" if c.get("jpop_unsure") else ""
        lines.append(f"{i + 1}位 {move_tag(c)} {name(c)} {c['points']}pt{unsure}")
        lines.append(f"　{chart_line(c, ctx, force=True)}")
        lines.append(f"　{c['url']}" + ("" if c.get("video_id") else "  ←MV未特定(検索リンク)"))
    pl = playlist_url(top)
    if pl:
        lines += ["", f"▶️ MVを連続再生してチェック: {pl}"]
    lines += ["", "各ファイルを確認してコピペ投稿してください:",
              "x.txt / threads.txt / instagram.txt / tiktok.txt / note.md",
              "動画: reel.mp4(縦型9:16)/ feed.mp4(4:5)※無音。BGMは各アプリの公式音源を付けてください"]
    return "\n".join(lines)

# ============================================================
# 7. Googleドライブ保存
# ============================================================

def copy_to_sync_dir(out_dir: Path, folder: str):
    sync = os.environ.get("GDRIVE_SYNC_DIR")
    if not sync:
        return
    dest = Path(sync) / CONFIG["drive_folder_name"] / folder
    shutil.copytree(out_dir, dest, dirs_exist_ok=True)
    print(f"[info] Googleドライブ同期フォルダに保存: {dest}")


DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]  # このアプリが作ったファイルだけ
TOKEN_FILE = DATA_DIR / "google_token.json"


def drive_login_local() -> None:
    """パソコンで1回だけ実行: ブラウザでGoogleにログインし、トークンを作る。
    表示されたJSONを GitHub の Secrets「GOOGLE_TOKEN_JSON」に登録すると Actions から保存できる"""
    from google_auth_oauthlib.flow import InstalledAppFlow
    cred_file = BASE_DIR / "credentials.json"
    if not cred_file.exists():
        sys.exit(f"{cred_file} がありません。Google Cloud ConsoleでOAuthクライアント(デスクトップアプリ)を"
                 "作成し、JSONを credentials.json という名前でこのスクリプトと同じ場所に置いてください")
    creds = InstalledAppFlow.from_client_secrets_file(str(cred_file), DRIVE_SCOPES).run_local_server(port=0)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"[info] トークンを保存しました: {TOKEN_FILE}")
    print("GitHub Actionsで使う場合は、このファイルの中身をまるごと Secrets の GOOGLE_TOKEN_JSON に登録してください。")
    print("※このファイルはGitHubにコミットしないでください(.gitignore で除外済み)")


def upload_with_drive_api(out_dir: Path, folder: str):
    """Google Drive API で「マイドライブ/J-MUSIC TOP10/日付/」にアップロード(同名は上書き)。
    トークンは 環境変数 GOOGLE_TOKEN_JSON(GitHub Actions)→ data/google_token.json(パソコン)の順に探す"""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        warn("Drive APIのライブラリがありません: pip install -r requirements.txt")
        return
    env_token = os.environ.get("GOOGLE_TOKEN_JSON")
    if env_token:
        creds = Credentials.from_authorized_user_info(json.loads(env_token), DRIVE_SCOPES)
    elif TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), DRIVE_SCOPES)
    else:
        warn("Googleドライブのトークンがありません。パソコンで python music_trend_watch.py --drive-login を"
             "実行し、GitHubの場合は GOOGLE_TOKEN_JSON に登録してください")
        return
    if not creds.valid:
        if not creds.refresh_token:
            warn("トークンが無効です。--drive-login をやり直してください")
            return
        try:
            creds.refresh(Request())
        except Exception as e:
            warn(f"トークンの更新に失敗しました({e})。--drive-login をやり直し、GOOGLE_TOKEN_JSON を更新してください")
            return
    svc = build("drive", "v3", credentials=creds)

    def find(name_: str, parent: str | None, folder_: bool):
        q = [f"name = '{name_}'", "trashed = false",
             f"'{parent}' in parents" if parent else "'root' in parents"]
        if folder_:
            q.append("mimeType = 'application/vnd.google-apps.folder'")
        r = svc.files().list(q=" and ".join(q), fields="files(id)", spaces="drive").execute()
        return (r.get("files") or [{}])[0].get("id")

    def ensure_folder(name_: str, parent: str | None) -> str:
        fid = find(name_, parent, True)
        if fid:
            return fid
        meta = {"name": name_, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            meta["parents"] = [parent]
        return svc.files().create(body=meta, fields="id").execute()["id"]

    mimes = {".txt": "text/plain", ".md": "text/markdown", ".json": "application/json", ".mp4": "video/mp4"}
    root = ensure_folder(CONFIG["drive_folder_name"], None)
    day = ensure_folder(folder, root)
    for f in sorted(out_dir.rglob("*")):
        if not f.is_file():
            continue
        parent = day if f.parent == out_dir else ensure_folder(f.parent.name, day)
        media = MediaFileUpload(str(f), mimetype=mimes.get(f.suffix, "application/octet-stream"),
                                resumable=f.suffix == ".mp4")
        existing = find(f.name, parent, False)
        if existing:
            svc.files().update(fileId=existing, media_body=media).execute()
        else:
            svc.files().create(body={"name": f.name, "parents": [parent]}, media_body=media).execute()
    print(f"[info] Googleドライブにアップロード: マイドライブ/{CONFIG['drive_folder_name']}/{folder}/")

# ============================================================
# 8. デモデータ(--demo用。実在アーティスト・実在チャートの数値ではない架空データ)
# ============================================================

def _e(*rows):
    return [{"rank": r, "title": t, "artist": a} for r, t, a in rows]


DEMO_SOURCES = [
    {"id": "billboard_hot100", "label": "Billboard JAPAN Hot 100", "weight": 3.0, "max_rank": 50,
     "period": "(デモ)2026/09/30 公開", "entries": _e(
        (1, "春雷", "ミナモ"), (2, "ハイウェイ・ブルー", "the KITEs"), (3, "Starlight Code", "POLARIS7"),
        (4, "からっぽの部屋", "ユノ"), (5, "Neon Wave", "LUMINA"), (6, "あかね色の約束", "雨宮ソウタ"),
        (7, "深海メモリー", "kurage"), (8, "祝祭", "サクラバ楽団"), (9, "線路沿い", "東京ノイズ"),
        (10, "まばたき", "ヒナタ"))},
    {"id": "usen_jpop", "label": "週間 USEN HIT J-POPランキング", "weight": 1.5, "max_rank": 25,
     "period": "(デモ)2026年9月18日～9月24日", "entries": _e(
        (1, "からっぽの部屋", "ユノ"), (2, "春雷", "ミナモ"), (3, "祝祭", "サクラバ楽団"),
        (4, "まばたき", "ヒナタ"), (5, "Midnight Convenience", "Lo-Fi Kids"))},
    {"id": "apple_music", "label": "Apple Music 日本 Top Songs", "weight": 1.5, "max_rank": 50,
     "period": "(デモ)2026-10-05 更新", "entries": _e(
        (1, "春雷 (Drama Edit)", "ミナモ"), (2, "からっぽの部屋", "ユノ"),
        (3, "Neon Wave", "LUMINA"), (4, "ハイウェイ・ブルー", "the KITEs"),
        (5, "深海メモリー", "kurage feat. 初音ミク"))},
    {"id": "tiktok", "label": "TikTok 人気楽曲", "weight": 1.5, "max_rank": 20,
     "period": "手動入力(デモ)", "entries": _e(
        (1, "からっぽの部屋", "ユノ"), (2, "Midnight Convenience", "Lo-Fi Kids"),
        (3, "Starlight Code", "POLARIS7"))},
]
DEMO_NOT_JPOP = {"LUMINA"}  # デモ用: 海外グループとして除外される例
DEMO_TEXT = {
    "春雷": ("J-POP", "ドラマ『夜明けの診療所』主題歌(架空)", ["ドラマ主題歌", "疾走感"],
           "合算チャート1位に加えApple Musicでも首位、全方位で強い"),
    "からっぽの部屋": ("シンガーソングライター", None, ["弾き語り", "TikTok"],
                "有線とTikTokで1位、生活の場とSNSの両方に浸透"),
    "ハイウェイ・ブルー": ("ロック", None, ["ギターロック", "復活作"], "合算2位、配信でも上位で復帰作が好発進"),
    "Starlight Code": ("アイドル", None, ["ダンス", "ファンダム"], "合算3位とTikTok3位、ダンス動画が後押し"),
    "祝祭": ("J-POP", "飲料CMソング(架空)", ["CMソング", "ブラス"], "有線3位、店舗BGMでの露出が厚い"),
    "まばたき": ("J-POP", None, ["新人", "透明感"], "有線4位が先行、合算チャートでも初TOP10"),
    "深海メモリー": ("ボカロ", None, ["ボカロ", "歌い手"], "配信と合算の両方で安定して上位"),
    "Midnight Convenience": ("ヒップホップ", None, ["チル", "夜"], "TikTok2位発、有線でもじわじわ浸透"),
    "あかね色の約束": ("アニソン", "TVアニメ『蒼の旅団』OP(架空)", ["アニメOP"], "合算チャート6位、アニメ放送で底堅い"),
    "線路沿い": ("ロック", None, ["青春"], "合算チャート9位、ロングヒットで圏内を維持"),
}

# ============================================================
# メイン
# ============================================================

def notify_discord(text: str):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        http_post_json(url, {"content": text[:1900]}, {})
        print("[info] Discordに通知しました")
    except Exception as e:
        warn(f"Discord通知失敗: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="APIキーなしで架空データを使い出力を確認")
    ap.add_argument("--upload-drive", action="store_true", help="Google Drive APIで直接アップロード")
    ap.add_argument("--video", action="store_true", help="make_video.py で reel.mp4 / feed.mp4 も生成")
    ap.add_argument("--drive-login", action="store_true",
                    help="(パソコンで1回だけ)Googleにログインしてドライブ用トークンを作る")
    args = ap.parse_args()
    if args.drive_login:
        drive_login_local()
        return

    now = jst_now()
    today_s, date_s = now.strftime("%Y-%m-%d"), now.strftime("%Y/%m/%d")
    out_dir = OUT_DIR / today_s
    if out_dir.exists():  # 同じ日に再実行したら上書き
        shutil.rmtree(out_dir)
    (out_dir / "sources").mkdir(parents=True)

    rankings: dict = load_json(RANKINGS_FILE, {})
    artist_cache: dict = load_json(ARTISTS_FILE, {})
    yt_key = os.environ.get("YOUTUBE_API_KEY")

    if args.demo:
        print("[info] デモモード: 架空データで出力を生成します")
        sources = [{**s, "entries": clean_entries(s["entries"], s["max_rank"])} for s in DEMO_SOURCES]
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY が設定されていません(動作確認だけなら --demo を付けてください)")
        if not yt_key:
            warn("YOUTUBE_API_KEY 未設定: MVリンクはYouTube検索リンクで代用し、新着MVの勢いは集計しません")
        sources = gather_sources(now, yt_key)
    if not sources:
        sys.exit("どのチャートも取得できませんでした")
    for s in sources:
        save_json(out_dir / "sources" / f"{s['id']}.json", s)  # 取得した生データ(検証用)

    merged = merge_sources(sources)
    pool = merged[:CONFIG["classify_pool"]]
    if args.demo:
        for s in pool:
            s["is_jpop"] = s["artist"] not in DEMO_NOT_JPOP
    else:
        classify_jpop(pool, artist_cache)
    excluded = [name(s) for s in pool if not s["is_jpop"]]
    top = [s for s in pool if s["is_jpop"]][:CONFIG["top_n"]]
    if not top:
        sys.exit("邦楽の曲が見つかりませんでした")

    labels = {s["id"]: s["label"] for s in sources}
    if args.demo:
        for i, c in enumerate(top):
            g, tie, kw, hito = DEMO_TEXT.get(c["title"], ("J-POP", None, [], "複数チャートで上位"))
            c.update({"genre": g, "tieup": tie, "keywords": kw, "hitokoto": hito,
                      "video_id": f"demo{i + 1}", "url": f"https://www.youtube.com/watch?v=demo{i + 1}",
                      "views": 3_000_000 // (i + 1)})
        rankings = {"2026-09-29": [merged[k]["key"] for k in (1, 0, 3, 2, 6, 5, 8)]}
    else:
        print("[info] 公式MVを検索中...")
        for c in top:
            find_mv(c, yt_key)
        print("[info] 一言分析を作成中...")
        analyze_top(top, labels)
    attach_movement(top, rankings, today_s)

    ctx = {"date_s": date_s, "labels": labels, "n_sources": len(sources),
           "sources": sources, "excluded": excluded}
    files = {
        "x.txt": render_x(top, ctx),
        "threads.txt": render_threads(top, ctx),
        "instagram.txt": render_instagram(top, ctx),
        "tiktok.txt": render_tiktok(top, ctx),
        "note.md": render_note(top, ctx),
        "digest.txt": render_digest(top, ctx),
    }
    for fname, text in files.items():
        (out_dir / fname).write_text(text, encoding="utf-8")
    save_json(out_dir / "ranking.json",
              [{k: v for k, v in c.items() if k not in ("tk", "ak", "description")} for c in top])
    save_json(out_dir / "meta.json", {"date_s": date_s, "brand": CONFIG["brand"],
                                      "n_sources": len(sources), "credit": f"※{short_credit(ctx)}"})
    print(f"[info] 出力: {out_dir}")

    if args.video:
        print("[info] 動画を生成中...")
        from make_video import make_videos  # Pillow と ffmpeg が必要
        make_videos(out_dir)

    if not args.demo:
        rankings[today_s] = [c["key"] for c in top]
        cutoff = (now - timedelta(days=183)).strftime("%Y-%m-%d")  # 半年より古い履歴は削除
        save_json(RANKINGS_FILE, {d: v for d, v in rankings.items() if d >= cutoff})
        save_json(ARTISTS_FILE, artist_cache)

    copy_to_sync_dir(out_dir, today_s)
    if args.upload_drive:
        try:
            upload_with_drive_api(out_dir, today_s)
        except Exception as e:
            warn(f"Googleドライブへのアップロードに失敗: {e}")

    notify_discord(files["digest.txt"])
    print("\n" + files["digest.txt"])


if __name__ == "__main__":
    main()
