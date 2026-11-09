# Run from the checkout root before/after the workflow's Release|x64 MSBuild.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('clean', 'verify')]
    [string] $Action
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$product = if ($env:PRODUCT_NAME) { $env:PRODUCT_NAME } else { 'Media Access Control' }
$base = if ($env:BASE_DIR) { $env:BASE_DIR } else { 'test' }
if ($base -cne 'test' -or $product -in @('.', '..') -or $product -match '[/\\\r\n]') {
    throw 'Unsafe product/base path'
}
$root = (Get-Location).ProviderPath

# Resolve only the requested components, case-insensitively as on Windows.
# This also lets offline tests exercise Windows casing on case-sensitive hosts.
function Resolve-BoundedPath([string] $Relative) {
    $current = $root
    foreach ($part in ($Relative -split '/')) {
        $candidate = Join-Path $current $part
        if (Test-Path -LiteralPath $current -PathType Container) {
            $matches = @(Get-ChildItem -LiteralPath $current -Force |
                Where-Object { $_.Name -ieq $part })
            if ($matches.Count -gt 1) { throw "Ambiguous case in path: $candidate" }
            if ($matches.Count -eq 1) { $candidate = $matches[0].FullName }
        }
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Symlink/reparse point in generated path: $candidate"
            }
        }
        $current = $candidate
    }
    return $current
}

$output = Resolve-BoundedPath "test/plugins/$product.bundle/contents/windows64/$product.4DX"
switch ($Action) {
    'clean' {
        # IntDir for Release|x64 in the vcxproj; never remove Objs or the bundle.
        foreach ($path in @($output, (Resolve-BoundedPath 'Objs/Release'))) {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
            }
        }
    }
    'verify' {
        if (-not (Test-Path -LiteralPath $output -PathType Leaf) -or
            (Get-Item -LiteralPath $output).Length -eq 0) {
            throw "Expected Release|x64 vcxproj output missing or empty: $output"
        }
        Write-Host "Verified Windows x64 output: $output"
    }
}
