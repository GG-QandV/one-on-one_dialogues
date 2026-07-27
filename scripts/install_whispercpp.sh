#!/usr/bin/env bash
set -euo pipefail

# Install whisper.cpp for speech-local
# Target: ~/.local/share/speech-local/whisper.cpp

WHISPER_DIR="${WHISPER_DIR:-$HOME/.local/share/speech-local/whisper.cpp}"
REPO="${REPO:-https://github.com/ggerganov/whisper.cpp.git}"

echo "Installing whisper.cpp to $WHISPER_DIR"

if [ -d "$WHISPER_DIR" ]; then
    echo "whisper.cpp already exists at $WHISPER_DIR, pulling updates"
    cd "$WHISPER_DIR" && git pull
else
    mkdir -p "$(dirname "$WHISPER_DIR")"
    git clone --depth 1 "$REPO" "$WHISPER_DIR"
fi

cd "$WHISPER_DIR"
make -j "$(nproc)"

echo "whisper.cpp installed successfully"
echo "Binaries: $WHISPER_DIR/main (whisper-cli)"
