"""OPTSIM global rule 1: the stock engine is untouched (task plan §1, T1; acceptance item 7).

Compares the OPTSIM base with the working tree (so it also judges changes that are not committed yet):

- nothing changed, appeared or disappeared under the stock engine's paths (`PROTECTED`);
- no test file that existed at the base changed, except the three files task T16 may edit (`ALLOWED_TESTS`)
  and the contract pins (`PINNED_TESTS`): existing tests that pin a list OPTSIM is specified to extend
  (the migration head, the `ApiServices` field list, the router count and order, the crontab lines a
  replay waits out). Each has a rule saying exactly which lines may change;
- the golden replay files are unchanged;
- every job line of `docker/crontab` at the base is still there, unaltered (new lines are allowed).

The base is the environment variable `TRADER_OPTSIM_BASE`, else the parent of the first commit on HEAD's
first-parent history whose subject starts `OPTSIM-T1:`. The test skips only when neither exists (or there is
no git checkout at all, as inside the container image). Git runs with the repository root as its working
directory and repository-root paths, so a path that matches nothing can't make the diff look empty; one
assertion proves the helper sees a real change.
"""

import os
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

APP = "Trader/app/"
TESTS = f"{APP}tests/"
CRONTAB = "Trader/docker/crontab"
T1_SUBJECT = "OPTSIM-T1:"

# No edit, rename, delete or new file under these (repository-root paths; a trailing slash is a directory).
PROTECTED = (
    f"{APP}trader/engine/",
    f"{APP}trader/broker/",
    f"{APP}trader/strategies/",
    f"{APP}trader/replay/",
    f"{APP}trader/market/",
    f"{APP}trader/decisions/",
    f"{APP}trader/marks/",
    f"{APP}trader/notify/",
    f"{APP}trader/runtime.py",
    f"{APP}trader/worker.py",
    f"{APP}trader/settings_store.py",
    f"{APP}trader/api/forms.py",
    f"{TESTS}replay/golden/",
)
# Existing test files a task may edit (task plan §1 rule 2: all three belong to T16).
ALLOWED_TESTS = frozenset(
    {
        f"{TESTS}test_crontab.py",
        f"{TESTS}test_docker_files.py",
        f"{TESTS}api/test_web_client_contract.py",
    }
)
_REVISION = re.compile(r"00\d\d")


def _changes(diff: str) -> tuple[Counter[str], Counter[str]]:
    """(removed, added) lines of a `git diff -U0`, each without its comment and surrounding blanks; lines
    that were only a comment or blank are left out."""
    removed: Counter[str] = Counter()
    added: Counter[str] = Counter()
    for line in diff.splitlines():
        if line.startswith(("---", "+++")) or not line.startswith(("-", "+")):
            continue
        code = line[1:].split("#", 1)[0].strip()
        if code:
            (removed if line[0] == "-" else added)[code] += 1
    return removed, added


def only_revision_numbers_changed(diff: str) -> bool:
    """True when the diff changes nothing but migration revision numbers (and comments)."""
    removed, added = _changes(diff)
    mask = lambda lines: Counter({_REVISION.sub("00NN", k): v for k, v in lines.items()})  # noqa: E731
    return mask(removed) == mask(added) and all(_REVISION.search(line) for line in removed)


def only_added(*allowed: str) -> Callable[[str], bool]:
    """A rule: the diff removes nothing and adds only the given lines, each at most once."""

    def rule(diff: str) -> bool:
        removed, added = _changes(diff)
        return not removed and all(line in allowed and n == 1 for line, n in added.items())

    return rule


def only_these(*, removed: tuple[str, ...] = (), added: tuple[str, ...]) -> Callable[[str], bool]:
    """A rule: the diff removes and adds nothing but the given lines, each at most as often as it is
    listed (a line listed twice may be added twice)."""
    may_remove, may_add = Counter(removed), Counter(added)

    def rule(diff: str) -> bool:
        gone, new = _changes(diff)
        return all(n <= may_remove[line] for line, n in gone.items()) and all(
            n <= may_add[line] for line, n in new.items()
        )

    return rule


