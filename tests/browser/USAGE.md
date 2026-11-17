# ボードの画面検証

Windows の実行 PC で、リポジトリのルートから次を実行する。Node.js 20 以降と Microsoft Edge が必要。

```powershell
npm.cmd ci --prefix tests/browser
node tests/browser/board.cjs configuration
node tests/browser/board.cjs waiting
node tests/browser/board.cjs retry
```

各コマンドは該当機能を含むブランチで実行する。別のチェックアウトを検証するには、シナリオ名の後にそのチェックアウトの `nextcloud/codex_queue` ディレクトリを渡す。

ワーカーで自動検証する場合は、依存関係をインストールした `board.cjs` の絶対パスを使って対象プロジェクトのテストコマンドに登録する。ワーカーは検証対象のチェックアウトを作業ディレクトリにして、Codex CLI の実行後にこのコマンドを起動する。Codex の Windows サンドボックス内で Edge が起動できない場合は、ホストの PowerShell から実行する。

テストは一時ブラウザープロファイルでアプリのテンプレート・JavaScript・CSS を読み込み、固定の API 応答で画面を操作する。Nextcloud のログインや本番データは使わない。PHP 側と同期処理は別途検証する。
