"""Release policy and version selection without registry writes or credentials."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "local,npm_version,github_version,expected",
    [
        ("0.1.0", "0.1.4", "0.1.6", "0.1.7"),
        ("0.1.0", "0.1.7", "0.1.4", "0.1.8"),
        ("1.0.0", "0.1.4", "0.1.6", "1.0.0"),
        ("0.1.0", "E404", "E404", "0.1.0"),
        ("0.1.0", "0.1.4", None, "0.1.5"),
    ],
)
def test_next_version_checks_selected_registries(tmp_path, local, npm_version, github_version, expected):
    result, calls = run_version(tmp_path, local, npm_version, github_version)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
    assert json.loads((tmp_path / "package.json").read_text())["version"] == expected
    assert (tmp_path / "plugin.yaml").read_text() == f"name: antigravity-oauth\nversion: {expected}\n"
    assert "https://registry.npmjs.org" in calls
    assert ("https://npm.pkg.github.com" in calls) == (github_version is not None)


@pytest.mark.parametrize("failure", ["E401", "E500", "ETIMEDOUT", "not-a-version"])
def test_github_lookup_failure_never_becomes_a_first_release(tmp_path, failure):
    result, _ = run_version(tmp_path, "0.1.0", "0.1.4", failure)
    assert result.returncode != 0
    assert json.loads((tmp_path / "package.json").read_text())["version"] == "0.1.0"
    assert "version: 0.1.0" in (tmp_path / "plugin.yaml").read_text()


def run_version(tmp_path, local, npm_version, github_version, *, workflow_step=False):
    (tmp_path / "package.json").write_text(json.dumps({"name": "hermes-antigravity-oauth", "version": local}))
    (tmp_path / "plugin.yaml").write_text(f"name: antigravity-oauth\nversion: {local}\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    npm = bindir / "npm"
    npm.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "with open(os.environ['CALL_LOG'], 'a') as f: f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "key = 'GITHUB_VERSION' if sys.argv[2].startswith('@') else 'NPM_VERSION'\n"
        "value = os.environ[key]\n"
        "if value.startswith('E'):\n"
        "    print(value, file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print(value)\n"
    )
    npm.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "CALL_LOG": str(tmp_path / "calls"),
        "NPM_VERSION": npm_version,
    }
    if github_version is not None:
        env.update(GITHUB_PACKAGE_NAME="@neerazz/hermes-antigravity-oauth", GITHUB_VERSION=github_version)
    node = shutil.which("node")
    assert node, "Node is required for release tests"
    command = [node, str(ROOT / "scripts/next-version.js")]
    if workflow_step:
        (tmp_path / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/next-version.js", tmp_path / "scripts/next-version.js")
        workflow = YAML(typ="safe").load((ROOT / ".github/workflows/release.yml").read_text())
        step = next(s for s in workflow["jobs"]["publish"]["steps"] if s.get("name") == "Choose version")
        env.update(RUNNER_TEMP=str(tmp_path), GITHUB_REPOSITORY_OWNER="neerazz", GITHUB_OUTPUT=str(tmp_path / "outputs"))
        env["PATH"] += f":{Path(node).parent}"
        command = ["/bin/bash", "-e", "-c", step["run"]]
    result = subprocess.run(
        command, cwd=tmp_path,
        env=env, capture_output=True, text=True, timeout=10,
    )
    return result, (tmp_path / "calls").read_text()


def test_workflow_version_lookup_reserves_versions_across_both_registries(tmp_path):
    result, calls = run_version(tmp_path, "0.1.0", "0.1.13", "0.1.14", workflow_step=True)
    assert result.returncode == 0, result.stderr
    assert "version=0.1.15" in (tmp_path / "outputs").read_text()
    assert "https://registry.npmjs.org" in calls and "https://npm.pkg.github.com" in calls
    configs = list(tmp_path.glob("version-registry.*"))
    assert len(configs) == 1
    config = configs[0].read_text()
    assert "//npm.pkg.github.com/:_authToken=${GITHUB_PACKAGES_TOKEN}" in config
    assert "//registry.npmjs.org/:_authToken" not in config


def test_version_failure_stops_the_release_shell_step(tmp_path):
    workflow = YAML(typ="safe").load((ROOT / ".github/workflows/release.yml").read_text())
    step = next(s for s in workflow["jobs"]["publish"]["steps"] if s.get("name") == "Choose version")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "next-version.js").write_text("process.exit(19);\n")
    node = shutil.which("node")
    assert node
    output = tmp_path / "outputs"
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", step["run"]], cwd=tmp_path,
        env={"PATH": f"{Path(node).parent}:/usr/bin:/bin", "GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(tmp_path), "GITHUB_REPOSITORY_OWNER": "neerazz"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 19
    assert not output.exists(), "Do not publish after a failed version lookup"


def test_release_stays_manual_main_only_and_ci_gated():
    workflow = YAML(typ="safe").load((ROOT / ".github/workflows/release.yml").read_text())
    assert set(workflow["on"]) == {"workflow_dispatch"}
    registry = workflow["on"]["workflow_dispatch"]["inputs"]["registry"]
    assert registry["default"] == "npm"
    assert registry["options"] == ["npm", "github"]
    assert workflow["jobs"]["test"]["if"] == "github.ref == 'refs/heads/main'"
    job = workflow["jobs"]["publish"]
    assert job["needs"] == "test"
    assert "workflow_dispatch" in job["if"] and "refs/heads/main" in job["if"]
    steps = {step.get("name"): step for step in job["steps"] if step.get("name")}
    assert steps["Publish to npm"]["if"] == "inputs.registry != 'github'"
    assert steps["Tag and release"]["if"] == "inputs.registry != 'github'"
    assert steps["Publish to GitHub Packages"]["if"] == "inputs.registry == 'github'"
    assert job["permissions"]["packages"] == "write"
    assert steps["Publish to GitHub Packages"]["env"]["NODE_AUTH_TOKEN"] == "${{ github.token }}"
    assert steps["Choose version"]["env"]["GITHUB_PACKAGE_NAME"] == "@${{ github.repository_owner }}/hermes-antigravity-oauth"
    assert steps["Choose version"]["env"]["GITHUB_PACKAGES_TOKEN"] == "${{ github.token }}"
    assert "NODE_AUTH_TOKEN" not in steps["Choose version"]["env"], "Never send the GitHub token to npmjs.com"
    assert "//npm.pkg.github.com/:_authToken=${GITHUB_PACKAGES_TOKEN}" in steps["Choose version"]["run"]
    assert "https://npm.pkg.github.com" in steps["Publish to GitHub Packages"]["run"]
    assert "npm exec" in steps["Verify GitHub package download"]["run"]
    assert "hermes-antigravity-oauth --help" in steps["Verify GitHub package download"]["run"]
    package = json.loads((ROOT / "package.json").read_text())
    assert package["name"] == "hermes-antigravity-oauth", "Do not rename the existing npm package"
    assert package["repository"]["url"] == "git+https://github.com/neerazz/hermes-antigravity-oauth.git"


@pytest.mark.parametrize("bad_field", [None, "name", "repository", "gitHead", "description"])
def test_registry_verification_checks_identity_and_uses_a_clean_directory(tmp_path, bad_field):
    workflow = YAML(typ="safe").load((ROOT / ".github/workflows/release.yml").read_text())
    step = next(s for s in workflow["jobs"]["publish"]["steps"] if s.get("name") == "Verify GitHub package download")
    metadata = {
        "name": "@neerazz/hermes-antigravity-oauth", "version": "0.1.5",
        "repository": {"url": "git+https://github.com/neerazz/hermes-antigravity-oauth.git"},
        "gitHead": "a" * 40, "description": "Registry verification fixture",
        "dist": {"integrity": "sha512-test-fixture"}, "bin": {"hermes-antigravity-oauth": "bin/cli.js"},
    }
    if bad_field:
        metadata[bad_field] = {"url": "git+https://github.com/wrong/repo.git"} if bad_field == "repository" else ""
    fixture = tmp_path / "metadata.json"
    fixture.write_text(json.dumps(metadata))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    npm = bindir / "npm"
    npm.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "if sys.argv[1] == 'view': print(pathlib.Path(os.environ['METADATA']).read_text())\n"
        "elif sys.argv[1] == 'exec': pathlib.Path(os.environ['EXEC_CWD']).write_text(os.getcwd())\n"
        "else: sys.exit(44)\n"
    )
    npm.chmod(0o755)
    node = shutil.which("node")
    assert node
    env = {
        "PATH": f"{bindir}:{Path(node).parent}:/usr/bin:/bin", "METADATA": str(fixture),
        "EXEC_CWD": str(tmp_path / "exec-cwd"), "RUNNER_TEMP": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"), "GITHUB_REPOSITORY": "neerazz/hermes-antigravity-oauth",
        "GITHUB_PACKAGE_NAME": "@neerazz/hermes-antigravity-oauth", "VERSION": "0.1.5", "GITHUB_SHA": "a" * 40,
    }
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", step["run"]], cwd=tmp_path,
        env=env, capture_output=True, text=True, timeout=10,
    )
    if bad_field:
        assert result.returncode != 0
        assert not (tmp_path / "exec-cwd").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert Path((tmp_path / "exec-cwd").read_text()) != tmp_path
        assert "0.1.5" in (tmp_path / "summary").read_text()


@pytest.mark.parametrize("command,expected", [("help", 0), ("--help", 0), ("-h", 0), ("unknown-command", 2)])
def test_packaged_cli_help_is_safe_without_hermes_or_agy(tmp_path, command, expected):
    node = shutil.which("node")
    assert node
    result = subprocess.run(
        [node, str(ROOT / "bin/cli.js"), command],
        env={"PATH": "", "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == expected, result.stderr
    assert "hermes-antigravity-oauth" in result.stdout
    assert "Usage:" in result.stdout
    assert not list(tmp_path.iterdir()), "Help must not install or authenticate anything"


def test_ci_preserves_isolated_module_execution_without_runtime_installation():
    workflow = YAML(typ="safe").load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(s for s in workflow["jobs"]["checks"]["steps"] if s.get("name", "").startswith("Offline regression"))
    assert step["env"]["HERMES_DISABLE_LAZY_INSTALLS"] == "1"
    assert "for t in tests/test_*.py" in step["run"]
    assert 'python "$t"' in step["run"]
    assert "python -m pytest tests" not in step["run"], "Do not combine modules with process-global provider mocks"
    assert workflow["jobs"]["test"]["needs"] == "checks"
    assert workflow["jobs"]["test"]["if"] == "always()"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
