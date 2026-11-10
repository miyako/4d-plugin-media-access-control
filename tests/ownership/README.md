# Native string ownership audit

## Scope and immutable provenance

Baseline: `988286b3a118dab50f32368430fe467f5e4fa81d`.
Canonical guidance: `miyako/skills`, `e6b492f`,
`4d-skills/skills/4dplugin/SKILL.md`.
This is a bounded first-party ownership audit, not a security or platform audit.

The compiled SDK is the complete, unchanged `4D Plugin API/4DPluginAPI.c`,
Git blob `62f745e91d81ef2c3c901c656ea8e175d3b173e8`; local history last
changed it in `767e777a773418e803553d1461646a5cf49ce5e7` (`mv`).
The `rev: v14` comment is not a verified upstream release identity.
Its companion immutable blobs are:

| SDK file | Git blob |
|---|---|
| `4DPluginAPI.h` | `528bf1d2552df08d6a8b1bc95011a9b72934ad5c` |
| `PrivateTypes.h` | `a3098cb662cdbc43ba574fdef72a059c4c6d1cd4` |
| `PublicTypes.h` | `9ae20b001bff9128f690fff3e6c3f873b6238333` |
| `EntryPoints.h` | `da428d5f6ca23b0a30371c7288a01d29aa3c466a` |
| `Flags.h` | `7a5a35539f27430a3fd9cb711d6b789051316471` |
| `4DPluginAPI.def` | `5bbdb82fb724b6f8e307feb01d9f158b802de89d` |

Reference harnesses were read via GitHub contents/tree APIs, not checked out:
`miyako/4d-plugin-jsmin@d12035643f874ec57ae634194ec0f1206b05a5f2`,
`tests/ownership/{host.mm,run.py}`, and the verified path inventory in
`miyako/4d-plugin-jpegoptim@e49adcd32bd5500cf3de680faa3649b32463ff5e`,
`.github/tests/{ownership_host.mm,test_ownership.py}`.
The host here is adapted to this repository's actual SDK, not a substitute SDK.

## Complete native inventory and reachability

Both `Media Access Control.vcxproj` and the Xcode `PBXSourcesBuildPhase`
compile exactly the SDK, dispatcher, `ARRAY_TEXT.cpp` and `C_TEXT.cpp`.
Windows compiles the SDK as C++; Xcode compiles the main source as Objective-C++.
`4DPluginAPI.def` exports `FourDPackex`, which invokes `PluginMain`.
Selector 1 is the only exported command. Root and bundled `manifest.json`
declare `Get hardware address(&Y;&Y;&Y;&Y):T`; four text arrays plus scalar text.
Other SDK selectors reach the default no-op dispatcher branch.

| First-party source/header | Ownership surface and result |
|---|---|
| `4DPlugin-Media-Access-Control.cpp`, `.h` | Active dispatcher and `Get_hardware_address`; four independent wrapper arrays and content-set scalar return. macOS Copy-rule array released after exception handling, borrowed platform strings copied into C++ buffers. Windows adapter buffer owns its strings until copied; only static inspection of Windows. |
| `4D Plugin API/Classes/ARRAY_TEXT.cpp`, `.h` | Active wrapper: copies borrowed element descriptors into independent C++ strings on input; creates a distinct SDK string per output (including index zero) and transfers it without post-transfer disposal. Conversions, append/set/copy/URL helpers included in static inspection. Initial creation and resize failure limits below. No code change. |
| `4D Plugin API/Classes/C_TEXT.cpp`, `.h` | Active scalar wrapper: copies parameter/descriptor/NSString text into C++ storage; output and return use `PA_SetUnistring`, not descriptor transfer. UTF-8/UTF-16 conversion and copied URL/path helpers inspected. Copy construction/assignment already deleted. No code change. |
| `4D Plugin API/Classes/C_TYPES.h` | Shared types, platform imports and independent `basic_string`/vector storage; no owning SDK setter. |
| `4D Plugin API/Classes/C_BLOB.cpp`, `.h` | Dormant: not listed in either project or included by the live dispatcher. CBytes/C_BLOB hex/base64 helpers copy through C_TEXT; handle allocation failures and non-string issues deferred. No picture wrapper exists. |
| `4D Plugin API/Classes/C_LONGINT.cpp`, `.h` | Dormant scalar-only wrapper; raw numeric copies, no string allocation/alias path. |
| `support/4DPlugin-JSON.cpp`, `.h` | Dormant, no production include/call from the dispatcher or either project. All wide/narrow string, picture, object, collection, numeric, boolean and null setters, existence check, five property getters and stringify inspected. Confirmed ownership fixes below; no automatic activation. |

