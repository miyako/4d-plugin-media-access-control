"""Offline bump CLI integration tests using isolated Git repositories.

Run: python3 -m unittest discover -s tests -p test_bump.py -v
No production checkout, GitHub API, native build, or inherited credentials are used.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
INTAKE = "7fc9a40f19c26fcd5305e2238b02a8f347ccaff0"
TRAILER = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
BOT = ("github-actions[bot]", "github-actions[bot]@users.noreply.github.com")
REF = "refs/heads/selected"


# This delegates to real Git, including its transport advertisement and real hook.
# Only the final push is raced; no ls-remote output or hook stdin is fabricated.
GIT_SHIM = r'''
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
real = os.environ["REAL_GIT"]
def git(*args):
    return subprocess.check_output([real, *args], text=True).strip()
if "ls-remote" in args:
    count_file = pathlib.Path(os.environ["ADVERTISE_COUNT"])
    count = int(count_file.read_text()) + 1 if count_file.exists() else 1
    count_file.write_text(str(count))
    result = subprocess.run([real, *args])
    if count == 2:
        hook = pathlib.Path(".github/hooks/pre-push")
        if os.environ.get("REMOVE_HOOK_AT_LAST_READ"):
            hook.unlink()
        if os.environ.get("DISABLE_HOOK_AT_LAST_READ"):
            hook.chmod(0o644)
    sys.exit(result.returncode)
if "push" in args:
    with open(os.environ["PUSH_LOG"], "a") as log:
        log.write(json.dumps(args) + "\n")
    action = os.environ.get("PUSH_ACTION", "")
    if action == "stop":
        sys.exit(91)
    if action in ("advance", "rewind", "delete", "same"):
        new = git("rev-parse", "HEAD")
        target = {
            "advance": os.environ.get("RACE_ADVANCE", ""),
            "rewind": os.environ.get("RACE_ANCESTOR", ""),
            "same": new,
        }.get(action)
        bare = "--git-dir=" + os.environ["BARE_ORIGIN"]
        if action == "same":
            git(bare, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                str(pathlib.Path.cwd()), new)
        if action == "delete":
            git(bare, "update-ref", "-d", os.environ["SELECTED_REF"])
        else:
            git(bare, "update-ref", os.environ["SELECTED_REF"], target)
        with open(os.environ["RACE_LOG"], "w") as log:
            json.dump({"action": action, "new": new, "target": target}, log)
os.execv(real, [real, *args])
'''


class BumpTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / (".bump-fixture-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.checkout = self.root / "detached checkout"
        self.checkout.mkdir()
        self.origin = self.root / "origin.git"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        shim = self.bin / "git"
        shim.write_text(f"#!{sys.executable}\n" + GIT_SHIM)
        shim.chmod(0o755)
        self.real_git = shutil.which("git")
        git_exec_path = subprocess.check_output(
            [self.real_git, "--exec-path"], text=True).strip()
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root),
            "GIT_CONFIG_GLOBAL": str(self.root / "global.gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_EXEC_PATH": git_exec_path,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_DATE": "2026-01-02T03:04:05+0000",
            "GIT_COMMITTER_DATE": "2026-01-02T03:04:05+0000",
            "PYTHONDONTWRITEBYTECODE": "1",
            "REAL_GIT": self.real_git,
            "BARE_ORIGIN": str(self.origin),
            "SELECTED_REF": REF,
            "ADVERTISE_COUNT": str(self.root / "advertisements"),
            "PUSH_LOG": str(self.root / "pushes.jsonl"),
            "RACE_LOG": str(self.root / "race.json"),
            "GITHUB_REF_TYPE": "branch",
            "GITHUB_REF": REF,
            "BUMP_MODE": "patch",
        }
        # Only actual current helper/source files are copied, not reimplementations.
        self.source_paths = json.loads(self.command(
            [sys.executable, "-B", "-c",
             "import sys,json; sys.path.insert(0,'.github/scripts'); "
             "from source_version import SOURCE_PATHS; "
             "print(json.dumps([str(p) for p in SOURCE_PATHS]))"],
            cwd=ROOT).stdout)
        for name in [
            ".github/scripts/bump.py", ".github/scripts/release.py",
            ".github/scripts/source_version.py", ".github/hooks/pre-push",
            *self.source_paths,
        ]:
            destination = self.checkout / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        self.git("init", "--quiet")
        self.git("config", "user.useConfigOnly", "true")
        self.git("config", "commit.gpgSign", "false")
        self.git("config", "--global", "push.followTags", "true")
        self.git("config", "--global", "safe.bareRepository", "explicit")
        self.git("config", "push.followTags", "true")
        self.bare("init", "--quiet", "--bare")
        (self.checkout / "fixture.txt").write_text("ancestor\n")
        self.git("add", "--all")
        self.commit("offline fixture ancestor")
        self.ancestor = self.head()
        (self.checkout / "fixture.txt").write_text("selected\n")
        self.git("add", "fixture.txt")
        self.commit("offline fixture selected")
        self.old = self.head()
        self.git("checkout", "--quiet", "--detach", self.old)
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "--quiet", "--no-follow-tags", "origin", f"HEAD:{REF}")
        self.env["GITHUB_SHA"] = self.old
        self.before = self.version()
        self.initial_sources = self.sources()
        self.env["RACE_ANCESTOR"] = self.ancestor
        self.env["RACE_ADVANCE"] = self.commit_tree(
            self.git("rev-parse", "HEAD^{tree}").stdout.strip(), [self.old],
            "offline concurrent advance")
        self.bare("fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                  str(self.checkout), self.env["RACE_ADVANCE"])

    def command(self, args, *, cwd=None, env=None, data=None, check=True):
        result = subprocess.run(
            args, cwd=cwd or self.checkout, env=env or self.env,
            input=data, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if check:
            self.assertEqual(result.returncode, 0,
                             f"{args!r}\n{result.stdout}\n{result.stderr}")
        return result

    def git(self, *args, **kwargs):
        return self.command([self.real_git, *args], **kwargs)

    def bare(self, *args, **kwargs):
        return self.git(f"--git-dir={self.origin}", *args, **kwargs)

    def commit(self, title, *, identity=BOT):
        self.git("-c", f"user.name={identity[0]}",
                 "-c", f"user.email={identity[1]}",
                 "commit", "--quiet", "-m", f"{title}\n\n{TRAILER}")

    def commit_tree(self, tree, parents, title, *, identity=BOT):
        args = ["-c", f"user.name={identity[0]}",
                "-c", f"user.email={identity[1]}", "commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        return self.git(*args, data=f"{title}\n\n{TRAILER}\n").stdout.strip()

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def version(self):
        return self.command([
            sys.executable, "-B", "-c",
            "import sys; sys.path.insert(0,'.github/scripts'); "
            "from source_version import read_version; print(read_version())",
        ]).stdout.strip()

    def sources(self):
        return {p: (self.checkout / p).read_bytes() for p in self.source_paths}

    def remote(self):
        result = self.bare("show-ref", "--verify", "--hash", REF, check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def pushes(self):
        path = Path(self.env["PUSH_LOG"])
        return [json.loads(line) for line in path.read_text().splitlines()] \
            if path.exists() else []

    def run_bump(self, **environment):
        env = dict(self.env, **environment)
        return self.command([sys.executable, "-B", ".github/scripts/bump.py"],
                            env=env, check=False)

    def guard(self, records, *, old=None, new=None, ref=REF):
        env = dict(self.env, BUMP_EXPECTED_OLD=old or self.old,
                   BUMP_EXPECTED_NEW=new or self.head(), BUMP_EXPECTED_REF=ref)
        return self.command([".github/hooks/pre-push", "origin", str(self.origin)],
                            env=env, data=records, check=False)

    def record(self, *, new=None):
        return f"HEAD {new or self.head()} {REF} {self.old}\n"

    def prepare_commit(self):
        result = self.run_bump(PUSH_ACTION="stop")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(self.head(), self.old, result.stderr)
        self.assertEqual(self.remote(), self.old)
        return self.head()

    def assert_no_edits(self, result, *, remote=None):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(self.sources(), self.initial_sources)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.remote(), remote or self.old)
        self.assertEqual(self.pushes(), [])
        self.assert_no_tags()

    def assert_no_tags(self):
        self.assertEqual(self.git("tag", "--list").stdout, "")
        self.assertEqual(self.bare("tag", "--list").stdout, "")

    def assert_bump(self, mode):
        self.assertNotEqual(self.git("symbolic-ref", "-q", "HEAD",
                                     check=False).returncode, 0)
        self.assertNotEqual(self.git("config", "--get", "user.name",
                                     check=False).returncode, 0)
        self.assertNotEqual(self.git("config", "--get", "user.email",
                                     check=False).returncode, 0)
        result = self.run_bump(BUMP_MODE=mode)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        parts = [int(p) for p in self.before.split(".")]
        index = {"major": 0, "minor": 1, "patch": 2}[mode]
        parts[index] += 1
        parts[index + 1:] = [0] * (2 - index)
        expected = ".".join(map(str, parts))
        self.assertEqual(self.version(), expected)
        new = self.head()
        self.assertEqual(self.remote(), new)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1",
                                  new).stdout.split(), [new, self.old])
        changed = set(self.git("diff-tree", "--no-commit-id", "--name-only",
                               "-r", self.old, new).stdout.splitlines())
        self.assertTrue(changed)
        self.assertLessEqual(changed, set(self.source_paths))
        self.assertNotEqual(self.sources(), self.initial_sources)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.git("show", "-s", "--format=%an%n%ae%n%cn%n%ce",
                                  new).stdout.splitlines(), list(BOT) * 2)
        self.assertTrue(self.git("show", "-s", "--format=%B", new)
                        .stdout.rstrip().endswith(TRAILER))
        pushes = self.pushes()
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0], [
            "-c", "push.followTags=false", "-c", "core.hooksPath=.github/hooks",
            "push", "--no-follow-tags", "origin", f"HEAD:{REF}",
        ])
        self.assertNotIn("--force", pushes[0])
        self.assertNotIn("--force-with-lease", pushes[0])

    def test_patch(self):
        self.assert_bump("patch")
        self.assert_no_tags()

    def test_minor(self):
        self.assert_bump("minor")
        self.assert_no_tags()

    def test_major(self):
        self.assert_bump("major")
        self.assert_no_tags()

    def test_annotated_tag_is_not_followed_even_when_configured_globally_and_locally(self):
        self.git("-c", f"user.name={BOT[0]}", "-c", f"user.email={BOT[1]}",
                 "tag", "-a", "fixture-annotated", "-m", "offline annotation",
                 self.old)
        self.assertEqual(self.git("cat-file", "-t", "fixture-annotated").stdout,
                         "tag\n")
        self.assertEqual(self.git("config", "--global", "--get",
                                  "push.followTags").stdout, "true\n")
        self.assertEqual(self.git("config", "--local", "--get",
                                  "push.followTags").stdout, "true\n")
        self.assert_bump("patch")
        self.assertEqual(self.git("tag", "--list").stdout, "fixture-annotated\n")
        self.assertEqual(self.bare("tag", "--list").stdout, "")

    def test_missing_hook_rejects_before_any_edits(self):
        self.git("rm", "--quiet", ".github/hooks/pre-push")
        self.commit("offline fixture without hook")
        self.old = self.head()
        self.env["GITHUB_SHA"] = self.old
        self.bare("fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                  str(self.checkout), self.old)
        self.bare("update-ref", REF, self.old)
        result = self.run_bump()
        self.assertIn("mandatory executable pre-push guard", result.stderr)
        self.assert_no_edits(result)

    def test_nonexecutable_hook_rejects_before_any_edits(self):
        hook = self.checkout / ".github/hooks/pre-push"
        hook.chmod(0o644)
        self.git("add", ".github/hooks/pre-push")
        self.commit("offline fixture with nonexecutable hook")
        self.old = self.head()
        self.env["GITHUB_SHA"] = self.old
        self.bare("fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                  str(self.checkout), self.old)
        self.bare("update-ref", REF, self.old)
        result = self.run_bump()
        self.assertIn("mandatory executable pre-push guard", result.stderr)
        self.assert_no_edits(result)

    def test_hook_is_required_again_immediately_before_push(self):
        result = self.run_bump(REMOVE_HOOK_AT_LAST_READ="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mandatory executable pre-push guard", result.stderr)
        self.assertNotEqual(self.head(), self.old)
        self.assertEqual(self.remote(), self.old)
        self.assertEqual(self.pushes(), [])
        self.assertEqual(Path(self.env["ADVERTISE_COUNT"]).read_text(), "2")
        self.assert_no_tags()

    def test_hook_must_still_be_executable_immediately_before_push(self):
        result = self.run_bump(DISABLE_HOOK_AT_LAST_READ="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mandatory executable pre-push guard", result.stderr)
        self.assertNotEqual(self.head(), self.old)
        self.assertEqual(self.remote(), self.old)
        self.assertEqual(self.pushes(), [])
        self.assertEqual(Path(self.env["ADVERTISE_COUNT"]).read_text(), "2")
        self.assert_no_tags()

    def test_stale_initial_selection_fails_before_edits(self):
        advanced = self.env["RACE_ADVANCE"]
        self.bare("update-ref", REF, advanced)
        result = self.run_bump()
        self.assertIn("moved since the immutable event", result.stderr)
        self.assert_no_edits(result, remote=advanced)

    def test_missing_selected_branch_fails_before_edits(self):
        self.bare("update-ref", "-d", REF)
        result = self.run_bump()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Selected branch is missing", result.stderr)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(self.sources(), self.initial_sources)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.pushes(), [])
        self.assertIsNone(self.remote())

    def test_tag_selection_fails_before_edits(self):
        result = self.run_bump(GITHUB_REF_TYPE="tag", GITHUB_REF="refs/tags/v0.0.0")
        self.assertIn("requires a selected branch", result.stderr)
        self.assert_no_edits(result)

    def test_invalid_branch_selection_fails_before_edits(self):
        for ref in ("", "selected", "refs/tags/v0.0.0", "refs/heads/bad..name"):
            with self.subTest(ref=ref):
                self.assert_no_edits(self.run_bump(GITHUB_REF=ref))

    def test_invalid_bump_mode_fails_before_edits(self):
        self.assert_no_edits(self.run_bump(BUMP_MODE="release"))

    def test_wrong_immutable_sha_fails_before_edits(self):
        result = self.run_bump(GITHUB_SHA=self.ancestor)
        self.assertIn("immutable event commit", result.stderr)
        self.assert_no_edits(result)

    def assert_real_race(self, action):
        result = self.run_bump(PUSH_ACTION=action)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        new = self.head()
        self.assertNotEqual(new, self.old)
        race = json.loads(Path(self.env["RACE_LOG"]).read_text())
        self.assertEqual(race["new"], new)
        self.assertEqual(race["action"], action)
        self.assertEqual(self.remote(), race["target"])
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(Path(self.env["ADVERTISE_COUNT"]).read_text(), "2")
        self.assertIn("Push advertisement must contain exactly HEAD/new/ref/old once",
                      result.stderr)
        self.assertEqual(self.pushes()[0], [
            "-c", "push.followTags=false", "-c", "core.hooksPath=.github/hooks",
            "push", "--no-follow-tags", "origin", f"HEAD:{REF}",
        ])
        self.assert_no_tags()
        if action == "rewind":
            self.assertEqual(self.git("merge-base", "--is-ancestor",
                                      self.ancestor, new).returncode, 0)

    def test_real_push_rejects_remote_advance_after_last_read(self):
        self.assert_real_race("advance")

    def test_real_push_rejects_remote_rewind_even_when_fast_forward(self):
        self.assert_real_race("rewind")

    def test_real_push_rejects_remote_deletion_instead_of_recreating_branch(self):
        self.assert_real_race("delete")

    def test_real_push_rejects_same_result_race_with_zero_hook_records(self):
        self.assert_real_race("same")

    def test_pre_push_accepts_only_exact_single_literal_head_record(self):
        new = self.prepare_commit()
        self.assertEqual(self.guard(self.record()).returncode, 0)
        wrong = "1" * 40 if new != "1" * 40 else "2" * 40
        records = {
            "zero": "",
            "duplicate": self.record() * 2,
            "additional ref": self.record() + f"HEAD {new} refs/heads/other {self.old}\n",
            "wrong new SHA": self.record(new=wrong),
            "wrong old SHA": f"HEAD {new} {REF} {self.ancestor}\n",
            "wrong destination": f"HEAD {new} refs/heads/other {self.old}\n",
            "nonliteral source": f"refs/heads/selected {new} {REF} {self.old}\n",
            "SHA source": f"{new} {new} {REF} {self.old}\n",
        }
        for label, data in records.items():
            with self.subTest(label=label):
                result = self.guard(data)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("exactly HEAD/new/ref/old once", result.stderr)
        self.assertEqual(self.remote(), self.old)

    def test_pre_push_validates_expected_sha_and_branch(self):
        self.prepare_commit()
        for old, new, ref in [
            ("not-a-sha", self.head(), REF),
            (self.old, "not-a-sha", REF),
            (self.old, self.head(), "refs/tags/fixture"),
        ]:
            with self.subTest(old=old, new=new, ref=ref):
                data = f"HEAD {new} {ref} {old}\n"
                result = self.guard(data, old=old, new=new, ref=ref)
                self.assertNotEqual(result.returncode, 0)

    def test_pre_push_rejects_empty_merge_outside_source_wrong_parent_and_identity(self):
        new = self.prepare_commit()
        old_tree = self.git("rev-parse", f"{self.old}^{{tree}}").stdout.strip()
        new_tree = self.git("rev-parse", f"{new}^{{tree}}").stdout.strip()
        (self.checkout / "fixture.txt").write_text("not a source-version input\n")
        self.git("add", "fixture.txt")
        outside_tree = self.git("write-tree").stdout.strip()
        self.git("reset", "--hard", "--quiet", new)
        cases = [
            ("empty", old_tree, [self.old], BOT, "nonempty source-only"),
            ("merge", new_tree, [self.old, self.ancestor], BOT, "sole parent"),
            ("outside", outside_tree, [self.old], BOT, "nonempty source-only"),
            ("wrong parent", new_tree, [self.ancestor], BOT, "sole parent"),
            ("wrong identity", new_tree, [self.old],
             ("Fixture Author", "fixture@example.invalid"), "Actions bot identity"),
        ]
        for title, tree, parents, identity, error in cases:
            with self.subTest(title=title):
                candidate = self.commit_tree(tree, parents, title, identity=identity)
                self.git("checkout", "--quiet", "--detach", candidate)
                result = self.guard(self.record())
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
        self.git("checkout", "--quiet", "--detach", new)
        result = self.guard(self.record(new=self.old), new=self.old)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("new literal HEAD", result.stderr)
        self.assertEqual(self.remote(), self.old)

    def test_original_bump_history_is_pinned_to_intake_not_current_head(self):
        original = self.git("show", f"{INTAKE}:.github/workflows/bump-version.yml",
                            cwd=ROOT).stdout
        self.assertIn('git tag "v${NEW}"', original)
        self.assertIn("MARKETING_VERSION/CURRENT_PROJECT_VERSION", original)


if __name__ == "__main__":
    unittest.main()
