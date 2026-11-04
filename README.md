# うぶごえライブ

登録者数が少なく、「ライブ」タブの動画もまだ数本しかない新人VTuberのうち、いま配信中の人を一覧にするWebサービスです。

- `finder.py` … YouTube Data API で検索し、結果を `site/data.json` に書き出す
- `site/index.html` … 一覧ページ（data.json を読んで表示、5分ごとに再読み込み）
- `.github/workflows/update.yml` … 1時間おきに finder.py を実行し、GitHub Pages に公開

## 公開までの手順

1. GitHubで新しいリポジトリを作る（例: `vtuber-live-finder`）。無料プランでPagesを使うには **Public** にします。
2. このフォルダの中身をすべてアップロードする（`.github` フォルダも忘れずに）。
3. リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で、
   名前 `YOUTUBE_API_KEY`、値にAPIキーを登録する。
4. **Settings → Pages** の「Source」を **GitHub Actions** にする。
5. **Actions** タブ →「配信データ更新」→ **Run workflow** で初回を手動実行する。
6. 数分後、`https://<ユーザー名>.github.io/<リポジトリ名>/` で一覧が見られます。以降は1時間おきに自動更新されます。

## 条件を変える

**Settings → Secrets and variables → Actions → Variables** で次を追加すると、コードを触らずに変えられます。

| 名前 | 意味 | 初期値 |
|---|---|---|
| `MAX_SUBSCRIBERS` | 登録者数の上限 | 1000 |
| `MAX_LIVE_VIDEOS` | 「ライブ」タブの本数の上限 | 5 |

## API の使用量

1回の実行で約330ユニット。1時間おき（24回/日）で約8,000ユニットになり、無料枠（1日10,000）に収まります。
検索キーワードを増やすと1語につき100ユニット増えるので、その場合は `update.yml` の cron の間隔を広げてください。
