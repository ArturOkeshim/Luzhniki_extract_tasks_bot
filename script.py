
import gspread
import json
from datetime import datetime
from google.oauth2.service_account import Credentials
from openai import OpenAI
from dotenv import load_dotenv
import os
from zoneinfo import ZoneInfo

load_dotenv()
api_key = os.getenv("VSE_GPT_API")

client = OpenAI(
    api_key=api_key, # ваш ключ в VseGPT после регистрации
    base_url="https://api.vsegpt.ru/v1",
)

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


class Editor:
    def __init__(self, credentials_path, spreadsheet_id):
        self.credentials_path = credentials_path
        self.spreadsheet_id = spreadsheet_id

        credentials = Credentials.from_service_account_file(
            credentials_path,
            scopes=SCOPES
        )
        self.client = gspread.authorize(credentials)
        self.spreadsheet = self.client.open_by_key(spreadsheet_id)
        self.sheet = self.spreadsheet.sheet1

    def get_sheet_names(self) -> list[str]:
        """Возвращает список названий всех листов таблицы (для динамического выбора объекта)."""
        worksheets = self.spreadsheet.worksheets()
        return [ws.title for ws in worksheets]

    def _col_number_to_letter(self, col: int) -> str:
        """Столбец по счёту (1, 2, 3...) в букву (A, B, C...)."""
        result = ""
        n = col
        while n > 0:
            n, r = divmod(n - 1, 26)
            result = chr(65 + r) + result
        return result

    def get_last_filled_row(self, col: int = 4, sheet_name=None) -> int:
        """
        Возвращает номер последней строки с непустым значением в столбце col (по умолчанию C).
        Нужно для вставки новой задачи в следующую строку: next_row = get_last_filled_row() + 1.
        """
        sheet = self.spreadsheet.worksheet(sheet_name) if sheet_name else self.sheet
        values = sheet.col_values(col)
        for i in range(len(values) - 1, -1, -1):
            v = values[i]
            if v and str(v).strip():
                return i + 1
        return 0
    def scan_table(self, sheet_name=None):
        """
        Читает таблицу задач и возвращает все значения.

        Считается, что заголовок в строке 3, данные — с 4-й строки,
        таблица в столбцах 2–9 (8 колонок). Читается диапазон от строки 3
        до последней заполненной строки в столбце C (get_last_filled_row).

        Возвращает список строк: каждая строка — список значений ячеек.
        Первый элемент списка — строка заголовков, остальные — строки данных.
        """
        sheet = self.spreadsheet.worksheet(sheet_name) if sheet_name else self.sheet
        last_row = self.get_last_filled_row(col=3, sheet_name=sheet_name)
        if last_row < 3:
            return []
        start_letter = self._col_number_to_letter(2)
        end_letter = self._col_number_to_letter(9)
        range_name = f"{start_letter}3:{end_letter}{last_row}"
        return sheet.get(range_name)
    def get_row_info(self, row_num: int, sheet_name=None, ):
        """Возвращает значения одной строки (столбцы B–I) как список списков, как и sheet.get."""
        sheet = self.spreadsheet.worksheet(sheet_name) if sheet_name else self.sheet
        start_letter = self._col_number_to_letter(2)
        end_letter = self._col_number_to_letter(9)
        range_name = f"{start_letter}{row_num}:{end_letter}{row_num}"
        return sheet.get(range_name)
    def insert_info(self, task_dict: dict, sheet_name=None) -> int:
        """
        Вставляет задачу из словаря (результат decipher_add_task_command) в таблицу.
        Колонки: Статус, Задача, Категория, Ответственные, Срок, Приоритет, Комментарии/Подзадачи.
        Статус для новых задач всегда 🔄. Пустые значения (None) записываются как пустая ячейка.
        Строка вставки: следующая после последней заполненной в столбце C.
        """
        sheet = self.spreadsheet.worksheet(sheet_name) if sheet_name else self.sheet
        next_row = self.get_last_filled_row(col=3, sheet_name=sheet_name) + 1
        row_data = [
            "🔄",
            (task_dict.get("task") or ""),
            (task_dict.get("category") or ""),
            (task_dict.get("responsible") or ""),
            datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M"),  # дата/время добавления (распознаётся в Таблице как дата)
            (task_dict.get("deadline") or ""),
            (task_dict.get("priority") or ""),
            (task_dict.get("comments") or ""),
        ]
        start_letter = self._col_number_to_letter(3)
        end_letter = self._col_number_to_letter(10)  # 8 колонок: C..J
        range_name = f"{start_letter}{next_row}:{end_letter}{next_row}"
        sheet.update(range_name, [row_data])
        # Возвращаем номер добавленной строки — удобно для последующей отмены.
        return next_row
    def update_info(self, search_result: dict, sheet_name=None) -> None:
        """
        Вносит изменения в таблицу по результату search_task_to_update.
        Берёт только первую подходящую задачу (matched_rows[0]), записывает в неё значения из changes.
        Если matched_rows пустой или changes пустой — ничего не делает.
        """
        matched = search_result.get("matched_rows", [])
        changes = search_result.get("changes", {})
        if not matched or not changes:
            raise ValueError(
                "Нет данных для обновления: укажите matched_rows и changes в search_result"
            )
        row = matched[0]
        raw = self.scan_table(sheet_name=sheet_name)
        if not raw:
            raise ValueError("Таблица пуста или не удалось прочитать данные листа")
        headers = [str(h).strip() for h in raw[0]]
        sheet = self.spreadsheet.worksheet(sheet_name) if sheet_name else self.sheet
        for header_name, value in changes.items():
            if header_name not in headers:
                continue
            col = 2 + headers.index(header_name)
            sheet.update_cell(row, col, value)

    def delete_row(self, row_num: int, sheet_name=None) -> None:
        """
        Очищает содержимое строки с колонки C по I (включительно).
        Строка не удаляется — колонка B с формулой нумерации остаётся нетронутой.
        Используется для отмены последнего добавления.
        """
        sheet = self.spreadsheet.worksheet(sheet_name) if sheet_name else self.sheet
        start_letter = self._col_number_to_letter(3)   # C
        end_letter = self._col_number_to_letter(9)     # I
        range_name = f"{start_letter}{row_num}:{end_letter}{row_num}"
        empty_row = [[""] * 7]  # 7 колонок: C, D, E, F, G, H, I
        sheet.update(range_name, empty_row)

    @staticmethod
    def decipher_add_task_command(command: str, client: OpenAI) -> dict:
        """
        Извлекает из команды пользователя структурированные данные для строки таблицы.
        Возвращает dict с полями: task, responsible, deadline, priority, comments.
        """
        today = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")
        prompt = f"""Сегодняшняя дата: {today}

Пользователь прислал команду для создания задачи:
«{command}»

Извлеки из команды данные и верни ТОЛЬКО валидный JSON без markdown и пояснений. Схема:

{{
  "task": "Лаконичное название задачи (1-10 слов). Переформулируй хаотичное описание в чёткую формулировку.",
  "responsible": "Имя ответственного в именительном падеже. Если не указано — null.",
  "deadline": "Дата в формате дд.мм.гггг. Учитывай относительные формулировки («через 2 дня», «к пятнице», «до конца недели») относительно сегодняшней даты. Если не указано — null.",
  "priority": "Приоритет: «высокий», «средний» или «низкий». Приведи к одному из этих значений. Если не указано — null.",
  "comments": "Комментарии, подзадачи, уточнения — всё полезное для исполнителя. Ничего не добавляй, если пользователь прямо в команде не указывает что-то написать в комментарии."
}}

Ответ — только JSON:"""

        response = client.chat.completions.create(
            model="openai/gpt-5-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        # Убираем markdown-обёртку если LLM добавил ```json ... ```
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        return json.loads(text)

    @staticmethod
    def extract_task_from_chat_message(message_text: str, client: OpenAI) -> dict | None:
        """
        Анализирует сообщение из рабочего чата: есть ли в нём постановка задачи
        (кто-то кому-то что-то поручил). Если да — извлекает поля для таблицы и возвращает
        dict для insert_info; если нет — возвращает None.
        """
        if not message_text or not message_text.strip():
            return None
        today = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")
        prompt = f"""Сегодняшняя дата: {today}

Сообщение в рабочем чате:
«{message_text.strip()}»

Определи: есть ли здесь постановка задачи — то есть кто-то явно или по смыслу поручает другому человеку (или группе) что-то сделать. Обычные обсуждения, вопросы, благодарности, новости без поручения — не задача.

Ответ — ТОЛЬКО один JSON без markdown и пояснений.

Если постановки задачи НЕТ — верни: {{"is_task": false}}

Если постановка задачи ЕСТЬ — верни JSON с полями:
{{
  "is_task": true,
  "task": "Краткое название задачи (1-10 слов), чёткая формулировка.",
  "responsible": "Имя ответственного в именительном падеже. Если не указано — null.",
  "deadline": "Дата в формате дд.мм.гггг (относительные формулировки переведи относительно сегодня). Если не указано — null.",
  "priority": "«высокий», «средний» или «низкий». Если не указано — null.",
  "comments": "Оставь пустым, только если пользователь напрямую не просит что-то отметить в комментарии",
  "category": "Категория задачи, если из контекста понятна. Иначе null."
}}

Ответ — только JSON:"""

        response = client.chat.completions.create(
            model="openai/gpt-5-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        data = json.loads(text)
        if not data.get("is_task"):
            return None
        # Приводим к формату insert_info (без is_task)
        return {
            "task": data.get("task") or "",
            "responsible": data.get("responsible"),
            "deadline": data.get("deadline"),
            "priority": data.get("priority"),
            "comments": data.get("comments"),
            "category": data.get("category"),
        }

    @staticmethod
    def parse_follow_up_for_deadline(
        pending_task_formulation: str, message_text: str, client: OpenAI
    ) -> dict:
        """
        Пользователь ранее отправил задачу без срока. Разобрать его новое сообщение:
        указал ли он срок, отказался ли от добавления задачи, или ответ неясен.
        Возвращает dict с полем "action": "add" | "decline" | "unclear"
        и при action=="add" — "deadline": "дд.мм.гггг".
        """
        if not message_text or not message_text.strip():
            return {"action": "unclear"}
        today = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")
        prompt = f"""Сегодняшняя дата: {today}

Ранее пользователь поставил задачу (без срока): «{pending_task_formulation}»
Его новое сообщение: «{message_text.strip()}»

Определи по смыслу сообщения одно из трёх:
1) Пользователь УКАЗЫВАЕТ СРОК для этой задачи (дата, «к пятнице», «через 2 дня», «до конца недели» и т.п.) → верни JSON: {{"action": "add", "deadline": "дд.мм.гггг"}}. Дата только в формате дд.мм.гггг, переведи относительные формулировки относительно сегодня.
2) Пользователь ОТКАЗЫВАЕТСЯ от добавления задачи: говорит, что срок неизвестен, пока не ставить задачу, не добавлять, отмена и т.п. → верни JSON: {{"action": "decline"}}
3) Непонятно или не относится к задаче → верни JSON: {{"action": "unclear"}}

Ответ — только один JSON, без markdown и пояснений."""

        response = client.chat.completions.create(
            model="openai/gpt-5-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        data = json.loads(text)
        action = (data.get("action") or "unclear").strip().lower()
        if action == "add":
            deadline = (data.get("deadline") or "").strip()
            if deadline:
                return {"action": "add", "deadline": deadline}
        if action == "decline":
            return {"action": "decline"}
        return {"action": "unclear"}

    def search_task_to_update(self, command: str, client: OpenAI, sheet_name=None) -> dict:
        """
        По описанию пользователя находит подходящие задачи и то, что нужно в них изменить.
        Возвращает dict:
          - matched_rows — список номеров строк на листе (sheet_row);
          - changes — словарь «название колонки» → новое значение (только колонки из таблицы).
        Если таблица пуста или задача не найдена — matched_rows пустой. Неизвестные ключи в changes отбрасываются.
        """
        raw = self.scan_table(sheet_name=sheet_name)
        if not raw or len(raw) < 2:
            return {"matched_rows": []}
        headers = [str(h).strip() for h in raw[0]]
        rows_for_llm = []
        for i in range(1, len(raw)):
            sheet_row = 3 + i
            row_dict = {"sheet_row": sheet_row}
            for j, header in enumerate(headers):
                row_dict[header] = raw[i][j] if j < len(raw[i]) else ""
            rows_for_llm.append(row_dict)
        table_json = json.dumps(rows_for_llm, ensure_ascii=False, indent=2)

        headers_help = ", ".join(headers)

        today = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")
        prompt = f"""Сегодняшняя дата: {today}
        
Таблица задач (каждая строка с полем sheet_row — номер строки на листе):

{table_json}

Колонки таблицы (названия используй точно): {headers_help}

Пользователь написал: «{command}»

Сделай три шага:
1) Определи, какая задача (или какие) имеется в виду — верни их sheet_row в массиве matched_rows.
2) Определи, что именно пользователь хочет изменить: в объекте changes укажи только те колонки, которые нужно обновить. Ключ — точное название колонки из списка выше, значение — новое значение для ячейки.
3) Обязательно заполни поле "Ответ в чате" — одну короткую фразу для пользователя в чате: что именно сделано. Укажи название задачи в кавычках и суть изменения. Примеры: "Перенёс срок по задаче «Отчёт по продажам» на 15.02.2025"; "Поменял статус по задаче «Связаться с подрядчиком» на Выполнено"; "Назначил ответственным по задаче «Подготовить зал» Петрова".

Примеры changes:
- «задача егорова по отчету выполнена» → changes: {{"Статус": "✅"}}
- «перенести срок на 15.02.2025» → changes: {{"Срок": "15.02.2025"}}
- «добавить в комментарий: согласовано с директором» → changes: {{"Комментарии / Подзадачи": "согласовано с директором"}}
- «сделай петрова ответственным по этой задаче» → changes: {{"Ответственный": "Петров"}}
Если пользователь не просит ничего менять — верни changes: {{}}.

Важно - если изменение касается статуса задачи, то используй следующие условные обозначения вместо текста:
🔄 — В работе / Планирование
✅ — Выполнено
⚠️ — Высокий приоритет / Контроль

Формат ответа — только один JSON с тремя полями:
{{"matched_rows": [4], "changes": {{"Заголовок": "Изменение"}}, "Ответ в чате": "Перенёс срок по задаче «Название» на 15.02.2025"}}
Только JSON, без markdown и пояснений."""

        response = client.chat.completions.create(
            model="openai/gpt-5-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        data = json.loads(text)
        matched = data.get("matched_rows", [])
        if not isinstance(matched, list):
            matched = [matched] if matched else []
        changes_raw = data.get("changes", {})
        if not isinstance(changes_raw, dict):
            changes_raw = {}
        changes = {k: str(v) for k, v in changes_raw.items() if k in headers}
        chat_reply = (data.get("Ответ в чате") or "").strip()

        # Сохраняем исходные значения строки для возможной отмены изменений.
        revert_row = None
        if matched:
            try:
                old_row_raw = self.get_row_info(sheet_name=sheet_name, row_num=matched[0])
                if old_row_raw and len(old_row_raw[0]) > 0:
                    row_values = old_row_raw[0]
                    revert_row = {
                        header: (row_values[i] if i < len(row_values) else "")
                        for i, header in enumerate(headers)
                    }
            except Exception:
                # Если не удалось прочитать строку — просто не даём возможность отката.
                revert_row = None

        return {
            "matched_rows": matched,
            "changes": changes,
            "chat_reply": chat_reply,
            "revert_row": revert_row,
        }


def transcribe_voice(file_path: str, client: OpenAI) -> str:
    """
    Транскрибирует голосовое сообщение в текст через VseGPT (Whisper).
    Логика совпадает с transcribe.py для тестов вне бота.
    """
    with open(file_path, "rb") as audio_file:
        response = client.audio.transcriptions.create(
            model="stt-openai/whisper-v3-turbo",
            response_format="json",
            language="ru",
            file=audio_file,
        )

    if hasattr(response, "text"):
        return response.text
    if isinstance(response, dict) and "text" in response:
        return str(response["text"])
    return str(response)


# Запуск тестов
if __name__ == "__main__":
    credentials_path = "calm-photon-486609-u4-96ce79c043ec.json"
    spreadsheet_id = "13ZYBzUNsUZvbcNj2cytfjKp8s0r_ULcQZKqrbtOsJz8"
    bot = Editor(credentials_path, spreadsheet_id)

    test_phrase = "Необходимо сегодня связаться с подрядчиком по ремонту и решить вопрос"
    print("Запрос:", test_phrase)
    result = bot.search_task_to_update(test_phrase, client)
    bot.update_info(result)
    print("matched_rows:", result["matched_rows"])
    print("changes:", result["changes"])

    


