import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

BOT_TOKEN = "8398181888:AAGRkEhnJv1AcFFyiUBtnKTMN04pB0eLJwo"
ADMIN_ID = 706575799

BASE_DIR = Path(__file__).parent
CHANNELS_FILE = BASE_DIR / "channels.txt"
CONFIG_FILE = BASE_DIR / "config.py"
STATS_FILE = BASE_DIR / "stats.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


def load_channels() -> list[str]:
    if not CHANNELS_FILE.exists():
        return []
    channels = []
    for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            channels.append(line)
    return channels


def normalize_channel(name: str) -> str:
    """Normalize channel name to @username format."""
    return "@" + name.strip().lstrip("@")


def save_channels(channels: list[str]):
    content = "# Добавьте каналы по одному на строку\n"
    for ch in channels:
        content += f"{ch}\n"
    CHANNELS_FILE.write_text(content, encoding="utf-8")


def load_stats() -> dict:
    if not STATS_FILE.exists():
        return {"today": "", "today_count": 0, "total_count": 0, "last_comment": ""}
    try:
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception):
        return {"today": "", "today_count": 0, "total_count": 0, "last_comment": ""}


async def run_command(*args) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode().strip()
    if stderr.decode().strip():
        output += "\n" + stderr.decode().strip()
    return output


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_admin(message):
        return
    await message.answer(
        "🤖 <b>NeuroChat Management Bot</b>\n\n"
        "Команды управления:\n"
        "/channels — список каналов\n"
        "/add username — добавить канал\n"
        "/remove username — удалить канал\n"
        "/stats — статистика\n"
        "/logs — последние логи\n"
        "/status — статус сервиса\n"
        "/restart — перезапустить бота\n"
        "/pause — остановить комментирование\n"
        "/resume — возобновить комментирование\n"
        "/delay min max — изменить задержку\n"
        "/prompt текст — изменить промпт\n"
        "/limit число — изменить лимит комментариев",
        parse_mode="HTML",
    )


@dp.message(Command("channels"))
async def cmd_channels(message: Message):
    if not is_admin(message):
        return
    channels = load_channels()
    if not channels:
        await message.answer("Список каналов пуст.")
        return
    lines = [f"{i+1}. {ch}" for i, ch in enumerate(channels)]
    await message.answer(
        f"📋 <b>Каналы ({len(channels)}):</b>\n" + "\n".join(lines),
        parse_mode="HTML",
    )


@dp.message(Command("add"))
async def cmd_add(message: Message):
    if not is_admin(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /add username")
        return
    username = args[1].strip().lstrip("@")
    channels = load_channels()
    normalized = [normalize_channel(ch) for ch in channels]
    channel_entry = f"@{username}"
    if channel_entry in normalized:
        await message.answer(f"Канал @{username} уже в списке.")
        return
    channels.append(channel_entry)
    save_channels(channels)
    await message.answer(f"✅ Канал @{username} добавлен. Перезапустите бота командой /restart")


@dp.message(Command("remove"))
async def cmd_remove(message: Message):
    if not is_admin(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /remove username")
        return
    username = args[1].strip().lstrip("@")
    channels = load_channels()
    channel_entry = normalize_channel(username)
    filtered = [ch for ch in channels if normalize_channel(ch) != channel_entry]
    if len(filtered) == len(channels):
        await message.answer(f"Канал @{username} не найден в списке.")
        return
    save_channels(filtered)
    await message.answer(f"✅ Канал @{username} удалён. Перезапустите бота командой /restart")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message):
        return
    channels = load_channels()
    stats = load_stats()
    today = datetime.now().strftime("%Y-%m-%d")
    today_count = stats.get("today_count", 0) if stats.get("today") == today else 0
    total_count = stats.get("total_count", 0)
    last_comment = stats.get("last_comment", "—")
    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Каналов: {len(channels)}\n"
        f"Комментариев сегодня: {today_count}\n"
        f"Всего комментариев: {total_count}\n"
        f"Последний комментарий: {last_comment}",
        parse_mode="HTML",
    )


@dp.message(Command("logs"))
async def cmd_logs(message: Message):
    if not is_admin(message):
        return
    output = await run_command("journalctl", "-u", "neurochat", "--no-pager", "-n", "15")
    if not output:
        output = "Логи пусты или сервис не найден."
    await message.answer(f"📝 <b>Логи:</b>\n<pre>{output[-3500:]}</pre>", parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if not is_admin(message):
        return
    output = await run_command("systemctl", "is-active", "neurochat")
    await message.answer(f"Статус neurochat: <b>{output}</b>", parse_mode="HTML")


@dp.message(Command("restart"))
async def cmd_restart(message: Message):
    if not is_admin(message):
        return
    await run_command("systemctl", "restart", "neurochat")
    await message.answer("✅ Бот комментирования перезапущен")


@dp.message(Command("pause"))
async def cmd_pause(message: Message):
    if not is_admin(message):
        return
    await run_command("systemctl", "stop", "neurochat")
    await message.answer("⏸ Комментирование приостановлено")


@dp.message(Command("resume"))
async def cmd_resume(message: Message):
    if not is_admin(message):
        return
    await run_command("systemctl", "start", "neurochat")
    await message.answer("▶️ Комментирование возобновлено")


@dp.message(Command("delay"))
async def cmd_delay(message: Message):
    if not is_admin(message):
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer("Использование: /delay min max")
        return
    try:
        min_delay = int(args[1])
        max_delay = int(args[2])
    except ValueError:
        await message.answer("Ошибка: укажите целые числа.")
        return
    config_text = CONFIG_FILE.read_text(encoding="utf-8")
    config_text = re.sub(r"MIN_DELAY\s*=\s*\d+", f"MIN_DELAY = {min_delay}", config_text)
    config_text = re.sub(r"MAX_DELAY\s*=\s*\d+", f"MAX_DELAY = {max_delay}", config_text)
    CONFIG_FILE.write_text(config_text, encoding="utf-8")
    await message.answer(f"✅ Задержка изменена: {min_delay}-{max_delay} сек")


@dp.message(Command("prompt"))
async def cmd_prompt(message: Message):
    if not is_admin(message):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer("Использование: /prompt текст нового промпта")
        return
    new_prompt = args[1].strip().replace("\\", "\\\\").replace('"', '\\"')
    config_text = CONFIG_FILE.read_text(encoding="utf-8")
    config_text = re.sub(
        r'SYSTEM_PROMPT\s*=\s*\(.*?\)',
        f'SYSTEM_PROMPT = "{new_prompt}"',
        config_text,
        flags=re.DOTALL,
    )
    CONFIG_FILE.write_text(config_text, encoding="utf-8")
    await message.answer("✅ Промпт обновлён")


@dp.message(Command("limit"))
async def cmd_limit(message: Message):
    if not is_admin(message):
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /limit число")
        return
    try:
        limit = int(args[1])
    except ValueError:
        await message.answer("Ошибка: укажите целое число.")
        return
    config_text = CONFIG_FILE.read_text(encoding="utf-8")
    config_text = re.sub(r"MAX_COMMENTS_PER_DAY\s*=\s*\d+", f"MAX_COMMENTS_PER_DAY = {limit}", config_text)
    CONFIG_FILE.write_text(config_text, encoding="utf-8")
    await message.answer(f"✅ Лимит изменён: {limit} комментариев в день")


async def run():
    log.info("Management bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run())