# Existing tests that pin a list OPTSIM is specified to extend, and what may change in each.
# - Migration 0011 moves the head (as every earlier migration did): only the revision number may change.
# - `ApiServices.options` is a new last field (task plan §1 rule 2): only that name may be added to the list.
# - T16 registers the options router before `stream` (task plan §3.11): the router count goes from 20 to 21
#   and "options" joins the pinned order (phase 4: one more `"options",` line; phase 5: the tail of tags).
PINNED_TESTS: dict[str, Callable[[str], bool]] = {
    f"{TESTS}db/test_migration_0007.py": only_revision_numbers_changed,
    f"{TESTS}db/test_migration_0008.py": only_revision_numbers_changed,
    f"{TESTS}db/test_migration_0009.py": only_revision_numbers_changed,
    f"{TESTS}db/test_migration_0010.py": only_revision_numbers_changed,
    f"{TESTS}api/test_system.py": only_revision_numbers_changed,
    f"{TESTS}test_phase4_contracts.py": only_these(
        removed=("len(ROUTERS) == 20",),
        added=('"options",', '"options",', "len(ROUTERS) == 21"),
    ),
    # `QUIET_TIMES` (trader/replay/data.py, a protected path) lists the stock cron lines a full-mode replay
    # waits out, and this test asserts it covers EVERY crontab line. The four `options-*` lines can't be
    # added to that table, so the test skips them: the options commands use their own Questrade client at
    # 2 requests per second (risk R9) and never push the limit a replay shares with the stock jobs.
    f"{TESTS}replay/test_data.py": only_these(
        added=('if line.split()[6].startswith("options-"):', "continue"),
    ),
    f"{TESTS}gauntlet/test_p5_t17_breaker.py": only_these(
        added=('if cmd[1].startswith("options-"):', "continue"),
    ),
    f"{TESTS}test_phase5_contracts.py": only_these(
        removed=(
            "assert len(ROUTERS) == 20",
            'assert tags[-6:] == ["replays", "reports", "decisions", "live", "control", "stream"]',
        ),
        added=(
            "assert len(ROUTERS) == 21",
            'assert tags[-7:] == ["replays", "reports", "decisions", "live", "control", "options", "stream"]',
        ),
    ),
}


def _repo_root() -> Path | None:
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=here, capture_output=True, text=True, check=False
        )
    except OSError:  # no git
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr.strip()}"
    return out.stdout


def _base(root: Path) -> str | None:
    given = os.environ.get("TRADER_OPTSIM_BASE", "").strip()
    if given:
        return _git(root, "rev-parse", "--verify", f"{given}^{{commit}}").strip()
    log = _git(root, "log", "--first-parent", "--format=%H%x09%s", "HEAD")
    first = None  # the log is newest first: the last match is the first OPTSIM-T1 commit
    for line in log.splitlines():
        commit, _, subject = line.partition("\t")
        if subject.startswith(T1_SUBJECT):
            first = commit
    return None if first is None else _git(root, "rev-parse", "--verify", f"{first}^").strip()


def changed_paths(root: Path, base: str) -> dict[str, str]:
    """Path -> status letter (A, M, D, ...) of everything that differs between `base` and the working tree,
    renames shown as a delete and an add; untracked files count as added."""
    out: dict[str, str] = {}
    for line in _git(root, "diff", "--no-renames", "--name-status", base, "--").splitlines():
        status, _, path = line.partition("\t")
        out[path] = status[:1]
    for path in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        out.setdefault(path, "A")
    return out


def _is_protected(path: str) -> bool:
    return any(path.startswith(p) if p.endswith("/") else path == p for p in PROTECTED)


def _job_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


@pytest.fixture(scope="module")
def checkout() -> tuple[Path, str]:
    root = _repo_root()
    if root is None:
        pytest.skip("not a git checkout")
    base = _base(root)
    if base is None:
        pytest.skip(f"no TRADER_OPTSIM_BASE and no {T1_SUBJECT} commit on HEAD's first-parent history")
    return root, base


