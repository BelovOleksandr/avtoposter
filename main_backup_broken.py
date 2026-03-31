import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import List

from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher import router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.exceptions import TelegramAPIError

from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Boolean, DateTime, Float, JSON, ForeignKey,
    select, delete, update, func
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

load_dotenv()

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ Ошибка: BOT_TOKEN не задан в .env")
    sys.exit(1)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
if not API_ID or not API_HASH:
    print("❌ Ошибка: API_ID и API_HASH не заданы в .env")
    sys.exit(1)

DEFAULT_DELAY = 1
DEFAULT_RETRIES = 3
SUBSCRIPTION_PRICES = {
    "3days": 100,
    "week": 200,
    "month": 500,
    "year": 5000
}

WELCOME_VIDEO_FILE = "welcome.mp4"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------- Инициализация бота ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

# ---------- База данных ----------
Base = declarative_base()


class Admin(Base):
    __tablename__ = 'admins'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, nullable=False)
    added_at = Column(DateTime, default=datetime.now)


class Chat(Base):
    __tablename__ = 'chats'
    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, unique=True, nullable=False)
    title = Column(String, nullable=False)
    type = Column(String)
    created_at = Column(DateTime, default=datetime.now)


class Broadcast(Base):
    __tablename__ = 'broadcasts'
    id = Column(Integer, primary_key=True)
    message_data = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class BroadcastTarget(Base):
    __tablename__ = 'broadcast_targets'
    id = Column(Integer, primary_key=True)
    broadcast_id = Column(Integer, ForeignKey('broadcasts.id'), nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    status = Column(String, default='pending')
    error_text = Column(Text)
    sent_at = Column(DateTime)


class User(Base):
    __tablename__ = 'users'
    user_id = Column(BigInteger, primary_key=True)
    username = Column(String)
    first_name = Column(String)
    balance = Column(Float, default=0.0)
    is_blocked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class Subscription(Base):
    __tablename__ = 'subscriptions'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id'), nullable=False)
    plan = Column(String)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True)
    auto_renew = Column(Boolean, default=False)


class Payment(Base):
    __tablename__ = 'payments'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id'), nullable=False)
    amount = Column(Float, nullable=False)
    method = Column(String)
    status = Column(String, default='pending')
    created_at = Column(DateTime, default=datetime.now)
    completed_at = Column(DateTime)
    external_id = Column(String)


class PaymentMethod(Base):
    __tablename__ = 'payment_methods'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    details = Column(JSON)
    is_active = Column(Boolean, default=True)


class BalanceHistory(Base):
    __tablename__ = 'balance_history'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id'), nullable=False)
    amount = Column(Float, nullable=False)
    reason = Column(String)
    created_at = Column(DateTime, default=datetime.now)


class SessionAccount(Base):
    __tablename__ = 'session_accounts'
    id = Column(Integer, primary_key=True)
    session_name = Column(String, unique=True, nullable=False)
    phone = Column(String, nullable=False)
    first_name = Column(String)
    username = Column(String)
    is_active = Column(Boolean, default=True)
    is_working = Column(Boolean, default=True)
    last_check = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
    messages_sent = Column(Integer, default=0)
    daily_limit = Column(Integer, default=100)
    messages_sent_today = Column(Integer, default=0)
    last_reset = Column(DateTime, default=datetime.now)


engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---------- Помощники БД ----------
async def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Admin).where(Admin.user_id == user_id))
        return result.scalar_one_or_none() is not None


async def get_all_admins():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Admin))
        return list(result.scalars().all())


async def add_admin(user_id: int):
    async with AsyncSessionLocal() as session:
        existing = await session.execute(select(Admin).where(Admin.user_id == user_id))
        if not existing.scalar_one_or_none():
            session.add(Admin(user_id=user_id))
            await session.commit()


async def remove_admin(user_id: int):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Admin).where(Admin.user_id == user_id))
        await session.commit()


async def add_chat(chat_id: int, title: str, type_: str):
    async with AsyncSessionLocal() as session:
        session.add(Chat(chat_id=chat_id, title=title, type=type_))
        await session.commit()


async def get_chats():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Chat))
        return result.scalars().all()


async def get_chat_by_chat_id(chat_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Chat).where(Chat.chat_id == chat_id))
        return result.scalar_one_or_none()


async def delete_chat(chat_id: int):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Chat).where(Chat.chat_id == chat_id))
        await session.commit()


async def clear_all_chats():
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Chat))
        await session.commit()


async def create_broadcast(message_data: dict) -> int:
    async with AsyncSessionLocal() as session:
        b = Broadcast(message_data=message_data)
        session.add(b)
        await session.commit()
        return b.id


async def add_broadcast_target(broadcast_id: int, chat_id: int, status: str = 'pending'):
    async with AsyncSessionLocal() as session:
        t = BroadcastTarget(broadcast_id=broadcast_id, chat_id=chat_id, status=status)
        session.add(t)
        await session.commit()
        return t.id


async def update_target_status(target_id: int, status: str, error_text: str = None):
    async with AsyncSessionLocal() as session:
        stmt = update(BroadcastTarget).where(BroadcastTarget.id == target_id).values(
            status=status, sent_at=datetime.now()
        )
        if error_text:
            stmt = stmt.values(error_text=error_text)
        await session.execute(stmt)
        await session.commit()


async def get_total_chats():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(func.count()).select_from(Chat))
        return result.scalar()


async def get_last_broadcast():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Broadcast.created_at).order_by(Broadcast.created_at.desc()).limit(1))
        return result.scalar()


async def get_total_sent_messages():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.count()).select_from(BroadcastTarget).where(BroadcastTarget.status == 'success'))
        return result.scalar()


async def get_total_errors():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.count()).select_from(BroadcastTarget).where(BroadcastTarget.status == 'error'))
        return result.scalar()


async def get_user_stats():
    async with AsyncSessionLocal() as session:
        total = await session.execute(select(func.count()).select_from(User))
        blocked = await session.execute(select(func.count()).where(User.is_blocked == True))
        total_balance = await session.execute(select(func.sum(User.balance)))
        return {
            'total': total.scalar() or 0,
            'blocked': blocked.scalar() or 0,
            'active': (total.scalar() or 0) - (blocked.scalar() or 0),
            'total_balance': total_balance.scalar() or 0.0
        }


async def get_all_users():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User))
        return result.scalars().all()


async def get_user(user_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        return result.scalar_one_or_none()


async def create_user_if_not_exists(user_id: int, username: str, first_name: str):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            user = User(user_id=user_id, username=username, first_name=first_name)
            session.add(user)
            await session.commit()


async def update_balance(user_id: int, amount: float, reason: str):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user:
            user.balance += amount
            session.add(BalanceHistory(user_id=user_id, amount=amount, reason=reason))
            await session.commit()


async def block_user(user_id: int):
    async with AsyncSessionLocal() as session:
        await session.execute(update(User).where(User.user_id == user_id).values(is_blocked=True))
        await session.commit()


async def unblock_user(user_id: int):
    async with AsyncSessionLocal() as session:
        await session.execute(update(User).where(User.user_id == user_id).values(is_blocked=False))
        await session.commit()


async def get_user_subscription(user_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.user_id == user_id, Subscription.is_active == True))
        return result.scalar_one_or_none()


