"""Provider-neutral extension point for pattern-discovery LLM adapters.

Sentinel intentionally ships no provider adapter. Integrators install an
adapter exposing the ``sentinel.pattern_discovery_providers`` entry-point
group; tests inject a mock implementation directly.
"""
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Mapping, Protocol, runtime_checkable
import os
import re


ENTRY_POINT_GROUP = "sentinel.pattern_discovery_providers"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProviderConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = 60.0

    @classmethod
    def from_environment(cls, environ=None):
        env = os.environ if environ is None else environ
        provider = (env.get("SENTINEL_DISCOVERY_PROVIDER") or "disabled").strip()
        model = (env.get("SENTINEL_DISCOVERY_MODEL") or "").strip() or None
        endpoint = (env.get("SENTINEL_DISCOVERY_ENDPOINT") or "").strip() or None
        api_key_env = (env.get("SENTINEL_DISCOVERY_API_KEY_ENV") or "").strip() or None
        try:
            timeout = float(env.get("SENTINEL_DISCOVERY_TIMEOUT_SECONDS", "60"))
        except (TypeError, ValueError) as exc:
            raise ProviderConfigurationError("SENTINEL_DISCOVERY_TIMEOUT_SECONDS must be numeric") from exc
        if not 1 <= timeout <= 600:
            raise ProviderConfigurationError("SENTINEL_DISCOVERY_TIMEOUT_SECONDS must be between 1 and 600")
        if provider.lower() != "disabled":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", provider):
                raise ProviderConfigurationError("SENTINEL_DISCOVERY_PROVIDER must be an adapter entry-point name")
            if not model:
                raise ProviderConfigurationError("SENTINEL_DISCOVERY_MODEL is required when a provider is enabled")
        if api_key_env and not _IDENTIFIER.fullmatch(api_key_env):
            raise ProviderConfigurationError("SENTINEL_DISCOVERY_API_KEY_ENV must name an environment variable")
        return cls(provider=provider, model=model, endpoint=endpoint, api_key_env=api_key_env, timeout_seconds=timeout)


@runtime_checkable
class PatternDiscoveryProvider(Protocol):
    """Adapter contract. Return a JSON object or JSON string; do not access Sentinel DB."""

    def generate(self, request: Mapping[str, Any]) -> Mapping[str, Any] | str:
        ...


def resolve_api_key(config: ProviderConfig, environ=None):
    """Resolve a provider secret by the configured variable name; never log it."""
    if not config.api_key_env:
        return None
    env = os.environ if environ is None else environ
    value = env.get(config.api_key_env)
    if not value:
        raise ProviderConfigurationError(f"The configured secret environment variable {config.api_key_env!r} is unset")
    return value


def load_provider(config: ProviderConfig | None = None, environ=None):
    config = config or ProviderConfig.from_environment(environ)
    if config.provider.lower() == "disabled":
        raise ProviderConfigurationError("AI discovery is disabled; set SENTINEL_DISCOVERY_PROVIDER to an installed adapter")
    matches = [entry for entry in entry_points(group=ENTRY_POINT_GROUP) if entry.name == config.provider]
    if not matches:
        raise ProviderConfigurationError(
            f"No installed discovery adapter named {config.provider!r}; install an adapter exposing {ENTRY_POINT_GROUP}"
        )
    try:
        factory = matches[0].load()
        provider = factory(config)
    except ProviderConfigurationError:
        raise
    except Exception as exc:
        raise ProviderConfigurationError(f"Could not initialize discovery adapter {config.provider!r} ({type(exc).__name__})") from exc
    if not isinstance(provider, PatternDiscoveryProvider):
        raise ProviderConfigurationError("Provider adapter must implement generate(request)")
    return provider
