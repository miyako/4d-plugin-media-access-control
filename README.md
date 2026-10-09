![version](https://img.shields.io/badge/version-19%2B-5682DF)
![platform](https://img.shields.io/static/v1?label=platform&message=mac-intel%20|%20mac-arm%20|%20win-64&color=blue)
[![license](https://img.shields.io/github/license/miyako/4d-plugin-media-access-control)](LICENSE)
![downloads](https://img.shields.io/github/downloads/miyako/4d-plugin-media-access-control/total)

# 4d-plugin-media-access-control

This plugin returns the list of MAC (Media Access Control) addresses of the local computer. It asks the operating system for its network interfaces (the SystemConfiguration framework on macOS, the IP Helper API on Windows) and hands the result back as four parallel `Text` arrays — one entry per interface — plus a single `Text` return value holding one "primary" address.

The plugin exposes one command.

| Command | Returns | Purpose |
|---|---|---|
| [Get hardware address](#get-hardware-address) | `Text` | Fills four arrays with address, type, name and display name of every network interface, and returns one primary MAC address |

**Platforms:** macOS (Intel and Apple Silicon) and Windows 64-bit, 4D 19 and later.

---

## Requirements & platform notes

- **Read the arrays, not just the return value.** The return value is a single address chosen by a platform-specific rule (see [Get hardware address](#get-hardware-address)). The rule is not the same on macOS and Windows, and on Windows it can pick a virtual adapter or return an empty string on a Wi-Fi-only machine. If you need a specific interface, select it yourself from the arrays (see the examples).
- **The meaning of parameters 3 and 4 differs by platform.** On macOS they are the BSD interface name and the localized display name. On Windows they are the adapter description and the adapter "friendly name".
- **Failure is silent.** The command never raises a 4D error. If the system cannot be queried, or there are no interfaces, you get empty arrays and an empty string.
- **Behavior marked *(revised source)*** describes the corrected `4DPlugin-Media-Access-Control.cpp` that accompanies this document. It is true once that source is built; an older binary may behave as described under [Error handling & troubleshooting](#error-handling--troubleshooting).
- **Minimum OS versions:** this document does not state one. The APIs used are long-standing, but no minimum was verified.
- **Threading:** the command keeps no shared state between calls. Whether it can run in a preemptive process is declared in the plugin's `manifest.json`, which was not reviewed. The plugin's own test method is marked `preemptive: capable`.

---

## Get hardware address

### Syntax

```
Get hardware address ( addresses ; types ; names ; displayNames ) → Text
```

| Parameter | Type | Description |
|---|---|---|
| `addresses` | Text array | Output. MAC address of each interface, for example `a4:83:e7:12:34:56`. Empty string when the interface has no 6-byte hardware address. |
| `types` | Text array | Output. Interface type of each interface. |
| `names` | Text array | Output. **macOS:** BSD name (for example `en0`). **Windows:** adapter description. |
| `displayNames` | Text array | Output. **macOS:** localized display name. **Windows:** adapter friendly name. |
| Result | Text | The primary MAC address, or `""` if none qualifies. |

The plugin writes to all four array parameters on every call, so pass all four. Declare them as `ARRAY TEXT` before the call, as the sample does.

### Description

After the call the four arrays have the same size, and element `n` of each array describes the same interface. The order is whatever the operating system returns; don't rely on it. Interfaces appear even when they have no MAC address (loopback, tunnels, and so on), with `""` in `addresses`.

**On macOS**, every interface returned by the system is listed.
- `addresses` is the hardware address string provided by the system.
- `types` is the system's interface type string, for example `Ethernet` or `IEEE80211`.
- `names` is the BSD name, and `displayNames` is the localized name, so it changes with the system language.
- A value the system does not provide is returned as `""`. *(revised source)* Each field is checked on its own, so an interface without a MAC address still reports its type, name and display name. In the older source, a missing MAC address also blanked those three fields.
- The result is the address of the interface named `en0`. On a Mac with no `en0`, or when `en0` has no address, the result is `""`.

**On Windows**, every adapter returned by the system is listed.
- `addresses` is formatted as lowercase, colon-separated hex (`aa:bb:cc:dd:ee:ff`), and only for adapters whose physical address is exactly 6 bytes. Others get `""`.
- `types` is one of `Other`, `Ethernet`, `Token-Ring`, `PPP`, `Software-Loopback`, `ATM`, `IEEE80211`, `Tunnel` or `IEEE1394`. An adapter of any other type, such as Fast Ethernet, Gigabit or mobile broadband, is listed with `""`.
- The result is the address of an adapter whose type is `Ethernet`, that has a 6-byte address, and whose description does not contain the text `Bluetooth`. When several adapters qualify, **the last one in the system's order is returned**. That can be a virtual adapter (Hyper-V, VPN, WSL and similar).
- **A Wi-Fi adapter has type `IEEE80211`, so it never becomes the result.** On a Windows machine with only Wi-Fi, the result is `""` even though `addresses` contains the Wi-Fi MAC.
- *(revised source)* The adapter list is requested without the per-adapter IP address lists, and the call is retried up to three times if the system reports that the buffer was too small.

### Example

From the plugin's own test method (`TEST.4dm`):

```4d
//%attributes = {"preemptive":"capable"}
ARRAY TEXT:C222($addresses;0)
ARRAY TEXT:C222($types;0)
ARRAY TEXT:C222($names;0)
ARRAY TEXT:C222($displayNames;0)

$address:=Get hardware address ($addresses;$types;$names;$displayNames)


ALERT:C41($addresses{1})
```

Note that `$addresses{1}` is out of range if the arrays came back empty (see [Error handling & troubleshooting](#error-handling--troubleshooting)), and the first interface may have `""` as its address.

List every interface as a collection of objects:

```4d
ARRAY TEXT($addresses;0)
ARRAY TEXT($types;0)
ARRAY TEXT($names;0)
ARRAY TEXT($displayNames;0)

$primary:=Get hardware address ($addresses;$types;$names;$displayNames)

$interfaces:=New collection

For ($i;1;Size of array($addresses))
	$interfaces.push(New object("address";$addresses{$i};"type";$types{$i};"name";$names{$i};"displayName";$displayNames{$i}))
End for
```

Pick the first Ethernet or Wi-Fi interface that has a MAC address. This works the same on both platforms, because both report the types `Ethernet` and `IEEE80211`:

```4d
ARRAY TEXT($addresses;0)
ARRAY TEXT($types;0)
ARRAY TEXT($names;0)
ARRAY TEXT($displayNames;0)

$primary:=Get hardware address ($addresses;$types;$names;$displayNames)

$mac:=""
For ($i;1;Size of array($addresses))
	If ($mac="")  //keep the first match
		If ($addresses{$i}#"")
			If (($types{$i}="Ethernet") | ($types{$i}="IEEE80211"))
				$mac:=$addresses{$i}
			End if
		End if
	End if
End for

If ($mac="")
	ALERT("No hardware address found.")
Else
	ALERT($mac)
End if
```

Look up a specific interface by its BSD name (macOS only, because on Windows `names` holds adapter descriptions):

```4d
$index:=Find in array($names;"en0")
If ($index>0)
	ALERT($addresses{$index})
End if
```

---

## Error handling & troubleshooting

- **Empty arrays and `""` mean the query failed or found nothing.** No 4D error is raised, so check `Size of array` before reading `$addresses{1}`.
- **The return value is not a stable machine identifier.** It uses different rules on macOS (`en0`) and Windows (last qualifying Ethernet adapter), and it can change when a VPN, VM or dock adds or removes an interface. For a stable value, pick an interface yourself from the arrays and store it.
- **A Wi-Fi-only Windows machine returns `""`.** The Wi-Fi adapter is reported as `IEEE80211` and is never the result. Read `addresses` instead.
- **Parameters 3 and 4 are not comparable across platforms.** A lookup such as `Find in array($names;"en0")` only makes sense on macOS.
- **Interface order is not guaranteed.** Don't assume that index 1 is the same interface on two machines, or on the same machine after a network change.
- **Entries with `""` in `addresses` are normal.** Loopback, tunnels, and any Windows adapter whose physical address is not 6 bytes are listed with an empty address.
- **Windows adapters of unlisted types have an empty `types` entry.** Only the nine types listed above are named.
- **Older Windows builds can return nothing on machines with many adapters.** The older source reads the adapter list into a single fixed 30,000-byte buffer with one attempt. With many virtual adapters this can overflow, and the command returns empty arrays and `""`. The revised source retries and requests a much smaller list.
- **Declare the arrays as `ARRAY TEXT` and pass all four.** The plugin writes to every array parameter.

---

## Quick reference

```4d
ARRAY TEXT($addresses;0)
ARRAY TEXT($types;0)
ARRAY TEXT($names;0)
ARRAY TEXT($displayNames;0)

$primary:=Get hardware address ($addresses;$types;$names;$displayNames)

If (Size of array($addresses)>0)
	ALERT($addresses{1})
End if

//macOS: address of a given interface by BSD name
$index:=Find in array($names;"en0")
```