def test_stock_untouched(checkout: tuple[Path, str]) -> None:
    root, base = checkout
    changed = changed_paths(root, base)

    # The helper sees real changes: OPTSIM's own package is new since the base.
    assert any(path.startswith(f"{APP}trader/options/") for path in changed), (
        "the diff from the OPTSIM base shows no change under Trader/app/trader/options: wrong base or paths"
    )

    assert sorted(p for p in changed if _is_protected(p)) == [], "the stock engine's files changed"

    existed = set(_git(root, "ls-tree", "-r", "--name-only", base, "--", TESTS.rstrip("/")).splitlines())
    assert existed, "the base has no test files: wrong base or paths"
    assert PINNED_TESTS.keys() <= existed and ALLOWED_TESTS <= existed, "an allow-list names a missing file"
    touched = sorted(p for p in changed if p in existed and p not in ALLOWED_TESTS | PINNED_TESTS.keys())
    assert touched == [], "existing test files changed"
    for path, rule in PINNED_TESTS.items():
        if path in changed:
            assert changed[path] == "M", f"{path} was removed"
            assert rule(_git(root, "diff", "-U0", base, "--", path)), f"{path}: more than its pin changed"

    before = _job_lines(_git(root, "show", f"{base}:{CRONTAB}"))
    now = _job_lines((root / CRONTAB).read_text(encoding="utf-8"))
    assert before, "the base crontab has no job lines: wrong path"
    missing = [line for line in before if line not in now]
    assert missing == [], "existing crontab job lines were removed or altered"
    kept = [line for line in now if line in before]
    assert kept == before, "existing crontab job lines changed order or were duplicated"


def test_the_checks_catch_what_they_should() -> None:
    """The helpers themselves: a protected path is recognised, and a pin edit is told from any other."""
    assert _is_protected(f"{APP}trader/engine/risk.py") and _is_protected(f"{APP}trader/runtime.py")
    assert _is_protected(f"{APP}trader/notify/messages.py") and _is_protected(f"{TESTS}replay/golden/a.json")
    assert not _is_protected(f"{APP}trader/options/runtime.py")  # the options runtime is OPTSIM's own
    assert not _is_protected(f"{APP}trader/runtime.py.bak") and not _is_protected(f"{APP}trader/jobs/x.py")

    pin = "\n".join(
        [
            "--- a/x.py",
            "+++ b/x.py",
            "@@ -1 +1 @@",
            '-    assert _version(engine) == "0010"  # FIX-DAY1',
            '+    assert _version(engine) == "0011"  # OPTSIM',
            "-def test_head_is_0010() -> None:",
            "+def test_head_is_0011() -> None:",
        ]
    )
    assert only_revision_numbers_changed(pin)
    assert only_revision_numbers_changed("")
    assert not only_revision_numbers_changed(pin + "\n+    assert True")
    assert not only_revision_numbers_changed(pin + '\n-    assert body["worker"]["ok"] is True')
    assert not only_revision_numbers_changed("-    assert rows == 3\n+    assert rows == 4")
    assert not only_revision_numbers_changed('-    x = "0010"\n+    y = "0011"')

    field = only_added('"options",')
    assert field('+        "options",  # OPTSIM-T1') and field("")
    assert not field('+        "options",\n+        "options",')
    assert not field('+        "options",\n+        "extra",')
    assert not field('-        "replays",\n+        "options",')

    routers = only_these(removed=("len(ROUTERS) == 20",), added=('"options",', '"options",', "x == 21"))
    assert routers("") and routers('+    "options",  # T1\n+    "options",  # T16\n-        len(ROUTERS) == 20')
    assert routers("-        len(ROUTERS) == 20\n+        x == 21")
    assert not routers('+    "options",\n+    "options",\n+    "options",')  # one more than listed
    assert not routers("-        len(ROUTERS) == 20\n-        len(ROUTERS) == 20")
    assert not routers('-    "stream",') and not routers('+    "extra",')

    assert _job_lines("# comment\n\n15 8 * * 1-5 trader x\n  0 9 * * 6 trader y  \n") == [
        "15 8 * * 1-5 trader x",
        "0 9 * * 6 trader y",
    ]
