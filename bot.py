import logging
import json
import os
import time
from collections import defaultdict
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
    JobQueue
)
import asyncio
import aiohttp
import telegram.error
from config import *

# Состояния для ConversationHandler
AWAITING_WITHDRAWAL = 1
AWAITING_TASK_INPUT = 2

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Глобальный массив для хранения доступных заданий
available_tasks = []

# Глобальный список всех special_tasks с общими счётчиками активаций
special_tasks = [
    {
        "link": "https://t.me/tonquestschannel",
        "reward": 1.00,
        "max_activations": 1000000,
        "current_activations": 0,
        "task_id": 1
    }
]
next_task_id = 2

def normalize_link(raw_link: str) -> str:
    """Приводит ссылки к единому формату для API-заданий."""
    base_link = raw_link.split('?')[0]
    base_link = base_link.replace("https://t.me//", "https://t.me/+")
    if not base_link.startswith("https://t.me/+"):
        base_link = base_link.replace("https://t.me/", "https://t.me/+", 1)
    base_link = base_link.rstrip('/')
    while "++" in base_link:
        base_link = base_link.replace("++", "+")
    return base_link

def extract_chat_id(link: str) -> str:
    """Извлекает chat_id из ссылки Telegram."""
    if link.startswith("https://t.me/+"):
        return link
    elif link.startswith("https://t.me/"):
        return "@" + link.split("https://t.me/")[1].split("/")[0]
    return link

