import asyncio
import json
import logging
import random
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, functions, utils
from telethon.errors import (
    FloodWaitError, ChatWriteForbiddenError,
    UserAlreadyParticipantError, ChannelPrivateError, InviteHashExpiredError,
)
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest, GetParticipantRequest
from groq import Groq
import httpx

from config import (
    API_ID, API_HASH, GROQ_API_KEY, GROQ_MODEL,
    MIN_DELAY, MAX_DELAY, MAX_COMMENTS_PER_DAY, SYSTEM_PROMPT, SKIP_CHANCE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

groq_client = Groq(api_key=GROQ_API_KEY)

BOT_TOKEN = "8398181888:AAGRkEhnJv1AcFFyiUBtnKTMN04pB0eLJwo"
ADMIN_ID = 706575799

comments_today = 0
last_reset_date = datetime.now().date()

# channel entity id -> (channel username, discussion group id)
channel_map: dict[int, tuple[str, int]] = {}

STATS_FILE = Path(__file__).parent / "stats.json"
STATUS_FILE = Path(__file__).parent / "channel_status.json"


def load_stats() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            pass
    return {"today": "", "today_count": 0, "total_count": 0, "last_comment": ""}


def save_stats(stats: dict):
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def load_channel_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            pass
    return {}


def save_channel_status(status: dict):
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def channel_key(username: str) -> str:
    """Normalize @username to bare name for status dict key."""
    return username.strip().lstrip("@")


async def check_membership(client: TelegramClient, linked_chat_id: int) -> str:
    """Check if we are a participant of the discussion group. Returns 'joined' or 'pending'."""
    try:
        me = await client.get_me()
        await client(GetParticipantRequest(channel=linked_chat_id, participant=me))
        return "joined"
    except Exception as e:
        if "USER_NOT_PARTICIPANT" in str(e):
            return "pending"
        return "pending"


async def notify_admin(text: str):
    try:
        async with httpx.AsyncClient() as http:
            await http.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception as e:
        log.error("Ошибка отправки уведомления админу: %s", e)


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


async def join_channels(client: TelegramClient, channels: list[str]) -> list:
    """Join channels and their discussion groups. Returns list of channel entities."""
    channel_entities = []
    status = load_channel_status()

    for channel in channels:
        key = channel_key(channel)
        try:
            # Join the channel
            try:
                await client(JoinChannelRequest(channel))
                log.info("Вступил в канал %s", channel)
            except UserAlreadyParticipantError:
                log.info("Уже в канале %s", channel)
            except (ChannelPrivateError, InviteHashExpiredError):
                log.warning("Канал %s приватный, пропускаю", channel)
                status[key] = "error"
                save_channel_status(status)
                continue

            # Get entity and linked discussion group
            entity = await client.get_entity(channel)
            full = await client(GetFullChannelRequest(entity))
            linked_chat_id = full.full_chat.linked_chat_id

            if not linked_chat_id:
                log.warning("Канал %s не имеет комментариев, пропускаю", channel)
                status[key] = "error"
                save_channel_status(status)
                continue

            # Join discussion group via GetDiscussionMessageRequest + explicit join
            try:
                messages = await client(functions.messages.GetHistoryRequest(
                    peer=entity, limit=1, offset_id=0, offset_date=None,
                    add_offset=0, max_id=0, min_id=0, hash=0,
                ))
                if messages.messages:
                    await client(functions.messages.GetDiscussionMessageRequest(
                        peer=entity, msg_id=messages.messages[0].id,
                    ))
            except Exception as e:
                log.warning("Не удалось открыть комментарии в %s: %s", channel, e)

            # Explicitly join discussion group
            join_error_text = ""
            try:
                discussion_entity = await client.get_entity(linked_chat_id)
                await client(JoinChannelRequest(discussion_entity))
                log.info("Вступил в группу обсуждения канала %s (id=%d)", channel, linked_chat_id)
            except UserAlreadyParticipantError:
                log.info("Уже в группе обсуждения канала %s", channel)
            except Exception as e:
                join_error_text = str(e).lower()
                log.warning("Ошибка JoinChannel для группы обсуждения %s: %s", channel, e)

            # Verify membership via GetParticipantRequest
            membership = await check_membership(client, linked_chat_id)
            if membership == "joined":
                log.info("✅ Подтверждено: вступил в группу обсуждения %s", channel)
                status[key] = "joined"
            else:
                if "requested to join" in join_error_text or "request" in join_error_text:
                    log.info("⏳ Заявка на вступление в группу обсуждения %s подана", channel)
                    await notify_admin(f"⏳ Заявка на вступление в группу обсуждения {channel} подана, ожидаю одобрения")
                else:
                    log.warning("⚠️ Не удалось вступить в группу обсуждения %s", channel)
                    await notify_admin(f"⚠️ Не удалось вступить в группу обсуждения {channel}")
                status[key] = "pending"

            save_channel_status(status)

            peer_id = utils.get_peer_id(entity)
            channel_map[peer_id] = (channel, linked_chat_id)
            channel_entities.append(entity)
            log.info("Канал %s (peer_id=%d, id=%d) -> группа обсуждения id=%d", channel, peer_id, entity.id, linked_chat_id)

        except FloodWaitError as e:
            log.warning("FloodWait при вступлении: ждём %d сек", e.seconds)
            await asyncio.sleep(e.seconds)
            continue
        except Exception as e:
            log.error("Ошибка при вступлении в %s: %s", channel, e)
            status[key] = "error"
            save_channel_status(status)
            continue

        # Delay between joins
        delay = random.randint(2, 3)
        log.info("Задержка %d сек перед следующим каналом...", delay)
        await asyncio.sleep(delay)

    return channel_entities


async def check_pending_channels(client: TelegramClient):
    """Check if pending channel join requests have been approved."""
    status = load_channel_status()
    changed = False
    for key, st in list(status.items()):
        if st != "pending":
            continue
        mapping = None
        for peer_id, (ch_username, linked_id) in channel_map.items():
            if channel_key(ch_username) == key:
                mapping = (ch_username, linked_id)
                break
        if not mapping:
            continue
        ch_username, linked_id = mapping
        membership = await check_membership(client, linked_id)
        if membership == "joined":
            status[key] = "joined"
            changed = True
            log.info("✅ Заявка в группу обсуждения %s одобрена! Канал активен.", ch_username)
            await notify_admin(f"✅ Заявка в группу обсуждения {ch_username} одобрена! Канал активен.")
    if changed:
        save_channel_status(status)


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
    channel_entities = await join_channels(client, channels)
    if not channel_entities:
        log.error("Нет активных каналов с комментариями")
        return

    entity_names = [getattr(e, "title", str(e.id)) for e in channel_entities]
    log.info("Активные каналы: %s", ", ".join(entity_names))

    @client.on(events.NewMessage(chats=channel_entities))
    async def on_new_post(event):
        global comments_today

        # Log every incoming message for debugging
        log.info(
            "Получено сообщение из %s: %s",
            event.chat_id,
            event.message.text[:50] if event.message.text else "нет текста",
        )

        # Only process channel posts, not discussion group messages
        if not event.is_channel or event.is_group:
            log.info("Пропускаю: не пост канала (is_channel=%s, is_group=%s)", event.is_channel, event.is_group)
            return

        reset_daily_counter()

        if comments_today >= MAX_COMMENTS_PER_DAY:
            log.info("Достигнут лимит комментариев за день (%d)", MAX_COMMENTS_PER_DAY)
            return

        post_text = event.message.text
        if not post_text:
            return

        # Random skip
        if random.random() < SKIP_CHANCE:
            log.info("⏭ Пост пропущен (случайный пропуск)")
            await notify_admin("⏭ Пост пропущен (случайный пропуск)")
            return

        chat = await event.get_chat()
        chat_title = getattr(chat, "title", str(chat.id))
        log.info("Новый пост в [%s]: %s", chat_title, post_text[:80])
        await notify_admin(f"📨 Новый пост в [{chat_title}]:\n{post_text[:100]}")

        # Look up discussion group by channel entity id
        mapping = channel_map.get(event.chat_id)
        if not mapping:
            log.warning("Не найдена группа обсуждения для канала %s (id=%d)", chat_title, event.chat_id)
            return

        channel_username, discussion_group_id = mapping

        # Skip channels with pending status
        key = channel_key(channel_username)
        ch_status = load_channel_status().get(key)
        if ch_status == "pending":
            log.info("⏳ Канал %s ожидает одобрения заявки, комментарий пропущен", channel_username)
            await notify_admin(f"⏳ Канал {channel_username} ожидает одобрения заявки, комментарий пропущен")
            return

        try:
            comment = generate_comment(post_text)
            log.info("Сгенерирован комментарий: %s", comment[:80])
        except Exception as e:
            log.error("Ошибка генерации комментария: %s", e)
            await notify_admin(f"❌ Ошибка: {e}")
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
        except ChatWriteForbiddenError:
            # Try joining discussion group via GetDiscussionMessageRequest and retry
            log.warning("Нет прав в [%s], пробую вступить через открытие комментариев...", chat_title)
            try:
                await client(functions.messages.GetDiscussionMessageRequest(
                    peer=event.chat_id, msg_id=event.message.id,
                ))
                await client.send_message(
                    event.chat_id,
                    comment,
                    comment_to=event.message.id,
                )
            except Exception as retry_err:
                log.error("Повторная отправка не удалась в [%s]: %s", chat_title, retry_err)
                await notify_admin(f"❌ Ошибка: нет прав на комментарии в [{chat_title}]")
                return
        except FloodWaitError as e:
            log.warning("FloodWait: ждём %d сек", e.seconds)
            await notify_admin(f"❌ Ошибка: FloodWait {e.seconds} сек")
            await asyncio.sleep(e.seconds)
            return
        except Exception as e:
            log.error("Ошибка отправки комментария: %s", e)
            await notify_admin(f"❌ Ошибка: {e}")
            return

        comments_today += 1
        log.info(
            "Комментарий опубликован в [%s] (%d/%d за день)",
            chat_title, comments_today, MAX_COMMENTS_PER_DAY,
        )
        # Update stats
        stats = load_stats()
        today_str = datetime.now().strftime("%Y-%m-%d")
        if stats.get("today") != today_str:
            stats["today"] = today_str
            stats["today_count"] = 0
        stats["today_count"] += 1
        stats["total_count"] = stats.get("total_count", 0) + 1
        stats["last_comment"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_stats(stats)
        await notify_admin(
            f"✅ Комментарий в [{chat_title}] ({comments_today}/{MAX_COMMENTS_PER_DAY}):\n{comment}"
        )

    # Startup notification with status counts
    status = load_channel_status()
    joined_count = sum(1 for v in status.values() if v == "joined")
    pending_count = sum(1 for v in status.values() if v == "pending")
    log.info("Бот запущен. Мониторинг каналов: %s", ", ".join(entity_names))
    await notify_admin(
        f"🚀 Бот запущен. Активных: {joined_count}, ожидают одобрения: {pending_count}"
    )

    # Periodic check for pending channels (every 60 seconds)
    async def pending_checker():
        while True:
            await asyncio.sleep(60)
            try:
                await check_pending_channels(client)
            except Exception as e:
                log.error("Ошибка проверки pending каналов: %s", e)

    asyncio.create_task(pending_checker())

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
