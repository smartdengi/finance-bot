import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message

# Для HTTP-запросов к FastAPI
import aiohttp

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # Обязательно задайте в Railway!
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")  # URL вашего FastAPI

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Хранилище JWT-токенов по chat_id
user_tokens: Dict[int, str] = {}

# Простое хранилище состояний для сценариев регистрации/логина/добавления
state_data: Dict[int, dict] = {}


def get_state(chat_id: int) -> Optional[dict]:
    return state_data.get(chat_id)


def set_state(chat_id: int, state: str, data: dict = None):
    state_data[chat_id] = {"state": state, "data": data or {}}


def clear_state(chat_id: int):
    state_data.pop(chat_id, None)


# ========== ФУНКЦИЯ ВЫЗОВА API ==========
async def call_api(method: str, path: str, payload: dict = None, token: str = None, form: bool = False):
    """
    Универсальный вызов FastAPI.
    Возвращает (json_response, status_code).
    """
    url = f"{API_BASE_URL}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with aiohttp.ClientSession() as session:
        try:
            if method.upper() == "GET":
                async with session.get(url, headers=headers) as resp:
                    return await resp.json(), resp.status

            elif method.upper() == "POST":
                if form:
                    # Для OAuth2 (логин) нужен формат x-www-form-urlencoded
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    async with session.post(url, data=payload, headers=headers) as resp:
                        return await resp.json(), resp.status
                else:
                    async with session.post(url, json=payload or {}, headers=headers) as resp:
                        return await resp.json(), resp.status

            elif method.upper() == "DELETE":
                async with session.delete(url, headers=headers) as resp:
                    return await resp.json(), resp.status
            else:
                raise ValueError(f"Unsupported method: {method}")

        except Exception as e:
            # Возвращаем читаемую ошибку
            return {"detail": str(e)}, 500


# ========== КОМАНДЫ ==========

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я — ваш финансовый помощник.\n\n"
        "📌 Основные команды:\n"
        "🔐 /register — создать аккаунт\n"
        "🔑 /login — войти в аккаунт\n"
        "📊 /stats — статистика\n"
        "💰 /budget — бюджет на месяц\n"
        "➖ /add — добавить расход или доход\n"
        "👤 /profile — профиль и статус подписки\n"
        "❓ /help — справка"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📚 Все команды:\n\n"
        "/start — начальное меню\n"
        "/register — регистрация\n"
        "/login — вход\n"
        "/logout — выход из аккаунта\n"
        "/stats — статистика расходов/доходов\n"
        "/budget — установить бюджет на месяц\n"
        "/add — добавить транзакцию\n"
        "/profile — просмотр профиля\n"
        "/premium — о Premium (скоро)\n"
        "/help — справка"
    )


@dp.message(Command("register"))
async def cmd_register(message: Message):
    chat_id = message.chat.id
    if chat_id in user_tokens:
        await message.answer("Вы уже вошли в аккаунт. Чтобы создать новый — сначала /logout.")
        return
    set_state(chat_id, "reg_email")
    await message.answer("Введите ваш email (например, user@mail.ru):")


@dp.message(Command("login"))
async def cmd_login(message: Message):
    chat_id = message.chat.id
    if chat_id in user_tokens:
        await message.answer("Вы уже вошли в аккаунт.")
        return
    set_state(chat_id, "login_email")
    await message.answer("Введите ваш email:")


@dp.message(Command("logout"))
async def cmd_logout(message: Message):
    chat_id = message.chat.id
    if chat_id in user_tokens:
        user_tokens.pop(chat_id, None)
        await message.answer("✅ Вы вышли из аккаунта.")
    else:
        await message.answer("Вы и так не авторизованы.")


