"""The D2 deploy diff (live dashboard plan, DB-T12 test 4; written with DB-T10's D2 proofs): from the commit
trader-dev runs (`DEPLOYED`) to this checkout (`BUILT` = HEAD), the build touches nothing a trading decision
depends on.

`DEPLOYED` comes from the environment variable `TRADER_D2_BASE` when it is set (the LIVE deploy step sets it
to the commit `/api/meta` reports), else from git: the parent of the oldest commit on HEAD's first-parent
history whose subject starts with `DB-T1:`, so the gate proves the dashboard commits alone touch no
decision-path file. No commit id is written in this file. `DEPLOYED` must be an ancestor of `BUILT`; with
`TRADER_D2_BASE` set the test never skips (a bad value fails), without it it skips only when git is
unavailable or no `DB-T1:` commit exists.

From `DEPLOYED` to `BUILT` (Phase 6 D2 rules 1-3):
- no file on the live decision path changed (`settings_store.py` included);
- in `tests/engine|strategies|broker|market|jobs|integration` files were only added (no assertion changed);
- `tests/replay/golden/` is unchanged;
- no job line of `docker/crontab` changed.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.decisions.test_static import DECISION_PATH

APP = Path(__file__).resolve().parents[2]
ENV = "TRADER_D2_BASE"
FIRST_TASK = "^DB-T1:"
PROTECTED_TESTS = ("engine", "strategies", "broker", "market", "jobs", "integration")


class GitError(Exception):
    pass


def git(*args: str) -> str:
    """One read-only git command in this checkout; its stdout, stripped."""
    exe = shutil.which("git")
    if exe is None:
        raise GitError("git is not installed")
    done = subprocess.run(  # noqa: S603 (fixed argv, no shell)
        [exe, *args], cwd=APP, capture_output=True, text=True, timeout=60, check=False
    )
    if done.returncode != 0:
        raise GitError(f"git {' '.join(args)}: exit {done.returncode}: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def repo_root() -> Path:
    return Path(git("rev-parse", "--show-toplevel"))


def app_prefix() -> str:
    """This app's path inside the repository (e.g. `Trader/app`)."""
    return APP.resolve().relative_to(repo_root().resolve()).as_posix()


def commit(ref: str) -> str:
    return git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def resolve_deployed(env_value: str | None, built: str = "HEAD") -> str:
    """`DEPLOYED` as a full commit id: the env value if given (it must name a commit that is an ancestor of
    `built`, else the test FAILS), else the parent of the first `DB-T1:` commit (skips if there is none)."""
    if env_value is not None:
        value = env_value.strip()
        try:
            deployed = commit(value)
        except GitError as exc:
            pytest.fail(f"{ENV}={value!r} is not a commit in this repository ({exc})")
    else:
        try:
            firsts = git("log", "--first-parent", "--format=%H", "--grep", FIRST_TASK, built).splitlines()
        except GitError as exc:
            pytest.skip(f"git unavailable: {exc}")
        if not firsts:
            pytest.skip(f"no {FIRST_TASK} commit on the first-parent history of {built}")
        deployed = commit(f"{firsts[-1]}^")
    try:
        git("merge-base", "--is-ancestor", deployed, commit(built))
    except GitError:
        pytest.fail(f"DEPLOYED {deployed} is not an ancestor of BUILT {built}: look at this deploy by hand")
    return deployed


@pytest.fixture(scope="module")
def span() -> tuple[str, str]:
    env_value = os.environ.get(ENV)
    if env_value is None:
        try:
            git("--version")
        except GitError as exc:
            pytest.skip(f"git unavailable: {exc}")
    return resolve_deployed(env_value), commit("HEAD")


def name_status(deployed: str, built: str, *paths: str) -> list[tuple[str, str]]:
    out = git("diff", "--no-renames", "--name-status", deployed, built, "--", *paths)
    return [(line.split("\t", 1)[0], line.split("\t", 1)[1]) for line in out.splitlines() if line]


# --- the base -----------------------------------------------------------------------------------------------


def test_the_base_is_resolved_from_git_or_the_environment(
    span: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    deployed, built = span
    assert len(deployed) == 40 and deployed != built
    git("merge-base", "--is-ancestor", deployed, built)
    # the environment wins, and an ancestor is accepted as is
    assert resolve_deployed(f"  {deployed[:12]} ", "HEAD") == deployed


def test_a_garbage_or_non_ancestor_base_fails_and_never_skips(span: tuple[str, str]) -> None:
    deployed, built = span
    with pytest.raises(pytest.fail.Exception, match="is not a commit"):
        resolve_deployed("not-a-commit-zzz")
    with pytest.raises(pytest.fail.Exception, match="is not a commit"):
        resolve_deployed("")
    # a base that is newer than the build (here HEAD against the older base commit) is no ancestor: a deploy
    # from an unrelated or newer commit must be looked at by hand
    with pytest.raises(pytest.fail.Exception, match="not an ancestor"):
        resolve_deployed(built, deployed)


# --- D2 rules 1-3 -------------------------------------------------------------------------------------------


def test_no_decision_path_file_changed(span: tuple[str, str]) -> None:
    deployed, built = span
    prefix = app_prefix()
    paths = [f"{prefix}/trader/{rel}" for rel in DECISION_PATH]
    assert f"{prefix}/trader/settings_store.py" in paths
    assert name_status(deployed, built, *paths) == []


def test_protected_test_folders_only_gained_files(span: tuple[str, str]) -> None:
    deployed, built = span
    prefix = app_prefix()
    changes = name_status(deployed, built, *(f"{prefix}/tests/{d}" for d in PROTECTED_TESTS))
    assert [c for c in changes if c[0] != "A"] == []


def test_the_golden_replay_files_are_unchanged(span: tuple[str, str]) -> None:
    deployed, built = span
    assert name_status(deployed, built, f"{app_prefix()}/tests/replay/golden") == []


def crontab_job_changes(diff: str) -> list[str]:
    """The added or removed lines of a crontab diff that are jobs (not blank, not a comment)."""
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
            continue
        body = line[1:].strip()
        if body and not body.startswith("#"):
            out.append(line)
    return out


def test_no_crontab_job_line_changed(span: tuple[str, str]) -> None:
    deployed, built = span
    crontab = f"{app_prefix().rsplit('/', 1)[0]}/docker/crontab"
    assert commit(built) and (repo_root() / crontab).exists(), crontab
    diff = git("diff", deployed, built, "--", crontab)
    assert crontab_job_changes(diff) == []


def test_the_crontab_filter_sees_a_job_line() -> None:
    diff = (
        "--- a/crontab\n+++ b/crontab\n@@ -1 +1 @@\n"
        "-# old comment\n+# new comment\n+\n-35 9 * * 1-5 trader x\n"
    )
    assert crontab_job_changes(diff) == ["-35 9 * * 1-5 trader x"]
