#!/usr/bin/env python3
"""Actual-source synthetic-only ownership regression, including dormant helpers."""
import argparse
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "988286b3a118dab50f32368430fe467f5e4fa81d"
CASES = (
    *(f"set-{i}" for i in range(5)),
    *(f"get-{kind}{suffix}" for kind in ("a", "b", "n", "o", "c")
      for suffix in ("", "-mismatch", "-failed")),
    "failed-key", "get-a-has-failed", "stringify", "stringify-mismatch",
    "stringify-failed", "stringify-sentinel", "stringify-copy-failed",
    "arrays", "scalars", "dispatch", "missing",
)
DEFECTS = {
    **{f"set-{i}": "setter initial owner leaked" for i in range(4)},
    "set-4": "all picture references balanced",
    **{f"get-{kind}{suffix}": "getter owned output leaked"
       for kind in ("a", "b", "n", "o", "c") for suffix in ("", "-mismatch")
       if suffix or kind == "a"},
    **{f"get-{kind}-failed": "not a valid value for type 'PA_VariableKind'"
       for kind in ("a", "b", "n", "o", "c")},
    "get-a-mismatch": "all object collection references balanced",
    "failed-key": "invalid property key",
    "get-a-has-failed": "existing property",
    "stringify-failed": "not a valid value for type 'PA_VariableKind'",
    "stringify-sentinel": "failed output must not be read",
    "stringify-copy-failed": "callback argument live",
}


def build(source, output):
    api = source / "4D Plugin API"
    subprocess.run([
        "clang++", "-std=c++11", "-x", "objective-c++",
        "-include", "Carbon/Carbon.h",
        "-include", str(ROOT / "tests/ownership/platform.h"),
        "-I", str(source), "-I", str(api), "-I", str(api / "Classes"),
        "-I", str(source / "support"), "-g", "-O1",
        "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
        "-fno-omit-frame-pointer", "-Wno-deprecated-declarations",
        "-Wno-logical-op-parentheses", "-Wno-writable-strings",
        "-Wno-multichar",
        str(api / "4DPluginAPI.c"),
        str(source / "4DPlugin-Media-Access-Control.cpp"),
        *map(str, sorted((api / "Classes").glob("*.cpp"))),
        str(source / "support/4DPlugin-JSON.cpp"),
        str(ROOT / "tests/ownership/host.mm"),
        "-framework", "Cocoa", "-framework", "Carbon",
        "-o", str(output),
    ], check=True)
    symbols = subprocess.check_output(["nm", "-u", str(output)], text=True)
    check_linkage = subprocess.check_output(["otool", "-L", str(output)], text=True)
    assert "SCNetworkInterface" not in symbols, symbols
    assert "SystemConfiguration.framework" not in check_linkage, check_linkage


def run(binary, logs, baseline=False):
    logs.mkdir(exist_ok=True)
    counts = {"pass": 0, "expected_failure": 0}
    for case in CASES:
        for empty in ("allocated", "null"):
            result = subprocess.run(
                [str(binary), case, empty], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0",
                     "UBSAN_OPTIONS": "halt_on_error=1"},
            )
            (logs / f"{case}-{empty}.log").write_text(result.stdout)
            # Null empty strings don't allocate an initial owner.
            expected_failure = baseline and case in DEFECTS and not (
                case in {f"set-{i}" for i in range(4)} and empty == "null")
            assert bool(result.returncode) == expected_failure, (
                f"Unexpected result {case}/{empty}:\n{result.stdout}")
            if expected_failure:
                assert DEFECTS[case] in result.stdout, (case, result.stdout)
                counts["expected_failure"] += 1
                print(f"EXPECTED BASELINE FAILURE {case}/{empty}: {DEFECTS[case]}")
            else:
                counts["pass"] += 1
                print(result.stdout.strip())
    print(("BASELINE" if baseline else "FIXED"), counts)
    result = subprocess.run(
        [str(binary), "deferred-create", "allocated"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0",
             "UBSAN_OPTIONS": "halt_on_error=1"},
    )
    (logs / "deferred-create.log").write_text(result.stdout)
    assert result.returncode and (
        "DEFERRED initial array create publishes success-shaped descriptor" in result.stdout
    ), result.stdout
    print("DEFERRED (not passing/certified): initial array creation failure")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    build(ROOT, args.output / "fixed")
    run(args.output / "fixed", args.output / "fixed-logs")
    if args.baseline:
        with tempfile.TemporaryDirectory(prefix="media-ownership-") as directory:
            baseline = Path(directory)
            archive = baseline / "baseline.tar"
            with archive.open("wb") as stream:
                subprocess.run(["git", "archive", BASELINE], cwd=ROOT, stdout=stream, check=True)
            with tarfile.open(archive) as source:
                source.extractall(baseline, filter="data")
            exemptions = {
                "support/4DPlugin-JSON.cpp", "support/4DPlugin-JSON.h",
                "README.md", "tests/test_workflows.py",
            }
            entries = subprocess.check_output(
                ["git", "ls-tree", "-r", "-z", BASELINE], cwd=ROOT,
            ).decode().split("\0")
            for entry in filter(None, entries):
                metadata, name = entry.split("\t", 1)
                mode = metadata.split()[0]
                if name not in exemptions:
                    assert (baseline / name).read_bytes() == (ROOT / name).read_bytes(), name
                assert bool((ROOT / name).stat().st_mode & 0o111) == (mode == "100755"), name
            build(baseline, args.output / "baseline")
            run(args.output / "baseline", args.output / "baseline-logs", baseline=True)


if __name__ == "__main__":
    main()
