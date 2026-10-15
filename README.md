# hermes-antigravity-oauth

[![npm](https://img.shields.io/npm/v/hermes-antigravity-oauth?logo=npm)](https://www.npmjs.com/package/hermes-antigravity-oauth)
[![release](https://github.com/neerazz/hermes-antigravity-oauth/actions/workflows/release.yml/badge.svg)](https://github.com/neerazz/hermes-antigravity-oauth/actions/workflows/release.yml)
[![provenance](https://img.shields.io/badge/npm-provenance-blue)](https://www.npmjs.com/package/hermes-antigravity-oauth#provenance)
[![license](https://img.shields.io/github/license/neerazz/hermes-antigravity-oauth)](LICENSE)

Run [Hermes Agent](https://hermes-agent.nousresearch.com) on the models available to your Google Antigravity plan with a single sign-in.

```bash
npx hermes-antigravity-oauth          # install + enable the plugin, then sign in
agy models                           # choose an exact ID available to your account
hermes --provider antigravity-oauth -m '<exact-model-id>'
```

## How it works

- **The calls are Antigravity CLI calls.** Every model request is made by Google's official `agy` binary, using agy's own login. The plugin sends no HTTP requests of its own and never imitates Google's OAuth client.
- **Sign-in belongs to agy.** `hermes auth add antigravity-oauth` runs agy's browser OAuth flow and then confirms it live with `agy models`. Over SSH, paste the code back when asked. The token stays wherever agy stores it (OS keychain or agy's token file). The plugin never reads, copies, uploads, or writes it.
- **What it does touch (disclosure):** each agy process runs in a private temp HOME. To let agy reuse its own login there, the plugin symlinks agy's `antigravity-oauth-token` file into that HOME, and on macOS it symlinks `~/Library/Keychains`. Before spawning, it checks the OS keyring for the credential's existence only (`security` / `secret-tool` / `cmdkey`) and never reads the secret. The temp HOME is deleted when the session closes.
- **Your Hermes SOUL.md is the persona.** Each turn, the plugin writes Hermes' system prompt into agy's private workspace as `GEMINI.md`, which agy loads as first-class rules. That prompt starts with `$HERMES_HOME/SOUL.md`, and the file is added directly if Hermes left it out.
- **Hermes runs the tools.** agy's built-in tools never execute on your host. When a model calls one anyway, the plugin re-issues it as the matching Hermes tool: `run_command`→`terminal`, `view_file`→`read_file`, `grep_search`/`find_by_name`→`search_files`, `write_to_file`→`write_file`, `search_web`→`web_search`, `read_url_content`→`web_extract`. It then runs under Hermes' approvals and sandbox. Any other native tool is blocked.
- **`/usage` asks agy for your quota.** `/usage` and `hermes usage` run `agy --version` and then `agy -p /usage --output-format json`, which answers from agy's own session without a model turn. You get one row per quota group, such as `Gemini 7d` and `Claude/GPT 7d`. It only runs when agy is signed in, from a temp folder with your normal HOME and agy's auto-updater turned off.

## Limitations

- `/usage` needs agy 1.1.11 or newer. On older versions it shows nothing.
- On Windows, a hung `/usage` call is cleaned up through Hermes' process helper (Hermes 0.21.4 or newer, with psutil installed), which kills everything agy started. Without it, a process agy started can keep running in the background.

## Install

Requirements: Hermes Agent, plus the Antigravity CLI (`agy`) from <https://antigravity.google/cli>.

| Route | Command |
|---|---|
| npx (recommended) | `npx hermes-antigravity-oauth` (add `--yes` to install agy if it is missing) |
| Hermes directly | `hermes plugins install https://github.com/neerazz/hermes-antigravity-oauth --enable` then `hermes auth add antigravity-oauth` |

npx commands: `install` (default), `login`, `status`, `uninstall`. Flags: `--yes`, `--no-login`, `--force`, and a 40-hex commit SHA to pin the version.

## Auth commands

| Command | Effect |
|---|---|
| `hermes auth add antigravity-oauth` | Runs agy's Google sign-in, then verifies it live |
| `hermes auth status antigravity-oauth` | Live check through `agy models` |
| `hermes auth logout antigravity-oauth` | Explains agy's `/logout` (Hermes holds no token) |

Aliases: `google-antigravity`, `agy-oauth`.

### Headless / remote login

`agy` prints its Google OAuth URL and waits ~60 seconds for the code on its
own TTY. When the approver is not at that terminal (SSH without a browser,
a chat-driven host), relay the code through a file instead:

```sh
git clone https://github.com/neerazz/hermes-antigravity-oauth
python3 hermes-antigravity-oauth/scripts/remote-login.py --code-file /tmp/agy-code.txt &
# open the printed LOGIN_URL, approve, then:
printf '%s' '<code>' > /tmp/agy-code.txt
wait  # exits 0 once `agy models` confirms the session
```

The script drives agy under a PTY with only the standard library, feeds the
code the moment it arrives (polling, not blocking on agy output — agy is
silent during its wait, which is where naive wrappers starve), and verifies
with `agy models`. Note: agy still allows ~60 seconds from URL to code, so
the approver should open the URL promptly; without `--code-file` the code is
read from stdin.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `ANTIGRAVITY_COMMAND` | `agy` | Path to the agy binary |
| `ANTIGRAVITY_WORKSPACE_RULES` | `1` | `0` puts SOUL/system prompt inline in the prompt instead of `GEMINI.md` |

### Live model IDs and reasoning

The model picker uses only structurally valid ID/name rows from a successful
`agy models` call. It preserves exact IDs, case, order, model families and native
effort variants, deduplicating only identical IDs. Failed, timed-out or incomplete
discovery returns no catalog; there is no hardcoded fallback list or recommended
auxiliary model. A successful empty response remains distinct from failure.
Discovery is bounded and noninteractive; use the auth commands above to sign in.

Selected IDs are passed verbatim to `--model` in both worker and one-shot calls.
Native effort suffixes own their effort and do not expose a second reasoning
control; contradictory direct or inherited effort settings cannot rewrite them.
For a manually entered suffixless `gemini-*` ID, the separate CLI `--effort` flag
accepts `low`, `medium`, `high`, `xhigh` and `max` without generating a model ID or
downgrading `max`. Claude and GPT-OSS get no separate flag. Generation-pinned
aliases and implicit plugin model/effort defaults are not used.

### Context metadata and serving limits

`ANTIGRAVITY_CONTEXT_LENGTH` accepts a positive integer token budget as an explicit
user override. When unset, invalid or nonpositive, the documented
`ProviderProfile.get_model_context_length` hook returns `None`, letting Hermes
resolve the **exact selected ID** through its native metadata. The plugin imposes
no blanket context capacity and imports no private metadata resolver.

Metadata is not proof of the maximum context served by your account or the CLI.
Unrecognized exact IDs can receive Hermes' generic unknown-model fallback: for
example, `gpt-oss-120b-medium` is currently unresolved by the native resolver.
That fallback is **unknown metadata, not native model capacity**. If necessary,
set an explicit conservative budget for your route. This change does not establish
million-token account acceptance; serving limits require separate live testing.

## Development

```bash
PYTHONPATH=<hermes-agent checkout>:. python tests/test_auth_handler.py   # etc. for each tests/test_*.py
```

## Releases

Every push to `main` runs `.github/workflows/release.yml`. It first runs the full test suite against Hermes Agent `main`. Then it publishes the next version to npm with provenance, using npm trusted publishing (GitHub OIDC), so the repo stores no npm token. Finally it tags the commit `vX.Y.Z`.

- Default: patch bump from the latest published version.
- Minor/major: set `version` in `package.json` above the published one in the commit, and that exact version is published.
- A red test run publishes nothing.

## Credits

The streaming client, process isolation, and keyring probes come from [soyelmismo/hermes-antigravity-subscription](https://github.com/soyelmismo/hermes-antigravity-subscription) (MIT) at `b7ab470`. This package adds the auth handler, SOUL delivery, native-tool bridging, and the npx installer.

MIT © Neeraj Kumar Singh Beshane
