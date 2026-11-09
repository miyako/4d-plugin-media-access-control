#!/usr/bin/env python3
"""Coordinated source versions, edited only at validated lexical value spans."""

import argparse
from dataclasses import dataclass
from pathlib import Path
import plistlib
import re
import sys
from xml.parsers.expat import ExpatError

sys.dont_write_bytecode = True

PRODUCT = "Media Access Control"
PROJECT = Path(f"{PRODUCT}.xcodeproj/project.pbxproj")
PLIST = Path("Info.plist")
LOCALIZED = Path("English.lproj/InfoPlist.strings")
SOURCE_PATHS = (PROJECT, PLIST, LOCALIZED)
KEYS = ("MARKETING_VERSION", "CURRENT_PROJECT_VERSION")
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
REFERENCES = {syntax.format(key): key for key in KEYS
              for syntax in ("$({})", "${{{}}}")}
SHORT, BUILD, INFO = "CFBundleShortVersionString", "CFBundleVersion", "CFBundleGetInfoString"
INFO_PREFIX = f"{PRODUCT} version "
INFO_SUFFIX = ", Copyright 2023 miyako."
CONFIGURATIONS = frozenset(("Development", "Deployment", "Default"))
PLIST_VERSION = re.compile(
    r"(<key>CFBundle(?:ShortVersionString|Version)</key>\s*<string>)([^<]*)(</string>)"
)
LOCALIZED_TOKEN = re.compile(
    r'\s+|/\*.*?\*/|//[^\r\n]*|'
    r'(?P<key>"[A-Za-z_][A-Za-z_0-9]*"|[A-Za-z_][A-Za-z_0-9]*)'
    r'\s*=\s*"(?P<value>(?:\\.|[^"\\])*)"\s*;', re.DOTALL,
)


class UniqueDictionary(dict):
    def __setitem__(self, key, value):
        if key in self:
            raise ValueError(f"Duplicate metadata key: {key}")
        super().__setitem__(key, value)


