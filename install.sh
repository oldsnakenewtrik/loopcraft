#!/usr/bin/env bash
# loopcraft installer — fetches the single-file harness onto your PATH.
#
#   curl -fsSL https://raw.githubusercontent.com/oldsnakenewtrik/loopcraft/v0.2.4/install.sh | bash
#
# Pin to a tag (default v0.2.4). Override with env vars:
#   LOOPCRAFT_REF=main   LOOPCRAFT_BIN=/usr/local/bin   bash install.sh
set -euo pipefail

REPO="oldsnakenewtrik/loopcraft"
REF="${LOOPCRAFT_REF:-v0.2.4}"
DEST="${LOOPCRAFT_BIN:-$HOME/.local/bin}"
URL="https://raw.githubusercontent.com/${REPO}/${REF}/loopcraft.py"

command -v python3 >/dev/null 2>&1 || {
  echo "loopcraft needs python3 (3.9+) on PATH — not found. Install it first." >&2
  exit 1
}

mkdir -p "$DEST"
echo "Downloading loopcraft (${REF}) -> ${DEST}/loopcraft"
curl -fsSL "$URL" -o "${DEST}/loopcraft"
chmod +x "${DEST}/loopcraft"

echo
echo "Installed: ${DEST}/loopcraft"
case ":$PATH:" in
  *":$DEST:"*) ;;
  *) echo "NOTE: $DEST is not on your PATH. Add it:"
     echo "      export PATH=\"$DEST:\$PATH\"" ;;
esac
echo
echo "Smoke test:  loopcraft --help"
echo "First run:   loopcraft -C . -g \"<goal>\" --verify \"<test cmd>\" --max-attempts 3"
