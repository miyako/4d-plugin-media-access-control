"""Offline execution tests: no Xcode, MSBuild, signing, network, or credentials.

Run: python3 -m unittest discover -s tests -p test_native.py -v
Fixtures live under this checkout and all external Bash tools use a closed PATH.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / ".github/scripts/native.sh"
WINDOWS = ROOT / ".github/scripts/windows.ps1"
PRODUCT = "Media Access Control"
PWSH = Path("/usr/local/bin/pwsh")

FAKE_TOOL = r'''
import base64, json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path.cwd()
with open(os.environ["TOOL_LOG"], "a") as log:
    log.write(json.dumps([name, args]) + "\n")
if name == os.environ.get("FAIL_TOOL") and os.environ.get("FAIL_MATCH", "") in " ".join(args):
    with open(os.environ["TOOL_LOG"]) as log:
        count = sum(json.loads(line)[0] == name for line in log)
    if "FAIL_NTH" not in os.environ or count == int(os.environ["FAIL_NTH"]):
        sys.exit(23)
bundle = root / "test/Plugins/Media Access Control.bundle"
binary = bundle / "Contents/MacOS/Media Access Control"
if name == "rm":
    for arg in args:
        if arg.startswith("-"):
            continue
        path = pathlib.Path(arg)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
elif name == "mkdir":
    for arg in args:
        if not arg.startswith("-"):
            pathlib.Path(arg).mkdir(parents=True, exist_ok=True)
elif name == "cp":
    shutil.copyfile(*args)
elif name == "xcodebuild":
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("fresh universal binary")
elif name == "lipo":
    print(os.environ.get("LIPO_ARCHS", "arm64 x86_64"))
elif name == "base64":
    pathlib.Path(args[args.index("-o") + 1]).write_bytes(base64.b64decode(sys.stdin.read(), validate=True))
elif name == "security" and args[:1] == ["find-identity"]:
    if not os.environ.get("NO_IDENTITY"):
        print('  1) ABCDEF "Developer ID Application: Offline Fixture (TEST)"')
elif name in ("ditto", "hdiutil"):
    pathlib.Path(args[-1]).write_text("offline archive")
elif name == "xcrun" and args[:2] == ["notarytool", "submit"]:
    extension = pathlib.Path(args[2]).suffix[1:].upper()
    status = os.environ.get(extension + "_STATUS", "Accepted")
    if status == "malformed":
        print("not JSON")
    else:
        print(json.dumps({"status": status, "id": "offline-only"}))
    sys.exit(int(os.environ.get(extension + "_EXIT", "0")))
'''


def workflow_run(step_name):
    """Extract the actual run scalar from the repository's workflow YAML subset."""
    lines = (ROOT / ".github/workflows/release.yml").read_text().splitlines()
    marker = f"      - name: {step_name}"
    if lines.count(marker) != 1:
        raise AssertionError(f"Expected one workflow step named {step_name!r}")
    start = lines.index(marker) + 1
    for index in range(start, len(lines)):
        line = lines[index]
        if line.startswith("      - "):
            break
        if line.startswith("        run: "):
            value = line[len("        run: "):]
            if value != "|":
                return value + "\n"
            body = []
            for block_line in lines[index + 1:]:
                if block_line and not block_line.startswith("          "):
                    break
                body.append(block_line[10:] if block_line else "")
            return "\n".join(body) + "\n"
    raise AssertionError(f"No supported run block in {step_name!r}")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix=".native-fixture-", dir=ROOT)
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.bin = self.root / "closed path"
        self.bin.mkdir()
        for name in ("rm", "mkdir", "cp", "xcodebuild", "lipo", "security",
                     "codesign", "ditto", "hdiutil", "xcrun", "base64"):
            tool = self.bin / name
            tool.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            tool.chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)
        (self.bin / "bash").symlink_to("/bin/bash")
        for script in (NATIVE, WINDOWS):
            destination = self.root / ".github/scripts" / script.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(script, destination)
        self.log = self.root / "tool log.jsonl"
        # Deliberately do not inherit signing/notary secrets or user tool paths.
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root),
            "PRODUCT_NAME": PRODUCT,
            "BASE_DIR": "test",
            "MACOS_DEPLOYMENT_TARGET": "10.13",
            "VERSION": "2.1.1",
            "KEYCHAIN_PATH": str(self.root / "offline.keychain"),
            "TOOL_LOG": str(self.log),
            "GITHUB_ENV": str(self.root / "github env"),
            "TEST_WINDOWS_SCRIPT": str(WINDOWS),
            "POWERSHELL_TELEMETRY_OPTOUT": "1",
            "POWERSHELL_UPDATECHECK": "Off",
            "DOTNET_CLI_HOME": str(self.root),
        }
        self.runner = self.root / "runner scratch"
        self.runner.mkdir()
        self.env.update({
            "RUNNER_TEMP": str(self.runner),
            "CERT_BASE64": "b2ZmbGluZSBmaXh0dXJlIGNlcnRpZmljYXRl",
            "CERT_PASSWORD": "fake-certificate-password",
            "KEYCHAIN_PASSWORD": "fake-keychain-password",
            "APPLE_ID": "offline@example.invalid",
            "TEAM_ID": "OFFLINEONLY",
            "APP_PASSWORD": "fake-app-password",
        })
        self.bundle = self.root / f"test/Plugins/{PRODUCT}.bundle"
        self.binary = self.bundle / f"Contents/MacOS/{PRODUCT}"
        self.windows = self.bundle / f"Contents/Windows64/{PRODUCT}.4DX"
        self.generated = [
            self.binary,
            self.bundle / "Contents/Info.plist",
            self.bundle / "Contents/Resources/English.lproj/InfoPlist.strings",
            self.bundle / "Contents/_CodeSignature/CodeResources",
            self.root / f"build/{PRODUCT}.build/Default/old.o",
            self.root / f"test/Plugins/{PRODUCT}.build/Default/old.o",
            self.root / f"test/Plugins/Default/{PRODUCT}.build/old.o",
        ]
        self.preserved = [
            self.windows,
            self.bundle / "Contents/Resources/manifest.json",
            self.bundle / "Contents/Resources/constants.xlf",
            self.bundle / "Contents/Resources/English.lproj/keep.txt",
            self.bundle / "Contents/QuickLook/Thumbnail.png",
            self.bundle / "Contents/Frameworks/fixture.dylib",
            self.root / "test/Plugins/Other.bundle/sentinel",
            self.root / "test/Plugins/Default/sentinel",
            self.root / "build/Other.build/sentinel",
            self.root / "a/libprebuilt.a",
            self.root / "lib/libdependency.a",
            self.root / "English.lproj/InfoPlist.strings",
            self.root / "Info.plist",
            self.root / "Objs/Debug/keep.obj",
        ]
        for path in self.generated + self.preserved:
            self.write(path, "preserve sentinel" if path in self.preserved else "stale")

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def calls(self, name=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [args for tool, args in calls if tool == name] if name else calls

    def run_native(self, action, success=True, **overrides):
        result = subprocess.run(
            ["/bin/bash", str(NATIVE), action], cwd=self.root,
            env=dict(self.env, **overrides), text=True, capture_output=True,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def run_workflow_bash(self, step_name, success=True, **overrides):
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
             workflow_run(step_name)], cwd=self.root,
            env=dict(self.env, **overrides), text=True, capture_output=True,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def assert_preserved(self):
        for path in self.preserved:
            self.assertEqual(path.read_text(), "preserve sentinel", str(path))


class BashTests(Fixture):
    def test_selective_clean_build_verify_and_merge(self):
        self.run_native("clean")
        for path in self.generated:
            self.assertFalse(path.exists(), str(path))
        self.assert_preserved()
        deleted = [args[-1] for args in self.calls("rm")]
        self.assertEqual(set(deleted), {
            str(self.binary), str(self.bundle / "Contents/Info.plist"),
            str(self.bundle / "Contents/Resources/English.lproj/InfoPlist.strings"),
            str(self.bundle / "Contents/_CodeSignature"),
            str(self.root / f"build/{PRODUCT}.build"),
            str(self.root / f"test/Plugins/{PRODUCT}.build"),
            str(self.root / f"test/Plugins/Default/{PRODUCT}.build"),
        })
        self.run_native("build")
        args, = self.calls("xcodebuild")
        self.assertEqual(args, [
            "-project", f"{PRODUCT}.xcodeproj", "-target", PRODUCT,
            "-configuration", "Default", "ARCHS=arm64 x86_64",
            "ONLY_ACTIVE_ARCH=NO", "MACOSX_DEPLOYMENT_TARGET=10.13",
            "CODE_SIGNING_ALLOWED=NO",
            f"CONFIGURATION_BUILD_DIR={self.root}/test/Plugins",
            f"OBJROOT={self.root}/build", "build",
        ])
        self.assertEqual(self.binary.read_text(), "fresh universal binary")
        self.run_native("verify")
        artifact = self.root / f"winbuild/{PRODUCT}.4DX"
        self.write(artifact, "fresh x64")
        self.run_native("merge")
        self.assertEqual(self.windows.read_text(), "fresh x64")
        self.assertIn(f"BUNDLE_PATH=test/Plugins/{PRODUCT}.bundle",
                      Path(self.env["GITHUB_ENV"]).read_text())

    def test_architecture_tokens_and_extras(self):
        self.write(self.root / "arm64", "not an architecture report")
        self.write(self.root / "x86_64", "not an architecture report")
        for archs in ("arm64e x86_64", "arm64 x86_64h", "arm64e x86_64h", "arm64", "*"):
            with self.subTest(archs=archs):
                self.run_native("verify", success=False, LIPO_ARCHS=archs)
                self.run_native("build", success=False, ARCHS=archs)
        self.assertEqual(self.calls("xcodebuild"), [])
        self.run_native("verify", LIPO_ARCHS="i386 x86_64 arm64 arm64e")
        self.run_native("build", ARCHS="x86_64 arm64 i386")
        self.assertIn("ARCHS=x86_64 arm64 i386", self.calls("xcodebuild")[-1])

    def test_missing_empty_and_noncanonical_outputs(self):
        self.binary.unlink()
        self.write(self.root / f"test/Plugins/Default/{PRODUCT}.bundle/Contents/MacOS/{PRODUCT}", "wrong")
        self.run_native("verify", success=False)
        self.write(self.binary, "")
        self.run_native("verify", success=False)
        self.run_native("merge", success=False)
        self.write(self.root / f"winbuild/nested/{PRODUCT}.4DX", "wrong")
        self.run_native("merge", success=False)
        self.assertEqual(self.calls("cp"), [])

    def test_symlinks_and_unsafe_overrides_fail_closed(self):
        self.binary.unlink()
        self.binary.symlink_to(self.root / "Info.plist")
        self.run_native("clean", success=False)
        self.assert_preserved()
        for overrides in ({"BASE_DIR": ".."}, {"PRODUCT_NAME": "../elsewhere"},
                          {"BUNDLE_PATH": "other.bundle"},
                          {"MACOS_DEPLOYMENT_TARGET": "11.5"}):
            self.run_native("build", success=False, **overrides)
        self.assertEqual(self.calls("xcodebuild"), [])
        self.run_native("unknown", success=False)

    def test_cleanup_and_native_failures_are_fatal(self):
        self.run_native("clean", success=False, FAIL_TOOL="rm")
        self.assertTrue(self.binary.exists())
        self.assert_preserved()
        self.run_native("build", success=False, FAIL_TOOL="xcodebuild")
        self.run_native("verify", success=False, FAIL_TOOL="lipo")
        self.write(self.root / f"winbuild/{PRODUCT}.4DX", "fresh")
        self.run_native("merge", success=False, FAIL_TOOL="cp")

    def test_signing_preserves_runtime_policy_and_strict_verification(self):
        self.run_native("sign")
        self.assertEqual(self.calls("codesign"), [
            ["--verbose", "--deep", "--timestamp", "--force", "--options=runtime",
             "--sign", "Developer ID Application: Offline Fixture (TEST)", str(self.bundle)],
            ["--verify", "--deep", "--strict", "--verbose=2", str(self.bundle)],
        ])
        self.assert_preserved()

    def test_signing_identity_and_command_failures(self):
        for overrides in (
            {"NO_IDENTITY": "1"}, {"FAIL_TOOL": "security"},
            {"FAIL_TOOL": "codesign", "FAIL_MATCH": "--sign"},
            {"FAIL_TOOL": "codesign", "FAIL_MATCH": "--verify"},
        ):
            with self.subTest(overrides=overrides):
                self.run_native("sign", success=False, **overrides)

    def test_package_both_acceptances_stapling_and_strict_verification(self):
        self.run_native("package", BUNDLE_PATH=f"test/Plugins/{PRODUCT}.bundle")
        calls = self.calls("xcrun")
        self.assertEqual([args[:2] for args in calls], [
            ["notarytool", "submit"], ["notarytool", "submit"],
            ["stapler", "staple"], ["stapler", "validate"],
        ])
        for args, suffix in zip(calls[:2], ("zip", "dmg")):
            self.assertEqual(args[2], str(self.root / f"{PRODUCT}-2.1.1.{suffix}"))
            self.assertIn("--wait", args)
            self.assertEqual(args[-2:], ["--output-format", "json"])
        self.assertEqual(len(self.calls("codesign")), 2)
        for args in self.calls("codesign"):
            self.assertIn("--strict", args)
        exported = Path(self.env["GITHUB_ENV"]).read_text()
        self.assertIn(f"ZIP_PATH={PRODUCT}-2.1.1.zip", exported)
        self.assertIn(f"DMG_PATH={PRODUCT}-2.1.1.dmg", exported)
        self.assert_preserved()

    def test_rejected_malformed_and_nonzero_notarizations_never_staple(self):
        cases = [
            {"ZIP_STATUS": "Invalid"}, {"DMG_STATUS": "Rejected"},
            {"ZIP_STATUS": "malformed"}, {"DMG_STATUS": "malformed"},
            {"ZIP_STATUS": "accepted"}, {"DMG_STATUS": "In Progress"},
            {"ZIP_EXIT": "17"}, {"DMG_EXIT": "17"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.log.unlink(missing_ok=True)
                self.run_native("package", success=False, **overrides)
                self.assertFalse(any(args[:1] == ["stapler"] for args in self.calls("xcrun")))
                self.assertFalse(Path(self.env["GITHUB_ENV"]).exists())

    def test_archive_staple_and_strict_verification_failures(self):
        for tool, match in (
            ("ditto", ""), ("hdiutil", ""), ("xcrun", "stapler staple"),
            ("xcrun", "stapler validate"), ("codesign", "--verify"),
        ):
            with self.subTest(tool=tool, match=match):
                self.run_native("package", success=False, FAIL_TOOL=tool, FAIL_MATCH=match)
                self.assertFalse(Path(self.env["GITHUB_ENV"]).exists())
        self.run_native("package", success=False, VERSION="../unsafe")

    def test_post_staple_strict_verification_is_required(self):
        self.run_native("package", success=False, FAIL_TOOL="codesign", FAIL_NTH="2")
        self.assertIn(["stapler", "validate", str(self.root / f"{PRODUCT}-2.1.1.dmg")],
                      self.calls("xcrun"))
        self.assertFalse(Path(self.env["GITHUB_ENV"]).exists())


class WorkflowBashTests(Fixture):
    def test_actual_native_run_blocks_end_to_end(self):
        self.run_workflow_bash("Build fresh canonical macOS output")
        self.assertEqual(self.binary.read_text(), "fresh universal binary")
        self.assert_preserved()
        self.assertEqual(len(self.calls("xcodebuild")), 1)
        self.assertEqual(len(self.calls("lipo")), 1)
        self.write(self.root / f"winbuild/{PRODUCT}.4DX", "fresh Windows artifact")
        self.run_workflow_bash("Merge Windows output")
        self.assertEqual(self.windows.read_text(), "fresh Windows artifact")
        self.run_workflow_bash("Sign")
        self.run_workflow_bash("Package, notarize and staple")
        self.assertEqual([args[:2] for args in self.calls("xcrun")], [
            ["notarytool", "submit"], ["notarytool", "submit"],
            ["stapler", "staple"], ["stapler", "validate"],
        ])
        self.assertTrue((self.root / f"{PRODUCT}-2.1.1.zip").is_file())
        self.assertTrue((self.root / f"{PRODUCT}-2.1.1.dmg").is_file())

    def test_actual_native_workflow_blocks_fail_fatally(self):
        for tool in ("rm", "xcodebuild", "lipo"):
            with self.subTest(tool=tool):
                self.log.unlink(missing_ok=True)
                self.run_workflow_bash("Build fresh canonical macOS output",
                                       success=False, FAIL_TOOL=tool)
                tools = [name for name, _ in self.calls()]
                if tool == "rm":
                    self.assertNotIn("xcodebuild", tools)
                    self.assertNotIn("lipo", tools)
                elif tool == "xcodebuild":
                    self.assertNotIn("lipo", tools)
        self.run_native("build")
        self.write(self.root / f"winbuild/{PRODUCT}.4DX", "fresh Windows artifact")
        for step, tool, match in (
            ("Merge Windows output", "cp", ""),
            ("Sign", "codesign", "--sign"),
            ("Package, notarize and staple", "xcrun", "notarytool submit"),
        ):
            with self.subTest(step=step):
                self.run_workflow_bash(step, success=False, FAIL_TOOL=tool, FAIL_MATCH=match)

    def test_actual_certificate_import_and_notary_store_blocks(self):
        self.run_workflow_bash("Import Developer ID certificate")
        certificate = self.runner / "certificate.p12"
        keychain = str(self.runner / "build.keychain-db")
        self.assertEqual(certificate.read_bytes(), b"offline fixture certificate")
        self.assertEqual(self.calls("base64"), [["--decode", "-o", str(certificate)]])
        self.assertEqual(self.calls("security"), [
            ["create-keychain", "-p", "fake-keychain-password", keychain],
            ["set-keychain-settings", "-lut", "21600", keychain],
            ["unlock-keychain", "-p", "fake-keychain-password", keychain],
            ["import", str(certificate), "-P", "fake-certificate-password",
             "-A", "-t", "cert", "-f", "pkcs12", "-k", keychain],
            ["list-keychain", "-d", "user", "-s", keychain, "login.keychain"],
            ["set-key-partition-list", "-S", "apple-tool:,apple:,codesign:",
             "-s", "-k", "fake-keychain-password", keychain],
        ])
        self.assertEqual(Path(self.env["GITHUB_ENV"]).read_text(),
                         f"KEYCHAIN_PATH={keychain}\n")
        self.run_workflow_bash("Store notarytool credentials", KEYCHAIN_PATH=keychain)
        self.assertEqual(self.calls("xcrun"), [[
            "notarytool", "store-credentials", "notarytool-profile",
            "--apple-id", "offline@example.invalid", "--team-id", "OFFLINEONLY",
            "--password", "fake-app-password", "--keychain", keychain,
        ]])

    def test_every_import_or_credential_error_aborts_actual_block(self):
        security_operations = [
            "create-keychain", "set-keychain-settings", "unlock-keychain",
            "import", "list-keychain", "set-key-partition-list",
        ]
        for tool, match in [("base64", "")] + [("security", op) for op in security_operations]:
            with self.subTest(tool=tool, match=match):
                self.log.unlink(missing_ok=True)
                self.run_workflow_bash("Import Developer ID certificate", success=False,
                                       FAIL_TOOL=tool, FAIL_MATCH=match)
                self.assertFalse(Path(self.env["GITHUB_ENV"]).exists())
                security = self.calls("security")
                if tool == "base64":
                    self.assertEqual(security, [])
                else:
                    self.assertEqual([args[0] for args in security],
                                     security_operations[:security_operations.index(match) + 1])
        self.run_workflow_bash("Store notarytool credentials", success=False,
                               FAIL_TOOL="xcrun", FAIL_MATCH="store-credentials")


@unittest.skipUnless(PWSH.is_file(), "PowerShell executable /usr/local/bin/pwsh is unavailable")
class PowerShellTests(Fixture):
    def run_windows(self, action, success=True, fail_remove=False, fail_match="", block=None):
        # Wrap the real cmdlet only to log exact paths or inject a deterministic
        # Remove-Item error (permissions vary when tests run as an administrator).
        command = r'''
function global:Remove-Item {
    [CmdletBinding()]
    param([string] $LiteralPath, [switch] $Recurse, [switch] $Force)
    Add-Content -LiteralPath $env:REMOVE_LOG -Value $LiteralPath
    if ($env:FAIL_REMOVE -eq '1' -and
        $LiteralPath.Contains($env:FAIL_REMOVE_MATCH)) {
        throw 'injected Remove-Item failure'
    }
    Microsoft.PowerShell.Management\Remove-Item @PSBoundParameters
}
'''
        command += "\ntry {\n" + (block if block is not None else
                                  "& $env:TEST_WINDOWS_SCRIPT -Action $env:WINDOWS_ACTION") + r'''
} catch {
    [Console]::Error.WriteLine($_)
    exit 1
}
'''
        result = subprocess.run(
            [str(PWSH), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=self.root, env=dict(self.env, WINDOWS_ACTION=action,
                                   FAIL_REMOVE="1" if fail_remove else "0",
                                   FAIL_REMOVE_MATCH=fail_match,
                                   REMOVE_LOG=str(self.root / "removed paths")),
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_case_insensitive_exact_cleanup_and_verify(self):
        # Deliberately mix directory and filename case, including on macOS/Linux.
        self.windows.unlink()
        mixed = self.bundle / "Contents/Windows64/mEDIA aCCESS cONTROL.4dx"
        self.write(mixed, "fresh x64")
        intermediate = self.root / "oBJs/rELEASE/target.obj"
        self.write(intermediate, "stale")
        self.run_windows("verify")
        self.run_windows("clean")
        self.assertFalse(mixed.exists())
        self.assertFalse(intermediate.exists())
        for path in self.preserved:
            if path != self.windows:
                self.assertEqual(path.read_text(), "preserve sentinel")
        for path in self.generated:
            self.assertTrue(path.exists())
        removed = (self.root / "removed paths").read_text().splitlines()
        self.assertEqual([path.casefold() for path in removed],
                         [str(mixed).casefold(), str(intermediate.parent).casefold()])
        self.run_windows("verify", success=False)

    def test_no_recursive_first_match_or_empty_output(self):
        self.windows.unlink()
        self.write(self.root / f"other/Plugins/{PRODUCT}.4DX", "decoy")
        self.run_windows("verify", success=False)
        self.write(self.windows, "")
        self.run_windows("verify", success=False)
        self.write(self.windows, "exact output")
        self.run_windows("verify")

    def test_remove_errors_are_fatal_and_preserve_everything(self):
        self.write(self.root / "Objs/Release/target.obj", "stale")
        result = self.run_windows("clean", success=False, fail_remove=True)
        self.assertIn("injected Remove-Item failure", result.stderr)
        self.assert_preserved()
        self.assertTrue((self.root / "Objs/Release/target.obj").exists())
        self.assertEqual((self.root / "removed paths").read_text().splitlines(), [str(self.windows)])

    def test_symlink_output_cannot_escape(self):
        self.windows.unlink()
        self.windows.symlink_to(self.root / "Info.plist")
        self.run_windows("clean", success=False)
        self.assertEqual((self.root / "Info.plist").read_text(), "preserve sentinel")

    def test_intermediate_remove_error_is_fatal(self):
        intermediate = self.root / "Objs/Release/target.obj"
        self.write(intermediate, "stale")
        self.run_windows("clean", success=False, fail_remove=True, fail_match="Release")
        self.assertFalse(self.windows.exists())
        self.assertTrue(intermediate.exists())

    def test_actual_workflow_powershell_clean_and_verify_blocks(self):
        self.write(self.root / "Objs/Release/target.obj", "stale")
        self.run_windows("verify", block=workflow_run("Verify exact vcxproj output"))
        self.run_windows("clean", block=workflow_run("Clean exact Windows outputs"))
        self.assertFalse(self.windows.exists())
        self.assertFalse((self.root / "Objs/Release").exists())
        self.run_windows("verify", success=False, block=workflow_run("Verify exact vcxproj output"))
        for path in self.preserved:
            if path != self.windows:
                self.assertEqual(path.read_text(), "preserve sentinel")
        self.write(self.windows, "fresh x64")
        self.run_windows("clean", success=False, fail_remove=True,
                         block=workflow_run("Clean exact Windows outputs"))
        self.assertEqual(self.windows.read_text(), "fresh x64")


if __name__ == "__main__":
    unittest.main()
