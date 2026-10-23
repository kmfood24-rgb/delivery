import os
import re
import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    ApplicationHandlerStop,
    filters,
)
from telegram.error import Forbidden, RetryAfter

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# Не писати в логи адреси запитів до Telegram — у них міститься токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)

# ==== НАЛАШТУВАННЯ (з environment variables) ====
BOT_TOKEN = os.environ["BOT_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]  # вміст JSON-ключа service account одним рядком

# Хто може робити розсилку: chat_id через кому (беруться з колонки A аркуша Users).
# Якщо змінну не задано — розсилка вимкнена, решта бота працює як завжди.
ADMIN_CHAT_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_CHAT_IDS", "")) if x.strip().lstrip("-").isdigit()
}

SHEET_ORDERS = "Замовлення"
SHEET_USERS = "Users"

# Час замовлень записуємо за Києвом (сервер Railway працює в UTC)
TIMEZONE = ZoneInfo("Europe/Kyiv")

# Скільки попередніх закладів показувати кнопками
MAX_ZAKLAD_BUTTONS = 6

# Стани розмови
ASK_NAME, ASK_PHONE, ASK_ZAKLAD, ASK_ORDER, ASK_COMMENT, CONFIRM, EDIT_CHOICE = range(7)

# ==== ТЕКСТИ КНОПОК ====
BTN_NEW_ORDER = "🛒 Зробити замовлення"
BTN_OLD_NEW_ORDER = "🔄 Нове замовлення"  # стара кнопка, може залишитись у людей на екрані
BTN_REPEAT = "🔁 Повторити минуле замовлення"
BTN_CANCEL = "❌ Скасувати"
BTN_SHARE_CONTACT = "📱 Надіслати мій номер"
BTN_NO_COMMENT = "Без коментаря"
BTN_CONFIRM = "✅ Все вірно, відправити"
BTN_EDIT = "✏️ Виправити"
BTN_EDIT_ZAKLAD = "Заклад"
BTN_EDIT_ORDER = "Замовлення"
BTN_EDIT_COMMENT = "Коментар"
BTN_NEXT = "➡️ Далі"

# ==== ТЕКСТИ ПІДКАЗОК ====
TEXT_ASK_ZAKLAD_NEW = (
    "Крок 1 з 3. Напишіть лише назву вашого закладу, одним рядком.\n"
    "Товари будемо писати на наступному кроці.\n\n"
    "Наприклад: Лофт 26"
)
TEXT_ASK_ZAKLAD_CHOOSE = (
    "Крок 1 з 3. Оберіть заклад кнопкою внизу 👇\n\n"
    "Якщо потрібен інший заклад — напишіть лише його назву, одним рядком."
)
TEXT_ASK_ORDER = (
    "Крок 2 з 3. Напишіть, що потрібно привезти.\n\n"
    "Кожен товар — з нового рядка. Для кожного вкажіть:\n"
    "• що саме: вид, сорт, жирність\n"
    "• скільки: кг, шт, ящиків, пачок\n\n"
    "Наприклад:\n"
    "Цибуля зелена — 0,3 кг\n"
    "Капуста пекінська — 2 кг\n"
    "Молоко 2,5% — 1 ящик\n"
    "Вершки 33%, по 1 л — 6 шт\n"
    "Сир моцарела — 1 кг\n"
    "Лайм — 5 шт\n\n"
    "Можна надіслати кількома повідомленнями — бот усе збере.\n"
    f"Коли закінчите, натисніть «{BTN_NEXT}» внизу."
)
TEXT_ASK_COMMENT = (
    "Крок 3 з 3. Є побажання до замовлення? Наприклад, зручний час доставки "
    "або чим замінити товар, якщо його не буде.\n\n"
    f"Напишіть їх або натисніть «{BTN_NO_COMMENT}» 👇"
)

NO_COMMENT_VALUE = "-"  # що записується в таблицю, якщо коментаря немає


# ==== ПІДКЛЮЧЕННЯ ДО GOOGLE SHEETS ====
def get_sheets_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


