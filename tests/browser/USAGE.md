# ボードの画面検証

Windows の実行 PC で、リポジトリのルートから次を実行する。Node.js 20 以降と Microsoft Edge が必要。

```powershell
npm.cmd ci --prefix tests/browser
node tests/browser/board.cjs configuration
node tests/browser/board.cjs waiting
node tests/browser/board.cjs retry
```

モーダルの検証では、稼働中の Nextcloud 33 の実 CSS を取得してから実行する（コンテナ名は環境に合わせる）。CSS とスクリーンショットは Git の対象外。

```powershell
New-Item -ItemType Directory -Force tests/browser/artifacts | Out-Null
docker cp codex-queue-nextcloud-dev-nextcloud-1:/var/www/html/core/css tests/browser/artifacts/nextcloud-css
node tests/browser/board.cjs modals
```

`modals` は Nextcloud の `server.css` と `variables.css`、アプリの実ファイル、固定 API 応答を使う。初期・閉じた後の非表示、中央配置、背景のフォーカス抑止、Escape、再表示、下書き保持、長文、320/390/1280px 幅、明暗配色、追加画面、Grilling の生成・回答・仕様確認・完了・中止・ブランチ承認を検証する。スクリーンショットは `artifacts/` に保存する。明暗配色は CSS 変数を切り替えるため、Nextcloud のテーマ設定自体の検証ではない。

2026-11-17 の検証結果: 修正前は実 CSS 下で `config must be hidden initially` に失敗し、修正後は4シナリオが成功。デスクトップ・モバイルの明暗スクリーンショットを目視確認した。`node tests/test_board_config.cjs` の DOM 代替テスト（下書き・競合・継承など）と Python 全106テストも成功した。

本番には対象のテンプレート・CSS・JS のみを反映し、既存の通信中表示を保持した。本番マウントのファイル更新と、本番 Nextcloud へのログイン後の初期非表示・設定/追加の開閉を確認した。Grilling の本番開閉はセッションを開始しない DOM 操作で確認し、回答・仕様確認・中止・ブランチ承認は固定 API のブラウザ検証で確認した。実行 PC との本番セッション同期や公開 HTTPS 経由の検証は行っていない。

各コマンドは該当機能を含むブランチで実行する。別のチェックアウトを検証するには、シナリオ名の後にそのチェックアウトの `nextcloud/codex_queue` ディレクトリを渡す。

ワーカーで自動検証する場合は、依存関係をインストールした `board.cjs` の絶対パスを使って対象プロジェクトのテストコマンドに登録する。ワーカーは検証対象のチェックアウトを作業ディレクトリにして、Codex CLI の実行後にこのコマンドを起動する。Codex の Windows サンドボックス内で Edge が起動できない場合は、ホストの PowerShell から実行する。

テストは一時ブラウザープロファイルでアプリのテンプレート・JavaScript・CSS を読み込み、固定の API 応答で画面を操作する。Nextcloud のログインや本番データは使わない。PHP 側と同期処理は別途検証する。
