from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from brain import AIAnalyzer
from database import Database

logger = logging.getLogger(__name__)

router = Router()


class SourceType(str, Enum):
    WEBSITE = "website"
    TG_CHANNEL = "tg_channel"


class AddSourceStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_type = State()


class AddTopicStates(StatesGroup):
    waiting_for_description = State()


class SetTimeStates(StatesGroup):
    waiting_for_time = State()


class SetLimitStates(StatesGroup):
    waiting_for_limit = State()


class DeleteSourceStates(StatesGroup):
    waiting_for_id = State()


class DeleteTopicStates(StatesGroup):
    waiting_for_id = State()


class FetchNowStates(StatesGroup):
    confirming = State()


# ── Клавиатуры ────────────────────────────────────────────────

def _build_source_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌐 Сайт / RSS", callback_data="source_type:website"),
            InlineKeyboardButton(text="📢 TG-канал", callback_data="source_type:tg_channel"),
        ]
    ])


def build_news_keyboard(news_id: str, url: str) -> InlineKeyboardMarkup:
    """
    Клавиатура под новостью: лайк / дизлайк + ссылка «Читать».

    ВАЖНО: кнопка с url НЕ может иметь callback_data — это ограничение Telegram API.
    Поэтому «Читать» — URL-кнопка без callback, а лайк/дизлайк — callback-кнопки.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍", callback_data=f"news:{news_id}:like"),
            InlineKeyboardButton(text="👎", callback_data=f"news:{news_id}:dislike"),
            InlineKeyboardButton(text="🔗 Читать", url=url),
        ],
    ])


def build_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Источник", callback_data="menu:add_source"),
            InlineKeyboardButton(text="📝 Тема", callback_data="menu:add_topic"),
        ],
        [
            InlineKeyboardButton(text="📋 Мои подписки", callback_data="menu:my_subs"),
        ],
        [
            InlineKeyboardButton(text="⏰ Время", callback_data="menu:set_time"),
            InlineKeyboardButton(text="🔢 Лимит", callback_data="menu:set_limit"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить источник", callback_data="menu:del_source"),
            InlineKeyboardButton(text="🗑 Удалить тему", callback_data="menu:del_topic"),
        ],
        [
            InlineKeyboardButton(text="📰 Получить новости сейчас", callback_data="menu:fetch_now"),
        ],
    ])


# ── /start ────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, db: Database) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    await db.upsert_user(user_id=user_id)
    await message.answer(
        "Привет! Я SmartNewsBot — собираю новости из твоих источников "
        "и фильтрую по темам.\n\n"
        "Начни с добавления источника и темы.\n"
        "/help — полный список команд",
        reply_markup=build_main_menu(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("📱 Меню:", reply_markup=build_main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды:</b>\n"
        "/add_source — добавить источник (RSS/сайт/TG-канал)\n"
        "/add_topic — добавить тему фильтрации\n"
        "/my_subs — все подписки и настройки\n"
        "/set_time — время рассылки (ЧЧ:ММ UTC)\n"
        "/set_limit — кол-во новостей\n"
        "/del_source — удалить источник\n"
        "/del_topic — удалить тему\n"
        "/fetch_now — получить новости прямо сейчас\n"
        "/cancel — отменить текущее действие\n\n"
        "<b>Как работает:</b>\n"
        "1. Добавь RSS-ленты, сайты или TG-каналы\n"
        "2. Опиши интересующие темы\n"
        "3. Получай ежедневную подборку или жми «Получить сейчас»",
        reply_markup=build_main_menu(),
    )


# ── /cancel ───────────────────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=build_main_menu())


# ── /add_source ───────────────────────────────────────────────

@router.message(Command("add_source"))
async def cmd_add_source(message: Message, state: FSMContext) -> None:
    await state.set_state(AddSourceStates.waiting_for_url)
    await message.answer(
        "Отправь ссылку на источник.\n\n"
        "Поддерживается:\n"
        "• RSS-лента (например https://habr.com/ru/rss/all/)\n"
        "• Сайт (попробую найти RSS автоматически)\n"
        "• Публичный TG-канал (например https://t.me/durov)"
    )


@router.message(AddSourceStates.waiting_for_url)
async def add_source_get_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not url:
        await message.answer("Пожалуйста, отправь ссылку.")
        return

    # Автодетект типа по URL
    if "t.me/" in url or "telegram.me/" in url:
        await state.update_data(source_url=url, source_type=SourceType.TG_CHANNEL.value)
        await state.set_state(AddSourceStates.waiting_for_type)
        await message.answer(
            f"Обнаружена ссылка на Telegram. Сохранить как TG-канал?",
            reply_markup=_build_source_type_keyboard(),
        )
    else:
        await state.update_data(source_url=url, source_type=SourceType.WEBSITE.value)
        await state.set_state(AddSourceStates.waiting_for_type)
        await message.answer(
            "Выбери тип источника:",
            reply_markup=_build_source_type_keyboard(),
        )


@router.callback_query(F.data.startswith("source_type:"), AddSourceStates.waiting_for_type)
async def add_source_get_type(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not callback.data:
        return
    raw_type = callback.data.split(":", maxsplit=1)[1]
    source_type = SourceType(raw_type)

    data = await state.get_data()
    source_url = data.get("source_url", "")
    if not source_url:
        await callback.answer("Ошибка: ссылка потерялась. Попробуй /add_source заново.")
        await state.clear()
        return

    user_id = callback.from_user.id
    await db.add_source(user_id=user_id, source_url=source_url, source_type=source_type.value)
    await state.clear()

    type_label = "🌐 Сайт" if source_type == SourceType.WEBSITE else "📢 TG-канал"
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"Источник добавлен:\n{type_label} {source_url}",
        reply_markup=build_main_menu(),
    )
    await callback.answer()


# ── /add_topic ────────────────────────────────────────────────

@router.message(Command("add_topic"))
async def cmd_add_topic(message: Message, state: FSMContext) -> None:
    await state.set_state(AddTopicStates.waiting_for_description)
    await message.answer(
        "Опиши тему свободным текстом. Чем подробнее — тем точнее фильтрация.\n\n"
        "Примеры:\n"
        "• «Изменение ключевой ставки ЦБ РФ»\n"
        "• «Теннис, турниры Большого шлема, начиная с 1/4 финала»\n"
        "• «Новые релизы Python и обновления pip»"
    )


@router.message(AddTopicStates.waiting_for_description)
async def add_topic_save(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправь текстовое описание темы.")
        return
    if len(text) > 500:
        await message.answer("Слишком длинное описание (макс 500 символов). Сократи.")
        return

    user_id = message.from_user.id  # type: ignore[union-attr]
    topic_id = await db.add_topic(user_id=user_id, topic_description=text)
    await state.clear()
    await message.answer(
        f"Тема #{topic_id} сохранена:\n«{text}»",
        reply_markup=build_main_menu(),
    )


# ── /set_time ─────────────────────────────────────────────────

@router.message(Command("set_time"))
async def cmd_set_time(message: Message, state: FSMContext) -> None:
    await state.set_state(SetTimeStates.waiting_for_time)
    await message.answer("Укажи время рассылки (формат ЧЧ:ММ, UTC).\nНапример: 09:00 или 18:30")


@router.message(SetTimeStates.waiting_for_time)
async def set_time_save(message: Message, state: FSMContext, db: Database) -> None:
    text = (message.text or "").strip()
    try:
        parts = text.split(":")
        if len(parts) != 2:
            raise ValueError
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        time_str = f"{h:02d}:{m:02d}"
    except (ValueError, IndexError):
        await message.answer("Неверный формат. Используй ЧЧ:ММ (например 09:00).")
        return

    user_id = message.from_user.id  # type: ignore[union-attr]
    await db.update_send_time(user_id=user_id, send_time=time_str)
    await state.clear()
    await message.answer(f"Время рассылки: {time_str} UTC", reply_markup=build_main_menu())


# ── /set_limit ────────────────────────────────────────────────

@router.message(Command("set_limit"))
async def cmd_set_limit(message: Message, state: FSMContext) -> None:
    await state.set_state(SetLimitStates.waiting_for_limit)
    await message.answer("Сколько новостей присылать за раз? (1–50)")


@router.message(SetLimitStates.waiting_for_limit)
async def set_limit_save(message: Message, state: FSMContext, db: Database) -> None:
    try:
        limit = int((message.text or "").strip())
        if not 1 <= limit <= 50:
            raise ValueError
    except ValueError:
        await message.answer("Отправь число от 1 до 50.")
        return

    user_id = message.from_user.id  # type: ignore[union-attr]
    await db.update_news_limit(user_id=user_id, news_limit=limit)
    await state.clear()
    await message.answer(f"Лимит новостей: {limit}", reply_markup=build_main_menu())


# ── /del_source ───────────────────────────────────────────────

@router.message(Command("del_source"))
async def cmd_del_source(message: Message, state: FSMContext, db: Database) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    sources = await db.list_sources(user_id=user_id)
    if not sources:
        await message.answer("У тебя нет источников.", reply_markup=build_main_menu())
        return

    lines = ["Твои источники:\n"]
    for s in sources:
        emoji = "🌐" if s.source_type == "website" else "📢"
        lines.append(f"<b>#{s.id}</b> {emoji} {s.source_url}")
    lines.append("\nОтправь номер (#) источника для удаления.")

    await state.set_state(DeleteSourceStates.waiting_for_id)
    await message.answer("\n".join(lines))


@router.message(DeleteSourceStates.waiting_for_id)
async def del_source_exec(message: Message, state: FSMContext, db: Database) -> None:
    try:
        source_id = int((message.text or "").strip().lstrip("#"))
    except ValueError:
        await message.answer("Отправь числовой ID источника.")
        return

    user_id = message.from_user.id  # type: ignore[union-attr]
    deleted = await db.delete_source(source_id, user_id)
    await state.clear()
    if deleted:
        await message.answer(f"Источник #{source_id} удалён.", reply_markup=build_main_menu())
    else:
        await message.answer("Источник не найден.", reply_markup=build_main_menu())


# ── /del_topic ────────────────────────────────────────────────

@router.message(Command("del_topic"))
async def cmd_del_topic(message: Message, state: FSMContext, db: Database) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    topics = await db.list_topics(user_id=user_id)
    if not topics:
        await message.answer("У тебя нет тем.", reply_markup=build_main_menu())
        return

    lines = ["Твои темы:\n"]
    for t in topics:
        lines.append(f"<b>#{t.id}</b> {t.topic_description}")
    lines.append("\nОтправь номер (#) темы для удаления.")

    await state.set_state(DeleteTopicStates.waiting_for_id)
    await message.answer("\n".join(lines))


@router.message(DeleteTopicStates.waiting_for_id)
async def del_topic_exec(message: Message, state: FSMContext, db: Database) -> None:
    try:
        topic_id = int((message.text or "").strip().lstrip("#"))
    except ValueError:
        await message.answer("Отправь числовой ID темы.")
        return

    user_id = message.from_user.id  # type: ignore[union-attr]
    deleted = await db.delete_topic(topic_id, user_id)
    await state.clear()
    if deleted:
        await message.answer(f"Тема #{topic_id} удалена.", reply_markup=build_main_menu())
    else:
        await message.answer("Тема не найдена.", reply_markup=build_main_menu())


# ── /my_subs ──────────────────────────────────────────────────

@router.message(Command("my_subs"))
async def cmd_my_subs(message: Message, db: Database) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    sources = await db.list_sources(user_id=user_id)
    topics = await db.list_topics(user_id=user_id)
    send_time, news_limit = await db.get_user_settings(user_id)

    lines = ["<b>📋 Подписки и настройки</b>\n"]
    lines.append(f"⏰ Рассылка: {send_time} UTC | 🔢 Лимит: {news_limit}\n")

    lines.append("<b>Источники:</b>")
    if sources:
        for s in sources:
            emoji = "🌐" if s.source_type == "website" else "📢"
            lines.append(f"  #{s.id} {emoji} {s.source_url}")
    else:
        lines.append("  — нет (/add_source)")

    lines.append("\n<b>Темы:</b>")
    if topics:
        for t in topics:
            lines.append(f"  #{t.id} {t.topic_description}")
    else:
        lines.append("  — нет (/add_topic)")

    await message.answer("\n".join(lines), reply_markup=build_main_menu())


# ── /fetch_now — получить новости прямо сейчас ────────────────

@router.message(Command("fetch_now"))
async def cmd_fetch_now(message: Message, db: Database, analyzer: AIAnalyzer) -> None:
    """Запустить сбор и отправку новостей вручную, не дожидаясь расписания."""
    from scheduler import send_news_for_user
    from parser import parse_website, parse_tg_channel

    user_id = message.from_user.id  # type: ignore[union-attr]
    user = await db.get_user(user_id)
    if not user:
        await message.answer("Сначала выполни /start.")
        return

    topics = await db.list_topics(user_id=user_id)
    sources = await db.list_sources(user_id=user_id)
    if not topics:
        await message.answer("Добавь хотя бы одну тему (/add_topic).", reply_markup=build_main_menu())
        return
    if not sources:
        await message.answer("Добавь хотя бы один источник (/add_source).", reply_markup=build_main_menu())
        return

    await message.answer("⏳ Собираю новости из твоих источников...")

    try:
        count = await send_news_for_user(
            bot=message.bot,  # type: ignore[arg-type]
            db=db,
            analyzer=analyzer,
            user=user,
        )
        if count == 0:
            await message.answer(
                "Не нашёл подходящих новостей. Возможные причины:\n"
                "• Источники пока не отдают статьи (проверь URL)\n"
                "• Ни одна статья не прошла фильтр по твоим темам\n"
                "• Все новости уже были отправлены ранее",
                reply_markup=build_main_menu(),
            )
        else:
            await message.answer(
                f"Готово! Отправлено {count} новостей.",
                reply_markup=build_main_menu(),
            )
    except Exception as e:
        logger.error(f"fetch_now error for user {user_id}: {e}", exc_info=True)
        await message.answer(f"Ошибка при сборе новостей: {e}", reply_markup=build_main_menu())


# ── Menu callback handler ────────────────────────────────────

@router.callback_query(F.data.startswith("menu:"))
async def menu_callbacks(callback: CallbackQuery, state: FSMContext, db: Database, analyzer: AIAnalyzer) -> None:
    if not callback.data:
        await callback.answer()
        return

    action = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    if action == "add_source":
        await state.set_state(AddSourceStates.waiting_for_url)
        await callback.message.edit_text(  # type: ignore[union-attr]
            "Отправь ссылку на источник (RSS / сайт / TG-канал)."
        )
        await callback.answer()

    elif action == "add_topic":
        await state.set_state(AddTopicStates.waiting_for_description)
        await callback.message.edit_text(  # type: ignore[union-attr]
            "Опиши тему свободным текстом.\n"
            "Примеры:\n"
            "• «Изменение ключевой ставки ЦБ РФ»\n"
            "• «Релизы Python»"
        )
        await callback.answer()

    elif action == "my_subs":
        sources = await db.list_sources(user_id=user_id)
        topics = await db.list_topics(user_id=user_id)
        send_time, news_limit = await db.get_user_settings(user_id)

        lines = ["<b>📋 Подписки</b>\n"]
        lines.append(f"⏰ {send_time} UTC | 🔢 {news_limit} новостей\n")
        if sources:
            lines.append("<b>Источники:</b>")
            for s in sources:
                emoji = "🌐" if s.source_type == "website" else "📢"
                lines.append(f"  #{s.id} {emoji} {s.source_url}")
        else:
            lines.append("Источники: нет")
        if topics:
            lines.append("\n<b>Темы:</b>")
            for t in topics:
                lines.append(f"  #{t.id} {t.topic_description}")
        else:
            lines.append("\nТемы: нет")

        await callback.message.edit_text("\n".join(lines), reply_markup=build_main_menu())  # type: ignore[union-attr]
        await callback.answer()

    elif action == "set_time":
        await state.set_state(SetTimeStates.waiting_for_time)
        await callback.message.edit_text("Укажи время рассылки (ЧЧ:ММ UTC):")  # type: ignore[union-attr]
        await callback.answer()

    elif action == "set_limit":
        await state.set_state(SetLimitStates.waiting_for_limit)
        await callback.message.edit_text("Сколько новостей присылать? (1–50)")  # type: ignore[union-attr]
        await callback.answer()

    elif action == "del_source":
        sources = await db.list_sources(user_id=user_id)
        if not sources:
            await callback.message.edit_text("Нет источников.", reply_markup=build_main_menu())  # type: ignore[union-attr]
            await callback.answer()
            return
        lines = ["Источники:\n"]
        for s in sources:
            emoji = "🌐" if s.source_type == "website" else "📢"
            lines.append(f"<b>#{s.id}</b> {emoji} {s.source_url}")
        lines.append("\nОтправь # для удаления.")
        await state.set_state(DeleteSourceStates.waiting_for_id)
        await callback.message.edit_text("\n".join(lines))  # type: ignore[union-attr]
        await callback.answer()

    elif action == "del_topic":
        topics = await db.list_topics(user_id=user_id)
        if not topics:
            await callback.message.edit_text("Нет тем.", reply_markup=build_main_menu())  # type: ignore[union-attr]
            await callback.answer()
            return
        lines = ["Темы:\n"]
        for t in topics:
            lines.append(f"<b>#{t.id}</b> {t.topic_description}")
        lines.append("\nОтправь # для удаления.")
        await state.set_state(DeleteTopicStates.waiting_for_id)
        await callback.message.edit_text("\n".join(lines))  # type: ignore[union-attr]
        await callback.answer()

    elif action == "fetch_now":
        await callback.answer("Собираю новости...")
        # Создаём фейковое сообщение через бот
        await callback.message.edit_text("⏳ Собираю новости...")  # type: ignore[union-attr]

        from scheduler import send_news_for_user

        user = await db.get_user(user_id)
        if not user:
            await callback.message.edit_text("Сначала /start.", reply_markup=build_main_menu())  # type: ignore[union-attr]
            return

        topics = await db.list_topics(user_id=user_id)
        sources = await db.list_sources(user_id=user_id)
        if not topics or not sources:
            await callback.message.edit_text(  # type: ignore[union-attr]
                "Добавь источники и темы.",
                reply_markup=build_main_menu(),
            )
            return

        try:
            count = await send_news_for_user(
                bot=callback.bot,  # type: ignore[arg-type]
                db=db,
                analyzer=analyzer,
                user=user,
            )
            if count == 0:
                await callback.message.edit_text(  # type: ignore[union-attr]
                    "Подходящих новостей не найдено.",
                    reply_markup=build_main_menu(),
                )
            else:
                await callback.message.edit_text(  # type: ignore[union-attr]
                    f"Отправлено {count} новостей!",
                    reply_markup=build_main_menu(),
                )
        except Exception as e:
            logger.error(f"fetch_now callback error: {e}", exc_info=True)
            await callback.message.edit_text(  # type: ignore[union-attr]
                f"Ошибка: {e}",
                reply_markup=build_main_menu(),
            )


# ── Оценки под новостями ──────────────────────────────────────

@router.callback_query(F.data.startswith("news:"))
async def news_callbacks(callback: CallbackQuery, db: Database, analyzer: AIAnalyzer) -> None:
    if not callback.data:
        await callback.answer()
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные.")
        return

    _, news_id, action = parts
    if action not in ("like", "dislike"):
        await callback.answer()
        return

    is_liked = action == "like"
    await db.log_interaction(
        user_id=callback.from_user.id,
        news_id=news_id,
        is_liked=is_liked,
        is_clicked=False,
    )

    if is_liked:
        await callback.answer("Спасибо! 👍")
    else:
        await callback.answer("Учту 👎")


__all__ = ["router", "build_news_keyboard"]
