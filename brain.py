"""
Мультипровайдерный AI-анализатор новостей.

Поддерживает несколько LLM API с автоматическим fallback:
  1. Google Gemini (gemini-2.5-flash-lite) — основной
  2. Groq (llama-3.3-70b-versatile) — fallback #1
  3. Cerebras (llama-3.3-70b) — fallback #2

Если один провайдер упирается в лимит (429), бот автоматически
переключается на следующий.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from database import UserTopic

logger = logging.getLogger(__name__)


# ── Результат анализа ─────────────────────────────────────────

@dataclass(slots=True)
class RelevanceResult:
    is_relevant: bool
    matched_topic_id: Optional[int]
    confidence: float = 0.0
    provider: str = ""


# ── Утилиты ───────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """Извлечь первый JSON-объект из текста, игнорируя markdown."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.strip()
    match = re.search(r"\{[^{}]*\}", text)
    if match:
        return json.loads(match.group())
    raise json.JSONDecodeError("No JSON object found", text, 0)


def _build_relevance_prompt(article_text: str, user_topics: Sequence[UserTopic]) -> str:
    """Общий промпт для всех провайдеров."""
    topics_list = "\n".join(
        f"- ID {t.id}: {t.topic_description}" for t in user_topics
    )
    return (
        "Ты аналитик новостей. Определи, относится ли статья к одной из тем пользователя.\n"
        "Учитывай сложные условия в описании тем (временные рамки, конкретные этапы, пороги и т.д.).\n\n"
        f"Темы пользователя:\n{topics_list}\n\n"
        f"Текст статьи:\n{article_text[:3000]}\n\n"
        'Ответь ТОЛЬКО JSON: {"is_relevant": true/false, "matched_topic_id": <id или null>, "confidence": <0.0-1.0>}'
    )


def _parse_relevance(raw: str, provider_name: str) -> RelevanceResult:
    """Распарсить JSON-ответ от любого провайдера."""
    result = _extract_json(raw)
    return RelevanceResult(
        is_relevant=bool(result.get("is_relevant", False)),
        matched_topic_id=result.get("matched_topic_id"),
        confidence=float(result.get("confidence", 0.0)),
        provider=provider_name,
    )


# ── Базовый класс провайдера ─────────────────────────────────

class LLMProvider(ABC):
    """Интерфейс LLM-провайдера."""

    name: str = "base"

    @abstractmethod
    async def generate(self, prompt: str) -> str:
        """Отправить промпт, вернуть текст ответа."""
        ...

    def is_configured(self) -> bool:
        """Есть ли API-ключ для этого провайдера."""
        return True


# ── Google Gemini ─────────────────────────────────────────────

class GeminiProvider(LLMProvider):
    """
    Google Gemini через новый SDK google-genai.
    Модель: gemini-2.5-flash-lite (1000 req/day free).
    """

    name = "Gemini"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._client = None

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def generate(self, prompt: str) -> str:
        client = self._get_client()
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
        )
        return response.text or ""


# ── Groq ──────────────────────────────────────────────────────

class GroqProvider(LLMProvider):
    """
    Groq с LPU-ускорением.
    Модель: llama-3.3-70b-versatile (~1000 req/day free).
    """

    name = "Groq"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.getenv("GROQ_API_KEY")
        self._client = None

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _get_client(self):
        if self._client is None:
            from groq import AsyncGroq
            self._client = AsyncGroq(api_key=self._api_key)
        return self._client

    async def generate(self, prompt: str) -> str:
        client = self._get_client()
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""


# ── Cerebras ──────────────────────────────────────────────────