async def assign_subscription(user_id: int, plan: str, start_date: datetime, end_date: datetime, auto_renew=False):
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Subscription).where(Subscription.user_id == user_id, Subscription.is_active == True).values(
                is_active=False))
        session.add(
            Subscription(user_id=user_id, plan=plan, start_date=start_date, end_date=end_date, auto_renew=auto_renew))
        await session.commit()


async def get_active_subscriptions():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Subscription).where(Subscription.is_active == True))
        return result.scalars().all()


async def get_expired_subscriptions():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.is_active == True, Subscription.end_date < datetime.now()))
        return result.scalars().all()


async def renew_subscription(sub: Subscription) -> bool:
    now = datetime.now()
    if sub.plan == "3days":
        new_end = now + timedelta(days=3)
    elif sub.plan == "week":
        new_end = now + timedelta(weeks=1)
    elif sub.plan == "month":
        new_end = now + timedelta(days=30)
    elif sub.plan == "year":
        new_end = now + timedelta(days=365)
    else:
        return False
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Subscription).where(Subscription.id == sub.id).values(start_date=now, end_date=new_end))
        await session.commit()
    return True


async def update_subscription_status(sub_id: int, is_active: bool):
    async with AsyncSessionLocal() as session:
        await session.execute(update(Subscription).where(Subscription.id == sub_id).values(is_active=is_active))
        await session.commit()


async def get_payment_methods():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(PaymentMethod).where(PaymentMethod.is_active == True))
        return result.scalars().all()


async def add_payment_method(name: str, details: dict):
    async with AsyncSessionLocal() as session:
        session.add(PaymentMethod(name=name, details=details))
        await session.commit()


async def get_payments(limit=20):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Payment).order_by(Payment.created_at.desc()).limit(limit))
        return result.scalars().all()


# ---------- Функции для работы с сессиями ----------
async def add_session(session_name: str, phone: str, first_name: str = None, username: str = None):
    async with AsyncSessionLocal() as session:
        existing = await session.execute(select(SessionAccount).where(SessionAccount.session_name == session_name))
        if not existing.scalar_one_or_none():
            acc = SessionAccount(
                session_name=session_name,
                phone=phone,
                first_name=first_name,
                username=username
            )
            session.add(acc)
            await session.commit()
            return True
    return False


async def get_all_sessions():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SessionAccount))
        return result.scalars().all()


async def get_active_sessions():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SessionAccount).where(
            SessionAccount.is_active == True,
            SessionAccount.is_working == True
        ))
        return result.scalars().all()


async def update_session_status(session_id: int, is_working: bool):
    async with AsyncSessionLocal() as session:
        stmt = update(SessionAccount).where(SessionAccount.id == session_id).values(
            is_working=is_working,
            last_check=datetime.now()
        )
        await session.execute(stmt)
        await session.commit()


async def increment_messages_sent(session_id: int):
    async with AsyncSessionLocal() as session:
        acc = await session.get(SessionAccount, session_id)
        if acc:
            acc.messages_sent += 1
            acc.messages_sent_today += 1
            await session.commit()


async def reset_daily_counters():
    async with AsyncSessionLocal() as session:
        await session.execute(update(SessionAccount).values(
            messages_sent_today=0,
            last_reset=datetime.now()
        ))
        await session.commit()


async def get_spam_stats():
    sessions = await get_all_sessions()
    active = [s for s in sessions if s.is_active and s.is_working]
    total_capacity = sum(s.daily_limit - s.messages_sent_today for s in active)
    return {
        'total': len(sessions),
        'active': len(active),
        'dead': len(sessions) - len(active),
        'total_messages': sum(s.messages_sent for s in sessions),
        'daily_capacity': total_capacity
    }


# ---------- Состояния FSM ----------
class AddChatStates(StatesGroup):
    waiting = State()


class BroadcastStates(StatesGroup):
    waiting_message = State()
    selecting_chats = State()
    confirming = State()


class SettingsStates(StatesGroup):
    set_delay = State()
    add_admin = State()
    remove_admin = State()


class UserManageStates(StatesGroup):
    balance_amount = State()
    select_sub_plan = State()
    assign_sub_user = State()
    assign_sub_plan = State()


class PaymentMethodStates(StatesGroup):
    add_name = State()
    add_details = State()


class AddSessionStates(StatesGroup):
    waiting_session = State()
    waiting_phone = State()


class JoinChatsStates(StatesGroup):
    waiting_chat_id = State()


