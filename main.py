import asyncio
import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, functions, utils
from telethon.errors import (
    FloodWaitError, ChatWriteForbiddenError,
    UserAlreadyParticipantError, ChannelPrivateError, InviteHashExpiredError,
)
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest, GetParticipantRequest
from telethon.tl.types import PeerChannel
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


def channel_key(channel) -> str:
    """Normalize channel identifier to a key for status dict."""
    if isinstance(channel, int):
        return str(channel)
    return str(channel).strip().lstrip("@")


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


async def resolve_channel(client: TelegramClient, channel: str):
    """Resolve channel by username or numeric ID (using PeerChannel)."""
    if channel.lstrip("-").isdigit():
        return await client.get_entity(PeerChannel(int(channel)))
    return await client.get_entity(channel)


async def join_channels(client: TelegramClient, channels: list[str]) -> list:
    """Join channels and their discussion groups. Returns list of channel entities."""
    channel_entities = []
    status = load_channel_status()

    for channel in channels:
        key = channel_key(channel)
        existing_status = status.get(key)

        try:
            # For channels already joined or pending — just resolve entity and map
            if existing_status in ("joined", "pending"):
                entity = await resolve_channel(client, channel)
                full = await client(GetFullChannelRequest(entity))
                linked_chat_id = full.full_chat.linked_chat_id
                if not linked_chat_id:
                    log.warning("Канал %s не имеет комментариев, пропускаю", channel)
                    status[key] = "error"
                    save_channel_status(status)
                    continue
                peer_id = utils.get_peer_id(entity)
                channel_map[peer_id] = (channel, linked_chat_id)
                channel_entities.append(entity)
                log.info("Канал %s загружен из кэша (статус: %s)", channel, existing_status)
                await asyncio.sleep(1)
                continue

            # New channel — full join flow
            entity = await resolve_channel(client, channel)
            try:
                await client(JoinChannelRequest(entity))
                log.info("Вступил в канал %s", channel)
            except UserAlreadyParticipantError:
                log.info("Уже в канале %s", channel)
            except (ChannelPrivateError, InviteHashExpiredError):
                log.warning("Канал %s приватный, пропускаю", channel)
                status[key] = "error"
                save_channel_status(status)
                continue

            # Get linked discussion group
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

        # Delay between channels
        await asyncio.sleep(1)

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


def solve_math_captcha(text: str) -> int | None:
    """Parse and solve a math captcha from message text."""
    text = text.replace("×", "*").replace("х", "*").replace("X", "*")
    # Match patterns like "9+3", "5 * 4", "12 - 7"
    match = re.search(r"(\d+)\s*([+\-*])\s*(\d+)", text)
    if not match:
        # Try word-based: "5 плюс 3", "9 минус 2"
        match = re.search(r"(\d+)\s*(?:плюс|plus)\s*(\d+)", text, re.IGNORECASE)
        if match:
            return int(match.group(1)) + int(match.group(2))
        match = re.search(r"(\d+)\s*(?:минус|minus)\s*(\d+)", text, re.IGNORECASE)
        if match:
            return int(match.group(1)) - int(match.group(2))
        match = re.search(r"(\d+)\s*(?:умножить|multiply)\s*\S*\s*(\d+)", text, re.IGNORECASE)
        if match:
            return int(match.group(1)) * int(match.group(2))
        return None
    a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    return None


COLOR_MAP = {
    "красн": ["🔴", "красн", "red"],
    "синю": ["🔵", "синю", "синий", "blue"],
    "голуб": ["🔵", "голуб", "blue"],
    "зелён": ["🟢", "зелён", "зелен", "green"],
    "жёлт": ["🟡", "жёлт", "желт", "yellow"],
    "бел": ["⚪", "бел", "white"],
    "чёрн": ["⚫", "чёрн", "черн", "black"],
}

