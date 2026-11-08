# うぶごえライブ

日本のVTuber（個人勢・企業勢）を見つける検索サービスです。登録者数・通常動画本数・Shorts本数・ライブ本数・現在の同時接続者数を、それぞれ最小値・最大値で絞り込めます。配信中／配信していない／すべての切り替えも可能です。

公開ページ: https://takuminurufff-collab.github.io/vtuber-live-finder/

## しくみ

1時間おきに GitHub Actions が `finder.py` を実行します。

1. **総ざらい**：「#新人Vtuber」「#Vtuber準備中」「#個人勢Vtuber」「#新人Vtuberを発掘せよ」の付いた動画を、前回確認した時刻から今まで、投稿日時で区切って探索します。API枠不足で途中終了した場合は区間と次のページ位置を保存します。ただしYouTube検索の上限やAPIエラーによる収集漏れの可能性はあります。
2. **配信検索**：配信中・配信予定の動画をキーワード検索し、タグを付けていない人も拾います（毎回1つ）。
3. **蓄積**：見つけたVTuber候補を事務所所属の有無で除外せず、`data/channels.json` に蓄積します。過去の拒否済みチャンネルもAPI枠を見ながら少しずつ再検査します。
4. **芋づる式**：蓄積したチャンネルの概要欄に書かれた「@ハンドル」「チャンネルURL」と、おすすめチャンネル欄から、まだ見つかっていないVTuberをたどります。
5. **更新**：登録者数を毎回、動画・ショート・ライブの本数を3日に1回と配信開始時に数え直します。
6. **確認**：各チャンネルのRSS（API枠を使いません）で新しい動画を見つけ、配信中かを判定します。
7. **出力**：`site/data.json` を書き出し、GitHub Pages に公開します。絞り込みはページ側で行います。

対象になるのは、上のタグを付けて投稿したか、配信検索で見つかったか、すでに見つかったVTuberから芋づる式にたどれたチャンネルです。どれにも当てはまらないチャンネルは拾えません。

登録者数が非公開・未取得のVTuberも候補として蓄積します。その場合は0人ではなく「不明」と表示し、登録者数による絞り込みを指定した場合のみ対象外になります。

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
月額0円運用を優先し、無料のYouTube Data API日次クォータ内で動作するよう制御します。投稿数が多い場合は発見が遅れることがあり、全VTuberの完全網羅は保証しません。

## 品質チェック

開発ブランチへのpush時はGitHub ActionsでPythonの構文・収集ロジックの単体テストと、Node.jsによる検索フィルターのテストを実行します。テストはYouTube APIを呼び出しません。公開サイトのブラウザ動作確認は別途必要です。