# ---------- Клавиатуры ----------
def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🔥 О боте", callback_data="about")],
        [InlineKeyboardButton(text="💎 Подписки", callback_data="subscriptions_menu")],
        [InlineKeyboardButton(text="💰 Мой баланс", callback_data="my_balance")],
        [InlineKeyboardButton(text="📞 Контакты", callback_data="contacts")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить чат", callback_data="add_chat")],
        [InlineKeyboardButton(text="📂 Список чатов", callback_data="list_chats")],
        [InlineKeyboardButton(text="✉️ Создать рассылку", callback_data="create_broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="list_users")],
        [InlineKeyboardButton(text="💳 Платежи", callback_data="payments_menu")],
        [InlineKeyboardButton(text="📅 Управление подписками", callback_data="subscriptions_admin")],
        [InlineKeyboardButton(text="👤 Сессии", callback_data="manage_sessions")],
        [InlineKeyboardButton(text="🔍 Чеккер аккаунтов", callback_data="check_accounts")],
        [InlineKeyboardButton(text="📊 Статистика акков", callback_data="accounts_stats")],
        [InlineKeyboardButton(text="🚪 Вступить в чат", callback_data="join_chat")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- Отправка приветствия ----------
async def send_welcome(message: Message):
    video_path = WELCOME_VIDEO_FILE
    welcome_text = (
        "🔥 <b>REDLINE 911 TURBO</b> 🔥\n"
        "<i>Скоростной порш для ваших рассылок</i>\n\n"
        "⚡️ <b>Мощность:</b> 911 л.с.\n"
        "🏁 <b>Разгон до 1000 сообщений:</b> 3 секунды\n"
        "💨 <b>Максимальная скорость рассылки:</b> ∞ сообщений/сек\n\n"
        "✅ <b>Возможности:</b>\n"
        "• 📨 Мощные рассылки с нескольких аккаунтов\n"
        "• 💎 Система подписок\n"
        "• 👥 Управление пользователями\n"
        "• 📊 Полная статистика\n\n"
        "👇 <b>Выберите действие:</b>"
    )
    try:
        if os.path.exists(video_path):
            video = FSInputFile(video_path)
            await message.answer_video(video, caption=welcome_text, reply_markup=get_main_menu_keyboard())
        else:
            await message.answer(welcome_text, reply_markup=get_main_menu_keyboard())
    except Exception as e:
        logger.error(f"Ошибка отправки видео: {e}")
        await message.answer(welcome_text, reply_markup=get_main_menu_keyboard())


# ---------- Команды ----------
@router.message(CommandStart())
async def cmd_start(message: Message):
    await create_user_if_not_exists(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await send_welcome(message)


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён. Вы не являетесь администратором.")
        return
    await message.answer(
        "⚙️ <b>Админ-панель</b>\n\nДобро пожаловать в панель управления!",
        reply_markup=get_admin_menu_keyboard()
    )


# ---------- Пользовательские обработчики ----------
@router.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery):
    try:
        await call.message.delete()
    except:
        pass
    await send_welcome(call.message)
    await call.answer()


@router.callback_query(F.data == "about")
async def about_bot(call: CallbackQuery):
    text = (
        "🏎️ <b>REDLINE 911 TURBO</b>\n\n"
        "Мощный инструмент для автоматизации рассылок в Telegram.\n\n"
        "🔥 <b>Особенности:</b>\n"
        "• Рассылка через несколько аккаунтов\n"
        "• Система подписок и балансов\n"
        "• Полная статистика\n"
        "• Управление чатами\n\n"
        "💡 <b>Для доступа к админ-панели</b> используйте команду /admin"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]])
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "my_balance")
async def my_balance(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    if not user:
        await create_user_if_not_exists(call.from_user.id, call.from_user.username, call.from_user.first_name)
        user = await get_user(call.from_user.id)
    sub = await get_user_subscription(call.from_user.id)
    sub_text = f"Активна до: {sub.end_date.strftime('%Y-%m-%d')}" if sub else "Нет активной подписки"
    text = (
        f"💰 <b>Ваш баланс</b>\n\n"
        f"💎 Баланс: <b>{(user.balance or 0.0):.2f}₽</b>\n"
        f"📅 Подписка: {sub_text}\n\n"
        f"💡 <i>Для пополнения обратитесь к администратору</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]])
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "contacts")
async def contacts(call: CallbackQuery):
    text = (
        "📞 <b>Контакты</b>\n\n"
        "👨‍💻 <b>Техподдержка:</b>\n"
        "https://t.me/redline_support\n\n"
        "📧 <b>Email:</b>\n"
        "support@redline911.com"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]])
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "subscriptions_menu")
async def user_subscriptions_menu(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 дня (100₽)", callback_data="buy_sub_3days"),
         InlineKeyboardButton(text="Неделя (200₽)", callback_data="buy_sub_week")],
        [InlineKeyboardButton(text="Месяц (500₽)", callback_data="buy_sub_month"),
         InlineKeyboardButton(text="Год (5000₽)", callback_data="buy_sub_year")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])
    text = (
        "💎 <b>Выберите тариф подписки</b>\n\n"
        "🔹 <b>3 дня</b> — 100₽\n"
        "🔹 <b>Неделя</b> — 200₽\n"
        "🔹 <b>Месяц</b> — 500₽\n"
        "🔹 <b>Год</b> — 5000₽\n\n"
        "После оплаты напишите администратору для активации."
    )
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("buy_sub_"))
async def buy_subscription(call: CallbackQuery):
    plan = call.data.split("_")[2]
    price = SUBSCRIPTION_PRICES.get(plan, 0)
    text = (
        f"💎 <b>Оформление подписки {plan}</b>\n\n"
        f"💰 Стоимость: {price}₽\n\n"
        f"📝 <b>Для оплаты:</b>\n"
        f"1. Переведите {price}₽ на карту:\n"
        f"   <code>2202 2012 3456 7890</code>\n"
        f"2. Отправьте чек администратору\n\n"
        f"После подтверждения подписка будет активирована!"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="subscriptions_menu")]])
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


# ---------- АДМИНСКИЕ ОБРАБОТЧИКИ ----------
async def admin_check(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Только для администраторов!", show_alert=True)
        return False
    return True


@router.callback_query(F.data == "add_chat")
async def add_chat_start(call: CallbackQuery, state: FSMContext):
    if not await admin_check(call):
        return
    await call.message.answer(
        "📝 <b>Добавление чата</b>\n\n"
        "Отправьте <b>username</b> (например, @channel), <b>ID</b> чата или <b>перешлите любое сообщение</b> из нужного чата."
    )
    await state.set_state(AddChatStates.waiting)
    await call.answer()


@router.message(AddChatStates.waiting)
async def add_chat_process(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    chat_id = None
    title = None
    type_ = None

    if message.forward_origin:
        if hasattr(message.forward_origin, 'chat'):
            chat = message.forward_origin.chat
            chat_id = chat.id
            title = chat.title or "Unknown"
            type_ = chat.type
        elif hasattr(message.forward_origin, 'type') and message.forward_origin.type in ('channel', 'group',
                                                                                         'supergroup'):
            chat = message.forward_origin
            chat_id = chat.id
            title = chat.title or "Unknown"
            type_ = chat.type
        else:
            await message.answer("❌ Перешлите сообщение из канала или группы.")
            return
    elif message.text:
        text = message.text.strip()
        if 't.me/' in text:
            parts = text.split('/')
            identifier = parts[-1]
            if identifier.startswith('+') or identifier.startswith('joinchat'):
                await message.answer("❌ Ссылки-приглашения не поддерживаются.")
                return
            else:
                username = f"@{identifier}"
        else:
            username = text if text.startswith('@') else f"@{text}"

        try:
            chat = await bot.get_chat(username)
            chat_id = chat.id
            title = chat.title or chat.first_name or "Unknown"
            type_ = chat.type
        except Exception:
            try:
                chat_id = int(text)
                chat = await bot.get_chat(chat_id)
                title = chat.title or chat.first_name or "Unknown"
                type_ = chat.type
            except Exception as e:
                logger.error(f"Ошибка получения чата: {e}")
                await message.answer("❌ Не удалось определить чат.")
                return

    if not chat_id:
        await message.answer("❌ Не удалось определить чат.")
        return

    existing = await get_chat_by_chat_id(chat_id)
    if existing:
        await message.answer(f"⚠️ Чат <b>{title}</b> уже добавлен.")
        await state.clear()
        return

    await add_chat(chat_id, title, type_)
    await message.answer(f"✅ Чат <b>{title}</b> успешно добавлен!")
    await state.clear()


@router.callback_query(F.data == "list_chats")
async def list_chats(call: CallbackQuery):
    if not await admin_check(call):
        return
    chats = await get_chats()
    if not chats:
        await call.message.answer("📭 <b>Список чатов пуст</b>")
        await call.answer()
        return
    buttons = [[InlineKeyboardButton(text=chat.title, callback_data=f"chat_{base64.b64encode(str(chat.chat_id).encode()).decode()}")] for chat in chats]
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer(f"📂 <b>Список чатов</b> (всего: {len(chats)}):", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("chat_"))
async def chat_details(call: CallbackQuery):
    if not await admin_check(call):
        return
    try:
        parts = call.data.split("_", 1)
        chat_id = int(base64.b64decode(parts[1].encode()).decode())
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data in chat_details: {call.data}")
        await call.answer("Некорректные данные", show_alert=True)
        return
    chat = await get_chat_by_chat_id(chat_id)
    if not chat:
        await call.answer("Чат не найден", show_alert=True)
        return
    text = f"📌 <b>Информация о чате</b>\n\n<b>Название:</b> {chat.title}\n<b>ID:</b> <code>{chat.chat_id}</code>\n<b>Тип:</b> {chat.type}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Удалить чат", callback_data=f"delchat_{base64.b64encode(str(chat.chat_id).encode()).decode()}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list_chats")]
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("delchat_"))
async def delete_chat_callback(call: CallbackQuery):
    if not await admin_check(call):
        return
    try:
        data_part = call.data.split("_", 1)[1]
        chat_id = int(base64.b64decode(data_part.encode()).decode())
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data in delete_chat_callback: {call.data}")
        await call.answer("Некорректные данные", show_alert=True)
        return
    chat = await get_chat_by_chat_id(chat_id)
    if chat:
        await delete_chat(chat_id)
        await call.answer(f"✅ Чат «{chat.title}» удалён", show_alert=True)
    else:
        await call.answer("❌ Чат не найден", show_alert=True)
    await list_chats(call)


