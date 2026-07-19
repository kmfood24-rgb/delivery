import os
import json
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==== НАЛАШТУВАННЯ (з environment variables) ====
BOT_TOKEN = os.environ["BOT_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]  # вміст JSON-ключа service account одним рядком

SHEET_ORDERS = "Замовлення"
SHEET_USERS = "Users"

# Стани розмови
ASK_NAME, ASK_PHONE, ASK_ZAKLAD, ASK_ORDER, ASK_COMMENT = range(5)

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


def write_order(name, phone, zaklad, order, comment):
    orders_sheet.append_row(
        [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            name,
            phone,
            zaklad,
            order,
            comment,
        ]
    )


# ==== КЛАВІАТУРИ ====
def contact_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Поділитись контактом", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def start_keyboard():
    return ReplyKeyboardMarkup(
        [["🔄 Нове замовлення"]], resize_keyboard=True, one_time_keyboard=True
    )


# ==== ХЕНДЛЕРИ ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = get_user(chat_id)

    if user:
        context.user_data["name"] = user["name"]
        context.user_data["phone"] = user["phone"]
        await update.message.reply_text(
            f"Привіт, {user['name']}! Введіть назву закладу:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_ZAKLAD
    else:
        await update.message.reply_text(
            "Вітаємо! Як вас звати?", reply_markup=ReplyKeyboardRemove()
        )
        return ASK_NAME


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text
    await update.message.reply_text(
        f"Дякую, {update.message.text}! Тепер поділіться номером телефону "
        f"(це потрібно лише один раз):",
        reply_markup=contact_keyboard(),
    )
    return ASK_PHONE


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    if not contact:
        await update.message.reply_text(
            'Будь ласка, скористайтесь кнопкою "Поділитись контактом" нижче 👇',
            reply_markup=contact_keyboard(),
        )
        return ASK_PHONE

    chat_id = update.effective_chat.id
    phone = contact.phone_number
    name = context.user_data["name"]
    context.user_data["phone"] = phone
    save_user(chat_id, name, phone)

    await update.message.reply_text(
        "Введіть назву закладу:", reply_markup=ReplyKeyboardRemove()
    )
    return ASK_ZAKLAD


async def ask_zaklad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["zaklad"] = update.message.text
    await update.message.reply_text("Опишіть замовлення:")
    return ASK_ORDER


async def ask_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["order"] = update.message.text
    await update.message.reply_text(
        'Додайте коментар (або напишіть "-", якщо коментаря немає):'
    )
    return ASK_COMMENT


async def ask_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text
    write_order(
        context.user_data["name"],
        context.user_data["phone"],
        context.user_data["zaklad"],
        context.user_data["order"],
        comment,
    )
    await update.message.reply_text(
        "Дякуємо! Ваше замовлення прийнято ✅", reply_markup=start_keyboard()
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Скасовано. Натисніть кнопку нижче, щоб почати нове замовлення.",
        reply_markup=start_keyboard(),
    )
    return ConversationHandler.END


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^🔄 Нове замовлення$"), start),
        ],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_PHONE: [MessageHandler(filters.CONTACT, ask_phone)],
            ASK_ZAKLAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_zaklad)],
            ASK_ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_order)],
            ASK_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_comment)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    logger.info("Бот запущено, чекаю на повідомлення...")
    application.run_polling()


if __name__ == "__main__":
    main()
