import sys
import json
import shutil
import asyncio
from pathlib import Path
from telethon import TelegramClient, functions
from telethon.errors import ChannelPrivateError
from config import API_ID, API_HASH

BASE_DIR = Path(__file__).parent


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
                channels.append({
                    "title": chat.title,
                    "username": chat.username or "",
                    "participants": full.full_chat.participants_count or 0,
                    "comments": has_comments,
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
