#!/usr/bin/env python3
"""Commit a source-only bump; bind its push to Git's actual advertisement."""

import os
from pathlib import Path
import re
import subprocess
import sys

from release import git, guard_checkout
from source_version import SOURCE_PATHS, bump, read_version


HOOK = Path(".github/hooks/pre-push")
TRAILER = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "github-actions[bot]@users.noreply.github.com"


def require_hook():
    if not HOOK.is_file() or HOOK.is_symlink() or not os.access(HOOK, os.X_OK):
        raise ValueError("The mandatory executable pre-push guard is missing")
    if HOOK.read_bytes() != git("show", f"HEAD:{HOOK.as_posix()}"):
        raise ValueError("The mandatory pre-push guard differs from immutable HEAD")


def advertised(ref):
    raw = git("ls-remote", "--refs", "origin", ref).decode()
    match = re.fullmatch(r"([0-9a-f]{40})\t" + re.escape(ref) + r"\n", raw)
    if not match:
        raise ValueError("Selected branch is missing or advertisement is malformed")
    return match[1]


def check_new_commit(old, new):
    if not re.fullmatch(r"[0-9a-f]{40}", old) or not re.fullmatch(r"[0-9a-f]{40}", new):
        raise ValueError("Guard requires exact old and new SHA values")
    if new == old or git("rev-parse", "HEAD").decode().strip() != new:
        raise ValueError("Guard requires a new literal HEAD")
    if git("rev-list", "--parents", "-n", "1", new).decode().split() != [new, old]:
        raise ValueError("Bump must have exactly the selected source as its sole parent")
    paths = {os.fsdecode(p) for p in git("diff-tree", "--no-commit-id", "--name-only",
                                       "-r", "-z", old, new).split(b"\0") if p}
    if not paths or not paths <= {str(p) for p in SOURCE_PATHS}:
        raise ValueError("Bump must have a nonempty source-only diff")
    message = git("show", "-s", "--format=%B", new).decode().rstrip()
    if not message.endswith(TRAILER):
        raise ValueError("Bump commit must end with the required coauthor trailer")
    if git("show", "-s", "--format=%an%n%ae", new).decode().splitlines() != [
            BOT_NAME, BOT_EMAIL]:
        raise ValueError("Bump must use the explicit Actions bot identity")
    previous_sha = os.environ.get("GITHUB_SHA")
    os.environ["GITHUB_SHA"] = new
    try:
        guard_checkout()
    finally:
        if previous_sha is None:
            del os.environ["GITHUB_SHA"]
        else:
            os.environ["GITHUB_SHA"] = previous_sha


def pre_push():
    require_hook()
    old = os.environ.get("BUMP_EXPECTED_OLD", "")
    new = os.environ.get("BUMP_EXPECTED_NEW", "")
    ref = os.environ.get("BUMP_EXPECTED_REF", "")
    if not ref.startswith("refs/heads/"):
        raise ValueError("Guard requires the exact selected branch ref")
    updates = [line.split() for line in sys.stdin.read().splitlines()]
    if updates != [["HEAD", new, ref, old]]:
        raise ValueError("Push advertisement must contain exactly HEAD/new/ref/old once")
    check_new_commit(old, new)


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] != "--pre-push":
            raise ValueError("Unexpected bump arguments")
        pre_push()
        return
    require_hook()
    if os.environ.get("GITHUB_REF_TYPE") != "branch":
        raise ValueError("Bump requires a selected branch, not a tag")
    ref = os.environ.get("GITHUB_REF", "")
    if not ref.startswith("refs/heads/"):
        raise ValueError("Bump requires an exact branch ref")
    git("check-ref-format", ref)
    mode = os.environ.get("BUMP_MODE", "")
    if mode not in {"patch", "minor", "major"}:
        raise ValueError("BUMP_MODE must be patch, minor or major")
    old, before = guard_checkout()
    if advertised(ref) != old:
        raise ValueError("Selected branch has moved since the immutable event")
    version = bump(mode)
    if version == before or read_version() != version:
        raise ValueError("Source bump did not produce the requested new version")
    git("add", "--", *(str(path) for path in SOURCE_PATHS))
    git("-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
        "commit", "-m", f"chore: bump version to {version}\n\n{TRAILER}")
    new = git("rev-parse", "HEAD").decode().strip()
    check_new_commit(old, new)
    if advertised(ref) != old:
        raise ValueError("Selected branch moved before push; refusing publication")
    require_hook()
    environment = dict(os.environ, BUMP_EXPECTED_OLD=old, BUMP_EXPECTED_NEW=new,
                       BUMP_EXPECTED_REF=ref)
    subprocess.run(
        ["git", "-c", "push.followTags=false", "-c",
         "core.hooksPath=.github/hooks", "push", "--no-follow-tags",
         "origin", f"HEAD:{ref}"],
        env=environment, check=True,
    )
    if advertised(ref) != new:
        raise ValueError("Published branch does not match the generated commit")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(f"### Source bumped to {version}\n\n"
                         f"Committed `{new}` on `{ref}`. No tag or release created.\n")
    print(f"Source bumped {before} -> {version}: {new}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Source bump stopped: {error}")
