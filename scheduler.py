"""
Планировщик ежедневной рассылки новостей.

Джоба запускается каждый час и проверяет, пора ли слать конкретному пользователю
(сравнивая текущий час UTC с его send_time).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from brain import AIAnalyzer
from database import Database, User
from handlers import build_news_keyboard
from parser import ParsedArticle, parse_tg_channel, parse_website

logger = logging.getLogger(__name__)


async def _collect_articles(db: Database, user: User) -> List[ParsedArticle]:
    """Собрать статьи из всех источников пользователя."""
    articles: List[ParsedArticle] = []
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
            logger.warning(f"Source {source.source_url} failed: {e}")

    return articles


async def send_news_for_user(
    bot: Bot,
    db: Database,
    analyzer: AIAnalyzer,
    user: User,
) -> int:
    """
    Собрать, отфильтровать и отправить новости одному пользователю.

    Returns:
        Количество отправленных новостей.
    """
    topics = await db.list_topics(user.user_id)
    if not topics:
        return 0

    articles = await _collect_articles(db, user)
    if not articles:
        return 0

    _, news_limit = await db.get_user_settings(user.user_id)

    # Фильтрация через AI + дедупликация
    relevant: list[tuple[ParsedArticle, int | None, float]] = []
    for article in articles:
        # Пропускаем уже отправленные
        if await db.is_news_sent(user.user_id, article.id):
            continue

        result = await analyzer.check_relevance(
            article_text=article.text,
            user_topics=topics,
        )
        if result.is_relevant:
            relevant.append((article, result.matched_topic_id, result.confidence))

    if not relevant:
        return 0

    # Сортируем по confidence (desc) и берём top-N
    relevant.sort(key=lambda x: x[2], reverse=True)
    to_send = relevant[:news_limit]

    # Отправляем каждую новость отдельным сообщением с кнопками
    sent_count = 0
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
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to send news to {user.user_id}: {e}")

    return sent_count


async def _hourly_check(bot: Bot, db: Database, analyzer: AIAnalyzer) -> None:
    """
    Джоба, запускаемая каждый час.
    Проверяет, совпадает ли текущий час с send_time пользователя.
    """
    now = datetime.now(timezone.utc)
    current_hhmm = f"{now.hour:02d}:{now.minute // 30 * 30:02d}"  # Округляем до 30 мин

    users = await db.list_users()
    for user in users:
        # Проверяем, совпадает ли час
        try:
            user_hour = int(user.send_time.split(":")[0])
        except (ValueError, IndexError):
            continue

        if user_hour != now.hour:
            continue

        logger.info(f"Sending daily news to user {user.user_id}")
        try:
            count = await send_news_for_user(bot, db, analyzer, user)
            logger.info(f"User {user.user_id}: sent {count} news")
        except Exception as e:
            logger.error(f"Failed daily news for user {user.user_id}: {e}", exc_info=True)

    # Периодическая очистка старых записей
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
    """
    Настраивает ежечасную проверку для рассылки новостей.

    Каждый час джоба проверяет, совпадает ли текущий час UTC
    с send_time пользователя, и если да — отправляет подборку.
    """
    scheduler.add_job(
        _hourly_check,
        "cron",
        minute=0,  # Каждый час в :00
        kwargs={"bot": bot, "db": db, "analyzer": analyzer},
        id="hourly_news_check",
        replace_existing=True,
    )


__all__ = ["setup_scheduler", "send_news_for_user"]
