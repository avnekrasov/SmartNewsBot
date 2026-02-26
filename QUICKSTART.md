# 🚀 Быстрый старт

## Минимальные шаги для запуска:

### 1. Создание виртуального окружения (опционально)
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**Если виртуальное окружение не нужно**, пропустите этот шаг.

### 2. Установка зависимостей
```powershell
pip install -r requirements.txt
```

### 3. Настройка .env
Откройте `.env` и укажите:
```env
BOT_TOKEN=ваш_токен_от_BotFather
GEMINI_API_KEY=ваш_ключ_от_Google_AI_Studio
```

### 4. Проверка настроек (опционально)
```powershell
python check_setup.py
```

### 5. Запуск бота
```powershell
python main.py
```

### 6. Тестирование
В Telegram отправьте боту: `/start`

---

## Где получить ключи:

- **BOT_TOKEN**: [@BotFather](https://t.me/BotFather) → `/newbot`
- **GEMINI_API_KEY**: [Google AI Studio](https://aistudio.google.com/app/apikey)

---

## Команды бота:

- `/start` - Регистрация
- `/add_source` - Добавить источник новостей
- `/add_topic` - Добавить тему для фильтрации
- `/my_subs` - Мои подписки

---

Подробная инструкция: см. [README.md](README.md)