@dp.message(Command("add"))
async def cmd_add(message: Message):
    chat_id = message.chat.id
    token = user_tokens.get(chat_id)
    if not token:
        await message.answer("Сначала войдите: /login или /register")
        return
    set_state(chat_id, "add_transaction")
    await message.answer(
        "Отправьте сумму и категорию одним сообщением.\n"
        "Форматы:\n"
        "• `500 продукты` — расход\n"
        "• `-500 фриланс` — доход\n"
        "• `расход 500 продукты` или `доход 500 фриланс`"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    chat_id = message.chat.id
    token = user_tokens.get(chat_id)
    if not token:
        await message.answer("Сначала войдите: /login или /register")
        return

    try:
        result, status = await call_api("GET", "/api/stats", token=token)
        if status == 200:
            expenses = result.get("expenses", 0)
            income = result.get("income", 0)
            await message.answer(
                f"📊 Статистика:\n"
                f"Расходы: {expenses} ₽\n"
                f"Доходы: {income} ₽\n"
                f"Баланс: {income - expenses} ₽"
            )
        else:
            await message.answer(f"Ошибка: {result.get('detail', result)}")
    except Exception as e:
        await message.answer(f"Ошибка соединения: {e}")


@dp.message(Command("budget"))
async def cmd_budget(message: Message):
    chat_id = message.chat.id
    token = user_tokens.get(chat_id)
    if not token:
        await message.answer("Сначала войдите: /login или /register")
        return

    # Пытаемся распарсить аргумент (если пользователь передал сумму)
    args = message.text.split(maxsplit=1)
    if len(args) == 2:
        try:
            amount = float(args[1].replace(",", "."))
            result, status = await call_api("POST", "/api/budget", {"amount": amount}, token=token)
            if status == 200:
                await message.answer(result.get("message", "Бюджет установлен"))
            else:
                await message.answer(f"Ошибка: {result.get('detail', result)}")
            return
        except ValueError:
            await message.answer("Формат: /budget 10000 (сумма в рублях)")
            return  # добавлено, чтобы не показывать текущий бюджет при ошибке

    # Если вызова не было — просто показываем текущий бюджет
    result, status = await call_api("GET", "/api/budget", token=token)
    if status == 200:
        data = result
        if data.get("budget") is None:
            await message.answer("У вас нет бюджета на этот месяц. Установите через /budget 10000 (сумма)")
        else:
            spent = data.get("spent", 0)
            remaining = data.get("remaining", 0)
            budget = data.get("budget", 0)
            msg = (
                f"💰 Бюджет на месяц: {budget} ₽\n"
                f"Потрачено: {spent} ₽\n"
                f"Осталось: {remaining} ₽"
            )
            if remaining < 0:
                msg += "\n⚠️ Вы превысили бюджет!"
            await message.answer(msg)
    else:
        await message.answer(f"Ошибка: {result.get('detail', result)}")


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    chat_id = message.chat.id
    token = user_tokens.get(chat_id)
    if not token:
        await message.answer("Вы не вошли в аккаунт. Используйте /login")
        return

    try:
        result, status = await call_api("GET", "/api/profile", token=token)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка соединения: {e}")
        return

    if status == 200:
        email = result.get("email", "—")
        premium = result.get("is_premium", False)
        premium_until = result.get("premium_until")

        premium_status = "✅ Премиум активен" if premium else "⬜️ Бесплатный аккаунт"
        text = f"👤 Ваш профиль:\n📧 Email: {email}\n💎 Статус: {premium_status}"

        if premium_until:
            # Преобразуем ISO-дату в читаемый вид
            try:
                date_obj = datetime.fromisoformat(premium_until.replace("Z", ""))
                text += f"\n📅 Действует до: {date_obj.strftime('%d.%m.%Y')}"
            except Exception:
                text += f"\n📅 Действует до: {premium_until}"

        if not premium:
            text += "\n\nХотите больше функций? /premium — оформить подписку"

        await message.answer(text)
    else:
        await message.answer(f"Ошибка: {result.get('detail', result)}")


# Временно заглушка для /premium (в следующем шаге подключим Telegram Stars)
@dp.message(Command("premium"))
async def cmd_premium(message: Message):
    chat_id = message.chat.id
    token = user_tokens.get(chat_id)
    if not token:
        await message.answer("Сначала войдите: /login или /register")
        return

    # Проверяем, активна ли уже подписка
    try:
        result, status = await call_api("GET", "/api/profile", token=token)
        if status == 200 and result.get("is_premium"):
            until = result.get("premium_until") or "долго"
            date_str = until[:10] if until else "?"
            await message.answer(f"✅ У вас уже есть премиум до {date_str}. Спасибо!")
            return
    except Exception as e:
        await message.answer(f"⚠️ Ошибка проверки: {e}")
        return

    await message.answer(
        "💎 Premium-подписка скоро будет доступна!\n"
        "Она откроет все функции без ограничений и AI-помощника."
    )


# ========== ОБРАБОТЧИК СОСТОЯНИЙ ==========
@dp.message()
async def handle_states(message: Message):
    chat_id = message.chat.id
    if message.text and message.text.startswith("/"):
        return  # команды обрабатываются выше

    state_data_obj = get_state(chat_id)
    if not state_data_obj:
        await message.answer("Используйте /start для начала работы.")
        return

    state = state_data_obj["state"]
    text = message.text.strip()
    print(f"DEBUG: chat={chat_id}, state={state}, text={text}")

    # ---- РЕГИСТРАЦИЯ ----
    if state == "reg_email":
        if not re.match(r"[^@]+@[^@]+\.[^@]+", text):
            await message.answer("Введите корректный email, например test@mail.ru:")
            return
        set_state(chat_id, "reg_pass", {"email": text})
        await message.answer("Теперь введите пароль (минимум 6 символов):")

    elif state == "reg_pass":
        if len(text) < 6:
            await message.answer("Пароль слишком короткий. Минимум 6 символов.")
            return
        data = state_data_obj["data"]
        set_state(chat_id, "reg_pass2", {**data, "password": text})
        await message.answer("Повторите пароль:")

    elif state == "reg_pass2":
        data = state_data_obj["data"]
        if text != data["password"]:
            await message.answer("Пароли не совпадают. Введите пароль ещё раз:")
            set_state(chat_id, "reg_pass", {"email": data["email"]})
            return

        payload = {"email": data["email"], "password": data["password"]}
        try:
            result, status = await call_api("POST", "/auth/register", payload)
            if status in (200, 201):
                clear_state(chat_id)
                await message.answer("✅ Аккаунт создан! Теперь войдите через /login")
            else:
                await message.answer(f"Ошибка регистрации: {result.get('detail', result)}")
        except Exception as e:
            await message.answer(f"Ошибка соединения с сервером: {e}")
        # Сбрасываем состояние при любом исходе
        clear_state(chat_id)

    # ---- ЛОГИН ----
    elif state == "login_email":
        set_state(chat_id, "login_pass", {"username": text})
        await message.answer("Введите пароль:")

    elif state == "login_pass":
        data = state_data_obj["data"]
        login_payload = {"username": data["username"], "password": text}
        try:
            result, status = await call_api("POST", "/auth/login", login_payload, form=True)
            if status == 200:
                token = result.get("access_token")
                if token:
                    user_tokens[chat_id] = token
                    clear_state(chat_id)
                    await message.answer("✅ Вы вошли в аккаунт!")
                else:
                    await message.answer("Ошибка: сервер не вернул токен.")
            else:
                await message.answer(f"Ошибка: {result.get('detail', result)}")
        except Exception as e:
            await message.answer(f"Ошибка соединения: {e}")
        clear_state(chat_id)

    # ---- ДОБАВЛЕНИЕ ТРАНЗАКЦИИ ----
    elif state == "add_transaction":
        token = user_tokens.get(chat_id)
        if not token:
            await message.answer("Сначала войдите: /login или /register")
            clear_state(chat_id)
            return

        lower_text = text.lower()
        try:
            if lower_text.startswith("расход"):
                parts = text.split(maxsplit=2)
                if len(parts) < 3:
                    raise ValueError()
                amount = float(parts[1].replace(",", "."))
                category = parts[2]
            elif lower_text.startswith("доход"):
                parts = text.split(maxsplit=2)
                if len(parts) < 3:
                    raise ValueError()
                amount = -float(parts[1].replace(",", "."))
                category = parts[2]
            else:
                # формат: "сумма категория"
                amount_str, category = text.split(maxsplit=1)
                amount = float(amount_str.replace(",", "."))
        except (IndexError, ValueError):
            await message.answer("Не понял формат. Пример: `500 продукты`, `-500 фриланс`, `расход 500 продукты`")
            return

        category = category.strip()[:50]  # ограничим длину
        payload = {"amount": amount, "category": category, "comment": None}
        try:
            result, status = await call_api("POST", "/api/transactions", payload, token=token)
            if status == 200:
                direction = "доход" if amount < 0 else "расход"
                await message.answer(f"✅ Добавлено: {direction} {abs(amount)} ₽ в категории «{category}»")
            else:
                await message.answer(f"Ошибка: {result.get('detail', result)}")
        except Exception as e:
            await message.answer(f"Ошибка соединения: {e}")
        clear_state(chat_id)


# ========== ТОЧКА ВХОДА (не используется при импорте в main.py) ==========
async def main():
    logging.basicConfig(level=logging.INFO)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())