class CerebrasProvider(LLMProvider):
    """
    Cerebras Inference (OpenAI-compatible API).
    Модель: llama-3.3-70b (1M tokens/day free).
    """

    name = "Cerebras"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.getenv("CEREBRAS_API_KEY")
        self._client = None

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url="https://api.cerebras.ai/v1",
            )
        return self._client

    async def generate(self, prompt: str) -> str:
        client = self._get_client()
        response = await client.chat.completions.create(
            model="llama-3.3-70b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""


# ── OpenRouter (бонус — 24+ бесплатных моделей) ──────────────

class OpenRouterProvider(LLMProvider):
    """
    OpenRouter — агрегатор с бесплатными моделями.
    Модель: meta-llama/llama-3.3-70b-instruct:free (50-1000 req/day).
    """

    name = "OpenRouter"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self._client = None

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url="https://openrouter.ai/api/v1",
            )
        return self._client

    async def generate(self, prompt: str) -> str:
        client = self._get_client()
        response = await client.chat.completions.create(
            model="meta-llama/llama-3.3-70b-instruct:free",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )
        return response.choices[0].message.content or ""


# ── Главный класс AIAnalyzer ─────────────────────────────────

class AIAnalyzer:
    """
    Мультипровайдерный анализатор с автоматическим fallback.

    Порядок провайдеров: Gemini → Groq → Cerebras → OpenRouter.
    Если текущий провайдер возвращает ошибку (429/500), бот пробует следующий.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        groq_key: Optional[str] = None,
        cerebras_key: Optional[str] = None,
        openrouter_key: Optional[str] = None,
    ) -> None:
        # Создаём провайдеров
        all_providers = [
            GeminiProvider(api_key),
            GroqProvider(groq_key),
            CerebrasProvider(cerebras_key),
            OpenRouterProvider(openrouter_key),
        ]

        # Оставляем только те, для которых есть API-ключ
        self._providers: List[LLMProvider] = [p for p in all_providers if p.is_configured()]

        if not self._providers:
            raise ValueError(
                "Ни один AI-провайдер не настроен. Добавь хотя бы один ключ в .env:\n"
                "GEMINI_API_KEY, GROQ_API_KEY, CEREBRAS_API_KEY или OPENROUTER_API_KEY"
            )

        names = ", ".join(p.name for p in self._providers)
        logger.info(f"AIAnalyzer: {len(self._providers)} providers configured: {names}")

    async def check_relevance(
        self,
        article_text: str,
        user_topics: Sequence[UserTopic],
    ) -> RelevanceResult:
        """
        Проверяет релевантность статьи, перебирая провайдеров при ошибках.
        """
        if not user_topics:
            return RelevanceResult(is_relevant=False, matched_topic_id=None)

        prompt = _build_relevance_prompt(article_text, user_topics)

        for provider in self._providers:
            for attempt in range(2):  # 2 попытки на каждого провайдера
                try:
                    raw = await provider.generate(prompt)
                    result = _parse_relevance(raw, provider.name)
                    return result

                except (json.JSONDecodeError, KeyError, AttributeError) as e:
                    logger.warning(f"{provider.name}: JSON parse error: {e}")
                    break  # Не retry — ответ пришёл, но кривой

                except Exception as e:
                    error_str = str(e)
                    if "429" in error_str or "rate" in error_str.lower() or "quota" in error_str.lower():
                        if attempt == 0:
                            logger.warning(f"{provider.name}: rate limited, retry in 3s")
                            await asyncio.sleep(3)
                            continue
                        else:
                            logger.warning(f"{provider.name}: rate limited, switching provider")
                            break  # Переходим к следующему провайдеру
                    else:
                        logger.error(f"{provider.name}: error: {e}")
                        break

        # Все провайдеры упали
        logger.error("All AI providers failed")
        return RelevanceResult(is_relevant=False, matched_topic_id=None)

    async def update_user_preferences(
        self,
        user_id: int,
        interactions_data: Sequence[Tuple[str, bool, bool]],
    ) -> None:
        """Логирует статистику реакций (расширяемо)."""
        if len(interactions_data) < 10:
            return
        liked = sum(1 for _, l, _ in interactions_data if l)
        total = len(interactions_data)
        logger.info(f"User {user_id}: {liked}/{total} liked")

    @property
    def provider_names(self) -> List[str]:
        """Список подключённых провайдеров (для диагностики)."""
        return [p.name for p in self._providers]


__all__ = ["AIAnalyzer", "RelevanceResult"]
