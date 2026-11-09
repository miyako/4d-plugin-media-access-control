"""Offline CLI publication tests using real disposable Git repositories.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_release.py -v
Only Git and the current Python interpreter execute; gh is a closed fake.
"""

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INTAKE = "7fc9a40f19c26fcd5305e2238b02a8f347ccaff0"
PRODUCT = "Media Access Control"
REPOSITORY = "miyako/4d-plugin-media-access-control"
CONTENTS = f"test/Plugins/{PRODUCT}.bundle/Contents"
GENERATED = (
    f"{CONTENTS}/MacOS/{PRODUCT}",
    f"{CONTENTS}/Windows64/{PRODUCT}.4DX",
    f"{CONTENTS}/Info.plist",
    f"{CONTENTS}/Resources/English.lproj/InfoPlist.strings",
    f"{CONTENTS}/_CodeSignature/CodeResources",
)
SOURCE = (
    ".gitignore",
    "Info.plist",
    "English.lproj/InfoPlist.strings",
    f"{PRODUCT}.xcodeproj/project.pbxproj",
    f"{PRODUCT}.vcxproj",
    "4DPlugin-Media-Access-Control.cpp",
    "manifest.json",
    "README.md",
    ".github/workflows/release.yml",
)
TRAILER = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"


FAKE_GIT = r'''
import json, os, subprocess, sys
with open(os.environ["EVENTS"], "a") as out:
    out.write(json.dumps(["git", sys.argv[1:]]) + "\n")
sys.exit(subprocess.run([os.environ["REAL_GIT"], *sys.argv[1:]]).returncode)
'''


FAKE_GH = r'''
import json, os, pathlib, subprocess, sys
from urllib.parse import parse_qs, urlsplit

state_path = pathlib.Path(os.environ["STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with open(os.environ["EVENTS"], "a") as out:
    out.write(json.dumps(["gh", args]) + "\n")

def save():
    state_path.write_text(json.dumps(state))

def fail(message="offline simulated API failure"):
    save()
    print(message, file=sys.stderr)
    sys.exit(22)

def emit(value):
    save()
    if isinstance(value, dict) and "__raw__" in value:
        print(value["__raw__"])
        sys.exit(0)
    if value in ("404", "network", "auth"):
        fail(value)
    if value == "malformed-json":
        print("{")
        sys.exit(0)
    print(json.dumps(value))
    sys.exit(0)

if len(args) < 5 or args[:2] != ["api", "--method"]:
    fail("Forbidden non-API gh operation")
method, endpoint = args[2:4]
call = {"method": method, "endpoint": endpoint}
if "--input" in args:
    operand = args[args.index("--input") + 1]
    if operand == "-":
        call["payload"] = json.load(sys.stdin)
    else:
        path = pathlib.Path(operand)
        call["filename"] = path.name
        call["size"] = path.stat().st_size
state["calls"].append(call)
save()
repo = "repos/miyako/4d-plugin-media-access-control"
tag = "v" + state["version"]
ref = "refs/tags/" + tag
if method == "GET" and endpoint.startswith(repo + "/releases?"):
    page = parse_qs(urlsplit(endpoint).query)["page"][0]
    response = state.get("pages", {}).get(page, [])
    emit(response)
elif method == "GET" and endpoint == repo + "/git/matching-refs/tags/" + tag:
    emit(state.get("matching_refs", []))
elif method == "GET" and endpoint == repo + "/git/ref/tags/" + tag:
    emit(state.get("verify_response", {
        "ref": ref, "object": {"type": "commit", "sha": state["sha"]}}))
elif method == "POST" and endpoint == repo + "/git/refs":
    assert call["payload"] == {"ref": ref, "sha": state["sha"]}
    if state.get("tag_failure") == "network":
        fail()
    command = [os.environ["REAL_GIT"], "--git-dir=" + os.environ["ORIGIN"],
               "update-ref", ref, state["sha"]]
    subprocess.run(command, check=True)
    state["tag_command"] = command[1:]
    state["tag_created"] = True
    if state.get("tag_failure") == "same-sha-race":
        fail("Reference already exists (created concurrently at identical SHA)")
    emit(state.get("tag_response", {
        "ref": ref, "object": {"type": "commit", "sha": state["sha"]}}))
elif method == "POST" and endpoint == repo + "/releases":
    assert call["payload"] == {
        "tag_name": tag, "name": tag, "target_commitish": state["sha"],
        "draft": False, "prerelease": False, "body": "",
        "generate_release_notes": False}
    if state.get("release_failure"):
        fail()
    release = dict(call["payload"], id=701)
    state["release_created"] = release
    emit(state.get("release_response", release))
elif method == "POST" and endpoint.startswith(
        "https://uploads.github.com/" + repo + "/releases/701/assets?"):
    number = len(state["uploads"]) + 1
    name = parse_qs(urlsplit(endpoint).query)["name"][0]
    assert name == call["filename"].replace(" ", ".")
    assert name == "Media.Access.Control-" + state["version"] + (
        ".zip" if number == 1 else ".dmg")
    if state.get("asset_failure") == number:
        fail()
    uploaded = {"id": 800 + number, "name": name,
                "size": call["size"], "state": "uploaded"}
    state["uploads"].append(uploaded)
    if "asset_response_raw" in state:
        emit({"__raw__": state["asset_response_raw"]})
    emit(dict(uploaded, **state.get("asset_response", {})))
else:
    fail("Forbidden endpoint or mutation: " + method + " " + endpoint)
'''


