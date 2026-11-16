import asyncio,json
from aiogram import Bot
from app.config import get_settings
async def main():
 b=Bot(get_settings().require_bot_token())
 try:
  chat=await b.get_chat(-1004389602150)
  print('DESTINATION_CHAT='+json.dumps({'id':chat.id,'title':chat.title,'username':chat.username,'type':chat.type},ensure_ascii=False))
 finally:await b.session.close()
asyncio.run(main())