@router.callback_query(F.data == "create_broadcast")
async def create_broadcast_start(call: CallbackQuery, state: FSMContext):
    if not await admin_check(call):
        return
    await call.message.answer(
        "✉️ <b>Создание рассылки</b>\n\n"
        "Отправьте сообщение, которое хотите разослать.\n\n"
        "Поддерживаются: <b>текст, фото, видео, документы</b>.\n\n"
        "Для текста с фото/видео отправьте файл с подписью."
    )
    await state.set_state(BroadcastStates.waiting_message)
    await call.answer()


@router.message(BroadcastStates.waiting_message)
async def broadcast_message_received(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    msg_data = {}
    if message.text:
        msg_data['text'] = message.text
    elif message.photo:
        msg_data['photo'] = message.photo[-1].file_id
        msg_data['caption'] = message.caption or ""
    elif message.video:
        msg_data['video'] = message.video.file_id
        msg_data['caption'] = message.caption or ""
    elif message.document:
        msg_data['document'] = message.document.file_id
        msg_data['caption'] = message.caption or ""
    else:
        await message.answer("❌ Неподдерживаемый тип сообщения.")
        return

    await state.update_data(message_data=msg_data)
    chats = await get_chats()
    if not chats:
        await message.answer("❌ Нет добавленных чатов. Сначала добавьте чаты.")
        await state.clear()
        return

    buttons = []
    for chat in chats:
        buttons.append([InlineKeyboardButton(text=f"☑️ {chat.title}", callback_data=f"selchat_{base64.b64encode(str(chat.chat_id).encode()).decode()}")])
    buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data="done_selection")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    sent_msg = await message.answer("📋 <b>Выберите чаты для рассылки</b> (можно несколько):", reply_markup=kb)
    await state.update_data(selected_chats=[], selection_message_id=sent_msg.message_id)
    await state.set_state(BroadcastStates.selecting_chats)


@router.callback_query(BroadcastStates.selecting_chats, F.data.startswith("selchat_"))
async def select_chat_callback(call: CallbackQuery, state: FSMContext):
    try:
        data_part = call.data.split("_", 1)[1]
        chat_id = int(base64.b64decode(data_part.encode()).decode())
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data in select_chat_callback: {call.data}")
        await call.answer("Некорректные данные", show_alert=True)
        return
    data = await state.get_data()
    selected = data.get('selected_chats', [])
    if chat_id in selected:
        selected.remove(chat_id)
    else:
        selected.append(chat_id)
    await state.update_data(selected_chats=selected)

    chats = await get_chats()
    buttons = []
    for chat in chats:
        check = "✅ " if chat.chat_id in selected else "☑️ "
        buttons.append([InlineKeyboardButton(text=f"{check}{chat.title}", callback_data=f"selchat_{base64.b64encode(str(chat.chat_id).encode()).decode()}")])
    buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data="done_selection")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer()


@router.callback_query(BroadcastStates.selecting_chats, F.data == "done_selection")
async def done_selection(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get('selected_chats', [])
    if not selected:
        await call.answer("❌ Выберите хотя бы один чат", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать рассылку", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast")]
    ])
    await call.message.edit_text(
        f"📨 <b>Подтверждение рассылки</b>\n\n"
        f"Вы собираетесь отправить сообщение в <b>{len(selected)} чат(ов)</b>.\n\n"
        f"Подтвердить?",
        reply_markup=kb
    )
    await state.set_state(BroadcastStates.confirming)
    await call.answer()


