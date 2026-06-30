from __future__ import annotations

import asyncio
import logging
import os
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Message
from dotenv import load_dotenv

from script import Editor, client

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PROXY = os.getenv("TELEGRAM_PROXY")
CREDENTIALS_PATH = os.getenv("CREDENTIALS_PATH", "calm-photon-486609-u4-96ce79c043ec.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
RETRY_DELAY_SECONDS = int(os.getenv("RETRY_DELAY_SECONDS", "5"))
TELEGRAM_TIMEOUT = int(os.getenv("TELEGRAM_TIMEOUT", "30"))


def _parse_allowed_chat_ids() -> set[int]:
    raw = os.getenv("TELEGRAM_BOT_CHAT_ID", "").strip()
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


ALLOWED_CHAT_IDS = _parse_allowed_chat_ids()
_raw_leader_id = os.getenv("TELEGRAM_BOT_LEADER_ID")
ALLOWED_LEADER = int(_raw_leader_id) if _raw_leader_id else None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)

editor = None
if CREDENTIALS_PATH and SPREADSHEET_ID:
    try:
        editor = Editor(CREDENTIALS_PATH, SPREADSHEET_ID)
        logger.info("Google Sheets: OK (spreadsheet_id=%s)", SPREADSHEET_ID)
    except Exception:
        logger.exception("Google Sheets: ошибка при инициализации Editor")
else:
    logger.warning(
        "Google Sheets: не настроено (CREDENTIALS_PATH=%r, SPREADSHEET_ID=%r)",
        CREDENTIALS_PATH,
        SPREADSHEET_ID,
    )

_vsegpt_key = os.getenv("VSE_GPT_API", "")
if _vsegpt_key:
    logger.info("VseGPT: ключ задан (%s...%s)", _vsegpt_key[:6], _vsegpt_key[-4:])
else:
    logger.warning("VseGPT: ключ VSE_GPT_API не задан в .env")

pending_tasks: dict[tuple[int, int], dict] = {}
dp = Dispatcher()


def _mask_token(token: str | None) -> str:
    if not token:
        return "<не задан>"
    if len(token) <= 10:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


def _log_skip(reason: str, **details) -> None:
    parts = [f"{k}={v!r}" for k, v in details.items()]
    logger.info("ПРОПУСК: %s | %s", reason, ", ".join(parts) if parts else "—")


@dp.update.outer_middleware()
async def log_incoming_update(handler, event, data):
    message = event.message or event.edited_message
    if message:
        text = message.text or message.caption or ""
        logger.info(
            "UPDATE от Telegram: update_id=%s chat_id=%s chat_type=%s user_id=%s "
            "msg_id=%s has_text=%s preview=%r",
            event.update_id,
            message.chat.id,
            message.chat.type,
            message.from_user.id if message.from_user else None,
            message.message_id,
            bool(message.text),
            text[:120] + ("..." if len(text) > 120 else ""),
        )
    return await handler(event, data)


