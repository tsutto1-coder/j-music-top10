# J-MUSIC TOP10

複数の公式チャートを独自に集計して、最新の邦楽TOP10を作るツールです。
GitHub Actions のボタンで実行すると、次の3つを自動で用意します。

- 各SNS用の投稿文
- 縦型動画とフィード用動画
- Googleドライブへの保存

## できること

| 生成物 | 内容 |
|---|---|
| `x.txt` / `threads.txt` / `instagram.txt` / `note.md` | コピペ用の投稿文(3位からカウントダウン) |
| `tiktok.txt` | TikTok用の台本(尺・テロップ)とキャプション |
| `reel.mp4` | 1080×1920 縦型動画(TikTok / リール / ショート) |
| `feed.mp4` | 1080×1350 フィード用動画(Instagram / X) |
| `digest.txt` | 確認用(各チャートの順位・除外した曲など) |
| `sources/*.json` | 各チャートから取得した生データ(検証用) |

動画は無音です。BGMは各アプリの楽曲ライブラリから公式音源を付けてください。
MVの映像・音声・ジャケット画像は権利の都合で使っていません。

## 集計に使うデータ

| データ | 取得方法 |
|---|---|
| Billboard JAPAN Hot 100 | Claude API の web_fetch で公式ページを読み取り |
| Billboard JAPAN Top User Generated Songs | 同上 |
| 週間 USEN HIT J-POPランキング | 同上 |
| Apple Music 日本 Top Songs | Apple公式のJSONフィード |
| YouTube 新着MVの勢い | YouTube Data API |
| オリコン・TikTok・LINE MUSIC・Spotify | `manual_charts/` に置いたCSV(手入力) |

各チャートの重み、使う順位の範囲、投稿文に各チャートの順位を載せるかどうかは、
`music_trend_watch.py` の先頭にある `CONFIG` で変えられます。

## 初回セットアップ

### 1. リポジトリにファイルを置く

このフォルダ一式をGitHubのリポジトリ(非公開推奨)にアップロードします。

### 2. Secrets を登録する

リポジトリの「Settings → Secrets and variables → Actions → New repository secret」で登録します。

| 名前 | 必須 | 内容 |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Claude APIキー |
| `YOUTUBE_API_KEY` | 推奨 | YouTube Data API v3 のキー(MVリンク・再生数用) |
| `GOOGLE_TOKEN_JSON` | ドライブ保存に必要 | 下の手順3で作るトークン |
| `DISCORD_WEBHOOK_URL` | 任意 | 完了通知を送りたい場合 |

### 3. Googleドライブの準備(初回だけ、パソコンで作業)

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作り、「Google Drive API」を有効にします。
2. 「OAuth同意画面」を作成します。
3. 公開ステータスを **「本番環境」** にします。
   - 「テスト」のままだと、7日でトークンが切れて保存できなくなります。
   - 使う権限は「このアプリが作ったファイルのみ」(drive.file)なので、Googleの審査は不要です。
4. 「認証情報 → OAuthクライアントID → デスクトップアプリ」を作成します。
5. JSONをダウンロードし、`credentials.json` という名前でスクリプトと同じフォルダに置きます。
6. パソコンで次を実行し、ブラウザでGoogleにログインします。
   ```
   pip install -r requirements.txt
   python music_trend_watch.py --drive-login
   ```
7. 作成された `data/google_token.json` の中身を丸ごとコピーし、Secrets の `GOOGLE_TOKEN_JSON` に登録します。

> ⚠️ `credentials.json` と `data/google_token.json` は絶対にコミットしないでください(`.gitignore` で除外済み)。

保存先は「マイドライブ / J-MUSIC TOP10 / 日付 /」です。

## 実行のしかた

1. GitHubのリポジトリで「Actions」タブを開きます。
2. 「J-MUSIC TOP10」を選んで「Run workflow」を押します。スマホのブラウザからも操作できます。
3. 次のオプションを選べます。
   - 動画を作るか
   - ドライブに保存するか
   - デモで試すか
4. 数分で完了します。生成物はGoogleドライブのほか、実行結果ページの「Artifacts」からも30日間ダウンロードできます。

最初は「デモで試す」にチェックを入れて実行してください。APIキーなしで動作と動画の見た目を確認できます。

前回の順位(`data/rankings.json`)は、実行のたびにリポジトリへ自動で書き戻されます。
次回の実行では、これをもとに前回比(NEW / ↑ / ↓)を付けます。

## オリコン・TikTokなどを手入力で加える

`manual_charts/` に、次のような名前のCSVを置くと集計に入ります。

- `oricon.csv`
- `tiktok.csv`
- `line_music.csv`
- `spotify.csv`

GitHubの画面上で直接作成・編集できます。

```
rank,title,artist
1,曲名,アーティスト名
2,曲名,アーティスト名
```

- 最後に更新(コミット)してから21日を過ぎたCSVは、自動で集計から外れます。
- 先頭が `_` のファイル(例: `_template.csv`)は無視されます。

## パソコンで動かす場合

```
pip install -r requirements.txt
# 環境変数 ANTHROPIC_API_KEY などを設定してから
python music_trend_watch.py --video                 # 動画も作る
python music_trend_watch.py --video --upload-drive  # Drive APIで保存
python music_trend_watch.py --demo --video          # APIキーなしで試す
```

パソコン版Googleドライブを使っている場合は、`--upload-drive` の代わりに環境変数を設定する方法もあります。
`GDRIVE_SYNC_DIR` に同期フォルダ(例: `G:/マイドライブ`)を設定すると、そこへコピーされます。

動画だけ作り直す場合は、次のように実行します。
`python make_video.py outputs/2026-10-06`

## 注意

各チャートの順位データの権利は、各運営者に帰属します。
このツールは、それらを参照して独自に集計する前提で作っています。
そのため、投稿文には各チャートの順位を載せない設定(`show_source_ranks: False`)を既定にしています。
収益化するメディアとして運営する場合は、各社の利用条件を確認してください。
