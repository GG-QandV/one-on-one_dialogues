"""Tests for config.py (B1)."""

import tempfile
import warnings
from pathlib import Path

import pytest

from app.config import Config, ConfigError, ConfigValidationError, load, load_or_default, defaults, to_toml, update
from app.errors import SpeechLocalError


def test_defaults_match_spec():
    """Defaults should match spec §19."""
    default_dict = defaults()
    # We can check a few key values known from the spec.
    assert default_dict["privacy"]["default_profile"] == "open"
    assert default_dict["privacy"]["allow_switch_midsession"] is True
    assert default_dict["provider"]["translation"]["active"] == "gemini"
    assert default_dict["provider"]["realtime"]["active"] == "openai"
    assert default_dict["provider"]["realtime"]["model"] == "gpt-realtime-translate"
    assert default_dict["provider"]["realtime"]["enabled_profiles"] == ["open"]
    assert default_dict["provider"]["draft"]["active"] == "gemini"
    assert default_dict["provider"]["draft"]["max_words"] == 120
    assert default_dict["stt"]["model"] == "ggml-base.bin"
    assert default_dict["stt"]["fallback_model"] == "ggml-tiny.bin"
    assert default_dict["stt"]["mode"] == "file_per_segment"
    assert default_dict["stt"]["json_output"] is True
    assert default_dict["stt"]["language_autodetect"] is True
    assert default_dict["streams"]["microphone"]["source_language"] == "ru"
    assert default_dict["streams"]["microphone"]["target_language"] == "en"
    assert default_dict["streams"]["microphone"]["enabled"] is True
    assert default_dict["streams"]["microphone"]["priority"] == "primary"
    assert default_dict["streams"]["meeting"]["source_language"] == "en"
    assert default_dict["streams"]["meeting"]["target_language"] == "ru"
    assert default_dict["streams"]["meeting"]["enabled"] is True
    assert default_dict["streams"]["meeting"]["priority"] == "secondary"
    assert default_dict["vad"]["silence_close_ms"] == 1000
    assert default_dict["vad"]["segment_min_s"] == 0.8
    assert default_dict["vad"]["segment_max_s"] == 15
    assert default_dict["vad"]["partial_interval_ms"] == 600
    assert default_dict["latency"]["target_ms"] == 3000
    assert default_dict["latency"]["degrade_above_ms"] == 3000
    assert default_dict["translation"]["context"]["window_short"] == 4
    assert default_dict["translation"]["context"]["window_mid"] == 3
    assert default_dict["translation"]["context"]["window_long"] == 2
    assert default_dict["translation"]["context"]["short_chars"] == 100
    assert default_dict["translation"]["context"]["long_chars"] == 200
    assert default_dict["draft"]["auto_generate"] is True
    assert default_dict["draft"]["library_max_tokens"] == 30000
    assert default_dict["draft"]["generate_language"] == "ru"
    assert default_dict["draft"]["translate_mode"] == "live_literal"
    assert default_dict["retention"]["audio"] == "after_stt"
    assert default_dict["memory"]["high_mb"] == 1750
    assert default_dict["memory"]["max_mb"] == 1900
    assert default_dict["delivery"]["clipboard_hotkey"] == "ctrl+alt+c"
    assert default_dict["ui"]["host"] == "127.0.0.1"
    assert default_dict["ui"]["port"] == 8790


def test_load_or_default_creates_file(tmp_path):
    """If config file missing, load_or_default creates it from defaults."""
    config_path = tmp_path / "config.toml"
    assert not config_path.exists()
    config = load_or_default(config_path)
    assert config_path.exists()
    # Check that the file content matches defaults when loaded back
    config2 = load(config_path)
    assert config == config2
    # Check a few values
    assert config.privacy.default_profile == "open"
    assert config.ui.host == "127.0.0.1"
    assert config.ui.port == 8790


def test_load_raises_on_missing_file(tmp_path):
    """load should raise ConfigError if file does not exist."""
    missing = tmp_path / "missing.toml"
    with pytest.raises(ConfigError, match="Config file not found"):
        load(missing)


def test_missing_section_uses_default(tmp_path):
    """If a section is missing, default values are used, no error."""
    toml_content = """
[privacy]
default_profile = "confidential"
# allow_switch_midsession missing -> should default to True
"""
    config_path = tmp_path / "config.toml"
    config_path.write_text(toml_content, encoding="utf-8")
    config = load(config_path)
    assert config.privacy.default_profile == "confidential"
    assert config.privacy.allow_switch_midsession is True  # default
    # other sections should be defaults
    assert config.provider.translation.active == "gemini"


