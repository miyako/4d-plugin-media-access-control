import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INTAKE = "7fc9a40f19c26fcd5305e2238b02a8f347ccaff0"


def original(path):
    return subprocess.check_output(
        ["git", "show", f"{INTAKE}:{path}"], cwd=ROOT, text=True
    )


class WorkflowTests(unittest.TestCase):
    def test_intake_topology_and_authority(self):
        bump = original(".github/workflows/bump-version.yml")
        build = original(".github/workflows/release.yml")
        self.assertIn("MARKETING_VERSION/CURRENT_PROJECT_VERSION", bump)
        self.assertIn('git tag "v${NEW}"', bump)
        self.assertIn("tags:\n      - 'v*.*.*'", build)
        self.assertIn("build-windows:", build)
        self.assertIn("needs: [version, build-windows]", build)
        self.assertIn("uses: softprops/action-gh-release@v2", build)
        self.assertNotIn("generate_release_notes:", build)
        self.assertNotIn("body:", build)

    def test_manual_interfaces_and_immutable_checkouts(self):
        workflows = ROOT / ".github/workflows"
        self.assertEqual(
            sorted(p.name for p in workflows.glob("*.yml")
                   if re.search(r"^name: (?:Bump|Build)\s*$",
                                p.read_text(), re.M)),
            ["bump-version.yml", "release.yml"],
        )
        bump = (workflows / "bump-version.yml").read_text()
        build = (workflows / "release.yml").read_text()
        self.assertTrue(bump.startswith("name: Bump\n"))
        self.assertTrue(build.startswith("name: Build\n"))
        self.assertIn("options: [patch, minor, major]", bump)
        self.assertIn("required: true", bump)
        self.assertIn("default: patch", bump)
        self.assertNotIn("inputs:", build)
        for text in (bump, build):
            self.assertEqual(re.findall(r"^  (\w+):", text.split("permissions:")[0],
                                        re.M), ["workflow_dispatch"])
            self.assertNotIn("ref: ${{ github.ref", text)
            self.assertEqual(
                text.count("uses: actions/checkout@"),
                text.count("ref: ${{ github.sha }}"),
            )
        self.assertIn("group: source-release-publication", build)
        self.assertIn("cancel-in-progress: false", build)
        self.assertNotIn("github.ref", build)
        self.assertNotIn("needs: version", bump)
        self.assertNotIn("release.py", bump)

    def test_native_topology_and_original_cmd_are_preserved(self):
        build = (ROOT / ".github/workflows/release.yml").read_text()
        before = original(".github/workflows/release.yml")
        cmd = ('msbuild "%PRODUCT_NAME%.sln" /p:Configuration=Release '
               '/p:Platform=x64 /p:PlatformToolset=v143 '
               '/p:WindowsTargetPlatformVersion=10.0 /m')
        self.assertIn(cmd, before)
        self.assertIn(cmd, build)
        self.assertIn("shell: cmd", build)
        self.assertIn("needs: [version, build-windows]", build)
        self.assertIn("MACOS_DEPLOYMENT_TARGET: '10.13'", build)
        self.assertEqual(build.count("runs-on: windows-latest"), 1)
        self.assertEqual(build.count("runs-on: macos-latest"), 1)
        self.assertLess(build.index("release.py preflight"),
                        build.index("build-windows:"))
        self.assertLess(build.index("native.sh package"),
                        build.index("release.py publish"))

    def test_unrelated_native_inputs_are_unchanged(self):
        for path in [
            "Media Access Control.vcxproj", "Media Access Control.sln",
            "4DPlugin-Media-Access-Control.cpp", "manifest.json", "constants.xlf",
        ]:
            data = subprocess.check_output(["git", "show", f"{INTAKE}:{path}"],
                                           cwd=ROOT)
            self.assertEqual((ROOT / path).read_bytes(), data, path)
        project = (ROOT / "Media Access Control.xcodeproj/project.pbxproj").read_bytes()
        old = subprocess.check_output(
            ["git", "show", f"{INTAKE}:Media Access Control.xcodeproj/project.pbxproj"],
            cwd=ROOT,
        )
        strip = lambda data: re.sub(
            rb"((?:MARKETING_VERSION|CURRENT_PROJECT_VERSION) = )[^;]+;",
            rb"\1VERSION;", data)
        self.assertEqual(strip(project), strip(old))

    def test_intake_alignment_preserves_source_and_prebuilt_bytes(self):
        project = (ROOT / "Media Access Control.xcodeproj/project.pbxproj").read_bytes()
        versions = set(re.findall(rb"MARKETING_VERSION = ([0-9.]+);", project))
        self.assertEqual(len(versions), 1)
        version = versions.pop()
        localized = "English.lproj/InfoPlist.strings"
        original_localized = subprocess.check_output(
            ["git", "show", f"{INTAKE}:{localized}"], cwd=ROOT)
        self.assertEqual((ROOT / localized).read_bytes(),
                         original_localized.replace(b"2.1.0", version))
        self.assertEqual((ROOT / localized).stat().st_mode & 0o777, 0o755)
        self.assertEqual(
            (ROOT / "Info.plist").read_bytes(),
            subprocess.check_output(["git", "show", f"{INTAKE}:Info.plist"], cwd=ROOT),
        )
        allowed = {
            ".github/workflows/bump-version.yml", ".github/workflows/release.yml",
            "Media Access Control.xcodeproj/project.pbxproj", localized, "README.md",
        }
        entries = subprocess.check_output(
            ["git", "ls-tree", "-rz", INTAKE], cwd=ROOT).split(b"\0")
        for entry in entries:
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            path = path.decode()
            if path in allowed:
                continue
            mode, kind, oid = metadata.decode().split()
            self.assertEqual(kind, "blob")
            self.assertEqual((ROOT / path).read_bytes(),
                             subprocess.check_output(["git", "cat-file", "blob", oid],
                                                     cwd=ROOT), path)
            self.assertEqual(bool((ROOT / path).stat().st_mode & 0o100),
                             mode == "100755", path)


if __name__ == "__main__":
    unittest.main()
