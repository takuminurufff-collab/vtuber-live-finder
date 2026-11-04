# うぶごえライブ

小規模な新人VTuberを、登録者数・ライブ本数・動画本数・ショート本数と、いま配信中かどうかで絞り込める一覧です。

公開ページ: https://takuminurufff-collab.github.io/vtuber-live-finder/

## しくみ

1時間おきに GitHub Actions が `finder.py` を実行します。

1. **発見**：配信中の動画・配信予定（予告枠）・直近24時間の新着動画をキーワード検索し、登録者 `POOL_MAX_SUBSCRIBERS`（初期値1万）人以下のVTuberチャンネルを `data/channels.json` に蓄積します。キーワードは毎時入れ替わります。
2. **更新**：蓄積したチャンネルの登録者数と、動画・ショート・ライブの本数を更新します。本数は1日1回、配信が始まったときにも数え直します。
3. **確認**：各チャンネルのRSS（API枠を使いません）から最新動画を取り、いま配信中かを判定します。
4. **出力**：`site/data.json` を書き出し、GitHub Pages に公開します。絞り込みはページ側で行います。

YouTubeには「配信中のすべてのチャンネル」を返すAPIがないため、検索で一度見つかったチャンネルが対象です。運用期間が長いほど蓄積が増えて、網羅率が上がります。

## 今すぐ更新（管理者だけ）

1. GitHubの右上アイコン → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token
2. Repository access で **Only select repositories** → `vtuber-live-finder`
3. Permissions → Repository permissions → **Actions** を **Read and write**
4. 作ったトークンを、公開ページ下部の「管理者メニュー」に貼って保存

保存したブラウザにだけ「今すぐ更新」ボタンが表示されます。

## 設定（Settings → Secrets and variables → Actions → Variables）

| 名前 | 意味 | 初期値 |
|---|---|---|
| `POOL_MAX_SUBSCRIBERS` | 蓄積するチャンネルの登録者数上限 | 10000 |
| `QUERIES_PER_RUN` | 1回の実行で使う検索キーワード数（1語100ユニット） | 1 |

## API の使用量

1回 約300〜400ユニット × 24回で、1日 約8,000〜9,500ユニット（上限 10,000）。
蓄積が数千チャンネルを超えたら、Google Cloud で YouTube Data API の上限引き上げを申請するか、実行間隔を広げてください。
