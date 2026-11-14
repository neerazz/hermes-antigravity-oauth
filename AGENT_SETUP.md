# Agent setup runbook

This file is written for an AI agent with a terminal (Hermes, Claude Code, Codex, Cursor and similar). A person pastes the prompt from the README, and the agent follows the steps below in order. Each step has a check. Do not move to the next step until its check passes.

Goal: Hermes Agent runs on the user's Google Antigravity plan through the `antigravity-oauth` provider, and one real test reply comes back.

## Rules for the agent

1. Never read, print, copy or upload the agy OAuth token, the keychain entry, or anything under `~/.gemini`. The plugin does not need you to touch them.
2. Stop and ask the user wherever a step says **ASK** or **HUMAN**. Sign-in needs their browser and their Google account.
3. Run the smoke test once. If it fails, match the error in the table at the end and report it. Do not loop retries, because repeated failed starts can lock the account for an hour or more (issue #21).
4. Finish with the report at the bottom, using real command output.

## Step 1: Check prerequisites

```sh
hermes --version   # need 0.21.0 or newer
agy --version      # Antigravity CLI; /usage needs 1.1.11 or newer
node --version     # 18 or newer; only needed for the npx route
```

- `hermes` missing: stop. Tell the user to install Hermes Agent first: <https://hermes-agent.nousresearch.com/docs>.
- `agy` missing: **ASK** the user before installing anything. With their OK, use Step 2 with `--yes`, which downloads Google's official installer from `https://antigravity.google/cli`. If agy installs but the command is still not found, open a new shell or add `~/.local/bin` to `PATH`.
- `node` missing: use the Hermes route in Step 2.

## Step 2: Install and enable the plugin

```sh
npx -y hermes-antigravity-oauth install --no-login          # add --yes only if the user approved installing agy
# without Node:
hermes plugins install https://github.com/neerazz/hermes-antigravity-oauth --enable
```

Use `--force` to update a copy that is already installed. Hermes may print `Skipped Node deps`; that is harmless, because the plugin is Python and has no Node dependencies.

Check: `hermes plugins list` shows `antigravity-oauth` as `enabled`.

## Step 3: Sign in (HUMAN)

```sh
hermes auth status antigravity-oauth
```

If it says `logged in`, skip to Step 4. Otherwise ask the user to run this in their own terminal, not yours:

```sh
hermes auth add antigravity-oauth
```

It opens Google sign-in in the browser. Over SSH it prints a URL, and the user pastes the code back. agy gives about 60 seconds from URL to code.

Headless host where the user is not at the terminal: the plugin ships a helper. Run it in the background with `python3 "${HERMES_HOME:-$HOME/.hermes}/plugins/antigravity-oauth/scripts/remote-login.py" --code-file /tmp/agy-code.txt`, send the user the printed `LOGIN_URL`, and write the code they send back to `/tmp/agy-code.txt`. The script exits 0 once `agy models` confirms the session. The 60-second window still applies, so the user must act right away.

Check: `hermes auth status antigravity-oauth` prints `antigravity-oauth: logged in` and a `detail: <N> models available` line.

## Step 4: Pick a model (ASK)

```sh
hermes usage --provider antigravity-oauth   # quota left per model group
agy models                                  # exact model IDs
```

Models in a group at `0% remaining` fail until that group resets, so offer only models from groups with quota left. Show the user the IDs and ask which one to use. Use the exact ID from the left column, for example `gemini-3.8-flash-medium`. The list depends on their plan and changes over time, so never use an ID from memory. If the user says "you pick", choose a `-medium` variant from the list and say which one you chose.

## Step 5: Smoke test

Run it from an empty temporary directory so no project files get pulled in:

```sh
cd "$(mktemp -d)"
hermes chat --oneshot -Q --provider antigravity-oauth -m '<exact-model-id>' -q 'Reply with exactly: OK'
```

Pass: the output starts with `OK`, usually within a minute.

If the output says `Hermes isn't configured yet`, Hermes has no default provider. This happens on a fresh Hermes install. Set this plugin as the default, then rerun the smoke test once:

```sh
hermes config set model.provider antigravity-oauth
hermes config set model.default '<exact-model-id>'
```

If the user already has a different default provider, do not change it. The `--provider` and `-m` flags are enough.

## When the smoke test fails

| Output contains | What it means | What to do |
|---|---|---|
| `Unknown provider 'antigravity-oauth'` | Hermes did not load the plugin | `hermes plugins enable antigravity-oauth`, check that `HERMES_HOME` points at the same Hermes the user runs, then retry once |
| `Hermes isn't configured yet` | Fresh Hermes with no default provider | The `config set` commands in Step 5 |
| `not logged in` or an auth error | agy session missing or expired | Step 3 |
| `Individual quota reached ... Resets in <time>` | Real plan quota for that model group is used up | Tell the user the reset time. Check groups with `hermes usage --provider antigravity-oauth`, and offer a model from a group with quota left |
| `RESOURCE_EXHAUSTED ... Resource has been exhausted (e.g. check quota)` after 2 to 3 minutes, while `hermes usage` still shows quota | Google rejects the request on its side. Known open issue [#27](https://github.com/neerazz/hermes-antigravity-oauth/issues/27) | Retry once with `ANTIGRAVITY_WORKSPACE_RULES=0` set in the environment. That fixed it for the reporter on agy 1.5.9 but not in our test on agy 1.5.8. If it still fails, stop and report; do not loop |
| `not currently available in your location` | Per-account throttle on new agy sessions, not a real location block ([#21](https://github.com/neerazz/hermes-antigravity-oauth/issues/21)) | Stop retrying. It clears after 1 to 2 hours without new attempts |
| `worker process exited with return code 1` next to `attempted native tool invocation` | agy 1.4 or newer used a built-in file tool the plugin cannot bridge yet ([#19](https://github.com/neerazz/hermes-antigravity-oauth/issues/19)) | Plain chat still works. Report it; there is no user-side fix |

Anything else: run `hermes doctor`, and include the last 30 lines of `$HERMES_HOME/logs/agent.log` (default `~/.hermes/logs/agent.log`) in the report, with any token-like strings removed.

## Report to the user

```
hermes-antigravity-oauth setup: PASS | FAIL at step <n>
Hermes <version>, agy <version>, plugin enabled: yes|no, plugin commit: <git -C "${HERMES_HOME:-$HOME/.hermes}/plugins/antigravity-oauth" rev-parse --short HEAD>
Signed in: yes|no (<model count> models)
Model: <exact id>
Smoke test: <first line of output>
Default provider changed: yes|no
Next: hermes --provider antigravity-oauth -m '<exact id>'
Open problem (if any): <error line> -> <table row or issue link>
```