def load_users_data():
    """Загружает данные пользователей из файла с миграцией старых данных."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            data = {int(k): v for k, v in data.items()}
            for user_id in data:
                new_tasks = {}
                for link in data[user_id].get("tasks", {}):
                    fixed_link = link
                    task_data = data[user_id]["tasks"][link]
                    if "permanently_completed" not in task_data:
                        task_data["permanently_completed"] = False
                    new_tasks[fixed_link] = task_data
                data[user_id]["tasks"] = new_tasks
                if "special_task" in data[user_id] and isinstance(data[user_id]["special_task"], dict):
                    data[user_id]["special_tasks"] = [data[user_id]["special_task"]]
                    del data[user_id]["special_task"]
            return defaultdict(lambda: {
                "balance": 0.00,
                "tasks": {},
                "referrals": 0,
                "referral_code": None,
                "total_earned": 0.00,
                "referral_earnings": 0.00,
                "level": 1,
                "xp": 0,
                "used_referral": False,
                "referrer_id": None,
                "last_check": 0,
                "chat_id": None,
                "last_notification": 0,
                "special_tasks": []
            }, data)
    return defaultdict(lambda: {
        "balance": 0.00,
        "tasks": {},
        "referrals": 0,
        "referral_code": None,
        "total_earned": 0.00,
        "referral_earnings": 0.00,
        "level": 1,
        "xp": 0,
        "used_referral": False,
        "referrer_id": None,
        "last_check": 0,
        "chat_id": None,
        "last_notification": 0,
        "special_tasks": []
    })

def save_users_data():
    """Сохраняет данные пользователей в файл."""
    with open(DATA_FILE, "w") as f:
        data = {str(k): dict(v) for k, v in users_data.items()}
        json.dump(data, f, indent=4, ensure_ascii=False)

users_data = load_users_data()

async def request_op(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, task_link: str = "", max_op: int = 1):
    """Выполняет запрос к API через прокси PythonAnywhere для конкретной задачи."""
    headers = {"Auth": SUBGRAM_API_KEY}
    data = {
        "UserId": str(user_id),
        "ChatId": str(chat_id),
        "TaskLink": task_link,
        "MaxOP": max_op
    }
    proxy = os.getenv('https_proxy')
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SUBGRAM_API_URL, headers=headers, json=data, proxy=proxy) as response:
                response_text = await response.text()
                if response.status == 200:
                    response_data = await response.json()
                    status = response_data.get("status", "warning").lower()
                    logger.info(f"Request_op для UserId {user_id}, TaskLink {task_link}: HTTP статус {response.status}, API статус: {status}, тело: {response_text}")
                    return response_data
                else:
                    logger.warning(f"API вернул статус {response.status} для user_id {user_id}, task_link {task_link}, тело: {response_text}")
                    return None
    except Exception as e:
        logger.warning(f"Ошибка подключения к API для user_id {user_id}, task_link {task_link}: {str(e)}")
        return None

async def update_available_tasks(context: ContextTypes.DEFAULT_TYPE):
    """Обновляет глобальный список доступных заданий через API для всех пользователей."""
    global available_tasks
    try:
        headers = {"Auth": SUBGRAM_API_KEY}
        all_tasks = set()
        logger.info("Начало обновления available_tasks")
        for user_id, user_data in users_data.items():
            chat_id = user_data.get("chat_id")
            if chat_id is None or not isinstance(chat_id, (int, str)):
                chat_id = str(user_id)
            else:
                chat_id = str(chat_id)
            response = await request_op(user_id, chat_id, context, task_link="", max_op=10)
            if response and "links" in response:
                new_tasks = response.get("links", [])
                tasks_list = [(task, normalize_link(task)) for task in new_tasks]
                all_tasks.update(task[1] for task in tasks_list)
                for raw_link, normalized_link in tasks_list:
                    if raw_link not in user_data["tasks"]:
                        user_data["tasks"][raw_link] = {
                            "completed": False,
                            "reward": BASE_REWARD,
                            "status": "warning",
                            "last_checked": int(time.time()),
                            "permanently_completed": False
                        }
        available_tasks = list(all_tasks)
        logger.info(f"Задания обновлены. Всего доступно: {len(available_tasks)}")
        save_users_data()
    except Exception as e:
        logger.error(f"Ошибка при обновлении заданий: {str(e)}")

LEVEL_REWARDS = {
    1: 1.00, 2: 2.00, 3: 3.00, 4: 4.00, 5: 6.00,
    6: 9.00, 7: 12.00, 8: 16.00, 9: 20.00, 10: 25.00
}

async def update_level(user_id: int):
    """Обновляет уровень пользователя на основе опыта."""
    user_data = users_data[user_id]
    xp_needed = user_data["level"] * 100
    if user_data["xp"] >= xp_needed:
        user_data["level"] += 1
        user_data["xp"] -= xp_needed
        reward = LEVEL_REWARDS.get(user_data["level"], 0.00)
        user_data["balance"] += reward
        user_data["total_earned"] += reward
        save_users_data()
        return True
    return False

def create_progress_bar(user, current_xp, max_xp, length=10):
    """Создает строку прогресса для уровня."""
    filled = "▓" * int((current_xp / max_xp) * length)
    empty = "░" * (length - len(filled))
    reward = LEVEL_REWARDS.get(users_data[user.id]["level"], 0.00)
    return f"{filled}{empty} {current_xp}/{max_xp} XP +{reward:.2f}₽"

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отображает профиль пользователя."""
    user = update.effective_user
    user_data = users_data[user.id]
    max_xp = user_data["level"] * 100
    progress_bar = create_progress_bar(user, user_data["xp"], max_xp)
    profile_text = (
        f"💼 *Ваш профиль*\n\n"
        f"💰 Баланс: {user_data['balance']:.2f}₽\n"
        f"🏆 Уровень: {user_data['level']}\n"
        f"🔋 Прогресс:\n`{progress_bar}`\n\n"
        f"👥 Рефералов: {user_data['referrals']}\n"
        f"💸 Заработано с рефералов: {user_data['referral_earnings']:.2f}₽\n"
        f"💵 Всего заработано: {user_data['total_earned']:.2f}₽"
    )
    await update.message.reply_text(text=profile_text, parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ref_code = context.args[0] if context.args else None
    users_data[user.id]["chat_id"] = update.effective_chat.id

    # Обработка реферального кода
    if ref_code and ref_code.startswith("ref") and not users_data[user.id]["used_referral"]:
        referrer_id = int(ref_code[3:])
        if referrer_id in users_data and referrer_id != user.id:
            users_data[referrer_id]["referrals"] += 1
            users_data[referrer_id]["balance"] += REFERRAL_BONUS
            users_data[referrer_id]["referral_earnings"] += REFERRAL_BONUS
            users_data[referrer_id]["total_earned"] += REFERRAL_BONUS
            users_data[user.id]["used_referral"] = True
            users_data[user.id]["referrer_id"] = referrer_id
            referrer_xp = int((users_data[referrer_id]["level"] * 100) * (REFERRAL_XP_PERCENT / 100))
            users_data[referrer_id]["xp"] += referrer_xp
            await update_level(referrer_id)
            await context.bot.send_message(
                chat_id=referrer_id,
                text=f"🎉 Новый реферал! +{REFERRAL_BONUS:.2f}₽ и +{referrer_xp} XP!"
            )

    users_data[user.id]["referral_code"] = f"ref{user.id}"

    # Добавление специальных заданий
    for task in special_tasks:
        if task["current_activations"] < task["max_activations"]:
            if not any(t["task_id"] == task["task_id"] for t in users_data[user.id]["special_tasks"]):
                task_copy = task.copy()
                task_copy["completed"] = False
                users_data[user.id]["special_tasks"].append(task_copy)

    # Обновление обычных заданий для нового пользователя
    chat_id = users_data[user.id]["chat_id"]
    response = await request_op(user.id, chat_id, context, task_link="", max_op=10)
    if response and "links" in response:
        new_tasks = response.get("links", [])
        for raw_link in new_tasks:
            if raw_link not in users_data[user.id]["tasks"]:
                users_data[user.id]["tasks"][raw_link] = {
                    "completed": False,
                    "reward": BASE_REWARD,
                    "status": "warning",
                    "last_checked": int(time.time()),
                    "permanently_completed": False
                }

    save_users_data()

    # Отправка приветственного сообщения
    buttons = [
        [KeyboardButton("🎯 Задания"), KeyboardButton("👤 Профиль")],
        [KeyboardButton("👥 Рефералы"), KeyboardButton("💳 Вывод")],
        [KeyboardButton("📞 Связь")]
    ]
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Привет, {user.first_name}! 🚀\nВыполняй задания и получай деньги!",
        reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    )