@router.callback_query(BroadcastStates.confirming, F.data == "confirm_broadcast")
async def confirm_broadcast(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    msg_data = data['message_data']
    selected = data['selected_chats']
    broadcast_id = await create_broadcast(msg_data)
    await call.message.edit_text("🚀 <b>Рассылка запущена!</b>\n\nОжидайте отчёт...")
    asyncio.create_task(send_broadcast_with_sessions(broadcast_id, selected, msg_data, call.from_user.id))
    await state.clear()
    await call.answer()


@router.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ <b>Рассылка отменена</b>.")
    await call.answer()


# ---------- Отправка через несколько сессий ----------
async def send_broadcast_with_sessions(broadcast_id: int, chat_ids: List[int], msg_data: dict, admin_id: int):
    from pyrogram import Client

    sessions = await get_active_sessions()
    if not sessions:
        await bot.send_message(admin_id, "❌ Нет активных сессий для рассылки!")
        return

    clients = []
    for s in sessions:
        try:
            client = Client(s.session_name.replace('.session', ''), api_id=API_ID, api_hash=API_HASH)
            await client.start()
            clients.append({'client': client, 'id': s.id, 'name': s.session_name})
            logger.info(f"✅ Запущен клиент {s.session_name}")
        except Exception as e:
            logger.error(f"❌ Ошибка запуска {s.session_name}: {e}")
            await update_session_status(s.id, False)

    if not clients:
        await bot.send_message(admin_id, "❌ Не удалось запустить ни одного клиента!")
        return

    # Распределяем чаты
    chats_per_client = len(chat_ids) // len(clients)
    remainder = len(chat_ids) % len(clients)

    semaphore = asyncio.Semaphore(3)

    async def send_to_chat(client_data, chat_id):
        async with semaphore:
            target_id = await add_broadcast_target(broadcast_id, chat_id, 'pending')
            chat = await get_chat_by_chat_id(chat_id)
            if not chat:
                await update_target_status(target_id, 'error', 'Chat not found')
                return

            text = msg_data.get('text', '')
            photo = msg_data.get('photo')
            video = msg_data.get('video')
            document = msg_data.get('document')
            caption = msg_data.get('caption', '')

            for attempt in range(DEFAULT_RETRIES):
                try:
                    if photo:
                        await client_data['client'].send_photo(chat_id, photo, caption=caption)
                    elif video:
                        await client_data['client'].send_video(chat_id, video, caption=caption)
                    elif document:
                        await client_data['client'].send_document(chat_id, document, caption=caption)
                    else:
                        await client_data['client'].send_message(chat_id, text)
                    await update_target_status(target_id, 'success')
                    await increment_messages_sent(client_data['id'])
                    logger.info(f"✅ [{client_data['name']}] Отправлено в {chat_id}")
                    break
                except Exception as e:
                    logger.error(f"❌ [{client_data['name']}] Ошибка (попытка {attempt + 1}): {e}")
                    if attempt == DEFAULT_RETRIES - 1:
                        await update_target_status(target_id, 'error', str(e))
                    else:
                        await asyncio.sleep(2 ** attempt)
            await asyncio.sleep(DEFAULT_DELAY)

    tasks = []
    idx = 0
    for i, client_data in enumerate(clients):
        count = chats_per_client + (1 if i < remainder else 0)
        client_chats = chat_ids[idx:idx + count]
        idx += count
        for chat_id in client_chats:
            tasks.append(send_to_chat(client_data, chat_id))

    await asyncio.gather(*tasks)

    for c in clients:
        await c['client'].stop()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(func.count()).select_from(BroadcastTarget)
            .where(BroadcastTarget.broadcast_id == broadcast_id, BroadcastTarget.status == 'success')
        )
        success = result.scalar() or 0
    errors = len(chat_ids) - success

    stats = await get_spam_stats()
    report = (
        f"📢 <b>Результаты рассылки</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📬 Всего чатов: {len(chat_ids)}\n"
        f"✅ Успешно: {success}\n"
        f"❌ Ошибок: {errors}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Аккаунтов: {len(clients)}\n"
        f"📊 Мощность: {stats['daily_capacity']}/день"
    )
    await bot.send_message(admin_id, report)


# ---------- Управление сессиями ----------
@router.callback_query(F.data == "manage_sessions")
async def manage_sessions(call: CallbackQuery):
    if not await admin_check(call):
        return
    sessions = await get_all_sessions()
    if not sessions:
        text = "📭 <b>Нет добавленных сессий</b>\n\nДобавьте сессию: отправьте .session файл"
    else:
        text = "👤 <b>Список сессий</b>\n\n"
        for s in sessions:
            status = "✅" if s.is_active and s.is_working else "❌"
            text += f"{status} <b>{s.session_name}</b>\n"
            text += f"   📱 {s.phone}\n"
            text += f"   📨 Отправлено: {s.messages_sent} | Сегодня: {s.messages_sent_today}/{s.daily_limit}\n\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить сессию", callback_data="add_session")],
        [InlineKeyboardButton(text="🔍 Проверить все", callback_data="check_accounts")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="accounts_stats")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "add_session")
async def add_session_start(call: CallbackQuery, state: FSMContext):
    await call.message.answer(
        "📁 <b>Добавление сессии</b>\n\n"
        "Введите имя файла сессии (например, my_account.session)\n"
        "Файл должен лежать в папке с ботом."
    )
    await state.set_state(AddSessionStates.waiting_session)
    await call.answer()


@router.message(AddSessionStates.waiting_session)
async def add_session_name(message: Message, state: FSMContext):
    session_name = message.text.strip()
    if not session_name.endswith('.session'):
        session_name += '.session'

    if not os.path.exists(session_name):
        await message.answer(f"❌ Файл {session_name} не найден!")
        return

    await state.update_data(session_name=session_name)
    await message.answer("📱 Введите номер телефона аккаунта (в формате +7XXXXXXXXXX):")
    await state.set_state(AddSessionStates.waiting_phone)


@router.message(AddSessionStates.waiting_phone)
async def add_session_phone(message: Message, state: FSMContext):
    from pyrogram import Client

    phone = message.text.strip()
    data = await state.get_data()
    session_name = data['session_name']

    first_name = None
    username = None
    try:
        async with Client(session_name.replace('.session', ''), api_id=API_ID, api_hash=API_HASH) as app:
            me = await app.get_me()
            first_name = me.first_name
            username = me.username
    except Exception as e:
        await message.answer(f"❌ Ошибка: сессия невалидная или не авторизована.\n{e}")
        await state.clear()
        return

    success = await add_session(session_name, phone, first_name, username)
    if success:
        await message.answer(f"✅ Сессия {session_name} добавлена!\n👤 {first_name or 'Имя не получено'}\n📱 {phone}")
    else:
        await message.answer("❌ Сессия с таким именем уже существует.")
    await state.clear()


@router.callback_query(F.data == "check_accounts")
async def check_accounts(call: CallbackQuery):
    if not await admin_check(call):
        return
    await call.message.answer("🔍 <b>Проверка аккаунтов...</b>\n\nЭто может занять время...")

    from pyrogram import Client

    sessions = await get_all_sessions()
    if not sessions:
        await call.message.answer("❌ Нет добавленных сессий")
        await call.answer()
        return

    results = []

    for s in sessions:
        try:
            async with Client(s.session_name.replace('.session', ''), api_id=API_ID, api_hash=API_HASH) as app:
                me = await app.get_me()
                results.append({
                    'name': s.session_name,
                    'status': '✅ ЖИВ',
                    'first_name': me.first_name,
                    'username': me.username
                })
                await update_session_status(s.id, True)
        except Exception as e:
            results.append({
                'name': s.session_name,
                'status': '❌ МЁРТВ',
                'error': str(e)[:50]
            })
            await update_session_status(s.id, False)

    text = "🔍 <b>Результаты проверки</b>\n━━━━━━━━━━━━━━━━━━━\n"
    live = 0
    dead = 0
    for r in results:
        if '✅' in r['status']:
            live += 1
            text += f"✅ <b>{r['name']}</b>\n   👤 {r.get('first_name', '?')}\n\n"
        else:
            dead += 1
            text += f"❌ <b>{r['name']}</b>\n   Ошибка: {r.get('error', '?')}\n\n"
    text += f"━━━━━━━━━━━━━━━━━━━\n📊 Живых: {live} | Мёртвых: {dead}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="manage_sessions")]])
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "accounts_stats")
async def accounts_stats(call: CallbackQuery):
    if not await admin_check(call):
        return
    stats = await get_spam_stats()
    text = (
        f"📊 <b>Статистика аккаунтов</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📱 Всего: {stats['total']}\n"
        f"✅ Активных: {stats['active']}\n"
        f"❌ Мёртвых: {stats['dead']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📨 Отправлено: {stats['total_messages']}\n"
        f"📊 Мощность: {stats['daily_capacity']}/день\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сбросить счётчики", callback_data="reset_counters")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_sessions")]
    ])
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "reset_counters")
async def reset_counters(call: CallbackQuery):
    if not await admin_check(call):
        return
    await reset_daily_counters()
    await call.answer("✅ Счётчики сброшены!", show_alert=True)
    await accounts_stats(call)


