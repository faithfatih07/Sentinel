"""OpenAI Responses API adapter for Sentinel's provider-neutral interface."""
import json
import socket
from dataclasses import replace
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pattern_discovery_providers import ProviderConfigurationError, ProviderConfig, ProviderRequestError, resolve_api_key


DEFAULT_ENDPOINT = "https://api.openai.com/v1/responses"
MAX_RESPONSE_BYTES = 3_000_000


class OpenAIResponsesProvider:
    """Synchronous Responses API adapter. Inject ``opener`` for offline tests."""

    def __init__(self, config: ProviderConfig, environ=None, opener=None):
        if not config.model:
            raise ProviderConfigurationError("SENTINEL_DISCOVERY_MODEL is required for OpenAI")
        endpoint = config.endpoint or DEFAULT_ENDPOINT
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or parsed.hostname != "api.openai.com" or parsed.path.rstrip("/") != "/v1/responses" or parsed.username or parsed.password:
            raise ProviderConfigurationError("OpenAI endpoint must be https://api.openai.com/v1/responses")
        self.config = config
        self.endpoint = endpoint
        key_config = config if config.api_key_env else replace(config, api_key_env="OPENAI_API_KEY")
        self._api_key = resolve_api_key(key_config, environ)
        if not self._api_key:
            raise ProviderConfigurationError("OPENAI_API_KEY is unset")
        self._opener = opener or urlopen

    def generate(self, request):
        evidence_payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        payload = {
            "model": self.config.model,
            "store": False,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": (
                        "You are Sentinel's pattern-discovery assistant. Identify possible companies, brands, "
                        "services, business terminology, and recurring expressions from the supplied selected "
                        "evidence. Use only the supplied evidence and active approved library. Every suggestion "
                        "must cite exact selected source IDs and verbatim excerpts. Do not invent facts, citations, "
                        "or quotes; do not infer fraud. Explain why the text may be reusable, classify it as a new "
                        "pattern, variant, or possible duplicate, and give an uncalibrated confidence basis. "
                        "Return an empty proposals list when evidence is insufficient."
                    )}],
                },
                {"role": "user", "content": [{"type": "input_text", "text": evidence_payload}]},
            ],
            "text": {"format": {
                "type": "json_schema",
                "name": "sentinel_pattern_discovery",
                "strict": True,
                "schema": request["response_schema"],
            }},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        api_request = Request(self.endpoint, data=body, method="POST", headers={
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            response = self._opener(api_request, timeout=self.config.timeout_seconds)
            try:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except HTTPError as error:
            raise ProviderRequestError(f"OpenAI API returned HTTP {error.code}") from None
        except (TimeoutError, socket.timeout):
            raise ProviderRequestError("OpenAI API request timed out") from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise ProviderRequestError("OpenAI API request timed out") from None
            raise ProviderRequestError("OpenAI API connection failed") from None
        except OSError:
            raise ProviderRequestError("OpenAI API connection failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderRequestError("OpenAI API response exceeded the size limit")
        try:
            response_json = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderRequestError("OpenAI API returned malformed JSON") from None
        if not isinstance(response_json, dict):
            raise ProviderRequestError("OpenAI API returned an invalid response object")
        if response_json.get("status") not in (None, "completed"):
            raise ProviderRequestError("OpenAI API response did not complete")
        text = response_json.get("output_text")
        if not isinstance(text, str):
            chunks = []
            for output_item in response_json.get("output", []):
                if not isinstance(output_item, dict) or output_item.get("type") != "message":
                    continue
                for content in output_item.get("content", []):
                    if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                        chunks.append(content["text"])
            text = "".join(chunks)
        if not text.strip():
            raise ProviderRequestError("OpenAI API returned no structured text output")
        return text


def create_provider(config):
    return OpenAIResponsesProvider(config)


__all__ = ["OpenAIResponsesProvider", "ProviderRequestError", "create_provider"]
