"""Google Gemini GenerateContent adapter for Sentinel pattern discovery."""
import json
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request

from pattern_discovery_providers import (
    ProviderConfig,
    ProviderConfigurationError,
    ProviderRequestError,
    resolve_api_key,
    verified_urlopen,
)


DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"
MAX_RESPONSE_BYTES = 3_000_000
PAGE_SIZE = 1000


class GeminiGenerateContentProvider:
    """Provider adapter with model capability validation before inference."""

    def __init__(self, config: ProviderConfig, environ=None, opener=None):
        if not config.model:
            raise ProviderConfigurationError("SENTINEL_DISCOVERY_MODEL is required for Gemini")
        endpoint = config.endpoint or DEFAULT_ENDPOINT
        parsed = urlparse(endpoint)
        if (parsed.scheme != "https" or parsed.hostname != "generativelanguage.googleapis.com"
                or parsed.path.rstrip("/") != "/v1beta" or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ProviderConfigurationError(
                "Gemini endpoint must be https://generativelanguage.googleapis.com/v1beta"
            )
        self.config = config
        self.endpoint = endpoint.rstrip("/")
        self._api_key = resolve_api_key(config, environ)
        self.opener = opener or verified_urlopen
        self.usage = None

    def _request_json(self, method, url, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, method=method, headers={
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            response = self.opener(request, timeout=self.config.timeout_seconds)
            try:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except HTTPError as error:
            raise self._http_error(error) from None
        except ssl.SSLCertVerificationError:
            raise ProviderRequestError("Gemini API TLS certificate verification failed") from None
        except ssl.SSLError:
            raise ProviderRequestError("Gemini API TLS connection failed") from None
        except (TimeoutError, socket.timeout):
            raise ProviderRequestError("Gemini API request timed out") from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise ProviderRequestError("Gemini API request timed out") from None
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise ProviderRequestError("Gemini API TLS certificate verification failed") from None
            if isinstance(error.reason, ssl.SSLError):
                raise ProviderRequestError("Gemini API TLS connection failed") from None
            if isinstance(error.reason, socket.gaierror):
                raise ProviderRequestError("Gemini API DNS lookup failed") from None
            raise ProviderRequestError("Gemini API connection failed") from None
        except OSError:
            raise ProviderRequestError("Gemini API connection failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderRequestError("Gemini API response exceeded the size limit")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderRequestError("Gemini API returned malformed JSON") from None
        if not isinstance(result, dict):
            raise ProviderRequestError("Gemini API returned an invalid response object")
        return result

    @staticmethod
    def _http_error(error):
        # Inspect only known error codes to classify failures. Never persist
        # provider error bodies, headers, URLs, or credentials.
        try:
            details = json.loads(error.read(32_768).decode("utf-8"))
            status = str((details.get("error") or {}).get("status", "")).upper()
            message = str((details.get("error") or {}).get("message", "")).lower()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            status, message = "", ""
        code = error.code
        if "API_KEY_INVALID" in status or "api key not valid" in message:
            label = "Gemini API key is invalid"
        elif code in (401, 403) or status == "PERMISSION_DENIED":
            label = "Gemini API rejected credentials or permissions"
        elif code == 429 or status == "RESOURCE_EXHAUSTED":
            label = "Gemini API rate limit or quota exceeded"
        elif code == 404 or status == "NOT_FOUND":
            if "model" in message and ("not found" in message or "not supported" in message or "not available" in message):
                label = "Gemini model was listed but is unavailable or unsupported for generateContent (HTTP 404)"
            else:
                label = "Gemini API endpoint or model is unavailable (HTTP 404)"
        elif code >= 500:
            label = "Gemini API service error"
        else:
            label = f"Gemini API rejected request (HTTP {code})"
        return ProviderRequestError(label)

    def _verify_model_support(self):
        configured = self.config.model.removeprefix("models/")
        if not configured or "/" in configured:
            raise ProviderConfigurationError("SENTINEL_DISCOVERY_MODEL must be a Gemini model ID")
        page_token = None
        while True:
            params = {"pageSize": str(PAGE_SIZE)}
            if page_token:
                params["pageToken"] = page_token
            response = self._request_json("GET", f"{self.endpoint}/models?{urlencode(params)}")
            models = response.get("models", [])
            if not isinstance(models, list):
                raise ProviderRequestError("Gemini model-list response is malformed")
            for model in models:
                if not isinstance(model, dict):
                    continue
                model_id = model.get("baseModelId") or model.get("name", "").removeprefix("models/")
                if model_id == configured:
                    methods = model.get("supportedGenerationMethods", [])
                    if not isinstance(methods, list) or "generateContent" not in methods:
                        raise ProviderConfigurationError(
                            f"Configured Gemini model {configured!r} does not support generateContent"
                        )
                    return configured
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        raise ProviderConfigurationError(
            f"Configured Gemini model {configured!r} is not available for this API key or does not exist"
        )

    def generate(self, request):
        self.usage = None
        model = self._verify_model_support()
        prompt = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        payload = {
            "systemInstruction": {"parts": [{"text": (
                "You are Sentinel's pattern-discovery assistant. Carefully inspect every supplied current selected "
                "evidence item and the active approved Pattern Library. Surface potentially useful companies or "
                "brands, publishers, channel or program titles, products, concrete services or offers, business "
                "terminology, distinctive claims or slogans, and "
                "recurring expressions as candidates for human review; discovery candidates need not already be "
                "proven or recurrent. A clear single source is enough to propose a company_brand, "
                "service_description, or business_terminology candidate, with confidence reflecting the limited "
                "support. Only classify wording as recurring_phrase when it appears in at least two distinct source "
                "artifacts. A repeated channel, publisher, or program title may be a brand lead, but do not infer "
                "legal company status or treat a person name alone as an organization. Respect the source language; "
                "use normalized_text to notice wording but keep candidate names in the original language. "
                "Treat the active library as a comparison aid: do not omit a supported related phrase; "
                "classify it as a variant or possible duplicate and cite its active target. For each citation, select "
                "one source_excerpt_id from the cited evidence item's citation_spans. Return exact_source_excerpt "
                "as null; Sentinel resolves the exact immutable source text from that span ID. Do not invent or alter "
                "span IDs. Select a span that contains candidate_name itself; do not infer names from uncited context "
                "or paraphrase them. Every proposal must cite real source candidate and artifact IDs and a supplied span ID. Never "
                "invent names, sources, quotations, or facts, and do not infer fraud or intent. Explain the basis "
                "and uncertainty of the suggestion. Return an empty proposals list only if a complete scan finds no "
                "source-supported candidate that could reasonably merit human review. Suggestions are not approvals."
            )}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": request["response_schema"],
                "maxOutputTokens": 8192,
            },
        }
        encoded_model = quote(model, safe="-_.")
        result = self._request_json(
            "POST", f"{self.endpoint}/models/{encoded_model}:generateContent", payload
        )
        prompt_feedback = result.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            raise ProviderRequestError("Gemini blocked the discovery prompt")
        candidates = result.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderRequestError("Gemini returned no response candidates")
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ProviderRequestError("Gemini returned a malformed candidate")
        finish_reason = candidate.get("finishReason")
        if finish_reason and finish_reason not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
            raise ProviderRequestError(f"Gemini response did not complete ({finish_reason})")
        content = candidate.get("content")
        parts = content.get("parts", []) if isinstance(content, dict) else []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        if not text.strip():
            raise ProviderRequestError("Gemini returned no structured text output")
        usage = result.get("usageMetadata")
        if isinstance(usage, dict):
            self.usage = {
                key: usage[key] for key in (
                    "promptTokenCount", "candidatesTokenCount", "totalTokenCount",
                    "cachedContentTokenCount", "thoughtsTokenCount",
                ) if isinstance(usage.get(key), int)
            }
        return text


def create_provider(config):
    return GeminiGenerateContentProvider(config)


__all__ = ["GeminiGenerateContentProvider", "create_provider"]