gc = get_sheets_client()
spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)
orders_sheet = spreadsheet.worksheet(SHEET_ORDERS)
users_sheet = spreadsheet.worksheet(SHEET_USERS)


def clean_lines(text: str) -> str:
    """Прибирає пробіли на початку і в кінці кожного рядка та порожні рядки."""
    return "\n".join(l.strip() for l in text.splitlines() if l.strip())


def digits_only(value) -> str:
    return re.sub(r"\D", "", str(value))


def get_user(chat_id: int):
    records = users_sheet.get_all_values()
    for row in records[1:]:
        if row and str(row[0]) == str(chat_id):
            return {"name": row[1], "phone": row[2]}
    return None


def save_user(chat_id: int, name: str, phone: str):
    records = users_sheet.get_all_values()
    for i, row in enumerate(records[1:], start=2):
        if row and str(row[0]) == str(chat_id):
            users_sheet.update(f"A{i}:C{i}", [[chat_id, name, phone]])
            return
    users_sheet.append_row([chat_id, name, phone])


def get_user_orders(phone: str):
    """Усі замовлення цього телефону з аркуша «Замовлення», від старих до нових.
    Колонки: A дата, B ім'я, C телефон, D заклад, E замовлення, F коментар."""
    target = digits_only(phone)
    if not target:
        return []
    result = []
    for row in orders_sheet.get_all_values()[1:]:
        row = row + [""] * (6 - len(row))
        if digits_only(row[2]) == target:
            result.append({"zaklad": row[3], "order": row[4], "comment": row[5]})
    return result


def get_recent_zaklady(phone: str):
    """Заклади, на які людина вже замовляла, — від останнього до старших, без повторів."""
    seen = []
    for o in reversed(get_user_orders(phone)):
        z = o["zaklad"].strip()
        if z and z not in seen:
            seen.append(z)
        if len(seen) >= MAX_ZAKLAD_BUTTONS:
            break
    return seen


def write_order(name, phone, zaklad, order, comment):
    orders_sheet.append_row(
        [
            datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
            name,
            phone,
            zaklad,
            order,
            comment,
        ]
    )


def normalize_typed_phone(text: str):
    """Приймає номер, набраний вручну: 0671234567, +380671234567, 067 123 45 67 тощо.
    Повертає 380XXXXXXXXX (як у таблиці) або None, якщо номер не схожий на український."""
    d = digits_only(text)
    if len(d) == 10 and d.startswith("0"):
        d = "38" + d
    if len(d) == 12 and d.startswith("380"):
        return d
    return None


# ==== КЛАВІАТУРИ ====
def keyboard(rows):
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [[BTN_NEW_ORDER], [BTN_REPEAT]],
        resize_keyboard=True,
        is_persistent=True,
    )


def cancel_keyboard():
    return keyboard([[BTN_CANCEL]])


def contact_keyboard():
    return keyboard(
        [[KeyboardButton(BTN_SHARE_CONTACT, request_contact=True)], [BTN_CANCEL]]
    )


def zaklad_keyboard(zaklady):
    rows = [[z] for z in zaklady]
    rows.append([BTN_CANCEL])
    return keyboard(rows)


def comment_keyboard():
    return keyboard([[BTN_NO_COMMENT], [BTN_CANCEL]])


def confirm_keyboard():
    return keyboard([[BTN_CONFIRM], [BTN_EDIT], [BTN_CANCEL]])


def edit_keyboard():
    return keyboard([[BTN_EDIT_ZAKLAD], [BTN_EDIT_ORDER], [BTN_EDIT_COMMENT], [BTN_CANCEL]])


def order_keyboard():
    return keyboard([[BTN_NEXT], [BTN_CANCEL]])


