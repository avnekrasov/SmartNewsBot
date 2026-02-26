"""
Планировщик ежедневной рассылки новостей.

Джоба запускается каждый час и проверяет, пора ли слать конкретному пользователю
(сравнивая текущий час МСК с его send_time).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from brain import AIAnalyzer
from database import Database, User
from handlers import build_news_keyboard
from parser import ParsedArticle, parse_tg_channel, parse_website

logger = logging.getLogger(__name__)

# Московское время UTC+3
MSK = timezone(timedelta(hours=3))


@dataclass
class FetchResult:
    """Результат сбора новостей для диагностики."""
    total_parsed: int = 0
    already_sent: int = 0
    ai_checked: int = 0
    ai_relevant: int = 0
    ai_failed: int = 0
    sent: int = 0
    source_errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.source_errors is None:
            self.source_errors = []

    @property
    def diagnostics(self) -> str:
        lines = []
        lines.append(f"📊 Собрано статей: {self.total_parsed}")
        if self.source_errors:
            lines.append(f"⚠️ Ошибки источников: {len(self.source_errors)}")
        if self.already_sent:
            lines.append(f"🔄 Уже отправлялись: {self.already_sent}")
        if self.ai_checked:
            lines.append(f"🤖 Проверено AI: {self.ai_checked}")
            lines.append(f"✅ Релевантных: {self.ai_relevant}")
        if self.ai_failed:
            lines.append(f"❌ AI ошибки (квота?): {self.ai_failed}")
        lines.append(f"📨 Отправлено: {self.sent}")
        return "\n".join(lines)


async def _collect_articles(db: Database, user: User) -> tuple[List[ParsedArticle], list[str]]:
    """Собрать статьи из всех источников. Вернуть (статьи, ошибки)."""
    articles: List[ParsedArticle] = []
    errors: list[str] = []
    sources = await db.list_sources(user_id=user.user_id)

    for source in sources:
        try:
            if source.source_type == "website":
                new = await parse_website(source.source_url)
            elif source.source_type == "tg_channel":
                new = await parse_tg_channel(source.source_url)
            else:
                continue
            articles.extend(new)
        except Exception as e:
            err = f"{source.source_url}: {e}"
            logger.warning(f"Source failed: {err}")
            errors.append(err)

    return articles, errors


async def send_news_for_user(
    bot: Bot,
    db: Database,
    analyzer: AIAnalyzer,
    user: User,
) -> FetchResult:
    """
    Собрать, отфильтровать и отправить новости одному пользователю.

    Если AI недоступен (все проверки упали) — отправляет без фильтрации.

    Returns:
        FetchResult с полной диагностикой.
    """
    result = FetchResult()

    topics = await db.list_topics(user.user_id)
    if not topics:
        return result

    articles, errors = await _collect_articles(db, user)
    result.source_errors = errors
    result.total_parsed = len(articles)

    if not articles:
        return result

    _, news_limit = await db.get_user_settings(user.user_id)

    # Дедупликация
    new_articles: list[ParsedArticle] = []
    for article in articles:
        if await db.is_news_sent(user.user_id, article.id):
            result.already_sent += 1
        else:
            new_articles.append(article)

    if not new_articles:
        return result

    # Фильтрация через AI
    relevant: list[tuple[ParsedArticle, int | None, float]] = []
    for article in new_articles:
        relevance = await analyzer.check_relevance(
            article_text=article.text,
            user_topics=topics,
        )
        result.ai_checked += 1

        if relevance.is_relevant:
            result.ai_relevant += 1
            relevant.append((article, relevance.matched_topic_id, relevance.confidence))
        elif relevance.confidence == 0.0 and not relevance.is_relevant:
            # confidence=0 + not relevant = вероятно AI упал (квота/ошибка)
            result.ai_failed += 1

    # Если AI полностью недоступен — шлём без фильтрации (первые N)
    to_send: list[tuple[ParsedArticle, int | None, float]]
    if relevant:
        relevant.sort(key=lambda x: x[2], reverse=True)
        to_send = relevant[:news_limit]
    elif result.ai_failed == result.ai_checked and result.ai_checked > 0:
        # AI лёг на всех запросах — шлём без фильтра
        logger.warning(f"AI unavailable for user {user.user_id}, sending unfiltered")
        to_send = [(a, None, 0.0) for a in new_articles[:news_limit]]
    else:
        return result

    # Отправка
    for article, topic_id, confidence in to_send:
        text = f"<b>{article.title}</b>\n\n"
        if article.text != article.title:
            preview = article.text[:400]
            if len(article.text) > 400:
                preview += "..."
            text += f"{preview}\n"

        try:
            await bot.send_message(
                chat_id=user.user_id,
                text=text,
                reply_markup=build_news_keyboard(news_id=article.id, url=article.url),
                disable_web_page_preview=True,
            )
            await db.mark_news_sent(user.user_id, article.id)
            result.sent += 1
        except Exception as e:
            logger.error(f"Failed to send news to {user.user_id}: {e}")

    return result


async def _hourly_check(bot: Bot, db: Database, analyzer: AIAnalyzer) -> None:
    """
    Джоба, запускаемая каждый час.
    Проверяет, совпадает ли текущий час МСК с send_time пользователя.
    """
    now = datetime.now(MSK)

    users = await db.list_users()
    for user in users:
        try:
            user_hour = int(user.send_time.split(":")[0])
        except (ValueError, IndexError):
            continue

        if user_hour != now.hour:
            continue

        logger.info(f"Sending daily news to user {user.user_id}")
        try:
            fetch_result = await send_news_for_user(bot, db, analyzer, user)
            logger.info(f"User {user.user_id}: {fetch_result.diagnostics}")
        except Exception as e:
            logger.error(f"Failed daily news for user {user.user_id}: {e}", exc_info=True)

    # Периодическая очистка
    try:
        await db.cleanup_old_sent_news(days=30)
    except Exception:
        pass


def setup_scheduler(
    scheduler: AsyncIOScheduler,
    *,
    bot: Bot,
    db: Database,
    analyzer: AIAnalyzer,
) -> None:
    """Ежечасная проверка для рассылки (по московскому времени)."""
    scheduler.add_job(
        _hourly_check,
        "cron",
        minute=0,
        kwargs={"bot": bot, "db": db, "analyzer": analyzer},
        id="hourly_news_check",
        replace_existing=True,
    )


__all__ = ["setup_scheduler", "send_news_for_user", "FetchResult"]
