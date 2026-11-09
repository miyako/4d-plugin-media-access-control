#!/usr/bin/env python3
"""Immutable source checks and deliberately create-only GitHub publication."""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import quote

from source_version import read_version, validate_built


PRODUCT = "Media Access Control"
BUNDLE = f"test/Plugins/{PRODUCT}.bundle/Contents"
GENERATED = frozenset({
    f"{BUNDLE}/MacOS/{PRODUCT}",
    f"{BUNDLE}/Windows64/{PRODUCT}.4DX",
    f"{BUNDLE}/Info.plist",
    f"{BUNDLE}/Resources/English.lproj/InfoPlist.strings",
    f"{BUNDLE}/_CodeSignature/CodeResources",
})
REPOSITORY = "miyako/4d-plugin-media-access-control"


def run(*args, data=None):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


def git(*args):
    return run("git", *args)


def selected_sha():
    sha = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("GITHUB_SHA must be the immutable event commit")
    if git("rev-parse", "HEAD").decode().strip() != sha:
        raise ValueError("HEAD no longer matches the immutable event commit")
    return sha


def generated_extra(path, version):
    if path in GENERATED or path in {
        f"winbuild/{PRODUCT}.4DX", f"{PRODUCT}-{version}.zip",
        f"{PRODUCT}-{version}.dmg",
    }:
        return True
    if Path(path).suffix.lower() in {
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".m", ".mm",
        ".pbxproj", ".vcxproj", ".sln", ".yml", ".yaml", ".sh", ".py",
        ".ps1", ".strings",
    }:
        return False
    return any(path.startswith(prefix) for prefix in (
        f"build/{PRODUCT}.build/", f"test/Plugins/{PRODUCT}.build/",
        "build/XCBuildData/", "Objs/Release/",
    ))


def no_symlink_ancestors(path):
    for parent in Path(path).parents:
        if parent.is_symlink():
            raise ValueError(f"Symlink ancestor of checkout path: {path}")


def guard_checkout(final=False):
    """Compare raw committed, indexed and on-disk input bytes independently."""
    sha = selected_sha()
    tree = {}
    for record in git("ls-tree", "-rz", "--full-tree", sha).split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            if kind != "blob" or mode not in {"100644", "100755", "120000"}:
                raise ValueError("Unsupported tracked object in immutable checkout")
            path = os.fsdecode(path)
            if path in tree:
                raise ValueError("Duplicate committed path")
            tree[path] = (mode, oid)
    index = {}
    for record in git("ls-files", "--stage", "-z").split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode().split()
            path = os.fsdecode(path)
            if stage != "0" or path in index:
                raise ValueError("Duplicate or unmerged index")
            index[path] = (mode, oid)
    for record in git("ls-files", "-v", "-z").split(b"\0"):
        if record and record[:2] != b"H ":
            raise ValueError("Hidden index flags are forbidden")
    if set(index) != set(tree):
        raise ValueError("Tracked path set changed")
    for path, (mode, oid) in tree.items():
        no_symlink_ancestors(path)
        if final and path in GENERATED:
            continue
        if index[path] != (mode, oid):
            raise ValueError(f"Staged input changed: {path}")
        file = Path(path)
        metadata = file.lstat()
        if mode == "120000":
            actual = os.fsencode(os.readlink(file))
            valid_mode = stat.S_ISLNK(metadata.st_mode)
        else:
            valid_mode = stat.S_ISREG(metadata.st_mode)
            actual = file.read_bytes() if valid_mode else b""
            valid_mode = valid_mode and bool(metadata.st_mode & stat.S_IXUSR) == (
                mode == "100755")
        if not valid_mode or actual != git("cat-file", "blob", oid):
            raise ValueError(f"Worktree input changed: {path}")
    version = read_version()
    others = set()
    for args in [
        ("ls-files", "--others", "--exclude-standard", "-z"),
        ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
    ]:
        others.update(os.fsdecode(p) for p in git(*args).split(b"\0") if p)
    for path in others:
        no_symlink_ancestors(path)
        if not final or not generated_extra(path, version):
            raise ValueError(f"Unexpected untracked or ignored file: {path}")
        if Path(path).is_symlink() or not Path(path).is_file():
            raise ValueError(f"Invalid generated output: {path}")
    return sha, version


def repository():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo != REPOSITORY:
        raise ValueError(f"GITHUB_REPOSITORY must be {REPOSITORY}")
    if not os.environ.get("GH_TOKEN"):
        raise ValueError("Explicit GH_TOKEN authentication is required")
    return repo