def test_unknown_key_preserved_in_update(tmp_path):
    """Unknown sections/keys should be preserved in update, not appear in Config."""
    # Start with a file containing extra sections
    toml_content = """
[privacy]
default_profile = "open"
allow_switch_midsession = true

[extra_section]
extra_key = 42

[provider.translation]
active = "gemini"
"""
    config_path = tmp_path / "config.toml"
    config_path.write_text(toml_content, encoding="utf-8")
    config = load(config_path)
    # The extra section should not be in the config object
    assert not hasattr(config, "extra_section")
    # Update with a change
    changes = {"privacy": {"default_profile": "confidential"}}
    updated = update(config_path, changes)
    assert updated.privacy.default_profile == "confidential"
    # Reload and ensure extra section still there
    reloaded = load(config_path)
    # The file should still contain extra_section
    raw = config_path.read_text(encoding="utf-8")
    assert "[extra_section]" in raw
    assert "extra_key = 42" in raw
    # The config object should not have extra_section
    assert not hasattr(reloaded, "extra_section")


def test_invalid_silence_close_ms_validation_error():
    """Invalid vad.silence_close_ms should produce validation error."""
    toml_content = """
[privacy]
default_profile = "open"
allow_switch_midsession = true

[provider.translation]
active = "gemini"

[provider.realtime]
active = "openai"
model = "gpt-realtime-translate"
enabled_profiles = ["open"]

[provider.draft]
active = "gemini"
max_words = 120

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = true

[streams.microphone]
source_language = "ru"
target_language = "en"
pipewire_node = ""
enabled = true
priority = "primary"

[streams.meeting]
source_language = "en"
target_language = "ru"
pipewire_node = ""
enabled = true
priority = "secondary"

[vad]
silence_close_ms = 500  # too low
segment_min_s = 0.8
segment_max_s = 15
partial_interval_ms = 600

[latency]
target_ms = 3000
degrade_above_ms = 3000

[translation.context]
window_short = 4
window_mid = 3
window_long = 2
short_chars = 100
long_chars = 200

[draft]
auto_generate = true
library_max_tokens = 30000
generate_language = "ru"
translate_mode = "live_literal"

[retention]
audio = "after_stt"

[memory]
high_mb = 1750
max_mb = 1900

[delivery]
clipboard_hotkey = "ctrl+alt+c"

[ui]
host = "127.0.0.1"
port = 8790
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        path = Path(f.name)
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            load(path)
        assert "vad.silence_close_ms must be between 800 and 1200" in str(excinfo.value)
    finally:
        path.unlink()


def test_invalid_retention_audio_error():
    """Invalid retention.audio should produce validation error."""
    toml_content = """
[privacy]
default_profile = "open"
allow_switch_midsession = true

[provider.translation]
active = "gemini"

[provider.realtime]
active = "openai"
model = "gpt-realtime-translate"
enabled_profiles = ["open"]

[provider.draft]
active = "gemini"
max_words = 120

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = true

[streams.microphone]
source_language = "ru"
target_language = "en"
pipewire_node = ""
enabled = true
priority = "primary"

[streams.meeting]
source_language = "en"
target_language = "ru"
pipewire_node = ""
enabled = true
priority = "secondary"

[vad]
silence_close_ms = 1000
segment_min_s = 0.8
segment_max_s = 15
partial_interval_ms = 600

[latency]
target_ms = 3000
degrade_above_ms = 3000

[translation.context]
window_short = 4
window_mid = 3
window_long = 2
short_chars = 100
long_chars = 200

[draft]
auto_generate = true
library_max_tokens = 30000
generate_language = "ru"
translate_mode = "live_literal"

[retention]
audio = "forever"  # invalid

[memory]
high_mb = 1750
max_mb = 1900

[delivery]
clipboard_hotkey = "ctrl+alt+c"

[ui]
host = "127.0.0.1"
port = 8790
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        path = Path(f.name)
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            load(path)
        assert 'retention.audio must be one of "after_stt", "24h", "7d", "keep"' in str(excinfo.value)
    finally:
        path.unlink()