def decode(data):
    for bom, encoding in (
        (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"),
        (b"\xef\xbb\xbf", "utf-8"),
    ):
        if data.startswith(bom):
            value = data[len(bom):].decode(encoding)
            break
    else:
        bom, encoding = b"", "utf-8"
        if b"\0" in data:
            even, odd = data[::2].count(0), data[1::2].count(0)
            if len(data) % 2:
                raise ValueError("Malformed BOM-less source encoding")
            if odd > 2 * even:
                encoding = "utf-16-le"
            elif even > 2 * odd:
                encoding = "utf-16-be"
            else:
                raise ValueError("Ambiguous BOM-less source encoding")
        value = data.decode(encoding)
    if "\0" in value:
        raise ValueError("NUL characters are unsupported in metadata")
    return value, encoding, bom


@dataclass
class Token:
    value: str
    start: int
    end: int
    quoted: bool = False


def project_tokens(value):
    result, offset = [], 0
    while offset < len(value):
        if value[offset].isspace():
            offset += 1
        elif value.startswith("//", offset):
            match = re.search(r"[\r\n]", value[offset:])
            offset = len(value) if match is None else offset + match.start()
        elif value.startswith("/*", offset):
            end = value.find("*/", offset + 2)
            if end < 0:
                raise ValueError("Unterminated project comment")
            offset = end + 2
        elif value[offset] == '"':
            start = offset = offset + 1
            while offset < len(value) and value[offset] != '"':
                offset += 2 if value[offset] == "\\" else 1
            if offset >= len(value):
                raise ValueError("Unterminated project string")
            result.append(Token(value[start:offset], start, offset, True))
            offset += 1
        elif value.startswith(("$(", "${"), offset):
            close = ")" if value[offset + 1] == "(" else "}"
            end = value.find(close, offset + 2)
            if end < 0 or re.search(r"\s", value[offset:end]):
                raise ValueError("Malformed project reference")
            result.append(Token(value[offset:end + 1], offset, end + 1))
            offset = end + 1
        elif value[offset] in "{}=;(),":
            result.append(Token(value[offset], offset, offset + 1))
            offset += 1
        else:
            start = offset
            while offset < len(value) and not value[offset].isspace() \
                    and value[offset] not in '{}=;(),"':
                if value.startswith(("/*", "//"), offset):
                    break
                offset += 1
            if start == offset:
                raise ValueError("Unsupported project token")
            result.append(Token(value[start:offset], start, offset))
    return result


def project_settings(value):
    tokens, offset = project_tokens(value), 0
    assignments = []

    def punctuation(character):
        nonlocal offset
        if offset >= len(tokens) or tokens[offset].quoted \
                or tokens[offset].value != character:
            raise ValueError(f"Expected project punctuation: {character}")
        offset += 1

    def parse():
        nonlocal offset
        if offset >= len(tokens):
            raise ValueError("Incomplete project serialization")
        token = tokens[offset]
        if not token.quoted and token.value == "{":
            offset += 1
            result = UniqueDictionary()
            while offset < len(tokens) and (tokens[offset].quoted or tokens[offset].value != "}"):
                key = tokens[offset]
                if not key.quoted and key.value in "{}=;(),":
                    raise ValueError("Invalid project dictionary key")
                offset += 1
                punctuation("=")
                actual = parse()
                punctuation(";")
                result[key.value] = actual
                if key.value in KEYS:
                    assignments.append((key.value, actual))
            punctuation("}")
            return result
        if not token.quoted and token.value == "(":
            offset += 1
            result = []
            while offset < len(tokens) and (tokens[offset].quoted or tokens[offset].value != ")"):
                result.append(parse())
                if offset < len(tokens) and tokens[offset].value == ")" and not tokens[offset].quoted:
                    break
                punctuation(",")
            punctuation(")")
            return result
        if not token.quoted and token.value in "{}=;(),":
            raise ValueError("Invalid project value")
        offset += 1
        return token

    def scalar(item):
        if not isinstance(item, Token):
            raise ValueError("Expected scalar project value")
        return item.value

    document = parse()
    if offset != len(tokens) or not isinstance(document, dict):
        raise ValueError("Invalid project dictionary serialization")
    objects = document.get("objects")
    if not isinstance(objects, dict):
        raise ValueError("Missing project objects")
    fields = {key: [] for key in KEYS}
    owners = {"PBXProject": [], "PBXNativeTarget": []}
    for obj in objects.values():
        if isinstance(obj, dict) and isinstance(obj.get("isa"), Token):
            kind = obj["isa"].value
            if kind in owners:
                owners[kind].append(obj)
    if any(len(group) != 1 for group in owners.values()):
        raise ValueError("Expected one project and one native target")
    consumed = set()
    for kind, group in owners.items():
        owner = group[0]
        config_list = objects.get(scalar(owner.get("buildConfigurationList")))
        if not isinstance(config_list, dict) or scalar(config_list.get("isa")) != "XCConfigurationList":
            raise ValueError("Invalid project configuration list")
        references = config_list.get("buildConfigurations")
        if not isinstance(references, list):
            raise ValueError("Missing project configurations")
        names = set()
        for reference in references:
            identity = scalar(reference)
            if identity in consumed:
                raise ValueError("Duplicate project configuration reference")
            consumed.add(identity)
            config = objects.get(identity)
            if not isinstance(config, dict) or scalar(config.get("isa")) != "XCBuildConfiguration":
                raise ValueError("Invalid build configuration")
            name = scalar(config.get("name"))
            if name in names:
                raise ValueError("Duplicate configuration name")
            names.add(name)
            settings = config.get("buildSettings")
            if not isinstance(settings, dict):
                raise ValueError("Missing configuration build settings")
            required = KEYS if kind == "PBXProject" else KEYS[:1]
            for key in KEYS:
                if (key in settings) != (key in required):
                    raise ValueError(f"Missing or extra {key} in {kind} {name}")
                if key in required:
                    actual = settings[key]
                    scalar(actual)
                    fields[key].append(actual)
        if names != CONFIGURATIONS:
            raise ValueError("Missing or extra named build configurations")
    expected = {(key, token.start) for key, values in fields.items() for token in values}
    if any(not isinstance(token, Token) or (key, token.start) not in expected
           for key, token in assignments) or len(assignments) != len(expected):
        raise ValueError("Version assignment outside authoritative build settings")
    return fields


def resolve_settings(fields):
    resolved = {}

    def resolve(key, visiting=()):
        if key in visiting:
            raise ValueError("Cyclic source version reference")
        if key not in resolved:
            values = set()
            for token in fields[key]:
                value = token.value
                if value in REFERENCES:
                    value = resolve(REFERENCES[value], visiting + (key,))
                elif not re.fullmatch(VERSION, value):
                    raise ValueError(f"{key} must resolve to canonical X.Y.Z")
                values.add(value)
            if len(values) != 1:
                raise ValueError(f"{key} has drifted across configurations")
            resolved[key] = values.pop()
        return resolved[key]

    if len({resolve(key) for key in KEYS}) != 1:
        raise ValueError("Coordinated source versions have drifted")
    return resolved


def load_plist(data):
    try:
        fmt = plistlib.FMT_BINARY if data.startswith(b"bplist00") else plistlib.FMT_XML
        return plistlib.loads(data, fmt=fmt, dict_type=UniqueDictionary)
    except (plistlib.InvalidFileException, ExpatError, UnicodeError) as error:
        raise ValueError("Invalid plist serialization") from error


def localized_fields(value):
    fields, offset = UniqueDictionary(), 0
    while offset < len(value):
        token = LOCALIZED_TOKEN.match(value, offset)
        if token is None:
            raise ValueError("Unsupported localized strings serialization")
        if token["key"] is not None:
            fields[token["key"].strip('"')] = Token(
                token["value"], token.start("value"), token.end("value"))
        offset = token.end()
    return fields


def inspect_data(data):
    decoded = {path: decode(data[path])[0] for path in SOURCE_PATHS}
    settings = project_settings(decoded[PROJECT])
    resolved = resolve_settings(settings)
    version = resolved[KEYS[0]]
    plist = load_plist(data[PLIST])
    if not isinstance(plist, dict) or SHORT not in plist:
        raise ValueError("Source Info.plist requires a dictionary with short version")
    xml = re.sub(r"<!--.*?-->", lambda match: " " * len(match[0]),
                 decoded[PLIST], flags=re.DOTALL)
    serialized = list(PLIST_VERSION.finditer(xml))
    if len(serialized) != 1 + (BUILD in plist):
        raise ValueError("Source plist versions must be editable XML strings")
    edits = {path: [] for path in SOURCE_PATHS}
    for key in (SHORT, BUILD):
        if key not in plist:
            continue
        value = plist[key]
        if not isinstance(value, str) or resolved.get(REFERENCES.get(value), value) != version:
            raise ValueError(f"{key} is invalid, unresolved or has drifted")
        matches = [match for match in serialized if f"<key>{key}</key>" in match[1]]
        if len(matches) != 1 or matches[0][2] != value:
            raise ValueError(f"{key} has unsupported XML serialization")
        if value not in REFERENCES:
            edits[PLIST].append(matches[0].span(2))
    for values in settings.values():
        edits[PROJECT].extend((token.start, token.end) for token in values
                              if token.value not in REFERENCES)
    localized = localized_fields(decoded[LOCALIZED])
    if SHORT not in localized or INFO not in localized:
        raise ValueError("Localized display metadata is missing")
    for key in (SHORT, BUILD):
        if key in localized:
            token = localized[key]
            if resolved.get(REFERENCES.get(token.value), token.value) != version:
                raise ValueError("Localized version is invalid or has drifted")
            if token.value not in REFERENCES:
                edits[LOCALIZED].append((token.start, token.end))
    info = localized[INFO]
    if info.value != INFO_PREFIX + version + INFO_SUFFIX:
        raise ValueError("Localized information version is invalid or has drifted")
    start = info.start + len(INFO_PREFIX)
    edits[LOCALIZED].append((start, start + len(version)))
    return version, edits


def read_version(root=Path(".")):
    root = Path(root)
    return inspect_data({path: (root / path).read_bytes() for path in SOURCE_PATHS})[0]


def bump(mode, root=Path(".")):
    index = ("major", "minor", "patch").index(mode)
    root = Path(root)
    data = {path: (root / path).read_bytes() for path in SOURCE_PATHS}
    current, edits = inspect_data(data)
    parts = list(map(int, current.split(".")))
    parts[index] += 1
    parts[index + 1:] = [0] * (2 - index)
    new = ".".join(map(str, parts))
    updates = {}
    for path in SOURCE_PATHS:
        value, encoding, bom = decode(data[path])
        for start, end in sorted(edits[path], reverse=True):
            value = value[:start] + new + value[end:]
        updates[path] = bom + value.encode(encoding)
    if inspect_data(updates)[0] != new:
        raise ValueError("Rendered metadata did not validate")
    for path, content in updates.items():
        if content != data[path]:
            (root / path).write_bytes(content)
    return new


def built_dictionary(path):
    raw = Path(path).read_bytes()
    if raw.startswith(b"bplist00") or decode(raw)[0].lstrip().startswith("<"):
        value = load_plist(raw)
        if not isinstance(value, dict):
            raise ValueError("Built metadata must be a dictionary")
        return value
    return {key: token.value for key, token in localized_fields(decode(raw)[0]).items()}


def validate_built(root=Path(".")):
    root = Path(root)
    version = read_version(root)
    contents = root / "test/Plugins" / f"{PRODUCT}.bundle/Contents"
    source = load_plist((root / PLIST).read_bytes())
    actual = load_plist((contents / "Info.plist").read_bytes())
    if not isinstance(actual, dict):
        raise ValueError("Built Info.plist must be an XML/binary plist dictionary")
    for key in (SHORT, BUILD):
        if (key in source) != (key in actual) or (key in source and actual[key] != version):
            raise ValueError("Built bundle version differs from selected source")
    localized = localized_fields(decode((root / LOCALIZED).read_bytes())[0])
    actual = built_dictionary(contents / "Resources" / LOCALIZED)
    for key in (SHORT, BUILD, INFO):
        expected = localized[key].value if key in localized else None
        if expected in REFERENCES:
            expected = version
        if (key in localized) != (key in actual) or (key in localized and actual[key] != expected):
            raise ValueError("Built localized display differs from selected source")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--bump", choices=("patch", "minor", "major"))
    operation.add_argument("--validate-built", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    if args.validate_built:
        validate_built(args.root)
    else:
        print(bump(args.bump, args.root) if args.bump else read_version(args.root))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, UnicodeError) as error:
        print(f"Version validation failed: {error}", file=sys.stderr)
        sys.exit(1)
