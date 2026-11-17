const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../..');
const read = name => fs.readFileSync(path.join(process.env.CQ_SPINNER_APP || path.join(root, 'nextcloud/codex_queue'), name), 'utf8');
const html = read('templates/index.php').replace(/<\?php[\s\S]*?\?>/g, '').replace('data-api=""', 'data-api="/api"');
const until = async check => {
    for (let i = 0; i < 100; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 20)); }
    assert.fail('State did not arrive');
};
test('independent network, accepted requests, redraw, reload and running lifecycle', async () => {
    const browser = await chromium.launch({channel: 'msedge', headless: true});
    try {
        const page = await browser.newPage();
        await page.clock.install();
        let state = {snapshot: {projects: {1: 'Project'}, ideas: [1, 2].map(id => ({id, project_id: 1, title: 'Idea '+id, detail: ''})), tasks: [{id: 3, project_id: 1, name: 'Task', status: 'RUNNING'}]}, pending: [], requests: []};
        let fail = false, getGate, postGates = [], posts = 0;
        let releaseGet;
        getGate = new Promise(resolve => {releaseGet = resolve;});
        await page.route('http://spinner.test/**', async route => {
            if (!route.request().url().includes('/api/')) return route.fulfill({contentType: 'text/html', body: html + '<style>'+read('css/workspace.css')+'</style><script>window.OC={requestToken:"test"};</script><script>'+read('js/board.js')+'</script>'});
            if (route.request().method() === 'POST') {
                const data = route.request().postDataJSON();
                const id = ++posts;
                await new Promise(resolve => postGates.push(resolve));
                state.pending.push({id, kind: data.kind, target_id: data.target, claimed: true});
                return route.fulfill({status: 202, json: {ocs: {data: {...state, message: 'Accepted'}}}});
            }
            if (getGate) await getGate;
            if (fail) return route.abort();
            return route.fulfill({json: {ocs: {data: state}}});
        });
        await page.goto('http://spinner.test/');
        assert.equal(await page.locator('#cq-network').getAttribute('aria-busy'), 'true');
        releaseGet(); getGate = null;
        await until(async () => await page.locator('.cq-card').count() === 3);
        assert.equal(await page.locator('[data-key="task-3"]').getAttribute('aria-busy'), 'true');
        const plan = id => page.locator('[data-request-kind="plan"][data-request-target="'+id+'"]');
        await plan(1).click();
        await until(() => postGates.length === 1);
        assert.equal(await plan(1).isDisabled(), true);
        assert.equal(await plan(2).isEnabled(), true);
        await plan(2).click();
        await until(() => postGates.length === 2);
        postGates[0]();
        await until(async () => (await page.locator('#cq-message').textContent()).includes('Accepted'));
        assert.equal(await page.locator('#cq-network').getAttribute('aria-busy'), 'true');
        postGates[1]();
        await until(async () => await page.locator('#cq-network').getAttribute('aria-busy') === 'false');
        assert.equal(await plan(1).getAttribute('aria-busy'), 'true');
        assert.equal(await plan(2).getAttribute('aria-busy'), 'true');
        await page.reload();
        await until(async () => await plan(1).count() === 1);
        assert.equal(await plan(1).getAttribute('aria-busy'), 'true');
        state.requests = Array.from({length: 25}, (_, i) => ({id: i+20, kind: 'sync', outcome: '完了'}));
        fail = true;
        await page.clock.runFor(5100);
        await until(async () => (await page.locator('#cq-message').textContent()).includes('fetch'));
        assert.equal(await page.locator('#cq-network').getAttribute('aria-busy'), 'false');
        assert.equal(await plan(1).getAttribute('aria-busy'), 'true');
        fail = false;
        await page.clock.runFor(5100);
        await until(async () => await page.locator('#cq-history li').count() === 27);
        assert.equal(await plan(1).getAttribute('aria-busy'), 'true');
        // Busy changes must arrive even while an open form suppresses card redraw.
        await page.locator('.cq-idea-menu').first().evaluate(el => el.open = true);
        state.pending = [];
        state.requests = [{id: 1, kind: 'plan', outcome: '失敗'}, {id: 2, kind: 'plan', outcome: '中断'}];
        state.snapshot.tasks[0].status = 'DONE';
        await page.clock.runFor(5100);
        await until(async () => await plan(1).getAttribute('aria-busy') === 'false');
        await page.locator('.cq-idea-menu').first().evaluate(el => el.open = false);
        await page.clock.runFor(5100);
        await until(async () => await page.locator('#cq-done .cq-card').count() === 1);
        assert.equal(await page.locator('[data-key="task-3"]').getAttribute('aria-busy'), 'false');
        await page.emulateMedia({reducedMotion: 'reduce'});
        await page.locator('#cq-network').evaluate(el => el.classList.add('cq-busy'));
        assert.equal(await page.locator('#cq-network').evaluate(el => getComputedStyle(el, '::before').animationName), 'none');
        assert.equal(posts, 2);
    } finally {await browser.close();}
});


