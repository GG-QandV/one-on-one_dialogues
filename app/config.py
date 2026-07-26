"""app/config.py — конфигурация: загрузка, валидация, запись из панели."""

from __future__ import annotations

import os
import tomllib
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List

from app.errors import SpeechLocalError


# ------------------------------------------------------------------ sections


@dataclass(frozen=True, slots=True)
class PrivacySection:
    default_profile: str  # "open" | "confidential"
    allow_switch_midsession: bool


@dataclass(frozen=True, slots=True)
class TranslationProviderSection:
    active: str  # "gemini" | "claude" | "custom"
    endpoint: str
    model: str


@dataclass(frozen=True, slots=True)
class RealtimeProviderSection:
    active: str  # "openai" | "gemini" | "none"
    model: str
    enabled_profiles: List[str]  # e.g. ["open"]


@dataclass(frozen=True, slots=True)
class DraftProviderSection:
    active: str  # "gemini"
    model: str
    max_words: int


@dataclass(frozen=True, slots=True)
class SttSection:
    model: str
    fallback_model: str
    mode: str  # "file_per_segment"
    json_output: bool
    language_autodetect: bool


@dataclass(frozen=True, slots=True)
class StreamSection:
    source_language: str
    target_language: str
    pipewire_node: str
    enabled: bool
    priority: str  # "primary" | "secondary"


@dataclass(frozen=True, slots=True)
class VadSection:
    silence_close_ms: int  # 800–1200
    segment_min_s: float
    segment_max_s: float
    partial_interval_ms: int


@dataclass(frozen=True, slots=True)
class LatencySection:
    target_ms: int
    degrade_above_ms: int


@dataclass(frozen=True, slots=True)
class ContextSection:
    window_short: int
    window_mid: int
    window_long: int
    short_chars: int
    long_chars: int


@dataclass(frozen=True, slots=True)
class DraftSection:
    auto_generate: bool
    library_max_tokens: int
    generate_language: str  # "ru"
    translate_mode: str  # "live_literal"


@dataclass(frozen=True, slots=True)
class RetentionSection:
    audio: str  # "after_stt" | "24h" | "7d" | "keep"


@dataclass(frozen=True, slots=True)
class MemorySection:
    high_mb: int
    max_mb: int


@dataclass(frozen=True, slots=True)
class DeliverySection:
    clipboard_hotkey: str


@dataclass(frozen=True, slots=True)
class UiSection:
    host: str
    port: int


# Containers for nested structure expected by tests
@dataclass(frozen=True, slots=True)
class Provider:
    translation: TranslationProviderSection
    realtime: RealtimeProviderSection
    draft: DraftProviderSection


@dataclass(frozen=True, slots=True)
class Translation:
    context: ContextSection


@dataclass(frozen=True, slots=True)
class Config:
    privacy: PrivacySection
    provider: Provider
    stt: SttSection
    streams: Dict[str, StreamSection]  # "microphone", "meeting"
    vad: VadSection
    latency: LatencySection
    translation: Translation
    draft: DraftSection
    retention: RetentionSection
    memory: MemorySection
    delivery: DeliverySection
    ui: UiSection
    source_path: Path


# ------------------------------------------------------------------ errors


class ConfigError(SpeechLocalError):
    """Base config error."""

    code = "CONFIG_ERROR"
    retryable = False


