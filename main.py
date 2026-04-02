import asyncio
import logging
import random
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, functions
from telethon.errors import (
    FloodWaitError, ChatWriteForbiddenError,
    UserAlreadyParticipantError, ChannelPrivateError, InviteHashExpiredError,
)
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
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

# channel username -> discussion group id
discussion_groups: dict[str, int] = {}


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


async def join_channels(client: TelegramClient, channels: list[str]) -> list[str]:
    """Join channels and their discussion groups. Returns list of active channels."""
    active_channels = []

    for channel in channels:
        try:
            # Join the channel
            try:
                await client(JoinChannelRequest(channel))
                log.info("Вступил в канал %s", channel)
            except UserAlreadyParticipantError:
                log.info("Уже в канале %s", channel)
            except (ChannelPrivateError, InviteHashExpiredError):
                log.warning("Канал %s приватный, пропускаю", channel)
                continue

            # Get linked discussion group
            entity = await client.get_entity(channel)
            full = await client(GetFullChannelRequest(entity))
            linked_chat_id = full.full_chat.linked_chat_id

            if not linked_chat_id:
                log.warning("Канал %s не имеет комментариев, пропускаю", channel)
                continue

            # Join discussion group
            try:
                discussion_entity = await client.get_entity(linked_chat_id)
                await client(JoinChannelRequest(discussion_entity))
                log.info("Вступил в группу обсуждения канала %s (id=%d)", channel, linked_chat_id)
            except UserAlreadyParticipantError:
                log.info("Уже в группе обсуждения канала %s", channel)

            discussion_groups[channel] = linked_chat_id
            active_channels.append(channel)

        except FloodWaitError as e:
            log.warning("FloodWait при вступлении: ждём %d сек", e.seconds)
            await asyncio.sleep(e.seconds)
            continue
        except Exception as e:
            log.error("Ошибка при вступлении в %s: %s", channel, e)
            continue

        # Delay between joins
        delay = random.randint(5, 10)
        log.info("Задержка %d сек перед следующим каналом...", delay)
        await asyncio.sleep(delay)

    return active_channels


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

    # Join channels and discover discussion groups
    active_channels = await join_channels(client, channels)
    if not active_channels:
        log.error("Нет активных каналов с комментариями")
        return

    log.info("Активные каналы: %s", ", ".join(active_channels))

    @client.on(events.NewMessage(chats=active_channels))
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

        # Find the discussion group for this channel
        channel_username = None
        for ch, group_id in discussion_groups.items():
            ch_entity = await client.get_entity(ch)
            if ch_entity.id == event.chat_id:
                channel_username = ch
                break

        if not channel_username:
            log.warning("Не найдена группа обсуждения для канала %s", chat_title)
            return

        discussion_group_id = discussion_groups[channel_username]

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
                discussion_group_id,
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

    log.info("Бот запущен. Мониторинг каналов: %s", ", ".join(active_channels))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
