const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join, resolve} = require('node:path');
const {chromium} = require('playwright');

const origin = 'http://codex-queue.test';
const snapshot = {
    projects: {1: 'Project A', 2: 'Project B'},
    configuration: {global: {parallel: '1', base: 'main'}, projects: {1: {base: null}}},
    tasks: [
        {id: 1, project_id: 1, name: 'Question', status: 'WAITING_FOR_INPUT', reason: 'Which option?'},
        {id: 2, project_id: 1, name: 'Review', status: 'NEEDS_REVIEW', reason: 'Check output'},
        {id: 3, project_id: 2, name: 'Stopped', status: 'ABORTED', reason: 'Stopped by user'},
        {id: 4, project_id: 1, name: 'Queued', status: 'QUEUED'},
    ],
};

async function configuration(page, fixture) {
    await page.locator('#cq-open-config').click();
    const parallel = page.locator('#cq-config-parallel');
    assert.equal(await parallel.inputValue(), '1');
    await parallel.fill('3');
    await page.locator('#cq-save-config').click();
    await page.locator('#cq-save-config:disabled').waitFor();
    await page.getByText('実行 PC の次回同期による応答待ちです。').waitFor();
    assert.equal(fixture.submissions.length, 1);
    const [request] = fixture.submissions;
    assert.equal(request.kind, 'config');
    assert.equal(request.target, 0);
    assert.deepEqual(JSON.parse(request.body), {changes: {parallel: '3'}, expected: {parallel: '1'}});
    fixture.pending = [];
    fixture.history = [{id: 1, kind: 'config', outcome: 'Saved'}];
    await page.locator('#cq-config-message').getByText('Saved').waitFor();
    await page.locator('#cq-config-scope').selectOption('1');
    await page.locator('#cq-config-base:disabled').waitFor();
    await page.getByLabel('開始元ブランチの継承').selectOption('value');
    await page.locator('#cq-config-base').fill('topic');
    fixture.error = 'Configuration rejected';
    await page.locator('#cq-save-config').click();
    await page.locator('#cq-config-message').getByText('Configuration rejected').waitFor();
    assert.equal(await page.locator('#cq-config-base').inputValue(), 'topic');
    assert.equal(await page.locator('#cq-save-config').isEnabled(), true);
}

async function waiting(page) {
    await page.setViewportSize({width: 390, height: 844});
    const section = page.getByRole('region', {name: 'Waiting Response'});
    await section.getByText('Which option?').waitFor();
    assert.deepEqual(await section.getByRole('heading', {level: 3}).allTextContents(), [
        'Question', 'Review', 'Stopped',
    ]);
    await page.locator('#cq-filter').selectOption('2');
    assert.deepEqual(await section.getByRole('heading', {level: 3}).allTextContents(), ['Stopped']);
    await page.locator('#cq-filter').selectOption('1');
    assert.deepEqual(await section.getByRole('heading', {level: 3}).allTextContents(), ['Question', 'Review']);
    assert.equal(await section.evaluate(element => element.scrollWidth <= element.clientWidth), true);
}

async function retry(page, fixture) {
    const buttons = page.getByRole('button', {name: '新しいセッションで再試行', exact: true});
    assert.equal(await buttons.count(), 2);
    const review = page.locator('.cq-card-with-actions').filter({hasText: 'Check output'});
    const button = review.getByRole('button', {name: '新しいセッションで再試行', exact: true});
    page.once('dialog', dialog => dialog.dismiss());
    await button.click();
    assert.deepEqual(fixture.submissions, []);
    let warning;
    page.once('dialog', dialog => {
        warning = dialog.message();
        return dialog.accept();
    });
    await button.click();
    await review.getByRole('button', {name: '次回同期時に実行', exact: true}).waitFor();
    assert.equal(warning, 'なにか変なエラーが出てる場合のみ実行してください');
    assert.equal(fixture.submissions.length, 1);
    assert.equal(fixture.submissions[0].kind, 'retry_fresh');
    assert.equal(fixture.submissions[0].target, 2);
}

