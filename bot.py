import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from script import Editor, client

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PROXY = os.getenv("TELEGRAM_PROXY")
CREDENTIALS_PATH = os.getenv("CREDENTIALS_PATH", "calm-photon-486609-u4-96ce79c043ec.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

editor = None
if CREDENTIALS_PATH and SPREADSHEET_ID:
    editor = Editor(CREDENTIALS_PATH, SPREADSHEET_ID)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)
        status = member.status
        if status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return
    except Exception as e:
        logger.warning("Не удалось проверить права в чате %s: %s", chat.id, e)
        return

    text = update.message.text.strip()
    if not text:
        return

    try:
        task_dict = Editor.extract_task_from_chat_message(text, client)
    except Exception as e:
        logger.exception("Ошибка LLM при разборе сообщения: %s", e)
        return

    if not task_dict:
        return

    if not editor:
        logger.error("Editor не инициализирован (CREDENTIALS_PATH / SPREADSHEET_ID)")
        await update.message.reply_text("Таблица не настроена, задачу записать нельзя.")
        return

    try:
        editor.insert_info(task_dict)
        title = task_dict.get("task") or "Задача"
        await update.message.reply_text(f"Задача добавлена в таблицу: «{title}»")
    except Exception as e:
        logger.exception("Ошибка записи в таблицу: %s", e)
        await update.message.reply_text("Не удалось записать задачу в таблицу.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Задайте TELEGRAM_BOT_TOKEN в .env")
    builder = Application.builder().token(TOKEN)
    if PROXY:
        builder.proxy(PROXY)
    app = builder.build()
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_group_message),
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
