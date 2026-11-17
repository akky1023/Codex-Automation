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

未計画 Idea のケバブメニューから **Grilling** を選ぶと、質問への回答、合意仕様の確認、中止をボード上で行える。モデルの応答中も `--watch` の同期を続け、画面は次の状態を定期取得する。仕様確認後は既存の ToDo 計画と、`auto` ブランチ方式の承認へ進む。既存の同期ポーリングを使うため、リアルタイム接続の追加設定は不要。

ボードの「今すぐ同期」を押し、依頼履歴に結果が表示されることと、指定したプロジェクトのカードが見えることを確認する。

更新時は `php occ upgrade` が `cq_requests` の Grilling セッション列を追加する。更新後、未計画 Idea のケバブメニューに Grilling が表示され、質問・回答・仕様確認の各段階が同期後に反映されることを確認する。

## 更新・停止

ボード上部の「設定」から全体設定と同期対象 Project の個別設定を編集できる。個別設定では「全体設定を継承」と「個別指定」を選べる。個別指定の空欄は明示的な未設定として保存し、モデル候補は入力した順序を維持する。リポジトリのパスは実行 PC の `cq project edit ID --path PATH` で変更する。

「保存を依頼」は Nextcloud に依頼を予約する。実行 PC の次回同期で検証・保存した結果を画面と依頼履歴に表示する。CLI や別タブが変更した項目と競合した場合は依頼全体を適用せず、「最新値を読み直す」から編集をやり直す。定期取得中も入力内容は保持する。

Project のボード同期を `off` にすると、その保存結果と同時に Project がボードから消える。再開は実行 PC で `cq project edit ID --nextcloud-sync on` を実行して同期する。

更新時は実行 PC の `cq` と Nextcloud の `codex_queue` アプリを両方更新し、単発の `cq nextcloud sync` または起動し直した `cq nextcloud sync --watch` で同期する。設定情報を送らない旧版の実行 PC ではボードの設定ボタンが無効になる。CLI の設定変更がボードの最新値に届くこと、ボードでの保存結果が CLI の `cq config` / `cq project edit ID` のプロジェクト表示で確認できることを確認する。

更新時はデータベースをバックアップし、配布元と同じ版のアプリソースを配置してから `php occ upgrade` を実行する。停止するときは Windows の同期コマンドを終了し、Nextcloud で `php occ app:disable codex_queue` を実行する。

[Nextcloud のアプリ管理](https://docs.nextcloud.com/server/stable/admin_manual/occ_apps.html)
