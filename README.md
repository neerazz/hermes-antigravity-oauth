# hermes-antigravity-oauth

[![npm](https://img.shields.io/npm/v/hermes-antigravity-oauth?logo=npm)](https://www.npmjs.com/package/hermes-antigravity-oauth)
[![release](https://github.com/neerazz/hermes-antigravity-oauth/actions/workflows/release.yml/badge.svg)](https://github.com/neerazz/hermes-antigravity-oauth/actions/workflows/release.yml)
[![provenance](https://img.shields.io/badge/npm-provenance-blue)](https://www.npmjs.com/package/hermes-antigravity-oauth#provenance)
[![downloads](https://img.shields.io/npm/dm/hermes-antigravity-oauth)](https://www.npmjs.com/package/hermes-antigravity-oauth)
[![stars](https://img.shields.io/github/stars/neerazz/hermes-antigravity-oauth)](https://github.com/neerazz/hermes-antigravity-oauth/stargazers)
[![Hermes plugin catalog](https://img.shields.io/badge/Hermes-plugin%20catalog-7B3FE4)](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/antigravity-oauth.yaml)
[![license](https://img.shields.io/github/license/neerazz/hermes-antigravity-oauth)](LICENSE)

Run [Hermes Agent](https://hermes-agent.nousresearch.com) on your Google Antigravity plan. One sign-in, no API key.

## Quick start

Needs Hermes Agent 0.21.0+, Node 18+ for npx, and the Antigravity CLI (`agy`) from <https://antigravity.google/cli>.

```bash
npx hermes-antigravity-oauth          # install the plugin and sign in
agy models                           # list the exact IDs your plan allows
hermes --provider antigravity-oauth -m '<exact-model-id>'
```

If Hermes says `Hermes isn't configured yet`, set the plugin as your default first:

```bash
hermes config set model.provider antigravity-oauth
hermes config set model.default '<exact-model-id>'
```

## Let your AI agent set it up

Paste this into an agent with a terminal. It configures Hermes, whichever agent runs it:

```text
Set up the hermes-antigravity-oauth plugin so Hermes Agent on this machine runs on my Google Antigravity plan.
Fetch https://raw.githubusercontent.com/neerazz/hermes-antigravity-oauth/main/AGENT_SETUP.md and follow it step by step.
Run every check it lists, stop and ask me at each ASK or HUMAN step, never read or print my agy token,
and finish with the setup report at the end of that file.
```

[AGENT_SETUP.md](AGENT_SETUP.md) covers sign-in, remote code handoff, a test message and troubleshooting.

## How it works

Hermes runs the agent loop. The Python plugin adapts model calls to Google's official `agy` subprocess, which owns sign-in and Google network requests. SOUL and system instructions reach agy through workspace `GEMINI.md` rules.

The private HOME isolates workspace/session state, **not credentials or OS access**. The plugin can link or copy the encrypted token file and links the real macOS Keychains directory. It does not decrypt or parse token contents. Supported native tool events become offered Hermes tool calls under Hermes approvals; this is not a universal native-tool sandbox.

[Architecture and graph](docs/architecture.md) explains those boundaries. [Internals](docs/internals.md) covers exact model IDs, context limits and optional live reasoning. `/usage` and `hermes usage` report agy's model-group quota without a model turn.

## Known issues

- Requests can fail with `RESOURCE_EXHAUSTED (429)` after 2 to 3 minutes while quota remains. Google rejects the request on its side ([#27](https://github.com/neerazz/hermes-antigravity-oauth/issues/27)).
- agy 1.4 and newer renamed its file tools, and the plugin does not bridge the new names yet ([#19](https://github.com/neerazz/hermes-antigravity-oauth/issues/19)).
- `/usage` needs agy 1.1.11 or newer.
- On Windows, a hung `/usage` call is cleaned up only with Hermes 0.21.4+ and `psutil` installed. Without them, an agy child process can keep running.
- The auth handler's post-login suggestion can strip a Gemini effort suffix. Use the exact ID from `agy models` instead ([details](docs/internals.md#live-model-ids-and-reasoning)).

## Install and sign in

Without npx: `hermes plugins install https://github.com/neerazz/hermes-antigravity-oauth --enable`, then `hermes auth add antigravity-oauth`.

npx commands: `install` (default), `login`, `status`, `uninstall`. Flags: `--yes` (also install agy), `--no-login`, `--force`, or a 40-character commit SHA to pin the plugin. Hermes may print `Skipped Node deps`; the plugin is Python.

| Command | Effect |
|---|---|
| `hermes auth add antigravity-oauth` | Runs agy's sign-in and verification prompt (can consume a model turn) |
| `hermes auth status antigravity-oauth` | Checks the session live with `agy models` |
| `hermes auth logout antigravity-oauth` | Prints agy's `/logout` instructions; does not sign you out |

Aliases: `google-antigravity`, `agy-oauth`. For remote sign-in, follow [AGENT_SETUP.md](AGENT_SETUP.md).

### GitHub Packages (optional)

The scoped distribution is [`@neerazz/hermes-antigravity-oauth`](https://github.com/neerazz/hermes-antigravity-oauth/pkgs/npm/hermes-antigravity-oauth). It uses the same installer and Git-based plugin install as npm. Availability depends on a GitHub registry release; adding this route does not itself publish a package.

GitHub requires a **personal access token (classic)** with `read:packages`, even for public package downloads. Enter it only at npm's login prompt:

```bash
npm login --scope=@neerazz --auth-type=legacy --registry=https://npm.pkg.github.com
npx --registry=https://npm.pkg.github.com @neerazz/hermes-antigravity-oauth
```

The unscoped `npx hermes-antigravity-oauth` remains the recommended route and needs no GitHub Packages token.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `ANTIGRAVITY_COMMAND` | `agy` | Path to the agy binary |
| `ANTIGRAVITY_WORKSPACE_RULES` | `1` | `0` sends SOUL and system instructions inline instead of `GEMINI.md` |
| `ANTIGRAVITY_CONTEXT_LENGTH` | unset | Override Hermes' context metadata ([details](docs/internals.md#context-metadata-and-serving-limits)) |

## Contribute and release

Start with [CONTRIBUTING.md](CONTRIBUTING.md) for local setup, isolated tests and PR expectations. See [SECURITY.md](SECURITY.md) for security reports.

Releases use the manual [release workflow](https://github.com/neerazz/hermes-antigravity-oauth/actions/workflows/release.yml) on `main`, after the CI gate. Select `npm` (default) or `github`; pushing to `main` alone does not publish. GitHub Packages uses the workflow's ephemeral token and does not publish to npm or create a release tag. npm uses trusted publishing (GitHub OIDC). See the contribution guide for version rules.

## Credits

The streaming client, process isolation and keyring checks come from [soyelmismo/hermes-antigravity-subscription](https://github.com/soyelmismo/hermes-antigravity-subscription) (MIT) at `b7ab470`. This package adds the auth handler, SOUL delivery, native-tool bridging and the npx installer.

MIT © Neeraj Kumar Singh Beshane