async def on_group_message(message: Message) -> None:
    logger.info("→ on_group_message: handler сработал")

    if not message.text:
        _log_skip(
            "сообщение без текста",
            chat_id=message.chat.id,
        )
        return

    chat = message.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        _log_skip("не групповой чат", chat_type=chat.type, chat_id=chat.id)
        return

    #if ALLOWED_CHAT_IDS and chat.id not in ALLOWED_CHAT_IDS:
    #    _log_skip("чат не в ALLOWED_CHAT_IDS", chat_id=chat.id)
    #    return
    #if ALLOWED_LEADER is not None and message.from_user and message.from_user.id != ALLOWED_LEADER:
    #    _log_skip("отправитель не лидер", user_id=message.from_user.id)
    #    return

    try:
        me = await message.bot.get_me()
        member = await message.bot.get_chat_member(chat.id, me.id)
        status = member.status
        logger.info(
            "Проверка прав бота: chat_id=%s bot_id=%s status=%s",
            chat.id,
            me.id,
            status,
        )
        if status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            _log_skip(
                "бот не администратор чата",
                chat_id=chat.id,
                bot_status=status,
            )
            return
    except Exception:
        logger.exception("Проверка прав бота: ошибка Telegram API")
        return

    text = message.text.strip()
    if not text:
        _log_skip("пустой текст после strip", chat_id=chat.id)
        return

    chat_id = chat.id
    user_id = message.from_user.id if message.from_user else 0
    chat_title = chat.title or str(chat.id)
    logger.info(
        "Обработка сообщения: чат «%s» id=%s user_id=%s текст=%r",
        chat_title,
        chat_id,
        user_id,
        text[:200] + ("..." if len(text) > 200 else ""),
    )

    pending_key = (chat_id, user_id)
    if pending_key in pending_tasks:
        pending = pending_tasks[pending_key]
        logger.info("Режим уточнения срока для задачи «%s»", pending["task"])
        try:
            t0 = time.monotonic()
            follow_up = await asyncio.to_thread(
                Editor.parse_follow_up_for_deadline,
                pending["task"],
                text,
                client,
            )
            logger.info(
                "VseGPT (срок): ответ за %.2f с — %s",
                time.monotonic() - t0,
                follow_up,
            )
        except Exception:
            logger.exception("VseGPT: ошибка при разборе ответа по сроку")
            await message.answer(
                "Не удалось разобрать ответ. Укажите срок в формате дд.мм.гггг или напишите, что задачу пока не добавлять."
            )
            return

        if follow_up["action"] == "add":
            task_dict = {**pending["task_dict"], "deadline": follow_up["deadline"]}
            del pending_tasks[pending_key]
            if editor:
                try:
                    row = await asyncio.to_thread(editor.insert_info, task_dict)
                    title = task_dict.get("task") or "Задача"
                    logger.info("Google Sheets: задача записана, строка %s", row)
                    await message.answer(f"Задача добавлена в таблицу: «{title}»")
                except Exception:
                    logger.exception("Google Sheets: ошибка записи задачи")
                    await message.answer("Не удалось записать задачу в таблицу.")
            else:
                await message.answer("Таблица не настроена, задачу записать нельзя.")
            return

        if follow_up["action"] == "decline":
            del pending_tasks[pending_key]
            await message.answer(
                "Хорошо, задачу не добавляю. Если позже понадобится внести её в таблицу — напишите с указанием срока."
            )
            return

        await message.answer(
            f"Не понял. Укажите срок для задачи «{pending['task']}» (например, 25.02.2025) или напишите, что задачу пока не добавлять."
        )
        return

    try:
        logger.info("VseGPT: отправка на разбор (задача или нет)...")
        t0 = time.monotonic()
        task_dict = await asyncio.to_thread(
            Editor.extract_task_from_chat_message, text, client
        )
        logger.info("VseGPT: ответ за %.2f с", time.monotonic() - t0)
    except Exception:
        logger.exception("VseGPT: ошибка при разборе сообщения")
        return

    if not task_dict:
        logger.info("VseGPT: задача не обнаружена — тихий пропуск")
        return

    logger.info(
        "VseGPT: извлечена задача — «%s», ответственный=%s, срок=%s",
        task_dict.get("task"),
        task_dict.get("responsible"),
        task_dict.get("deadline"),
    )

    if not task_dict.get("deadline"):
        formulation = task_dict.get("task") or "Задача"
        pending_tasks[pending_key] = {"task": formulation, "task_dict": task_dict}
        logger.info("Задача без срока — ждём ответ: «%s»", formulation)
        await message.answer(
            f"По задаче «{formulation}» не указан срок. Ответьте на это сообщение, указав срок (например, 25.02.2025), или напишите, что срок пока неизвестен / задачу пока не добавлять.",
            reply_to_message_id=message.message_id,
        )
        return

    if not editor:
        await message.answer("Таблица не настроена, задачу записать нельзя.")
        return

    try:
        row = await asyncio.to_thread(editor.insert_info, task_dict)
        title = task_dict.get("task") or "Задача"
        logger.info("Google Sheets: задача записана, строка %s", row)
        await message.answer(f"Задача добавлена в таблицу: «{title}»")
    except Exception:
        logger.exception("Google Sheets: ошибка записи")
        await message.answer("Не удалось записать задачу в таблицу.")


dp.message.register(
    on_group_message,
    F.text,
    ~F.text.startswith("/"),
)


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("Задайте TELEGRAM_BOT_TOKEN в .env")

    logger.info("=== Запуск бота (aiogram) ===")
    logger.info("TELEGRAM_BOT_TOKEN: %s", _mask_token(TOKEN))
    logger.info("TELEGRAM_PROXY: %s", PROXY or "<не задан>")
    logger.info("Google Sheets editor: %s", "готов" if editor else "НЕ готов")

    session = AiohttpSession(timeout=TELEGRAM_TIMEOUT, proxy=PROXY or None)
    if PROXY:
        logger.info("Прокси через AiohttpSession (timeout=%s с)", TELEGRAM_TIMEOUT)

    bot = Bot(token=TOKEN, session=session)

    try:
        me = await bot.get_me()
        logger.info(
            "Старт: @%s | видит все сообщения в группе: %s",
            me.username,
            me.can_read_all_group_messages,
        )
        webhook = await bot.get_webhook_info()
        if webhook.pending_update_count:
            logger.warning(
                "В очереди Telegram %s апдейтов — заберём при polling",
                webhook.pending_update_count,
            )
        if webhook.url:
            await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        logger.exception("Старт: ошибка проверки Telegram API")

    while True:
        try:
            logger.info("Polling запущен, жду сообщения...")
            await dp.start_polling(bot, drop_pending_updates=False)
            break
        except TelegramNetworkError as error:
            logger.warning(
                "Telegram network error: %s. Повтор через %s с...",
                error,
                RETRY_DELAY_SECONDS,
            )
            await asyncio.sleep(RETRY_DELAY_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout Telegram API. Повтор через %s с...",
                RETRY_DELAY_SECONDS,
            )
            await asyncio.sleep(RETRY_DELAY_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
