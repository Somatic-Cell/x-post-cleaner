from __future__ import annotations

import httpx

from ..domain import digest
from ..errors import AppError, ProviderError
from .http import read_request

URL = "https://api.cognitive.microsofttranslator.com/translate"


class AzureTranslator:
    def __init__(self, client: httpx.AsyncClient, key: str, region: str = "", *,
                 revision: str = "azure-v3-auto-en-cache-v1"):
        if not key:
            raise AppError("missing_azure_translator_key")
        self.client, self.key, self.region, self.revision = client, key, region, revision

    def cache_key(self, text: str) -> str:
        return digest({"provider": "azure", "revision": self.revision, "from": "auto",
                       "to": "en", "text": text, "textType": "plain", "profanityAction": "NoAction"})

    async def translate(self, text: str) -> str:
        headers = {"Ocp-Apim-Subscription-Key": self.key}
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region
        # Autodetection permits English / mixed-language posts in the same archive.
        response = await read_request(self.client, "POST", URL, service="azure", headers=headers,
                                      params={"api-version": "3.0", "to": "en", "textType": "plain",
                                              "profanityAction": "NoAction"}, json=[{"Text": text}])
        try:
            obj = response.json()
            if not isinstance(obj, list) or len(obj) != 1:
                raise ValueError
            translations = obj[0]["translations"]
            if len(translations) != 1 or translations[0]["to"] != "en":
                raise ValueError
            result = translations[0]["text"]
            if not isinstance(result, str) or text and not result:
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError, IndexError):
            raise ProviderError("azure_invalid_response") from None
