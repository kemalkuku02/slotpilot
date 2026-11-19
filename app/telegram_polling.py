import asyncio
import os

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

from app.main import _receive
from app.db.session import SessionLocal


async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")
    bot = Bot(token=token)
    dispatcher = Dispatcher()

    @dispatcher.message(F.text)
    async def on_text(message: Message):
        def process():
            with SessionLocal() as db:
                _, reply = _receive(db, "telegram", str(message.from_user.id), message.text)
                return reply
        reply = await asyncio.to_thread(process)
        await message.answer(reply)

    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
