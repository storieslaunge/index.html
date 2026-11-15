import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

dispatcher = Dispatcher()


@dispatcher.message(CommandStart())
async def start(message: Message) -> None:
    if not WEBAPP_URL.startswith("https://"):
        await message.answer("Привет! Ссылка Mini App не настроена. Укажите WEBAPP_URL с HTTPS в .env.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть меню и бронь", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    is_admin = message.from_user is not None and message.from_user.id == ADMIN_TELEGRAM_ID and ADMIN_TELEGRAM_ID != 0
    greeting = "Добро пожаловать в лаунж-бар!"
    if is_admin:
        greeting += " Вам доступна панель администратора внутри приложения."
    await message.answer(greeting + " Выберите блюдо, оформите доставку или забронируйте столик.", reply_markup=keyboard)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN в .env перед запуском бота")
    if not WEBAPP_URL:
        raise RuntimeError("Укажите WEBAPP_URL в .env перед запуском бота")
    bot = Bot(token=BOT_TOKEN)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
