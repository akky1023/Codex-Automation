# Nextcloud spinner tests

```powershell
cd tests/browser
npm ci
npm test
```

Uses the installed Microsoft Edge in headless mode. Routes control response timing and the browser clock controls polling. `CQ_SPINNER_APP` can point to another checkout's `nextcloud/codex_queue` to test a deployment that contains additional UI changes.

`spinner.test.cjs` covers initial loading/failure/recovery, sending/rejection, continued waiting after HTTP 202, independent concurrent requests, accepted success/failure/interruption, polling failure/recovery, redraw while editing, reload, history over 20 entries, provisional Idea cards, RUNNING completion and reduced motion. No external Nextcloud account or Codex call is used by these tests.

## Live local Nextcloud

`live-spinner.cjs` only accepts localhost ports 8087/8088 and a dedicated `cq-spinner-*` account. Create a temporary account with `occ user:add --password-from-env`, then mark the first-run wizard read with `occ user:setting USER firstrunwizard show 99.0.0`. The mounted app version must match the installed app version.

```powershell
$env:CQ_SPINNER_USER = 'cq-spinner-verification'
$env:CQ_SPINNER_PASSWORD = '<temporary test account password>'
$env:CQ_SPINNER_PORT = '8087'
node tests/browser/live-spinner.cjs
```

The script publishes an isolated board for that account, submits a plan through the UI, claims it through the real bridge, holds completion across polling and reload, creates 25 completed requests to check the PHP history boundary, opens/closes another dialog and releases completion. It restores the original board snapshot. To also run a real Codex smoke command while the request is pending, set `CQ_SPINNER_CODEX_ENTRY` to the installed `@openai/codex/bin/codex.js`. This invokes Codex in a temporary directory with a read-only sandbox; it does not use a real project's planner configuration.

After testing, copy `cleanup-live.php` into the container and run `php /tmp/cleanup-live.php USER` as `www-data`, then delete the temporary account with `occ user:delete USER`. The cleanup removes only the named test account's board/requests. Remove the copied PHP script afterward.

## Verification on 2026-11-17

- Development sources: both browser tests passed with Microsoft Edge.
- Production sources including existing UI changes: both browser tests passed.
- Python: `tests/test_nextcloud.py` passed all 15 tests, including asynchronous plan success and revision failure; `tests/test_progress.py` also passed all 6 tests for interactive and plain output.
- PHP: the template passed syntax checking inside both Nextcloud containers.
- Live development Nextcloud (8087): real HTTP acceptance, bridge claiming, 25 completed requests plus an older pending request, polling, reload, an independent dialog and completion passed with a temporary user.
- Real Codex: a separate read-only smoke invocation remained running during the live UI check and exited successfully; the real project-specific plan generation was not invoked. Async planner/revision integration is covered by the Python tests above.
- Live production Nextcloud (8088): the same isolated-account UI/bridge checks passed using the mounted production application.
- Pending requests already use a separate unbounded `BoardStore::pending()` query; the existing PHP store and async Python paths were retained.
- Deployment: merged changes into production's three corresponding UI files, retaining its pre-existing configuration and Waiting Response features. Personal data, credentials and production configuration were retained.
- Development app metadata was temporarily matched to the container's installed 0.2.35 for live verification, then restored to this branch's original 0.2.34. Its pre-existing installed/source version mismatch remains; production's metadata was unchanged.
