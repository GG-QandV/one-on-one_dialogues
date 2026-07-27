#!/usr/bin/env bash
set -euo pipefail

# Download whisper.cpp models for speech-local
# Usage: ./download_model.sh [base|tiny|small]

MODEL="${1:-base}"
MODELS_DIR="${MODELS_DIR:-$HOME/.local/share/speech-local/models}"

BASE_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

case "$MODEL" in
    base)  FILE="ggml-base.bin" ;;
    tiny)  FILE="ggml-tiny.bin" ;;
    small) FILE="ggml-small.bin" ;;
    *)
        echo "Usage: $0 [base|tiny|small]"
        exit 1
        ;;
esac

mkdir -p "$MODELS_DIR"
echo "Downloading $FILE to $MODELS_DIR/"

curl -L -o "$MODELS_DIR/$FILE" "$BASE_URL/$FILE"

echo "Model downloaded: $MODELS_DIR/$FILE"
ls -lh "$MODELS_DIR/$FILE"
