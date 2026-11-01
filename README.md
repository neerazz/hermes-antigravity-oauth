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
- **`/usage` reads cached quota only.** `/usage` and `hermes usage` never launch agy or check authentication. They retain the original observation timestamp and label stale/error observations. Refresh is an explicit, separate action (below).

### Optional quota/reset dock

From the provider checkout, use `python3 usage.py status` for cache-only status,
or `python3 usage.py refresh` to explicitly request a new observation. Add
`--json` for structured output. Refresh retains PR3's saved-session and
agy >=1.1.11 guards, private cwd, auto-update disable switch, shared 7.5-second
CLI budget and process-tree timeout cleanup. It accepts only a successful
`/usage` command with zero turns and zero token accounting; no inference,
login, billing, token reads or model switches are added.

The cache is profile-scoped at
`$HERMES_HOME/plugin-data/antigravity-oauth/quota/latest.json` (default
`~/.hermes`), sanitized, atomically replaced with mode 0600, and protected by
a nonblocking OS writer lock. State ancestry must be trusted; symlink
directories/files and nonregular/multiply linked state files are refused.
Readers never create state directories. A failed observation retains prior
values with error health when state storage remains writable. Cache lifetime
is 30 minutes; elapsed resets are **due/unconfirmed**, never invented refills.

The pool set is account-driven, not hardcoded: the cache holds one row per
quota bucket `agy /usage` reports for your account — typically the five-hour
and weekly windows, plus optional pools such as the Gemini 4 Argon group when
your plan has one. A new pool surfaces on the next refresh with no plugin
update; a pool agy does not report stays Unknown rather than an invented
value, and labels stay unique per provider so rows and alert history key
stably across renames.

Five-hour and weekly meters are shown independently. If agy reports only
weekly data, five-hour quota stays **Unknown** and complete pool availability
cannot be established.

To opt in to the Ink TUI dock, create a small adapter in your active profile's
`tui-widgets/agy-usage.mjs`, replacing the illustrative path below with the
provider checkout's absolute path:

```js
export { default } from 'file:///path/to/hermes-antigravity-oauth/tui/agy-usage.mjs';
```

Registration is inert; `/agy-usage` toggles the ambient dock without capturing
composer keys. `/agy-usage refresh` refreshes that dock, `/agy-usage status`
rereads the cache, and `/agy-usage details` opens exact reset timestamps in a
closable modal. `R` explicitly refreshes the modal only; reread the dock with
`/agy-usage status` afterward. `/agy-usage alerts on` opts in to local <=10%
crossing warnings, deduplicated by fresh healthy observation identity; alerts
are off initially and cleared when the dock closes. No backend polling or
collector is installed: timers only update elapsed age/countdowns. The
argv-only transport uses `HERMES_PYTHON` or `python3` and inherits the active
profile. Closing or replacing a viewer discards its late results. An explicitly
started refresh may finish after the dock/modal closes: UI cancellation does
not terminate its Python supervisor, so it retains the writer lock through
bounded child cleanup and cache publication. A competing refresh returns
`refresh_busy`; there are no automatic retries. Pre-cancelled requests never
spawn, and cache-only status reads remain cancellable. The 15-second outer
transport timeout is still a last-resort supervisor termination, not a
process-tree cleanup guarantee; arbitrary OS kills or interpreter crashes can
also bypass Python cleanup. This optional widget is adapted from the MIT-licensed public
[quota widget](https://github.com/taylorwtf/hermes-antigravity-oauth-plus/tree/b2bdf8c56966f26552168553bf8cc29470524971).

Offline verification: run the Python files as prescribed by the existing CI
workflow and `node --test tests/usage_widget.test.mjs`. Widget tests exercise
SDK lifecycle, geometry, request fencing and warnings, not real terminal
painting; actual Ink TUI appearance remains a separate manual check.

## Limitations

- Explicit quota refresh needs agy 1.1.11 or newer and a saved session. Older versions never receive `/usage` as a prompt; cached reads still work.
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

## Live reasoning on non-streaming Hermes turns

`external_process` authentication remains unchanged: Hermes selects its normal
non-streaming path. For foreground agents with an installed reasoning callback
and a text-stream consumer, the plugin's collector forwards available reasoning
while it assembles the response. It calls the existing callback, preserving the
TUI's show/hide and notification-mute gates. Reasoning remains separate from
answer/tool content and is never added to the replay prompt.

The profile's main-request projection installs `llm_execution` middleware using
`PluginContext.register_middleware`. This explicit bootstrap is necessary because
Hermes' provider discovery imports model-provider plugins but skips their general
`register(ctx)` lifecycle. Registration is profile-local, idempotent, and uses an
unloadable host registration handle. Auxiliary projections carry no session ID.

A context-local, single-collector lease fences delivery by profile, provider,
model, session, turn, request, current callback identity and client liveness.
Cancellation, closing sessions, request replacement and middleware exit disable
late delivery. Missing host compatibility seams disable only the optional live
presentation; completed reasoning and usage remain available. Streaming callers
continue to receive ordinary chunks, with no extra callback delivery.

This uses read-only host internals for current-agent/session lookup; compatibility
is tested against the installed Hermes checkout, not guaranteed for all releases.
Already running processes retain their previously loaded plugin code; this plugin
does not hot-patch them. No daemon restart or configuration change is performed.
Agy may return no thinking text even when a request otherwise succeeds. Live
history restoration and actual frontend rendering are separate from delivery.

The opt-in `scripts/verify_live_reasoning.py` makes one bounded, tool-free call to
bare `gemini-4-argon` using the existing account. It exercises real AIAgent kwargs,
the unchanged `_should_stream=False` decision, middleware, the nonstream worker,
agy/DB reader, collector, and installed TUI reasoning callback. Only UI egress is
captured; it does not prove display in an existing desktop window. Its receipt
records counts/times, never reasoning text. See `VERIFICATION.md`.

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
