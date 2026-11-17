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

const scenarios = {configuration, waiting, retry};

async function main() {
    const [scenario, app = 'nextcloud/codex_queue'] = process.argv.slice(2);
    assert.ok(Object.hasOwn(scenarios, scenario), 'Usage: node tests/browser/board.cjs configuration|waiting|retry [app-directory]');
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
            if (Object.hasOwn(files, url.pathname)) {
                const contentType = url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css';
                return route.fulfill({contentType, body: read(files[url.pathname])});
            }
            if (url.pathname === '/') return route.fulfill({contentType: 'text/html; charset=utf-8', body:
                '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">' +
                '<link rel="stylesheet" href="/workspace.css"><script defer src="/board.js"></script>' + template});
            if (url.pathname === '/api/requests') {
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
