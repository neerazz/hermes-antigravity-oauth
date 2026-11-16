# Contributing
Keep pull requests focused and include reproducible evidence. Report vulnerabilities through [SECURITY.md](SECURITY.md), never public issues.

## Local setup and offline checks
Use Python 3.11 and Node.js 18+ with Git and Bash. From a fresh clone, run:

```bash
git clone https://github.com/neerazz/hermes-antigravity-oauth.git
cd hermes-antigravity-oauth
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
git clone https://github.com/NousResearch/hermes-agent.git .hermes-agent
git -C .hermes-agent checkout 3b421578588243f920886e6fe79a6faa87ea026e
export PYTHONPATH="$PWD/.hermes-agent:$PWD"
export HERMES_DISABLE_LAZY_INSTALLS=1
for t in tests/test_*.py; do python "$t" || exit; done
node bin/cli.js --help
```

Setup downloads dependencies; the checks need no credentials or agy login. The environment flag prevents Hermes from installing/relaunching runtimes for isolated test homes; missing dependencies still fail normally. Run each test file in a separate Python process: combined pytest collection leaks registry monkeypatches between files. Keep `.venv/` and `.hermes-agent/` out of your PR.

## Before opening a PR
- Describe the problem, behavior change, and relevant issue. Include a regression test for code changes.
- Record exact commands, results, skipped checks, OS, Python, Node, and Hermes revision. Separate offline proof from live account checks.
- Check platform-specific behavior explicitly. Linux/macOS CI does not validate Windows; a platform badge is not test evidence.
- Follow the [PR template](.github/pull_request_template.md) and get passing CI before merge. This is contributor policy, not a claim that branch protection is configured.

Use [the architecture/source map](docs/architecture.md) to locate runtime boundaries. Preserve official agy OAuth ownership and Hermes approval behavior; workspace isolation is not a security sandbox. Never attach tokens, OAuth codes, private prompts, conversation databases, or unredacted account output.

## Live checks and releases
Live sign-in and model calls are optional, require account-owner consent, and may consume quota. Bare `node bin/cli.js` installs and starts login; `status` performs a live authentication check. Use `--help` for an offline smoke check.

Maintainers publish through the explicit release workflow on `main`, after CI. Choose `github` for GitHub Packages or `npm` (default) for npmjs.com; ordinary pushes and PRs must not publish. GitHub Packages uses `@neerazz/hermes-antigravity-oauth`; npm retains `hermes-antigravity-oauth`.

Both targets check both registries to prevent version reuse. A higher committed version ships unchanged; otherwise the workflow increments the patch of the higher registry version. No source bump is needed for a PR. See [release instructions](README.md#contribute-and-release).