def api(method, endpoint, payload=None, binary=None):
    args = ["gh", "api", "--method", method, endpoint,
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28"]
    if binary is not None:
        args += ["-H", "Content-Type: application/octet-stream", "--input", str(binary)]
        raw = run(*args)
    elif payload is not None:
        args += ["--input", "-"]
        raw = run(*args, data=json.dumps(payload).encode())
    else:
        raw = run(*args)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    result = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(result, (dict, list)):
        raise ValueError("Malformed GitHub response")
    return result


def integer(value):
    return type(value) is int and value > 0


def available(repo, tag):
    page = 1
    seen = set()
    while True:
        releases = api("GET", f"repos/{repo}/releases?per_page=100&page={page}")
        if not isinstance(releases, list) or len(releases) > 100:
            raise ValueError("Malformed releases page")
        for release in releases:
            if (not isinstance(release, dict) or not integer(release.get("id"))
                    or not isinstance(release.get("tag_name"), str)
                    or type(release.get("draft")) is not bool
                    or release["id"] in seen):
                raise ValueError("Malformed or repeated release entry")
            seen.add(release["id"])
            if release["tag_name"] == tag:
                raise ValueError(f"Release {tag} already exists (including drafts)")
        if not releases:
            break
        page += 1
    ref = f"refs/tags/{tag}"
    matches = api("GET", f"repos/{repo}/git/matching-refs/tags/{quote(tag, safe='')}")
    if not isinstance(matches, list):
        raise ValueError("Malformed matching-refs response")
    refs = set()
    for match in matches:
        if (not isinstance(match, dict) or not isinstance(match.get("ref"), str)
                or not match["ref"].startswith(ref)
                or match["ref"] in refs
                or not isinstance(match.get("object"), dict)
                or match["object"].get("type") not in {"commit", "tag"}
                or not isinstance(match["object"].get("sha"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", match["object"]["sha"])):
            raise ValueError("Malformed matching ref")
        refs.add(match["ref"])
        if match["ref"] == ref:
            raise ValueError(f"Tag {tag} already exists in the authenticated API")
    advertised = git("ls-remote", "--refs", "origin", ref)
    if advertised:
        lines = advertised.decode().splitlines()
        if any(not re.fullmatch(r"[0-9a-f]{40}\t" + re.escape(ref), x)
               for x in lines):
            raise ValueError("Malformed exact remote tag advertisement")
        raise ValueError(f"Tag {tag} already exists")
    return seen


def exact_tag(result, ref, sha):
    return (isinstance(result, dict) and result.get("ref") == ref
            and isinstance(result.get("object"), dict)
            and result["object"].get("type") == "commit"
            and result["object"].get("sha") == sha)


def publish():
    sha, version = guard_checkout(final=True)
    validate_built()
    repo = repository()
    tag = f"v{version}"
    assets = [Path(f"{PRODUCT}-{version}.{ext}") for ext in ("zip", "dmg")]
    for path in assets:
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"Missing or empty final asset: {path}")
    existing_ids = available(repo, tag)
    ref = f"refs/tags/{tag}"
    created = api("POST", f"repos/{repo}/git/refs", {"ref": ref, "sha": sha})
    if not exact_tag(created, ref, sha):
        raise ValueError("New tag response does not match the selected source")
    if git("ls-remote", "--refs", "origin", ref) != f"{sha}\t{ref}\n".encode():
        raise ValueError("New tag does not advertise the exact selected SHA")
    verified = api("GET", f"repos/{repo}/git/ref/tags/{quote(tag, safe='')}")
    if not exact_tag(verified, ref, sha):
        raise ValueError("New tag API verification does not match selected source")
    print(f"Created {ref} at {sha}; later failures retain this tag", flush=True)
    release = api("POST", f"repos/{repo}/releases", {
        "tag_name": tag, "name": tag, "target_commitish": sha,
        "draft": False, "prerelease": False, "body": "",
        "generate_release_notes": False,
    })
    if (not isinstance(release, dict) or not integer(release.get("id"))
            or release["id"] in existing_ids
            or release.get("tag_name") != tag or release.get("name") != tag
            or release.get("target_commitish") != sha
            or release.get("draft") is not False
            or release.get("prerelease") is not False
            or release.get("body") not in ("", None)):
        raise ValueError("Malformed newly-created release response")
    release_id = release["id"]
    print(f"Created release ID {release_id}; later failures retain it", flush=True)
    for path in assets:
        asset_name = path.name.replace(" ", ".")
        uploaded = api("POST",
                       f"https://uploads.github.com/repos/{repo}/releases/"
                       f"{release_id}/assets?name={quote(asset_name, safe='')}",
                       binary=path)
        if (not isinstance(uploaded, dict) or not integer(uploaded.get("id"))
                or uploaded.get("name") != asset_name
                or type(uploaded.get("size")) is not int
                or uploaded["size"] != path.stat().st_size
                or uploaded.get("state") != "uploaded"):
            raise ValueError(f"Asset verification failed: {path}")
        print(f"Uploaded {asset_name} to new release ID {release_id}", flush=True)
    print(f"Created {tag} at {sha}, release ID {release_id}")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"preflight", "initial", "publish"}:
        raise ValueError("Expected preflight, initial or publish")
    if sys.argv[1] == "publish":
        publish()
        return
    sha, version = guard_checkout()
    if sys.argv[1] == "preflight":
        available(repository(), f"v{version}")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"version={version}\ntag=v{version}\n")
    print(f"Validated immutable source {sha}: {version}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        sys.exit(f"Publication stopped: {error}. Partial remote state is retained; "
                 "inspect it and recover manually. No retry or rollback was attempted.")
