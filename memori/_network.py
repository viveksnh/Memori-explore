r"""
 __  __                           _
|  \/  | ___ _ __ ___   ___  _ __(_)
| |\/| |/ _ \ '_ ` _ \ / _ \| '__| |
| |  | |  __/ | | | | | (_) | |  | |
|_|  |_|\___|_| |_| |_|\___/|_|  |_|
                  perfectam memoriam
                       memorilabs.ai
"""

import asyncio
import os

import aiohttp
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from memori._config import Config
from memori._exceptions import QuotaExceededError


class Api:
    def __init__(self, config: Config):
        test_mode = os.environ.get("MEMORI_TEST_MODE") == "1"

        self.__base = os.environ.get("MEMORI_API_URL_BASE")

        if self.__base is None:
            if test_mode:
                # Use staging for test mode
                self.__x_api_key = "c18b1022-7fe2-42af-ab01-b1f9139184f0"
                self.__base = "https://staging-api.memorilabs.ai"
            else:
                # Use production
                self.__x_api_key = "96a7ea3e-11c2-428c-b9ae-5a168363dc80"
                self.__base = "https://api.memorilabs.ai"
        else:
            # Custom URL provided, use staging key as default
            self.__x_api_key = "c18b1022-7fe2-42af-ab01-b1f9139184f0"

        self.config = config

    async def augmentation_async(self, payload: dict) -> dict:
        url = self.url("sdk/augmentation")
        headers = self.headers()

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status in (403, 429):
                    if self._is_anonymous():
                        try:
                            quota_response = await r.json()
                            message = quota_response.get("message")
                        except Exception:
                            message = None

                        if message:
                            raise QuotaExceededError(message)
                        raise QuotaExceededError()
                    if r.status == 429:
                        return {}

                r.raise_for_status()
                return await r.json()

    def delete(self, route):
        r = self.__session().delete(self.url(route), headers=self.headers())

        self._handle_quota_response(r)
        r.raise_for_status()

        return r.json()

    def get(self, route):
        r = self.__session().get(self.url(route), headers=self.headers())

        self._handle_quota_response(r)
        r.raise_for_status()

        return r.json()

    async def get_async(self, route):
        return await self.__request_async("GET", route)

    def patch(self, route, json=None):
        r = self.__session().patch(self.url(route), headers=self.headers(), json=json)

        self._handle_quota_response(r)
        r.raise_for_status()

        return r.json()

    async def patch_async(self, route, json=None):
        return await self.__request_async("PATCH", route, json=json)

    def post(self, route, json=None):
        r = self.__session().post(self.url(route), headers=self.headers(), json=json)

        self._handle_quota_response(r)
        r.raise_for_status()

        return r.json()

    async def post_async(self, route, json=None):
        return await self.__request_async("POST", route, json=json)

    def headers(self):
        headers = {"X-Memori-API-Key": self.__x_api_key}

        api_key = self._get_api_key()
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"

        return headers

    def _is_anonymous(self):
        return not self._get_api_key()

    async def __request_async(self, method: str, route: str, json=None):
        url = self.url(route)
        headers = self.headers()
        attempts = 0
        max_retries = 5
        backoff_factor = 1
        print(url)

        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method.upper(),
                        url,
                        headers=headers,
                        json=json,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as r:
                        if r.status in (403, 429) and self._is_anonymous():
                            try:
                                quota_response = await r.json()
                                message = quota_response.get("message")
                            except Exception:
                                message = None

                            if message:
                                raise QuotaExceededError(message)
                            raise QuotaExceededError()

                        r.raise_for_status()
                        return await r.json()
            except aiohttp.ClientResponseError as e:
                if e.status < 500 or e.status > 599:
                    raise

                if attempts >= max_retries:
                    raise

                sleep = backoff_factor * (2**attempts)
                await asyncio.sleep(sleep)
                attempts += 1
            except Exception:
                if attempts >= max_retries:
                    raise

                sleep = backoff_factor * (2**attempts)
                await asyncio.sleep(sleep)
                attempts += 1

    def __session(self):
        adapter = HTTPAdapter(
            max_retries=_ApiRetryRecoverable(
                allowed_methods=["GET", "PATCH", "POST", "PUT", "DELETE"],
                backoff_factor=1,
                raise_on_status=False,
                status=None,
                total=5,
            )
        )

        session = requests.Session()
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    def url(self, route):
        return f"{self.__base}/v1/{route}"

    def _get_api_key(self):
        return self.config.api_key or os.environ.get("MEMORI_API_KEY")

    def _handle_quota_response(self, response):
        if response.status_code not in (403, 429):
            return

        if not self._is_anonymous():
            return

        message = None
        try:
            payload = response.json()
            message = payload.get("message")
        except Exception:
            message = None

        if message:
            raise QuotaExceededError(message)
        raise QuotaExceededError()


class _ApiRetryRecoverable(Retry):
    def is_retry(self, method, status_code, has_retry_after=False):
        return 500 <= status_code <= 599
