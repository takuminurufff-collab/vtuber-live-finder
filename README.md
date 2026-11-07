# うぶごえライブ

日本の個人勢VTuberを、登録者数・ライブ本数・動画本数・ショート本数と、いま配信中かどうかで絞り込める一覧です。

公開ページ: https://takuminurufff-collab.github.io/vtuber-live-finder/

## しくみ

1時間おきに GitHub Actions が `finder.py` を実行します。

1. **総ざらい**：「#新人Vtuber」「#Vtuber準備中」「#個人勢Vtuber」「#新人Vtuberを発掘せよ」の付いた動画を、前回確認した時刻から今まで、投稿日時で区切ってすべてのページをたどります。実行が遅れても、すき間なく確認します。
2. **配信検索**：配信中・配信予定の動画をキーワード検索し、タグを付けていない人も拾います（毎回1つ）。
3. **蓄積**：見つけたチャンネルのうち、VTuberで事務所所属ではない人を `data/channels.json` に追加します。概要欄に「所属」などがあれば企業勢として外し、「無所属」「個人勢」とあれば個人勢として残します。
4. **芋づる式**：蓄積したチャンネルの概要欄に書かれた「@ハンドル」「チャンネルURL」と、おすすめチャンネル欄から、まだ見つかっていないVTuberをたどります。
5. **更新**：登録者数を毎回、動画・ショート・ライブの本数を3日に1回と配信開始時に数え直します。
6. **確認**：各チャンネルのRSS（API枠を使いません）で新しい動画を見つけ、配信中かを判定します。
7. **出力**：`site/data.json` を書き出し、GitHub Pages に公開します。絞り込みはページ側で行います。

対象になるのは、上のタグを付けて投稿したか、配信検索で見つかったか、すでに見つかったVTuberから芋づる式にたどれたチャンネルです。どれにも当てはまらないチャンネルは拾えません。

## 今すぐ更新（管理者だけ）

1. GitHubの右上アイコン → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token
2. Repository access で **Only select repositories** → `vtuber-live-finder`
3. Permissions → Repository permissions → **Actions** を **Read and write**
4. 作ったトークンを、公開ページ下部の「管理者メニュー」に貼って保存

保存したブラウザにだけ「今すぐ更新」ボタンが表示されます。

## 設定（Settings → Secrets and variables → Actions → Variables）

| 名前 | 意味 | 初期値 |
|---|---|---|
| `POOL_MAX_SUBSCRIBERS` | 蓄積するチャンネルの登録者数上限 | 実質なし |
| `DAILY_BUDGET` | 1日に使うAPIユニット | 9300 |

## API の使用量

`data/state.json` に今日（太平洋時間）の使用量を記録し、次のリセット（日本時間17時）まで均等に使い切るよう、1回あたりの量を自動で決めます。
タグの投稿が多くて追いつかなくなったら、Google Cloud で YouTube Data API の上限引き上げを申請してください。