async def handle_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает реферальную систему."""
    user = update.effective_user
    user_data = users_data[user.id]
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref{user.id}"
    text = (
        r"👥 *Реферальная система*" + "\n\n"
        r"🔗 Ваша ссылка: `" + ref_link.replace(".", r"\.") + r"`" + "\n\n"
        r"💎 За каждого приглашенного\:" + "\n"
        r"• \+" + f"{REFERRAL_BONUS:.2f}".replace(".", r"\.") + r"₽ на баланс" + "\n"
        r"• \+" + f"{REFERRAL_XP_PERCENT}\\% опыта" + "\n\n"
        r"📊 *Статистика\:*" + "\n"
        r"👥 Приглашено\: " + f"{user_data['referrals']}" + "\n"
        r"💸 Заработано\: " + f"{user_data['referral_earnings']:.2f}".replace(".", r"\.") + r"₽"
    )
    keyboard = [
        [InlineKeyboardButton("🔗 Поделиться ссылкой", url=f"https://t.me/share/url?url={ref_link}")]
    ]
    await update.message.reply_text(
        text,
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_withdrawal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает процесс вывода средств."""
    user_data = users_data[update.effective_user.id]
    if user_data["balance"] < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"🚫 Минимальная сумма вывода: {MIN_WITHDRAWAL:.2f}₽\n"
            f"Ваш баланс: {user_data['balance']:.2f}₽"
        )
        return ConversationHandler.END
    await update.message.reply_text(
        f"На данном этапе разработки выплаты принимаются вручную и осуществляются через CryptoBot\n\n"
        f"💳 Введите сумму для вывода, и ждите ответа от администратора.\n"
        f"Ваш баланс: {user_data['balance']:.2f}₽"
    )
    return AWAITING_WITHDRAWAL