CONFIRM_WORDS = [
    "я не робот", "не робот", "подтвердить", "войти",
    "i'm not a bot", "not a bot", "confirm", "verify", "press", "нажми",
]


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

    @client.on(events.ChatAction)
    async def on_chat_action(event):
        if event.user_kicked or event.user_left:
            me = await client.get_me()
            if event.user_id == me.id:
                chat = await event.get_chat()
                title = getattr(chat, "title", "Неизвестный чат")
                username = getattr(chat, "username", "")
                log.warning("🚫 Исключён из чата: %s (@%s)", title, username)
                await notify_admin(f"🚫 Вас исключили из чата: {title} (@{username})")
                # Update status for matching channel
                status = load_channel_status()
                for key, (ch_username, linked_id) in list(channel_map.items()):
                    ch_key = channel_key(ch_username)
                    if key == event.chat_id or linked_id == event.chat_id:
                        status[ch_key] = "kicked"
                        log.info("Статус канала %s обновлён на 'kicked'", ch_username)
                        break
                save_channel_status(status)

    # Collect discussion group IDs for captcha handler
    discussion_group_ids = set(linked_id for _, linked_id in channel_map.values())

    @client.on(events.NewMessage(func=lambda e: e.buttons and not e.is_private))
    async def on_captcha(event):
        # Only handle messages in our discussion groups
        if event.chat_id not in discussion_group_ids and abs(event.chat_id) not in discussion_group_ids:
            return

        text = event.message.text or event.message.message or ""
        text_lower = text.lower()
        chat = await event.get_chat()
        chat_title = getattr(chat, "title", str(event.chat_id))

        # Collect all button texts
        all_buttons = []
        for row in event.buttons:
            for btn in row:
                all_buttons.append(btn)

        solved = False

        # Type 1: Math captcha
        answer = solve_math_captcha(text)
        if answer is not None:
            for btn in all_buttons:
                btn_text = (btn.text or "").strip()
                if btn_text == str(answer):
                    try:
                        await btn.click()
                        await asyncio.sleep(2)
                        log.info("🔓 Капча решена в %s (математика: %s=%d)", chat_title, text[:30], answer)
                        await notify_admin(f"🔓 Автоматически решена капча в {chat_title}: математика ({answer})")
                        solved = True
                    except Exception as e:
                        log.error("Ошибка нажатия кнопки капчи: %s", e)
                    break
            if solved:
                return

        # Type 2: Color captcha
        for color_key, color_variants in COLOR_MAP.items():
            if color_key in text_lower:
                for btn in all_buttons:
                    btn_text = (btn.text or "").lower()
                    for variant in color_variants:
                        if variant.lower() in btn_text:
                            try:
                                await btn.click()
                                await asyncio.sleep(2)
                                log.info("🔓 Капча решена в %s (цвет: %s)", chat_title, color_key)
                                await notify_admin(f"🔓 Автоматически решена капча в {chat_title}: цвет ({color_key})")
                                solved = True
                            except Exception as e:
                                log.error("Ошибка нажатия кнопки капчи: %s", e)
                            break
                    if solved:
                        break
                if solved:
                    return

        # Type 3: Confirm button
        for btn in all_buttons:
            btn_text = (btn.text or "").lower()
            for word in CONFIRM_WORDS:
                if word in btn_text:
                    try:
                        await btn.click()
                        await asyncio.sleep(2)
                        log.info("🔓 Капча решена в %s (подтверждение: %s)", chat_title, btn.text)
                        await notify_admin(f"🔓 Автоматически решена капча в {chat_title}: подтверждение")
                        solved = True
                    except Exception as e:
                        log.error("Ошибка нажатия кнопки капчи: %s", e)
                    break
            if solved:
                break
        if solved:
            return

        # Unknown captcha
        log.warning("❓ Неизвестная капча в %s: %s", chat_title, text[:100])
        await notify_admin(f"❓ Неизвестная капча в {chat_title}: {text[:200]}. Решите вручную.")

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

        # Skip channels with pending or kicked status
        key = channel_key(channel_username)
        ch_status = load_channel_status().get(key)
        if ch_status == "pending":
            log.info("⏳ Канал %s ожидает одобрения заявки, комментарий пропущен", channel_username)
            await notify_admin(f"⏳ Канал {channel_username} ожидает одобрения заявки, комментарий пропущен")
            return
        if ch_status == "kicked":
            log.info("🚫 Канал %s — исключены, комментарий пропущен", channel_username)
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
                await notify_admin(
                    f"⚠️ Не могу комментировать в [{chat_title}] — нет доступа. Возможно исключили."
                )
                # Mark as kicked
                status = load_channel_status()
                status[channel_key(channel_username)] = "kicked"
                save_channel_status(status)
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
    channel_keys = [channel_key(ch) for ch in channels]
    joined_count = sum(1 for k in channel_keys if status.get(k) == "joined")
    pending_count = sum(1 for k in channel_keys if status.get(k) == "pending")
    kicked_count = sum(1 for k in channel_keys if status.get(k) == "kicked")
    error_count = sum(1 for k in channel_keys if status.get(k) == "error")
    log.info("Бот запущен. Мониторинг каналов: %s", ", ".join(entity_names))
    await notify_admin(
        f"🚀 Бот запущен. Каналов: {len(channels)}\n"
        f"✅ Активных: {joined_count}\n"
        f"⏳ Ожидают одобрения: {pending_count}\n"
        f"🚫 Исключён: {kicked_count}\n"
        f"❌ Ошибка: {error_count}"
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
