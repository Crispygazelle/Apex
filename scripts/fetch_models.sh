#!/usr/bin/env bash
# Download the offline speech models the voice layer expects.
# Run from the repo root. The models/ directory is gitignored.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODELS="$ROOT/models"
mkdir -p "$MODELS"

VOSK_NAME="vosk-model-small-en-us-0.15"
VOSK_URL="https://alphacephei.com/vosk/models/${VOSK_NAME}.zip"
PIPER_STEM="en_US-amy-low"
PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low"

if [[ ! -d "$MODELS/$VOSK_NAME/am" ]]; then
  echo "Fetching $VOSK_NAME (~40 MB)..."
  curl -L --fail --retry 3 -o "/tmp/${VOSK_NAME}.zip" "$VOSK_URL"
  unzip -q -o "/tmp/${VOSK_NAME}.zip" -d "$MODELS"
else
  echo "Vosk model already present."
fi

if [[ ! -f "$MODELS/${PIPER_STEM}.onnx" ]]; then
  echo "Fetching Piper $PIPER_STEM..."
  curl -L --fail --retry 3 -o "$MODELS/${PIPER_STEM}.onnx" \
    "${PIPER_BASE}/${PIPER_STEM}.onnx"
  curl -L --fail --retry 3 -o "$MODELS/${PIPER_STEM}.onnx.json" \
    "${PIPER_BASE}/${PIPER_STEM}.onnx.json"
else
  echo "Piper voice already present."
fi

echo "Models ready in $MODELS"