# ==== ДОПОМІЖНІ ====
# Рядок схожий на товар, якщо в ньому є число з одиницею виміру: «2кг», «0.300 г», «5 шт», «1ящ»
ITEM_PATTERN = re.compile(
    r"\d[\d.,]*\s*-?\s*(?:"
    r"(?:кг|гр|г|л|мл)(?![а-яіїєґa-z])"  # короткі одиниці — лише окремим словом
    r"|(?:шт|ящ|пач|уп|бан|пак|пучк|лот)[а-яіїєґ]*"  # «ящ», «ящики», «пачки»...
    r")",
    re.IGNORECASE,
)


def looks_like_item(text: str) -> bool:
    return bool(ITEM_PATTERN.search(text))


def greeting(name: str = "") -> str:
    """Привітання за київським часом: «Доброго ранку, Олена! 👋»."""
    hour = datetime.now(TIMEZONE).hour
    if 5 <= hour < 11:
        hello = "Доброго ранку"
    elif 11 <= hour < 17:
        hello = "Доброго дня"
    elif 17 <= hour < 23:
        hello = "Доброго вечора"
    else:
        hello = "Доброї ночі"
    return f"{hello}, {name}! 👋" if name else f"{hello}! 👋"


def order_summary(data) -> str:
    comment = data.get("comment") or NO_COMMENT_VALUE
    comment_text = "немає" if comment == NO_COMMENT_VALUE else comment
    return (
        f"🏠 Заклад: {data.get('zaklad', '')}\n\n"
        f"📦 Замовлення:\n{data.get('order', '')}\n\n"
        f"💬 Коментар: {comment_text}"
    )


def clear_order(context):
    for key in ("zaklad", "order", "order_parts", "comment", "editing"):
        context.user_data.pop(key, None)


async def load_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Підтягує ім'я і телефон з таблиці. Повертає True, якщо людина вже зареєстрована."""
    user = get_user(update.effective_chat.id)
    if user:
        context.user_data["name"] = user["name"]
        context.user_data["phone"] = user["phone"]
        return True
    return False


async def go_to_zaklad(update: Update, context: ContextTypes.DEFAULT_TYPE, intro: str = ""):
    zaklady = get_recent_zaklady(context.user_data.get("phone", ""))
    if zaklady:
        text = TEXT_ASK_ZAKLAD_CHOOSE
        markup = zaklad_keyboard(zaklady)
    else:
        text = TEXT_ASK_ZAKLAD_NEW
        markup = cancel_keyboard()
    if intro:
        text = intro + "\n\n" + text
    await update.message.reply_text(text, reply_markup=markup)
    return ASK_ZAKLAD


async def go_to_order(update: Update, context: ContextTypes.DEFAULT_TYPE, intro: str = ""):
    parts = context.user_data.get("order_parts", [])
    if parts:
        # Частина замовлення вже є (людина написала товари разом із закладом)
        text = (
            "Крок 2 з 3. Ось що вже записано в замовлення:\n\n"
            + "\n".join(parts)
            + f"\n\nЯкщо це все — натисніть «{BTN_NEXT}».\n"
            "Якщо щось забули — просто допишіть."
        )
        markup = order_keyboard()
    else:
        text = TEXT_ASK_ORDER
        markup = cancel_keyboard()
    if intro:
        text = intro + "\n\n" + text
    await update.message.reply_text(text, reply_markup=markup)
    return ASK_ORDER


async def go_to_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TEXT_ASK_COMMENT, reply_markup=comment_keyboard())
    return ASK_COMMENT


async def go_to_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str = None):
    title = title or "Перевірте, будь ласка, ваше замовлення:"
    await update.message.reply_text(
        f"{title}\n\n{order_summary(context.user_data)}\n\n"
        f"Якщо все правильно — натисніть «{BTN_CONFIRM}».",
        reply_markup=confirm_keyboard(),
    )
    return CONFIRM


async def start_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"{greeting()}\n\nЦе бот для замовлень. Спочатку познайомимось — "
        "це потрібно лише один раз.\n\nНапишіть, як вас звати:",
        reply_markup=cancel_keyboard(),
    )
    return ASK_NAME


# ==== ХЕНДЛЕРИ ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start і кнопка «Зробити замовлення»."""
    clear_order(context)
    if await load_user(update, context):
        return await go_to_zaklad(update, context, greeting(context.user_data["name"]))
    return await start_registration(update, context)