@router.callback_query(F.data == "join_chat")
async def join_chat_start(call: CallbackQuery, state: FSMContext):
    if not await admin_check(call):
        return
    await call.message.answer(
        "🚪 <b>Вступление в чат</b>\n\n"
        "Введите ссылку или username чата (например, @channel или https://t.me/joinchat/...)"
    )
    await state.set_state(JoinChatsStates.waiting_chat_id)
    await call.answer()


@router.message(JoinChatsStates.waiting_chat_id)
async def join_chat_process(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    from pyrogram import Client

    chat_input = message.text.strip()
    sessions = await get_active_sessions()

    if not sessions:
        await message.answer("❌ Нет активных сессий для вступления!")
        return

    await message.answer("🚀 Начинаю вступление в чат со всех аккаунтов...")

    results = []

    for s in sessions:
        try:
            async with Client(s.session_name.replace('.session', ''), api_id=API_ID, api_hash=API_HASH) as app:
                if 't.me/' in chat_input:
                    await app.join_chat(chat_input)
                else:
                    await app.join_chat(chat_input)
                results.append(f"✅ {s.session_name} — вступил")
        except Exception as e:
            results.append(f"❌ {s.session_name} — ошибка: {str(e)[:50]}")

    text = "📋 <b>Результаты вступления</b>\n\n" + "\n".join(results)
    await message.answer(text)
    await state.clear()


# ---------- Остальные админские обработчики ----------
@router.callback_query(F.data == "show_stats")
async def show_stats(call: CallbackQuery):
    if not await admin_check(call):
        return
    total_chats = await get_total_chats()
    last_broadcast = await get_last_broadcast()
    sent = await get_total_sent_messages()
    errors = await get_total_errors()
    user_stats = await get_user_stats()
    text = (
        f"📊 <b>Статистика</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📂 Чатов: {total_chats}\n"
        f"📅 Последняя рассылка: {last_broadcast.strftime('%Y-%m-%d %H:%M') if last_broadcast else 'нет'}\n"
        f"📨 Отправлено сообщений: {sent}\n"
        f"⚠️ Ошибок: {errors}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Пользователи:\n"
        f"   • Всего: {user_stats['total']}\n"
        f"   • Активных: {user_stats['active']}\n"
        f"   • Заблокированных: {user_stats['blocked']}\n"
        f"💰 Общий баланс: {user_stats['total_balance']:.2f}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "settings")
async def settings_menu(call: CallbackQuery):
    if not await admin_check(call):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱️ Задержка между сообщениями", callback_data="set_delay")],
        [InlineKeyboardButton(text="🗑 Очистить список чатов", callback_data="clear_chats")],
        [InlineKeyboardButton(text="👤 Управление админами", callback_data="manage_admins")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await call.message.edit_text("⚙️ <b>Настройки</b>", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "clear_chats")
async def clear_chats_callback(call: CallbackQuery):
    if not await admin_check(call):
        return
    await clear_all_chats()
    await call.answer("✅ Все чаты удалены", show_alert=True)


@router.callback_query(F.data == "manage_admins")
async def manage_admins(call: CallbackQuery):
    if not await admin_check(call):
        return
    admins = await get_all_admins()
    text = "👥 <b>Список администраторов</b>\n\n"
    for a in admins:
        text += f"• ID: <code>{a.user_id}</code>\n"
    if not admins:
        text += "Список пуст"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="add_admin")],
        [InlineKeyboardButton(text="❌ Удалить админа", callback_data="remove_admin")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="settings")]
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "add_admin")
async def add_admin_prompt(call: CallbackQuery, state: FSMContext):
    await call.message.answer("📝 Введите Telegram ID нового администратора:")
    await state.set_state(SettingsStates.add_admin)
    await call.answer()


