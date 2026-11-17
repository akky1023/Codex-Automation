document.addEventListener('DOMContentLoaded', () => {
    const board = document.querySelector('#codex-queue-workspace');
    const find = selector => board.querySelector(selector);
    const filter = find('#cq-filter');
    const add = find('#cq-add');
    const addDialog = find('#cq-add-dialog');
    const addError = find('#cq-add-error');
    const addSubmit = add.querySelector('[type="submit"]');
    const addCancel = find('#cq-cancel-add');
    const grillingDialog = find('#cq-grilling-dialog');
    const grillingContent = find('#cq-grilling-content');
    const grillingForm = find('#cq-grilling-form');
    const grillingAnswer = grillingForm.elements.answer;
    const grillingMessage = find('#cq-grilling-message');
    const grillingSend = find('#cq-grilling-send');
    const grillingConfirm = find('#cq-grilling-confirm');
    const grillingCancel = find('#cq-grilling-cancel');
    const revision = find('#cq-revision');
    const revisionSize = new ResizeObserver(() => {
        board.style.setProperty('--cq-revision-height', revision.offsetHeight + 'px');
    });
    revisionSize.observe(revision);
    const revisionBody = revision.elements.body;
    const revisionSubmit = revision.querySelector('[type="submit"]');
    const revisionClear = find('#cq-clear-revision');
    const revisionMessage = find('#cq-revision-message');
    const revisionPicker = find('#cq-revision-picker');
    const revisionToggle = find('#cq-pick-revision');
    const revisionSort = find('#cq-revision-sort');
    const revisionDirection = find('#cq-revision-direction');
    let revisionAscending = true;
    const collator = new Intl.Collator('ja', {numeric: true, sensitivity: 'base'});
    const labels = {
        config: '設定変更', add: 'Idea 追加', edit: 'Idea 編集', remove: 'Idea 削除', plan: '計画',
        revise: '修正依頼', reply: '回答', retry: '再試行', done: '完了', abort: '中止', sync: '同期',
        grilling_start: 'Grilling', grilling_answer: 'Grilling 回答', grilling_confirm: 'Grilling 仕様確認',
        grilling_cancel: 'Grilling 中止', approve_branch: 'ブランチ方式承認',
    };
    const states = {
        QUEUED: '待機中', RUNNING: '実行中', DONE: '完了', ABORTED: '中止',
        WAITING_FOR_INPUT: '回答待ち', NEEDS_REVIEW: '確認待ち', INTERRUPTED: '中断',
        AUTH_REQUIRED: '認証待ち', RETRY_WAIT: '再試行待ち', RATE_LIMITED: '利用上限待ち',
        RESUME_QUEUED: '再開待ち',
    };
    let snapshot = {};
    let fingerprint;
    let loading = false;
    let boardVersion = 0;
    let pending = [];
    const scheduled = '次回同期時に実行';
    let editingIdea = null;
    let revisionTask = null;
    const recentDoneLimit = 15;

    const configDialog = find('#cq-config-dialog');
    const configScope = find('#cq-config-scope');
    const configFields = find('#cq-config-fields');
    const configMessage = find('#cq-config-message');
    const configSave = find('#cq-save-config');
    let configuration;
    let configPending = [];
    let configBase = {};
    let configRequest = null;
    let configSending = false;
    const configLabels = {
        parallel: '同時実行数', priority: '既定の優先度', branch: 'ブランチ運用', base: '開始元ブランチ',
        on_abort: '中止後の動作', completion_approval: '完了承認', merge_mode: 'マージ方式',
        on_conflict: 'マージ競合時', test_retries: 'テスト修正回数', test_commands: 'テストコマンド（JSON 引数配列）',
        sync: 'Nextcloud 同期方式', language: '表示言語', planner_model: '計画モデル',
        implementation_model: '実装モデル', allowed_models: 'モデル候補（性能の低い順、カンマ区切り）',
        idea_planning: 'Idea の計画', name: '名前', prompt: '共通指示', nextcloud_sync: 'ボード同期',
    };
    const configChoices = {
        branch: ['per-idea', 'per-task', 'auto', 'current'], on_abort: ['pause', 'continue', 'next-idea'],
        completion_approval: ['planner', 'tests', 'manual', 'report'], merge_mode: ['auto', 'clean-only', 'approval', 'off'],
        on_conflict: ['resolve', 'hold'], sync: ['auto', 'manual'], language: ['en', 'ja'],
        idea_planning: ['auto', 'manual'], nextcloud_sync: ['on', 'off'],
    };

    function updateConfiguration(payload) {
        configuration = payload.snapshot?.configuration;
        configPending = (payload.pending || []).filter(request => request.kind === 'config');
        find('#cq-open-config').disabled = !configuration;
        find('#cq-open-config').title = configuration ? '' : "実行 PC を更新して同期してください。";
        if (configDialog.open) {
            const finished = (payload.requests || []).find(request => Number(request.id) === configRequest);
            if (finished) {
                configRequest = null;
                configMessage.textContent = finished.outcome;
            }
            const waiting = configSending || configRequest !== null || configPending.some(request => Number(request.target_id) === Number(configScope.value));
            const inScope = configScope.value === '0' || Boolean(configuration?.projects?.[configScope.value]);
            configSave.disabled = !configuration || !inScope || waiting || !Object.keys(configBase).length;
            if (waiting) configMessage.textContent = "実行 PC の次回同期による応答待ちです。";
        }
    }

    function loadConfiguration() {
        const target = configScope.value;
        configBase = {...(target === '0' ? configuration?.global : configuration?.projects?.[target])};
        configFields.replaceChildren();
        for (const [key, value] of Object.entries(configBase)) {
            const row = node('div');
            row.className = 'cq-config-field';
            row.dataset.field = key;
            const label = node('label', configLabels[key] || key);
            const input = node(configChoices[key] ? 'select' : ['prompt', 'test_commands'].includes(key) ? 'textarea' : 'input');
            input.dataset.value = key;
            input.id = 'cq-config-' + key;
            label.htmlFor = input.id;
            if (configChoices[key]) for (const choice of configChoices[key]) {
                const option = node('option', choice); option.value = choice; input.append(option);
            }
            input.value = value ?? configuration.global[key] ?? '';
            if (input.tagName !== 'SELECT') input.maxLength = 12000;
            if (['parallel', 'priority', 'test_retries'].includes(key)) {
                input.type = 'number'; input.min = key === 'test_retries' ? '0' : '1'; input.step = '1';
                if (key === 'priority') input.max = '9';
                input.required = true;
            }
            row.append(label);
            if (target !== '0' && !['name', 'prompt', 'nextcloud_sync'].includes(key)) {
                const mode = node('select'); mode.dataset.mode = key; mode.setAttribute('aria-label', configLabels[key] + "の継承");
                for (const [id, text] of [['inherit', "全体設定を継承"], ['value', "個別指定（空欄は未設定）"]]) {
                    const option = node('option', text); option.value = id; mode.append(option);
                }
                mode.value = value === null ? 'inherit' : 'value';
                input.disabled = value === null;
                mode.addEventListener('change', () => { input.disabled = mode.value === 'inherit'; });
                row.append(mode);
            }
            row.append(input, node('small', "編集開始時: " + (value === null
                ? "継承（" + (configuration.global[key] || "未設定") + "）" : value || "未設定")));
            configFields.append(row);
        }
        configSave.disabled = configSending || configRequest !== null || !Object.keys(configBase).length || configPending.some(request => Number(request.target_id) === Number(target));
        configMessage.textContent = configSave.disabled ? "対象が同期範囲外か、実行 PC の応答待ちです。" : '';
    }

    find('#cq-open-config').addEventListener('click', () => {
        configScope.replaceChildren();
        for (const [id, name] of [['0', "全体設定"], ...Object.entries(snapshot.projects || {})]) {
            const option = node('option', name); option.value = id; configScope.append(option);
        }
        loadConfiguration(); configDialog.showModal();
    });
    configScope.addEventListener('change', loadConfiguration);
    find('#cq-reload-config').addEventListener('click', loadConfiguration);
    find('#cq-close-config').addEventListener('click', () => configDialog.close());
    find('#cq-config-form').addEventListener('submit', async event => {
        event.preventDefault();
        if (configSave.disabled) return;
        const changes = {}, expected = {};
        for (const row of configFields.children) {
            const key = row.dataset.field;
            const value = row.querySelector('[data-mode]')?.value === 'inherit' ? null : row.querySelector('[data-value]').value;
            if (value !== configBase[key]) { changes[key] = value; expected[key] = configBase[key]; }
        }
        if (!Object.keys(changes).length) { configMessage.textContent = "変更はありません。"; return; }
        configSave.disabled = true;
        configSending = true;
        try {
            const result = await api('/requests', {kind: 'config', target: Number(configScope.value), body: JSON.stringify({changes, expected})});
            configRequest = Number(result.request_id);
            configSending = false;
            boardVersion++; updateBoard(result, true);
        } catch (error) { configSending = false; configMessage.textContent = error.message; configSave.disabled = false; }
    });

    function taskColumn(task) {
        return ['DONE', 'ABORTED'].includes(task.status) ? 'Done' : 'To Do';
    }

    function canRevise(task) {
        return !['RUNNING', 'RESUME_QUEUED'].includes(task.status);
    }

    function node(tag, label = '') {
        const item = document.createElement(tag);
        item.textContent = label;
        return item;
    }

    async function api(path, data) {
        const response = await fetch(board.dataset.api + path + '?format=json', {
            method: data ? 'POST' : 'GET',
            headers: {'OCS-APIRequest': 'true', 'Content-Type': 'application/json', requesttoken: OC.requestToken},
            body: data ? JSON.stringify(data) : undefined,
        });
        const result = (await response.json()).ocs;
        if (!response.ok) throw new Error(result?.data?.message || 'Nextcloud との通信に失敗しました。');
        return result.data;
    }

    function reservation(kind, target) {
        return pending.find(request => request.kind === kind && Number(request.target_id) === Number(target));
    }

    function showReservation(button, request, label) {
        button.textContent = request ? scheduled : label;
        button.disabled = Boolean(request?.claimed);
        button.title = request ? label + (request.claimed ? '（同期処理中）' : '（押すと予約を取り消す）') : '';
    }

    async function submit(button, kind, target, title = '', body = '', errorTarget = find('#cq-message'), extra = {}) {
        button.disabled = true;
        try {
            const result = await api('/requests', {kind, target: Number(target), title, body, ...extra});
            boardVersion++;
            updateBoard(result, true);
            find('#cq-message').textContent = result.message;
            return true;
        } catch (error) {
            errorTarget.textContent = error.message;
            return false;
        } finally {
            if (button !== find('#cq-sync') || !reservation('sync', 0)?.claimed) button.disabled = false;
        }
    }

    function iconButton(label, iconName) {
        const button = node('button');
        button.type = 'button';
        button.className = 'cq-icon-button';
        button.setAttribute('aria-label', label);
        button.title = label;
        const icon = node('span');
        icon.className = iconName;
        icon.setAttribute('aria-hidden', 'true');
        button.append(icon);
        return button;
    }

    function action(card, kind, id, label = labels[kind]) {
        const button = node('button', label);
        button.type = 'button';
        const request = reservation(kind, id);
        showReservation(button, request, label);
        button.addEventListener('click', () => {
            if (request || kind !== 'remove' || window.confirm('この Idea を削除しますか？')) {
                submit(button, kind, id, '', '', find('#cq-message'), {cancelId: Number(request?.id || 0)});
            }
        });
        card.append(button);
        return button;
    }

    function ideaActions(idea) {
        const actions = node('div');
        actions.className = 'cq-card-actions';
        const plan = action(actions, 'plan', idea.id, 'To Do 化');
        if (idea.planned_at) {
            plan.disabled = true;
            return actions;
        }

        const menu = node('details');
        menu.className = 'cq-idea-menu';
        const toggle = node('summary');
        toggle.setAttribute('aria-label', 'Idea の操作');
        toggle.title = 'Idea の操作';
        const dots = node('span', '⋮');
        dots.setAttribute('aria-hidden', 'true');
        toggle.append(dots);

        const panel = node('div');
        panel.className = 'cq-idea-menu-panel';
        const edit = iconButton('Idea を編集', 'icon-rename');
        edit.append(node('span', '編集'));
        edit.setAttribute('aria-haspopup', 'dialog');
        edit.addEventListener('click', () => {
            menu.open = false;
            toggle.focus();
            openIdeaDialog(idea);
        });
        const remove = action(panel, 'remove', idea.id, '削除');
        const trash = node('span');
        trash.className = 'icon-delete';
        trash.setAttribute('aria-hidden', 'true');
        remove.prepend(trash);
        panel.prepend(edit);
        const grill = node('button', 'Grilling');
        grill.type = 'button';
        grill.addEventListener('click', () => {
            menu.open = false;
            openGrillingDialog(idea);
        });
        panel.append(grill);
        menu.append(toggle, panel);
        actions.append(menu);
        return actions;
    }

    function requestForm(card, kind, id) {
        if (reservation(kind, id)) {
            action(card, kind, id);
            return;
        }
        const details = node('details');
        details.className = 'cq-card-request';
        const form = node('form');
        const label = node('label', labels[kind]);
        const body = node('textarea');
        body.maxLength = 12000;
        body.required = true;
        body.rows = 3;
        label.append(body);
        const button = node('button', '送信');
        form.append(label, button);
        form.addEventListener('submit', async event => {
            event.preventDefault();
            if (await submit(button, kind, id, '', body.value)) {
                details.open = false;
                body.value = '';
            }
        });
        details.append(node('summary', labels[kind]), form);
        card.append(details);
    }

    function card(row, title, status, key) {
        const item = node('details');
        item.className = 'cq-card';
        item.dataset.key = key + row.id;
        const summary = node('summary');
        const heading = node('span', title);
        heading.className = 'cq-card-title';
        const project = snapshot.projects[row.project_id];
        const meta = node('span', row.provisional ? project : '#' + row.id + ' · ' + project);
        meta.className = 'cq-meta';
        const badge = node('span', status);
        badge.className = 'cq-card-status';
        const metadata = node('span');
        metadata.className = 'cq-card-metadata';
        metadata.append(meta, badge);
        summary.append(heading, metadata);
        const content = node('div');
        content.className = 'cq-card-content';
        item.append(summary, content);
        return {item, content};
    }

    function cardWithActions(item, actions) {
        const wrapper = node('div');
        wrapper.className = 'cq-card-with-actions';
        wrapper.append(item, actions);
        return wrapper;
    }

    function cardSection(card, label, text, key) {
        if (!text) return;
        const section = node('details');
        section.className = 'cq-card-section';
        section.dataset.key = key;
        section.append(node('summary', label), node('p', text));
        card.append(section);
    }

    function appendArchive(column, label, key, cards) {
        if (!cards.length) return;
        const archive = node('details');
        archive.className = 'cq-column-archive';
        archive.dataset.key = key;
        const content = node('div');
        content.append(...cards);
        archive.append(node('summary', label + '（' + cards.length + '件）'), content);
        column.append(archive);
    }

    function render() {
        const ideas = find('#cq-ideas');
        const todos = find('#cq-todos');
        const done = find('#cq-done');
        const expanded = new Set([...board.querySelectorAll('details[data-key][open]')].map(item => item.dataset.key));
        for (const column of [ideas, todos, done]) column.replaceChildren();
        const visible = row => !filter.value || String(row.project_id) === filter.value;
        const tasks = (snapshot.tasks || []).filter(visible);
        const completedIdeas = new Map();
        for (const task of tasks) {
            if (task.idea_id == null) continue;
            completedIdeas.set(task.idea_id, completedIdeas.get(task.idea_id) !== false && task.status === 'DONE');
        }
        const implementedIdeas = [];
        const editedIdeas = new Map();
        const addedIdeas = [];
        for (const request of pending) {
            const target = Number(request.target_id);
            if (request.kind === 'edit') editedIdeas.set(target, request);
            if (request.kind === 'add') addedIdeas.unshift({
                id: 'pending-' + request.id, project_id: target, title: request.title, detail: request.body,
                provisional: true,
            });
        }
        const currentIdeas = (snapshot.ideas || []).map(idea => {
            const edit = editedIdeas.get(idea.id);
            return edit ? {...idea, title: edit.title, detail: edit.body, edited: true} : idea;
        });
        for (const idea of [...addedIdeas, ...currentIdeas].filter(visible)) {
            const implemented = Boolean(idea.planned_at && completedIdeas.get(idea.id));
            const status = idea.provisional || idea.edited ? '未同期'
                : implemented ? '実装済み' : (idea.planned_at ? '計画済み' : '未計画');
            const {item, content} = card(idea, idea.title, status, 'idea-');
            if (idea.detail) content.append(node('p', idea.detail));
            if (idea.planning_error) content.append(node('p', idea.planning_error));
            if (idea.provisional) ideas.append(item);
            else if (implemented) implementedIdeas.push(item);
            else ideas.append(cardWithActions(item, ideaActions(idea)));
        }
        appendArchive(ideas, '実装済みのIdea', 'implemented-ideas', implementedIdeas);
        const doneCards = [];
        for (const task of tasks) {
            const {item, content} = card(task, task.name, states[task.status] || task.status, 'task-');
            if (task.abstract) content.append(node('p', task.abstract));
            cardSection(content, '詳細', task.prompt, item.dataset.key + '-detail');
            cardSection(content, '停止理由', task.reason, item.dataset.key + '-reason');
            if (task.completed_at) content.append(node('p', task.completed_at));
            if (task.report) {
                try {
                    const report = JSON.parse(task.report);
                    if (report.verification) content.append(node('p', report.verification));
                } catch {
                    content.append(node('p', task.report));
                }
            }
            const actions = node('div');
            actions.className = 'cq-card-actions cq-task-actions';
            if (reservation('revise', task.id)) {
                action(actions, 'revise', task.id);
            } else if (canRevise(task)) {
                const revise = node('button', labels.revise);
                revise.type = 'button';
                revise.addEventListener('click', () => {
                    if (revisionClear.disabled) return;
                    selectRevisionTask(task);
                    revisionMessage.textContent = '';
                    revisionBody.focus({preventScroll: true});
                    showRevisionPicker(false);
                });
                actions.append(revise);
            }
            if (task.status === 'WAITING_FOR_INPUT') requestForm(content, 'reply', task.id);
            if (['NEEDS_REVIEW', 'INTERRUPTED', 'AUTH_REQUIRED', 'RETRY_WAIT', 'ABORTED'].includes(task.status)) {
                action(actions, 'retry', task.id);
            }
            if (task.status === 'NEEDS_REVIEW') {
                action(actions, 'done', task.id);
                action(actions, 'abort', task.id);
            }
            const taskCard = actions.childElementCount ? cardWithActions(item, actions) : item;
            if (taskColumn(task) === 'Done') doneCards.push({task, taskCard});
            else todos.append(taskCard);
        }
        doneCards.sort((a, b) => (b.task.completed_at || '').localeCompare(a.task.completed_at || '') || b.task.id - a.task.id);
        done.append(...doneCards.slice(0, recentDoneLimit).map(({taskCard}) => taskCard));
        appendArchive(done, '以前の Done', 'older-done', doneCards.slice(recentDoneLimit).map(({taskCard}) => taskCard));
        for (const item of board.querySelectorAll('details[data-key]')) item.open = expanded.has(item.dataset.key);
        for (const column of [ideas, todos, done]) {
            if (!column.childElementCount) column.append(node('p', 'カードはありません。'));
        }
    }

    function projectOptions(select, projects, emptyLabel) {
        const selected = select.value;
        select.replaceChildren(new Option(emptyLabel, ''));
        for (const [id, name] of Object.entries(projects)) select.add(new Option(name, id));
        if ([...select.options].some(option => option.value === selected)) select.value = selected;
    }

    function updateBoard(payload, force = false) {
        updateConfiguration(payload);
        const {synced_at: time, ...cards} = payload.snapshot || {};
        find('#cq-synced').textContent = time
            ? '最終同期 ' + new Date(time).toLocaleString()
            : '実行 PC との同期を待っています';
        const nextPending = payload.pending || [];
        const next = JSON.stringify([cards, nextPending]);
        const editing = addDialog.open || find('.cq-card-request[open], .cq-idea-menu[open]');
        if (next !== fingerprint && (force || !editing)) {
            const projects = cards.projects || {};
            cards.projects = projects;
            projectOptions(filter, projects, 'すべて');
            projectOptions(add.elements.target, projects, '選択してください');
            snapshot = cards;
            pending = nextPending;
            fingerprint = next;
            render();
            showReservation(find('#cq-sync'), reservation('sync', 0), '今すぐ同期');
            if (!revisionPicker.hidden) renderRevisionOptions();
        }
        if (grillingDialog.open) renderGrillingDialog();
        const history = [...nextPending.map(request => ({...request, outcome: request.claimed
            ? '同期処理中' : ['add', 'edit'].includes(request.kind) ? '未同期' : scheduled})),
        ...(payload.requests || [])];
        history.sort((left, right) => Number(right.id) - Number(left.id));
        find('#cq-history').replaceChildren(...history.map(request => node(
            'li', (labels[request.kind] || request.kind) + ' — ' + request.outcome,
        )));
    }

    async function refresh() {
        if (loading) return;
        loading = true;
        const version = boardVersion;
        try {
            const payload = await api('/board');
            if (version === boardVersion) updateBoard(payload);
        } catch (error) {
            find('#cq-message').textContent = error.message;
        } finally {
            loading = false;
        }
    }

    filter.addEventListener('change', render);
    find('.cq-board').addEventListener('click', event => {
        if (event.defaultPrevented || event.target.closest('.cq-card-section, summary, button, a, form, input, select, textarea, label')) return;
        if (window.getSelection()?.type === 'Range') return;
        const surface = event.target.closest('.cq-card-with-actions, .cq-card');
        if (!surface) return;
        const card = surface.matches('.cq-card') ? surface : surface.querySelector('.cq-card');
        card.open = !card.open;
    });
    function selectRevisionTask(task) {
        revisionTask = task;
        const chip = find('#cq-revision-target');
        chip.hidden = !task;
        chip.dataset.column = task && taskColumn(task) === 'Done' ? 'done' : 'todo';
        find('#cq-revision-label').textContent = task ? `${taskColumn(task)} #${task.id} · ${task.name}` : '';
        revisionSubmit.disabled = !task;
    }

    function renderRevisionOptions() {
        const options = find('#cq-revision-options');
        const projects = snapshot.projects || {};
        const tasks = (snapshot.tasks || []).filter(task => canRevise(task) && !reservation('revise', task.id));
        const direction = revisionAscending ? 1 : -1;
        const order = revisionSort.value;
        tasks.sort((left, right) => {
            if (order === 'added') return direction * (left.id - right.id);
            if (order === 'project') {
                const projectOrder = collator.compare(projects[left.project_id], projects[right.project_id])
                    || left.project_id - right.project_id;
                if (projectOrder) return direction * projectOrder;
            }
            return direction * (collator.compare(left.name, right.name) || left.id - right.id);
        });
        options.replaceChildren();
        let lastProject;
        for (const task of tasks) {
            const project = projects[task.project_id];
            if (order === 'project' && lastProject !== task.project_id) {
                options.append(node('h3', project));
                lastProject = task.project_id;
            }
            const button = node('button');
            button.type = 'button';
            button.className = 'cq-revision-option';
            button.append(node('span', task.name), node('small', `${taskColumn(task)} #${task.id} · ${project}`));
            button.addEventListener('click', () => {
                selectRevisionTask(task);
                revisionMessage.textContent = '';
                revisionBody.focus({preventScroll: true});
                showRevisionPicker(false);
            });
            options.append(button);
        }
        if (!tasks.length) options.append(node('p', '修正できるカードがありません。'));
    }

    function showRevisionPicker(open) {
        revisionPicker.hidden = !open;
        revisionToggle.setAttribute('aria-expanded', String(open));
        if (open) renderRevisionOptions();
    }

    revisionBody.addEventListener('focus', () => {
        if (!revisionTask && !revisionBody.readOnly) showRevisionPicker(true);
    });
    revisionBody.addEventListener('input', () => {
        if (!revisionTask && revisionPicker.hidden) showRevisionPicker(true);
    });
    revisionToggle.addEventListener('click', () => showRevisionPicker(revisionPicker.hidden));
    find('#cq-expand-revision').addEventListener('click', event => {
        const button = event.currentTarget;
        const expanded = revision.classList.toggle('cq-revision-expanded');
        const label = expanded ? '入力欄を縮める' : '入力欄を広げる';
        button.setAttribute('aria-pressed', String(expanded));
        button.setAttribute('aria-label', label);
        button.title = label;
    });
    revisionSort.addEventListener('change', renderRevisionOptions);
    revisionDirection.addEventListener('click', () => {
        revisionAscending = !revisionAscending;
        revisionDirection.textContent = revisionAscending ? '↑' : '↓';
        const label = revisionAscending ? '降順にする（現在は昇順）' : '昇順にする（現在は降順）';
        revisionDirection.setAttribute('aria-label', label);
        revisionDirection.title = label;
        renderRevisionOptions();
    });
    document.addEventListener('focusin', event => {
        if (!revision.contains(event.target)) showRevisionPicker(false);
    });
    document.addEventListener('click', event => {
        if (!revision.contains(event.target)) showRevisionPicker(false);
    });
    revision.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !revisionPicker.hidden) {
            event.preventDefault();
            revisionToggle.focus();
            showRevisionPicker(false);
        }
    });

    revisionClear.addEventListener('click', () => {
        selectRevisionTask(null);
        revisionMessage.textContent = '';
        revisionBody.focus({preventScroll: true});
    });
    revision.addEventListener('submit', async event => {
        event.preventDefault();
        if (!revisionTask || revisionSubmit.disabled) return;
        revisionMessage.textContent = '';
        revisionClear.disabled = true;
        revisionToggle.disabled = true;
        showRevisionPicker(false);
        revisionBody.readOnly = true;
        try {
            if (await submit(revisionSubmit, 'revise', revisionTask.id, '', revisionBody.value, revisionMessage)) {
                revisionBody.value = '';
                selectRevisionTask(null);
                revisionMessage.textContent = '修正依頼を受け付けました。';
            }
        } finally {
            revisionClear.disabled = false;
            revisionToggle.disabled = false;
            revisionBody.readOnly = false;
        }
    });
    document.addEventListener('click', event => {
        for (const menu of board.querySelectorAll('.cq-idea-menu[open]')) {
            if (!menu.contains(event.target)) menu.open = false;
        }
    });
    board.addEventListener('keydown', event => {
        const menu = event.target.closest('.cq-idea-menu[open]');
        if (event.key === 'Escape' && menu) {
            event.preventDefault();
            menu.open = false;
            menu.querySelector('summary').focus();
        }
    });
    find('#cq-sync').addEventListener('click', event => {
        submit(event.currentTarget, 'sync', 0, '', '', find('#cq-message'), {cancelId: Number(reservation('sync', 0)?.id || 0)});
    });
    function openIdeaDialog(idea = null) {
        editingIdea = idea;
        add.reset();
        addError.textContent = '';
        find('#cq-add-title').textContent = idea ? 'Idea を編集' : 'Idea を追加';
        addSubmit.textContent = idea ? '保存' : '追加';
        find('#cq-idea-project').hidden = Boolean(idea);
        add.elements.target.disabled = Boolean(idea);
        add.elements.target.value = filter.value;
        add.elements.title.value = idea?.title || '';
        add.elements.body.value = idea?.detail || '';
        addDialog.showModal();
    }

    let grillingIdea = null;
    function openGrillingDialog(idea) {
        grillingIdea = idea;
        grillingMessage.textContent = '';
        grillingAnswer.value = '';
        grillingDialog.showModal();
        renderGrillingDialog();
        if (!idea.grilling) submit(grillingSend, 'grilling_start', idea.id, '', '', grillingMessage);
    }

    function renderGrillingDialog() {
        if (!grillingIdea || !grillingDialog.open) return;
        const idea = (snapshot.ideas || []).find(item => item.id === grillingIdea.id) || grillingIdea;
        grillingIdea = idea;
        const session = idea.grilling;
        grillingContent.replaceChildren();
        const answerLabel = find('#cq-grilling-answer-label');
        answerLabel.hidden = true;
        grillingSend.hidden = true;
        grillingConfirm.hidden = true;
        grillingForm.querySelector('.cq-grilling-branch')?.remove();
        if (!session) {
            grillingContent.append(node('p', 'Grilling セッションを開始しています。同期を待っています。'));
            grillingCancel.hidden = true;
            return;
        }
        grillingCancel.hidden = ['CONFIRMED', 'CANCELLED'].includes(session.status);
        if (session.status === 'ACTIVE') {
            session.questions.forEach((question, index) => {
                const section = node('section');
                section.append(node('h3', `❓ Q${index + 1} - ${question.title}`), node('p', question.body),
                    node('p', `➡️ ${question.recommendation}`));
                grillingContent.append(section);
            });
            answerLabel.hidden = false;
            grillingSend.hidden = false;
        } else if (session.status === 'CONFIRM') {
            grillingContent.append(node('h3', '合意した仕様'), node('p', session.summary));
            grillingConfirm.hidden = false;
        } else if (session.status === 'CONFIRMED' && idea.draft_plan) {
            const draft = JSON.parse(idea.draft_plan);
            grillingContent.append(node('p', `ブランチ方式の承認待ちです。推奨: ${draft.branch_mode} — ${draft.reason}`));
            const choices = node('div');
            choices.className = 'cq-grilling-branch';
            for (const [value, label] of [['recommended', '推奨を承認'], ['per-idea', 'Idea ごと'], ['per-task', 'ToDo ごと']]) {
                const button = node('button', label);
                button.type = 'button';
                button.addEventListener('click', () => submit(button, 'approve_branch', idea.id, '', '', grillingMessage,
                    {sessionId: session.session_id, branchMode: value}));
                choices.append(button);
            }
            grillingForm.append(choices);
        } else if (session.status === 'CONFIRMED') {
            grillingContent.append(node('p', 'ToDo を作成しました。'));
        } else if (session.status === 'CANCELLED') {
            grillingContent.append(node('p', 'Grilling は中止されました。'));
        } else {
            grillingContent.append(node('p', 'Codex が処理中です。通常のボード同期は継続します。'));
        }
    }

    grillingForm.addEventListener('submit', async event => {
        event.preventDefault();
        const idea = grillingIdea && (snapshot.ideas || []).find(item => item.id === grillingIdea.id);
        const session = idea?.grilling;
        if (!idea || !session) return;
        const kind = session.status === 'CONFIRM' ? 'grilling_confirm' : 'grilling_answer';
        const extra = {sessionId: session.session_id, round: session.round};
        const sent = await submit(kind === 'grilling_confirm' ? grillingConfirm : grillingSend,
            kind, idea.id, '', kind === 'grilling_confirm' ? '' : grillingAnswer.value, grillingMessage, extra);
        if (sent && kind === 'grilling_answer') {
            grillingAnswer.value = '';
            grillingMessage.textContent = '回答を受け付けました。次の質問を待っています。';
        }
    });
    grillingCancel.addEventListener('click', async () => {
        const idea = grillingIdea && (snapshot.ideas || []).find(item => item.id === grillingIdea.id);
        if (idea?.grilling) await submit(grillingCancel, 'grilling_cancel', idea.id, '', '', grillingMessage,
            {sessionId: idea.grilling.session_id});
        grillingDialog.close();
    });
    grillingDialog.addEventListener('close', () => { grillingIdea = null; });

    find('#cq-open-add').addEventListener('click', () => openIdeaDialog());
    addCancel.addEventListener('click', () => addDialog.close());
    addDialog.addEventListener('cancel', event => {
        if (addSubmit.disabled) event.preventDefault();
    });
    add.addEventListener('submit', async event => {
        event.preventDefault();
        const {target, title, body} = add.elements;
        addError.textContent = '';
        addCancel.disabled = true;
        try {
            const kind = editingIdea ? 'edit' : 'add';
            const id = editingIdea?.id ?? target.value;
            if (await submit(addSubmit, kind, id, title.value, body.value, addError)) {
                add.reset();
                addDialog.close();
            }
        } finally {
            addCancel.disabled = false;
        }
    });
    const scheduleRefresh = async () => {
        await refresh();
        const grillingActive = grillingDialog.open || (snapshot.ideas || []).some(idea =>
            idea.grilling && ['ACTIVE', 'GENERATING', 'CONFIRM', 'PLAN_GENERATING'].includes(idea.grilling.status));
        window.setTimeout(scheduleRefresh, grillingActive ? 1000 : 5000);
    };
    scheduleRefresh();
});