def test_memory_high_mb_exceeds_max_mb_error():
    """memory.high_mb > memory.max_mb should produce validation error."""
    toml_content = """
[privacy]
default_profile = "open"
allow_switch_midsession = true

[provider.translation]
active = "gemini"

[provider.realtime]
active = "openai"
model = "gpt-realtime-translate"
enabled_profiles = ["open"]

[provider.draft]
active = "gemini"
max_words = 120

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = true

[streams.microphone]
source_language = "ru"
target_language = "en"
pipewire_node = ""
enabled = true
priority = "primary"

[streams.meeting]
source_language = "en"
target_language = "ru"
pipewire_node = ""
enabled = true
priority = "secondary"

[vad]
silence_close_ms = 1000
segment_min_s = 0.8
segment_max_s = 15
partial_interval_ms = 600

[latency]
target_ms = 3000
degrade_above_ms = 3000

[translation.context]
window_short = 4
window_mid = 3
window_long = 2
short_chars = 100
long_chars = 200

[draft]
auto_generate = true
library_max_tokens = 30000
generate_language = "ru"
translate_mode = "live_literal"

[retention]
audio = "after_stt"

[memory]
high_mb = 2000  # greater than max_mb
max_mb = 1900

[delivery]
clipboard_hotkey = "ctrl+alt+c"

[ui]
host = "127.0.0.1"
port = 8790
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        path = Path(f.name)
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            load(path)
        assert "memory.high_mb must be <= memory.max_mb" in str(excinfo.value)
    finally:
        path.unlink()


def test_draft_translate_mode_live_safe_error():
    """draft.translate_mode = \"live_safe\" should produce validation error."""
    toml_content = """
[privacy]
default_profile = "open"
allow_switch_midsession = true

[provider.translation]
active = "gemini"

[provider.realtime]
active = "openai"
model = "gpt-realtime-translate"
enabled_profiles = ["open"]

[provider.draft]
active = "gemini"
max_words = 120

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = true

[streams.microphone]
source_language = "ru"
target_language = "en"
pipewire_node = ""
enabled = true
priority = "primary"

[streams.meeting]
source_language = "en"
target_language = "ru"
pipewire_node = ""
enabled = true
priority = "secondary"

[vad]
silence_close_ms = 1000
segment_min_s = 0.8
segment_max_s = 15
partial_interval_ms = 600

[latency]
target_ms = 3000
degrade_above_ms = 3000

[translation.context]
window_short = 4
window_mid = 3
window_long = 2
short_chars = 100
long_chars = 200

[draft]
auto_generate = true
library_max_tokens = 30000
generate_language = "ru"
translate_mode = "live_safe"  # invalid

[retention]
audio = "after_stt"

[memory]
high_mb = 1750
max_mb = 1900

[delivery]
clipboard_hotkey = "ctrl+alt+c"

[ui]
host = "127.0.0.1"
port = 8790
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        path = Path(f.name)
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            load(path)
        assert 'draft.translate_mode must be "live_literal"' in str(excinfo.value)
    finally:
        path.unlink()


def test_realtime_enabled_profiles_confidential_error():
    """provider.realtime.enabled_profiles = [\"confidential\"] should produce validation error."""
    toml_content = """
[privacy]
default_profile = "open"
allow_switch_midsession = true

[provider.translation]
active = "gemini"

[provider.realtime]
active = "openai"
model = "gpt-realtime-translate"
enabled_profiles = ["confidential"]  # invalid

[provider.draft]
active = "gemini"
max_words = 120

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = true

[streams.microphone]
source_language = "ru"
target_language = "en"
pipewire_node = ""
enabled = true
priority = "primary"

[streams.meeting]
source_language = "en"
target_language = "ru"
pipewire_node = ""
enabled = true
priority = "secondary"

[vad]
silence_close_ms = 1000
segment_min_s = 0.8
segment_max_s = 15
partial_interval_ms = 600

[latency]
target_ms = 3000
degrade_above_ms = 3000

[translation.context]
window_short = 4
window_mid = 3
window_long = 2
short_chars = 100
long_chars = 200

[draft]
auto_generate = true
library_max_tokens = 30000
generate_language = "ru"
translate_mode = "live_literal"

[retention]
audio = "after_stt"

[memory]
high_mb = 1750
max_mb = 1900

[delivery]
clipboard_hotkey = "ctrl+alt+c"

[ui]
host = "127.0.0.1"
port = 8790
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        path = Path(f.name)
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            load(path)
        assert "provider.realtime.enabled_profiles contains invalid profile 'confidential'" in str(excinfo.value)
    finally:
        path.unlink()


def test_ui_host_loopback_warning():
    """ui.host = \"0.0.0.0\" should produce a warning but load succeeds."""
    toml_content = """
[privacy]
default_profile = "open"
allow_switch_midsession = true

[provider.translation]
active = "gemini"

[provider.realtime]
active = "openai"
model = "gpt-realtime-translate"
enabled_profiles = ["open"]

[provider.draft]
active = "gemini"
max_words = 120

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = true

[streams.microphone]
source_language = "ru"
target_language = "en"
pipewire_node = ""
enabled = true
priority = "primary"

[streams.meeting]
source_language = "en"
target_language = "ru"
pipewire_node = ""
enabled = true
priority = "secondary"

[vad]
silence_close_ms = 1000
segment_min_s = 0.8
segment_max_s = 15
partial_interval_ms = 600

[latency]
target_ms = 3000
degrade_above_ms = 3000

[translation.context]
window_short = 4
window_mid = 3
window_long = 2
short_chars = 100
long_chars = 200

[draft]
auto_generate = true
library_max_tokens = 30000
generate_language = "ru"
translate_mode = "live_literal"

[retention]
audio = "after_stt"

[memory]
high_mb = 1750
max_mb = 1900

[delivery]
clipboard_hotkey = "ctrl+alt+c"

[ui]
host = "0.0.0.0"  # loopback? actually 0.0.0.0 is not loopback, but spec says non-loopback is warning; we'll treat as warning
port = 8790
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        path = Path(f.name)
    try:
        with pytest.warns(UserWarning, match="non-loopback address"):
            config = load(path)
        assert config.ui.host == "0.0.0.0"
        assert config.ui.port == 8790
    finally:
        path.unlink()


def test_secret_key_not_in_config(tmp_path):
    """If config contains a secret-like key, it should not be present in Config object and a warning should be logged."""
    toml_content = """
[privacy]
default_profile = "open"
allow_switch_midsession = true

[provider.translation]
active = "gemini"
endpoint = ""
model = ""
# secret key
api_key = "***"

[provider.realtime]
active = "openai"
model = "gpt-realtime-translate"
enabled_profiles = ["open"]

[provider.draft]
active = "gemini"
max_words = 120

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = true

[streams.microphone]
source_language = "ru"
target_language = "en"
pipewire_node = ""
enabled = true
priority = "primary"

[streams.meeting]
source_language = "en"
target_language = "ru"
pipewire_node = ""
enabled = true
priority = "secondary"

[vad]
silence_close_ms = 1000
segment_min_s = 0.8
segment_max_s = 15
partial_interval_ms = 600

[latency]
target_ms = 3000
degrade_above_ms = 3000

[translation.context]
window_short = 4
window_mid = 3
window_long = 2
short_chars = 100
long_chars = 200

[draft]
auto_generate = true
library_max_tokens = 30000
generate_language = "ru"
translate_mode = "live_literal"

[retention]
audio = "after_stt"

[memory]
high_mb = 1750
max_mb = 1900

[delivery]
clipboard_hotkey = "ctrl+alt+c"

[ui]
host = "127.0.0.1"
port = 8790
"""
    config_path = tmp_path / "config.toml"
    config_path.write_text(toml_content, encoding="utf-8")
    # We expect a warning when loading because of the api_key
    with pytest.warns(UserWarning, match="secret.*api_key"):
        config = load(config_path)
    # The secret key should not be accessible as an attribute
    assert not hasattr(config, "api_key")
    # Also, the raw dict should not have it (since we flattened and ignored extra keys)
    # But we don't expose the raw dict, so we just check that accessing it as an attribute fails.


def test_three_errors_returned_by_validate():
    """Multiple errors in config should be returned by validate and included in ConfigError."""
    toml_content = """
[privacy]
default_profile = "invalid"  # error 1
allow_switch_midsession = "yes"  # error 2

[provider.translation]
active = "invalid_provider"  # error 3

[provider.realtime]
active = "openai"
model = "gpt-realtime-translate"
enabled_profiles = ["open"]

[provider.draft]
active = "gemini"
max_words = 120

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = true

[streams.microphone]
source_language = "ru"
target_language = "en"
pipewire_node = ""
enabled = true
priority = "primary"

[streams.meeting]
source_language = "en"
target_language = "ru"
pipewire_node = ""
enabled = true
priority = "secondary"

[vad]
silence_close_ms = 1000
segment_min_s = 0.8
segment_max_s = 15
partial_interval_ms = 600

[latency]
target_ms = 3000
degrade_above_ms = 3000

[translation.context]
window_short = 4
window_mid = 3
window_long = 2
short_chars = 100
long_chars = 200

[draft]
auto_generate = true
library_max_tokens = 30000
generate_language = "ru"
translate_mode = "live_literal"

[retention]
audio = "after_stt"

[memory]
high_mb = 1750
max_mb = 1900

[delivery]
clipboard_hotkey = "ctrl+alt+c"

[ui]
host = "127.0.0.1"
port = 8790
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        f.flush()
        path = Path(f.name)
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            load(path)
        assert "privacy.default_profile must be one of ['open', 'confidential']" in str(excinfo.value)
        assert "privacy.allow_switch_midsession must be boolean" in str(excinfo.value)
        assert "provider.translation.active must be one of ['gemini', 'claude', 'custom']" in str(excinfo.value)
    finally:
        path.unlink()


def test_update_atomic_on_failure(tmp_path):
    """If update fails mid-write (e.g., due to validation error), original file should remain unchanged."""
    # Create a valid config file
    toml_content = """
[privacy]
default_profile = "open"
allow_switch_midsession = true

[provider.translation]
active = "gemini"

[provider.realtime]
active = "openai"
model = "gpt-realtime-translate"
enabled_profiles = ["open"]

[provider.draft]
active = "gemini"
max_words = 120

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = true

[streams.microphone]
source_language = "ru"
target_language = "en"
pipewire_node = ""
enabled = true
priority = "primary"

[streams.meeting]
source_language = "en"
target_language = "ru"
pipewire_node = ""
enabled = true
priority = "secondary"

[vad]
silence_close_ms = 1000
segment_min_s = 0.8
segment_max_s = 15
partial_interval_ms = 600

[latency]
target_ms = 3000
degrade_above_ms = 3000

[translation.context]
window_short = 4
window_mid = 3
window_long = 2
short_chars = 100
long_chars = 200

[draft]
auto_generate = true
library_max_tokens = 30000
generate_language = "ru"
translate_mode = "live_literal"

[retention]
audio = "after_stt"

[memory]
high_mb = 1750
max_mb = 1900

[delivery]
clipboard_hotkey = "ctrl+alt+c"

[ui]
host = "127.0.0.1"
port = 8790
"""
    config_path = tmp_path / "config.toml"
    config_path.write_text(toml_content, encoding="utf-8")
    original_content = config_path.read_text(encoding="utf-8")
    # Attempt an update that will fail validation
    changes = {"vad": {"silence_close_ms": 500}}  # too low
    with pytest.raises(ConfigValidationError):
        update(config_path, changes)
    # Ensure the file is unchanged
    assert config_path.read_text(encoding="utf-8") == original_content


def test_roundtrip_toml(tmp_path):
    """to_toml(load(path)) should produce a file that loads to an equivalent config."""
    # Create a config with some non-default values
    toml_content = """
[privacy]
default_profile = "confidential"
allow_switch_midsession = false

[provider.translation]
active = "claude"
endpoint = "https://api.example.com"
model = "claude-3"

[provider.realtime]
active = "none"

[provider.draft]
active = "gemini"
max_words = 100

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
mode = "file_per_segment"
json_output = true
language_autodetect = false

[streams.microphone]
source_language = "en"
target_language = "es"
pipewire_node = "some_node"
enabled = false
priority = "secondary"

[streams.meeting]
source_language = "es"
target_language = "en"
pipewire_node = "another_node"
enabled = true
priority = "primary"

[vad]
silence_close_ms = 900
segment_min_s = 0.5
segment_max_s = 10
partial_interval_ms = 300

[latency]
target_ms = 2500
degrade_above_ms = 2500

[translation.context]
window_short = 2
window_mid = 1
window_long = 1
short_chars = 50
long_chars = 150

[draft]
auto_generate = false
library_max_tokens = 15000
generate_language = "ru"
translate_mode = "live_literal"

[retention]
audio = "24h"

[memory]
high_mb = 1000
max_mb = 1500

[delivery]
clipboard_hotkey = "ctrl+alt+v"

[ui]
host = "127.0.0.1"
port = 9999
"""
    config_path = tmp_path / "config.toml"
    config_path.write_text(toml_content, encoding="utf-8")
    config1 = load(config_path)
    # Generate TOML from the loaded config
    toml_out = to_toml(config1)
    # Load it back
    out_path = tmp_path / "out.toml"
    out_path.write_text(toml_out, encoding="utf-8")
    config2 = load(out_path)
    # Compare the two configs
    assert config1 == config2