#requires -Version 5.1
<#
.SYNOPSIS
  One-command install of the Black Eclipse desktop app from a published release.

.DESCRIPTION
  Resolves a release (latest by default), downloads the NSIS installer, verifies
  its SHA-256 against the release's SHA256SUMS.txt when that file exists, then
  runs it silently. No Node.js, Python, admin rights or git are required - the
  installer payload carries its own runtimes.

  This is the SAME artifact the README download button serves. It is not a
  source build: nothing is compiled, and nothing from the checkout is used.

  Trust model: the installers are unsigned. HTTPS transport plus the SHA-256 in
  SHA256SUMS.txt protect against a corrupted or truncated download, not against
  a compromised release host. Pass -Sha256 <hex> to pin a digest you obtained
  out of band. A digest is always printed so you can compare it yourself.

.PARAMETER Version
  "latest" (default) or a release tag such as "0.4.0" / "v0.4.0".

.PARAMETER Full
  Install the offline edition (BlackEclipse-Setup-<version>-full.exe), which
  bundles the optional Python feature packs. For machines with no network.

.PARAMETER Sha256
  Expected SHA-256 of the installer. When supplied it is enforced even if the
  release publishes no checksum file.

.PARAMETER SkipChecksum
  Do not require SHA256SUMS.txt. The computed digest is still printed.

.PARAMETER NoLaunch
  Install, but do not start the app afterwards.

.PARAMETER DryRun
  Print what would be downloaded and where, then stop.

.EXAMPLE
  irm https://raw.githubusercontent.com/dumbpabloo/BlackEclipse-V1.0/main/Black-Eclipse-site/scripts/install-release.ps1 -OutFile install-release.ps1
  ./install-release.ps1

.EXAMPLE
  ./install-release.ps1 -Version 0.4.0 -Sha256 <64 hex chars> -NoLaunch
#>
[CmdletBinding()]
param(
  [string]$Version = "latest",
  [switch]$Full,
  [string]$Sha256,
  [switch]$SkipChecksum,
  [switch]$NoLaunch,
  [switch]$DryRun,
  [string]$Repo = $(if ($env:BLACK_ECLIPSE_REPO) { $env:BLACK_ECLIPSE_REPO } else { "dumbpabloo/BlackEclipse-V1.0" })
)

$ErrorActionPreference = "Stop"
try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
  # Older/newer hosts may already enforce TLS 1.2+; nothing to do.
}

function Write-Step([string]$Message) { Write-Host "[install] $Message" }
function Stop-Install([string]$Message) {
  Write-Host "[install] $Message" -ForegroundColor Red
  exit 1
}

if ($Repo -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
  Stop-Install "Repository must look like 'owner/name' (got '$Repo')."
}
if ($Sha256 -and $Sha256 -notmatch '^[0-9a-fA-F]{64}$') {
  Stop-Install "-Sha256 must be 64 hexadecimal characters."
}

$tag = $null
if ($Version -and $Version -ne "latest") {
  $tag = if ($Version.StartsWith("v")) { $Version } else { "v$Version" }
}
$downloadBase = if ($tag) {
  "https://github.com/$Repo/releases/download/$tag"
} else {
  "https://github.com/$Repo/releases/latest/download"
}

# The slim installer has a version-independent alias, so `latest` needs no API
# call. The offline edition is versioned in its own filename, so its exact name
# has to be discovered from the release.
$asset = "BlackEclipse-Setup.exe"
if ($Full) {
  if ($tag) {
    $asset = "BlackEclipse-Setup-$($tag.TrimStart('v'))-full.exe"
  } else {
    Write-Step "Resolving the latest release to find the offline edition..."
    try {
      $api = Invoke-RestMethod -UseBasicParsing `
        -Uri "https://api.github.com/repos/$Repo/releases/latest" `
        -Headers @{ Accept = "application/vnd.github+json"; "User-Agent" = "black-eclipse-install" }
    } catch {
      Stop-Install "Could not read the latest release from the GitHub API. Pass -Version <tag> instead."
    }
    $match = @($api.assets | Where-Object { $_.name -match '^BlackEclipse-Setup-.*-full\.exe$' } | Select-Object -First 1)
    if (-not $match) {
      Stop-Install "That release publishes no offline (-full) edition. Run without -Full."
    }
    $asset = $match[0].name
  }
}

$url = "$downloadBase/$asset"
$sumsUrl = "$downloadBase/SHA256SUMS.txt"
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("blackeclipse-install-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
$installer = Join-Path $work $asset

Write-Step "Release:   $downloadBase"
Write-Step "Installer: $asset"
Write-Step "Target:    $installer"

if ($DryRun) {
  Write-Step "Dry run: nothing downloaded, nothing installed."
  exit 0
}

New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
  Write-Step "Downloading (~141 MB, this takes a minute)..."
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $installer
  } catch {
    Stop-Install "Download failed. Check the release exists at $downloadBase"
  }

  $actual = (Get-FileHash -Algorithm SHA256 -Path $installer).Hash.ToLowerInvariant()
  Write-Step "SHA-256:   $actual"

  $expected = $null
  try {
    $sumsText = (Invoke-WebRequest -UseBasicParsing -Uri $sumsUrl).Content
    foreach ($line in ($sumsText -split "`r?`n")) {
      if ($line -match '^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$' -and $Matches[2] -eq $asset) {
        $expected = $Matches[1].ToLowerInvariant()
        break
      }
    }
    if (-not $expected) {
      Write-Step "SHA256SUMS.txt has no entry for $asset."
    }
  } catch {
    Write-Step "No SHA256SUMS.txt published for this release."
  }

  if ($Sha256) {
    $pinned = $Sha256.ToLowerInvariant()
    if ($expected -and $expected -ne $pinned) {
      Stop-Install "SHA256SUMS.txt disagrees with -Sha256. Refusing to install."
    }
    if ($actual -ne $pinned) {
      Stop-Install "Downloaded file does not match -Sha256. Refusing to install."
    }
    Write-Step "Checksum:  matches -Sha256."
  } elseif ($expected) {
    if ($actual -ne $expected) {
      Stop-Install "Checksum mismatch: SHA256SUMS.txt says $expected. Refusing to install."
    }
    Write-Step "Checksum:  matches SHA256SUMS.txt."
  } elseif (-not $SkipChecksum -and -not $Full) {
    Write-Step "Continuing without a published checksum. Compare the digest above with the release page."
  }

  Write-Step "Installing (per-user, no administrator prompt)..."
  $proc = Start-Process -FilePath $installer -ArgumentList "/S" -Wait -PassThru
  if ($proc.ExitCode -ne 0) {
    Stop-Install "Installer exited with code $($proc.ExitCode)."
  }

  $appExe = Join-Path $env:LOCALAPPDATA "Programs\BlackEclipse\Black Eclipse.exe"
  if (-not (Test-Path $appExe)) {
    Write-Step "Installer finished, but $appExe is missing. Run the installer manually to see why."
    exit 1
  }
  Write-Step "Installed: $appExe"

  if (-not $NoLaunch) {
    Start-Process -FilePath $appExe | Out-Null
    Write-Step "Launched Black Eclipse. First open asks for a model key in Settings > AI."
  } else {
    Write-Step "Skipped launching (-NoLaunch)."
  }
} finally {
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $work
}
