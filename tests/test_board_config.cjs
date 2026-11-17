// Exercise the real settings handlers with a small DOM double; this is not browser QA.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor(tag, text = '') {
        this.tagName = tag.toUpperCase(); this.textContent = text; this.children = [];
        this.dataset = {}; this.listeners = {}; this.value = ''; this.disabled = false;
    }
    append(...children) {
        this.children.push(...children);
        if (children.length && this.tagName === 'SELECT' && this.children.length === children.length) this.value = children[0].value;
    }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    addEventListener(event, handler) { this.listeners[event] = handler; }
    setAttribute() {}
    querySelector(selector) { return this.children.find(child => selector === '[data-mode]' ? child.dataset.mode : child.dataset.value); }
    showModal() { this.open = true; }
    close() { this.open = false; }
}
const elements = new Map();
const find = selector => {
    if (!elements.has(selector)) elements.set(selector, new Element(selector.includes('scope') ? 'select' : 'div'));
    return elements.get(selector);
};
const source = fs.readFileSync('nextcloud/codex_queue/js/board.js', 'utf8');
const settingsCode = source.slice(source.indexOf('    const configDialog'), source.indexOf('    function taskColumn'));
let sent;
const context = vm.createContext({find, node: (tag, text) => new Element(tag, text),
    snapshot: {projects: {1: 'One'}}, boardVersion: 0,
    api: async (path, data) => {
        sent = data;
        return {request_id: 12, snapshot: payload.snapshot, pending: [{id: 12, kind: 'config', target_id: data.target}]};
    },
});
vm.runInContext(settingsCode + '\nglobalThis.updateBoard = updateConfiguration; globalThis.update = updateConfiguration;', context);
let payload = {snapshot: {configuration: {global: {parallel: '1', base: 'main'}, projects: {1: {name: 'One', base: null}}}}, pending: []};
context.update({snapshot: {}});
assert.equal(find('#cq-open-config').disabled, true);
context.update(payload);
assert.equal(find('#cq-open-config').disabled, false);
find('#cq-open-config').listeners.click();
const fields = find('#cq-config-fields');
const input = fields.children[0].querySelector('[data-value]');
input.value = '3';
context.update({...payload, snapshot: {...payload.snapshot, configuration: {...payload.snapshot.configuration, global: {parallel: '2', base: 'main'}}}});
assert.equal(input.value, '3', 'polling keeps the draft');
(async () => {
    await find('#cq-config-form').listeners.submit({preventDefault() {}});
    assert.deepEqual(JSON.parse(sent.body), {changes: {parallel: '3'}, expected: {parallel: '1'}});
    assert.equal(find('#cq-save-config').disabled, true);
    context.update({...payload, requests: [{id: 12, outcome: 'conflict'}]});
    assert.equal(find('#cq-config-message').textContent, 'conflict');
    find('#cq-config-scope').value = '1';
    find('#cq-config-scope').listeners.change();
    const base = fields.children.find(row => row.dataset.field === 'base');
    assert.equal(base.querySelector('[data-value]').disabled, true);
    const mode = base.querySelector('[data-mode]'); mode.value = 'value'; mode.listeners.change();
    base.querySelector('[data-value]').value = '';
    await find('#cq-config-form').listeners.submit({preventDefault() {}});
    assert.deepEqual(JSON.parse(sent.body), {changes: {base: ''}, expected: {base: null}});
    context.update({snapshot: {configuration: {global: {base: 'main'}, projects: {}}}, requests: [{id: 12, outcome: 'saved'}]});
    assert.equal(find('#cq-save-config').disabled, true, 'a disabled project cannot be edited again');
    console.log('PASS: settings draft, original values, pending, conflicts, inheritance, empty values, removed scope, legacy PC');
})().catch(error => { console.error(error); process.exitCode = 1; });
