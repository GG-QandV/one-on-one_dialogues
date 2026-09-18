"""tests/test_stt_dispatch.py — облако-first диспетчер (A) и роли local-фолбэка.

Семантика: local только (1) при закрытом профиле или (2) когда облако
недоступно. Параллельно облаку local не запускается.
"""

from __future__ import annotations

from pathlib import Path

from app.audio.segmenter import FinalSegment
from app.main import AppConfig, Application
from app.privacy import PrivacyController, PrivacyProfile
from app.stt.base import SttResult
from app.stt.chain import SttChainExhausted


class _Chain:
    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = 0

    async def transcribe(self, req, *, fence=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result

    async def close(self):
        return None


class _Stt:
    def __init__(self, *, accept=True):
        self.accept = accept
        self.submitted: list[FinalSegment] = []

    def submit(self, seg) -> bool:
        self.submitted.append(seg)
        return self.accept


class _Jobs:
    def __init__(self):
        self.enqueued: list[tuple] = []

    async def enqueue(self, *args, **kwargs) -> None:
        self.enqueued.append((args, kwargs))


def _seg() -> FinalSegment:
    return FinalSegment(
        id="s1", role="microphone", t_start_ms=0, t_end_ms=1000,
        audio_path=Path("/dev/null"), reason=None, mean_level_db=0.0,  # type: ignore[arg-type]
    )


def _app(profile: PrivacyProfile, *, chain, stt=None):
    app = Application(AppConfig())
    app.privacy = PrivacyController(profile)
    app.jobs = _Jobs()
    app.stt = stt or _Stt()
    app._stt_cloud = chain
    app._stt_dispatch_sem = None
    results: list[SttResult] = []

    async def on_result(seg, res):
        results.append(res)

    app._on_stt_result = on_result  # type: ignore[method-assign]
    return app, results


async def test_cloud_success_skips_local():
    result = SttResult(
        raw_text="ok", provider="custom_api", entry="custom_api:groq_0"
    )
    chain = _Chain(result=result)
    app, results = _app(PrivacyProfile.OPEN, chain=chain)
    ok = await app._dispatch_stt(_seg())

    assert ok is True
    assert chain.calls == 1
    assert app.stt.submitted == []  # local не тронут
    assert results and results[0].entry == "custom_api:groq_0"


async def test_cloud_exhausted_falls_back_to_local():
    chain = _Chain(exc=SttChainExhausted("все звенья недоступны"))
    app, results = _app(PrivacyProfile.OPEN, chain=chain)
    ok = await app._dispatch_stt(_seg())

    assert ok is True
    assert len(app.stt.submitted) == 1
    assert results == []


async def test_confidential_skips_cloud_entirely():
    chain = _Chain(result=SttResult(raw_text="не должно случиться"))
    app, results = _app(PrivacyProfile.CONFIDENTIAL, chain=chain)
    ok = await app._dispatch_stt(_seg())

    assert ok is True
    assert chain.calls == 0  # облако не трогаем вообще
    assert len(app.stt.submitted) == 1


async def test_no_cloud_chain_goes_local():
    app, _ = _app(PrivacyProfile.OPEN, chain=None)
    ok = await app._dispatch_stt(_seg())
    assert ok is True
    assert len(app.stt.submitted) == 1


async def test_local_overflow_enqueues_job_and_reports_false():
    chain = _Chain(exc=SttChainExhausted("down"))
    stt = _Stt(accept=False)
    app, _ = _app(PrivacyProfile.OPEN, chain=chain, stt=stt)
    ok = await app._dispatch_stt(_seg())

    assert ok is False
    assert len(stt.submitted) == 1
    assert len(app.jobs.enqueued) == 1