async def repeat_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Повторити минуле замовлення»."""
    clear_order(context)
    if not await load_user(update, context):
        return await start_registration(update, context)

    hello = greeting(context.user_data["name"])
    orders = get_user_orders(context.user_data["phone"])
    if not orders:
        return await go_to_zaklad(
            update, context, f"{hello}\n\nМинулих замовлень поки немає — оформимо нове."
        )

    last = orders[-1]
    context.user_data["zaklad"] = last["zaklad"]
    context.user_data["order"] = last["order"]
    context.user_data["comment"] = last["comment"] or NO_COMMENT_VALUE
    return await go_to_confirm(
        update,
        context,
        f"{hello}\n\nОсь ваше минуле замовлення. Його можна відправити ще раз "
        "або спочатку виправити:",
    )


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    context.user_data["name"] = name
    await update.message.reply_text(
        f"Приємно познайомитись, {name}!\n\n"
        f"Тепер натисніть кнопку «{BTN_SHARE_CONTACT}» внизу 👇\n\n"
        "Або напишіть номер телефону цифрами, наприклад: 0671234567",
        reply_markup=contact_keyboard(),
    )
    return ASK_PHONE


async def phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    context.user_data["phone"] = phone
    save_user(update.effective_chat.id, context.user_data["name"], phone)
    return await go_to_zaklad(update, context, "Дякуємо, номер збережено ✅")


async def ask_phone_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await phone_received(update, context, update.message.contact.phone_number)


async def ask_phone_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = normalize_typed_phone(update.message.text)
    if not phone:
        await update.message.reply_text(
            "Не вдалося розпізнати номер 🙁\n\n"
            f"Найпростіше — натисніть кнопку «{BTN_SHARE_CONTACT}» внизу 👇\n\n"
            "Або напишіть номер ще раз, лише цифрами, наприклад: 0671234567",
            reply_markup=contact_keyboard(),
        )
        return ASK_PHONE
    return await phone_received(update, context, phone)


async def ask_zaklad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    editing = context.user_data.get("editing", False)

    # Людина написала одразу і заклад, і товари (як «Шо таке Азія» + список)
    if len(lines) > 1:
        if editing:
            await update.message.reply_text(
                "Напишіть, будь ласка, лише назву закладу, одним рядком.",
                reply_markup=zaklad_keyboard(
                    get_recent_zaklady(context.user_data.get("phone", ""))
                ),
            )
            return ASK_ZAKLAD
        if looks_like_item(lines[0]):
            # Перший рядок — теж товар, тобто назви закладу тут немає
            context.user_data.setdefault("order_parts", []).append("\n".join(lines))
            return await go_to_zaklad(
                update,
                context,
                "Схоже, ви одразу написали замовлення — я його запам'ятав ✅\n"
                "Але спочатку потрібна назва закладу.",
            )
        context.user_data["zaklad"] = lines[0]
        context.user_data.setdefault("order_parts", []).append("\n".join(lines[1:]))
        return await go_to_order(
            update, context, f"Заклад: {lines[0]} ✅\nРешту я переніс у замовлення."
        )

    # Один рядок, але схожий на товар («Морква 2кг»), а не на назву закладу
    if looks_like_item(text) and not editing:
        context.user_data.setdefault("order_parts", []).append(text)
        return await go_to_zaklad(
            update,
            context,
            "Схоже, це товар із замовлення — я його запам'ятав ✅\n"
            "Але спочатку потрібна назва закладу.",
        )

    context.user_data["zaklad"] = text
    if context.user_data.pop("editing", False):
        return await go_to_confirm(update, context)
    return await go_to_order(update, context, f"Заклад: {text} ✅")


async def ask_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Збирає замовлення з одного або кількох повідомлень, поки людина не натисне «Далі»."""
    context.user_data.setdefault("order_parts", []).append(clean_lines(update.message.text))
    await update.message.reply_text(
        "Записав ✅\n\n"
        f"Якщо це все — натисніть «{BTN_NEXT}» внизу 👇\n"
        "Якщо щось забули — просто допишіть ще одним повідомленням.",
        reply_markup=order_keyboard(),
    )
    return ASK_ORDER


