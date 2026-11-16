# Nextcloud アプリの開発環境

Docker Desktop を Linux コンテナモードで起動し、リポジトリのルートで実行する。

```powershell
docker compose -f nextcloud/compose.yaml up -d --wait --wait-timeout 180
docker compose -f nextcloud/compose.yaml exec -T -u www-data nextcloud php occ app:enable codex_queue
```

[開発用 Nextcloud](http://127.0.0.1:8087/) にユーザー名 `cqadmin`、開発専用の初期パスワード `codex-queue-local-dev` でログインし、上部のメニューから **Codex Queue** を開く。ポートを変える場合は起動前に `CQ_NEXTCLOUD_PORT` を設定する。初期パスワードを変える場合は、初回起動前に `CQ_NEXTCLOUD_ADMIN_PASSWORD` を設定する。

`nextcloud/codex_queue` のアプリソースをコンテナにマウントしているため、PHP や CSS の変更は画面を再読み込みして確認できる。Nextcloud 33 と SQLite を使い、データは開発環境専用の Docker ボリュームに保存する。

```powershell
docker compose -f nextcloud/compose.yaml stop
```

## カンバンと実行 PC の同期

開発用 Nextcloud の個人設定 → セキュリティでアプリパスワードを発行する。開発中の Python コードを使うには、リポジトリのルートで実行する。

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m codex_queue.cli project ls
python -m codex_queue.cli nextcloud-board --dev-port 8087 --project-id <プロジェクトID> --watch
```

プロジェクト ID は一覧に出た番号を指定する。複数のプロジェクトを出すときは `--project-id` を繰り返す。入力する Nextcloud アカウントはブラウザ側と同じものを使う。指定したプロジェクトの Idea・To Do・実行結果がそのアカウントのボードに表示される。

`--watch` を付けると実行 PC が約5秒間隔で依頼を取りに行く。「今すぐ同期」は同期依頼を積み、実行 PC が取得すると完了を表示する。CalDAV の資格情報を設定済みの場合は、同じボタンで既存の Tasks 同期も実行する。`--watch` を省くと一回だけ依頼を取得してボードを更新する。

HTTPS の Nextcloud では、事前に `cq nextcloud connect` を実行してから `--dev-port` を省く。同じ Nextcloud アカウントのボードには一台の実行 PC を接続する。既存の Tasks 連携は専用アプリを追加せずに使用できる。
# Grilling synchronization

While a Grilling session is active, the browser refresh and `cq nextcloud-board --watch` request check use a 1-second interval. They return to 5 seconds after the session ends. This uses the existing authenticated OCS read/write endpoints; the initial start request is still received by the normal 5-second bridge sync. SSE and long polling were not adopted because buffering and worker capacity have not been measured on the production HTTPS/proxy path. The 1-second interval is a configured polling delay, not a measured delivery latency.
