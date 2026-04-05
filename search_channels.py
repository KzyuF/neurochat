import sys
import json
import shutil
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from telethon import TelegramClient, functions
from telethon.errors import ChannelPrivateError
from config import API_ID, API_HASH

BASE_DIR = Path(__file__).parent
MAX_POST_AGE_DAYS = 7


async def search(keyword):
    # Copy main session to avoid blocking the running client
    main_session = BASE_DIR / "neurochat_session.session"
    search_session = BASE_DIR / "search_session.session"
    if main_session.exists():
        shutil.copy2(main_session, search_session)

    client = TelegramClient(str(BASE_DIR / "search_session"), API_ID, API_HASH)
    await client.start()
    result = await client(functions.contacts.SearchRequest(q=keyword, limit=30))
    channels = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_POST_AGE_DAYS)
    for chat in result.chats:
        if hasattr(chat, "broadcast") and chat.broadcast:
            try:
                full = await client(functions.channels.GetFullChannelRequest(chat))
                linked_chat_id = full.full_chat.linked_chat_id
                has_comments = False
                if linked_chat_id:
                    try:
                        discussion = await client.get_entity(linked_chat_id)
                        await client(functions.channels.GetFullChannelRequest(discussion))
                        has_comments = True
                    except (ChannelPrivateError, ValueError, Exception):
                        has_comments = False

                # Check last post date
                last_post = None
                try:
                    messages = await client(functions.messages.GetHistoryRequest(
                        peer=chat, limit=1, offset_id=0, offset_date=None,
                        add_offset=0, max_id=0, min_id=0, hash=0,
                    ))
                    if messages.messages:
                        last_post = messages.messages[0].date
                except Exception:
                    pass

                # Skip channels with no posts or posts older than 7 days
                if not last_post or last_post < cutoff:
                    continue

                channels.append({
                    "title": chat.title,
                    "username": chat.username or "",
                    "channel_id": chat.id,
                    "participants": full.full_chat.participants_count or 0,
                    "comments": has_comments,
                    "last_post": last_post.isoformat(),
                })
            except Exception:
                pass
    await client.disconnect()
    print(json.dumps(channels, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("[]")
    else:
        asyncio.run(search(sys.argv[1]))
