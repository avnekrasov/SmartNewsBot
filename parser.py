"""
Модуль парсинга новостей из различных источников.

Поддерживает:
- RSS/Atom-ленты (feedparser)
- HTML-страницы с автообнаружением RSS (BeautifulSoup)
- Публичные Telegram-каналы (через t.me/s/ preview)
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Общий HTTP-клиент с разумными таймаутами
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_HEADERS = {
    "User-Agent": "SmartNewsBot/1.0 (RSS reader; +https://github.com/smartnewsbot)"
}


@dataclass(slots=True)
class ParsedArticle:
    """Унифицированное представление новости."""

    id: str
    title: str
    url: str
    text: str
    published_at: Optional[datetime] = None


def _make_id(url: str, title: str) -> str:
    """Детерминированный ID статьи на основе URL и заголовка."""
    raw = f"{url}|{title}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _clean_html(html: str) -> str:
    """Убрать HTML-теги, оставив чистый текст."""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def _parse_date(entry: dict) -> Optional[datetime]:
    """Попытаться распарсить дату из RSS entry."""
    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if raw:
            try:
                return parsedate_to_datetime(raw)
            except (ValueError, TypeError):
                pass
        # feedparser парсит даты в *_parsed
        parsed = entry.get(f"{field}_parsed")
        if parsed:
            try:
                return datetime(*parsed[:6])
            except (ValueError, TypeError):
                pass
    return None


# ── RSS / Atom ────────────────────────────────────────────────

async def _fetch_feed(url: str) -> List[ParsedArticle]:
    """Скачать и распарсить RSS/Atom-ленту."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.text

    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        logger.warning(f"Feed parse error for {url}: {feed.bozo_exception}")
        return []

    articles: List[ParsedArticle] = []
    for entry in feed.entries[:30]:  # Ограничиваем — свежих обычно хватает
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue

        # Текст: summary > content > description
        text = ""
        if entry.get("content"):
            text = entry["content"][0].get("value", "")
        elif entry.get("summary"):
            text = entry["summary"]
        elif entry.get("description"):
            text = entry["description"]

        text = _clean_html(text)
        if not text:
            text = title

        articles.append(ParsedArticle(
            id=_make_id(link, title),
            title=title,
            url=link,
            text=text[:5000],
            published_at=_parse_date(entry),
        ))

    logger.info(f"RSS {url}: {len(articles)} articles")
    return articles


# ── Автообнаружение RSS на HTML-странице ──────────────────────

async def _discover_rss(url: str) -> Optional[str]:
    """Ищет <link rel=alternate type=application/rss+xml> на странице."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

        for link_tag in soup.find_all("link", rel="alternate"):
            link_type = (link_tag.get("type") or "").lower()
            if "rss" in link_type or "atom" in link_type:
                href = link_tag.get("href", "")
                if href:
                    return urljoin(url, href)
    except Exception as e:
        logger.debug(f"RSS discovery failed for {url}: {e}")
    return None


# ── HTML-fallback парсинг ─────────────────────────────────────

async def _parse_html_page(url: str) -> List[ParsedArticle]:
    """
    Fallback: если RSS не найден, пытаемся извлечь ссылки на статьи со страницы.
    Ищем <a> внутри <article>, <h2>, <h3> или элементов с классами типа 'post', 'article'.
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.warning(f"HTML fetch failed for {url}: {e}")
        return []

    articles: List[ParsedArticle] = []
    seen_urls: set[str] = set()

    # Стратегия: ищем ссылки в заголовках и article-элементах
    selectors = [
        "article a",
        "h2 a", "h3 a",
        ".post a", ".article a", ".news a",
        "[class*='item'] a", "[class*='card'] a",
    ]

    for selector in selectors:
        for a_tag in soup.select(selector):
            href = a_tag.get("href", "").strip()
            title = a_tag.get_text(strip=True)
            if not href or not title or len(title) < 10:
                continue

            full_url = urljoin(url, href)
            # Фильтруем: только того же домена, не дубли
            if urlparse(full_url).netloc != urlparse(url).netloc:
                continue
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            articles.append(ParsedArticle(
                id=_make_id(full_url, title),
                title=title,
                url=full_url,
                text=title,  # Полный текст можно дозагрузить позже
            ))

        if len(articles) >= 20:
            break

    logger.info(f"HTML {url}: {len(articles)} links extracted")
    return articles[:20]


# ── Telegram channels ─────────────────────────────────────────

def _normalize_tg_url(url: str) -> str:
    """
    Преобразует URL канала в формат preview: https://t.me/s/channel_name
    Принимает: t.me/channel, @channel, https://t.me/channel
    """
    url = url.strip().rstrip("/")

    # @channel_name
    if url.startswith("@"):
        return f"https://t.me/s/{url[1:]}"

    # Извлекаем имя канала из URL
    match = re.search(r"t\.me/(?:s/)?(\w+)", url)
    if match:
        return f"https://t.me/s/{match.group(1)}"

    return url


async def parse_tg_channel(url: str) -> List[ParsedArticle]:
    """
    Парсит публичный Telegram-канал через web preview (t.me/s/).

    Не требует Telegram API ключей — работает через обычный HTTP.
    Ограничение: доступны только ~20 последних постов.
    """
    preview_url = _normalize_tg_url(url)

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(preview_url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.warning(f"TG channel fetch failed for {url}: {e}")
        return []

    articles: List[ParsedArticle] = []

    # t.me/s/ рендерит посты в div.tgme_widget_message_wrap
    for widget in soup.select(".tgme_widget_message_wrap"):
        msg_div = widget.select_one(".tgme_widget_message")
        if not msg_div:
            continue

        # Текст поста
        text_div = msg_div.select_one(".tgme_widget_message_text")
        text = text_div.get_text(separator=" ", strip=True) if text_div else ""
        if not text or len(text) < 15:
            continue

        # Ссылка на пост
        link_el = msg_div.get("data-post", "")
        if link_el:
            post_url = f"https://t.me/{link_el}"
        else:
            continue

        # Дата
        date_el = msg_div.select_one("time")
        pub_date = None
        if date_el and date_el.get("datetime"):
            try:
                pub_date = datetime.fromisoformat(date_el["datetime"].replace("Z", "+00:00"))
            except ValueError:
                pass

        # Заголовок = первые ~100 символов текста
        title = text[:100].split("\n")[0]
        if len(title) > 80:
            title = title[:77] + "..."

        articles.append(ParsedArticle(
            id=_make_id(post_url, title),
            title=title,
            url=post_url,
            text=text[:5000],
            published_at=pub_date,
        ))

    logger.info(f"TG {url}: {len(articles)} posts")
    return articles


# ── Главная точка входа для сайтов ────────────────────────────

async def parse_website(url: str) -> List[ParsedArticle]:
    """
    Парсит сайт. Стратегия:
    1. Пробуем как RSS/Atom напрямую
    2. Если не RSS — ищем RSS-ссылку на странице
    3. Fallback: извлекаем ссылки на статьи из HTML
    """
    # Шаг 1: пробуем как RSS напрямую
    try:
        articles = await _fetch_feed(url)
        if articles:
            return articles
    except Exception:
        pass

    # Шаг 2: ищем RSS на странице
    rss_url = await _discover_rss(url)
    if rss_url:
        try:
            articles = await _fetch_feed(rss_url)
            if articles:
                return articles
        except Exception as e:
            logger.warning(f"Discovered RSS {rss_url} failed: {e}")

    # Шаг 3: fallback HTML
    return await _parse_html_page(url)


__all__ = ["ParsedArticle", "parse_website", "parse_tg_channel"]
