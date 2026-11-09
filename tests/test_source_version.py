"""Coordinated metadata regressions with no native tools or external fixtures."""

import importlib.util
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "source_version", ROOT / ".github/scripts/source_version.py")
source = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault(SPEC.name, source)
SPEC.loader.exec_module(source)


class SourceVersionTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "tests" / (".source-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        for path in source.SOURCE_PATHS:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / path, target)
        fields = source.project_settings(source.decode((self.root / source.PROJECT).read_bytes())[0])
        self.current = fields["MARKETING_VERSION"][0].value
        # Isolate tests from the one-time intake repair owned by the migration.
        project = self.root / source.PROJECT
        project.write_bytes(re.sub(
            rb"(CURRENT_PROJECT_VERSION = )2\.1\.0;",
            lambda match: match[1] + self.current.encode() + b";", project.read_bytes()))
        localized = self.root / source.LOCALIZED
        raw = localized.read_bytes()
        raw = raw.replace(b'CFBundleShortVersionString = "2.1.0";',
                          f'CFBundleShortVersionString = "{self.current}";'.encode())
        raw = raw.replace(b"version 2.1.0,", f"version {self.current},".encode())
        localized.write_bytes(raw)
        self.assertEqual(source.read_version(self.root), self.current)
        self.originals = self.snapshot()

    def snapshot(self):
        return {path: ((self.root / path).read_bytes(),
                       stat.S_IMODE((self.root / path).stat().st_mode))
                for path in source.SOURCE_PATHS if (self.root / path).exists()}

    def reset(self):
        for path, (raw, mode) in self.originals.items():
            (self.root / path).write_bytes(raw)
            (self.root / path).chmod(mode)

    def modify(self, path, transform):
        target = self.root / path
        target.write_bytes(transform(target.read_bytes()))

    def reject(self):
        before = self.snapshot()
        with patch.object(Path, "write_bytes") as write:
            with self.assertRaises((ValueError, UnicodeError, OSError)):
                source.read_version(self.root)
            with self.assertRaises((ValueError, UnicodeError, OSError)):
                source.bump("patch", self.root)
        write.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def plist(self, **fields):
        target = self.root / source.PLIST
        data = plistlib.loads(target.read_bytes())
        data.update(fields)
        target.write_bytes(plistlib.dumps(data))

    def test_actual_six_marketing_three_current_and_optional_build_absence(self):
        self.assertEqual(source.read_version(ROOT), self.current)
        fields = source.project_settings(source.decode((self.root / source.PROJECT).read_bytes())[0])
        self.assertEqual({key: len(values) for key, values in fields.items()},
                         {"MARKETING_VERSION": 6, "CURRENT_PROJECT_VERSION": 3})
        actual = plistlib.loads((self.root / source.PLIST).read_bytes())
        self.assertEqual(actual[source.SHORT], "$(MARKETING_VERSION)")
        self.assertNotIn(source.BUILD, actual)
        raw = (self.root / source.LOCALIZED).read_bytes()
        self.assertIn(b"\r", raw)
        self.assertNotIn(b"\n", raw)

    def test_live_and_stable_all_modes_preserve_every_other_byte_and_mode(self):
        for current in (self.current, "3.2.1"):
            for mode in ("major", "minor", "patch"):
                with self.subTest(current=current, mode=mode):
                    self.reset()
                    for path in source.SOURCE_PATHS:
                        self.modify(path, lambda raw: raw.replace(self.current.encode(), current.encode()))
                        (self.root / path).chmod(0o750)
                    before = self.snapshot()
                    parts = list(map(int, current.split(".")))
                    index = ("major", "minor", "patch").index(mode)
                    parts[index] += 1
                    parts[index + 1:] = [0] * (2 - index)
                    new = ".".join(map(str, parts))
                    self.assertEqual(source.bump(mode, self.root), new)
                    self.assertEqual(source.read_version(self.root), new)
                    for path, (raw, filemode) in before.items():
                        self.assertEqual((self.root / path).read_bytes(),
                                         raw.replace(current.encode(), new.encode()))
                        self.assertEqual(stat.S_IMODE((self.root / path).stat().st_mode), filemode)
                    self.assertNotIn(source.BUILD,
                                     plistlib.loads((self.root / source.PLIST).read_bytes()))

    def test_missing_extra_invalid_and_drifted_settings_never_write(self):
        for key in source.KEYS:
            token = f"{key} = {self.current};".encode()
            for value in ("1.2", "7", "01.2.3", "v1.2.3", "1.2.3-rc", "$(UNKNOWN)", "",
                          f"$({key})", f"${{{key}}}", "900.800.700"):
                self.reset()
                self.modify(source.PROJECT, lambda raw: raw.replace(
                    token, f"{key} = {value};".encode(), 1))
                self.reject()
            self.reset()
            self.modify(source.PROJECT, lambda raw: raw.replace(token, b"", 1))
            self.reject()
            self.reset()
            self.modify(source.PROJECT, lambda raw: raw.replace(token, token + token, 1))
            self.reject()
        self.reset()
        self.modify(source.PROJECT, lambda raw: raw.replace(
            f"LIBRARY_STYLE = Bundle;".encode(),
            f"CURRENT_PROJECT_VERSION = {self.current};".encode(), 1))
        self.reject()

    def test_duplicate_replacing_missing_assignment_rejected(self):
        for key in source.KEYS:
            self.reset()
            raw = (self.root / source.PROJECT).read_bytes()
            token = f"{key} = {self.current};".encode()
            first = raw.index(token)
            second = raw.index(token, first + len(token))
            raw = raw[:second] + raw[second:].replace(token, b"", 1)
            raw = raw[:first] + f'"{key}" = "{self.current}"; '.encode() + raw[first:]
            (self.root / source.PROJECT).write_bytes(raw)
            self.reject()

    def test_configuration_references_names_and_orphan_settings_rejected(self):
        raw = self.originals[source.PROJECT][0]
        for before, after in (
            (b"42D60B400A9DDBC8002ABB42 /* Development */,",
             b"42D60B410A9DDBC8002ABB42 /* Deployment */,"),
            (b"name = Development;", b"name = Deployment;"),
            (b"isa = XCBuildConfiguration;", b"isa = Other;"),
            (b"archiveVersion = 1;",
             f"archiveVersion = 1; MARKETING_VERSION = {self.current};".encode()),
        ):
            self.reset()
            self.modify(source.PROJECT, lambda data: data.replace(before, after, 1))
            self.assertNotEqual((self.root / source.PROJECT).read_bytes(), raw)
            self.reject()

    def test_inline_quoted_settings_ignore_comment_and_string_noise(self):
        self.modify(source.PROJECT, lambda raw: raw.replace(
            b"archiveVersion = 1;",
            b'archiveVersion = 1; NOISE = "CURRENT_PROJECT_VERSION = 9.9.9;";'
            b' /* MARKETING_VERSION = 8.8.8; */'))
        for key in source.KEYS:
            self.modify(source.PROJECT, lambda raw: raw.replace(
                f"{key} = {self.current};".encode(),
                f'"{key}" = "{self.current}";'.encode()))
        self.modify(source.PROJECT, lambda raw: raw.replace(b";\n", b"; "))
        before = (self.root / source.PROJECT).read_bytes()
        new = source.bump("patch", self.root)
        self.assertEqual((self.root / source.PROJECT).read_bytes(),
                         before.replace(self.current.encode(), new.encode()))
        self.assertIn(b"9.9.9", (self.root / source.PROJECT).read_bytes())
        for noise in (b"/* unterminated", b'"unterminated', b"}", b"{"):
            self.reset()
            self.modify(source.PROJECT, lambda raw: raw + noise)
            self.reject()

    def test_recursive_references_both_syntaxes_and_quotes_preserved(self):
        for referencing, target in (source.KEYS, tuple(reversed(source.KEYS))):
            for ref in (f"$({target})", f"${{{target}}}"):
                for quoted in (False, True):
                    self.reset()
                    value = f'"{ref}"' if quoted else ref
                    self.modify(source.PROJECT, lambda raw: raw.replace(
                        f"{referencing} = {self.current};".encode(),
                        f"{referencing} = {value};".encode()))
                    for plist_ref in source.REFERENCES:
                        self.plist(**{source.SHORT: plist_ref, source.BUILD: plist_ref})
                        new = source.bump("patch", self.root)
                        self.assertIn(ref.encode(), (self.root / source.PROJECT).read_bytes())
                        actual = plistlib.loads((self.root / source.PLIST).read_bytes())
                        self.assertEqual(actual[source.SHORT], plist_ref)
                        self.assertEqual(actual[source.BUILD], plist_ref)
                        self.assertEqual(source.read_version(self.root), new)
        self.reset()
        for key, target in (source.KEYS, tuple(reversed(source.KEYS))):
            self.modify(source.PROJECT, lambda raw: raw.replace(
                f"{key} = {self.current};".encode(), f"{key} = $({target});".encode()))
        self.reject()

    def test_required_short_optional_build_literals_and_invalid_types(self):
        for key in (source.SHORT, source.BUILD):
            for bad in (True, 123, [], {}, "", "$(UNKNOWN)", "01.2.3", "900.800.700"):
                self.reset()
                self.plist(**{key: bad})
                self.reject()
        for fields in ({}, {source.BUILD: self.current}):
            self.reset()
            (self.root / source.PLIST).write_bytes(plistlib.dumps(fields))
            self.reject()
        self.reset()
        self.plist(**{source.BUILD: self.current})
        new = source.bump("minor", self.root)
        self.assertEqual(plistlib.loads((self.root / source.PLIST).read_bytes())[source.BUILD], new)
        for raw in (b"not plist", plistlib.dumps([]),
                    plistlib.dumps({source.SHORT: self.current}, fmt=plistlib.FMT_BINARY)):
            self.reset()
            (self.root / source.PLIST).write_bytes(raw)
            self.reject()

    def test_duplicate_xml_localized_keys_and_invalid_localization(self):
        for key in (source.SHORT, source.BUILD, "CFBundleIdentifier"):
            self.reset()
            self.plist(**{key: self.current})
            self.modify(source.PLIST, lambda raw: raw.replace(
                b"</dict>", f"<key>{key}</key><string>{self.current}</string></dict>".encode()))
            self.reject()
        for key in (source.SHORT, source.INFO, "CFBundleName"):
            for quoted in (False, True):
                self.reset()
                token = f'"{key}"' if quoted else key
                self.modify(source.LOCALIZED, lambda raw: raw + f'{token} = "{self.current}";'.encode())
                self.reject()
        for before, after in ((b"Copyright 2023", b"Copyright 2022"),
                              (b"Media Access Control version ", b"Other version "),
                              (b"CFBundleShortVersionString", b"MissingShort")):
            self.reset()
            self.modify(source.LOCALIZED, lambda raw: raw.replace(before, after))
            self.reject()

    def test_bom_encoding_every_line_ending_preserved(self):
        encodings = (("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"),
                     ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"),
                     ("utf-16-le", b""), ("utf-16-be", b""))
        for codec, bom in encodings:
            for ending in ("\r", "\n", "\r\n", "mixed"):
                self.reset()
                originals = {}
                for path in source.SOURCE_PATHS:
                    value = source.decode((self.root / path).read_bytes())[0]
                    if path == source.PLIST and codec.startswith("utf-16"):
                        value = value.replace('encoding="UTF-8"', 'encoding="UTF-16"')
                    value = "".join(line + (("\r", "\r\n", "\n")[i % 3]
                                            if ending == "mixed" else ending)
                                    for i, line in enumerate(value.splitlines()))
                    raw = bom + value.encode(codec)
                    (self.root / path).write_bytes(raw)
                    originals[path] = raw
                new = source.bump("patch", self.root)
                for path, raw in originals.items():
                    self.assertEqual((self.root / path).read_bytes(),
                                     raw.replace(self.current.encode(codec), new.encode(codec)))

    def test_unrelated_equal_versions_and_comments_not_rewritten(self):
        self.plist(**{"UnrelatedVersion": self.current})
        self.modify(source.PLIST, lambda raw: raw.replace(
            b"</dict>", f"<!-- <key>{source.SHORT}</key><string>9.9.9</string> --></dict>".encode()))
        self.modify(source.LOCALIZED, lambda raw: raw + f'Other = "{self.current}";\r'.encode())
        self.modify(source.PROJECT, lambda raw: raw.replace(
            b"archiveVersion = 1;",
            f'archiveVersion = 1; Other = "{self.current}";'.encode()))
        new = source.bump("patch", self.root)
        self.assertEqual(plistlib.loads((self.root / source.PLIST).read_bytes())["UnrelatedVersion"],
                         self.current)
        for path in (source.LOCALIZED, source.PROJECT):
            self.assertIn(f'Other = "{self.current}";'.encode(), (self.root / path).read_bytes())
        self.assertNotEqual(new, self.current)

    def test_localized_references_and_optional_build_preserved(self):
        self.modify(source.LOCALIZED, lambda raw: raw.replace(
            f'{source.SHORT} = "{self.current}"'.encode(),
            f'{source.SHORT} = "$(MARKETING_VERSION)"'.encode())
            + b'CFBundleVersion = "${CURRENT_PROJECT_VERSION}";\r')
        source.bump("patch", self.root)
        raw = (self.root / source.LOCALIZED).read_bytes()
        self.assertIn(b"$(MARKETING_VERSION)", raw)
        self.assertIn(b"${CURRENT_PROJECT_VERSION}", raw)

    def test_invalid_missing_source_encoding_and_mode_never_write(self):
        for path in source.SOURCE_PATHS:
            self.reset()
            (self.root / path).unlink()
            self.reject()
            self.reset()
            (self.root / path).write_bytes(b"\xffbroken")
            self.reject()
        self.reset()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            source.bump("other", self.root)
        self.assertEqual(self.snapshot(), before)

    def built(self):
        contents = self.root / f"test/Plugins/{source.PRODUCT}.bundle/Contents"
        localized = contents / "Resources" / source.LOCALIZED
        localized.parent.mkdir(parents=True, exist_ok=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({source.SHORT: self.current}))
        localized.write_bytes((self.root / source.LOCALIZED).read_bytes())
        return contents, localized

    def test_built_binary_xml_utf16_metadata_and_optional_absence(self):
        contents, localized = self.built()
        expected = {key: token.value for key, token in source.localized_fields(
            source.decode((self.root / source.LOCALIZED).read_bytes())[0]).items()}
        for fmt in (plistlib.FMT_BINARY, plistlib.FMT_XML):
            (contents / "Info.plist").write_bytes(plistlib.dumps({source.SHORT: self.current}, fmt=fmt))
            for style in ("strings", "utf-16", "utf-16-be", "xml", "binary"):
                if style in ("xml", "binary"):
                    raw = plistlib.dumps(expected, fmt=(
                        plistlib.FMT_XML if style == "xml" else plistlib.FMT_BINARY))
                else:
                    value = source.decode((self.root / source.LOCALIZED).read_bytes())[0]
                    raw = value.encode("utf-8" if style == "strings" else style)
                    if style == "utf-16-be":
                        raw = b"\xfe\xff" + raw
                localized.write_bytes(raw)
                source.validate_built(self.root)
                localized.write_bytes(plistlib.dumps({**expected, source.INFO: "wrong"}, fmt=fmt))
                with self.assertRaises(ValueError):
                    source.validate_built(self.root)
        localized.write_bytes(plistlib.dumps(expected))
        for data in ({}, [], {source.SHORT: self.current, source.BUILD: self.current},
                     {source.SHORT: "900.800.700"}):
            (contents / "Info.plist").write_bytes(plistlib.dumps(data))
            with self.assertRaises(ValueError):
                source.validate_built(self.root)
        self.plist(**{source.BUILD: self.current})
        (contents / "Info.plist").write_bytes(plistlib.dumps(
            {source.SHORT: self.current, source.BUILD: self.current}))
        source.validate_built(self.root)

    def test_built_missing_empty_duplicate_and_wrong_serialization(self):
        contents, localized = self.built()
        for path in (contents / "Info.plist", localized):
            saved = path.read_bytes()
            for raw in (None, b"", b"not metadata"):
                if raw is None:
                    path.unlink()
                else:
                    path.write_bytes(raw)
                with self.assertRaises((ValueError, OSError)):
                    source.validate_built(self.root)
                path.write_bytes(saved)
        localized.write_bytes(plistlib.dumps({source.SHORT: self.current, source.INFO: "wrong"}))
        with self.assertRaises(ValueError):
            source.validate_built(self.root)

    def test_prebuilt_schema_and_other_files_untouched(self):
        excluded = {
            "test/Plugins/Media Access Control.bundle/Contents/Info.plist": b"old prebuilt",
            "manifest.json": b'{"version":"1.0","id":20000}',
            "constants.xlf": b'<xliff version="1.0"/>',
            "test/Project/Sources/catalog.4DCatalog": b"4D counter",
            "Media Access Control.vcxproj": b"Windows project",
            "a/library.a": b"dependency",
        }
        for path, raw in excluded.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        source.bump("major", self.root)
        for path, raw in excluded.items():
            self.assertEqual((self.root / path).read_bytes(), raw)

    def test_cli_read_and_invalid_mode_never_write(self):
        helper = ROOT / ".github/scripts/source_version.py"
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-B", str(helper), "--root", str(self.root)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.current)
        result = subprocess.run([sys.executable, "-B", str(helper), "--root", str(self.root),
                                 "--bump", "other"], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
