from __future__ import annotations

import json
import time

import httpx
import pytest

from post_review.errors import AppError, DeleteUnknown, ProviderError
from post_review.providers.azure import AzureTranslator
from post_review.providers.http import read_request, retry_deadline
from post_review.providers.jev import JevClassifier, profile_config
from post_review.providers.x import OAuth1UserAuth, XClient, parse_live


def jev_response(model="jev-1.13.0"):
    return {"model": model, "answers": {"publication": {"type": "choice", "choice": "DELETE_CANDIDATE",
            "confidence": 0.7, "probabilities": {"DELETE_CANDIDATE": 0.9, "KEEP": 0.06, "NEEDS_CONTEXT": 0.04}}},
            "usage": {"input_tokens": 120, "output_tokens": 20}}


def auth():
    return OAuth1UserAuth("test-key", "test-secret", "test-token", "test-token-secret")


async def test_jev_http_contract_and_no_unneeded_identifiers(post, config):
    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["state"]["original"] == post.text
        assert "owner_id" not in payload["state"] and "id" not in payload["state"]
        assert payload["questions"]["publication"]["type"] == "choice"
        return httpx.Response(200, json=jev_response())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await JevClassifier(client, "test-key", config).classify(post)
    assert result.delete_probability == 0.9
    assert result.confidence == 0.7
    assert result.usage["input_tokens"] == 120


async def test_jev_both_input_preserves_original(post, config):
    config = dict(config, input_mode="both")
    def handler(request):
        state = json.loads(request.content)["state"]
        assert state["original"] == post.text
        assert state["english_translation"] == "translation"
        return httpx.Response(200, json=jev_response())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await JevClassifier(client, "test-key", config).classify(post, "translation")
    assert result.translation == "translation"


@pytest.mark.parametrize("response", [jev_response("jev-9.9.9"), {}, {"answers": []}])
async def test_jev_rejects_model_drift_and_malformed_response(post, config, response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as client:
        with pytest.raises(ProviderError, match="jev_invalid_response"):
            await JevClassifier(client, "test-key", config).classify(post)


async def test_jev_malformed_probability_does_not_become_high_score(post, config):
    response = jev_response()
    response["answers"]["publication"]["probabilities"]["KEEP"] = -1
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as client:
        with pytest.raises(ProviderError):
            await JevClassifier(client, "test-key", config).classify(post)


async def test_no_silent_translation_fallback(post, config):
    config = dict(config, input_mode="english")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("must not call API"))) as client:
        with pytest.raises(AppError, match="translation_required"):
            await JevClassifier(client, "test-key", config).classify(post)


def test_version_alias_rejected(config):
    with pytest.raises(AppError, match="versioned_jev"):
        profile_config(config["rules"], "jev-latest", "original", 16000)


async def test_azure_contract(post):
    def handler(request):
        assert request.headers["Ocp-Apim-Subscription-Key"] == "key"
        assert request.headers["Ocp-Apim-Subscription-Region"] == "japaneast"
        assert request.url.params["to"] == "en"
        assert request.url.params["profanityAction"] == "NoAction"
        assert "from" not in request.url.params
        assert json.loads(request.content) == [{"Text": post.text}]
        return httpx.Response(200, json=[{"translations": [{"text": "English", "to": "en"}]}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await AzureTranslator(client, "key", "japaneast").translate(post.text) == "English"


@pytest.mark.parametrize("body", [[], [{"translations": []}], [{"translations": [{"text": "", "to": "en"}]}]])
async def test_azure_malformed_response(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        with pytest.raises(ProviderError, match="azure_invalid_response"):
            await AzureTranslator(client, "key").translate("原文")


def test_translation_cache_has_explicit_revision():
    client = object()
    a = AzureTranslator(client, "key", revision="v1")
    b = AzureTranslator(client, "key", revision="v2")
    assert a.cache_key("abc") != b.cache_key("abc")
    assert a.cache_key("abc") != a.cache_key("abc ")


async def test_x_oauth_and_me_lookup(post):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers["authorization"].startswith("OAuth ")
        assert "oauth_signature=" in request.headers["authorization"]
        assert "test-secret" not in request.headers["authorization"]
        if request.url.path == "/2/users/me":
            return httpx.Response(200, json={"data": {"id": post.owner_id}})
        assert "author_id" in request.url.params["tweet.fields"]
        return httpx.Response(200, json={"data": {"id": post.id, "author_id": post.owner_id,
                                                  "text": post.text}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        x = XClient(client, auth())
        assert await x.me() == post.owner_id
        live = await x.lookup(post.id)
    assert live.text == post.text and len(requests) == 2


async def test_x_delete_requires_confirmed_true(post):
    def handler(request):
        assert request.method == "DELETE"
        return httpx.Response(200, json={"data": {"deleted": True}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await XClient(client, auth()).delete(post.id) is None


@pytest.mark.parametrize("body", [{"data": {"deleted": False}}, {"data": {"deleted": 1}},
                                  {"data": {"deleted": "true"}}, {},
                                  {"data": {"deleted": True}, "errors": [{"detail": "private-text"}]}])
async def test_ambiguous_success_is_unknown(post, body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        with pytest.raises(DeleteUnknown) as error:
            await XClient(client, auth()).delete(post.id)
    assert "private-text" not in str(error.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
async def test_delete_http_error_not_counted_as_success(post, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"private": "SECRET BODY"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            await XClient(client, auth()).delete(post.id)
    assert not isinstance(error.value, DeleteUnknown)
    assert len(calls) == 1 and "SECRET BODY" not in str(error.value)


@pytest.mark.parametrize("status", [302, 500, 503])
async def test_delete_5xx_or_redirect_is_unknown_without_retry(post, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://untrusted.invalid/"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DeleteUnknown):
            await XClient(client, auth()).delete(post.id)
    assert len(calls) == 1


async def test_delete_timeout_is_not_retried(post):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("might include private text", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DeleteUnknown) as error:
            await XClient(client, auth()).delete(post.id)
    assert len(calls) == 1
    assert "private text" not in str(error.value)


def test_rate_deadline_honors_server_headers():
    response = httpx.Response(429, headers={"Retry-After": "40", "x-rate-limit-reset": "150"})
    assert retry_deadline(response, now=100.0) == 151.0
    response = httpx.Response(200, headers={"x-rate-limit-remaining": "0", "x-rate-limit-reset": "999"})
    assert retry_deadline(response, now=100.0) == 1000.0


async def test_long_retry_after_stops_instead_of_retrying_too_soon():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "900"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            await read_request(client, "GET", "https://api.x.com/2/users/me", service="x")
    assert len(calls) == 1
    assert error.value.retry_at > time.time() + 850


async def test_inference_retry_respects_429_then_succeeds(monkeypatch):
    waits = []
    async def no_sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr("post_review.providers.http.asyncio.sleep", no_sleep)
    responses = [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={})]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: responses.pop(0))) as client:
        result = await read_request(client, "POST", "https://api.typesafe.ai/v1/systemone", service="jev")
    assert result.status_code == 200 and len(waits) == 1 and waits[0] > 1.5


def test_live_reply_and_media_flags(post):
    obj = {"data": {"id": post.id, "author_id": post.owner_id, "text": post.text,
                    "referenced_tweets": [{"type": "replied_to", "id": "1"}],
                    "attachments": {"media_keys": ["x"]}}}
    live = parse_live(obj)
    assert set(live.flags) == {"reply_context_missing", "media_not_analyzed"}