async def handle_withdrawal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает сумму вывода."""
    user = update.effective_user
    user_data = users_data[user.id]
    try:
        amount = float(update.message.text)
        if amount < MIN_WITHDRAWAL:
            await update.message.reply_text(f"🚫 Минимальная сумма: {MIN_WITHDRAWAL:.2f}₽")
            return ConversationHandler.END
        if amount > user_data["balance"]:
            await update.message.reply_text(f"🚫 Недостаточно средств. Баланс: {user_data['balance']:.2f}₽")
            return ConversationHandler.END
        user_data["balance"] -= amount
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"📥 Новая заявка на вывод:\n\n"
                 f"👤 Пользователь: @{user.username}\n"
                 f"💳 Сумма: {amount:.2f}₽"
        )
        await update.message.reply_text(f"✅ Заявка на {amount:.2f}₽ отправлена!")
        save_users_data()
    except ValueError:
        await update.message.reply_text("❌ Введите корректную сумму")
    return ConversationHandler.END

async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает процесс добавления нового задания администратором."""
    ADMIN_ID = 992930870
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Эта команда доступна только администратору!")
        return ConversationHandler.END
    await update.message.reply_text(
        "Введите данные нового задания в формате:\n"
        "ссылка количество_активаций цена\n"
        "Пример: https://t.me/examplechat 100 2.50"
    )
    return AWAITING_TASK_INPUT

