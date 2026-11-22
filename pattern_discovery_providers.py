"""Provider-neutral extension point for pattern-discovery LLM adapters."""
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Mapping, Protocol, runtime_checkable
import os
import re
import ssl
from urllib.request import urlopen
from pathlib import Path


ENTRY_POINT_GROUP = "sentinel.pattern_discovery_providers"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def verified_urlopen(request, timeout):
    """Use certifi roots when installed; otherwise preserve Python's TLS defaults."""
    try:
        import certifi
    except ImportError:
        return urlopen(request, timeout=timeout)
    context = ssl.create_default_context(cafile=certifi.where())
    return urlopen(request, timeout=timeout, context=context)


class ProviderConfigurationError(RuntimeError):
    pass


class ProviderRequestError(RuntimeError):
    """Sanitized provider request failure safe to persist in run audit data."""

    def __init__(self, safe_message):
        super().__init__(safe_message)
        self.safe_message = safe_message


def project_environment(environ=None, dotenv_path=None):
    """Return process settings plus root .env values without exposing secrets.

    An explicit mapping is treated as a complete environment, which keeps
    tests deterministic and prevents the developer's .env from leaking into
    fixtures. Process variables override .env values during normal execution.
    """
    if environ is not None and dotenv_path is None:
        return dict(environ)
    values = {}
    env_path = Path(dotenv_path) if dotenv_path is not None else Path(__file__).resolve().parent / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not _IDENTIFIER.fullmatch(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value
    values.update(os.environ if environ is None else environ)
    return values


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = 60.0

    @classmethod
    def from_environment(cls, environ=None):
        env = project_environment(environ)
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
        if provider.lower() == "openai":
            if api_key_env not in (None, "OPENAI_API_KEY"):
                raise ProviderConfigurationError("The OpenAI adapter requires OPENAI_API_KEY")
            api_key_env = "OPENAI_API_KEY"
        if provider.lower() == "gemini":
            if api_key_env not in (None, "GEMINI_API_KEY"):
                raise ProviderConfigurationError("The Gemini adapter requires GEMINI_API_KEY")
            api_key_env = "GEMINI_API_KEY"
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
    env = project_environment(environ)
    value = env.get(config.api_key_env)
    if not value:
        raise ProviderConfigurationError(f"The configured secret environment variable {config.api_key_env!r} is unset")
    return value


def load_provider(config: ProviderConfig | None = None, environ=None):
    config = config or ProviderConfig.from_environment(environ)
    if config.provider.lower() == "disabled":
        raise ProviderConfigurationError("AI discovery is disabled; set SENTINEL_DISCOVERY_PROVIDER to an installed adapter")
    try:
        matches = [entry for entry in entry_points(group=ENTRY_POINT_GROUP) if entry.name == config.provider]
        if matches:
            factory = matches[0].load()
        elif config.provider.lower() == "openai":
            from pattern_discovery_openai import OpenAIResponsesProvider
            provider = OpenAIResponsesProvider(config, environ=environ)
            factory = None
        elif config.provider.lower() == "gemini":
            from pattern_discovery_gemini import GeminiGenerateContentProvider
            provider = GeminiGenerateContentProvider(config, environ=environ)
            factory = None
        else:
            raise ProviderConfigurationError(
                f"No installed discovery adapter named {config.provider!r}; install an adapter exposing {ENTRY_POINT_GROUP}"
            )
        if factory is not None:
            provider = factory(config)
    except ProviderConfigurationError:
        raise
    except Exception as exc:
        raise ProviderConfigurationError(f"Could not initialize discovery adapter {config.provider!r} ({type(exc).__name__})") from exc
    if not isinstance(provider, PatternDiscoveryProvider):
        raise ProviderConfigurationError("Provider adapter must implement generate(request)")
    return provider
