# Codex Queue

Codex Queue runs tasks through Codex CLI in local Git repositories. On Windows, Task Scheduler checks the queue every 10 minutes and at sign-in by default.

## Install on Windows

Install Python 3.10 or later, pipx, Git, and Codex CLI. Make Git and Codex available on PATH. Download the wheel from GitHub Releases and run:

```powershell
pipx install .\codex_queue-0.1.0-py3-none-any.whl
cq setup
```

Setup reuses your Codex CLI login and registers the worker under your Windows account. On the first setup, automatic execution stays paused. From a Git repository, add a task and start the queue:

```powershell
cq add "Update the search results"
cq start
cq
```

Run `cq --help` for commands. `cq ls` shows the queue and completed tasks. `cq logs ID` shows attempts and their log locations. `cq pause` lets running tasks finish. `cq stop` interrupts them and preserves their working changes and sessions. `cq resume` enables execution again. Use `cq reply ID "answer"` for a task waiting for input.

By default, each task runs on its own branch and returns to the original branch after successful completion. Review and integrate task branches with Git. Use `cq config --parallel 3 --base develop` to configure concurrency and a default starting branch.

## Optional Nextcloud Tasks integration

Run `cq nextcloud connect` and approve the connection in your browser. Use `cq nextcloud lists` to find the list ID and `cq project ls` to find the project ID. Link a list using `cq nextcloud link --list LIST_ID --project-id PROJECT_ID`. Give edit access to people you trust to submit Codex instructions and assign one execution PC per list.

Run `cq sync` to synchronize immediately. Use `cq config --sync manual` to synchronize only on request. Nextcloud credentials are stored in Windows Credential Manager. Run `cq nextcloud disconnect` to remove the stored credentials.
