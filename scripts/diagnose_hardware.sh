#!/usr/bin/env bash
set -euo pipefail

# Diagnose audio hardware and system state for speech-local

echo "=== PipeWire ==="
pw-dump | python3 -c "
import json, sys
data = json.load(sys.stdin)
nodes = [n for n in data if n.get('type') == 'PipeWire:Interface:Node']
for n in nodes:
    info = n.get('info', {})
    props = info.get('props', {})
    if props.get('media.class') in ('Audio/Source', 'Audio/Sink'):
        print(f\"  {props.get('node.name', '?'):30s} class={props.get('media.class', '?')}\")
"

echo ""
echo "=== Audio sinks (pw-cli) ==="
pw-cli list-objects | grep -E "node|name|class" | head -30

echo ""
echo "=== Capture nodes ==="
pw-cli list-objects | grep -E "Audio/Source|Audio/Sink" | head -10

echo ""
echo "=== Memory info ==="
if [ -f /sys/fs/cgroup/memory.current ]; then
    MEM=$(cat /sys/fs/cgroup/memory.current)
    echo "cgroup memory.current: $((MEM / 1048576)) MB"
elif [ -f /proc/self/status ]; then
    grep VmRSS /proc/self/status
fi

echo ""
echo "=== Whisper models ==="
MODELS="${HOME}/.local/share/speech-local/models"
if [ -d "$MODELS" ]; then
    ls -lh "$MODELS"
else
    echo "No models found at $MODELS"
fi

echo ""
echo "=== System info ==="
echo "Kernel: $(uname -r)"
echo "CPU: $(grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2)"
echo "RAM: $(free -h | grep Mem | awk '{print $2}') total"
echo "Python: $(python3 --version 2>/dev/null || echo 'not found')"
