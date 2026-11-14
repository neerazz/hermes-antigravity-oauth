# Agent setup runbook

For an AI agent with a terminal (Hermes, Claude Code, Codex, Cursor). Follow the steps in order, and do not start a step until the previous step's check passes.

Goal: Hermes Agent answers one real test message through the `antigravity-oauth` provider on the user's Google Antigravity plan. Any agent can run this; the thing being configured is always Hermes.

## Rules for the agent

1. Never read, print, copy or upload the agy OAuth token, the keychain entry, or anything under `~/.gemini`. The plugin never needs them.
2. Stop and ask the user wherever a step says **ASK** or **HUMAN**. Sign-in needs their browser and their Google account.
3. At most two smoke-test runs may reach Google: the first run, plus one retry only where the failure table says so. Do not loop, because repeated failed starts can lock the account for an hour or more (issue #21).
4. Give every `hermes chat` command a terminal timeout of at least 300 seconds. A failing request can take 2 to 3 minutes to return its error.
5. Finish with the report at the bottom, using real command output.

## Step 1: Check prerequisites

```sh
hermes --version   # need 0.21.0 or newer
agy --version      # Antigravity CLI; /usage needs 1.1.11 or newer
node --version     # 18 or newer; only needed for the npx route
```

- `hermes` missing: stop. Tell the user to install Hermes Agent first: <https://hermes-agent.nousresearch.com/docs>.
- `hermes` installed but erroring (for example `run hermes pm repair`): check whether `HERMES_HOME` is set to a directory the user does not normally use. If so, unset it and retry. Otherwise ask the user before running `hermes pm repair`.
- `agy` missing: **ASK** the user before installing anything. With their OK, use Step 2 with `--yes`, which downloads Google's official installer from `https://antigravity.google/cli`. If agy installs but the command is still not found, open a new shell or add `~/.local/bin` to `PATH`.
- `node` missing: use the Hermes route in Step 2.

## Step 2: Install and enable the plugin

```sh
npx -y hermes-antigravity-oauth install --no-login          # add --yes only if the user approved installing agy
# without Node:
hermes plugins install https://github.com/neerazz/hermes-antigravity-oauth --enable
```

Both routes install from the GitHub `main` branch. Add `--force` to update an existing copy. Ignore `Skipped Node deps`; the plugin is Python.

Check: `hermes plugins list --plain --no-bundled` prints a line with `enabled` and `antigravity-oauth`. Skip plain `hermes plugins list`; its table is huge.

## Step 3: Sign in (HUMAN)

```sh
hermes auth status antigravity-oauth
```

If it prints `antigravity-oauth: logged in`, skip to Step 4. Otherwise ask the user to run this in their own terminal, not yours:

```sh
hermes auth add antigravity-oauth
```

It opens Google sign-in in the browser. Over SSH it prints a URL and the user pastes the code back within about 60 seconds.

If the user is not at the machine's terminal, use the helper that ships with the plugin. Start it in the background, send the user the printed `LOGIN_URL`, and write the code they send back into the file:

```sh
CODE_FILE="$(mktemp)"; chmod 600 "$CODE_FILE"   # private; the helper waits until it is non-empty
python3 "${HERMES_HOME:-$HOME/.hermes}/plugins/antigravity-oauth/scripts/remote-login.py" --code-file "$CODE_FILE" &
# after the user replies:  printf '%s' '<code>' > "$CODE_FILE"
```

The helper exits 0 once `agy models` confirms the session. The 60-second limit still applies, so tell the user to approve right away.

Check: `hermes auth status antigravity-oauth` prints `antigravity-oauth: logged in` and a `detail: <N> models available` line.

## Step 4: Pick a model (ASK)

```sh
hermes usage --provider antigravity-oauth   # quota left per model group
agy models                                  # exact model IDs
```

Every model sits in a quota group, such as Gemini or Claude/GPT. If either window of a group (5-hour or 7-day) shows `0% remaining`, its models fail, so offer only models from groups with quota left.

Show the user the exact IDs from the left column of `agy models`, for example `gemini-3.8-flash-medium`, and ask which one to use. The list depends on the plan and changes, so never use an ID from memory. If the user says "you pick", take a `-medium` model from the group with the most quota left and say which one.

## Step 5: Make sure Hermes has a provider

A fresh Hermes install has no default provider and refuses to run, even with `--provider`. Check the `model:` block of its config:

```sh
grep -A3 '^model:' "$(hermes config path)"
```

If there is no `provider:` line under `model:`, set this plugin as the default:

```sh
hermes config set model.provider antigravity-oauth
hermes config set model.default '<exact-model-id>'
```

If a different provider is already there, leave it. The flags in Step 6 are enough.

## Step 6: Smoke test

Run it from an empty temp directory so no project files get loaded, with a terminal timeout of at least 300 seconds:

```sh
cd "$(mktemp -d)"
hermes chat --oneshot -Q --provider antigravity-oauth -m '<exact-model-id>' -q 'Reply with exactly: OK'
```

Pass: the output starts with `OK`, usually in under a minute.

## When the smoke test fails

If the error mentions quota, billing or `RESOURCE_EXHAUSTED`, run `hermes usage --provider antigravity-oauth` before picking a row. Ignore Hermes' "add credits or update billing" advice; Antigravity plans have no credits.

| Output contains | What it means | What to do |
|---|---|---|
| `Unknown provider 'antigravity-oauth'` | Hermes did not load the plugin | `hermes plugins enable antigravity-oauth`, check that `HERMES_HOME` points at the same Hermes the user runs, then retry once |
| `Hermes isn't configured yet` | No default provider | Step 5. This run never reached Google, so it does not count toward the two runs |
| `not logged in` or an auth error | agy session missing or expired | Step 3 |
| `Individual quota reached ... Resets in <time>` | Real plan quota for that model group is used up | Tell the user the reset time, and offer a model from a group that `hermes usage` shows with quota left |
| `RESOURCE_EXHAUSTED (code 429): Resource has been exhausted (e.g. check quota)` after 2 to 3 minutes, while `hermes usage` shows quota left for the model's group | Google rejects the request on its side. Known open issue [#27](https://github.com/neerazz/hermes-antigravity-oauth/issues/27) | Retry once with `ANTIGRAVITY_WORKSPACE_RULES=0` in the environment. It worked for the reporter on agy 1.5.9, not for us on 1.5.8. If it fails again, stop and report |
| `not currently available in your location` | Per-account throttle on new agy sessions, not a real location block ([#21](https://github.com/neerazz/hermes-antigravity-oauth/issues/21)) | Stop retrying. It clears after 1 to 2 hours without new attempts |
| `worker process exited with return code 1` next to `attempted native tool invocation` | agy 1.4 or newer used a built-in file tool the plugin cannot bridge yet ([#19](https://github.com/neerazz/hermes-antigravity-oauth/issues/19)) | Plain chat still works. Report it; there is no user-side fix |

Anything else: run `hermes doctor`, and include the output of `grep -E 'ERROR|WARNING' "${HERMES_HOME:-$HOME/.hermes}/logs/agent.log" | tail -30` in the report, with any token-like strings removed.

## Report to the user

```
hermes-antigravity-oauth setup: PASS | FAIL at step <n>
Hermes <version>, agy <version>, plugin enabled: yes|no, plugin commit: <git -C "${HERMES_HOME:-$HOME/.hermes}/plugins/antigravity-oauth" rev-parse --short HEAD>
Signed in: yes|no (<model count> models)
Model: <exact id>
Smoke test: <first line of output>
Default provider changed: yes|no
Next: hermes chat --provider antigravity-oauth -m '<exact id>'
Open problem (if any): <error line> -> <table row or issue link>
```