test('failed fetch and rejected submission release only their own spinner', async () => {
    const browser = await chromium.launch({channel: 'msedge', headless: true});
    try {
        const page = await browser.newPage();
        await page.clock.install();
        let readFailed = true, reject = true;
        const state = {snapshot: {projects: {1: 'Project'}, ideas: [], tasks: []}, pending: [], requests: []};
        await page.route('http://spinner.test/**', async route => {
            if (!route.request().url().includes('/api/')) return route.fulfill({contentType: 'text/html', body: html + '<script>window.OC={requestToken:"test"};</script><script>'+read('js/board.js')+'</script>'});
            if (route.request().method() === 'GET') return route.fulfill({status: readFailed ? 503 : 200, json: {ocs: {data: readFailed ? {message: 'Unavailable'} : state}}});
            if (reject) return route.fulfill({status: 400, json: {ocs: {data: {message: 'Rejected'}}}});
            const data = route.request().postDataJSON();
            state.pending = [{id: 1, kind: data.kind, target_id: data.target, title: data.title, body: '', claimed: true}];
            return route.fulfill({status: 202, json: {ocs: {data: state}}});
        });
        await page.goto('http://spinner.test/');
        await until(async () => await page.locator('#cq-message').textContent() === 'Unavailable');
        assert.equal(await page.locator('#cq-network').getAttribute('aria-busy'), 'false');
        readFailed = false;
        await page.clock.runFor(5100);
        await until(async () => await page.locator('#cq-filter option').count() === 2);
        await page.locator('#cq-sync').click();
        await until(async () => await page.locator('#cq-message').textContent() === 'Rejected');
        assert.equal(await page.locator('#cq-sync').isEnabled(), true);
        assert.equal(await page.locator('#cq-sync').getAttribute('aria-busy'), 'false');
        reject = false;
        await page.locator('#cq-open-add').click();
        await page.locator('#cq-add select[name="target"]').selectOption('1');
        await page.locator('#cq-add input[name="title"]').fill('Pending idea');
        await page.locator('#cq-add button[type="submit"]').click();
        await until(async () => await page.locator('[data-key="idea-pending-1"]').count() === 1);
        assert.equal(await page.locator('[data-key="idea-pending-1"]').getAttribute('aria-busy'), 'true');
        state.pending = [];
        state.snapshot.ideas = [{id: 7, project_id: 1, title: 'Pending idea', detail: ''}];
        state.requests = [{id: 1, kind: 'add', outcome: 'Success'}];
        await page.clock.runFor(5100);
        await until(async () => await page.locator('[data-key="idea-7"]').count() === 1);
        assert.equal(await page.locator('[data-key="idea-7"]').getAttribute('aria-busy'), 'false');
    } finally {await browser.close();}
});