async def order_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = context.user_data.get("order_parts", [])
    if not parts:
        await update.message.reply_text(
            "Замовлення поки порожнє. Напишіть, що потрібно привезти 👇",
            reply_markup=cancel_keyboard(),
        )
        return ASK_ORDER
    context.user_data["order"] = "\n".join(parts)
    context.user_data.pop("order_parts", None)
    if context.user_data.pop("editing", False):
        return await go_to_confirm(update, context)
    return await go_to_comment(update, context)


async def ask_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = clean_lines(update.message.text)
    context.user_data["comment"] = NO_COMMENT_VALUE if text == BTN_NO_COMMENT else text
    context.user_data.pop("editing", None)
    return await go_to_confirm(update, context)


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    try:
        write_order(
            data["name"],
            data["phone"],
            data["zaklad"],
            data["order"],
            data.get("comment") or NO_COMMENT_VALUE,
        )
    except Exception:
        logger.exception("Не вдалося записати замовлення в таблицю")
        await update.message.reply_text(
            "⚠️ На жаль, замовлення не вдалося відправити через технічну помилку.\n\n"
            f"Спробуйте натиснути «{BTN_CONFIRM}» ще раз через хвилину.",
            reply_markup=confirm_keyboard(),
        )
        return CONFIRM

    summary = order_summary(data)
    name = data.get("name", "")
    clear_order(context)
    await update.message.reply_text(
        "✅ Ваше замовлення прийнято!\n\n"
        f"{summary}\n\n"
        f"Дякуємо{', ' + name if name else ''}! Гарного дня 🙂\n\n"
        f"Щоб зробити нове замовлення, натисніть «{BTN_NEW_ORDER}» внизу 👇",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Що потрібно виправити? Натисніть кнопку внизу 👇",
        reply_markup=edit_keyboard(),
    )
    return EDIT_CHOICE


async def edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text
    context.user_data["editing"] = True
    if choice == BTN_EDIT_ZAKLAD:
        return await go_to_zaklad(update, context)
    if choice == BTN_EDIT_ORDER:
        context.user_data.pop("order_parts", None)
        await update.message.reply_text(
            "Напишіть замовлення заново, повністю.\n\n"
            f"Зараз записано:\n{context.user_data.get('order', '')}",
            reply_markup=cancel_keyboard(),
        )
        return ASK_ORDER
    if choice == BTN_EDIT_COMMENT:
        return await go_to_comment(update, context)
    context.user_data.pop("editing", None)
    await update.message.reply_text(
        "Будь ласка, натисніть одну з кнопок внизу 👇", reply_markup=edit_keyboard()
    )
    return EDIT_CHOICE


