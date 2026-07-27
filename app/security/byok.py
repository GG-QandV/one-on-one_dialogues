"""app/security/byok.py — G2 BYOK keystore."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from app.security.redactor import LogRedactor
from app.errors import InvariantViolation, ProviderAuthError


@dataclass(frozen=True, slots=True)
class KeyStoreConfig:
    ttl_s: float = 3600.0              # 60 minutes by spec §16
    warn_before_s: float = 300.0       # warn UI 5 minutes before expiry


class KeyStore:
    def __init__(
        self,
        config: KeyStoreConfig | None = None,
        *,
        redactor: LogRedactor | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config or KeyStoreConfig()
        self._redactor = redactor
        self._clock = clock
        # provider -> (key, put_time)
        self._store: dict[str, tuple[str, float]] = {}

    def put(self, provider: str, key: str) -> None:
        """Store a provider key in memory, update expiry, and register with redactor."""
        if not key or not key.strip():
            raise ProviderAuthError("empty key")
        # Remove old literal from redactor if present
        if provider in self._store and self._redactor is not None:
            old_key, _ = self._store[provider]
            self._redactor.remove_literal(old_key)
        # Store new
        self._store[provider] = (key, self._clock())
        # Add to redactor
        if self._redactor is not None:
            self._redactor.add_literal(key)

    def get(self, provider: str) -> str:
        """Retrieve a provider key, checking TTL.
        Raises ProviderAuthError if missing or expired.
        """
        if provider not in self._store:
            raise ProviderAuthError(f"unknown provider: {provider}")
        key, put_time = self._store[provider]
        now = self._clock()
        if now - put_time > self._config.ttl_s:
            # Expired: remove and raise
            del self._store[provider]
            if self._redactor is not None:
                self._redactor.remove_literal(key)
            raise ProviderAuthError(f"key expired for provider: {provider}")
        return key

    def revoke(self, provider: str | None = None) -> None:
        """Revoke key(s) and remove from redactor."""
        if provider is None:
            # Revoke all
            to_remove = list(self._store.keys())
        else:
            to_remove = [provider] if provider in self._store else []
        for prov in to_remove:
            key, _ = self._store.pop(prov, (None, None))
            if key is not None and self._redactor is not None:
                self._redactor.remove_literal(key)

    def has(self, provider: str) -> bool:
        """Check if a provider has a non-expired key."""
        if provider not in self._store:
            return False
        _, put_time = self._store[provider]
        return (self._clock() - put_time) <= self._config.ttl_s

    def masked(self, provider: str) -> str:
        """Return a masked version for UI: show first part up to first dash and last 4 chars.
        If key is too short, return only mask.
        Format: e.g., 'sk-…f12a' for 'sk-...' key.
        """
        if provider not in self._store:
            return ""
        key, _ = self._store[provider]
        if not key:
            return ""
        # Find first dash
        dash = key.find('-')
        if dash != -1:
            prefix = key[:dash+1]  # include the dash
        else:
            prefix = ''
        # Last 4 characters
        if len(key) >= 4:
            suffix = key[-4:]
        else:
            suffix = key
        # If the key is short, we might just show the whole key masked? Spec says:
        #   Для ключей короче 8 символов — только маска, без хвоста.
        # We'll interpret as: if the key length is less than 8, return only a mask of same length?
        # But the mask is supposed to be the same for all keys? Actually, the spec says:
        #   маска — единственная форма, в которой ключ покидает объект, кроме get().
        #   и пример: sk-…f12a
        # Let's produce: first part up to first dash (if any) and then ellipsis and last 4.
        # If the key is too short to have 4 chars tail, we adjust.
        if len(key) < 8:
            # For short keys, return a mask of same length? But spec says only mask, no tail.
            # We'll return a string of bullet characters? However, the example uses ellipsis.
            # We'll follow: if key is short, we show only the first character and then dots? Not clear.
            # Let's stick to the rule: for keys shorter than 8, we return only the mask (which is a fixed string of dots?).
            # But the masked function is used in UI to show something like sk-…f12a.
            # We'll implement as: if key length < 8, return first char + '…' + last min(4, len) chars? 
            # However, spec says only mask, no tail. So we return a string of length len(key) of a mask char? 
            # But the example uses ellipsis and tail. Let's re-read:
            #   «маasked» показывает не более 4 последних символов и префикс до первого дефиса, если он есть: `sk-…f12a`. Для ключей короче 8 символов — только маска, без хвоста.
            # So for short keys, we return only the mask (which is a string of dots?).

            # We'll define the mask as a string of eight dots? But the example uses one ellipsis character.
            # Actually, the example shows: sk-…f12a  (that's two characters, an ellipsis, and four characters).
            # The ellipsis is one Unicode character (U+2026) or three dots? In the spec it's written as three dots.
            # We'll use three dots as the ellipsis.

            # For short keys, we cannot show 4 tail chars. So we show only the mask only example of mask is the ellipsis part.
            # Let's assume: for short keys, we return just the ellipsis (three dots) as the mask.
            # However, the UI expects something to show. We'll return a string of three dots.
            return '...'
        # For long enough keys:
        if dash != -1:
            return f"{prefix}...{suffix}"
        else:
            # No dash, just show first 0 chars? Actually, prefix is empty.
            # We'll show ellipsis and last 4.
            return f"...{suffix}"

    def expires_in_s(self, provider: str) -> float | None:
        """Seconds until expiry, or None if no key or expired."""
        if provider not in self._store:
            return None
        _, put_time = self._store[provider]
        left = self._config.ttl_s - (self._clock() - put_time)
        return max(0.0, left)

    def snapshot(self) -> dict:
        """Return a dict of provider info without exposing keys."""
        result = {}
        now = self._clock()
        for provider, (_, put_time) in self._store.items():
            expires_in = self._config.ttl_s - (now - put_time)
            if expires_in < 0:
                # Already expired, but we haven't cleaned up yet? We'll show expired.
                expired = True
                expires_in = 0.0
            else:
                expired = False
            result[provider] = {
                "has_key": True,
                "expires_in_s": max(0.0, expires_in),
                "expiring_soon": expires_in < self._config.warn_before_s,
                "expired": expired,
            }
        return result

    def __getstate__(self):
        # Disallow pickling
        raise InvariantViolation("KeyStore is not serializable")

    def __setstate__(self, state):
        raise InvariantViolation("KeyStore is not serializable")

    def __repr__(self) -> str:
        return f"<KeyStore providers={len(self._store)}>"

    def __str__(self) -> str:
        return self.__repr__()