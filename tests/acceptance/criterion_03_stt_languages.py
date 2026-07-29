"""Критерий 3 (§21): распознавание EN/ES/RU/UA/PL через ggml-base.bin.

Эталонные WAV по языку с ожидаемым текстом хранятся в
tests/fixtures/acceptance_stt/<lang>.wav + .json (H2, ловушка: ссылка на
внешний источник делает приёмку невоспроизводимой, значит только в репо).

Прогоняет WER на КАЖДОМ языке, для которого фикстура уже есть — не ждёт,
пока появятся все пять. `passed=True` только когда все пять на месте и все
в пределах порога; если каких-то ещё нет — `passed=None` (не хватает
покрытия), но с честным отчётом по уже проверенным языкам, а не тишиной.
Ошибка WER на уже доступном языке — это `passed=False` независимо от того,
сколько языков ещё не хватает: это не блокировка окружением, а реальный
провал.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "acceptance_stt"
LANGUAGES = ("en", "es", "ru", "uk", "pl")
MODEL_PATH = Path("models/ggml-base.bin")
TITLE = "распознавание EN/ES/RU/UA/PL (ggml-base.bin)"


def _base_prereqs_missing() -> list[str]:
    missing = []
    if shutil.which("whisper-cli") is None:
        missing.append("whisper-cli не найден в PATH")
    if not MODEL_PATH.exists():
        missing.append(f"модель не найдена: {MODEL_PATH}")
    return missing


def _available_languages() -> tuple[list[str], list[str]]:
    available, missing = [], []
    for lang in LANGUAGES:
        wav = FIXTURES_DIR / f"{lang}.wav"
        meta = FIXTURES_DIR / f"{lang}.json"
        if wav.exists() and meta.exists():
            available.append(lang)
        else:
            missing.append(lang)
    return available, missing


async def _run(env: CheckEnv) -> CheckResult:
    base_missing = _base_prereqs_missing()
    if base_missing:
        return CheckResult(
            3, TITLE, CheckKind.AUTO, None,
            "не выполнялось: " + "; ".join(base_missing),
        )

    available, missing_langs = _available_languages()
    if not available:
        return CheckResult(
            3, TITLE, CheckKind.AUTO, None,
            "не выполнялось: нет ни одной эталонной пары "
            f"<lang>.wav/<lang>.json в {FIXTURES_DIR}",
        )

    from app.stt.runner import WhisperConfig, WhisperRunner

    runner = WhisperRunner(WhisperConfig(model_path=MODEL_PATH))
    passed_langs: list[str] = []
    failures: list[str] = []
    for lang in available:
        meta = json.loads((FIXTURES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        expected_text = meta["expected_text"]
        wer_threshold = meta["wer_threshold"]
        result = await runner.transcribe(
            FIXTURES_DIR / f"{lang}.wav", meta["audio_ms"], language=lang
        )
        recognized = result.payload.get("text", "")
        wer = _word_error_rate(expected_text, recognized)
        if wer > wer_threshold:
            failures.append(f"{lang}: WER {wer:.2f} > порог {wer_threshold}")
        else:
            passed_langs.append(f"{lang}: WER {wer:.2f}")

    if failures:
        detail = f"провалено: {'; '.join(failures)}"
        if passed_langs:
            detail += f"; пройдено: {'; '.join(passed_langs)}"
        if missing_langs:
            detail += f"; ещё нет фикстур: {', '.join(missing_langs)}"
        return CheckResult(3, TITLE, CheckKind.AUTO, False, detail, evidence_path=env.tmp_dir)

    if missing_langs:
        return CheckResult(
            3, TITLE, CheckKind.AUTO, None,
            f"частично: пройдено {', '.join(passed_langs)}; "
            f"не хватает фикстур для {', '.join(missing_langs)}",
        )

    return CheckResult(
        3, TITLE, CheckKind.AUTO, True,
        f"все {len(LANGUAGES)} языков в пределах порога WER: {'; '.join(passed_langs)}",
    )


def _word_error_rate(expected: str, actual: str) -> float:
    """Расстояние Левенштейна по словам, нормированное длиной эталона."""
    e, a = expected.lower().split(), actual.lower().split()
    dp = [[0] * (len(a) + 1) for _ in range(len(e) + 1)]
    for i in range(len(e) + 1):
        dp[i][0] = i
    for j in range(len(a) + 1):
        dp[0][j] = j
    for i in range(1, len(e) + 1):
        for j in range(1, len(a) + 1):
            cost = 0 if e[i - 1] == a[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    return dp[len(e)][len(a)] / max(1, len(e))


CHECK = CheckDef(number=3, title=TITLE, kind=CheckKind.AUTO, run=_run)
