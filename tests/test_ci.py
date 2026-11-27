"""The release workflow's alias script (.github/scripts/update-image-aliases.sh),
run with stub ``gh`` and ``docker`` commands."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / ".github/scripts/update-image-aliases.sh"
IMAGE = "ghcr.io/its-marvin/lan-party-seat-selection"

pytestmark = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("jq")), reason="needs bash and jq")

GH = """#!/usr/bin/env bash
case "$*" in
  *releases/latest*) [ -n "$FAKE_LATEST" ] || { echo '{"message":"Not Found"}' >&2; exit 1; }
                     echo "$FAKE_LATEST" ;;
  *) echo "$FAKE_RELEASES" ;;
esac
"""
DOCKER = """#!/usr/bin/env bash
if [ "$3" = inspect ]; then
  for v in $FAKE_PUBLISHED; do [ "$4" = "$IMAGE_NAME:$v" ] && exit 0; done
  exit 1
fi
echo "${@: -2:1} ${@: -1}" >> "$CREATED"     # ... --tag <alias> <source>
"""


def release(tag, draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


def run_aliases(tmp_path, releases, latest, published):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gh", GH), ("docker", DOCKER)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    created = tmp_path / "created"
    created.touch()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "its-Marvin/x",
           "FAKE_RELEASES": json.dumps(releases), "FAKE_LATEST": latest or "",
           "FAKE_PUBLISHED": " ".join(published), "IMAGE_NAME": IMAGE, "CREATED": str(created)}
    result = subprocess.run(["bash", str(SCRIPT), IMAGE], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    moves = {}
    for line in created.read_text().splitlines():
        alias, source = line.split()
        moves[alias.rsplit(":", 1)[1]] = source.rsplit(":", 1)[1]
    return moves


def test_a_fix_for_an_older_line_moves_no_alias_backwards(tmp_path):
    # v1.2.0 is out; then v1.1.5 fixes the 1.1 line
    releases = [release("v1.1.5"), release("v1.2.0"), release("v1.1.0")]
    moves = run_aliases(tmp_path, releases, "v1.2.0", ["1.1.0", "1.1.5", "1.2.0"])
    assert moves == {"1": "1.2.0", "1.1": "1.1.5", "1.2": "1.2.0", "latest": "1.2.0"}


def test_every_line_is_reconciled_when_another_run_was_replaced(tmp_path):
    """GitHub cancels a pending job when a newer one joins the concurrency group: the
    queued run for v1.1.5 may never happen, so the v2.0.0 run must fix :1.1 as well."""
    releases = [release("v2.0.0"), release("v1.1.5"), release("v1.2.0"), release("v1.1.0")]
    moves = run_aliases(tmp_path, releases, "v2.0.0", ["1.1.0", "1.1.5", "1.2.0", "2.0.0"])
    assert moves == {"1": "1.2.0", "1.1": "1.1.5", "1.2": "1.2.0",
                     "2": "2.0.0", "2.0": "2.0.0", "latest": "2.0.0"}


def test_drafts_and_pre_releases_are_ignored(tmp_path):
    releases = [release("v2.0.0-rc.1", prerelease=True), release("v1.3.0", draft=True), release("v1.2.0")]
    moves = run_aliases(tmp_path, releases, "v1.2.0", ["2.0.0-rc.1", "1.2.0"])
    # no stable 2.x yet (and the 1.3.0 draft doesn't count): only the 1.2.0 aliases move
    assert moves == {"1": "1.2.0", "1.2": "1.2.0", "latest": "1.2.0"}


def test_an_unpublished_newer_image_is_left_to_its_own_run(tmp_path):
    releases = [release("v1.3.0"), release("v1.2.0")]
    moves = run_aliases(tmp_path, releases, "v1.3.0", ["1.2.0"])   # 1.3.0 still building
    assert moves == {"1.2": "1.2.0"}


def test_only_pre_releases_so_far_is_not_an_error(tmp_path):
    moves = run_aliases(tmp_path, [release("v1.0.0-beta.1", prerelease=True)], None, ["1.0.0-beta.1"])
    assert moves == {}


def test_minor_lines_are_matched_exactly(tmp_path):
    releases = [release("v1.10.0"), release("v1.1.0")]
    moves = run_aliases(tmp_path, releases, "v1.10.0", ["1.1.0", "1.10.0"])
    assert moves == {"1": "1.10.0", "1.1": "1.1.0", "1.10": "1.10.0", "latest": "1.10.0"}
