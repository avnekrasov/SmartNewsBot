from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import google.generativeai as genai

from database import UserTopic

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RelevanceResult:
    is_relevant: bool
    matched_topic_id: Optional[int]
    confidence: float = 0.0


def _extract_json(text: str) -> dict:
    """Извлечь первый JSON-объект из текста, игнорируя markdown-обёртки."""
    # Убираем markdown code blocks
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.strip()

    # Ищем первый JSON-объект
    match = re.search(r"\{[^{}]*\}", text)
    if match:
        return json.loads(match.group())
    raise json.JSONDecodeError("No JSON object found", text, 0)


class AIAnalyzer:
    """
    Семантический анализатор на Google Gemini.

    Отвечает за:
    1. Проверку релевантности статей пользовательским темам.
    2. Анализ предпочтений пользователя по его реакциям.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self._api_key:
            raise ValueError("GEMINI_API_KEY не установлен.")
        genai.configure(api_key=self._api_key)
        self._model = genai.GenerativeModel("gemini-2.0-flash")
        logger.info("AIAnalyzer: Gemini 2.0 Flash initialized")

    async def check_relevance(
        self,
        article_text: str,
        user_topics: Sequence[UserTopic],
    ) -> RelevanceResult:
        """
        Проверяет, относится ли статья к одной из тем пользователя.

        Отправляет текст статьи и темы в Gemini с промптом для семантического
        сопоставления. Учитывает сложные условия в описании тем.

        Returns:
            RelevanceResult с флагом релевантности и ID совпавшей темы.
        """
        if not user_topics:
            return RelevanceResult(is_relevant=False, matched_topic_id=None)

        topics_list = "\n".join(
            f"- ID {t.id}: {t.topic_description}" for t in user_topics
        )

        # Обрезаем текст до разумного размера (экономия токенов)
        truncated = article_text[:3000]

        prompt = (
            "Ты аналитик новостей. Определи, относится ли статья к одной из тем пользователя.\n"
            "Учитывай сложные условия, указанные в описании тем (временные рамки, конкретные этапы, пороги и т.д.).\n\n"
            f"Темы пользователя:\n{topics_list}\n\n"
            f"Текст статьи:\n{truncated}\n\n"
            'Ответь ТОЛЬКО JSON: {"is_relevant": true/false, "matched_topic_id": <id или null>, "confidence": <0.0-1.0>}'
        )

        # Retry с exponential backoff для rate limiting
        for attempt in range(3):
            try:
                response = await self._model.generate_content_async(prompt)
                result = _extract_json(response.text)
                return RelevanceResult(
                    is_relevant=bool(result.get("is_relevant", False)),
                    matched_topic_id=result.get("matched_topic_id"),
                    confidence=float(result.get("confidence", 0.0)),
                )
            except (json.JSONDecodeError, KeyError, AttributeError) as e:
                logger.warning(f"Gemini response parse error: {e}")
                return RelevanceResult(is_relevant=False, matched_topic_id=None)
            except Exception as e:
                if "429" in str(e) or "ResourceExhausted" in str(e):
                    wait = 2 ** attempt * 5  # 5, 10, 20 секунд
                    logger.warning(f"Rate limited, retry in {wait}s (attempt {attempt + 1}/3)")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Gemini API error: {e}")
                return RelevanceResult(is_relevant=False, matched_topic_id=None)

        logger.error("Gemini API: all retries exhausted")
        return RelevanceResult(is_relevant=False, matched_topic_id=None)

    async def update_user_preferences(
        self,
        user_id: int,
        interactions_data: Sequence[Tuple[str, bool, bool]],
    ) -> None:
        """
        Анализирует реакции пользователя для будущей адаптации фильтрации.

        Пока только логирует результат. В будущем можно:
        - сохранять веса тем в БД (таблица user_topic_weights)
        - корректировать confidence threshold
        - автоматически предлагать новые темы
        """
        if len(interactions_data) < 10:
            return

        liked = sum(1 for _, l, _ in interactions_data if l)
        clicked = sum(1 for _, _, c in interactions_data if c)
        total = len(interactions_data)

        logger.info(
            f"User {user_id} stats: {liked}/{total} liked, "
            f"{clicked}/{total} clicked"
        )


__all__ = ["AIAnalyzer", "RelevanceResult"]
