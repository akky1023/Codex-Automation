# Codex Queue

[English](README.md)

Codex Queue は、ローカルの Git リポジトリで Codex CLI にタスクを実行させるツールです。Windows では、既定でタスク スケジューラが10分ごととサインイン時にキューを確認します。

## Windows でのインストール

Python 3.10 以降、pipx、Git、Codex CLI をインストールしてください。Git と Codex CLI は PATH から実行できるようにします。GitHub Releases から wheel をダウンロードし、そのファイルがあるフォルダーで次を実行します。

```powershell
pipx install .\codex_queue-0.1.0-py3-none-any.whl
cq setup
```

セットアップでは、Codex CLI のログインを再利用し、現在の Windows アカウントで動くワーカーを登録します。初回セットアップ直後は自動実行が一時停止しています。Git リポジトリ内でタスクを追加し、キューを開始します。

```powershell
cq add "検索結果の表示を更新する"
cq start
cq
```

`cq config --language ja` で表示を日本語に切り替えられます。コマンド一覧は `cq help`、各コマンドの使い方は `cq help add` のように確認できます。対話画面では `/help` も使えます。

`cq ls` でキューと完了済みタスクを確認できます。`cq logs ID` では、実行履歴とログの保存先が表示されます。`cq pause` は実行中のタスクが終わるまで待ち、`cq stop` は作業中の変更とセッションを残して実行を中断します。再開には `cq resume` を使います。タスクが回答を待っているときは `cq reply ID "回答"` を実行します。

既定ではタスクごとにブランチを作成し、実行が正常に完了すると元のブランチに戻ります。変更は Git で確認して取り込んでください。同時実行数と開始元ブランチの既定値は、`cq config --parallel 3 --base develop` のように設定できます。

## Nextcloud Tasks との連携（任意）

`cq nextcloud connect` を実行し、ブラウザーで接続を許可します。`cq nextcloud lists` でリスト ID、`cq project ls` でプロジェクト ID を確認します。`cq nextcloud link --list LIST_ID --project-id PROJECT_ID` でリストをプロジェクトに連携します。Codex への指示を任せられる人に編集権限を付与し、リストごとに実行を担当する PC を1台指定してください。

`cq sync` で今すぐ同期できます。`cq config --sync manual` にすると、要求したときだけ同期します。Nextcloud の資格情報は Windows 資格情報マネージャーに保存されます。保存済みの資格情報を削除するには、`cq nextcloud disconnect` を実行します。
