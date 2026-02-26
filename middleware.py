"""
Middleware для внедрения зависимостей (db, analyzer) в хэндлеры.
"""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from brain import AIAnalyzer
from database import Database


class DependencyInjectionMiddleware(BaseMiddleware):
    """
    Middleware для автоматического внедрения db и analyzer в хэндлеры.
    """

    def __init__(self, db: Database, analyzer: AIAnalyzer) -> None:
        super().__init__()
        self.db = db
        self.analyzer = analyzer

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """
        Внедряет db и analyzer в data для доступа в хэндлерах.
        """
        data["db"] = self.db
        data["analyzer"] = self.analyzer
        return await handler(event, data)
