import html
import asyncio
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from aiogram.filters import Command
from dotenv import load_dotenv

try:
    from google import genai
    from google.genai import types
except ImportError:
    import google.generativeai as genai
    from google.generativeai import types

# --- БЕЗОПАСНАЯ ЗАГРУЗКА ОКРУЖЕНИЯ ---
current_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(current_dir, ".env"))


def get_clean_env(key: str) -> str:
    """Достает переменную из .env, очищая её от случайных пробелов и кавычек клиентов"""
    value = os.getenv(key)
    if value:
        return value.strip().replace('"', '').replace("'", "")
    return None


TELEGRAM_TOKEN = get_clean_env("TELEGRAM_TOKEN")
GEMINI_API_KEY = get_clean_env("GEMINI_API_KEY")
LOG_CHANNEL_ID = get_clean_env("LOG_CHANNEL_ID")  # Канал для ИИ-логов

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    print("❌ КРИТИЧЕСКАЯ ОШИБКА: Токены не найдены или заполнены неверно в файле .env!")
    exit(1)

# --- ДИНАМИЧЕСКИЙ ВЫНОС КОММЕРЧЕСКИХ НАСТРОЕК (ЗАЩИТА ОТ ОШИБОК КЛИЕНТА) ---
try:
    raw_limit = get_clean_env("DAILY_AI_LIMIT")
    if raw_limit is not None:
        DAILY_AI_LIMIT = int(raw_limit)
    else:
        DAILY_AI_LIMIT = 20
        print("ℹ️ DAILY_AI_LIMIT не указан в .env. Применен базовый лимит: 20")
except (TypeError, ValueError):
    DAILY_AI_LIMIT = 20
    print("⚠️ Ошибка: В .env указано некорректное значение DAILY_AI_LIMIT. Включен защитный лимит: 20")

# --- ИНИЦИАЛИЗАЦИЯ КОМПОНЕНТОВ ---
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

DB_NAME = os.path.join(current_dir, "moderator.db")
KNOWLEDGE_FILE = os.path.join(current_dir, "knowledge.txt")
last_warn_time = {}

# --- ТЕХНИЧЕСКИЙ ТЫЛ: БЕЛЫЙ СПИСОК СВЯЩЕННЫХ ДОМЕНОВ ---
WHITE_LIST_DOMAINS = [
    'netology.ru', 'skillbox.ru', 'github.com', 'zoom.us',
    'youtube.com', 'habr.com', 'stepik.org', 'google.com'
]


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS infractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id INTEGER,
            timestamp TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()