All six first-party `.cpp` files are compiled by the regression harness, including
the dormant ones. The first-party header declaration
`ARRAY_TEXT::setUTF16StringAtIndex(const PA_Unistring*, uint32_t)` has no
definition or call; it is not an implemented hidden ownership path.

SDK implementation/headers/exports above are a separate unchanged boundary,
not first-party fix targets. `include/json/{allocator,assertions,autolink,config,
features,forwards,json,reader,value,version,writer}.h` and
`a/libjsoncpp.a`, `lib/windows/jsoncpp.lib`, `lib/windows64/jsoncpp.lib`
are third-party inputs; no first-party source includes jsoncpp. Native projects,
SDK files, manifests, localization, prebuilt plugin bundles, zip/dmg, libraries
and manual release workflows retain baseline bytes and modes.

## Contracts and fixes

| Bundled SDK implementation | Contract and caller consequence |
|---|---|
| `PA_CreateUnistring` 265-273 | Engine creates caller owner; error output is not safe to inspect/dispose. The new property lookup checks error first and throws, preserving caller output bytes. Other void-boundary allocation paths are deferred. |
| `PA_SetStringInArray` 4933-4948 | Only accepts Unicode array, non-null data/string pointer and `0 <= index <= count`; disposes old descriptor and shallow-transfers new descriptor. Rejected strings remain caller-owned; index zero is accepted. |
| `PA_GetStringInArray` 4738-4755 | Borrowed alias, must be copied before replacement/shrink/clear. Wrapper input already copies it. |
| `PA_SetStringVariable` 4254-4259 | Shallow alias, no copy or disposal of overwritten owner. Four JSON string setters now initialize Undefined instead of allocating an empty string that is overwritten. Their independently created value is cleared once through the variable after synchronous property assignment. |
| `PA_CreateVariable` 3920-4066; `PA_SetPictureVariable` 4280-4285 | Picture initialization allocates an owner, setter aliases without disposing it. `ob_set_p` now initializes Undefined. Existing consuming input semantics remain: clearing its argument releases that input after property assignment. |
| `PA_GetObjectProperty` 637-652; `PA_GetCollectionElement` 587-602 | Successful result owns a value (explicit SDK clear-after-use comment). Five JSON property getters now guard successful results with RAII. Scalars copy before clear, including wrong-kind outputs; matching object/collection returns disarm cleanup to hand ownership to the caller. No clear-and-relabel borrowed alias. No first-party collection-element calls. |
| `PA_ExecuteCommandByID` 5778-5810 | Initial handle failure returns raw fType 0; `PA_GetVariableKind` normalizes this to string. Stringify now checks error before inspecting/adopting result, with RAII for successful result and independent synchronous copied object argument. Errors are captured before any cleanup overwrites last error. Failed outputs are neither read nor cleared. |
| `PA_SetUnistring` 289-299; `PA_ReturnString` | Engine content setter, distinct from array struct transfer. Host models copy semantics, separately tests survival after local destruction. Not a real 4D engine certification. |
| SDK object/collection setters and disposal helpers | Setters shallow-alias references; successful handoffs must survive source disposal and the receiving caller must release them. Stringify's OB Copy input is independent of the original source. |

