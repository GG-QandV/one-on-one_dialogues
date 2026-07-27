#!/usr/bin/env bash
set -euo pipefail

# Create PipeWire virtual sinks for speech-local
# Requires: pipewire, pipewire-pulse, pw-cli

SINK_NAME="speech-local.monitor"
SINK_DESCRIPTION="speech-local monitor sink"

echo "Creating PipeWire virtual sink: $SINK_NAME"

# Check if already exists
if pw-cli list-objects | grep -q "$SINK_NAME"; then
    echo "Virtual sink already exists"
    exit 0
fi

# Create virtual sink using pw-cli
pw-cli create-node adapter {
    factory.name=support.null-audio-sink
    node.name=$SINK_NAME
    node.description="$SINK_DESCRIPTION"
    media.class=Audio/Sink
    audio.position=FL,FR
}

echo "Virtual sink $SINK_NAME created"
echo ""
echo "To monitor meeting audio, set in config.toml:"
echo "  [streams.meeting]"
echo "  pipewire_node = \"$SINK_NAME\""
echo ""
echo "In Zoom/Meet/Teams, set output device to: $SINK_DESCRIPTION"