async def confirm_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """На кроці перевірки людина щось написала замість кнопки."""
    await update.message.reply_text(
        f"Щоб відправити замовлення, натисніть «{BTN_CONFIRM}».\n"
        f"Щоб змінити щось — «{BTN_EDIT}».",
        reply_markup=confirm_keyboard(),
    )
    return CONFIRM


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_order(context)
    await update.message.reply_text(
        "Замовлення скасовано.\n\n"
        f"Щоб почати нове, натисніть «{BTN_NEW_ORDER}» внизу 👇",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def not_text_in_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото, голосове, стікер тощо посеред оформлення замовлення."""
    await update.message.reply_text(
        "Голосові повідомлення, фото та стікери бот, на жаль, не розуміє.\n"
        "Будь ласка, напишіть текстом або натисніть кнопку внизу.\n\n"
        f"Щоб почати спочатку — «{BTN_CANCEL}»."
    )
    return None  # залишаємось на тому ж кроці


async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Будь-яке повідомлення поза оформленням замовлення — вітаємось і показуємо меню."""
    if not update.message:
        return
    user = get_user(update.effective_chat.id)
    await update.message.reply_text(
        f"{greeting(user['name'] if user else '')}\n\n"
        f"Щоб зробити замовлення, натисніть «{BTN_NEW_ORDER}» внизу 👇",
        reply_markup=main_menu_keyboard(),
    )


# ==== РОЗСИЛКА ВСІМ КЛІЄНТАМ (лише для адміністраторів) ====
BC_TEXT, BC_CONFIRM = 100, 101
BTN_BC_SEND = "✅ Надіслати всім"
MAIN_CONV = None  # посилання на основну розмову (оформлення замовлення)


def reset_main_conversation(update: Update):
    """Якщо адмін почав розсилку посеред оформлення замовлення — скидаємо те замовлення,
    щоб його наступні повідомлення не потрапили в поле «заклад»."""
    try:
        MAIN_CONV._conversations.pop((update.effective_chat.id, update.effective_user.id), None)
    except Exception:
        pass


def get_all_chat_ids():
    ids = []
    for row in users_sheet.get_all_values()[1:]:
        if row and str(row[0]).strip().lstrip("-").isdigit():
            cid = int(str(row[0]).strip())
            if cid not in ids:
                ids.append(cid)
    return ids


async def bc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_main_conversation(update)
    clear_order(context)
    context.user_data.pop("bc_msg", None)
    await update.message.reply_text(
        "📢 Розсилка всім клієнтам.\n\n"
        "Надішліть боту те, що мають отримати клієнти:\n"
        "• текст, або\n"
        "• одне фото з підписом, або\n"
        "• одне відео з підписом.\n\n"
        "Підпис додається в полі під фото/відео перед відправкою.",
        reply_markup=cancel_keyboard(),
    )
    raise ApplicationHandlerStop(BC_TEXT)


async def bc_got_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отримали текст, фото або відео. Запам'ятовуємо саме повідомлення —
    клієнтам буде надіслана його копія (з підписом і форматуванням)."""
    msg = update.message
    context.user_data["bc_msg"] = (msg.chat_id, msg.message_id)
    count = len(get_all_chat_ids())
    await msg.reply_text("Ось так клієнти побачать повідомлення 👇")
    await context.bot.copy_message(
        chat_id=msg.chat_id, from_chat_id=msg.chat_id, message_id=msg.message_id
    )
    await msg.reply_text(
        f"Отримувачів: {count}.\n\nНадіслати?",
        reply_markup=keyboard([[BTN_BC_SEND], [BTN_CANCEL]]),
    )
    raise ApplicationHandlerStop(BC_CONFIRM)


async def bc_wrong_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Для розсилки підходять лише текст, одне фото або одне відео.\n"
        "Надішліть, будь ласка, щось із цього.",
        reply_markup=cancel_keyboard(),
    )
    raise ApplicationHandlerStop(BC_TEXT)


async def bc_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = context.user_data.pop("bc_msg", None)
    if not source:
        await update.message.reply_text("Повідомлення для розсилки не знайдено. Почніть знову: /rozsylka",
                                        reply_markup=main_menu_keyboard())
        raise ApplicationHandlerStop(ConversationHandler.END)
    from_chat_id, message_id = source

    try:
        chat_ids = get_all_chat_ids()
    except Exception:
        logger.exception("Розсилка: не вдалося прочитати аркуш Users")
        await update.message.reply_text(
            "⚠️ Не вдалося прочитати список клієнтів з таблиці. Спробуйте пізніше: /rozsylka",
            reply_markup=main_menu_keyboard(),
        )
        raise ApplicationHandlerStop(ConversationHandler.END)
    await update.message.reply_text(f"Надсилаю {len(chat_ids)} клієнтам, зачекайте…")
    sent, blocked, failed = 0, 0, 0
    for cid in chat_ids:
        for attempt in range(2):
            try:
                await context.bot.copy_message(
                    chat_id=cid,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    reply_markup=main_menu_keyboard(),
                )
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
            except Forbidden:
                blocked += 1
                break
            except Exception:
                logger.exception("Розсилка: не вдалося надіслати %s", cid)
                failed += 1
                break
        await asyncio.sleep(0.06)  # не більше ~15 повідомлень на секунду

    report = f"✅ Розсилку завершено.\n\nНадіслано: {sent}"
    if blocked:
        report += f"\nЗаблокували бота або видалили чат: {blocked}"
    if failed:
        report += f"\nНе вдалося надіслати через помилку: {failed}"
    await update.message.reply_text(report, reply_markup=main_menu_keyboard())
    raise ApplicationHandlerStop(ConversationHandler.END)


async def bc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("bc_msg", None)
    await update.message.reply_text("Розсилку скасовано.", reply_markup=main_menu_keyboard())
    raise ApplicationHandlerStop(ConversationHandler.END)


async def bc_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Натисніть «{BTN_BC_SEND}» або «{BTN_CANCEL}».",
        reply_markup=keyboard([[BTN_BC_SEND], [BTN_CANCEL]]),
    )
    raise ApplicationHandlerStop(BC_CONFIRM)


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    def exact(text):
        return filters.Regex(f"^{re.escape(text)}$")

    cancel_filter = exact(BTN_CANCEL)
    free_text = filters.TEXT & ~filters.COMMAND & ~cancel_filter
    cancel_handler = MessageHandler(cancel_filter, cancel)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(exact(BTN_NEW_ORDER), start),
            MessageHandler(exact(BTN_OLD_NEW_ORDER), start),
            MessageHandler(exact(BTN_REPEAT), repeat_order),
        ],
        states={
            ASK_NAME: [cancel_handler, MessageHandler(free_text, ask_name)],
            ASK_PHONE: [
                cancel_handler,
                MessageHandler(filters.CONTACT, ask_phone_contact),
                MessageHandler(free_text, ask_phone_text),
            ],
            ASK_ZAKLAD: [cancel_handler, MessageHandler(free_text, ask_zaklad)],
            ASK_ORDER: [
                cancel_handler,
                MessageHandler(exact(BTN_NEXT), order_done),
                MessageHandler(free_text, ask_order),
            ],
            ASK_COMMENT: [cancel_handler, MessageHandler(free_text, ask_comment)],
            CONFIRM: [
                cancel_handler,
                MessageHandler(exact(BTN_CONFIRM), confirm_order),
                MessageHandler(exact(BTN_EDIT), edit_start),
                MessageHandler(free_text, confirm_other),
            ],
            EDIT_CHOICE: [cancel_handler, MessageHandler(free_text, edit_choice)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.ALL & ~filters.COMMAND, not_text_in_dialog),
        ],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    global MAIN_CONV
    MAIN_CONV = conv_handler

    if ADMIN_CHAT_IDS:
        admin = filters.Chat(chat_id=ADMIN_CHAT_IDS)
        bc_cancel_handler = MessageHandler(admin & (cancel_filter | filters.Regex("^/cancel")), bc_cancel)
        broadcast_handler = ConversationHandler(
            entry_points=[CommandHandler("rozsylka", bc_start, filters=admin)],
            states={
                BC_TEXT: [
                    bc_cancel_handler,
                    MessageHandler(
                        admin & ((filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.VIDEO),
                        bc_got_content,
                    ),
                    MessageHandler(admin & ~filters.COMMAND, bc_wrong_content),
                ],
                BC_CONFIRM: [
                    bc_cancel_handler,
                    MessageHandler(admin & exact(BTN_BC_SEND), bc_send),
                    MessageHandler(admin & filters.TEXT & ~filters.COMMAND, bc_other),
                ],
            },
            fallbacks=[],
            allow_reentry=True,
        )
        # Група -1: перевіряється раніше за оформлення замовлення, щоб не змішувались
        application.add_handler(broadcast_handler, group=-1)
    # Усе, що прийшло поза оформленням замовлення (наприклад, «Привіт»)
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, any_message))

    logger.info("Бот запущено, чекаю на повідомлення...")
    application.run_polling()


if __name__ == "__main__":
    main()