Lookup and stringify API failures now throw `std::runtime_error`. Missing
properties and valid kind mismatches retain their previous scalar defaults
and text-output behavior. This is a deliberate error-channel change confined
to dormant native helpers; the exported command's behavior is unchanged.
`ob_is_defined` is a boolean existence convenience, not an owning getter.
SDK JSON parse synchronously borrows its input; no first-party parse call exists.
SDK-only app-path/picture-data strings and C_POINTER metadata remain ambiguous,
unmodified and outside any ownership certification.

## Reproducible, synthetic-only evidence

On macOS, with existing Clang/Python tools:

```sh
python3 tests/ownership/run.py --baseline --output /absolute/scratch/ownership
```

The runner compiles the SAME final actual production files and complete unchanged
SDK for fixed and immutable `git archive` baseline runs. The only production
exemptions are JSON `.cpp`/`.h` and the related root README; it checks every
other baseline tracked file byte-for-byte, and all original executable modes.
The existing byte-preservation test has a narrow exception for those two
authorized JSON edits; that test file is the only additional test exemption.
No native project/release scripts or real plugin binary is run.

`platform.h` replaces all five SystemConfiguration identity functions BEFORE
compilation and dispatch. Their only linked implementations return synthetic
text, a synthetic `en0` selector fixture, null fields or no interfaces.
The runner asserts no unresolved SCNetworkInterface symbols and no
SystemConfiguration framework linkage. No hardware/network identity, serial,
registry, IOKit, WMI, user data or configuration is queried. Only the synthetic
engine is installed in `gCall4D`.

The suite runs ASan + UBSan and explicitly tracks all SDK string allocations,
handles, object/collection references and pictures; macOS LSan is unavailable
and disabled. Allocated and null empty representations are both exercised.
ASCII/BMP/supplementary/empty fixtures assert exact UTF-8 bytes and UTF-16
units, allocated capacity and terminators; array replacement/shrink/clear,
rejected setters (including wrong type/null data/negative/too-large index/null
input), copied borrowed input, owning handoff survival, synchronous argument
cleanup, poisoned failed outputs, raw command sentinel, scalar content setters
and synthetic dispatcher output shape are separate cases.

Baseline: **24 passing controls, 38 exact expected failures**.
Fixed: **62 passes, zero unexpected failures**, plus one separately labelled
known failing initial-array-create probe on each source revision.
Named baseline failures are initial string/picture-owner leaks, scalar and
mismatched getter leaks, UBSan invalid-kind reads of poisoned getter/command
outputs, invalid initial property-key adoption, raw-sentinel string read and
failed OB Copy consumption. `run.py` checks the exact marker for every case;
an arbitrary compiler/sanitizer crash does not count as a reproduction.

## Explicit deferrals and platform gaps

- The existing void `ARRAY_TEXT::toParamAtIndex` has no explicit error channel.
  Failed initial array creation publishes a success-shaped null descriptor and
  can lose an unaccepted string. The separately failing probe documents this;
  it is NOT silently fixed, passed or certified. A safe API-boundary redesign
  is required instead of introducing a success-shaped empty fallback.
- SDK resize-OOM updates element count even on failure. Normal-array passes do
  not fix or certify that SDK behavior. No SDK production edits are authorized.
- Remaining void setter/string creation failures, conversion allocation failures,
  CBytes/non-string handle failures, and ambiguous SDK-only ownership are
  deferred. Only the new checked property-key creation path claims failure
  preservation evidence.
- Windows branches received static ownership inspection only. No Windows host
  or actual 4D host was executed, and no real hardware enumeration was allowed.
  Foundation/CoreFoundation synthetic conversions are not a real 4D engine.
- Xcode Deployment has signing/notarization/packaging side effects. Existing
  Build/Bump workflows are manual and untouched. The new path-scoped
  `macos-latest` Ownership workflow only compiles this unsigned harness and runs
  offline tests; no dispatch, release, signing, packaging, tag or upload.