async function modals(page, fixture) {
    const hidden = async () => {
        for (const id of ['config', 'grilling', 'add']) {
            assert.equal(await page.locator('#cq-' + id + '-dialog').isVisible(), false, id + ' must be hidden');
        }
    };
    const geometry = async dialog => {
        const box = await dialog.boundingBox(), size = page.viewportSize();
        assert.ok(Math.abs(box.x + box.width / 2 - size.width / 2) < 2, 'horizontal center');
        assert.ok(Math.abs(box.y + box.height / 2 - size.height / 2) < 2, 'vertical center');
        assert.ok(box.x >= 15 && box.y >= 15 && box.y + box.height <= size.height - 15, 'viewport margins');
        assert.equal(await dialog.evaluate(e => e.scrollWidth <= e.clientWidth), true, 'no horizontal overflow');
        assert.equal(await dialog.evaluate(e => e.matches(':modal')), true);
        await page.locator('#cq-sync').evaluate(e => e.focus());
        assert.equal(await dialog.evaluate(e => e.contains(document.activeElement)), true, 'background cannot take focus');
    };
    await hidden();
    snapshot.configuration.global.prompt = 'Long prompt '.repeat(250);
    snapshot.ideas = [{id: 10, project_id: 1, title: 'Grilling example', grilling: {
        session_id: 'fixture', round: 1, status: 'ACTIVE', questions: [{title: 'Question', body: 'Long question '.repeat(160), recommendation: 'Recommended answer '.repeat(40)}],
    }}];
    await page.reload();
    await page.locator('[data-key="idea-10"]').waitFor();
    const openGrilling = async () => {
        await page.locator('.cq-idea-menu > summary').click();
        await page.getByRole('button', {name: 'Grilling', exact: true}).click();
    };
    for (const size of [{width: 1280, height: 900}, {width: 390, height: 640}, {width: 320, height: 480}]) {
        await page.setViewportSize(size);
        for (const theme of ['light', 'dark']) {
            await page.emulateMedia({colorScheme: theme});
            await page.evaluate(dark => {
                document.documentElement.style.setProperty('--color-main-background', dark ? '#181818' : '#fff');
                document.documentElement.style.setProperty('--color-main-text', dark ? '#eee' : '#222');
                document.documentElement.style.setProperty('--color-background-dark', dark ? '#303030' : '#f3f3f3');
            }, theme === 'dark');
            await page.locator('#cq-open-config').click();
            const config = page.locator('#cq-config-dialog');
            await geometry(config);
            await page.locator('#cq-config-parallel').fill('7');
            await page.waitForTimeout(1150);
            assert.equal(await page.locator('#cq-config-parallel').inputValue(), '7');
            await page.screenshot({path: join(__dirname, 'artifacts', 'config-' + size.width + '-' + theme + '.png')});
            await page.locator('#cq-close-config').click();
            assert.equal(await page.locator('#cq-open-config').evaluate(e => e === document.activeElement), true);
            await hidden();
            await openGrilling();
            const grilling = page.locator('#cq-grilling-dialog');
            await geometry(grilling);
            const answer = grilling.locator('textarea');
            await answer.fill('Draft answer');
            await page.waitForTimeout(1150);
            assert.equal(await answer.inputValue(), 'Draft answer');
            await page.screenshot({path: join(__dirname, 'artifacts', 'grilling-' + size.width + '-' + theme + '.png')});
            await page.keyboard.press('Escape');
            await hidden();
            assert.equal(await page.locator('.cq-idea-menu > summary').evaluate(e => e === document.activeElement), true);
            await openGrilling();
            assert.equal(await answer.inputValue(), 'Draft answer');
            await page.locator('#cq-grilling-close').click();
            assert.equal(fixture.submissions.length, 0, 'closing never cancels');
            await page.locator('#cq-open-add').click();
            await geometry(page.locator('#cq-add-dialog'));
            assert.equal(await page.locator('#cq-add input[name="title"]').evaluate(e => e === document.activeElement), true);
            await page.locator('#cq-cancel-add').click();
            await hidden();
        }
    }
    for (const status of ['GENERATING', 'CONFIRM', 'CONFIRMED', 'CANCELLED', 'PLAN_GENERATING']) {
        snapshot.ideas[0].grilling.status = status;
        snapshot.ideas[0].grilling.summary = 'Specification\n'.repeat(90);
        await page.reload();
        await page.locator('[data-key="idea-10"]').waitFor();
        await openGrilling();
        assert.equal(await page.locator('#cq-grilling-send').isVisible(), false);
        assert.equal(await page.locator('#cq-grilling-confirm').isVisible(), status === 'CONFIRM');
        await geometry(page.locator('#cq-grilling-dialog'));
        if (status === 'CONFIRM') {
            await page.locator('#cq-grilling-confirm').click();
            assert.equal(fixture.submissions.at(-1).kind, 'grilling_confirm');
        }
        await page.locator('#cq-grilling-close').click();
    }
    snapshot.ideas[0].grilling.status = 'CONFIRMED';
    snapshot.ideas[0].draft_plan = JSON.stringify({branch_mode: 'per-idea', reason: 'Recommended'});
    fixture.pending = [];
    await page.reload();
    await page.locator('[data-key="idea-10"]').waitFor();
    await openGrilling();
    const approval = page.getByRole('button', {name: '推奨を承認'});
    await approval.focus();
    await page.waitForTimeout(1150);
    assert.equal(await approval.evaluate(e => e === document.activeElement), true, 'polling preserves branch focus');
    await approval.click();
    assert.equal(fixture.submissions.at(-1).kind, 'approve_branch');
    delete snapshot.ideas[0].draft_plan;
    fixture.pending = [];
    snapshot.ideas[0].grilling.status = 'ACTIVE';
    await page.reload();
    await page.locator('[data-key="idea-10"]').waitFor();
    await openGrilling();
    await page.locator('#cq-grilling-dialog textarea').fill('Answer');
    await page.locator('#cq-grilling-send').click();
    assert.equal(fixture.submissions.at(-1).kind, 'grilling_answer');
    await page.locator('#cq-grilling-cancel').click();
    assert.equal(fixture.submissions.at(-1).kind, 'grilling_cancel');
    await hidden();
}