class ReleaseCLI(unittest.TestCase):
    """No production module is mocked or imported into the test process."""

    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix=".release-fixture-", dir=ROOT)
        self.addCleanup(self.fixture.cleanup)
        self.base = Path(self.fixture.name)
        self.work = self.base / "checkout"
        self.work.mkdir()
        self.tools = self.base / "tools"
        self.tools.mkdir()
        self.origin = self.base / "origin.git"
        self.state_path = self.base / "state.json"
        self.events_path = self.base / "events.jsonl"
        self.output = self.base / "github-output"
        self.real_git = shutil.which("git")
        self.assertIsNotNone(self.real_git)
        for transport in ("git-receive-pack", "git-upload-pack"):
            (self.tools / transport).symlink_to(self.real_git)
        for name, program in (("git", FAKE_GIT), ("gh", FAKE_GH)):
            path = self.tools / name
            path.write_text(f"#!{sys.executable}\n" + program)
            path.chmod(0o755)
        self.global_config = self.base / "gitconfig"
        self.global_config.write_text("[safe]\n\tbareRepository = explicit\n")
        self.env = {
            "PATH": str(self.tools),
            "HOME": str(self.base),
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.global_config),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_AUTHOR_NAME": "Offline fixture",
            "GIT_AUTHOR_EMAIL": "offline@example.invalid",
            "GIT_COMMITTER_NAME": "Offline fixture",
            "GIT_COMMITTER_EMAIL": "offline@example.invalid",
            "GH_TOKEN": "fake",
            "GH_HOST": "github.com",
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_OUTPUT": str(self.output),
            "REAL_GIT": self.real_git,
            "ORIGIN": str(self.origin),
            "STATE": str(self.state_path),
            "EVENTS": str(self.events_path),
        }
        # Copy working files, including untracked scripts under active development,
        # rather than stale versions from HEAD or handwritten parser stand-ins.
        for relative in SOURCE + GENERATED:
            self.copy(relative)
        for source in (ROOT / ".github/scripts").glob("*.py"):
            self.copy(str(source.relative_to(ROOT)))
        self.assertTrue(
            (self.work / ".github/scripts/source_version.py").is_file(),
            "Pending source_version.py interface: read_version() and validate_built()",
        )
        self.git("init", "--quiet")
        self.git("add", "--all")
        self.commit("Current actual source fixture")
        self.git("init", "--bare", "--quiet", str(self.origin))
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "--quiet", "origin", "HEAD:refs/heads/main")
        self.sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.env["GITHUB_SHA"] = self.sha
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.github/scripts'); "
             "from source_version import read_version; print(read_version())"],
            cwd=self.work, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.version = result.stdout.strip()
        self.assertRegex(self.version, r"^\d+\.\d+\.\d+$")
        self.state_path.write_text(json.dumps({
            "version": self.version, "sha": self.sha, "calls": [], "uploads": [],
        }))
        self.clear_events()

    def copy(self, relative):
        destination = self.work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)

    def git(self, *args, check=True):
        result = subprocess.run(
            [self.real_git, *args], cwd=self.work, env=self.env,
            text=True, capture_output=True,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def commit(self, message):
        self.git("commit", "--quiet", "-m", message + "\n\n" + TRAILER)

    def state(self):
        return json.loads(self.state_path.read_text())

    def configure(self, **updates):
        state = self.state()
        state.update(updates)
        self.state_path.write_text(json.dumps(state))

    def clear_events(self):
        self.events_path.write_text("")

    def events(self):
        return [json.loads(line) for line in self.events_path.read_text().splitlines()]

    def remote_tag(self):
        return self.git(
            "--git-dir=" + str(self.origin), "show-ref", "--verify",
            "refs/tags/v" + self.version, check=False,
        )

    def build(self, fmt=plistlib.FMT_XML):
        source = plistlib.loads((self.work / "Info.plist").read_bytes())
        source["CFBundleShortVersionString"] = self.version
        # Do not invent a build version if the actual source deliberately has none.
        if "CFBundleVersion" in source and "$(" in str(source["CFBundleVersion"]):
            source["CFBundleVersion"] = self.version
        (self.work / GENERATED[2]).write_bytes(plistlib.dumps(source, fmt=fmt))
        shutil.copyfile(
            self.work / "English.lproj/InfoPlist.strings",
            self.work / GENERATED[3],
        )
        for relative in (GENERATED[0], GENERATED[1], GENERATED[4]):
            (self.work / relative).write_bytes(b"native rewritten bytes\x00\xff\n")
        for extension in ("zip", "dmg"):
            (self.work / f"{PRODUCT}-{self.version}.{extension}").write_bytes(
                b"offline archive " + extension.encode()
            )

    def invoke(self, command, success=False, env=None):
        environment = dict(self.env)
        for key, value in (env or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        result = subprocess.run(
            [sys.executable, ".github/scripts/release.py", command],
            cwd=self.work, env=environment, capture_output=True, text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        for kind, args in self.events():
            if kind == "gh":
                self.assertEqual(args[:2], ["api", "--method"])
                self.assertIn(args[2], ("GET", "POST"))
                self.assertFalse(any(token in args for token in (
                    "DELETE", "PATCH", "--clobber", "--verify-tag",
                )))
            if kind == "git":
                self.assertNotIn(args[0], (
                    "reset", "restore", "checkout", "clean", "push", "tag", "update-ref",
                ))
        return result

    def assert_no_api(self):
        self.assertEqual(self.state()["calls"], [])
        self.assertFalse(any(kind == "gh" for kind, _ in self.events()))

    def assert_retained(self, tag=False, release=False, uploads=0):
        state = self.state()
        self.assertEqual(self.remote_tag().returncode == 0, tag)
        self.assertEqual("release_created" in state, release)
        self.assertEqual(len(state["uploads"]), uploads)
        calls = state["calls"]
        for endpoint in ("/git/refs", "/releases"):
            self.assertLessEqual(sum(
                c["method"] == "POST" and c["endpoint"].endswith(endpoint)
                for c in calls
            ), 1)

    def full_page(self):
        return [{"id": n, "tag_name": f"old-{n}", "draft": bool(n % 2)}
                for n in range(1, 101)]

    def matching_ref(self, suffix="", kind="commit", sha=None):
        return {"ref": "refs/tags/v" + self.version + suffix,
                "object": {"type": kind, "sha": sha or self.sha}}

    def reset_remote_scenario(self, **updates):
        self.git("--git-dir=" + str(self.origin), "update-ref", "-d",
                 "refs/tags/v" + self.version)
        self.configure(calls=[], uploads=[], **updates)
        self.clear_events()

    def test_initial_clean_checkout_has_no_api_or_writes(self):
        before = {p: (self.work / p).read_bytes() for p in SOURCE + GENERATED}
        self.invoke("initial", success=True)
        self.assert_no_api()
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(before, {p: (self.work / p).read_bytes() for p in before})

    def test_preflight_enumerates_all_pages_and_exact_remote_ref(self):
        self.configure(pages={"1": self.full_page(), "2": [
            {"id": 101, "tag_name": "other-draft", "draft": True},
        ]})
        self.invoke("preflight", success=True)
        self.assertEqual(self.output.read_text(),
                         f"version={self.version}\ntag=v{self.version}\n")
        self.assertEqual([c["endpoint"] for c in self.state()["calls"]], [
            f"repos/{REPOSITORY}/releases?per_page=100&page={page}"
            for page in (1, 2, 3)
        ] + [f"repos/{REPOSITORY}/git/matching-refs/tags/v{self.version}"])
        self.assertTrue(all(c["method"] == "GET" for c in self.state()["calls"]))
        self.assertIn(["git", ["ls-remote", "--refs", "origin",
                               "refs/tags/v" + self.version]], self.events())
        self.assert_retained()

    def test_short_pages_continue_until_empty_in_preflight_and_publish(self):
        self.configure(pages={
            "1": [{"id": 1, "tag_name": "old-public", "draft": False}],
            "2": [{"id": 2, "tag_name": "old-draft", "draft": True}],
            "3": [],
        })
        self.invoke("preflight", success=True)
        expected = [f"repos/{REPOSITORY}/releases?per_page=100&page={page}"
                    for page in (1, 2, 3)]
        expected.append(
            f"repos/{REPOSITORY}/git/matching-refs/tags/v{self.version}")
        self.assertEqual([c["endpoint"] for c in self.state()["calls"]], expected)
        self.configure(calls=[])
        self.clear_events()
        self.build()
        self.invoke("publish", success=True)
        self.assertEqual([c["endpoint"] for c in self.state()["calls"][:4]], expected)
        self.assertEqual(self.state()["calls"][4]["method"], "POST")

    def test_collision_after_a_short_page_stops_before_post(self):
        self.build()
        for draft in (False, True):
            with self.subTest(draft=draft):
                self.configure(pages={
                    "1": [{"id": 1, "tag_name": "unrelated", "draft": True}],
                    "2": [{"id": 2, "tag_name": "v" + self.version, "draft": draft}],
                }, calls=[])
                self.invoke("publish")
                self.assertEqual([c["method"] for c in self.state()["calls"]],
                                 ["GET", "GET"])
                self.assert_retained()

    def test_failure_after_a_short_page_stops_before_post(self):
        self.build()
        for response in ("auth", "404", "network", "malformed-json"):
            with self.subTest(response=response):
                self.configure(pages={
                    "1": [{"id": 1, "tag_name": "unrelated", "draft": False}],
                    "2": response,
                }, calls=[])
                self.invoke("publish")
                self.assertEqual([c["method"] for c in self.state()["calls"]],
                                 ["GET", "GET"])
                self.assert_retained()

    def test_successful_publish_preserves_all_native_bytes_and_uses_new_id(self):
        self.build()
        self.configure(pages={"1": self.full_page(), "2": []})
        before = {p: (self.work / p).read_bytes() for p in GENERATED}
        self.invoke("publish", success=True)
        self.assertEqual(before, {p: (self.work / p).read_bytes() for p in before})
        state = self.state()
        self.assertEqual([c["method"] for c in state["calls"]],
                         ["GET", "GET", "GET", "POST", "GET", "POST", "POST", "POST"])
        self.assertEqual([c["endpoint"] for c in state["calls"]], [
            f"repos/{REPOSITORY}/releases?per_page=100&page=1",
            f"repos/{REPOSITORY}/releases?per_page=100&page=2",
            f"repos/{REPOSITORY}/git/matching-refs/tags/v{self.version}",
            f"repos/{REPOSITORY}/git/refs",
            f"repos/{REPOSITORY}/git/ref/tags/v{self.version}",
            f"repos/{REPOSITORY}/releases",
        ] + [
            f"https://uploads.github.com/repos/{REPOSITORY}/releases/701/"
            f"assets?name=Media.Access.Control-{self.version}.{extension}"
            for extension in ("zip", "dmg")
        ])
        self.assertEqual(state["tag_command"],
                         ["--git-dir=" + str(self.origin), "update-ref",
                          "refs/tags/v" + self.version, self.sha])
        self.assertEqual(state["release_created"]["id"], 701)
        for upload, extension in zip(state["uploads"], ("zip", "dmg")):
            path = self.work / f"{PRODUCT}-{self.version}.{extension}"
            self.assertEqual(upload["name"], path.name.replace(" ", "."))
            self.assertEqual(upload["size"], path.stat().st_size)
            self.assertEqual(upload["state"], "uploaded")
        remote_checks = [i for i, event in enumerate(self.events())
                         if event == ["git", ["ls-remote", "--refs", "origin",
                                              "refs/tags/v" + self.version]]]
        self.assertEqual(len(remote_checks), 2)
        first_post = next(i for i, (kind, args) in enumerate(self.events())
                          if kind == "gh" and args[2] == "POST")
        self.assertLess(remote_checks[0], first_post)
        self.assert_retained(tag=True, release=True, uploads=2)

    def test_binary_built_plist_is_validated_by_real_parser(self):
        self.build(plistlib.FMT_BINARY)
        self.invoke("publish", success=True)
        self.assert_retained(tag=True, release=True, uploads=2)

    def test_build_preserves_optional_source_build_version_absence(self):
        source = plistlib.loads((self.work / "Info.plist").read_bytes())
        self.build()
        built = plistlib.loads((self.work / GENERATED[2]).read_bytes())
        self.assertEqual("CFBundleVersion" in built, "CFBundleVersion" in source)
        self.invoke("publish", success=True)

    def test_final_allows_staged_exact_generated_paths_without_restoring_them(self):
        self.build()
        self.git("add", "--", *GENERATED)
        before = {p: (self.work / p).read_bytes() for p in GENERATED}
        index_before = self.git("ls-files", "--stage", "-z").stdout
        self.invoke("publish", success=True)
        self.assertEqual(before, {p: (self.work / p).read_bytes() for p in before})
        self.assertEqual(index_before, self.git("ls-files", "--stage", "-z").stdout)

    def test_final_allows_only_declared_generated_outputs(self):
        self.build()
        for relative in (
            f"winbuild/{PRODUCT}.4DX",
            f"build/{PRODUCT}.build/Release/objects.o",
            f"test/Plugins/{PRODUCT}.build/Release/objects.o",
            "Objs/Release/objects.obj",
        ):
            path = self.work / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fresh native output")
        self.invoke("publish", success=True)

    def test_initial_rejects_even_generated_dirt(self):
        (self.work / GENERATED[0]).write_bytes(b"changed native output")
        self.invoke("initial")
        self.assert_no_api()

    def test_initial_rejects_untracked_and_ignored_globally(self):
        for relative in ("unrelated.txt", "build/ignored-output"):
            with self.subTest(path=relative):
                path = self.work / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"unexpected")
                self.invoke("initial")
                self.assert_no_api()
                path.unlink()

    def test_source_raw_worktree_change_stops_before_api(self):
        self.build()
        path = self.work / "README.md"
        path.write_bytes(path.read_bytes() + b"\x00uncommitted source")
        self.invoke("publish")
        self.assert_no_api()

    def test_staged_source_with_restored_worktree_stops_before_api(self):
        self.build()
        path = self.work / "README.md"
        original = path.read_bytes()
        path.write_bytes(original + b"staged source")
        self.git("add", "README.md")
        path.write_bytes(original)
        self.invoke("publish")
        self.assert_no_api()

    def test_hidden_index_flags_and_hidden_raw_changes_fail_before_api(self):
        self.build()
        path = self.work / "README.md"
        original = path.read_bytes()
        for flag, undo in (
            ("--assume-unchanged", "--no-assume-unchanged"),
            ("--skip-worktree", "--no-skip-worktree"),
        ):
            for dirty in (False, True):
                with self.subTest(flag=flag, dirty=dirty):
                    self.git("update-index", flag, "README.md")
                    if dirty:
                        path.write_bytes(original + b"\x00hidden bytes")
                    self.invoke("publish")
                    self.assert_no_api()
                    self.git("update-index", undo, "README.md")
                    path.write_bytes(original)

    def test_head_change_stops_before_api(self):
        self.build()
        self.git(
            "commit", "--quiet", "--allow-empty", "-m", "New HEAD\n\n" + TRAILER)
        self.invoke("publish")
        self.assert_no_api()

    def test_bad_event_sha_stops_before_api(self):
        self.build()
        for sha in ("", "main", "a" * 40, self.sha.upper()):
            with self.subTest(sha=sha):
                self.invoke("publish", env={"GITHUB_SHA": sha})
                self.assert_no_api()

    def test_untracked_and_ignored_source_or_configuration_fail_before_api(self):
        self.build()
        for relative in (
            "new-source.cpp", ".github/new-config.yml",
            "build/injected-source.cpp", "build/source_version.py",
            f"test/Plugins/{PRODUCT}.bundle/Contents/Resources/unexpected.json",
        ):
            with self.subTest(path=relative):
                path = self.work / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"not an allowed build output")
                self.invoke("publish")
                self.assert_no_api()
                path.unlink()

    def test_changed_tracked_paths_and_modes_fail_before_api(self):
        self.build()
        source = self.work / "README.md"
        original = source.read_bytes()
        self.git("rm", "--quiet", "README.md")
        source.write_bytes(original)
        self.invoke("publish")
        self.assert_no_api()
        self.git("add", "README.md")
        source.chmod(source.stat().st_mode ^ 0o100)
        self.invoke("publish")
        self.assert_no_api()

    def test_unmerged_index_fails_before_api(self):
        self.build()
        oid = self.git("rev-parse", "HEAD:README.md").stdout.strip()
        self.git("update-index", "--force-remove", "README.md")
        result = subprocess.run(
            [self.real_git, "update-index", "--index-info"],
            input=f"100644 {oid} 1\tREADME.md\n100644 {oid} 2\tREADME.md\n",
            cwd=self.work, env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.invoke("publish")
        self.assert_no_api()

    def test_built_plist_version_and_malformed_plists_fail_before_api(self):
        self.build()
        path = self.work / GENERATED[2]
        good = path.read_bytes()
        for fmt in (plistlib.FMT_XML, plistlib.FMT_BINARY):
            metadata = plistlib.loads(good)
            metadata["CFBundleShortVersionString"] = "999.999.999"
            with self.subTest(format=fmt):
                path.write_bytes(plistlib.dumps(metadata, fmt=fmt))
                self.invoke("publish")
                self.assert_no_api()
        for content in (b"not a plist", b"bplist00corrupt", b"<plist><dict>"):
            with self.subTest(content=content):
                path.write_bytes(content)
                self.invoke("publish")
                self.assert_no_api()

    def test_actual_built_localization_is_checked_before_api(self):
        self.build()
        path = self.work / GENERATED[3]
        original = path.read_bytes()
        text = original.decode("utf-16" if original.startswith((b"\xff\xfe", b"\xfe\xff"))
                               else "utf-8")
        self.assertIn(self.version, text)
        for content in (
            text.replace(self.version, "999.999.999").encode("utf-8"),
            b'CFBundleShortVersionString = "not-a-version";',
        ):
            with self.subTest(content=content):
                path.write_bytes(content)
                self.invoke("publish")
                self.assert_no_api()

    def test_missing_built_metadata_stops_before_api(self):
        self.build()
        for relative in (GENERATED[2], GENERATED[3]):
            with self.subTest(path=relative):
                path = self.work / relative
                original = path.read_bytes()
                path.unlink()
                self.invoke("publish")
                self.assert_no_api()
                path.write_bytes(original)

    def test_missing_empty_and_symlink_assets_stop_before_api(self):
        self.build()
        for extension in ("zip", "dmg"):
            path = self.work / f"{PRODUCT}-{self.version}.{extension}"
            good = path.read_bytes()
            for kind in ("missing", "empty", "symlink"):
                with self.subTest(extension=extension, kind=kind):
                    path.unlink()
                    if kind == "empty":
                        path.write_bytes(b"")
                    elif kind == "symlink":
                        path.symlink_to(self.work / GENERATED[0])
                    self.invoke("publish")
                    self.assert_no_api()
                    if path.exists() or path.is_symlink():
                        path.unlink()
                    path.write_bytes(good)

    def test_generated_extra_symlink_is_not_permitted(self):
        self.build()
        path = self.work / f"winbuild/{PRODUCT}.4DX"
        path.parent.mkdir()
        path.symlink_to(self.work / GENERATED[1])
        self.invoke("publish")
        self.assert_no_api()

    def test_source_and_generated_symlink_ancestors_fail_before_api(self):
        self.build()
        for relative in (
            "English.lproj",
            f"{CONTENTS}/MacOS",
            f"{CONTENTS}/Resources/English.lproj",
        ):
            with self.subTest(directory=relative):
                path = self.work / relative
                moved = self.base / "outside-checkout"
                path.rename(moved)
                path.symlink_to(moved, target_is_directory=True)
                self.invoke("publish")
                self.assert_no_api()
                path.unlink()
                moved.rename(path)

    def test_ignored_generated_directory_cannot_hide_source_or_config(self):
        self.build()
        for relative in (
            f"build/{PRODUCT}.build/injected.cpp",
            f"build/{PRODUCT}.build/build.yml",
            f"build/{PRODUCT}.build/project.pbxproj",
            "build/XCBuildData/source_version.py",
            "Objs/Release/injected.h",
        ):
            with self.subTest(path=relative):
                path = self.work / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"unexpected source or configuration")
                self.invoke("publish")
                self.assert_no_api()
                path.unlink()

    def test_missing_explicit_authentication_fails_before_api(self):
        self.build()
        for command in ("preflight", "publish"):
            with self.subTest(command=command):
                self.invoke(command, env={"GH_TOKEN": None})
                self.assert_no_api()

    def test_invalid_repository_fails_before_api(self):
        self.build()
        for repo in ("", "owner repo/name", "owner/repo/extra",
                     "offline/media-access-control", "miyako/another-repository",
                     REPOSITORY.upper()):
            with self.subTest(repo=repo):
                self.invoke("publish", env={"GITHUB_REPOSITORY": repo})
                self.assert_no_api()

    def test_failed_or_malformed_first_page_fails_closed(self):
        self.build()
        for response in (
            "auth", "404", "network", "malformed-json", {}, None,
            [{"id": True, "tag_name": "old", "draft": False}],
            [{"id": 0, "tag_name": "old", "draft": False}],
            [{"id": 1, "tag_name": "old", "draft": "false"}],
            [{"id": 1, "tag_name": None, "draft": False}],
            self.full_page() + [{"id": 101, "tag_name": "old", "draft": False}],
        ):
            with self.subTest(response=response):
                self.configure(pages={"1": response}, calls=[])
                self.invoke("publish")
                self.assertEqual([c["method"] for c in self.state()["calls"]], ["GET"])
                self.assert_retained()

    def test_page_two_failure_or_repeated_entry_fails_closed(self):
        self.build()
        for response in ("404", "network", "malformed-json", {}, [
            {"id": 1, "tag_name": "repeated", "draft": False},
        ]):
            with self.subTest(response=response):
                self.configure(pages={"1": self.full_page(), "2": response}, calls=[])
                self.invoke("publish")
                self.assertEqual([c["method"] for c in self.state()["calls"]],
                                 ["GET", "GET"])
                self.assert_retained()

    def test_page_two_collision_including_draft_stops_before_post(self):
        self.build()
        for draft in (False, True):
            with self.subTest(draft=draft):
                self.configure(pages={"1": self.full_page(), "2": [
                    {"id": 101, "tag_name": "v" + self.version, "draft": draft},
                ]}, calls=[])
                self.invoke("publish")
                self.assertEqual([c["method"] for c in self.state()["calls"]],
                                 ["GET", "GET"])
                self.assert_retained()

    def test_duplicate_json_keys_and_nonfinite_constants_fail_before_post(self):
        self.build()
        for raw in (
            '[{"id":1,"id":2,"tag_name":"old","draft":false}]',
            '[{"id":1,"tag_name":"old","draft":false,"extra":NaN}]',
            '[{"id":1,"tag_name":"old","draft":false,"extra":Infinity}]',
            '[{"id":1,"tag_name":"old","draft":false,"extra":-Infinity}]',
            '[{"id":1,"tag_name":"old","draft":false,'
            '"extra":{"nested":1,"nested":2}}]',
        ):
            with self.subTest(raw=raw):
                self.configure(pages={"1": {"__raw__": raw}}, calls=[])
                self.invoke("publish")
                self.assertEqual([c["method"] for c in self.state()["calls"]], ["GET"])
                self.assert_retained()

    def test_matching_refs_auth_network_and_malformed_fail_closed(self):
        self.build()
        for response in (
            "auth", "404", "network", "malformed-json", {}, None,
            [{"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.sha}}],
            [{"ref": "refs/tags/v" + self.version + "-other",
              "object": {"type": "blob", "sha": self.sha}}],
            [{"ref": "refs/tags/v" + self.version + "-other",
              "object": {"type": "commit", "sha": "not-a-sha"}}],
            [self.matching_ref("-other"), self.matching_ref("-other")],
            {"__raw__": '[{"ref":"unused","ref":"duplicate"}]'},
            {"__raw__": '[{"extra":NaN}]'},
        ):
            with self.subTest(response=response):
                self.configure(matching_refs=response, calls=[])
                self.invoke("publish")
                self.assertEqual([c["method"] for c in self.state()["calls"]],
                                 ["GET", "GET"])
                self.assertFalse(any(kind == "git" and args[0] == "ls-remote"
                                     for kind, args in self.events()))
                self.assert_retained()
                self.clear_events()

    def test_api_exact_matching_ref_collision_blocks_preflight_and_publish(self):
        for command in ("preflight", "publish"):
            for kind in ("commit", "tag"):
                with self.subTest(command=command, kind=kind):
                    if command == "publish":
                        self.build()
                    self.configure(matching_refs=[self.matching_ref(kind=kind)], calls=[])
                    self.invoke(command)
                    self.assertEqual([c["method"] for c in self.state()["calls"]],
                                     ["GET", "GET"])
                    self.assert_retained()

    def test_prefix_matching_refs_are_not_exact_tag_collisions(self):
        self.build()
        self.configure(matching_refs=[
            self.matching_ref("-other"),
            self.matching_ref(".backup", kind="tag"),
        ])
        self.invoke("publish", success=True)
        self.assert_retained(tag=True, release=True, uploads=2)

    def test_existing_remote_tag_is_never_reused_even_at_same_sha(self):
        self.build()
        self.git("--git-dir=" + str(self.origin), "update-ref",
                 "refs/tags/v" + self.version, self.sha)
        self.invoke("publish")
        self.assertEqual([c["method"] for c in self.state()["calls"]], ["GET", "GET"])
        self.assert_retained(tag=True)

    def test_existing_tag_and_draft_are_never_updated(self):
        self.build()
        self.git("--git-dir=" + str(self.origin), "update-ref",
                 "refs/tags/v" + self.version, self.sha)
        self.configure(pages={"1": [
            {"id": 4, "tag_name": "v" + self.version, "draft": True},
        ]})
        self.invoke("publish")
        self.assertEqual([c["method"] for c in self.state()["calls"]], ["GET"])
        self.assert_retained(tag=True)

    def test_unreachable_origin_fails_closed_before_post(self):
        self.build()
        self.git("remote", "set-url", "origin", str(self.base / "missing.git"))
        self.invoke("publish")
        self.assertEqual([c["method"] for c in self.state()["calls"]], ["GET", "GET"])
        self.assert_retained()

    def test_tag_post_failure_is_not_retried(self):
        self.build()
        self.configure(tag_failure="network")
        self.invoke("publish")
        self.assertEqual([c["method"] for c in self.state()["calls"]],
                         ["GET", "GET", "POST"])
        self.assert_retained()

    def test_same_sha_tag_creation_race_retains_tag_without_release(self):
        self.build()
        self.configure(tag_failure="same-sha-race")
        self.invoke("publish")
        self.assertEqual([c["method"] for c in self.state()["calls"]],
                         ["GET", "GET", "POST"])
        self.assert_retained(tag=True)

    def test_malformed_tag_response_retains_created_tag(self):
        self.build()
        self.configure(tag_response={"ref": "refs/tags/not-selected",
                                     "object": {"type": "commit", "sha": self.sha}})
        self.invoke("publish")
        self.assert_retained(tag=True)

    def test_tag_post_requires_commit_object_and_selected_exact_sha(self):
        self.build()
        ref = "refs/tags/v" + self.version
        for response in (
            {"ref": ref, "object": {"sha": self.sha}},
            {"ref": ref, "object": {"type": "tag", "sha": self.sha}},
            {"ref": ref, "object": {"type": "commit", "sha": "a" * 40}},
            {"ref": ref, "object": None},
            {"ref": ref, "object": []},
            {"__raw__": '{"ref":"x","ref":"y"}'},
            {"__raw__": '{"extra":NaN}'},
        ):
            with self.subTest(response=response):
                self.reset_remote_scenario(tag_response=response)
                self.invoke("publish")
                self.assertEqual([c["method"] for c in self.state()["calls"]],
                                 ["GET", "GET", "POST"])
                self.assert_retained(tag=True)

    def test_post_tag_authenticated_ref_verification_failure_retains_tag(self):
        self.build()
        ref = "refs/tags/v" + self.version
        for response in (
            "auth", "404", "network", "malformed-json",
            {"ref": ref, "object": {"type": "tag", "sha": self.sha}},
            {"ref": ref, "object": {"type": "commit", "sha": "a" * 40}},
            {"ref": ref + "-other", "object": {"type": "commit", "sha": self.sha}},
            {"ref": ref, "object": {"sha": self.sha}},
            {"__raw__": '{"ref":"x","ref":"y"}'},
            {"__raw__": '{"extra":NaN}'},
        ):
            with self.subTest(response=response):
                self.reset_remote_scenario(verify_response=response)
                self.invoke("publish")
                self.assertEqual([c["method"] for c in self.state()["calls"]],
                                 ["GET", "GET", "POST", "GET"])
                self.assertEqual(self.state()["calls"][-1]["endpoint"],
                                 f"repos/{REPOSITORY}/git/ref/tags/v{self.version}")
                self.assert_retained(tag=True)

    def test_prior_release_id_cannot_be_reused_for_new_release(self):
        self.build()
        self.configure(pages={
            "1": [{"id": 1, "tag_name": "unrelated-public", "draft": False}],
            "2": [{"id": 701, "tag_name": "unrelated-draft", "draft": True}],
            "3": [],
        })
        self.invoke("publish")
        self.assert_retained(tag=True, release=True)
        self.assertEqual([c["method"] for c in self.state()["calls"]],
                         ["GET", "GET", "GET", "GET", "POST", "GET", "POST"])

    def test_strict_json_applies_after_remote_mutations_without_rollback(self):
        self.build()
        for stage, response, release, uploads in (
            ("release_response", {"__raw__": '{"id":701,"id":702}'}, True, 0),
            ("release_response", {"__raw__": '{"extra":NaN}'}, True, 0),
            ("asset_response_raw", '{"id":801,"id":802}', True, 1),
            ("asset_response_raw", '{"extra":NaN}', True, 1),
        ):
            with self.subTest(stage=stage, response=response):
                self.reset_remote_scenario()
                state = self.state()
                state.pop("release_response", None)
                state.pop("asset_response_raw", None)
                state[stage] = response
                self.state_path.write_text(json.dumps(state))
                self.invoke("publish")
                self.assert_retained(tag=True, release=release, uploads=uploads)

    def test_release_failure_retains_tag(self):
        self.build()
        self.configure(release_failure=True)
        self.invoke("publish")
        self.assert_retained(tag=True)
        self.assertEqual(len(self.state()["calls"]), 5)

    def test_release_response_requires_strict_integer_id(self):
        self.build()
        for invalid in (True, "701", 701.0, 0, -1, None):
            with self.subTest(id=invalid):
                self.configure(release_response={
                    "id": invalid, "tag_name": "v" + self.version,
                    "name": "v" + self.version, "target_commitish": self.sha,
                    "draft": False, "prerelease": False, "body": "",
                }, calls=[])
                self.invoke("publish")
                self.assert_retained(tag=True, release=True)
                # Reset only the disposable remote between independent scenarios.
                self.git("--git-dir=" + str(self.origin), "update-ref", "-d",
                         "refs/tags/v" + self.version)

    def test_malformed_new_release_response_retains_release_without_upload(self):
        self.build()
        expected = {
            "id": 701, "tag_name": "v" + self.version, "name": "v" + self.version,
            "target_commitish": self.sha, "draft": False, "prerelease": False,
            "body": "",
        }
        for key, value in (
            ("tag_name", "wrong"), ("name", "wrong"), ("target_commitish", "b" * 40),
            ("draft", True), ("draft", 0), ("prerelease", True), ("body", "unexpected"),
        ):
            with self.subTest(key=key, value=value):
                self.configure(release_response=dict(expected, **{key: value}), calls=[])
                self.invoke("publish")
                self.assert_retained(tag=True, release=True)
                self.git("--git-dir=" + str(self.origin), "update-ref", "-d",
                         "refs/tags/v" + self.version)

    def test_first_upload_failure_retains_tag_and_release(self):
        self.build()
        self.configure(asset_failure=1)
        self.invoke("publish")
        self.assert_retained(tag=True, release=True)
        self.assertEqual(len(self.state()["calls"]), 6)

    def test_second_upload_failure_retains_first_asset(self):
        self.build()
        self.configure(asset_failure=2)
        self.invoke("publish")
        self.assert_retained(tag=True, release=True, uploads=1)
        self.assertEqual(len(self.state()["calls"]), 7)

    def test_asset_response_exact_name_size_state_and_integer_id(self):
        self.build()
        size = (self.work / f"{PRODUCT}-{self.version}.zip").stat().st_size
        for update in (
            {"id": True}, {"id": "801"}, {"id": 801.0}, {"id": 0},
            {"name": "wrong.zip"}, {"size": True}, {"size": str(size)},
            {"size": float(size)}, {"size": size + 1}, {"state": "new"},
        ):
            with self.subTest(update=update):
                self.configure(asset_response=update, calls=[], uploads=[])
                self.invoke("publish")
                self.assert_retained(tag=True, release=True, uploads=1)
                self.assertEqual(len(self.state()["calls"]), 6)
                self.git("--git-dir=" + str(self.origin), "update-ref", "-d",
                         "refs/tags/v" + self.version)

    def test_intake_history_is_immutable_and_incoherent_source_is_rejected(self):
        # The intake really contains stale localization; never "repair" history
        # to manufacture a coherent legacy release fixture.
        historical = {}
        for relative in ("Info.plist", "English.lproj/InfoPlist.strings",
                         f"{PRODUCT}.xcodeproj/project.pbxproj"):
            result = subprocess.run(
                [self.real_git, "-C", str(ROOT), "show", f"{INTAKE}:{relative}"],
                capture_output=True, env=self.env,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            historical[relative] = result.stdout
            (self.work / relative).write_bytes(result.stdout)
        self.git("add", "--all")
        self.commit("Unmodified intake metadata fixture")
        self.env["GITHUB_SHA"] = self.git("rev-parse", "HEAD").stdout.strip()
        self.invoke("initial")
        self.assert_no_api()
        self.assertEqual(historical, {
            p: (self.work / p).read_bytes() for p in historical
        })


class ReleaseWorkflow(unittest.TestCase):
    def test_current_workflow_uses_immutable_source_and_cli_guards(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("PYTHONDONTWRITEBYTECODE: '1'", workflow)
        self.assertEqual(workflow.count("ref: ${{ github.sha }}"), 3)
        self.assertIn("release.py preflight", workflow)
        self.assertLess(workflow.index("release.py initial"),
                        workflow.index("native.sh clean"))
        self.assertLess(workflow.index("native.sh package"),
                        workflow.index("release.py publish"))
        for forbidden in ("gh release create", "gh release upload", "--clobber",
                          "git tag ", "git push ", "bump-version"):
            self.assertNotIn(forbidden, workflow)


if __name__ == "__main__":
    unittest.main()
