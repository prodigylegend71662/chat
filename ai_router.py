"""Free, keyless AI routing with round-robin load balancing and failover.

The router keeps a rotating provider index so each request is sent to the next
provider in the cycle, which spreads load evenly across all five APIs. If a
provider returns an error, malformed data, or an empty response, the router
moves on to the next provider in order. Providers that fail three consecutive
requests are marked as dead and skipped until the next revive cycle.
"""

import logging
from urllib.parse import quote

import requests


logger = logging.getLogger("ai_router")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)

TIMEOUT_SECONDS = 30
MAX_RESPONSE_LENGTH = 500
FALLBACK_MESSAGE = "All AI providers are down right now. Try again in a minute."

PROVIDERS = [
    {
        "name": "Pollinations.ai",
        "method": "GET",
        "url": "https://text.pollinations.ai/{prompt}",
        "headers": {},
    },
    {
        "name": "KeylessAI",
        "method": "POST",
        "url": "https://keylessai.thryx.workers.dev/v1/chat/completions",
        "headers": {"Authorization": "Bearer not-needed", "Content-Type": "application/json"},
        "payload_template": {
            "model": "openai-fast",
            "messages": [{"role": "user", "content": "{prompt}"}],
        },
    },
    {
        "name": "ApiAirforce",
        "method": "POST",
        "url": "https://api.airforce/v1/chat/completions",
        "headers": {"Content-Type": "application/json"},
        "payload_template": {
            "model": "grok-4.1-mini:free",
            "messages": [{"role": "user", "content": "{prompt}"}],
        },
    },
    {
        "name": "UncloseAI Hermes",
        "method": "POST",
        "url": "https://hermes.ai.unturf.com/v1/chat/completions",
        "headers": {"Content-Type": "application/json"},
        "payload_template": {
            "model": "hermes",
            "messages": [{"role": "user", "content": "{prompt}"}],
        },
    },
    {
        "name": "crax-gpt",
        "method": "POST",
        "url": "https://gpt.crax.lol/v1/chat/completions",
        "headers": {"Authorization": "Bearer crax-gpt", "Content-Type": "application/json"},
        "payload_template": {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "{prompt}"}],
        },
    },
]

_PROVIDER_STATE = {
    provider["name"]: {"dead": False, "consecutive_failures": 0}
    for provider in PROVIDERS
}

_rotation_index = 0
_call_count = 0


def _trim_response(text):
    """Keep AI responses bounded so large provider outputs do not blow up the DB."""
    if not text:
        return ""
    return text.strip()[:MAX_RESPONSE_LENGTH]


def _mark_success(provider_name):
    state = _PROVIDER_STATE[provider_name]
    state["consecutive_failures"] = 0
    state["dead"] = False


def _mark_failure(provider_name):
    state = _PROVIDER_STATE[provider_name]
    state["consecutive_failures"] += 1
    if state["consecutive_failures"] >= 3:
        state["dead"] = True
        logger.warning("Provider %s marked dead after 3 consecutive failures.", provider_name)


def _revive_dead_provider():
    """Every 10th request, revive exactly one dead provider so it can be retried."""
    for provider in PROVIDERS:
        name = provider["name"]
        if _PROVIDER_STATE[name]["dead"]:
            _PROVIDER_STATE[name]["dead"] = False
            _PROVIDER_STATE[name]["consecutive_failures"] = 0
            logger.info("Attempting to revive provider %s.", name)
            return name
    return None


def _build_payload(provider, prompt):
    if provider.get("payload_template"):
        payload = {
            "model": provider["payload_template"]["model"],
            "messages": [{"role": "user", "content": prompt}],
        }
        return payload
    return None


def _parse_chat_completion_response(response):
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    return content.strip()


def _request_provider(provider, prompt):
    """Call a single provider and return a cleaned response string or None on failure."""
    name = provider["name"]
    logger.info("Trying provider %s", name)

    try:
        if provider["method"] == "GET":
            url = provider["url"].format(prompt=quote(prompt, safe=""))
            response = requests.get(url, timeout=TIMEOUT_SECONDS)
            if response.status_code != 200:
                logger.warning("Provider %s returned non-200 status %s.", name, response.status_code)
                return None
            content = _trim_response(response.text)
            if not content:
                logger.warning("Provider %s returned an empty response.", name)
                return None
            logger.info("Provider %s succeeded.", name)
            return content

        payload = _build_payload(provider, prompt)
        response = requests.post(
            provider["url"],
            headers=provider.get("headers", {}),
            json=payload,
            timeout=TIMEOUT_SECONDS,
        )

        if response.status_code != 200:
            logger.warning("Provider %s returned non-200 status %s.", name, response.status_code)
            return None

        content = _trim_response(_parse_chat_completion_response(response))
        if not content:
            logger.warning("Provider %s returned empty chat content.", name)
            return None

        logger.info("Provider %s succeeded.", name)
        return content

    except Exception as exc:
        logger.exception("Provider %s raised an exception: %s", name, exc)
        return None


def get_response(prompt: str) -> str:
    """Return a provider response for the supplied prompt.

    The function uses round-robin rotation across the list of providers. Each
    request starts at the current rotation index and walks forward through the
    remaining providers until it finds a live one. A provider that fails three
    times in a row is skipped for the rest of the cycle until a later revive
    attempt re-enables it.
    """

    global _rotation_index, _call_count

    prompt = (prompt or "").strip()
    if not prompt:
        return "Please provide a prompt after @ChatBot."

    _call_count += 1
    if _call_count % 10 == 0:
        _revive_dead_provider()

    attempted = 0
    providers_to_try = len(PROVIDERS)

    while attempted < providers_to_try:
        index = (_rotation_index + attempted) % providers_to_try
        provider = PROVIDERS[index]
        name = provider["name"]

        if _PROVIDER_STATE[name]["dead"]:
            attempted += 1
            continue

        response = _request_provider(provider, prompt)
        if response:
            _rotation_index = (index + 1) % providers_to_try
            _mark_success(name)
            return response

        _mark_failure(name)
        attempted += 1

    # If every provider failed, keep the round-robin cursor moving forward so the
    # next request starts from the next provider instead of getting stuck.
    _rotation_index = (_rotation_index + 1) % providers_to_try
    logger.error("All AI providers failed for prompt: %s", prompt)
    return FALLBACK_MESSAGE
