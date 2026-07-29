"""Критерий 3 (§21): распознавание EN/ES/RU/UA/PL через ggml-base.bin.

Заблокировано в этом прогоне (не хватает окружения, не логики): нужны
(а) собранный whisper-cli в PATH, (б) ggml-base.bin, (в) 5 эталонных WAV
по языку с ожидаемым текстом — их предлагается хранить в
tests/fixtures/acceptance_stt/<lang>.wav + .json (H2, ловушка: ссылка на
внешний источник делает приёмку невоспроизводимой, значит только в репо).
Ни того, ни другого, ни третьего сейчас нет. Проверка написана так, чтобы
заработать сама, как только фикстуры появятся — руки трогать не нужно.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tests.acceptance.harness import CheckDef, CheckEnv, CheckKind, CheckResult

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "acceptance_stt"
LANGUAGES = ("en", "es", "ru", "uk", "pl")
MODEL_PATH = Path("models/ggml-base.bin")


def _missing_prereqs() -> list[str]:
    missing = []
    if shutil.which("whisper-cli") is None:
        missing.append("whisper-cli не найден в PATH")
    if not MODEL_PATH.exists():
        missing.append(f"модель не найдена: {MODEL_PATH}")
    for lang in LANGUAGES:
        wav = FIXTURES_DIR / f"{lang}.wav"
        meta = FIXTURES_DIR / f"{lang}.json"
        if not wav.exists() or not meta.exists():
            missing.append(f"нет эталонной пары {lang}.wav/{lang}.json в {FIXTURES_DIR}")
    return missing


async def _run(env: CheckEnv) -> CheckResult:
    missing = _missing_prereqs()
    if missing:
        return CheckResult(
            3,
            "распознавание EN/ES/RU/UA/PL (ggml-base.bin)",
            CheckKind.AUTO,
            None,
            "не выполнялось: " + "; ".join(missing),
        )

    from app.stt.runner import WhisperConfig, WhisperRunner

    runner = WhisperRunner(WhisperConfig(model_path=MODEL_PATH))
    failures = []
    for lang in LANGUAGES:
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

    if failures:
        return CheckResult(
            3,
            "распознавание EN/ES/RU/UA/PL (ggml-base.bin)",
            CheckKind.AUTO,
            False,
            "; ".join(failures),
            evidence_path=env.tmp_dir,
        )

    return CheckResult(
        3,
        "распознавание EN/ES/RU/UA/PL (ggml-base.bin)",
        CheckKind.AUTO,
        True,
        f"все {len(LANGUAGES)} языков в пределах порога WER",
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


CHECK = CheckDef(
    number=3, title="распознавание EN/ES/RU/UA/PL (ggml-base.bin)", kind=CheckKind.AUTO, run=_run
)
