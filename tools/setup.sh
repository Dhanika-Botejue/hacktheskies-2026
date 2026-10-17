#!/bin/sh
# Downloads the Brush Gaussian-splat trainer (Apple Silicon) into tools/bin.
set -e
cd "$(dirname "$0")"
VERSION=v0.3.0
SHA=65b2631398c839be3c1d4d7160fe2326389dec87830aac0710985e6690a1048c
mkdir -p bin
TMP=$(mktemp -d)
curl -sSL -o "$TMP/brush.tar.xz" "https://github.com/ArthurBrussee/brush/releases/download/$VERSION/brush-app-aarch64-apple-darwin.tar.xz"
echo "$SHA  $TMP/brush.tar.xz" | shasum -a 256 -c -
tar -xJf "$TMP/brush.tar.xz" -C "$TMP"
mv "$TMP"/brush-app-aarch64-apple-darwin/brush_app bin/brush_app
xattr -d com.apple.quarantine bin/brush_app 2>/dev/null || true
rm -rf "$TMP"
echo "Brush installed at tools/bin/brush_app"
