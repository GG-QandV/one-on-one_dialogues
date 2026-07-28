#!/usr/bin/env python3
"""scripts/gen_tree.py — перегенерирует TREE_speech_local.txt.

Обходит дерево репозитория и печатает его в формате, похожем на `tree -h`:
размер файла/директории в человекочитаемых единицах в квадратных скобках.

Ручные комментарии (`# ...`) после существующих записей сохраняются между
перегенерациями — они привязаны к относительному пути и переносятся из
старого файла, если путь в дереве не изменился. Для новых путей комментарий
пустой — его можно дописать руками, следующий прогон его не потеряет.

Использование:
    python scripts/gen_tree.py                  # обновить TREE_speech_local.txt
    python scripts/gen_tree.py --check           # ничего не писать, код возврата
                                                  # 1, если дерево устарело
    python scripts/gen_tree.py --also-backup     # синхронизировать копию в
                                                  # backup/backup_docs/
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREE_FILE = ROOT / "TREE_speech_local.txt"
BACKUP_TREE_FILE = ROOT / "backup" / "backup_docs" / "TREE_speech_local.txt"

# Каталоги/паттерны, которые не имеет смысла показывать в дереве:
# рантайм-данные, кеши инструментов, артефакты сборки — не источник истины.
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "node_modules",
    "data",       # рантайм БД/аудио, в .gitignore
    "models",     # скачиваемые GGML-модели whisper, не коммитятся
    "c0_out",     # артефакты scripts/c0_spike.py
}
EXCLUDE_SUFFIXES = (".pyc", ".egg-info")


def human_size(n: int) -> str:
    if n < 1000:
        return f"{n}B"
    size = float(n)
    for unit in ("K", "M", "G", "T"):
        size /= 1024
        if size < 10:
            return f"{size:.1f}{unit}"
        if size < 1000:
            return f"{round(size)}{unit}"
    return f"{round(size)}T"


def should_skip(path: Path) -> bool:
    return path.name in EXCLUDE_DIRS or any(
        path.name.endswith(suf) for suf in EXCLUDE_SUFFIXES
    )


def load_comments(tree_text: str) -> dict[str, str]:
    """Извлекает {относительный_путь: '# комментарий'} из существующего дерева."""
    comments: dict[str, str] = {}
    stack: list[str] = []  # имена директорий по уровням вложенности
    line_re = re.compile(
        r"^(?P<prefix>[│ ]*)(?:├── |└── )(?P<name>[^\[#]+?)"
        r"(?:\s{2,}\[[^\]]*\])?(?:\s{2,}(?P<comment>#.*))?$"
    )
    for raw in tree_text.splitlines()[1:]:  # первая строка — корень "speech-local/"
        m = line_re.match(raw)
        if not m:
            continue
        depth = len(m.group("prefix")) // 4
        name = m.group("name").rstrip()
        stack = stack[:depth]
        stack.append(name.rstrip("/"))
        comment = m.group("comment")
        if comment:
            comments["/".join(stack)] = comment.strip()
    return comments


def iter_entries(directory: Path):
    entries = [p for p in directory.iterdir() if not should_skip(p)]
    return sorted(entries, key=lambda p: p.name)


def build_tree_lines(
    directory: Path,
    comments: dict[str, str],
    prefix: str = "",
    rel_prefix: str = "",
) -> list[str]:
    lines: list[str] = []
    entries = iter_entries(directory)
    for i, entry in enumerate(entries):
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        rel = f"{rel_prefix}{entry.name}" if not rel_prefix else f"{rel_prefix}/{entry.name}"
        try:
            size = human_size(entry.stat().st_size)
        except OSError:
            size = "  ?B"
        name_field = entry.name + ("/" if entry.is_dir() else "")
        size_field = f"[{size:>4}]" if len(size) <= 4 else f"[{size}]"
        comment = comments.get(rel, "")
        line = f"{prefix}{connector}{name_field:<38} {size_field}"
        if comment:
            line += f"  {comment}"
        lines.append(line.rstrip())
        if entry.is_dir():
            extension = "    " if is_last else "│   "
            lines.extend(
                build_tree_lines(entry, comments, prefix + extension, rel)
            )
    return lines


def render() -> str:
    old_comments: dict[str, str] = {}
    if TREE_FILE.exists():
        old_comments = load_comments(TREE_FILE.read_text(encoding="utf-8"))
    body = build_tree_lines(ROOT, old_comments)
    return "speech-local/\n" + "\n".join(body) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="не писать файл, только сверить")
    ap.add_argument(
        "--also-backup", action="store_true", help="обновить и backup/backup_docs/ копию"
    )
    args = ap.parse_args()

    new_content = render()

    if args.check:
        current = TREE_FILE.read_text(encoding="utf-8") if TREE_FILE.exists() else ""
        if current != new_content:
            print("TREE_speech_local.txt устарело, перегенерируйте: python scripts/gen_tree.py")
            return 1
        print("TREE_speech_local.txt актуально")
        return 0

    TREE_FILE.write_text(new_content, encoding="utf-8")
    print(f"записано {TREE_FILE}")
    if args.also_backup:
        BACKUP_TREE_FILE.write_text(new_content, encoding="utf-8")
        print(f"записано {BACKUP_TREE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