@router.message(SettingsStates.add_admin)
async def add_admin_process(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    try:
        user_id = int(message.text)
        await add_admin(user_id)
        await message.answer(f"✅ Администратор <code>{user_id}</code> добавлен.")
    except:
        await message.answer("❌ Неверный ID.")
    await state.clear()


@router.callback_query(F.data == "remove_admin")
async def remove_admin_prompt(call: CallbackQuery, state: FSMContext):
    await call.message.answer("📝 Введите Telegram ID администратора для удаления:")
    await state.set_state(SettingsStates.remove_admin)
    await call.answer()


@router.message(SettingsStates.remove_admin)
async def remove_admin_process(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    try:
        user_id = int(message.text)
        await remove_admin(user_id)
        await message.answer(f"✅ Администратор <code>{user_id}</code> удалён.")
    except:
        await message.answer("❌ Неверный ID.")
    await state.clear()


@router.callback_query(F.data == "set_delay")
async def set_delay_prompt(call: CallbackQuery, state: FSMContext):
    await call.message.answer("⏱️ Введите задержку между сообщениями (в секундах, от 1 до 10):")
    await state.set_state(SettingsStates.set_delay)
    await call.answer()


@router.message(SettingsStates.set_delay)
async def set_delay_process(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    try:
        delay = int(message.text)
        if 1 <= delay <= 10:
            global DEFAULT_DELAY
            DEFAULT_DELAY = delay
            await message.answer(f"✅ Задержка установлена: {delay} сек.")
        else:
            await message.answer("❌ Введите число от 1 до 10.")
    except:
        await message.answer("❌ Введите число.")
    await state.clear()


@router.callback_query(F.data == "list_users")
async def list_users(call: CallbackQuery):
    if not await admin_check(call):
        return
    users = await get_all_users()
    if not users:
        await call.message.edit_text("👥 <b>Список пользователей пуст</b>")
        await call.answer()
        return

    buttons = []
    for u in users[:20]:
        encoded_id = base64.b64encode(str(u.user_id).encode()).decode()
        buttons.append(
            [InlineKeyboardButton(text=f"👤 {u.first_name} (@{u.username})", callback_data=f"user_{encoded_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("👥 <b>Выберите пользователя</b>:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("user_"))
async def user_details(call: CallbackQuery):
    if not await admin_check(call):
        return

    data_parts = call.data.split("_")

    if len(data_parts) < 2:
        await call.answer("Некорректные данные", show_alert=True)
        return

    # Пытаемся получить user_id
    user_id_str = data_parts[1]

    # Проверяем, не закодирован ли ID в base64
    try:
        # Если это base64 строка
        decoded = base64.b64decode(user_id_str).decode('utf-8')
        user_id = int(decoded)
    except:
        # Если обычный ID
        try:
            user_id = int(user_id_str)
        except:
            await call.answer("Некорректные данные", show_alert=True)
            return

    user = await get_user(user_id)
    if not user:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    sub = await get_user_subscription(user_id)
    sub_text = f"Активна до: {sub.end_date.strftime('%Y-%m-%d')}" if sub else "Нет активной подписки"
    text = (
        f"👤 <b>Информация о пользователе</b>\n\n"
        f"<b>Имя:</b> {user.first_name}\n"
        f"<b>Username:</b> @{user.username}\n"
        f"💰 <b>Баланс:</b> {user.balance:.2f}\n"
        f"🔒 <b>Статус:</b> {'Заблокирован' if user.is_blocked else 'Активен'}\n"
        f"📅 <b>Подписка:</b> {sub_text}"
    )

    # Кодируем user_id в base64 для следующих callback
    encoded_id = base64.b64encode(str(user_id).encode()).decode()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Выдать баланс", callback_data=f"user_balance_add_{encoded_id}"),
         InlineKeyboardButton(text="💸 Забрать баланс", callback_data=f"user_balance_remove_{encoded_id}")],
        [InlineKeyboardButton(text="🔒 Заблокировать" if not user.is_blocked else "🔓 Разблокировать",
                              callback_data=f"user_toggle_block_{encoded_id}")],
        [InlineKeyboardButton(text="📅 Изменить подписку", callback_data=f"user_sub_{encoded_id}")],
        [InlineKeyboardButton(text="📜 История операций", callback_data=f"user_history_{encoded_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list_users")]
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("user_balance_"))
async def user_balance_action(call: CallbackQuery, state: FSMContext):
    data_parts = call.data.split("_")
    if len(data_parts) != 3:
        await call.answer("Некорректные данные", show_alert=True)
        return

    action = data_parts[2]  # add или remove
    user_id_encoded = data_parts[1]

    try:
        decoded = base64.b64decode(user_id_encoded).decode('utf-8')
        user_id = int(decoded)
    except:
        await call.answer("Некорректные данные", show_alert=True)
        return

    await state.update_data(user_id=user_id, action=action)
    await call.message.answer("💰 Введите сумму:")
    await state.set_state(UserManageStates.balance_amount)
    await call.answer()


@router.message(UserManageStates.balance_amount)
async def process_balance_amount(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    try:
        amount = float(message.text)
        data = await state.get_data()
        user_id = data['user_id']
        action = data['action']
        if action == 'add':
            await update_balance(user_id, amount, 'admin')
            await message.answer(f"✅ Баланс увеличен на {amount}.")
        elif action == 'remove':
            await update_balance(user_id, -amount, 'admin')
            await message.answer(f"✅ Баланс уменьшен на {amount}.")
        else:
            await message.answer("❌ Неизвестное действие.")
    except ValueError:
        await message.answer("❌ Некорректные данные! Введите число.")
    await state.clear()


@router.callback_query(F.data.startswith("user_toggle_block_"))
async def user_toggle_block(call: CallbackQuery):
    data_parts = call.data.split("_")
    if len(data_parts) != 3:
        await call.answer("Некорректные данные", show_alert=True)
        return

    user_id_encoded = data_parts[2]

    try:
        decoded = base64.b64decode(user_id_encoded).decode('utf-8')
        user_id = int(decoded)
    except:
        await call.answer("Некорректные данные", show_alert=True)
        return

    user = await get_user(user_id)
    if user.is_blocked:
        await unblock_user(user_id)
        await call.answer("🔓 Пользователь разблокирован", show_alert=True)
    else:
        await block_user(user_id)
        await call.answer("🔒 Пользователь заблокирован", show_alert=True)

    # Обновляем карточку пользователя
    encoded_id = base64.b64encode(str(user_id).encode()).decode()
    call.data = f"user_{encoded_id}"
    await user_details(call)


@router.callback_query(F.data.startswith("user_sub_"))
async def user_subscription_manage(call: CallbackQuery, state: FSMContext):
    data_parts = call.data.split("_")
    if len(data_parts) != 3:
        await call.answer("Некорректные данные", show_alert=True)
        return

    user_id_encoded = data_parts[2]

    try:
        decoded = base64.b64decode(user_id_encoded).decode('utf-8')
        user_id = int(decoded)
    except:
        await call.answer("Некорректные данные", show_alert=True)
        return

    await state.update_data(user_id=user_id)

    encoded_id = base64.b64encode(str(user_id).encode()).decode()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 дня", callback_data="sub_plan_3days"),
         InlineKeyboardButton(text="Неделя", callback_data="sub_plan_week")],
        [InlineKeyboardButton(text="Месяц", callback_data="sub_plan_month"),
         InlineKeyboardButton(text="Год", callback_data="sub_plan_year")],
        [InlineKeyboardButton(text="❌ Отменить подписку", callback_data="sub_cancel")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"user_{encoded_id}")]
    ])
    await call.message.edit_text("📅 <b>Выберите план подписки</b>:", reply_markup=kb)
    await state.set_state(UserManageStates.select_sub_plan)
    await call.answer()


@router.callback_query(UserManageStates.select_sub_plan, F.data.startswith("sub_plan_"))
async def assign_sub_plan(call: CallbackQuery, state: FSMContext):
    plan = call.data.split("_")[2]
    data = await state.get_data()
    user_id = data['user_id']
    start = datetime.now()
    if plan == "3days":
        end = start + timedelta(days=3)
    elif plan == "week":
        end = start + timedelta(weeks=1)
    elif plan == "month":
        end = start + timedelta(days=30)
    elif plan == "year":
        end = start + timedelta(days=365)
    else:
        await call.answer("Неизвестный план")
        return
    await assign_subscription(user_id, plan, start, end)
    await call.message.edit_text(f"✅ Подписка <b>{plan}</b> назначена.")
    await state.clear()
    await call.answer()


@router.callback_query(UserManageStates.select_sub_plan, F.data == "sub_cancel")
async def cancel_subscription(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = data['user_id']
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Subscription).where(Subscription.user_id == user_id, Subscription.is_active == True).values(
                is_active=False))
        await session.commit()
    await call.message.edit_text("❌ Подписка отменена.")
    await state.clear()
    await call.answer()


@router.callback_query(F.data.startswith("user_history_"))
async def user_history(call: CallbackQuery):
    data_parts = call.data.split("_")
    if len(data_parts) != 3:
        await call.answer("Некорректные данные", show_alert=True)
        return

    user_id_encoded = data_parts[2]

    try:
        decoded = base64.b64decode(user_id_encoded).decode('utf-8')
        user_id = int(decoded)
    except:
        await call.answer("Некорректные данные", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(BalanceHistory)
            .where(BalanceHistory.user_id == user_id)
            .order_by(BalanceHistory.created_at.desc())
            .limit(10)
        )
        history = result.scalars().all()

    text = "📜 <b>История операций</b>\n\n"
    for h in history:
        text += f"• {h.created_at.strftime('%Y-%m-%d %H:%M')} {h.amount:+.2f} ({h.reason})\n"
    if not history:
        text = "Нет операций."

    encoded_id = base64.b64encode(str(user_id).encode()).decode()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=f"user_{encoded_id}")]])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "payments_menu")
