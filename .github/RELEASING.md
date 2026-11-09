# Manual source versions and builds

The two release-related Actions are independent. **Bump** has one required
`patch`/`minor`/`major` choice (default `patch`); it commits source metadata on
the selected branch, without building, tagging, releasing, or dispatching.
**Build** has no custom inputs: use GitHub's built-in ref selector. It builds
the immutable event commit, derives the canonical `X.Y.Z` from source, and
publishes `vX.Y.Z` only if neither that tag nor any matching release exists.
Neither action is triggered by pushes or by the other action.

The original workflows imposed no publication branch restriction. Build
therefore supports any selected branch or tag; it does not invent a
default-branch-only restriction. Publication uses a constant repository-wide
concurrency group with cancellation disabled. GitHub does not promise FIFO
execution and may replace pending runs.

## Source authority

The immutable intake is `7fc9a40f19c26fcd5305e2238b02a8f347ccaff0`.
The original Bump was introduced at
`767e777a773418e803553d1461646a5cf49ce5e7` with an explicit header promising
coordinated `MARKETING_VERSION`, `CURRENT_PROJECT_VERSION`, and plist versions.
Its implementation instead updated marketing versions only. The intake
commit changed all six marketing assignments from `2.1.0` to `2.1.1` and left
three current assignments and the copied source localization at `2.1.0`.
This migration repairs only those stale source tokens to the existing
`2.1.1` authority; it does not increment the version.

The source metadata is `Media Access Control.xcodeproj/project.pbxproj`,
`Info.plist`, and `English.lproj/InfoPlist.strings`. The plist's short version
references `$(MARKETING_VERSION)`; its absent build key stays absent. The
English localization is an actual Xcode resource copied into the package,
not the prebuilt destination. Source rendering preserves encoding, BOM,
individual line endings, modes, references, and unrelated bytes.
Malformed, duplicate, unsupported, cyclic, or drifting metadata must fail
before any source writes. Checked-in bundle metadata is a generated output,
not version authority, and is not normalized by Bump.

## Build and publication

The actual native topology is retained: Windows Release/x64 MSBuild with
the existing toolset/SDK overrides, then macOS configuration `Default`,
universal `arm64 x86_64`, with deployment override `10.13`. Frameworks,
libraries, resources, prebuilt platform files, 4D assets, and the original
hardened-runtime signing policy remain intact. Cleanup is selective.
Xcode's configuration output directory is explicitly the canonical
`test/Plugins` package root, not a `Default` sibling.

An initially dirty checkout is rejected. Native tools subsequently rewrite
tracked executable, plist, copied localization, and signature outputs.
The final guard allows exactly those generated paths while checking the
immutable HEAD and both staged and worktree bytes of every other tracked
file. Hidden index flags, unexpected files (including ignored source or
configuration), and source/index drift fail before publication. Generated
bytes are retained, never reset. The final built plist (XML or binary) and
actual localization must match source. Architecture checks require exact
`arm64` and `x86_64` tokens, not similarly named architectures.

Signing verification, both successful notarytool exit codes and JSON
`Accepted` statuses, stapling, and strict signature verification are
mandatory. Authenticated, all-page, drafts-inclusive release enumeration
and exact remote-tag absence checks run before native work and again after
all native/sign/notary/staple work. Any authentication, network, malformed
response, pagination, or availability failure aborts.

Publication is create-only: create the tag ref at the selected source SHA,
verify the exact advertised ref, create a new public non-prerelease with an
explicit tag/name/target, then upload assets only to that new release ID.
The original `softprops` invocation supplied only tag, name, and files:
the retained notes policy is an empty body with generated notes disabled.
Asset names, integer sizes, and uploaded state are verified.
The original remote asset names use dots instead of product-name spaces
(`Media.Access.Control-X.Y.Z.zip` and `.dmg`); uploads request those exact
names explicitly.

There is no overwrite, reuse, force, rollback, delete, PATCH, clobber, or
automatic publication retry. A race or partial failure can leave a tag,
release, or some uploaded assets behind. Inspect the reported remote state
and recover manually; do not rerun expecting an update or automatic cleanup.
A same-SHA tag race is still a failure.

## Local checks

Run the full offline unittest suite and `actionlint` across all workflows.
The suite uses disposable repositories, bare origins, fake authenticated
GitHub CLI responses, and closed-PATH native command stubs; it must not
invoke real builds, signing, notarization, 4D, or production publication.
The executable pre-push guard is mandatory before a Bump edit and again
immediately before push. It accepts exactly one advertised update from the
selected old SHA to the literal new HEAD, with one direct parent and a
nonempty source-only commit. Both `push.followTags=false` and
`--no-follow-tags` are required for pushes.
