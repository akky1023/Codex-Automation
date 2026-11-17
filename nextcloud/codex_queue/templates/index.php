<?php
style('codex_queue', 'workspace');
script('codex_queue', 'board');
?>
<main id="codex-queue-workspace" data-api="<?php p($_['api']); ?>">
    <header class="cq-toolbar">
        <h1>Codex Queue</h1>
        <label>プロジェクト <select id="cq-filter"><option value="">すべて</option></select></label>
        <button id="cq-sync" type="button">今すぐ同期</button>
        <button id="cq-open-config" type="button" disabled>設定</button>
        <span id="cq-synced"></span>
    </header>
    <p id="cq-message" role="status"></p>
    <div class="cq-board">
        <section>
            <div class="cq-column-header">
                <h2>Idea</h2>
                <button id="cq-open-add" class="cq-icon-button" type="button" aria-haspopup="dialog"
                    aria-label="Idea を追加" title="Idea を追加">
                    <span class="icon-add" aria-hidden="true"></span>
                </button>
            </div>
            <div id="cq-ideas"></div>
        </section>
        <section><h2>To Do</h2><div id="cq-todos"></div></section>
        <section><h2>Done</h2><div id="cq-done"></div></section>
    </div>
    <details class="cq-history-panel"><summary>依頼履歴</summary><ul id="cq-history"></ul></details>
    <form id="cq-revision">
        <section id="cq-revision-picker" aria-labelledby="cq-revision-picker-title" hidden>
            <div class="cq-picker-header">
                <strong id="cq-revision-picker-title">修正対象を選択</strong>
                <label class="cq-picker-control" title="並び順">
                    <span aria-hidden="true">≡</span>
                    <select id="cq-revision-sort" aria-label="並び順">
                    <option value="added">追加順</option>
                    <option value="name">名前順</option>
                    <option value="project">プロジェクトごと</option>
                </select></label>
                <button id="cq-revision-direction" class="cq-picker-control" type="button"
                    aria-label="降順にする（現在は昇順）" title="降順にする（現在は昇順）">↑</button>
            </div>
            <div id="cq-revision-options"></div>
        </section>
        <div id="cq-revision-target" hidden>
            <span id="cq-revision-label"></span>
            <button id="cq-clear-revision" type="button" aria-label="修正依頼の対象を外す" title="対象を外す">×</button>
        </div>
        <div class="cq-revision-input">
            <textarea id="cq-revision-body" name="body" aria-label="修正依頼" placeholder="修正してほしい内容を入力" maxlength="12000" rows="2" required></textarea>
            <div class="cq-revision-controls">
                <button id="cq-expand-revision" type="button" aria-controls="cq-revision-body"
                    aria-pressed="false" aria-label="入力欄を広げる" title="入力欄を広げる">
                    <span class="cq-expand-icon" aria-hidden="true"></span>
                </button>
                <div class="cq-revision-actions">
                    <button id="cq-pick-revision" type="button" aria-label="対象を選ぶ" title="対象を選ぶ"
                        aria-controls="cq-revision-picker" aria-expanded="false">
                        <span class="icon-add" aria-hidden="true"></span>
                    </button>
                    <button class="primary" type="submit" disabled>送信</button>
                </div>
            </div>
        </div>
        <p id="cq-revision-message" role="status"></p>
    </form>
    <dialog id="cq-config-dialog" aria-labelledby="cq-config-title">
        <h2 id="cq-config-title">キュー設定</h2>
        <label>対象 <select id="cq-config-scope"></select></label>
        <form id="cq-config-form">
            <div id="cq-config-fields"></div>
            <p id="cq-config-message" role="status"></p>
            <div class="cq-dialog-actions">
                <button id="cq-close-config" type="button">閉じる</button>
                <button id="cq-reload-config" type="button">最新値を読み直す</button>
                <button id="cq-save-config" type="submit" class="primary">保存を依頼</button>
            </div>
        </form>
    </dialog>
    <dialog id="cq-add-dialog" aria-labelledby="cq-add-title">
        <h2 id="cq-add-title">Idea を追加</h2>
        <form id="cq-add">
            <label id="cq-idea-project">プロジェクト <select name="target" required></select></label>
            <label>タイトル <input name="title" maxlength="200" required autofocus></label>
            <label>詳細 <textarea name="body" maxlength="12000" rows="4"></textarea></label>
            <p id="cq-add-error" role="alert"></p>
            <div class="cq-dialog-actions">
                <button id="cq-cancel-add" type="button">キャンセル</button>
                <button type="submit" class="primary">追加</button>
            </div>
        </form>
    </dialog>
    <dialog id="cq-grilling-dialog" aria-labelledby="cq-grilling-title">
        <h2 id="cq-grilling-title">Grilling</h2>
        <div id="cq-grilling-content"></div>
        <form id="cq-grilling-form">
            <label id="cq-grilling-answer-label" hidden>回答 <textarea name="answer" maxlength="12000" rows="5"></textarea></label>
            <p id="cq-grilling-message" role="status"></p>
            <div class="cq-dialog-actions">
                <button id="cq-grilling-cancel" type="button">中止</button>
                <button id="cq-grilling-confirm" type="submit" class="primary" hidden>この仕様で ToDo 化</button>
                <button id="cq-grilling-send" type="submit" class="primary" hidden>回答を送信</button>
            </div>
        </form>
    </dialog>
</main>
