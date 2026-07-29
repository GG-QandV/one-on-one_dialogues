"""Критерий 11 (§21): черновик недоставим без действия пользователя;
горячая клавиша работает.

Смешанный критерий (AUTO + LIVE, H2 карта): здесь проверяется только
AUTO-часть — статический граф вызовов. Импорт app.delivery.clipboard.copy
не должен встречаться нигде в конвейере генерации черновика (app/drafts,
app/queue.py, app/main.py); единственный легитимный путь — HTTP-хендлер
POST /api/clipboard, вызываемый по явному клику пользователя в UI.

Горячая клавиша (LIVE-часть) руками: см. detail при провале AUTO-части
или прогон с --live.
"""

from __future__ import annotations

from pathlib import Path

from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult

APP_ROOT = Path(__file__).resolve().parent.parent.parent / "app"
ALLOWED_CALLERS = {APP_ROOT / "ui" / "server.py"}
#: Ищем именно импорт модуля, а не любое упоминание строки "delivery.clipboard"
#: (она встречается в app/config.py как ключ конфига delivery.clipboard_hotkey).
NEEDLE = "delivery.clipboard import"


async def _run(_env: CheckEnv) -> CheckResult:
    offenders: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if path == APP_ROOT / "delivery" / "clipboard.py":
            continue
        text = path.read_text(encoding="utf-8")
        if NEEDLE in text and path not in ALLOWED_CALLERS:
            offenders.append(str(path.relative_to(APP_ROOT.parent)))

    if offenders:
        return CheckResult(
            11,
            "черновик недоставим без действия пользователя (AUTO-часть)",
            CheckKind.AUTO,
            False,
            f"найден путь к clipboard.copy вне HTTP-хендлера: {offenders}",
        )

    server_path = APP_ROOT / "ui" / "server.py"
    if NEEDLE not in server_path.read_text(encoding="utf-8"):
        return CheckResult(
            11,
            "черновик недоставим без действия пользователя (AUTO-часть)",
            CheckKind.AUTO,
            False,
            "ожидаемый вызов clipboard.copy в _clipboard_handler не найден — "
            "критерий не может подтвердить путь доставки вообще",
        )

    return CheckResult(
        11,
        "черновик недоставим без действия пользователя (AUTO-часть)",
        CheckKind.AUTO,
        True,
        "единственный вызов delivery.clipboard.copy — в app/ui/server.py "
        "(_clipboard_handler, POST /api/clipboard по клику пользователя); "
        "проверка горячей клавиши — LIVE, руками",
    )


CHECK = CheckDef(
    number=11, title="черновик недоставим без действия пользователя", kind=CheckKind.AUTO, run=_run
)