const scenarios = {configuration, waiting, retry, modals};

async function main() {
    const [scenario, app = 'nextcloud/codex_queue'] = process.argv.slice(2);
    assert.ok(Object.hasOwn(scenarios, scenario), 'Usage: node tests/browser/board.cjs configuration|waiting|retry|modals [app-directory]');
    const source = resolve(app);
    const read = file => readFileSync(join(source, file), 'utf8');
    const template = read('templates/index.php')
        .replace(/data-api="<\?php p\(\$_\['api'\]\); \?>"/, 'data-api="/api"')
        .replace(/^\s*<\?php[\s\S]*?\?>/, '');
    const fixture = {pending: [], history: [], submissions: []};
    const browser = await chromium.launch({channel: process.env.CQ_BROWSER_CHANNEL || 'msedge'});
    try {
        const page = await browser.newPage();
        page.setDefaultTimeout(12000);
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.addInitScript(() => { window.OC = {requestToken: 'fixture'}; });
        await page.route('**/*', async route => {
            const url = new URL(route.request().url());
            if (url.origin !== origin) return route.abort();
            const files = {'/board.js': 'js/board.js', '/workspace.css': 'css/workspace.css'};
            if (url.pathname.startsWith('/core/css/')) {
                return route.fulfill({contentType: 'text/css', body: readFileSync(join(__dirname, 'artifacts/nextcloud-css', url.pathname.split('/').pop()))});
            }
            if (Object.hasOwn(files, url.pathname)) {
                const contentType = url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css';
                return route.fulfill({contentType, body: read(files[url.pathname])});
            }
            if (url.pathname === '/') return route.fulfill({contentType: 'text/html; charset=utf-8', body:
                '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">' +
                (scenario === 'modals' ? '<link rel="stylesheet" href="/core/css/server.css"><link rel="stylesheet" href="/core/css/variables.css"><style>#codex-queue-workspace {height:100dvh;box-sizing:border-box}</style>' : '') +
                '<link rel="stylesheet" href="/workspace.css"><script defer src="/board.js"></script>' + template});
            if (url.pathname === '/api/requests') {
                if (fixture.error) {
                    const message = fixture.error;
                    fixture.error = null;
                    return route.fulfill({status: 400, json: {ocs: {data: {message}}}});
                }
                const request = route.request().postDataJSON();
                fixture.submissions.push(request);
                fixture.pending.push({id: 1, kind: request.kind, target_id: request.target});
            } else if (url.pathname !== '/api/board') {
                return route.abort();
            }
            return route.fulfill({json: {ocs: {data: {
                snapshot, pending: fixture.pending, requests: fixture.history, request_id: 1, message: 'Scheduled',
            }}}});
        });
        await page.goto(origin);
        await page.locator('[data-key="task-2"]').waitFor();
        await scenarios[scenario](page, fixture);
        assert.deepEqual(errors, []);
        console.log(`PASS: ${scenario}`);
    } finally {
        await browser.close();
    }
}

main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
