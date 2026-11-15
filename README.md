# hermes-antigravity-oauth

[![npm](https://img.shields.io/npm/v/hermes-antigravity-oauth?logo=npm)](https://www.npmjs.com/package/hermes-antigravity-oauth)
[![release](https://github.com/neerazz/hermes-antigravity-oauth/actions/workflows/release.yml/badge.svg)](https://github.com/neerazz/hermes-antigravity-oauth/actions/workflows/release.yml)
[![provenance](https://img.shields.io/badge/npm-provenance-blue)](https://www.npmjs.com/package/hermes-antigravity-oauth#provenance)
[![downloads](https://img.shields.io/npm/dm/hermes-antigravity-oauth)](https://www.npmjs.com/package/hermes-antigravity-oauth)
[![stars](https://img.shields.io/github/stars/neerazz/hermes-antigravity-oauth)](https://github.com/neerazz/hermes-antigravity-oauth/stargazers)
[![Hermes plugin catalog](https://img.shields.io/badge/Hermes-plugin%20catalog-7B3FE4)](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/antigravity-oauth.yaml)
[![platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey)](#install)
[![license](https://img.shields.io/github/license/neerazz/hermes-antigravity-oauth)](LICENSE)

Run [Hermes Agent](https://hermes-agent.nousresearch.com) on your Google Antigravity plan. One sign-in, no API key.

## Quick start

```bash
npx hermes-antigravity-oauth          # install the plugin and sign in
agy models                           # list the model IDs your plan allows
hermes --provider antigravity-oauth -m '<exact-model-id>'
```

On a fresh Hermes install, the last command stops with `Hermes isn't configured yet`. Set the plugin as your default first:

```bash
hermes config set model.provider antigravity-oauth
hermes config set model.default '<exact-model-id>'
```

## Let your AI agent set it up

Paste this into Hermes, Claude Code, Codex, Cursor or any agent with a terminal. Whichever agent runs it, the thing it configures is Hermes:

```text
Set up the hermes-antigravity-oauth plugin so Hermes Agent on this machine runs on my Google Antigravity plan.
Fetch https://raw.githubusercontent.com/neerazz/hermes-antigravity-oauth/main/AGENT_SETUP.md and follow it step by step.
Run every check it lists, stop and ask me at each ASK or HUMAN step, never read or print my agy token,
and finish with the setup report at the end of that file.
```

The agent installs the plugin, hands you the Google sign-in, asks which model to use, sends one test message and reports PASS or the step that failed. It follows [AGENT_SETUP.md](AGENT_SETUP.md), which ends with a table of known errors and their fixes.

## How it works

- Every model call is made by Google's official `agy` CLI with agy's own login. The plugin sends no HTTP requests of its own and never imitates Google's OAuth client.
- The plugin never reads, copies or stores your token. Each agy process runs in a private temp HOME. The plugin symlinks agy's token file into it, plus `~/Library/Keychains` on macOS. It checks only that the keyring entry exists, and it deletes the temp HOME when the session ends.
- Your `SOUL.md` and the Hermes system prompt reach agy as workspace rules (`GEMINI.md`).
- Tools run in Hermes, under Hermes approvals; agy's built-in tools never execute on your machine. When a model calls one, the plugin maps it to the Hermes equivalent and blocks anything unmapped: `run_command`→`terminal`, `view_file`→`read_file`, `grep_search`/`find_by_name`→`search_files`, `write_to_file`→`write_file`, `search_web`→`web_search`, `read_url_content`→`web_extract`.
- `/usage` and `hermes usage` show your quota per model group (for example `Gemini 7d`, `Claude/GPT 7d`), read from agy without spending a model turn.

Model IDs, context limits and live reasoning: [docs/internals.md](docs/internals.md).

## Known issues

- Requests can fail with `RESOURCE_EXHAUSTED (429)` after 2 to 3 minutes while quota remains. Google rejects the request on its side ([#27](https://github.com/neerazz/hermes-antigravity-oauth/issues/27)).
- agy 1.4 and newer renamed its file tools, and the plugin does not bridge the new names yet ([#19](https://github.com/neerazz/hermes-antigravity-oauth/issues/19)).
- `/usage` needs agy 1.1.11 or newer.
- On Windows, a hung `/usage` call is cleaned up only with Hermes 0.21.4+ and `psutil` installed. Without them, an agy child process can keep running.

## Install

Needs Hermes Agent 0.21.0+ and the Antigravity CLI (`agy`) from <https://antigravity.google/cli>.

| Route | Command |
|---|---|
| npx (recommended) | `npx hermes-antigravity-oauth` (add `--yes` to also install agy) |
| Hermes only | `hermes plugins install https://github.com/neerazz/hermes-antigravity-oauth --enable`, then `hermes auth add antigravity-oauth` |

npx commands: `install` (default), `login`, `status`, `uninstall`. Flags: `--yes`, `--no-login`, `--force`, or a 40-character commit SHA to pin a version. Hermes may print `Skipped Node deps` during install. Ignore it; the plugin is Python.

## Sign-in

| Command | Effect |
|---|---|
| `hermes auth add antigravity-oauth` | Runs agy's Google sign-in and confirms it |
| `hermes auth status antigravity-oauth` | Checks the session live with `agy models` |
| `hermes auth logout antigravity-oauth` | Points you to agy's `/logout`; Hermes holds no token |

Aliases: `google-antigravity`, `agy-oauth`.

### Signing in on a remote machine

agy prints a Google URL and waits about 60 seconds for the code on its own terminal. If you approve from somewhere else (SSH without a browser, a chat-driven host), pass the code through a file:

```sh
git clone https://github.com/neerazz/hermes-antigravity-oauth
CODE_FILE="$(mktemp)"; chmod 600 "$CODE_FILE"
python3 hermes-antigravity-oauth/scripts/remote-login.py --code-file "$CODE_FILE" &
# open the printed LOGIN_URL, approve, then:
printf '%s' '<code>' > "$CODE_FILE"
wait   # exits 0 once agy models confirms the session
```

The script keeps checking the file while agy sits silent, so the code goes in the moment it lands. The 60-second limit still applies. Without `--code-file`, it reads the code from stdin.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `ANTIGRAVITY_COMMAND` | `agy` | Path to the agy binary |
| `ANTIGRAVITY_WORKSPACE_RULES` | `1` | `0` sends SOUL and the system prompt inside the message instead of `GEMINI.md` |
| `ANTIGRAVITY_CONTEXT_LENGTH` | unset | Token budget that overrides Hermes' context metadata ([details](docs/internals.md#context-metadata-and-serving-limits)) |

## Development

```bash
PYTHONPATH=<hermes-agent checkout>:. python tests/test_auth_handler.py   # repeat for each tests/test_*.py
```

## Releases

Every push to `main` runs the test suite against Hermes Agent `main`. If it passes, the workflow publishes the next npm version with provenance and tags the commit `vX.Y.Z`. Publishing uses npm trusted publishing (GitHub OIDC), so the repo stores no npm token.

- Default: patch bump over the latest published version.
- Minor or major: set a higher `version` in `package.json`, and that exact version ships.
- A failing test run publishes nothing.

## Credits

The streaming client, process isolation and keyring checks come from [soyelmismo/hermes-antigravity-subscription](https://github.com/soyelmismo/hermes-antigravity-subscription) (MIT) at `b7ab470`. This package adds the auth handler, SOUL delivery, native-tool bridging and the npx installer.

MIT © Neeraj Kumar Singh Beshane
