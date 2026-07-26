#!/usr/bin/env bash
# CI-проверка: нет пустых .py в app/ и пустых .md в CONTRACTS/
set -euo pipefail

ROOT="${1:-$(dirname "$0")/..}"
cd "$ROOT"

errors=0

# Пустые .py в app/ (кроме __init__.py)
while IFS= read -r -d '' f; do
    if [[ "$(basename "$f")" == "__init__.py" ]]; then
        continue
    fi
    if [[ ! -s "$f" ]]; then
        echo "STUB_PY: $f"
        errors=$((errors + 1))
    fi
done < <(find app/ -name '*.py' -type f -print0)

# Пустые .md в CONTRACTS/
while IFS= read -r -d '' f; do
    if [[ ! -s "$f" ]]; then
        echo "STUB_MD: $f"
        errors=$((errors + 1))
    fi
done < <(find CONTRACTS/ -name '*.md' -type f -print0)

if [[ $errors -gt 0 ]]; then
    echo "CHECK_NO_STUBS FAILED: $errors stub file(s) found"
    exit 1
fi

echo "CHECK_NO_STUBS OK: no stubs"
