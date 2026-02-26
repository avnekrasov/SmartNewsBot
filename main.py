from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from brain import AIAnalyzer
from database import Database
from handlers import router as handlers_router, set_bot_commands
from middleware import DependencyInjectionMiddleware
from scheduler import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    db = Database()
    await db.init_db()

    # AI-анализатор: подхватывает все ключи из .env автоматически
    # Нужен хотя бы один: GEMINI_API_KEY, GROQ_API_KEY, CEREBRAS_API_KEY, OPENROUTER_API_KEY
    analyzer = AIAnalyzer(
        api_key=os.getenv("GEMINI_API_KEY"),
        groq_key=os.getenv("GROQ_API_KEY"),
        cerebras_key=os.getenv("CEREBRAS_API_KEY"),
        openrouter_key=os.getenv("OPENROUTER_API_KEY"),
    )

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())

    # DI через workflow_data (встроенный механизм aiogram 3)
    dp["db"] = db
    dp["analyzer"] = analyzer

    # Middleware как fallback
    dp.message.middleware(DependencyInjectionMiddleware(db, analyzer))
    dp.callback_query.middleware(DependencyInjectionMiddleware(db, analyzer))

    dp.include_router(handlers_router)

    # Установить команды бота (кнопка / в Telegram)
    await set_bot_commands(bot)
    logger.info("Bot commands registered")

    # Планировщик (время — МСК, UTC+3)
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    setup_scheduler(scheduler, bot=bot, db=db, analyzer=analyzer)
    scheduler.start()
    logger.info("Scheduler started (hourly check, MSK timezone)")

    logger.info("Bot is starting...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await db.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