class ConfigValidationError(ConfigError):
    """Validation failed; list of messages in `msg`."""

    def __init__(self, messages: List[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


# ------------------------------------------------------------------ helpers


FLAT_DEFAULTS: Dict[str, Any] = {
    "privacy.default_profile": "open",
    "privacy.allow_switch_midsession": True,
    "provider.translation.active": "gemini",
    "provider.translation.endpoint": "",
    "provider.translation.model": "",
    "provider.realtime.active": "openai",
    "provider.realtime.model": "gpt-realtime-translate",
    "provider.realtime.enabled_profiles": ["open"],
    "provider.draft.active": "gemini",
    "provider.draft.model": "",
    "provider.draft.max_words": 120,
    "stt.model": "ggml-base.bin",
    "stt.fallback_model": "ggml-tiny.bin",
    "stt.mode": "file_per_segment",
    "stt.json_output": True,
    "stt.language_autodetect": True,
    "streams.microphone.source_language": "ru",
    "streams.microphone.target_language": "en",
    "streams.microphone.pipewire_node": "",
    "streams.microphone.enabled": True,
    "streams.microphone.priority": "primary",
    "streams.meeting.source_language": "en",
    "streams.meeting.target_language": "ru",
    "streams.meeting.pipewire_node": "",
    "streams.meeting.enabled": True,
    "streams.meeting.priority": "secondary",
    "vad.silence_close_ms": 1000,
    "vad.segment_min_s": 0.8,
    "vad.segment_max_s": 15,
    "vad.partial_interval_ms": 600,
    "latency.target_ms": 3000,
    "latency.degrade_above_ms": 3000,
    "translation.context.window_short": 4,
    "translation.context.window_mid": 3,
    "translation.context.window_long": 2,
    "translation.context.short_chars": 100,
    "translation.context.long_chars": 200,
    "draft.auto_generate": True,
    "draft.library_max_tokens": 30000,
    "draft.generate_language": "ru",
    "draft.translate_mode": "live_literal",
    "retention.audio": "after_stt",
    "memory.high_mb": 1750,
    "memory.max_mb": 1900,
    "delivery.clipboard_hotkey": "ctrl+alt+c",
    "ui.host": "127.0.0.1",
    "ui.port": 8790,
}


def _nested_from_flat(flat: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a flat dict with dot-separated keys to a nested dict."""
    result: Dict[str, Any] = {}
    for key, value in flat.items():
        parts = key.split(".")
        d = result
        for part in parts[:-1]:
            if part not in d:
                d[part] = {}
            d = d[part]
        d[parts[-1]] = value
    return result


def defaults() -> Dict[str, Any]:
    """Return default configuration as nested dict (for compatibility with tests)."""
    return _nested_from_flat(FLAT_DEFAULTS)


def _flatten_dict(d: Dict[str, Any], parent_key: str = "", sep: str = ".") -> Dict[str, Any]:
    """Flatten a nested dictionary with dot-separated keys."""
    items: List[tuple[str, Any]] = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(_flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def _validate_positive_int(value: int, name: str) -> List[str]:
    if value <= 0:
        return [f"{name} must be > 0"]
    return []


def _validate_in_range(value: int, low: int, high: int, name: str) -> List[str]:
    if not (low <= value <= high):
        return [f"{name} must be between {low} and {high}"]
    return []


def _validate_str_in(val: str, allowed: List[str], name: str) -> List[str]:
    if val not in allowed:
        return [f"{name} must be one of {allowed}"]
    return []


def _validate_stream(sections: Dict[str, Dict[str, Any]]) -> List[str]:
    msgs: List[str] = []
    for key, sect in sections.items():
        if not isinstance(sect, dict):
            msgs.append(f"streams.{key} must be a table")
            continue
        msgs.extend(_validate_str_in(sect.get("source_language", ""), ["ru", "en", "es", "pl"], f"streams.{key}.source_language"))
        msgs.extend(_validate_str_in(sect.get("target_language", ""), ["ru", "en", "es", "pl"], f"streams.{key}.target_language"))
        msgs.extend(_validate_in_range(len(sect.get("pipewire_node", "")), 0, 255, f"streams.{key}.pipewire_node length"))
        enabled = sect.get("enabled")
        if not isinstance(enabled, bool):
            msgs.append(f"streams.{key}.enabled must be boolean")
        prio = sect.get("priority")
        if prio not in ["primary", "secondary"]:
            msgs.append(f"streams.{key}.priority must be 'primary' or 'secondary'")
    return msgs


def _validate_privacy(priv: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    msgs.extend(_validate_str_in(priv.get("default_profile", ""), ["open", "confidential"], "privacy.default_profile"))
    allow = priv.get("allow_switch_midsession")
    if not isinstance(allow, bool):
        msgs.append("privacy.allow_switch_midsession must be boolean")
    return msgs


def _validate_translation_provider(prov: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    msgs.extend(_validate_str_in(prov.get("active", ""), ["gemini", "claude", "custom"], "provider.translation.active"))
    # endpoint and model can be empty strings; no validation
    return msgs


def _validate_realtime_provider(prov: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    msgs.extend(_validate_str_in(prov.get("active", ""), ["openai", "gemini", "none"], "provider.realtime.active"))
    model = prov.get("model", "")
    if prov.get("active", "none") != "none" and not model:
        msgs.append("provider.realtime.model must be set when active != 'none'")
    enabled = prov.get("enabled_profiles", [])
    if not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled):
        msgs.append("provider.realtime.enabled_profiles must be list of strings")
    else:
        for p in enabled:
            if p not in ["open"]:  # only "open" is allowed per spec
                msgs.append(f"provider.realtime.enabled_profiles contains invalid profile '{p}'")
    return msgs


def _validate_draft_provider(prov: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    msgs.extend(_validate_str_in(prov.get("active", ""), ["gemini"], "provider.draft.active"))
    # model can be empty
    max_words = prov.get("max_words", 0)
    if not isinstance(max_words, int) or max_words <= 0:
        msgs.append("provider.draft.max_words must be positive integer")
    return msgs


def _validate_stt(stt: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    msgs.extend(_validate_str_in(stt.get("mode", ""), ["file_per_segment"], "stt.mode"))
    json_out = stt.get("json_output")
    if not isinstance(json_out, bool):
        msgs.append("stt.json_output must be boolean")
    lang_detect = stt.get("language_autodetect")
    if not isinstance(lang_detect, bool):
        msgs.append("stt.language_autodetect must be boolean")
    return msgs


def _validate_vad(vad: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    msgs.extend(_validate_in_range(vad.get("silence_close_ms", 0), 800, 1200, "vad.silence_close_ms"))
    seg_min = vad.get("segment_min_s", 0.0)
    if not isinstance(seg_min, (int, float)) or seg_min <= 0:
        msgs.append("vad.segment_min_s must be positive")
    seg_max = vad.get("segment_max_s", 0.0)
    if not isinstance(seg_max, (int, float)) or seg_max <= 0:
        msgs.append("vad.segment_max_s must be positive")
    if seg_max < seg_min:
        msgs.append("vad.segment_max_s must be >= segment_min_s")
    msgs.extend(_validate_in_range(vad.get("partial_interval_ms", 0), 100, 5000, "vad.partial_interval_ms"))
    return msgs


def _validate_latency(lat: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    msgs.extend(_validate_positive_int(lat.get("target_ms", 0), "latency.target_ms"))
    msgs.extend(_validate_positive_int(lat.get("degrade_above_ms", 0), "latency.degrade_above_ms"))
    return msgs


def _validate_context(ctx: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    for name in ("window_short", "window_mid", "window_long"):
        val = ctx.get(name, 0)
        if not isinstance(val, int) or val <= 0:
            msgs.append(f"translation.context.{name} must be positive integer")
    for name in ("short_chars", "long_chars"):
        val = ctx.get(name, 0)
        if not isinstance(val, int) or val <= 0:
            msgs.append(f"translation.context.{name} must be positive integer")
    return msgs


def _validate_draft(draft: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    ag = draft.get("auto_generate")
    if not isinstance(ag, bool):
        msgs.append("draft.auto_generate must be boolean")
    lmt = draft.get("library_max_tokens", 0)
    if not isinstance(lmt, int) or lmt <= 0:
        msgs.append("draft.library_max_tokens must be positive integer")
    glang = draft.get("generate_language", "")
    if glang != "ru":
        msgs.append('draft.generate_language must be "ru"')
    tmode = draft.get("translate_mode", "")
    if tmode != "live_literal":
        msgs.append('draft.translate_mode must be "live_literal"')
    return msgs


def _validate_retention(ret: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    val = ret.get("audio", "")
    if val not in ["after_stt", "24h", "7d", "keep"]:
        msgs.append('retention.audio must be one of "after_stt", "24h", "7d", "keep"')
    return msgs


def _validate_memory(mem: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    high = mem.get("high_mb", 0)
    maximum = mem.get("max_mb", 0)
    if not isinstance(high, int) or high <= 0:
        msgs.append("memory.high_mb must be positive integer")
    if not isinstance(maximum, int) or maximum <= 0:
        msgs.append("memory.max_mb must be positive integer")
    if high > maximum:
        msgs.append("memory.high_mb must be <= memory.max_mb")
    return msgs


def _validate_delivery(deliv: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    hotkey = deliv.get("clipboard_hotkey", "")
    if not isinstance(hotkey, str) or not hotkey:
        msgs.append("delivery.clipboard_hotkey must be non-empty string")
    return msgs


def _validate_ui(ui: Dict[str, Any]) -> List[str]:
    msgs: List[str] = []
    host = ui.get("host", "")
    if not isinstance(host, str) or not host:
        msgs.append("ui.host must be non-empty string")
    port = ui.get("port", 0)
    if not isinstance(port, int) or not (1 <= port <= 65535):
        msgs.append("ui.port must be in 1..65535")
    return msgs


def _get_section(flat: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Get a nested dictionary from a flat dict by a sequence of keys.
    Returns empty dict if any key is missing.
    """
    result: Dict[str, Any] = {}
    prefix = ".".join(keys)
    for k, v in flat.items():
        if k.startswith(prefix):
            remainder = k[len(prefix):]
            if remainder.startswith("."):
                remainder = remainder[1:]
            # Now split the remainder by the first dot to get the key and the rest
            if "." in remainder:
                key, rest = remainder.split(".", 1)
                if key not in result:
                    result[key] = {}
                result[key][rest] = v
            else:
                # This is a top-level key under the prefix (should not happen for our usage)
                result[remainder] = v
    return result


def validate(flat: Dict[str, Any]) -> List[str]:
    """Return list of error messages; empty list means valid."""
    msgs: List[str] = []

    msgs.extend(_validate_privacy(_get_section(flat, "privacy")))
    msgs.extend(_validate_translation_provider(_get_section(flat, "provider", "translation")))
    msgs.extend(_validate_realtime_provider(_get_section(flat, "provider", "realtime")))
    msgs.extend(_validate_draft_provider(_get_section(flat, "provider", "draft")))
    msgs.extend(_validate_stt(_get_section(flat, "stt")))
    msgs.extend(_validate_vad(_get_section(flat, "vad")))
    msgs.extend(_validate_latency(_get_section(flat, "latency")))
    msgs.extend(_validate_context(_get_section(flat, "translation", "context")))
    msgs.extend(_validate_draft(_get_section(flat, "draft")))
    msgs.extend(_validate_retention(_get_section(flat, "retention")))
    msgs.extend(_validate_memory(_get_section(flat, "memory")))
    msgs.extend(_validate_delivery(_get_section(flat, "delivery")))
    msgs.extend(_validate_ui(_get_section(flat, "ui")))
    streams_raw = _get_section(flat, "streams")
    if not isinstance(streams_raw, dict):
        msgs.append("streams must be a table")
    else:
        msgs.extend(_validate_stream(streams_raw))

    return msgs


def _check_warnings(flat: Dict[str, Any]) -> None:
    """Check for conditions that should emit warnings."""
    host_val = flat.get("ui.host")
    if isinstance(host_val, str) and host_val not in ("127.0.0.1", "localhost"):
        warnings.warn(f"non-loopback address: {host_val}", UserWarning)
    secret_keys = ["_key", "_secret", "_token", "_password", "api_key", "api_secret"]
    for k, v in flat.items():
        if any(s in k.lower() for s in secret_keys):
            if isinstance(v, str) and v.strip() != "":
                warnings.warn(f"secret key found: {k}", UserWarning)


def _dict_to_config(data: Dict[str, Any], source_path: Path = Path(".")) -> Config:
    """Convert flat dict (with dot-separated keys) to Config dataclass."""
    # Helper to get a section as a dict
    def get_section(*keys: str) -> Dict[str, Any]:
        prefix = ".".join(keys)
        result: Dict[str, Any] = {}
        for k, v in data.items():
            if k.startswith(prefix):
                remainder = k[len(prefix):]
                if remainder.startswith("."):
                    remainder = remainder[1:]
                # Now split the remainder by the first dot to get the key and the rest
                if "." in remainder:
                    key, rest = remainder.split(".", 1)
                    if key not in result:
                        result[key] = {}
                    result[key][rest] = v
                else:
                    # This is a top-level key under the prefix (should not happen for our usage)
                    result[remainder] = v
        return result

    privacy = PrivacySection(
        default_profile=get_section("privacy").get("default_profile", "open"),
        allow_switch_midsession=get_section("privacy").get("allow_switch_midsession", True),
    )
    provider_translation = TranslationProviderSection(
        active=get_section("provider", "translation").get("active", "gemini"),
        endpoint=get_section("provider", "translation").get("endpoint", ""),
        model=get_section("provider", "translation").get("model", ""),
    )
    provider_realtime = RealtimeProviderSection(
        active=get_section("provider", "realtime").get("active", "openai"),
        model=get_section("provider", "realtime").get("model", "gpt-realtime-translate"),
        enabled_profiles=get_section("provider", "realtime").get("enabled_profiles", ["open"]),
    )
    provider_draft = DraftProviderSection(
        active=get_section("provider", "draft").get("active", "gemini"),
        model=get_section("provider", "draft").get("model", ""),
        max_words=get_section("provider", "draft").get("max_words", 120),
    )
    provider = Provider(
        translation=provider_translation,
        realtime=provider_realtime,
        draft=provider_draft,
    )
    stt = SttSection(
        model=get_section("stt").get("model", "ggml-base.bin"),
        fallback_model=get_section("stt").get("fallback_model", "ggml-tiny.bin"),
        mode=get_section("stt").get("mode", "file_per_segment"),
        json_output=get_section("stt").get("json_output", True),
        language_autodetect=get_section("stt").get("language_autodetect", True),
    )
    # Build streams dict
    streams_raw = get_section("streams")
    streams: Dict[str, StreamSection] = {}
    for stream_name, attrs in streams_raw.items():
        if isinstance(attrs, dict):
            streams[stream_name] = StreamSection(
                source_language=attrs.get("source_language", "ru"),
                target_language=attrs.get("target_language", "en"),
                pipewire_node=attrs.get("pipewire_node", ""),
                enabled=attrs.get("enabled", True),
                priority=attrs.get("priority", "primary"),
            )
    vad = VadSection(
        silence_close_ms=get_section("vad").get("silence_close_ms", 1000),
        segment_min_s=get_section("vad").get("segment_min_s", 0.8),
        segment_max_s=get_section("vad").get("segment_max_s", 15),
        partial_interval_ms=get_section("vad").get("partial_interval_ms", 600),
    )
    latency = LatencySection(
        target_ms=get_section("latency").get("target_ms", 3000),
        degrade_above_ms=get_section("latency").get("degrade_above_ms", 3000),
    )
    translation_context = ContextSection(
        window_short=get_section("translation", "context").get("window_short", 4),
        window_mid=get_section("translation", "context").get("window_mid", 3),
        window_long=get_section("translation", "context").get("window_long", 2),
        short_chars=get_section("translation", "context").get("short_chars", 100),
        long_chars=get_section("translation", "context").get("long_chars", 200),
    )
    translation = Translation(
        context=translation_context,
    )
    draft = DraftSection(
        auto_generate=get_section("draft").get("auto_generate", True),
        library_max_tokens=get_section("draft").get("library_max_tokens", 30000),
        generate_language=get_section("draft").get("generate_language", "ru"),
        translate_mode=get_section("draft").get("translate_mode", "live_literal"),
    )
    retention = RetentionSection(
        audio=get_section("retention").get("audio", "after_stt"),
    )
    memory = MemorySection(
        high_mb=get_section("memory").get("high_mb", 1750),
        max_mb=get_section("memory").get("max_mb", 1900),
    )
    delivery = DeliverySection(
        clipboard_hotkey=get_section("delivery").get("clipboard_hotkey", "ctrl+alt+c"),
    )
    ui = UiSection(
        host=get_section("ui").get("host", "127.0.0.1"),
        port=get_section("ui").get("port", 8790),
    )

    return Config(
        privacy=privacy,
        provider=provider,
        stt=stt,
        streams=streams,
        vad=vad,
        latency=latency,
        translation=translation,
        draft=draft,
        retention=retention,
        memory=memory,
        delivery=delivery,
        ui=ui,
        source_path=source_path,
    )


def _to_toml_value(val: Any) -> str:
    """Convert a Python value to TOML string."""
    if isinstance(val, str):
        # escape quotes and backslashes
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        if not val:
            return "[]"
        items = ", ".join(_to_toml_value(v) for v in val)
        return f"[{items}]"
    # fallback
    return str(val)


def _nested_dict_to_toml(nested_dict: Dict[str, Any], parent_key: str = "") -> List[str]:
    """Convert a nested dict to TOML lines."""
    lines: List[str] = []
    for key, value in nested_dict.items():
        full_key = f"{parent_key}.{key}" if parent_key else key
        if isinstance(value, dict):
            lines.append(f"[{full_key}]")
            lines.extend(_nested_dict_to_toml(value, full_key))
        else:
            val_str = _to_toml_value(value)
            lines.append(f"{key} = {val_str}")
    return lines


def to_toml(config: Config) -> str:
    """Serialize Config to TOML string."""
    lines: List[str] = []

    def _add_section(title: str, mapping: Dict[str, Any]) -> None:
        lines.append(f"[{title}]")
        for key, value in sorted(mapping.items()):
            if isinstance(value, bool):
                val_str = "true" if value else "false"
            elif isinstance(value, (int, float)):
                val_str = str(value)
            elif isinstance(value, str):
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                val_str = f'"{escaped}"'
            elif isinstance(value, list):
                if not value:
                    val_str = "[]"
                else:
                    items = ", ".join(_to_toml_value(v) for v in value)
                    val_str = f"[{items}]"
            else:
                val_str = str(value)
            lines.append(f"{key} = {val_str}")
        lines.append("")  # blank line between sections

    # Build each section's mapping
    # [privacy]
    _add_section("privacy", asdict(config.privacy))

    # [provider.translation]
    _add_section("provider.translation", asdict(config.provider.translation))
    # [provider.realtime]
    _add_section("provider.realtime", asdict(config.provider.realtime))
    # [provider.draft]
    _add_section("provider.draft", asdict(config.provider.draft))

    # [stt]
    _add_section("stt", asdict(config.stt))

    # [streams.microphone] and [streams.meeting]
    for stream_name, stream in config.streams.items():
        _add_section(f"streams.{stream_name}", asdict(stream))

    # [vad]
    _add_section("vad", asdict(config.vad))

    # [latency]
    _add_section("latency", asdict(config.latency))

    # [translation.context]
    _add_section("translation.context", asdict(config.translation.context))

    # [draft]
    _add_section("draft", asdict(config.draft))

    # [retention]
    _add_section("retention", asdict(config.retention))

    # [memory]
    _add_section("memory", asdict(config.memory))

    # [delivery]
    _add_section("delivery", asdict(config.delivery))

    # [ui]
    _add_section("ui", asdict(config.ui))

    # Join and remove trailing blank line
    return "\n".join(lines).strip()


def _load_raw(path: Path) -> Dict[str, Any]:
    """Load raw TOML data as a nested dict."""
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("rb") as f:
        return tomllib.load(f)


def load(path: Path) -> Config:
    """Load config from TOML file; raise ConfigError if missing or invalid."""
    raw = _load_raw(path)  # nested dict
    flat_raw = _flatten_dict(raw)
    _check_warnings(flat_raw)  # warn about the input
    merged = {**FLAT_DEFAULTS, **flat_raw}  # latter overrides former
    errors = validate(merged)
    if errors:
        raise ConfigValidationError(errors)
    config = _dict_to_config(merged, source_path=path.parent)
    return config


def load_or_default(path: Path) -> Config:
    """Load config; if missing, create it from defaults and return that config."""
    if not path.is_file():
        default_toml = to_toml(_dict_to_config(FLAT_DEFAULTS, source_path=path.parent))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_toml, encoding="utf-8")
        return _dict_to_config(FLAT_DEFAULTS, source_path=path.parent)
    return load(path)


def update(path: Path, changes: Dict[str, Any]) -> Config:
    """Apply changes to config file and return new Config."""
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    raw = _load_raw(path)
    flat_raw = _flatten_dict(raw)
    _check_warnings(flat_raw)  # warn about the original file (for consistency)
    merged = {**FLAT_DEFAULTS, **flat_raw}
    # Apply changes (which may be nested or flat; we flatten them)
    flat_changes = _flatten_dict(changes)
    merged = {**merged, **flat_changes}
    errors = validate(merged)
    if errors:
        raise ConfigValidationError(errors)
    # Convert merged flat dict back to nested dict (preserving all keys)
    updated_nested = _nested_from_flat(merged)
    toml_lines = _nested_dict_to_toml(updated_nested)
    new_toml = "\n".join(toml_lines).strip()
    _atomic_write(path, new_toml)
    return load(path)


def _atomic_write(path: Path, text: str) -> None:
    """Write text to file atomically: temp file, fsync, replace."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    with tmp.open("rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, path)