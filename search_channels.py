import sys
import json
import asyncio
from telethon import TelegramClient, functions
from config import API_ID, API_HASH


async def search(keyword):
    client = TelegramClient("neurochat_session", API_ID, API_HASH)
    await client.start()
    result = await client(functions.contacts.SearchRequest(q=keyword, limit=30))
    channels = []
    for chat in result.chats:
        if hasattr(chat, "broadcast") and chat.broadcast:
            try:
                full = await client(functions.channels.GetFullChannelRequest(chat))
                has_comments = bool(full.full_chat.linked_chat_id)
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
