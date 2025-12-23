r"""
 __  __                           _
|  \/  | ___ _ __ ___   ___  _ __(_)
| |\/| |/ _ \ '_ ` _ \ / _ \| '__| |
| |  | |  __/ | | | | | (_) | |  | |
|_|  |_|\___|_| |_| |_|\___/|_|  |_|
                  perfectam memoriam
                       memorilabs.ai
"""

import os
import sys
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import psycopg

from memori._config import Config
from memori._auth import get_api_key
from memori._exceptions import (
    QuotaExceededError,
    warn_if_legacy_memorisdk_installed,
)
from memori.llm._providers import Agno as LlmProviderAgno
from memori.llm._providers import Anthropic as LlmProviderAnthropic
from memori.llm._providers import Google as LlmProviderGoogle
from memori.llm._providers import LangChain as LlmProviderLangChain
from memori.llm._providers import OpenAi as LlmProviderOpenAi
from memori.llm._providers import PydanticAi as LlmProviderPydanticAi
from memori.llm._providers import XAi as LlmProviderXAi
from memori.memory.augmentation import Manager as AugmentationManager
from memori.memory.recall import Recall
from memori.storage import Manager as StorageManager

__all__ = ["Memori", "QuotaExceededError"]

warn_if_legacy_memorisdk_installed()

_AUTH_NUDGE_SHOWN = False


class LlmRegistry:
    def __init__(self, memori):
        self.memori = memori

    def register(
        self,
        client=None,
        openai_chat=None,
        claude=None,
        gemini=None,
        xai=None,
        chatbedrock=None,
        chatgooglegenai=None,
        chatopenai=None,
        chatvertexai=None,
    ):
        from memori.llm._registry import register_llm

        return register_llm(
            self.memori,
            client=client,
            openai_chat=openai_chat,
            claude=claude,
            gemini=gemini,
            xai=xai,
            chatbedrock=chatbedrock,
            chatgooglegenai=chatgooglegenai,
            chatopenai=chatopenai,
            chatvertexai=chatvertexai,
        )


class Memori:
    def __init__(self, conn: Callable[[], Any] | Any | None = None):
        self.config = Config()
        self.config.api_key = get_api_key()
        self.config.enterprise = os.environ.get("MEMORI_ENTERPRISE", "0") == "1"
        self.config.session_id = uuid4()

        self._check_auth()

        if conn is None:
            conn = self._get_default_connection()

        self.config.storage = StorageManager(self.config).start(conn)
        self.config.augmentation = AugmentationManager(self.config).start(conn)

        self.augmentation = self.config.augmentation
        self.llm = LlmRegistry(self)
        self.agno = LlmProviderAgno(self)
        self.anthropic = LlmProviderAnthropic(self)
        self.google = LlmProviderGoogle(self)
        self.langchain = LlmProviderLangChain(self)
        self.openai = LlmProviderOpenAi(self)
        self.pydantic_ai = LlmProviderPydanticAi(self)
        self.xai = LlmProviderXAi(self)

    def _check_auth(self):
        global _AUTH_NUDGE_SHOWN
        if _AUTH_NUDGE_SHOWN:
            return

        if self.config.api_key:
            return

        if os.environ.get("MEMORI_DISABLE_NUDGE") == "1":
            return

        if not (sys.stdout.isatty() or sys.stderr.isatty()):
            return

        _AUTH_NUDGE_SHOWN = True

        message = (
            "Memori is running without an API key.\n"
            "Run `memori login` to unlock higher limits and sync your account."
        )

        try:
            from rich.console import Console
            from rich.panel import Panel
            from rich.text import Text
        except Exception:
            print(message, file=sys.stderr)
            return

        console = Console(stderr=True)
        text = Text("Memori is running without an API key.\n", style="bold yellow")
        text.append(
            "Run `memori login` to unlock higher limits and sync your account."
        )
        console.print(Panel(text, title="Memori login", border_style="yellow"))

    def _get_default_connection(self) -> Callable[[], Any]:
        connection_string = os.environ.get("MEMORI_COCKROACHDB_CONNECTION_STRING")
        if connection_string:
            return lambda: psycopg.connect(connection_string)

        raise RuntimeError(
            "No connection factory provided. Either pass 'conn' parameter or set "
            "MEMORI_COCKROACHDB_CONNECTION_STRING environment variable."
        )

    def attribution(self, entity_id=None, process_id=None):
        if entity_id is not None:
            entity_id = str(entity_id)

            if len(entity_id) > 100:
                raise RuntimeError("entity_id cannot be greater than 100 characters")

        if process_id is not None:
            process_id = str(process_id)

            if len(process_id) > 100:
                raise RuntimeError("process_id cannot be greater than 100 characters")

        self.config.entity_id = entity_id
        self.config.process_id = process_id

        return self

    def new_session(self):
        self.config.session_id = uuid4()
        self.config.reset_cache()
        return self

    def set_session(self, id):
        self.config.session_id = id
        return self

    def recall(self, query: str, limit: int = 5):
        return Recall(self.config).search_facts(query, limit)
