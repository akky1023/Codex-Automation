# Codex Queue を Nextcloud に導入する

対象は Nextcloud 33.x。開発環境では 33.0.11 で動作を確認済み。本番では HTTPS の Nextcloud と、`nextcloud sync` コマンドを含む Windows 版 `cq` を使う。

## Nextcloud 管理者

1. `occ status` で Nextcloud のバージョンを確認し、導入前に既存の手順で Nextcloud のデータベースと設定をバックアップする。
2. `occ config:system:get apps_paths` でカスタムアプリの配置先を確認する。配布元の `nextcloud/codex_queue/` をディレクトリごとコピーし、配置先が `custom_apps/codex_queue/appinfo/info.xml` のようになることを確認する。Docker では、コンテナを作り直しても残るボリュームまたはマウント先に配置する。
3. Nextcloud のルートで、Web サーバーの実行ユーザーとして以下を実行する。`www-data` は環境に合わせて変更する。Docker では同じ `php occ` コマンドを Nextcloud コンテナ内で実行する。

```sh
sudo -u www-data php occ app:enable codex_queue
sudo -u www-data php occ app:list --enabled
```

4. 一覧に `codex_queue` があることを確認する。利用者アカウントでログインし、アプリメニューから **Codex Queue** を開く。公開範囲をグループに限定する場合は、有効化コマンドに `--groups <グループID>` を指定する。

## Windows の実行 PC

対象プロジェクトを `cq` に登録しておく。ブラウザでボードを使う Nextcloud アカウントと同じアカウントで接続する。

```powershell
cq nextcloud connect
cq nextcloud sync --watch
```

登録済みプロジェクトの Idea、To Do、実行結果が接続先アカウントのボードに表示される。プロジェクト単位で同期を止める場合は `cq project edit 1 --nextcloud-sync off` を使い、再開する場合は `on` に戻す。`1` は `cq project ls` に表示されるプロジェクト ID に置き換える。変更は次回同期時に反映される。1アカウントにつき実行 PC は1台とし、常時同期には `--watch` のプロセスを起動しておく。一度だけ同期する場合は `--watch` を省く。

ボードの「今すぐ同期」を押し、依頼履歴に結果が表示されることと、指定したプロジェクトのカードが見えることを確認する。

## 更新・停止

更新時はデータベースをバックアップし、配布元と同じ版のアプリソースを配置してから `php occ upgrade` を実行する。停止するときは Windows の同期コマンドを終了し、Nextcloud で `php occ app:disable codex_queue` を実行する。

[Nextcloud のアプリ管理](https://docs.nextcloud.com/server/stable/admin_manual/occ_apps.html)
