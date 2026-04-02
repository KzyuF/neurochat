import asyncio
import logging
import random
from datetime import datetime, time
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, ChatWriteForbiddenError
from groq import Groq

from config import (
    API_ID, API_HASH, GROQ_API_KEY, GROQ_MODEL,
    MIN_DELAY, MAX_DELAY, MAX_COMMENTS_PER_DAY, SYSTEM_PROMPT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)

comments_today = 0
last_reset_date = datetime.now().date()


def load_channels() -> list[str]:
    path = Path(__file__).parent / "channels.txt"
    if not path.exists():
        log.warning("channels.txt не найден")
        return []
    channels = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            channels.append(line)
    log.info("Загружено каналов: %d", len(channels))
    return channels


def generate_comment(post_text: str) -> str:
    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Напиши комментарий к этому посту:\n\n{post_text}"},
        ],
        max_tokens=200,
        temperature=0.9,
    )
    return response.choices[0].message.content


def reset_daily_counter():
    global comments_today, last_reset_date
    today = datetime.now().date()
    if today != last_reset_date:
        log.info("Новый день — сброс счётчика комментариев (было %d)", comments_today)
        comments_today = 0
        last_reset_date = today


async def main():
    global comments_today

    channels = load_channels()
    if not channels:
        log.error("Список каналов пуст. Добавьте каналы в channels.txt")
        return

    client = TelegramClient("neurochat_session", API_ID, API_HASH)
    await client.start()
    log.info("Клиент Telegram запущен")

    @client.on(events.NewMessage(chats=channels))
    async def on_new_post(event):
        global comments_today

        reset_daily_counter()

        if comments_today >= MAX_COMMENTS_PER_DAY:
            log.info("Достигнут лимит комментариев за день (%d)", MAX_COMMENTS_PER_DAY)
            return

        post_text = event.message.text
        if not post_text:
            return

        chat = await event.get_chat()
        chat_title = getattr(chat, "title", str(chat.id))
        log.info("Новый пост в [%s]: %s", chat_title, post_text[:80])

        try:
            comment = generate_comment(post_text)
            log.info("Сгенерирован комментарий: %s", comment[:80])
        except Exception as e:
            log.error("Ошибка генерации комментария: %s", e)
            return

        delay = random.randint(MIN_DELAY, MAX_DELAY)
        log.info("Ожидание %d сек перед отправкой...", delay)
        await asyncio.sleep(delay)

        reset_daily_counter()
        if comments_today >= MAX_COMMENTS_PER_DAY:
            log.info("Лимит комментариев достигнут после ожидания")
            return

        try:
            await client.send_message(
                event.chat_id,
                comment,
                comment_to=event.message.id,
            )
            comments_today += 1
            log.info(
                "Комментарий опубликован в [%s] (%d/%d за день)",
                chat_title, comments_today, MAX_COMMENTS_PER_DAY,
            )
        except FloodWaitError as e:
            log.warning("FloodWait: ждём %d сек", e.seconds)
            await asyncio.sleep(e.seconds)
        except ChatWriteForbiddenError:
            log.warning("Нет прав на комментарии в [%s], пропускаем", chat_title)
        except Exception as e:
            log.error("Ошибка отправки комментария: %s", e)

    log.info("Бот запущен. Мониторинг каналов: %s", ", ".join(channels))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