async def payments_menu(call: CallbackQuery):
    if not await admin_check(call):
        return
    methods = await get_payment_methods()
    text = "💳 <b>Способы оплаты</b>\n\n"
    for m in methods:
        text += f"{'✅' if m.is_active else '❌'} <b>{m.name}</b>\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить способ", callback_data="add_payment_method")],
        [InlineKeyboardButton(text="📋 Просмотр платежей", callback_data="view_payments")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "add_payment_method")
async def add_method_prompt(call: CallbackQuery, state: FSMContext):
    await call.message.answer("📝 Введите название способа оплаты:")
    await state.set_state(PaymentMethodStates.add_name)
    await call.answer()


@router.message(PaymentMethodStates.add_name)
async def add_payment_name(message: Message, state: FSMContext):
    await state.update_data(payment_name=message.text)
    await message.answer("🔧 Введите реквизиты в формате JSON (например, {\"card\": \"1234 5678\"}):")
    await state.set_state(PaymentMethodStates.add_details)


@router.message(PaymentMethodStates.add_details)
async def add_payment_details(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data['payment_name']
    try:
        details = json.loads(message.text)
        await add_payment_method(name, details)
        await message.answer("✅ Способ оплаты добавлен.")
    except json.JSONDecodeError:
        await message.answer("❌ Неверный JSON. Попробуйте снова.")
        return
    await state.clear()


@router.callback_query(F.data == "view_payments")
async def view_payments(call: CallbackQuery):
    payments = await get_payments()
    text = "💸 <b>Последние платежи</b>\n\n"
    for p in payments:
        user = await get_user(p.user_id)
        username = user.username if user else str(p.user_id)
        text += f"• {p.created_at.strftime('%Y-%m-%d %H:%M')} {username}: {p.amount} ({p.method}) — {p.status}\n"
    if not payments:
        text = "Нет платежей."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="payments_menu")]])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "subscriptions_admin")
async def subscriptions_admin(call: CallbackQuery):
    if not await admin_check(call):
        return
    subs = await get_active_subscriptions()
    text = f"📅 <b>Активные подписки</b> (всего: {len(subs)})\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Назначить подписку", callback_data="assign_subscription")],
        [InlineKeyboardButton(text="📋 Список активных", callback_data="list_active_subs")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "assign_subscription")
async def assign_sub_start(call: CallbackQuery, state: FSMContext):
    users = await get_all_users()
    if not users:
        await call.answer("Нет пользователей", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"{u.first_name} (@{u.username})", callback_data=f"sub_user_{base64.b64encode(str(u.user_id).encode()).decode()}")] for
               u in users[:20]]
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="subscriptions_admin")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("👥 <b>Выберите пользователя</b>:", reply_markup=kb)
    await state.set_state(UserManageStates.assign_sub_user)
    await call.answer()


@router.callback_query(UserManageStates.assign_sub_user, F.data.startswith("sub_user_"))
async def assign_sub_user_selected(call: CallbackQuery, state: FSMContext):
    try:
        data_part = call.data.split("_", 1)[1]
        user_id = int(base64.b64decode(data_part.encode()).decode())
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data in assign_sub_user_selected: {call.data}")
        await call.answer("❌ Некорректные данные!", show_alert=True)
        return
    await state.update_data(assign_user_id=user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 дня", callback_data="sub_assign_plan_3days"),
         InlineKeyboardButton(text="Неделя", callback_data="sub_assign_plan_week")],
        [InlineKeyboardButton(text="Месяц", callback_data="sub_assign_plan_month"),
         InlineKeyboardButton(text="Год", callback_data="sub_assign_plan_year")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="assign_subscription")]
    ])
    await call.message.edit_text("📅 <b>Выберите план</b>:", reply_markup=kb)
    await state.set_state(UserManageStates.assign_sub_plan)
    await call.answer()


@router.callback_query(UserManageStates.assign_sub_plan, F.data.startswith("sub_assign_plan_"))
async def assign_sub_plan(call: CallbackQuery, state: FSMContext):
    try:
        plan = call.data.split("_")[3]
    except IndexError:
        await call.answer("❌ Некорректные данные!", show_alert=True)
        return
    data = await state.get_data()
    user_id = data['assign_user_id']
    start = datetime.now()
    if plan == "3days":
        end = start + timedelta(days=3)
    elif plan == "week":
        end = start + timedelta(weeks=1)
    elif plan == "month":
        end = start + timedelta(days=30)
    elif plan == "year":
        end = start + timedelta(days=365)
    else:
        await call.answer("Неизвестный план")
        return
    await assign_subscription(user_id, plan, start, end)
    await call.message.edit_text(f"✅ Подписка <b>{plan}</b> назначена.")
    await state.clear()
    await call.answer()


@router.callback_query(F.data == "list_active_subs")
async def list_active_subs(call: CallbackQuery):
    subs = await get_active_subscriptions()
    if not subs:
        await call.message.answer("📭 Нет активных подписок.")
        await call.answer()
        return
    text = "📋 <b>Активные подписки</b>\n\n"
    for sub in subs:
        user = await get_user(sub.user_id)
        username = user.username if user else str(sub.user_id)
        text += f"• {username}: <b>{sub.plan}</b> до {sub.end_date.strftime('%Y-%m-%d')}\n"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="subscriptions_admin")]])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "admin_panel")
async def admin_panel_callback(call: CallbackQuery):
    if not await admin_check(call):
        return
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer(
        "⚙️ <b>Админ-панель</b>\n\nДобро пожаловать в панель управления!",
        reply_markup=get_admin_menu_keyboard()
    )
    await call.answer()


# ---------- Планировщик ----------
async def check_subscriptions_job():
    expired = await get_expired_subscriptions()
    for sub in expired:
        if sub.auto_renew:
            price = SUBSCRIPTION_PRICES.get(sub.plan)
            user = await get_user(sub.user_id)
            if user and user.balance >= price:
                success = await renew_subscription(sub)
                if success:
                    await update_balance(sub.user_id, -price, "subscription")
                    await bot.send_message(sub.user_id, f"🔄 Ваша подписка {sub.plan} автоматически продлена.")
                else:
                    await update_subscription_status(sub.id, False)
                    await bot.send_message(sub.user_id, f"❌ Не удалось продлить подписку.")
            else:
                await update_subscription_status(sub.id, False)
                await bot.send_message(sub.user_id, f"⚠️ Недостаточно средств для продления подписки.")
        else:
            await update_subscription_status(sub.id, False)
            await bot.send_message(sub.user_id, f"⏰ Срок подписки истёк.")


# ---------- Запуск ----------
async def main():
    await init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_subscriptions_job, CronTrigger(hour=0, minute=0))
    scheduler.add_job(reset_daily_counters, CronTrigger(hour=0, minute=0))
    scheduler.start()

    dp.include_router(router)
    logger.info("✅ Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await scheduler.shutdown()
        logger.info("🛑 Бот остановлен")


if __name__ == '__main__':
    asyncio.run(main())