def check_ai_limit(chat_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    time_limit = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("SELECT COUNT(*) FROM ai_usage WHERE chat_id = ? AND timestamp > ?", (chat_id, time_limit))
    used_requests = cursor.fetchone()[0]
    conn.close()
    print(f"📊 Лимит ИИ для чата {chat_id}: использовано {used_requests}/{DAILY_AI_LIMIT} (UTC)")
    return used_requests < DAILY_AI_LIMIT


def log_ai_request(chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("INSERT INTO ai_usage (chat_id, timestamp) VALUES (?, ?)", (chat_id, now_str))
    conn.commit()
    conn.close()


def get_active_warns(user_id: int, chat_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    time_limit = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("SELECT COUNT(*) FROM infractions WHERE user_id = ? AND chat_id = ? AND timestamp > ?",
                   (user_id, chat_id, time_limit))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def add_infraction(user_id: int, chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute("INSERT INTO infractions (user_id, chat_id, timestamp) VALUES (?, ?, ?)",
                   (user_id, chat_id, now_str))
    conn.commit()
    conn.close()


async def send_admin_log(user_name: str, user_id: int, chat_title: str, bad_text: str, reason: str, action: str):
    if not LOG_CHANNEL_ID:
        return

    clean_user_name = html.escape(user_name)
    clean_chat_title = html.escape(chat_title)
    clean_bad_text = html.escape(bad_text)
    clean_reason = html.escape(reason)
    clean_action = html.escape(action)

    now_str = datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M (UTC)')

    log_message = (
        f"🚨 <b>ЛОГ НАКАЗАНИЯ | Context AI</b>\n"
        f"--------------------------------\n"
        f"🌐 <b>Чат:</b> {clean_chat_title}\n"
        f"👤 <b>Нарушитель:</b> {clean_user_name} (ID: <code>{user_id}</code>)\n"
        f"📅 <b>Время:</b> {now_str}\n\n"
        f"❌ <b>Текст сообщения:</b>\n"
        f"<i>\"{clean_bad_text}\"</i>\n\n"
        f"🧠 <b>Причина блокировки:</b>\n"
        f"<i>{clean_reason}</i>\n\n"
        f"🛠 <b>Действие системы:</b> {clean_action}\n"
        f"--------------------------------"
    )
    try:
        target_chat = int(LOG_CHANNEL_ID) if LOG_CHANNEL_ID.startswith("-") else LOG_CHANNEL_ID
        await bot.send_message(chat_id=target_chat, text=log_message, parse_mode="HTML")
    except Exception as e:
        print(f"⚠️ Не удалось отправить лог в канал: {e}")


LINK_PATTERN = re.compile(r'(https?://\S+|t\.me/\S+)')


def is_link_whitelisted(text: str) -> bool:
    """Проверяет, содержатся ли в тексте только разрешенные домены"""
    links = LINK_PATTERN.findall(text)
    if not links:
        return False

    for link in links:
        link_lower = link.lower()
        # Если ссылка содержит хотя бы один домен не из белого списка, значит проверку не прошла
        whitelisted = any(domain in link_lower for domain in WHITE_LIST_DOMAINS)
        if not whitelisted:
            return False
    return True


# --- УМНАЯ СБОРКА ИНСТРУКЦИИ ДЛЯ GEMINI ---
def get_ai_instruction() -> str:
    knowledge_base = ""
    if os.path.exists(KNOWLEDGE_FILE):
        try:
            with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
                knowledge_base = f.read().strip()
        except Exception as e:
            print(f"⚠️ Ошибка чтения knowledge.txt: {e}. Используем пустую базу.")

    base_prompt = (
        "Ты — профессиональный ИИ-модератор коммерческого Telegram-чата Context AI.\n"
        "Твоя задача — жестко фильтровать рекламу и спам, НО сохранять живое общение участников.\n\n"
        "ЖЕСТКИЕ ПРАВИЛА АНАЛИЗА КОНТЕКСТА:\n"
        "1. Одиночные символы, плюсы («+»), смайлики и знаки согласия в контексте прогрева — это нормальное поведение пользователей, их удалять ЗАПРЕЩЕНО. Отвечай СТРОГО: OK.\n"
        "2. Если пользователь просто упоминает слова 'крипта', 'заработок', 'инвестиции' в рамках обычного диалога, рассуждает или задает легитимный вопрос (например: 'что думаете про крипту?', 'инвестиции это сложно') — это РАЗРЕШЕНО. Отвечай СТРОГО: OK.\n"
        "3. Если сообщение содержит ССЫЛКУ, проанализируй намерения автора. Если пользователь искренне делится полезным материалом по теме чата (статья на Хабре, код на GitHub) или отвечает на вопрос другого участника — это РАЗРЕШЕНО. Отвечай СТРОГО: OK.\n"
        "4. Если ссылка или текст ведут на сторонние Telegram-каналы, ботов, сомнительные схемы заработка, замаскированы под 'подарок/слив', содержат призывы 'пиши в ЛС', 'подробности в профиле' или продают чужие курсы — это СТРОГО СПАМ. Отвечай СТРОГО: SPAM.\n\n"
    )

    if knowledge_base:
        base_prompt += f"ДОПУСТИМАЯ БАЗА ЗНАНИЙ КОМПАНИИ (на эти вопросы отвечай развернуто, а не словом OK):\n{knowledge_base}\n\n"
        base_prompt += "Если пользователь задает вопрос по Базе Знаний — дай ему вежливый, краткий ответ на основе этих фактов.\n"

    base_prompt += "ВАЖНО: Если в тексте нет спама и нет вопроса по Базе Знаний — отвечай СТРОГО одним словом: OK. Ничего не придумывай."
    return base_prompt


async def process_with_ai(user_text: str, chat_id: int) -> str:
    if not check_ai_limit(chat_id):
        return "LIMIT_EXCEEDED"

    try:
        current_instruction = get_ai_instruction()

        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: ai_client.models.generate_content(
                model='gemini-2.5-flash', contents=user_text,
                config=types.GenerateContentConfig(
                    system_instruction=current_instruction,
                    temperature=0.1,
                    safety_settings=safety_settings
                )
            )
        )
        log_ai_request(chat_id)
        return response.text.strip()
    except Exception as e:
        print(f"📡 Ошибка связи с Gemini API: {e}")
        return "AI_ERROR"


async def punish_user(message: Message, log_reason: str):
    user_id = message.from_user.id
    chat_id = message.chat.id
    user_name = message.from_user.username or message.from_user.first_name
    chat_title = message.chat.title or f"ID: {chat_id}"
    text_content = message.text

    add_infraction(user_id, chat_id)
    active_warns = get_active_warns(user_id, chat_id)

    if active_warns >= 3:
        action_taken = f"Удалено сообщение + Бан пользователя ({active_warns}/3 варнов)"
    else:
        action_taken = f"Удалено сообщение + Выдан варн ({active_warns}/3)"

    await send_admin_log(
        user_name=f"@{user_name}" if message.from_user.username else user_name,
        user_id=user_id,
        chat_title=chat_title,
        bad_text=text_content,
        reason=log_reason,
        action=action_taken
    )

    try:
        await message.delete()
    except Exception as e:
        print(f"Не удалось удалить (возможно, нет прав): {e}")

    print(f"❌ Нарушение от {user_name}. Активных варнов: {active_warns}/3")

    if active_warns >= 3:
        try:
            await message.chat.ban(user_id=user_id)
            await message.answer(f"🚫 Пользователь {user_name} набрал {active_warns}/3 варнов и был ЗАБАНЕН!")
        except Exception as e:
            await message.answer(f"⚠️ {user_name} превысил лимит, но я не могу забанить администратора.")
    else:
        now = datetime.now(timezone.utc)
        last_time = last_warn_time.get((user_id, chat_id))
        if last_time and (now - last_time) < timedelta(seconds=10):
            return
        last_warn_time[(user_id, chat_id)] = now
        await message.answer(f"⚠️ Предупреждение для {user_name}! Обнаружен спам. Варны: {active_warns}/3.")


@router.message(Command("unban"))
async def handle_unban(message: Message):
    member = await message.chat.get_member(message.from_user.id)
    if member.status not in ["administrator", "creator"]:
        await message.reply("⚠️ Эта команда доступна только администраторам чата.")
        return

    if not message.reply_to_message:
        await message.reply(
            "💡 Перешлите сообщение/уведомление пользователя, которого нужно разбанить, и напишите /unban.")
        return

    reply = message.reply_to_message
    bot_info = await bot.get_me()
    if reply.from_user.id == bot_info.id:
        await message.reply(
            "⚠️ <b>Ошибка логики!</b> Вы сделали ответ (reply) на техническое сообщение бота.\n"
            "Чтобы разбанить человека, сделайте ответ на его <i>собственное</i> старое сообщение.",
            parse_mode="HTML"
        )
        return

    if reply.forward_from:
        target_user_id = reply.forward_from.id
        target_user_name = reply.forward_from.username or reply.forward_from.first_name
    elif reply.forward_sender_name:
        await message.reply("⚠️ У этого пользователя скрыт профиль приватности Telegram.", parse_mode="HTML")
        return
    else:
        target_user_id = reply.from_user.id
        target_user_name = reply.from_user.username or reply.from_user.first_name

    chat_title = message.chat.title or f"ID: {message.chat.id}"

    try:
        await message.chat.unban(user_id=target_user_id, vacancies_remain=True)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM infractions WHERE user_id = ? AND chat_id = ?", (target_user_id, message.chat.id))
        conn.commit()
        conn.close()

        await message.answer(f"✅ Пользователь {target_user_name} разбанен, счетчик варнов сброшен!")
        await send_admin_log(
            user_name=f"@{target_user_name}" if target_user_name else f"ID: {target_user_id}",
            user_id=target_user_id,
            chat_title=chat_title,
            bad_text="[Команда разбана]",
            reason=f"Администратор @{message.from_user.username or message.from_user.first_name} амнистировал пользователя.",
            action="Снят бан в Telegram + очищена бд варнов"
        )
    except Exception as e:
        await message.reply(f"❌ Не удалось разбанить: {e}")


# --- ОБНОВЛЕННЫЙ УМНЫЙ ФИЛЬТР СООБЩЕНИЙ ---
@router.message()
async def handle_message(message: Message):
    if message.from_user is None or message.from_user.is_bot or not message.text:
        return

    text = message.text
    chat_id = message.chat.id

    # УРОВЕНЬ ЗАЩИТЫ 1: Иммунитет для администрации проекта
    try:
        user_member = await message.chat.get_member(message.from_user.id)
        if user_member.status in ["administrator", "creator"]:
            return  # Ссылки и сообщения от админов/кураторов никогда не трогаем
    except Exception as e:
        print(f"⚠️ Не удалось проверить статус пользователя: {e}")

    # УРОВЕНЬ ЗАЩИТЫ 2: Проверка белого списка доменов
    if LINK_PATTERN.search(text):
        if is_link_whitelisted(text) or "contextai.ru" in text:
            print(f"✅ Пропущена полезная ссылка из белого списка в чате {chat_id}")
            return  # Ссылка разрешена, ИИ дергать не нужно
        else:
            # Если это левая ссылка (не из белого списка), Линия 1 сразу наказывает
            await punish_user(message, log_reason="Сработала автоматическая Линия 1 (Локальный фильтр левых ссылок)")
            return

    # УРОВЕНЬ ЗАЩИТЫ 3: Интеллектуальный ИИ-анализ (для скрытого нативного спама)
    try:
        ai_response = await asyncio.wait_for(process_with_ai(text, chat_id), timeout=4.0)
    except asyncio.TimeoutError:
        print(f"⏰ Ошибка: ИИ Gemini не ответил за 4 секунды. Запрос пропущен.")
        return

    if ai_response is None or ai_response in ["LIMIT_EXCEEDED", "AI_ERROR"]:
        return

    if ai_response.upper() == "SPAM":
        await punish_user(message, log_reason="Сработала Линия 2 (ИИ Gemini распознал нативный спам/рекламу)")
    elif ai_response.upper() == "OK":
        return
    else:
        await message.reply(ai_response)


async def main():
    init_db()
    dp.include_router(router)
    print("🔥 Идеальный Context AI Commercial V1.2 успешно запущен и готов к продаже!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())