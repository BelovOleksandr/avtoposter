import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")

async def main():
    from pyrogram import Client
    async with Client("my_account", api_id, api_hash) as app:
        await app.send_message("me", "Сессия создана!")
        print("✅ Сессия сохранена в my_account.session")

if __name__ == "__main__":
    asyncio.run(main())