async def handle_task_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод данных нового задания."""
    global special_tasks, next_task_id
    ADMIN_ID = 992930870
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    try:
        text = update.message.text.strip().split()
        if len(text) != 3:
            await update.message.reply_text("❌ Неверный формат! Используйте: ссылка количество_активаций цена")
            return AWAITING_TASK_INPUT
        link = text[0]
        if not link.startswith("https://t.me/"):
            await update.message.reply_text("❌ Ссылка должна начинаться с https://t.me/")
            return AWAITING_TASK_INPUT
        activations = int(text[1])
        if activations <= 0:
            await update.message.reply_text("❌ Количество активаций должно быть положительным!")
            return AWAITING_TASK_INPUT
        price = float(text[2])
        if price <= 0:
            await update.message.reply_text("❌ Цена должна быть положительной!")
            return AWAITING_TASK_INPUT
        new_task = {
            "link": link,
            "reward": price,
            "max_activations": activations,
            "current_activations": 0,
            "task_id": next_task_id
        }
        special_tasks.append(new_task)
        next_task_id += 1
        added_count = 0
        for user_id in users_data:
            if not any(t["task_id"] == new_task["task_id"] for t in users_data[user_id]["special_tasks"]):
                task_copy = new_task.copy()
                task_copy["completed"] = False
                users_data[user_id]["special_tasks"].append(task_copy)
                added_count += 1
        if added_count == 0:
            task_copy = new_task.copy()
            task_copy["completed"] = False
            users_data[ADMIN_ID]["special_tasks"].append(task_copy)
            added_count = 1
        save_users_data()
        await update.message.reply_text(
            f"✅ Задание успешно добавлено!\n"
            f"Ссылка: {link}\n"
            f"Активаций: {activations}\n"
            f"Цена: {price}₽\n"
            f"Добавлено пользователям: {added_count}"
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Ошибка в числах! Используйте формат: ссылка количество_активаций цена")
        return AWAITING_TASK_INPUT
    except Exception as e:
        await update.message.reply_text(f"❌ Произошла ошибка: {str(e)}")
        return AWAITING_TASK_INPUT

async def handle_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отображает задания пользователя с общей суммой наград."""
    global available_tasks
    user = update.effective_user
    user_data = users_data[user.id]
    
    message = await update.message.reply_text("🔄 Загрузка заданий...")
    
    chat_id = user_data.get("chat_id", str(user.id))
    response = await request_op(user.id, chat_id, context, task_link="", max_op=10)
    
    if response and "links" in response:
        new_tasks = response.get("links", [])
        tasks_list = [(task, normalize_link(task)) for task in new_tasks]
        available_tasks = [task[1] for task in tasks_list]
        
        for raw_link, normalized_link in tasks_list:
            if raw_link not in user_data["tasks"]:
                user_data["tasks"][raw_link] = {
                    "completed": False,
                    "reward": BASE_REWARD,
                    "status": "warning",
                    "last_checked": int(time.time()),
                    "permanently_completed": False
                }

    # Рассчитываем общую сумму наград за все невыполненные задания
    total_reward = 0.0
    for link, task in user_data["tasks"].items():
        if not task.get("completed", False) and not task.get("permanently_completed", False):
            total_reward += task.get("reward", BASE_REWARD)
    
    keyboard = []
    DEFAULT_CHANNEL = "https://t.me/tonquestschannel"
    
    # Отображаем special_tasks
    for special_task in user_data["special_tasks"]:
        global_task = next(t for t in special_tasks if t["task_id"] == special_task["task_id"])
        if not special_task.get("completed", False) and global_task["current_activations"] < global_task["max_activations"]:
            display_text = "🌟 Наш канал" if special_task["link"] == DEFAULT_CHANNEL else f"🌟 Канал (+{special_task['reward']}₽)"
            keyboard.append([
                InlineKeyboardButton(text=display_text, url=special_task["link"])
            ])
            if not special_task.get("completed", False):
                total_reward += special_task["reward"]
    
    # Отображаем tasks
    for link, task in user_data["tasks"].items():
        if not task.get("permanently_completed", False):
            display_text = "🌟 Наш канал" if link == DEFAULT_CHANNEL else f"🌟 Канал (+{task['reward']:.2f}₽)"
            keyboard.append([
                InlineKeyboardButton(text=display_text, url=link)
            ])
    
    if keyboard:
        keyboard.append([InlineKeyboardButton("✅ Проверить все задания", callback_data="check_all_tasks")])
        keyboard.append([InlineKeyboardButton("🔄 Обновить задания", callback_data="refresh_tasks")])
        await message.edit_text(
            f"📝 Ваши задания:\n💰 Вы заработаете {total_reward:.2f}₽, выполнив все задания!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await message.edit_text("🚫 Нет доступных заданий.")
    save_users_data()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки."""
    global special_tasks
    query = update.callback_query
    user = query.from_user
    user_data = users_data[user.id]
    
    try:
        await query.answer()
        
        if query.data == "refresh_tasks":
            await handle_tasks(update, context)
            return
        
        if query.data.startswith("check_special_"):
            task_id = int(query.data.split("_")[2])
            special_task = next((t for t in user_data["special_tasks"] if t["task_id"] == task_id), None)
            global_task = next((t for t in special_tasks if t["task_id"] == task_id), None)
            
            if not special_task or not global_task:
                await query.edit_message_text("❌ Задание не найдено!")
                return
                
            if special_task["completed"]:
                await query.edit_message_text("❌ Это задание уже выполнено вами!")
                return
                
            if global_task["current_activations"] >= global_task["max_activations"]:
                await query.edit_message_text("❌ Это задание больше недоступно - лимит активаций достигнут!")
                user_data["special_tasks"] = [t for t in user_data["special_tasks"] if t["task_id"] != task_id]
                save_users_data()
                return
                
            chat_id = extract_chat_id(special_task["link"])
            try:
                member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user.id)
                if member.status in ["member", "administrator", "creator"]:
                    reward = special_task["reward"]
                    user_data["balance"] += reward
                    user_data["total_earned"] += reward
                    XP_PER_TASK = 4
                    user_data["xp"] += XP_PER_TASK
                    special_task["completed"] = True
                    
                    global_task["current_activations"] += 1
                    
                    for uid in users_data:
                        for user_task in users_data[uid]["special_tasks"]:
                            if user_task["task_id"] == task_id:
                                user_task["current_activations"] = global_task["current_activations"]
                    
                    referrer_id = user_data.get("referrer_id")
                    if referrer_id and referrer_id in users_data:
                        referral_bonus = round(reward * 0.15, 2)
                        users_data[referrer_id]["balance"] += referral_bonus
                        users_data[referrer_id]["referral_earnings"] += referral_bonus
                        users_data[referrer_id]["total_earned"] += referral_bonus
                        await context.bot.send_message(
                            chat_id=referrer_id,
                            text=f"🎉 Ваш реферал выполнил специальное задание! +{referral_bonus:.2f}₽ (15%)"
                        )
                    
                    level_up = await update_level(user.id)
                    message_text = f"✅ Вы подписаны! +{reward:.2f}₽ +{XP_PER_TASK}XP"
                    if level_up:
                        message_text += f"\n🎉 Новый уровень: {user_data['level']}!"
                    await query.edit_message_text(message_text)
                    
                    if global_task["current_activations"] >= global_task["max_activations"]:
                        special_tasks = [t for t in special_tasks if t["task_id"] != task_id]
                        for uid in users_data:
                            users_data[uid]["special_tasks"] = [t for t in users_data[uid]["special_tasks"] if t["task_id"] != task_id]
                    
                    save_users_data()
                else:
                    await query.edit_message_text(
                        "❌ Задание не выполнено: вы не подписаны на канал!",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔔 Подписаться", url=special_task["link"]),
                                                            InlineKeyboardButton("Проверить снова", callback_data=f"check_special_{task_id}")]])
                    )
            except telegram.error.BadRequest as e:
                await query.edit_message_text(f"❌ Ошибка: {str(e)}. Проверьте правильность ссылки на канал!")
            save_users_data()
            return
        
        if query.data == "check_all_tasks":
            await query.answer("⏳ Проверяем выполнение всех заданий...")
            original_message = query.message
            original_keyboard = original_message.reply_markup.inline_keyboard
            new_keyboard = []
            for row in original_keyboard:
                new_row = [InlineKeyboardButton(text="🔄 Проверка...", callback_data=btn.callback_data) if btn.callback_data == "check_all_tasks" else btn for btn in row]
                new_keyboard.append(new_row)
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
            await asyncio.sleep(0.5)
            
            response = await request_op(user.id, query.message.chat.id, context, task_link="", max_op=10)
            if not response or "status" not in response:
                await query.answer("🔴 Ошибка API", show_alert=True)
                return
            
            XP_PER_TASK = 4
            total_reward = 0.0
            completed_tasks = 0
            
            if response["status"].lower() == "ok":
                for link, task in user_data["tasks"].items():
                    if not task.get("completed", False) and not task.get("permanently_completed", False):
                        reward = task.get("reward", BASE_REWARD)
                        total_reward += reward
                        task["completed"] = True
                        task["permanently_completed"] = True
                        task["status"] = "ok"
                        completed_tasks += 1
                if total_reward > 0:
                    user_data["balance"] = round(user_data["balance"] + total_reward, 2)
                    user_data["total_earned"] = round(user_data["total_earned"] + total_reward, 2)
                    user_data["xp"] += XP_PER_TASK * completed_tasks
                    level_up = await update_level(user.id)
                    message_text = f"✅ Все задания выполнены!\n+{total_reward:.2f}₽ +{XP_PER_TASK * completed_tasks}XP"
                    if level_up:
                        message_text += f"\n🎉 Новый уровень: {user_data['level']}!"
                    await query.edit_message_text(text=message_text, reply_markup=None)
                    referrer_id = user_data.get("referrer_id")
                    if referrer_id and referrer_id in users_data:
                        referral_bonus = round(total_reward * 0.15, 2)
                        users_data[referrer_id]["balance"] += referral_bonus
                        users_data[referrer_id]["referral_earnings"] += referral_bonus
                        users_data[referrer_id]["total_earned"] += referral_bonus
                        await context.bot.send_message(
                            chat_id=referrer_id,
                            text=f"🎉 Ваш реферал выполнил задания! +{referral_bonus:.2f}₽ (15%)"
                        )
            else:
                await query.edit_message_text(
                    text=f"❌ Не все задания выполнены!\n{response.get('message', 'Требуется подписка на все каналы')}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 К заданиям", callback_data="refresh_tasks")]])
                )
            save_users_data()

    except Exception as e:
        logger.error(f"Ошибка в callback для user_id {user.id}: {str(e)}", exc_info=True)
        await query.answer("⚡ Техническая ошибка", show_alert=True)
    finally:
        save_users_data()





async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает запросы на связь с администратором."""
    keyboard = [[InlineKeyboardButton("📱 Написать администратору", url="https://t.me/nikon_gd")]]
    await update.message.reply_text("📞 Связь с администратором:\nПо всем вопросам пишите @nikon_gd", reply_markup=InlineKeyboardMarkup(keyboard))
async def check_new_tasks(context: ContextTypes.DEFAULT_TYPE):
    async def check_user_tasks(user_id, chat_id, user_data):
        try:
            # Запрашиваем персональные задания через API
            response = await request_op(user_id, chat_id, context, task_link="", max_op=10)
            if response and "links" in response:
                new_tasks = response.get("links", [])
                # Фильтруем только те задания, которых нет или они не выполнены навсегда
                actual_new_tasks = [task for task in new_tasks if task not in user_data["tasks"] or not user_data["tasks"][task].get("permanently_completed", False)]
                
                # Проверяем специальные задания (если они есть в твоём коде)
                existing_special_task_ids = {t["task_id"] for t in user_data.get("special_tasks", [])}
                new_special_tasks = [t for t in special_tasks if t["task_id"] not in existing_special_task_ids and t["current_activations"] < t["max_activations"]]
                
                # Если есть новые задания
                if actual_new_tasks or new_special_tasks:
                    current_time = time.time()
                    last_notification = user_data.get("last_notification", 0)
                    # Уведомляем не чаще, чем раз в час
                    if current_time - last_notification >= 3600:
                        message_text = f"✨ У вас есть {len(actual_new_tasks) + len(new_special_tasks)} новых заданий!\nНажмите '🎯 Задания', чтобы посмотреть."
                        await context.bot.send_message(chat_id=chat_id, text=message_text)
                        user_data["last_notification"] = current_time
                        # Добавляем новые специальные задания
                        for new_task in new_special_tasks:
                            if not any(t["task_id"] == new_task["task_id"] for t in user_data["special_tasks"]):
                                task_copy = new_task.copy()
                                task_copy["completed"] = False
                                user_data["special_tasks"].append(task_copy)
                        save_users_data()  # Сохраняем данные
        except telegram.error.Forbidden:
            # Если пользователь заблокировал бота
            del users_data[user_id]
            save_users_data()
        except Exception as e:
            logger.error(f"Ошибка для пользователя {user_id}: {str(e)}")

    # Запускаем проверку для всех пользователей
    tasks = [check_user_tasks(user_id, users_data[user_id].get("chat_id", user_id), users_data[user_id]) for user_id in list(users_data.keys())]
    await asyncio.gather(*tasks)
    
    
    

def main():
    """Запускает бота."""
    application = ApplicationBuilder().token(TOKEN).build()
    job_queue = application.job_queue
    job_queue.run_repeating(update_available_tasks, interval=900, first=10)
    job_queue.run_repeating(check_new_tasks, interval=910, first=10)
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("💳 Вывод"), handle_withdrawal_start),
            CommandHandler("addtask", add_task_start)
        ],
        states={
            AWAITING_WITHDRAWAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdrawal_amount)],
            AWAITING_TASK_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_input)]
        },
        fallbacks=[]
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("🎯 Задания"), handle_tasks))
    application.add_handler(MessageHandler(filters.Regex("👤 Профиль"), show_profile))
    application.add_handler(MessageHandler(filters.Regex("👥 Рефералы"), handle_referrals))
    application.add_handler(MessageHandler(filters.Regex("📞 Связь"), handle_contact))
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_callback))
    application.run_polling()

if __name__ == "__main__":
    main()
