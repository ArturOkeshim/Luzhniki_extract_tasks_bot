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
def _parse_allowed_chat_ids() -> set[int]:
    raw = os.getenv("TELEGRAM_BOT_CHAT_ID", "").strip()
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


ALLOWED_CHAT_IDS = _parse_allowed_chat_ids()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Убираем шум от telegram (подключение, polling, получение update)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

editor = None
if CREDENTIALS_PATH and SPREADSHEET_ID:
    editor = Editor(CREDENTIALS_PATH, SPREADSHEET_ID)

# Ожидающие задачи без срока: (chat_id, user_id) -> {"task": формулировка, "task_dict": dict для insert_info}
pending_tasks: dict[tuple[int, int], dict] = {}


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    if ALLOWED_CHAT_IDS and chat.id not in ALLOWED_CHAT_IDS:
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

    chat_id = chat.id
    user = update.effective_user
    user_id = user.id if user else 0
    chat_title = getattr(chat, "title", None) or chat.id
    logger.info("Сообщение в чате «%s» (id=%s): %s", chat_title, chat_id, text[:200] + ("..." if len(text) > 200 else ""))

    # Есть ли ожидающая задача без срока от этого пользователя?
    pending_key = (chat_id, user_id)
    if pending_key in pending_tasks:
        pending = pending_tasks[pending_key]
        try:
            follow_up = Editor.parse_follow_up_for_deadline(
                pending["task"], text, client
            )
        except Exception as e:
            logger.exception("Ошибка LLM при разборе ответа по сроку: %s", e)
            await update.message.reply_text(
                "Не удалось разобрать ответ. Укажите срок в формате дд.мм.гггг или напишите, что задачу пока не добавлять."
            )
            return
        if follow_up["action"] == "add":
            task_dict = {**pending["task_dict"], "deadline": follow_up["deadline"]}
            del pending_tasks[pending_key]
            if editor:
                try:
                    row = editor.insert_info(task_dict)
                    title = task_dict.get("task") or "Задача"
                    logger.info("Задача (со сроком из ответа) записана в таблицу, строка %s", row)
                    await update.message.reply_text(f"Задача добавлена в таблицу: «{title}»")
                except Exception as e:
                    logger.exception("Ошибка записи в таблицу: %s", e)
                    await update.message.reply_text("Не удалось записать задачу в таблицу.")
            else:
                await update.message.reply_text("Таблица не настроена, задачу записать нельзя.")
            return
        if follow_up["action"] == "decline":
            del pending_tasks[pending_key]
            await update.message.reply_text(
                "Хорошо, задачу не добавляю. Если позже понадобится внести её в таблицу — напишите с указанием срока."
            )
            return
        # unclear
        await update.message.reply_text(
            f"Не понял. Укажите срок для задачи «{pending['task']}» (например, 25.02.2025) или напишите, что задачу пока не добавлять."
        )
        return

    try:
        logger.info("Отправка в LLM на разбор (задача или нет)...")
        task_dict = Editor.extract_task_from_chat_message(text, client)
    except Exception as e:
        logger.exception("Ошибка LLM при разборе сообщения: %s", e)
        return

    if not task_dict:
        logger.info("LLM: в сообщении задача не обнаружена, пропуск")
        return

    logger.info(
        "LLM: извлечена задача — «%s», ответственный=%s, срок=%s, приоритет=%s",
        task_dict.get("task"),
        task_dict.get("responsible"),
        task_dict.get("deadline"),
        task_dict.get("priority"),
    )

    # Задачу без срока в таблицу не ставим — запрашиваем срок и запоминаем задачу
    if not task_dict.get("deadline"):
        formulation = task_dict.get("task") or "Задача"
        pending_tasks[pending_key] = {"task": formulation, "task_dict": task_dict}
        logger.info("Задача без срока сохранена в ожидание: «%s»", formulation)
        await update.message.reply_text(
            f"По задаче «{formulation}» не указан срок. Ответьте на это сообщение, указав срок (например, 25.02.2025), или напишите, что срок пока неизвестен / задачу пока не добавлять.",
            reply_to_message_id=update.message.message_id,
        )
        return

    if not editor:
        logger.error("Editor не инициализирован (CREDENTIALS_PATH / SPREADSHEET_ID)")
        await update.message.reply_text("Таблица не настроена, задачу записать нельзя.")
        return

    try:
        row = editor.insert_info(task_dict)
        title = task_dict.get("task") or "Задача"
        logger.info("Задача записана в таблицу, строка %s", row)
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
