#!/usr/bin/env bash
# One-command install of the Black Eclipse desktop app from a published release.
#
# Downloads the native package for this machine (.deb on Debian/Ubuntu, .dmg on
# Apple silicon), verifies its SHA-256 when the release publishes
# SHA256SUMS.txt, and installs it. No Node.js, Python, git or admin tooling is
# required beyond the OS package manager - the payload carries its own runtimes.
#
# This is the SAME artifact the README download buttons serve. It is not a
# source build: nothing is compiled.
#
# Trust model: the packages are unsigned. HTTPS plus a SHA-256 digest protects
# against a corrupted or truncated download, not against a compromised release
# host. Use --sha256 <hex> to pin a digest obtained out of band. The computed
# digest is always printed so you can compare it with the release page.
#
# Usage:
#   ./install-release.sh [--version <tag>] [--sha256 <hex>] [--no-launch]
#                        [--dry-run] [--repo <owner/name>]
#
# Not supported here: Windows (-full offline edition is Windows-only), Intel
# macOS (only an arm64 build is produced). Use the README download links.

set -euo pipefail

REPO="${BLACK_ECLIPSE_REPO:-dumbpabloo/BlackEclipse-V1.0}"
VERSION="latest"
SHA256=""
NO_LAUNCH=0
DRY_RUN=0

step() { printf '[install] %s\n' "$*"; }
fail() { printf '[install] %s\n' "$*" >&2; exit 1; }

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="${2:?--version needs a value}"; shift 2 ;;
    --sha256) SHA256="${2:?--sha256 needs a value}"; shift 2 ;;
    --no-launch) NO_LAUNCH=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --repo) REPO="${2:?--repo needs a value}"; shift 2 ;;
    -h|--help) usage ;;
    *) fail "Unknown argument: $1" ;;
  esac
done

printf '%s' "$REPO" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' \
  || fail "Repository must look like 'owner/name' (got '$REPO')."
if [ -n "$SHA256" ]; then
  printf '%s' "$SHA256" | grep -Eq '^[0-9a-fA-F]{64}$' || fail "--sha256 must be 64 hexadecimal characters."
  SHA256="$(printf '%s' "$SHA256" | tr 'A-F' 'a-f')"
fi

uname_s="$(uname -s)"
uname_m="$(uname -m)"

case "$uname_s" in
  Linux)  OS="linux" ;;
  Darwin) OS="macos" ;;
  *)      fail "Unsupported OS '$uname_s'. Windows users: run install-release.ps1." ;;
esac

if [ "$OS" = "linux" ]; then
  [ "$uname_m" = "x86_64" ] || [ "$uname_m" = "amd64" ] \
    || fail "Only the amd64 Linux build is published (this machine is $uname_m)."
  ASSET="BlackEclipse-linux-amd64.deb"
else
  [ "$uname_m" = "arm64" ] || fail "Only the Apple-silicon build is published (this machine is $uname_m)."
  ASSET="BlackEclipse-mac-arm64.dmg"
fi

TAG=""
if [ "$VERSION" != "latest" ]; then
  case "$VERSION" in
    v*) TAG="$VERSION" ;;
    *)  TAG="v$VERSION" ;;
  esac
fi

if [ -n "$TAG" ]; then
  BASE="https://github.com/$REPO/releases/download/$TAG"
else
  BASE="https://github.com/$REPO/releases/latest/download"
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/blackeclipse-install.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PACKAGE="$WORK/$ASSET"
SUMS="$WORK/SHA256SUMS.txt"

step "Release:   $BASE"
step "Package:   $ASSET"
step "Target:    $PACKAGE"

if [ "$DRY_RUN" = "1" ]; then
  step "Dry run: nothing downloaded, nothing installed."
  exit 0
fi

step "Downloading..."
curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 -o "$PACKAGE" "$BASE/$ASSET" \
  || fail "Download failed. Check the release exists at $BASE"

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "$PACKAGE" | awk '{print $1}')"
else
  ACTUAL="$(shasum -a 256 "$PACKAGE" | awk '{print $1}')"
fi
step "SHA-256:   $ACTUAL"

EXPECTED=""
if curl -fsSL --retry 2 -o "$SUMS" "$BASE/SHA256SUMS.txt" 2>/dev/null; then
  EXPECTED="$(awk -v want="$ASSET" '{h=$1; f=$2; sub(/^\*/, "", f); if (f==want) print tolower(h)}' "$SUMS" | head -n1)"
  [ -n "$EXPECTED" ] || step "SHA256SUMS.txt has no entry for $ASSET."
else
  step "No SHA256SUMS.txt published for this release."
fi

if [ -n "$SHA256" ]; then
  if [ -n "$EXPECTED" ] && [ "$EXPECTED" != "$SHA256" ]; then
    fail "SHA256SUMS.txt disagrees with --sha256. Refusing to install."
  fi
  [ "$ACTUAL" = "$SHA256" ] || fail "Downloaded file does not match --sha256. Refusing to install."
  step "Checksum:  matches --sha256."
elif [ -n "$EXPECTED" ]; then
  [ "$ACTUAL" = "$EXPECTED" ] || fail "Checksum mismatch: SHA256SUMS.txt says $EXPECTED. Refusing to install."
  step "Checksum:  matches SHA256SUMS.txt."
else
  step "Continuing without a published checksum. Compare the digest above with the release page."
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "Installing needs root; sudo is not available."
  SUDO="sudo"
fi

if [ "$OS" = "linux" ]; then
  step "Installing the .deb (this replaces any previous version; your data is kept)..."
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get install -y "$PACKAGE"
  else
    $SUDO dpkg -i "$PACKAGE"
  fi
  step "Installed. Launch it from your applications menu, or run 'blackeclipse'."
  if [ "$NO_LAUNCH" = "0" ] && command -v blackeclipse >/dev/null 2>&1; then
    nohup blackeclipse >/dev/null 2>&1 &
    step "Launched Black Eclipse."
  fi
else
  step "Mounting the .dmg..."
  MOUNT="$WORK/mnt"
  mkdir -p "$MOUNT"
  hdiutil attach -nobrowse -quiet -mountpoint "$MOUNT" "$PACKAGE"
  trap 'hdiutil detach -quiet "$MOUNT" 2>/dev/null || true; rm -rf "$WORK"' EXIT
  [ -d "$MOUNT/Black Eclipse.app" ] || fail "The .dmg does not contain 'Black Eclipse.app'."
  step "Installing to /Applications (your data is kept)..."
  $SUDO ditto "$MOUNT/Black Eclipse.app" "/Applications/Black Eclipse.app"
  hdiutil detach -quiet "$MOUNT" || true
  # The build is unsigned, so the download carries a quarantine flag Gatekeeper
  # would honour. Clearing it is what the README's right-click > Open does.
  xattr -dr com.apple.quarantine "/Applications/Black Eclipse.app" 2>/dev/null || true
  step "Installed: /Applications/Black Eclipse.app"
  if [ "$NO_LAUNCH" = "0" ]; then
    open -a "Black Eclipse" && step "Launched Black Eclipse."
  fi
fi

step "First open asks for a model key in Settings > AI (or point it at Ollama)."
