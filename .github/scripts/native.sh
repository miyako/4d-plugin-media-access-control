#!/bin/bash
# Run from the checkout root: clean, build, verify, merge, sign, or package.
# VERSION is the workflow's source/tag version; credentials stay in the keychain.
set -euo pipefail
set -f

die() { printf 'native: %s\n' "$*" >&2; exit 1; }
ROOT="$(pwd -P)"
PRODUCT_NAME="${PRODUCT_NAME:-Media Access Control}"
BASE_DIR="${BASE_DIR:-test}"
MACOS_DEPLOYMENT_TARGET="${MACOS_DEPLOYMENT_TARGET:-10.13}"
case "$PRODUCT_NAME" in ''|.|..|*/*|*\\*|*$'\n'*|*$'\r'*) die "Unsafe product name";; esac
[[ "$BASE_DIR" == test ]] || die "BASE_DIR must be test"
BUNDLE="$ROOT/test/Plugins/$PRODUCT_NAME.bundle"
if [[ -n "${BUNDLE_PATH:-}" ]]; then
    [[ "$BUNDLE_PATH" == "$BUNDLE" || "$BUNDLE_PATH" == "test/Plugins/$PRODUCT_NAME.bundle" ]] ||
        die "BUNDLE_PATH must name the canonical bundle"
fi
BINARY="$BUNDLE/Contents/MacOS/$PRODUCT_NAME"

# Refuse symlink traversal even for an otherwise bounded removal/copy path.
safe_path() {
    local path="$1"
    [[ "$path" == "$ROOT/"* ]] || die "Path outside checkout: $path"
    while [[ "$path" != "$ROOT" ]]; do
        [[ ! -L "$path" ]] || die "Symlink in generated path: $path"
        path="${path%/*}"
    done
}
remove_generated() {
    safe_path "$1"
    rm -rf -- "$1"
}
require_file() {
    safe_path "$1"
    [[ -s "$1" && -f "$1" ]] || die "Missing or empty file: $1"
}
require_universal() {
    local tokens="$1" token arm=no intel=no
    for token in $tokens; do
        case "$token" in arm64) arm=yes;; x86_64) intel=yes;; esac
    done
    [[ "$arm" == yes && "$intel" == yes ]] ||
        die "Expected exact arm64 and x86_64 architecture tokens: $tokens"
}
export_value() {
    if [[ -n "${GITHUB_ENV:-}" ]]; then
        printf '%s=%s\n' "$1" "$2" >> "$GITHUB_ENV"
    fi
}
clean() {
    # Keep Windows, QuickLook, manifests, constants, libraries and other resources.
    remove_generated "$BINARY"
    remove_generated "$BUNDLE/Contents/Info.plist"
    remove_generated "$BUNDLE/Contents/Resources/English.lproj/InfoPlist.strings"
    remove_generated "$BUNDLE/Contents/_CodeSignature"
    remove_generated "$ROOT/build/$PRODUCT_NAME.build"
    remove_generated "$ROOT/test/Plugins/$PRODUCT_NAME.build"
    remove_generated "$ROOT/test/Plugins/Default/$PRODUCT_NAME.build"
}
build() {
    local archs="${ARCHS:-arm64 x86_64}"
    require_universal "$archs"
    [[ "$MACOS_DEPLOYMENT_TARGET" == 10.13 ]] || die "Deployment override must be 10.13"
    safe_path "$BUNDLE"
    safe_path "$ROOT/build/$PRODUCT_NAME.build"
    xcodebuild -project "$PRODUCT_NAME.xcodeproj" -target "$PRODUCT_NAME" \
        -configuration Default "ARCHS=$archs" ONLY_ACTIVE_ARCH=NO \
        "MACOSX_DEPLOYMENT_TARGET=$MACOS_DEPLOYMENT_TARGET" CODE_SIGNING_ALLOWED=NO \
        "CONFIGURATION_BUILD_DIR=$ROOT/test/Plugins" "OBJROOT=$ROOT/build" build
}
verify() {
    require_file "$BINARY"
    local archs
    archs="$(lipo -archs "$BINARY")"
    require_universal "$archs"
}
merge() {
    local source="$ROOT/winbuild/$PRODUCT_NAME.4DX"
    require_file "$source"
    safe_path "$BUNDLE/Contents/Windows64/$PRODUCT_NAME.4DX"
    mkdir -p "$BUNDLE/Contents/Windows64"
    cp "$source" "$BUNDLE/Contents/Windows64/$PRODUCT_NAME.4DX"
    export_value BUNDLE_PATH "test/Plugins/$PRODUCT_NAME.bundle"
}
strict_verify() {
    codesign --verify --deep --strict --verbose=2 "$BUNDLE"
}
sign() {
    require_file "$BINARY"
    : "${KEYCHAIN_PATH:?KEYCHAIN_PATH is required}"
    local identities identity="" line
    identities="$(security find-identity -v -p codesigning "$KEYCHAIN_PATH")"
    while IFS= read -r line; do
        if [[ "$line" == *"Developer ID Application"* && "$line" == *\"*\"* ]]; then
            identity="${line#*\"}"
            identity="${identity%%\"*}"
            break
        fi
    done <<< "$identities"
    [[ -n "$identity" ]] || die "No Developer ID Application identity found"
    codesign --verbose --deep --timestamp --force --options=runtime \
        --sign "$identity" "$BUNDLE"
    strict_verify
}
notarize() {
    local result
    result="$(xcrun notarytool submit "$1" --keychain-profile notarytool-profile \
        --keychain "$KEYCHAIN_PATH" --wait --output-format json)"
    printf '%s' "$result" | python3 -c \
        'import json,sys; r=json.load(sys.stdin); sys.exit(0 if isinstance(r,dict) and r.get("status") == "Accepted" else 1)'
}
package() {
    require_file "$BINARY"
    require_file "$BUNDLE/Contents/Windows64/$PRODUCT_NAME.4DX"
    : "${VERSION:?VERSION is required}" "${KEYCHAIN_PATH:?KEYCHAIN_PATH is required}"
    case "$VERSION" in *[!A-Za-z0-9._+-]*|.|..) die "Unsafe VERSION";; esac
    local zip="$ROOT/$PRODUCT_NAME-$VERSION.zip" dmg="$ROOT/$PRODUCT_NAME-$VERSION.dmg"
    safe_path "$zip"
    safe_path "$dmg"
    strict_verify
    remove_generated "$zip"
    remove_generated "$dmg"
    ditto -c -k --keepParent "$BUNDLE" "$zip"
    hdiutil create -format UDBZ -volname "$PRODUCT_NAME" -srcfolder "$BUNDLE" "$dmg"
    require_file "$zip"
    require_file "$dmg"
    notarize "$zip"
    notarize "$dmg"
    xcrun stapler staple "$dmg"
    xcrun stapler validate "$dmg"
    strict_verify
    export_value ZIP_PATH "$PRODUCT_NAME-$VERSION.zip"
    export_value DMG_PATH "$PRODUCT_NAME-$VERSION.dmg"
}
[[ $# == 1 ]] || die "Usage: $0 clean|build|verify|merge|sign|package"
case "$1" in
    clean|build|verify|merge|sign|package) "$1";;
    *) die "Unknown action: $1";;
esac
