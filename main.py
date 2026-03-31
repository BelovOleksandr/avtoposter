import asyncio
import sys

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

import base64
import json
import logging
import os
import random
from datetime import date, datetime, timedelta
from typing import List, Optional

from pyrogram import Client, raw as pyrogram_raw
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid,
    FloodWait, UserAlreadyParticipant, ChatWriteForbidden,
    UserBannedInChannel, PeerIdInvalid, ChannelPrivate
)

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)

from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Boolean, DateTime, Float, JSON, ForeignKey,
    select, delete, update, func, text as sa_text
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ BOT_TOKEN не задан")
    sys.exit(1)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")

if not API_ID or not API_HASH:
    print("❌ API_ID и API_HASH не заданы")
    sys.exit(1)

DEFAULT_DELAY = 2          # секунд между сообщениями (одного аккаунта)
DEFAULT_RETRIES = 3
SUBSCRIPTION_PRICES = {
    "3days": 100,
    "week": 200,
    "month": 500,
    "year": 5000
}

WELCOME_VIDEO_FILE = "welcome.mp4"
BANNER_FILE = "banner.jpg"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Глобальное хранилище Pyrogram-клиентов (FSM не умеет сериализовывать объекты)
_active_clients: dict = {}  # key: user_id -> Client

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

# ==================== БД ====================

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
    username = Column(String)
    invite_link = Column(String)
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
    daily_limit = Column(Integer, default=150)
    messages_sent_today = Column(Integer, default=0)
    last_reset = Column(DateTime, default=datetime.now)
    # 0 = глобальная сессия (для рассылки всех), user_id = личная сессия пользователя
    owner_id = Column(BigInteger, default=0, nullable=True)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30} if "sqlite" in DATABASE_URL else {},
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Миграция: добавляем owner_id если колонки нет (существующая БД)
        try:
            await conn.execute(sa_text("ALTER TABLE session_accounts ADD COLUMN owner_id BIGINT DEFAULT 0"))
            logger.info("Миграция: добавлена колонка owner_id в session_accounts")
        except Exception:
            pass  # Колонка уже существует — ок

# ==================== ХЕЛПЕРЫ БД ====================

async def db_retry(coro_func, max_retries=10):
    for attempt in range(max_retries):
        try:
            return await coro_func()
        except Exception as e:
            if ("database is locked" in str(e).lower() or "database is busy" in str(e).lower()) and attempt < max_retries - 1:
                wait = min(1.0 * (attempt + 1), 8)
                await asyncio.sleep(wait)
                continue
            raise

async def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Admin).where(Admin.user_id == user_id))
        return r.scalar_one_or_none() is not None

async def has_active_sub(user_id: int) -> bool:
    """Возвращает True если у юзера есть активная подписка"""
    sub = await get_user_subscription(user_id)
    return sub is not None and sub.is_active

async def is_admin_or_sub(user_id: int) -> bool:
    """Администратор ИЛИ пользователь с подпиской"""
    return await is_admin(user_id) or await has_active_sub(user_id)

async def get_all_admins():
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Admin))
        return list(r.scalars().all())

async def add_admin(user_id: int):
    async with AsyncSessionLocal() as s:
        ex = await s.execute(select(Admin).where(Admin.user_id == user_id))
        if not ex.scalar_one_or_none():
            s.add(Admin(user_id=user_id))
            await s.commit()

async def remove_admin(user_id: int):
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Admin).where(Admin.user_id == user_id))
        await s.commit()

async def add_chat(chat_id: int, title: str, type_: str, username: str = None, invite_link: str = None):
    async with AsyncSessionLocal() as s:
        s.add(Chat(chat_id=chat_id, title=title, type=type_, username=username, invite_link=invite_link))
        await s.commit()

async def get_chats():
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Chat))
        return r.scalars().all()

async def get_chat_by_chat_id(chat_id: int):
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Chat).where(Chat.chat_id == chat_id))
        return r.scalar_one_or_none()

async def delete_chat(chat_id: int):
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Chat).where(Chat.chat_id == chat_id))
        await s.commit()

async def clear_all_chats():
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Chat))
        await s.commit()

async def create_broadcast(message_data: dict) -> int:
    async with AsyncSessionLocal() as s:
        b = Broadcast(message_data=message_data)
        s.add(b)
        await s.commit()
        return b.id

async def add_broadcast_target(broadcast_id: int, chat_id: int, status: str = 'pending'):
    async def _do():
        async with AsyncSessionLocal() as s:
            t = BroadcastTarget(broadcast_id=broadcast_id, chat_id=chat_id, status=status)
            s.add(t)
            await s.commit()
            return t.id
    return await db_retry(_do)

async def update_target_status(target_id: int, status: str, error_text: str = None):
    async def _do():
        async with AsyncSessionLocal() as s:
            vals = {"status": status, "sent_at": datetime.now()}
            if error_text:
                vals["error_text"] = error_text
            await s.execute(update(BroadcastTarget).where(BroadcastTarget.id == target_id).values(**vals))
            await s.commit()
    await db_retry(_do)

async def get_total_chats():
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(func.count()).select_from(Chat))
        return r.scalar()

async def get_last_broadcast():
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Broadcast.created_at).order_by(Broadcast.created_at.desc()).limit(1))
        return r.scalar()

async def get_total_sent_messages():
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(func.count()).select_from(BroadcastTarget).where(BroadcastTarget.status == 'success'))
        return r.scalar()

async def get_total_errors():
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(func.count()).select_from(BroadcastTarget).where(BroadcastTarget.status == 'error'))
        return r.scalar()

async def get_user_stats():
    async with AsyncSessionLocal() as s:
        total = (await s.execute(select(func.count()).select_from(User))).scalar() or 0
        blocked = (await s.execute(select(func.count()).where(User.is_blocked == True))).scalar() or 0
        total_balance = (await s.execute(select(func.sum(User.balance)))).scalar() or 0.0
        return {'total': total, 'blocked': blocked, 'active': total - blocked, 'total_balance': total_balance}

async def get_all_users():
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(User))
        return r.scalars().all()

async def get_user(user_id: int):
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(User).where(User.user_id == user_id))
        return r.scalar_one_or_none()

async def create_user_if_not_exists(user_id: int, username: str, first_name: str):
    async def _do():
        async with AsyncSessionLocal() as s:
            user = await s.get(User, user_id)
            if not user:
                s.add(User(user_id=user_id, username=username, first_name=first_name))
                await s.commit()
    await db_retry(_do)

async def update_balance(user_id: int, amount: float, reason: str):
    async def _do():
        async with AsyncSessionLocal() as s:
            user = await s.get(User, user_id)
            if user:
                user.balance += amount
                s.add(BalanceHistory(user_id=user_id, amount=amount, reason=reason))
                await s.commit()
    await db_retry(_do)

async def block_user(user_id: int):
    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.user_id == user_id).values(is_blocked=True))
        await s.commit()

async def unblock_user(user_id: int):
    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.user_id == user_id).values(is_blocked=False))
        await s.commit()

async def get_user_subscription(user_id: int):
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Subscription).where(Subscription.user_id == user_id, Subscription.is_active == True))
        return r.scalar_one_or_none()

async def assign_subscription(user_id: int, plan: str, start_date: datetime, end_date: datetime, auto_renew=False):
    async with AsyncSessionLocal() as s:
        await s.execute(update(Subscription).where(Subscription.user_id == user_id, Subscription.is_active == True).values(is_active=False))
        s.add(Subscription(user_id=user_id, plan=plan, start_date=start_date, end_date=end_date, auto_renew=auto_renew))
        await s.commit()

async def get_active_subscriptions():
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Subscription).where(Subscription.is_active == True))
        return r.scalars().all()

async def get_expired_subscriptions():
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Subscription).where(Subscription.is_active == True, Subscription.end_date < datetime.now()))
        return r.scalars().all()

async def update_subscription_status(sub_id: int, is_active: bool):
    async with AsyncSessionLocal() as s:
        await s.execute(update(Subscription).where(Subscription.id == sub_id).values(is_active=is_active))
        await s.commit()

async def renew_subscription(sub: Subscription) -> bool:
    now = datetime.now()
    delta = {"3days": timedelta(days=3), "week": timedelta(weeks=1), "month": timedelta(days=30), "year": timedelta(days=365)}
    if sub.plan not in delta:
        return False
    new_end = now + delta[sub.plan]
    async with AsyncSessionLocal() as s:
        await s.execute(update(Subscription).where(Subscription.id == sub.id).values(start_date=now, end_date=new_end))
        await s.commit()
    return True

async def get_payment_methods():
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(PaymentMethod).where(PaymentMethod.is_active == True))
        return r.scalars().all()

async def add_payment_method(name: str, details: dict):
    async with AsyncSessionLocal() as s:
        s.add(PaymentMethod(name=name, details=details))
        await s.commit()

async def get_payments(limit=20):
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Payment).order_by(Payment.created_at.desc()).limit(limit))
        return r.scalars().all()

# ==================== СЕССИИ ====================

async def add_session_safe(session_name: str, phone: str, first_name: str = None, username: str = None, owner_id: int = 0):
    async def _do():
        async with AsyncSessionLocal() as s:
            ex = await s.execute(select(SessionAccount).where(SessionAccount.session_name == session_name))
            if ex.scalar_one_or_none():
                return False
            s.add(SessionAccount(
                session_name=session_name, phone=phone,
                first_name=first_name, username=username,
                owner_id=owner_id
            ))
            await s.commit()
            return True
    return await db_retry(_do, max_retries=15)

async def get_all_sessions():
    """Все сессии (только для админов)"""
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(SessionAccount))
        return r.scalars().all()

async def get_active_sessions():
    """Активные глобальные сессии (owner_id=0) — для рассылки/вступления в чаты через бота-рассыльщика"""
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(SessionAccount).where(
            SessionAccount.is_active == True,
            SessionAccount.is_working == True,
            (SessionAccount.owner_id == 0) | (SessionAccount.owner_id == None)
        ))
        return r.scalars().all()

async def get_user_sessions(user_id: int):
    """Активные личные сессии конкретного пользователя"""
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(SessionAccount).where(
            SessionAccount.owner_id == user_id,
            SessionAccount.is_active == True,
            SessionAccount.is_working == True
        ))
        return r.scalars().all()

async def get_user_all_sessions(user_id: int):
    """Все личные сессии пользователя (включая неактивные)"""
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(SessionAccount).where(
            SessionAccount.owner_id == user_id
        ))
        return r.scalars().all()

async def update_session_status(session_id: int, is_working: bool):
    async def _do():
        async with AsyncSessionLocal() as s:
            await s.execute(update(SessionAccount).where(SessionAccount.id == session_id).values(is_working=is_working, last_check=datetime.now()))
            await s.commit()
    await db_retry(_do, max_retries=5)

async def increment_messages_sent(session_id: int):
    async def _do():
        async with AsyncSessionLocal() as s:
            acc = await s.get(SessionAccount, session_id)
            if acc:
                acc.messages_sent += 1
                acc.messages_sent_today += 1
                await s.commit()
    await db_retry(_do, max_retries=5)

async def reset_daily_counters():
    async with AsyncSessionLocal() as s:
        await s.execute(update(SessionAccount).values(messages_sent_today=0, last_reset=datetime.now()))
        await s.commit()

async def get_spam_stats():
    sessions = await get_all_sessions()
    active = [s for s in sessions if s.is_active and s.is_working]
    total_capacity = sum(s.daily_limit - s.messages_sent_today for s in active)
    return {
        'total': len(sessions),
        'active': len(active),
        'dead': len(sessions) - len(active),
        'total_messages': sum(s.messages_sent for s in sessions),
        'daily_capacity': max(0, total_capacity)
    }

# ==================== РАССЫЛКА С АНТИБАН ====================

async def get_peer_safe(client: Client, chat_id: int, chat_obj):
    """Попытка получить peer разными способами"""
    # 1. По username
    if chat_obj and chat_obj.username:
        try:
            return await client.get_chat(f"@{chat_obj.username}")
        except Exception:
            pass
    # 2. По invite_link
    if chat_obj and chat_obj.invite_link:
        try:
            return await client.get_chat(chat_obj.invite_link)
        except Exception:
            pass
    # 3. По числовому ID напрямую
    try:
        return await client.get_chat(chat_id)
    except Exception:
        pass
    return None

async def join_chat_safe(client: Client, chat_id: int, chat_obj) -> bool:
    """Вступление в чат, возвращает True если успешно. Таймаут 45 сек."""
    async def _try_join(target):
        try:
            await asyncio.wait_for(client.join_chat(target), timeout=45)
            return True
        except UserAlreadyParticipant:
            return True
        except asyncio.TimeoutError:
            logger.warning(f"Таймаут при вступлении в {target}")
            return False
        except Exception:
            return False

    # 1. По username
    if chat_obj and chat_obj.username:
        ok = await _try_join(f"@{chat_obj.username}")
        if ok:
            return True
    # 2. По invite_link
    if chat_obj and chat_obj.invite_link:
        ok = await _try_join(chat_obj.invite_link)
        if ok:
            return True
    # 3. По числовому ID (только если chat_id > 0)
    if chat_id:
        try:
            result = await asyncio.wait_for(client.join_chat(chat_id), timeout=45)
            return True
        except UserAlreadyParticipant:
            return True
        except asyncio.TimeoutError:
            logger.warning(f"Таймаут при вступлении в {chat_id}")
            return False
        except Exception as e:
            logger.warning(f"Не удалось вступить в {chat_id}: {e}")
            return False
    return False

async def send_message_safe(client: Client, chat_id: int, msg_data: dict):
    """Отправка сообщения — возвращает True/False"""
    try:
        if msg_data.get('photo'):
            await client.send_photo(chat_id, msg_data['photo'], caption=msg_data.get('caption', ''))
        elif msg_data.get('video'):
            await client.send_video(chat_id, msg_data['video'], caption=msg_data.get('caption', ''))
        elif msg_data.get('document'):
            await client.send_document(chat_id, msg_data['document'], caption=msg_data.get('caption', ''))
        else:
            await client.send_message(chat_id, msg_data.get('text', ''))
        return True
    except FloodWait as e:
        logger.warning(f"FloodWait {e.value}s для {chat_id}")
        await asyncio.sleep(e.value + 2)
        # Повторная попытка после ожидания
        try:
            if msg_data.get('photo'):
                await client.send_photo(chat_id, msg_data['photo'], caption=msg_data.get('caption', ''))
            elif msg_data.get('video'):
                await client.send_video(chat_id, msg_data['video'], caption=msg_data.get('caption', ''))
            elif msg_data.get('document'):
                await client.send_document(chat_id, msg_data['document'], caption=msg_data.get('caption', ''))
            else:
                await client.send_message(chat_id, msg_data.get('text', ''))
            return True
        except Exception:
            return False
    except (ChatWriteForbidden, UserBannedInChannel, ChannelPrivate) as e:
        logger.warning(f"Запрещено слать в {chat_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Ошибка отправки в {chat_id}: {e}")
        return False

async def send_broadcast_with_sessions(broadcast_id: int, chat_ids: List[int], msg_data: dict, admin_id: int, session_owner: int = None):
    """
    Антибан-рассылка.
    session_owner=None → использует глобальные сессии (admin)
    session_owner=user_id → использует только личные сессии пользователя
    """
    if session_owner is not None:
        # Пользователь с подпиской — только свои сессии
        sessions = await get_user_sessions(session_owner)
        if not sessions:
            await bot.send_message(
                admin_id,
                "❌ <b>У вас нет добавленных сессий!</b>\n\n"
                "Добавьте хотя бы один аккаунт в разделе 📱 Мои сессии, "
                "затем попробуйте снова."
            )
            return
    else:
        sessions = await get_active_sessions()
        if not sessions:
            await bot.send_message(admin_id, "❌ Нет активных глобальных сессий!")
            return

    success_count = 0
    error_count = 0
    skip_count = 0

    status_msg = await bot.send_message(
        admin_id,
        f"🚀 Рассылка запущена\n📊 Аккаунтов: {len(sessions)}\n💬 Чатов: {len(chat_ids)}"
    )

    for idx, session in enumerate(sessions):
        client = None
        try:
            client = Client(
                name=session.session_name.replace('.session', ''),
                api_id=API_ID,
                api_hash=API_HASH,
                workdir=SESSIONS_DIR
            )
            await client.start()

            # Проверка дневного лимита
            if session.messages_sent_today >= session.daily_limit:
                skip_count += len(chat_ids)
                logger.info(f"Аккаунт {session.session_name} исчерпал дневной лимит")
                continue

            for chat_id in chat_ids:
                # Проверка лимита перед каждой отправкой
                if session.messages_sent_today >= session.daily_limit:
                    skip_count += 1
                    continue

                target_id = await add_broadcast_target(broadcast_id, chat_id, 'pending')
                chat_obj = await get_chat_by_chat_id(chat_id)

                # Вступаем в чат перед отправкой
                joined = await join_chat_safe(client, chat_id, chat_obj)
                if not joined:
                    # Попробуем всё равно отправить — вдруг уже в чате
                    pass

                # Небольшая пауза после вступления
                await asyncio.sleep(random.uniform(1.5, 3.0))

                # Отправляем
                ok = await send_message_safe(client, chat_id, msg_data)

                if ok:
                    await update_target_status(target_id, 'success')
                    await increment_messages_sent(session.id)
                    # Обновляем локальный счётчик
                    session.messages_sent_today += 1
                    success_count += 1
                else:
                    error_count += 1
                    await update_target_status(target_id, 'error', 'Send failed')

                # Антибан задержка — случайная между DEFAULT_DELAY и DEFAULT_DELAY*2
                delay = random.uniform(DEFAULT_DELAY, DEFAULT_DELAY * 2)
                await asyncio.sleep(delay)

            # Пауза между аккаунтами
            await asyncio.sleep(random.uniform(3, 6))

        except Exception as e:
            logger.error(f"Ошибка сессии {session.session_name}: {e}")
            await update_session_status(session.id, False)
        finally:
            if client:
                try:
                    await client.stop()
                except Exception:
                    pass

        # Обновляем статус каждые N аккаунтов
        if (idx + 1) % 3 == 0:
            try:
                await bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=status_msg.message_id,
                    text=f"🚀 Рассылка...\n✅ Успешно: {success_count}\n❌ Ошибок: {error_count}\n⏭ Пропущено: {skip_count}\n📱 Аккаунтов обработано: {idx+1}/{len(sessions)}"
                )
            except Exception:
                pass

    report = (
        f"📢 <b>Рассылка завершена!</b>\n\n"
        f"✅ Успешно: <b>{success_count}</b>\n"
        f"❌ Ошибок: <b>{error_count}</b>\n"
        f"⏭ Пропущено (лимит): <b>{skip_count}</b>\n"
        f"📊 Всего чатов: {len(chat_ids)}\n"
        f"📱 Аккаунтов использовано: {len(sessions)}"
    )
    try:
        await bot.edit_message_text(chat_id=admin_id, message_id=status_msg.message_id, text=report)
    except Exception:
        await bot.send_message(admin_id, report)


# ==================== ВСТУПЛЕНИЕ ВСЕХ СЕССИЙ В ЧАТ ====================

async def join_all_sessions_to_chat(chat_id: int, chat_obj) -> List[str]:
    """Все активные сессии вступают в чат"""
    sessions = await get_active_sessions()
    if not sessions:
        return ["❌ Нет активных сессий"]

    results = []
    for s in sessions:
        client = None
        try:
            client = Client(
                name=s.session_name.replace('.session', ''),
                api_id=API_ID,
                api_hash=API_HASH,
                workdir=SESSIONS_DIR
            )
            await client.start()
            ok = await join_chat_safe(client, chat_id, chat_obj)
            if ok:
                results.append(f"✅ {s.phone} — вступил")
            else:
                results.append(f"❌ {s.phone} — не удалось вступить")
        except Exception as e:
            results.append(f"❌ {s.phone} — {str(e)[:60]}")
        finally:
            if client:
                try:
                    await client.stop()
                except Exception:
                    pass
        await asyncio.sleep(1)
    return results


# ==================== FSM СОСТОЯНИЯ ====================

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
    change_banner = State()
    change_price_plan = State()
    change_payment_details = State()

class UserManageStates(StatesGroup):
    balance_amount = State()
    assign_sub_user = State()
    assign_sub_plan = State()

class PaymentMethodStates(StatesGroup):
    add_name = State()
    add_details = State()

class SessionStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()

class JoinChatsStates(StatesGroup):
    waiting_chat_id = State()

# ==================== КЛАВИАТУРЫ ====================

def kb_main(has_access: bool = False):
    """Клавиатура главного меню. has_access=True если у юзера подписка или он админ."""
    rows = [
        [InlineKeyboardButton(text="🔥 О боте", callback_data="about"),
         InlineKeyboardButton(text="💎 Подписки", callback_data="subscriptions_menu"),
         InlineKeyboardButton(text="💰 Баланс", callback_data="my_balance")],
    ]
    if has_access:
        rows.append([
            InlineKeyboardButton(text="✉️ Рассылка", callback_data="create_broadcast"),
            InlineKeyboardButton(text="📱 Мои сессии", callback_data="manage_sessions")
        ])
    rows.append([InlineKeyboardButton(text="📞 Контакты", callback_data="contacts")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить чат", callback_data="add_chat"),
         InlineKeyboardButton(text="📂 Список чатов", callback_data="list_chats"),
         InlineKeyboardButton(text="✉️ Рассылка", callback_data="create_broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats"),
         InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
         InlineKeyboardButton(text="👥 Пользователи", callback_data="list_users")],
        [InlineKeyboardButton(text="💳 Платежи", callback_data="payments_menu"),
         InlineKeyboardButton(text="📅 Подписки", callback_data="subscriptions_admin"),
         InlineKeyboardButton(text="📱 Сессии", callback_data="manage_sessions")],
        [InlineKeyboardButton(text="🔍 Чеккер акков", callback_data="check_accounts"),
         InlineKeyboardButton(text="📊 Статистика акков", callback_data="accounts_stats"),
         InlineKeyboardButton(text="🚪 Вступить в чат", callback_data="join_chat_manual")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
    ])

# ==================== ПРИВЕТСТВИЕ ====================

async def send_welcome(message: Message, user_id: int = None):
    uid = user_id or (message.from_user.id if message.from_user else 0)
    has_access = await is_admin_or_sub(uid) if uid else False
    text = (
        "🔥 <b>REDLINE 911 TURBO</b> 🔥\n"
        "<i>🚀 Скоростной порш для ваших рассылок</i>\n\n"
        "⚡️ Мощность: 911 л.с. 🏎️\n"
        "🏁 Разгон до 1000 сообщений: 3 сек\n\n"
        "✅ <b>Возможности:</b>\n"
        "• Рассылка с нескольких аккаунтов\n"
        "• Антибан защита\n"
        "• Система подписок\n"
        "• Полная статистика\n\n"
    )
    if has_access:
        text += "✅ <b>Подписка активна</b> — доступны рассылка и сессии\n\n"
    else:
        text += "🔒 Для рассылки нужна <b>подписка</b>. Нажми 💎 Подписки\n\n"
    text += "👇 <b>Выберите действие:</b>"
    kb = kb_main(has_access=has_access)
    try:
        if os.path.exists(BANNER_FILE):
            await message.answer_photo(FSInputFile(BANNER_FILE), caption=text, reply_markup=kb)
        elif os.path.exists(WELCOME_VIDEO_FILE):
            await message.answer_video(FSInputFile(WELCOME_VIDEO_FILE), caption=text, reply_markup=kb)
        else:
            await message.answer(text, reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка приветствия: {e}")
        await message.answer(text, reply_markup=kb)

# ==================== КОМАНДЫ ====================

@router.message(CommandStart())
async def cmd_start(message: Message):
    await create_user_if_not_exists(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await send_welcome(message, user_id=message.from_user.id)

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer("⚙️ <b>Админ-панель</b>", reply_markup=kb_admin())

@router.message(Command("work"))
async def cmd_work(message: Message):
    """Команда для пользователей с подпиской — показывает меню рассылки"""
    uid = message.from_user.id
    await create_user_if_not_exists(uid, message.from_user.username, message.from_user.first_name)
    if not await is_admin_or_sub(uid):
        await message.answer(
            "🔒 <b>Доступ закрыт</b>\n\n"
            "Для рассылки нужна активная подписка.\n"
            "Купите её в разделе 💎 Подписки."
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Создать рассылку", callback_data="create_broadcast")],
        [InlineKeyboardButton(text="📱 Мои сессии", callback_data="manage_sessions")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
    ])
    sub = await get_user_subscription(uid)
    sub_info = f"\n📅 Подписка до: <b>{sub.end_date.strftime('%d.%m.%Y')}</b>" if sub else ""
    await message.answer(
        f"🚀 <b>Панель рассылки</b>{sub_info}\n\nВыберите действие:",
        reply_markup=kb
    )

# ==================== НАВИГАЦИЯ ====================

@router.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_welcome(call.message, user_id=call.from_user.id)
    await call.answer()

@router.callback_query(F.data == "admin_panel")
async def admin_panel_cb(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Только для администраторов!", show_alert=True)
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer("⚙️ <b>Админ-панель</b>", reply_markup=kb_admin())
    await call.answer()

async def admin_check(call: CallbackQuery) -> bool:
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Только для администраторов!", show_alert=True)
        return False
    return True

# ==================== ПОЛЬЗОВАТЕЛЬСКИЕ КНОПКИ ====================

@router.callback_query(F.data == "about")
async def about_bot(call: CallbackQuery):
    text = (
        "🏎️ <b>REDLINE 911 TURBO</b>\n\n"
        "Мощный инструмент для рассылок в Telegram.\n\n"
        "• Рассылка через несколько аккаунтов\n"
        "• Антибан защита\n"
        "• Система подписок\n\n"
        "Для admin-панели: /admin"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]])
    try:
        await call.message.delete()
    except Exception:
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
    sub_text = f"Активна до: {sub.end_date.strftime('%d.%m.%Y')}" if sub else "Нет активной подписки"
    text = (
        f"💰 <b>Ваш баланс</b>\n\n"
        f"💎 Баланс: <b>{(user.balance or 0.0):.2f}₽</b>\n"
        f"📅 Подписка: {sub_text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]])
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "contacts")
async def contacts(call: CallbackQuery):
    text = "📞 <b>Контакты</b>\n\n👨‍💻 Поддержка: @redline_support"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]])
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "subscriptions_menu")
async def user_subscriptions_menu(call: CallbackQuery):
    p = SUBSCRIPTION_PRICES
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"3 дня ({p['3days']}₽)", callback_data="buy_sub_3days"),
         InlineKeyboardButton(text=f"Неделя ({p['week']}₽)", callback_data="buy_sub_week")],
        [InlineKeyboardButton(text=f"Месяц ({p['month']}₽)", callback_data="buy_sub_month"),
         InlineKeyboardButton(text=f"Год ({p['year']}₽)", callback_data="buy_sub_year")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])
    text = (
        "💎 <b>Тарифы подписки</b>\n\n"
        f"🔹 3 дня — {p['3days']}₽\n"
        f"🔹 Неделя — {p['week']}₽\n"
        f"🔹 Месяц — {p['month']}₽\n"
        f"🔹 Год — {p['year']}₽"
    )
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("buy_sub_"))
async def buy_subscription(call: CallbackQuery):
    plan = call.data.split("_", 2)[2]
    price = SUBSCRIPTION_PRICES.get(plan, 0)
    user = await get_user(call.from_user.id)
    balance = (user.balance or 0.0) if user else 0.0

    buttons = []
    if balance >= price:
        buttons.append([InlineKeyboardButton(text=f"💰 С баланса ({price}₽)", callback_data=f"pay_balance_{plan}")])
    buttons += [
        [InlineKeyboardButton(text="💳 Карта", callback_data=f"pay_card_{plan}"),
         InlineKeyboardButton(text="⭐ Stars", callback_data=f"pay_stars_{plan}")],
        [InlineKeyboardButton(text="₿ Крипто", callback_data=f"pay_crypto_{plan}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="subscriptions_menu")]
    ]
    text = f"💎 <b>{plan}</b>\n💰 Стоимость: {price}₽\n💎 Ваш баланс: {balance:.2f}₽\n\n<b>Способ оплаты:</b>"
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()

@router.callback_query(F.data.startswith("pay_balance_"))
async def pay_balance(call: CallbackQuery):
    plan = call.data.split("_", 2)[2]
    price = SUBSCRIPTION_PRICES.get(plan, 0)
    user = await get_user(call.from_user.id)
    if not user or (user.balance or 0.0) < price:
        await call.answer("❌ Недостаточно средств", show_alert=True)
        return
    now = datetime.now()
    delta = {"3days": timedelta(days=3), "week": timedelta(weeks=1), "month": timedelta(days=30), "year": timedelta(days=365)}
    end = now + delta.get(plan, timedelta(days=1))
    await update_balance(call.from_user.id, -price, f"Покупка подписки {plan}")
    await assign_subscription(call.from_user.id, plan, now, end, auto_renew=True)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(
        f"✅ <b>Подписка активирована!</b>\n\n📅 {plan} до {end.strftime('%d.%m.%Y %H:%M')}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]])
    )

@router.callback_query(F.data.startswith("pay_card_"))
async def pay_card(call: CallbackQuery):
    plan = call.data.split("_", 2)[2]
    price = SUBSCRIPTION_PRICES.get(plan, 0)
    text = f"💳 <b>Оплата картой</b>\n\n{price}₽ → карта: <code>2202 2012 3456 7890</code>\n\nОтправьте чек администратору."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=f"buy_sub_{plan}")]])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("pay_stars_"))
async def pay_stars(call: CallbackQuery):
    plan = call.data.split("_", 2)[2]
    price = SUBSCRIPTION_PRICES.get(plan, 0)
    text = f"⭐ <b>Оплата Stars</b>\n\n{price * 10} Stars → отправьте боту.\n\nВ разработке."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=f"buy_sub_{plan}")]])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("pay_crypto_"))
async def pay_crypto(call: CallbackQuery):
    plan = call.data.split("_", 2)[2]
    price = SUBSCRIPTION_PRICES.get(plan, 0)
    text = f"₿ <b>Оплата криптой</b>\n\n{price} USDT → @crypto_payment_bot\n\nОтправьте tx hash администратору."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 @crypto_payment_bot", url="https://t.me/crypto_payment_bot")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"buy_sub_{plan}")]
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

# ==================== ЧАТЫ ====================

@router.callback_query(F.data == "add_chat")
async def add_chat_start(call: CallbackQuery, state: FSMContext):
    if not await admin_check(call):
        return
    await call.message.answer(
        "📝 <b>Добавление чата</b>\n\n"
        "Отправьте username (@channel), ID чата или перешлите сообщение из нужного чата.\n\n"
        "⚠️ Все активные сессии автоматически вступят в этот чат."
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
    username = None
    invite_link = None

    if message.forward_origin and hasattr(message.forward_origin, 'chat'):
        chat = message.forward_origin.chat
        chat_id = chat.id
        title = chat.title or "Unknown"
        type_ = str(chat.type)
        username = getattr(chat, 'username', None)
    elif message.text:
        text = message.text.strip()
        # Ссылка приглашения
        if 't.me/+' in text or 'joinchat' in text:
            invite_link = text
            try:
                chat = await bot.get_chat(text)
                chat_id = chat.id
                title = chat.title or "Unknown"
                type_ = str(chat.type)
            except Exception as e:
                await message.answer(f"❌ Не удалось получить чат: {e}")
                return
        elif 't.me/' in text:
            un = text.split('t.me/')[-1].strip('/')
            username = un if un else None
            try:
                chat = await bot.get_chat(f"@{un}" if not un.startswith('@') else un)
                chat_id = chat.id
                title = chat.title or "Unknown"
                type_ = str(chat.type)
                username = getattr(chat, 'username', None)
            except Exception as e:
                await message.answer(f"❌ Ошибка: {e}")
                return
        else:
            identifier = text if text.startswith('@') else f"@{text}"
            try:
                chat = await bot.get_chat(identifier)
                chat_id = chat.id
                title = chat.title or "Unknown"
                type_ = str(chat.type)
                username = getattr(chat, 'username', None)
            except Exception:
                try:
                    chat_id = int(text)
                    chat = await bot.get_chat(chat_id)
                    title = chat.title or "Unknown"
                    type_ = str(chat.type)
                    username = getattr(chat, 'username', None)
                except Exception as e:
                    await message.answer(f"❌ Не удалось получить чат: {e}")
                    return

    if not chat_id:
        await message.answer("❌ Не удалось определить чат.")
        return

    existing = await get_chat_by_chat_id(chat_id)
    if existing:
        await message.answer(f"⚠️ Чат <b>{title}</b> уже добавлен.")
        await state.clear()
        return

    await add_chat(chat_id, title, type_, username, invite_link)

    # Создаём временный объект для join
    class _ChatObj:
        pass
    co = _ChatObj()
    co.username = username
    co.invite_link = invite_link

    await message.answer(f"✅ Чат <b>{title}</b> добавлен!\n\n🔄 Подключаю все сессии...")
    res = await join_all_sessions_to_chat(chat_id, co)
    if res:
        text_res = "\n".join(res)
        # Разбиваем если длинный
        if len(text_res) > 3000:
            parts = [text_res[i:i+3000] for i in range(0, len(text_res), 3000)]
            for part in parts:
                await message.answer(part)
        else:
            await message.answer(text_res)
    await state.clear()

@router.callback_query(F.data == "list_chats")
async def list_chats(call: CallbackQuery):
    if not await admin_check(call):
        return
    chats = await get_chats()
    if not chats:
        await call.message.answer("📭 Список чатов пуст")
        await call.answer()
        return
    buttons = [[InlineKeyboardButton(text=c.title, callback_data=f"chat_{base64.b64encode(str(c.chat_id).encode()).decode()}")] for c in chats]
    buttons.append([InlineKeyboardButton(text="🗑 Очистить все", callback_data="clear_chats_confirm"),
                    InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(f"📂 <b>Чаты</b> (всего: {len(chats)}):", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("chat_"))
async def chat_details(call: CallbackQuery):
    if not await admin_check(call):
        return
    try:
        chat_id = int(base64.b64decode(call.data.split("_", 1)[1].encode()).decode())
    except Exception:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    chat = await get_chat_by_chat_id(chat_id)
    if not chat:
        await call.answer("Чат не найден", show_alert=True)
        return
    text = f"📌 <b>{chat.title}</b>\nID: <code>{chat.chat_id}</code>\nТип: {chat.type}\nUsername: @{chat.username or '-'}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Удалить", callback_data=f"delchat_{base64.b64encode(str(chat.chat_id).encode()).decode()}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list_chats")]
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("delchat_"))
async def delete_chat_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    try:
        chat_id = int(base64.b64decode(call.data.split("_", 1)[1].encode()).decode())
    except Exception:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    chat = await get_chat_by_chat_id(chat_id)
    if chat:
        await delete_chat(chat_id)
        await call.answer(f"✅ Чат «{chat.title}» удалён", show_alert=True)
    else:
        await call.answer("❌ Чат не найден", show_alert=True)
    await list_chats(call)

@router.callback_query(F.data == "clear_chats_confirm")
async def clear_chats_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    await clear_all_chats()
    await call.answer("✅ Все чаты удалены", show_alert=True)
    await list_chats(call)

# ==================== РАССЫЛКА ====================

@router.callback_query(F.data == "create_broadcast")
async def create_broadcast_start(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if not await is_admin_or_sub(uid):
        await call.answer("⛔ Требуется подписка!", show_alert=True)
        return
    await call.message.answer(
        "✉️ <b>Создание рассылки</b>\n\n"
        "Отправьте сообщение для рассылки.\n"
        "Поддерживаются: текст, фото, видео, документы."
    )
    await state.set_state(BroadcastStates.waiting_message)
    await call.answer()

@router.message(BroadcastStates.waiting_message)
async def broadcast_message_received(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not await is_admin_or_sub(uid):
        await message.answer("❌ Требуется активная подписка.")
        await state.clear()
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
        await message.answer("❌ Неподдерживаемый тип.")
        return

    await state.update_data(message_data=msg_data)
    chats = await get_chats()
    if not chats:
        await message.answer("❌ Нет добавленных чатов.")
        await state.clear()
        return

    buttons = [[InlineKeyboardButton(text=f"☑️ {c.title}", callback_data=f"selchat_{base64.b64encode(str(c.chat_id).encode()).decode()}")] for c in chats]
    buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data="done_selection")])
    await message.answer("📋 <b>Выберите чаты для рассылки:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.update_data(selected_chats=[])
    await state.set_state(BroadcastStates.selecting_chats)

@router.callback_query(BroadcastStates.selecting_chats, F.data.startswith("selchat_"))
async def select_chat_cb(call: CallbackQuery, state: FSMContext):
    try:
        chat_id = int(base64.b64decode(call.data.split("_", 1)[1].encode()).decode())
    except Exception:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    data = await state.get_data()
    selected = data.get('selected_chats', [])
    if chat_id in selected:
        selected.remove(chat_id)
    else:
        selected.append(chat_id)
    await state.update_data(selected_chats=selected)
    chats = await get_chats()
    buttons = [[InlineKeyboardButton(text=f"{'✅' if c.chat_id in selected else '☑️'} {c.title}", callback_data=f"selchat_{base64.b64encode(str(c.chat_id).encode()).decode()}")] for c in chats]
    buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data="done_selection")])
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()

@router.callback_query(BroadcastStates.selecting_chats, F.data == "done_selection")
async def done_selection(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get('selected_chats', [])
    if not selected:
        await call.answer("❌ Выберите хотя бы один чат", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast")]
    ])
    await call.message.edit_text(f"📨 Разослать в <b>{len(selected)}</b> чат(ов). Подтвердить?", reply_markup=kb)
    await state.set_state(BroadcastStates.confirming)
    await call.answer()

@router.callback_query(BroadcastStates.confirming, F.data == "confirm_broadcast")
async def confirm_broadcast(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    msg_data = data['message_data']
    selected = data['selected_chats']
    uid = call.from_user.id
    is_adm = await is_admin(uid)
    broadcast_id = await create_broadcast(msg_data)
    await call.message.edit_text("🚀 <b>Рассылка запущена!</b>")
    # Админ → глобальные сессии (session_owner=None), юзер → свои личные
    session_owner = None if is_adm else uid
    asyncio.create_task(send_broadcast_with_sessions(broadcast_id, selected, msg_data, uid, session_owner))
    await state.clear()
    await call.answer()

@router.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Рассылка отменена.")
    await call.answer()

# ==================== СЕССИИ ====================

@router.callback_query(F.data == "manage_sessions")
async def manage_sessions_cb(call: CallbackQuery):
    uid = call.from_user.id
    if not await is_admin_or_sub(uid):
        await call.answer("⛔ Требуется подписка!", show_alert=True)
        return
    is_adm = await is_admin(uid)

    # Админ видит ВСЕ сессии, юзер — только свои личные
    if is_adm:
        sessions = await get_all_sessions()
        text = "📱 <b>Управление сессиями (все)</b>\n\n"
    else:
        sessions = await get_user_all_sessions(uid)
        text = "📱 <b>Мои сессии</b>\n\n"

    if sessions:
        for s in sessions:
            status = "✅" if (s.is_active and s.is_working) else "❌"
            text += f"{status} <b>{s.phone}</b> — {s.messages_sent_today}/{s.daily_limit} сегодня\n"
    else:
        if is_adm:
            text += "Нет сессий.\n"
        else:
            text += "У вас нет сессий.\n➕ Добавьте аккаунт для рассылки!\n"

    buttons = [
        [InlineKeyboardButton(text="📱 Добавить аккаунт по номеру", callback_data="add_session_phone")],
        [InlineKeyboardButton(text="🔍 Проверить все", callback_data="check_accounts")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="accounts_stats")],
    ]
    if is_adm:
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    else:
        buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()

# ==================== СОЗДАНИЕ СЕССИИ ====================

@router.callback_query(F.data.in_({"add_session_phone", "create_session_phone"}))
async def add_session_phone_start(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if not await is_admin_or_sub(uid):
        await call.answer("⛔ Требуется подписка или права администратора!", show_alert=True)
        return
    text = (
        "📱 <b>Добавить аккаунт</b>\n\n"
        "Введите номер телефона в международном формате:\n"
        "<code>+48500255953</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="manage_sessions")]])
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await state.set_state(SessionStates.waiting_phone)
    await call.answer()

@router.message(SessionStates.waiting_phone)
async def session_phone_process(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not phone.startswith('+') or len(phone) < 10:
        await message.answer("❌ Неверный формат. Пример: <code>+48500255953</code>")
        return

    session_name = f"session_{phone.replace('+','').replace(' ','').replace('-','')}_{int(datetime.now().timestamp())}"
    await state.update_data(phone=phone, session_name=session_name)
    await message.answer("📲 Отправляю код в Telegram...")

    uid = message.from_user.id
    client = None
    try:
        client = Client(
            name=session_name,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSIONS_DIR,
            in_memory=False
        )
        await client.connect()
        logger.info(f"Pyrogram connected for {phone}, sending code...")

        sent_code = await client.send_code(phone)
        logger.info(f"Code sent to {phone}, hash={sent_code.phone_code_hash[:8]}...")

        _active_clients[uid] = client
        await state.update_data(phone_code_hash=sent_code.phone_code_hash)
        await message.answer("✅ Код отправлен!\n\nВведите код из Telegram:")
        await state.set_state(SessionStates.waiting_code)

    except FloodWait as e:
        await message.answer(f"⏳ Telegram просит подождать {e.value} секунд. Попробуйте позже.")
        if client:
            try: await client.disconnect()
            except Exception: pass
        _active_clients.pop(uid, None)
        await state.clear()
    except Exception as e:
        logger.error(f"send_code error for {phone}: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:300]}")
        if client:
            try: await client.disconnect()
            except Exception: pass
        _active_clients.pop(uid, None)
        await state.clear()

@router.message(SessionStates.waiting_code)
async def session_code_process(message: Message, state: FSMContext):
    code = message.text.strip().replace(' ', '')  # убираем пробелы из кода
    data = await state.get_data()
    uid = message.from_user.id
    client = _active_clients.get(uid)
    phone = data.get('phone')
    phone_code_hash = data.get('phone_code_hash')
    session_name = data.get('session_name')

    if not client:
        await message.answer("❌ Сессия утеряна. Начните заново.")
        await state.clear()
        return

    if not phone_code_hash:
        await message.answer("❌ Ошибка: phone_code_hash отсутствует. Начните заново.")
        _active_clients.pop(uid, None)
        await state.clear()
        return

    try:
        # Определяем правильное имя параметра для этой версии Pyrogram
        import inspect
        _signin_params = list(inspect.signature(pyrogram_raw.functions.auth.SignIn.__init__).parameters.keys())
        _phone_key = 'phone_number' if 'phone_number' in _signin_params else 'phone'

        signed_in = await client.invoke(
            pyrogram_raw.functions.auth.SignIn(**{
                _phone_key: phone,
                'phone_code_hash': phone_code_hash,
                'phone_code': code
            })
        )
        await _finish_session(message, state, client, session_name)
    except SessionPasswordNeeded:
        await message.answer("🔒 Включена 2FA. Введите пароль:")
        await state.set_state(SessionStates.waiting_password)
    except PhoneCodeInvalid:
        await message.answer("❌ Неверный код. Попробуйте ещё раз:")
    except Exception as e:
        err_str = str(e)
        logger.error(f"sign_in error: {err_str}")
        upper = err_str.upper()
        if "PHONE_CODE_EXPIRED" in upper:
            # Код просрочен — автоматически запрашиваем новый
            try:
                sent_code = await client.send_code(phone)
                await state.update_data(phone_code_hash=sent_code.phone_code_hash)
                await message.answer(
                    "⏰ Код просрочен. Отправил новый!\n\n"
                    "Введите новый код из Telegram:"
                )
            except Exception as resend_err:
                await message.answer(f"❌ Код просрочен и не удалось отправить новый: {str(resend_err)[:150]}\n\nНачните заново.")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                _active_clients.pop(uid, None)
                await state.clear()
        elif "PHONE_CODE_INVALID" in upper:
            await message.answer("❌ Неверный код. Попробуйте ещё раз:")
        elif "SESSION_PASSWORD_NEEDED" in upper:
            await message.answer("🔒 Включена 2FA. Введите пароль:")
            await state.set_state(SessionStates.waiting_password)
        else:
            await message.answer(f"❌ Ошибка авторизации: {err_str[:250]}")
            try:
                await client.disconnect()
            except Exception:
                pass
            _active_clients.pop(uid, None)
            await state.clear()

@router.message(SessionStates.waiting_password)
async def session_password_process(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    uid = message.from_user.id
    client = _active_clients.get(uid)
    session_name = data.get('session_name')

    if not client:
        await message.answer("❌ Сессия утеряна. Начните заново.")
        await state.clear()
        return

    try:
        await client.check_password(password)
        await _finish_session(message, state, client, session_name)
    except PasswordHashInvalid:
        await message.answer("❌ Неверный пароль! Попробуйте ещё раз:")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:200]}")
        try:
            await client.disconnect()
        except Exception:
            pass
        _active_clients.pop(uid, None)
        await state.clear()

async def _finish_session(message: Message, state: FSMContext, client, session_name: str):
    uid = message.from_user.id
    try:
        me = await client.get_me()
        session_file = f"{session_name}.session"
        # Определяем owner_id: 0 = глобальная (для адм рассылок), uid = личная
        owner_id = 0 if await is_admin(uid) else uid
        ok = await add_session_safe(
            session_file, me.phone_number or "",
            me.first_name, me.username,
            owner_id=owner_id
        )

        if ok:
            owner_label = "(глобальная, для рассылок бота)" if owner_id == 0 else "(личная, только ваша)"
            await message.answer(
                f"✅ <b>Аккаунт добавлен!</b> {owner_label}\n"
                f"👤 {me.first_name or '—'}\n"
                f"📱 {me.phone_number or ''}\n\n"
                f"🔄 Вступаю во все добавленные чаты..."
            )
            chats = await get_chats()
            join_results = []
            for chat in chats:
                ok_join = await join_chat_safe(client, chat.chat_id, chat)
                icon = "✅" if ok_join else "❌"
                join_results.append(f"{icon} {chat.title}")
                await asyncio.sleep(1.5)

            if join_results:
                await message.answer("📋 Вступление в чаты:\n" + "\n".join(join_results))
            else:
                await message.answer("ℹ️ Чаты для вступления не найдены (добавьте чаты через админ-панель).")
        else:
            await message.answer("⚠️ Сессия уже существует в базе.")
    except Exception as e:
        await message.answer(f"❌ Ошибка при сохранении: {str(e)[:200]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        _active_clients.pop(uid, None)
        await state.clear()

# ==================== ЧЕККЕР / СТАТИСТИКА ====================

@router.callback_query(F.data == "check_accounts")
async def check_accounts_cb(call: CallbackQuery):
    uid = call.from_user.id
    is_adm = await is_admin(uid)
    if not is_adm and not await has_active_sub(uid):
        await call.answer("⛔ Только для администраторов или с подпиской!", show_alert=True)
        return
    # Админ проверяет все сессии, юзер — только свои
    if is_adm:
        sessions = await get_all_sessions()
    else:
        sessions = await get_user_all_sessions(uid)
    if not sessions:
        label = "сессий" if is_adm else "ваших сессий"
        await call.message.answer(f"❌ Нет {label}")
        await call.answer()
        return
    await call.message.answer(f"🔍 Проверяю {len(sessions)} аккаунтов...")

    live, dead = 0, 0
    lines = []
    for s in sessions:
        try:
            async with Client(s.session_name.replace('.session', ''), api_id=API_ID, api_hash=API_HASH, workdir=SESSIONS_DIR) as app:
                me = await app.get_me()
                live += 1
                lines.append(f"✅ {s.phone} — {me.first_name or '?'}")
                await update_session_status(s.id, True)
        except Exception as e:
            dead += 1
            lines.append(f"❌ {s.phone} — {str(e)[:40]}")
            await update_session_status(s.id, False)

    text = "🔍 <b>Результаты</b>\n\n" + "\n".join(lines) + f"\n\n✅ Живых: {live} | ❌ Мёртвых: {dead}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="manage_sessions")]])
    try:
        await call.message.delete()
    except Exception:
        pass
    # Разбиваем если длинный
    if len(text) > 4000:
        await call.message.answer(text[:4000])
        await call.message.answer(text[4000:], reply_markup=kb)
    else:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "accounts_stats")
async def accounts_stats_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    stats = await get_spam_stats()
    text = (
        f"📊 <b>Статистика аккаунтов</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📱 Всего: {stats['total']}\n"
        f"✅ Активных: {stats['active']}\n"
        f"❌ Мёртвых: {stats['dead']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📨 Отправлено всего: {stats['total_messages']}\n"
        f"📊 Мощность сегодня: {stats['daily_capacity']} сообщений\n"
        f"━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сбросить счётчики", callback_data="reset_counters")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="manage_sessions")]
    ])
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "reset_counters")
async def reset_counters_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    await reset_daily_counters()
    await call.answer("✅ Счётчики сброшены!", show_alert=True)
    await accounts_stats_cb(call)

# ==================== РУЧНОЕ ВСТУПЛЕНИЕ ====================

@router.callback_query(F.data == "join_chat_manual")
async def join_chat_manual_start(call: CallbackQuery, state: FSMContext):
    if not await admin_check(call):
        return
    await call.message.answer("🚪 Введите ссылку или username чата (@channel или https://t.me/...):")
    await state.set_state(JoinChatsStates.waiting_chat_id)
    await call.answer()

@router.message(JoinChatsStates.waiting_chat_id)
async def join_chat_manual_process(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    chat_input = message.text.strip()
    sessions = await get_active_sessions()
    if not sessions:
        await message.answer("❌ Нет активных сессий!")
        return

    await message.answer(f"🚀 Вступаю в чат с {len(sessions)} аккаунтов...")
    results = []

    class _CO:
        username = chat_input.lstrip('@') if not chat_input.startswith('http') else None
        invite_link = chat_input if chat_input.startswith('http') else None

    for s in sessions:
        client = None
        try:
            client = Client(s.session_name.replace('.session', ''), api_id=API_ID, api_hash=API_HASH, workdir=SESSIONS_DIR)
            await client.start()
            ok = await join_chat_safe(client, 0, _CO())
            results.append(f"{'✅' if ok else '❌'} {s.phone}")
        except Exception as e:
            results.append(f"❌ {s.phone} — {str(e)[:50]}")
        finally:
            if client:
                try:
                    await client.stop()
                except Exception:
                    pass
        await asyncio.sleep(1)

    await message.answer("📋 Результаты:\n" + "\n".join(results))
    await state.clear()

# ==================== СТАТИСТИКА ОБЩАЯ ====================

@router.callback_query(F.data == "show_stats")
async def show_stats_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    total_chats = await get_total_chats()
    last_bc = await get_last_broadcast()
    sent = await get_total_sent_messages()
    errors = await get_total_errors()
    user_stats = await get_user_stats()
    spam_stats = await get_spam_stats()
    text = (
        f"📊 <b>Статистика</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📂 Чатов: {total_chats}\n"
        f"📅 Последняя рассылка: {last_bc.strftime('%d.%m.%Y %H:%M') if last_bc else 'нет'}\n"
        f"📨 Отправлено: {sent}\n"
        f"⚠️ Ошибок: {errors}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👥 Пользователей: {user_stats['total']} (активных: {user_stats['active']})\n"
        f"💰 Общий баланс: {user_stats['total_balance']:.2f}₽\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📱 Аккаунтов: {spam_stats['total']} (живых: {spam_stats['active']})\n"
        f"📊 Мощность: {spam_stats['daily_capacity']}/день"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

# ==================== НАСТРОЙКИ ====================

@router.callback_query(F.data == "settings")
async def settings_menu_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱️ Задержка", callback_data="set_delay"),
         InlineKeyboardButton(text="🖼️ Баннер", callback_data="change_banner")],
        [InlineKeyboardButton(text="💰 Цены подписок", callback_data="change_subscription_prices"),
         InlineKeyboardButton(text="💳 Реквизиты", callback_data="change_payment_details")],
        [InlineKeyboardButton(text="👤 Админы", callback_data="manage_admins"),
         InlineKeyboardButton(text="🗑 Очистить чаты", callback_data="clear_chats_confirm")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])
    await call.message.edit_text("⚙️ <b>Настройки</b>", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "set_delay")
async def set_delay_prompt(call: CallbackQuery, state: FSMContext):
    await call.message.answer("⏱️ Введите задержку между сообщениями (сек, 1-30):")
    await state.set_state(SettingsStates.set_delay)
    await call.answer()

@router.message(SettingsStates.set_delay)
async def set_delay_process(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    try:
        delay = int(message.text)
        if 1 <= delay <= 30:
            global DEFAULT_DELAY
            DEFAULT_DELAY = delay
            await message.answer(f"✅ Задержка: {delay} сек.")
        else:
            await message.answer("❌ Введите от 1 до 30.")
    except Exception:
        await message.answer("❌ Введите число.")
    await state.clear()

@router.callback_query(F.data == "change_banner")
async def change_banner_start(call: CallbackQuery, state: FSMContext):
    if not await admin_check(call):
        return
    await call.message.answer("🖼️ Отправьте изображение (banner.jpg) или видео (welcome.mp4):")
    await state.set_state(SettingsStates.change_banner)
    await call.answer()

@router.message(SettingsStates.change_banner)
async def process_banner(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    try:
        if message.photo:
            await bot.download(message.photo[-1], destination=BANNER_FILE)
            await message.answer(f"✅ Баннер обновлён.")
        elif message.document and message.document.file_name.endswith('.mp4'):
            await bot.download(message.document, destination=WELCOME_VIDEO_FILE)
            await message.answer(f"✅ Видео обновлено.")
        else:
            await message.answer("❌ Отправьте фото или .mp4")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()

@router.callback_query(F.data == "manage_admins")
async def manage_admins_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    admins = await get_all_admins()
    text = "👥 <b>Администраторы</b>\n\n"
    for a in admins:
        text += f"• <code>{a.user_id}</code>\n"
    if not admins:
        text += "Список пуст"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="add_admin"),
         InlineKeyboardButton(text="❌ Удалить", callback_data="remove_admin")],
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
        uid = int(message.text)
        await add_admin(uid)
        await message.answer(f"✅ Добавлен администратор <code>{uid}</code>.")
    except Exception:
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
        uid = int(message.text)
        await remove_admin(uid)
        await message.answer(f"✅ Удалён администратор <code>{uid}</code>.")
    except Exception:
        await message.answer("❌ Неверный ID.")
    await state.clear()

@router.callback_query(F.data == "change_subscription_prices")
async def change_prices_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    p = SUBSCRIPTION_PRICES
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 дня", callback_data="set_price_3days"),
         InlineKeyboardButton(text="Неделя", callback_data="set_price_week")],
        [InlineKeyboardButton(text="Месяц", callback_data="set_price_month"),
         InlineKeyboardButton(text="Год", callback_data="set_price_year")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="settings")]
    ])
    text = f"💰 <b>Цены подписок</b>\n\n3 дня: {p['3days']}₽\nНеделя: {p['week']}₽\nМесяц: {p['month']}₽\nГод: {p['year']}₽\n\nВыберите план:"
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("set_price_"))
async def set_price_prompt(call: CallbackQuery, state: FSMContext):
    plan = call.data.replace("set_price_", "")
    await state.update_data(price_plan=plan)
    await call.message.answer(f"💰 Текущая цена {plan}: {SUBSCRIPTION_PRICES.get(plan)}₽\nВведите новую цену:")
    await state.set_state(SettingsStates.change_price_plan)
    await call.answer()

@router.message(SettingsStates.change_price_plan)
async def process_new_price(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    try:
        price = int(message.text)
        data = await state.get_data()
        SUBSCRIPTION_PRICES[data['price_plan']] = price
        await message.answer(f"✅ Цена {data['price_plan']} → {price}₽")
    except Exception:
        await message.answer("❌ Введите число.")
    await state.clear()

@router.callback_query(F.data == "change_payment_details")
async def change_payment_menu(call: CallbackQuery):
    if not await admin_check(call):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Карта", callback_data="edit_payment_card")],
        [InlineKeyboardButton(text="⭐ Stars", callback_data="edit_payment_stars")],
        [InlineKeyboardButton(text="₿ Крипто", callback_data="edit_payment_crypto")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="settings")]
    ])
    await call.message.edit_text("💳 Выберите способ оплаты:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("edit_payment_"))
async def edit_payment_prompt(call: CallbackQuery, state: FSMContext):
    method = call.data.replace("edit_payment_", "")
    await state.update_data(payment_method=method)
    prompts = {"card": "Введите номер карты:", "stars": "Введите @бот или username:", "crypto": "Введите адрес кошелька:"}
    await call.message.answer(prompts.get(method, "Введите реквизиты:"))
    await state.set_state(SettingsStates.change_payment_details)
    await call.answer()

@router.message(SettingsStates.change_payment_details)
async def process_payment_details(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    await message.answer(f"✅ Реквизиты {data['payment_method']} обновлены: {message.text.strip()}")
    await state.clear()

# ==================== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ ====================

@router.callback_query(F.data == "list_users")
async def list_users_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    users = await get_all_users()
    if not users:
        await call.message.edit_text("👥 Пользователей нет")
        await call.answer()
        return
    buttons = []
    for u in users[:20]:
        name = u.first_name or "Без имени"
        un = f" (@{u.username})" if u.username else ""
        icon = "🔒" if u.is_blocked else "✅"
        buttons.append([InlineKeyboardButton(text=f"{icon} {name}{un}", callback_data=f"userinfo_{u.user_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await call.message.edit_text("👥 <b>Пользователи</b>:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()

@router.callback_query(F.data.startswith("userinfo_"))
async def user_info_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    try:
        uid = int(call.data.replace("userinfo_", ""))
    except Exception:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    user = await get_user(uid)
    if not user:
        await call.answer("❌ Не найден", show_alert=True)
        return
    sub = await get_user_subscription(uid)
    sub_text = f"✅ до {sub.end_date.strftime('%d.%m.%Y')}" if sub else "❌ Нет"
    text = (
        f"👤 <b>ID:</b> <code>{uid}</code>\n"
        f"<b>Имя:</b> {user.first_name or '-'}\n"
        f"<b>Username:</b> @{user.username or '-'}\n"
        f"<b>Статус:</b> {'🔒 Заблокирован' if user.is_blocked else '✅ Активен'}\n"
        f"💰 <b>Баланс:</b> {(user.balance or 0.0):.2f}₽\n"
        f"📅 <b>Подписка:</b> {sub_text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 +Баланс", callback_data=f"addbalance_{uid}"),
         InlineKeyboardButton(text="💸 -Баланс", callback_data=f"subbalance_{uid}")],
        [InlineKeyboardButton(text="📅 +Подписка", callback_data=f"addsub_{uid}"),
         InlineKeyboardButton(text="❌ Отменить подписку", callback_data=f"cancelsub_{uid}")],
        [InlineKeyboardButton(
            text="🔓 Разблокировать" if user.is_blocked else "🔒 Заблокировать",
            callback_data=f"toggleblock_{uid}"
        )],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list_users")]
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("addbalance_"))
async def add_balance_start(call: CallbackQuery, state: FSMContext):
    if not await admin_check(call):
        return
    uid = int(call.data.replace("addbalance_", ""))
    await state.update_data(target_user_id=uid, action="add")
    await call.message.answer("💰 Введите сумму для пополнения:")
    await state.set_state(UserManageStates.balance_amount)
    await call.answer()

@router.callback_query(F.data.startswith("subbalance_"))
async def sub_balance_start(call: CallbackQuery, state: FSMContext):
    if not await admin_check(call):
        return
    uid = int(call.data.replace("subbalance_", ""))
    await state.update_data(target_user_id=uid, action="sub")
    await call.message.answer("💸 Введите сумму для списания:")
    await state.set_state(UserManageStates.balance_amount)
    await call.answer()

@router.message(UserManageStates.balance_amount)
async def process_balance_amount(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    uid = data.get('target_user_id')
    action = data.get('action')
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            await message.answer("❌ Сумма > 0")
            return
        if action == "add":
            await update_balance(uid, amount, "admin_add")
            await message.answer(f"✅ Пополнено {amount}₽")
        else:
            await update_balance(uid, -amount, "admin_sub")
            await message.answer(f"✅ Списано {amount}₽")
    except Exception:
        await message.answer("❌ Введите число")
        return
    await state.clear()

@router.callback_query(F.data.startswith("toggleblock_"))
async def toggle_block_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    uid = int(call.data.replace("toggleblock_", ""))
    user = await get_user(uid)
    if not user:
        await call.answer("❌ Не найден", show_alert=True)
        return
    if user.is_blocked:
        await unblock_user(uid)
        await call.answer("✅ Разблокирован", show_alert=True)
    else:
        await block_user(uid)
        await call.answer("✅ Заблокирован", show_alert=True)

@router.callback_query(F.data.startswith("addsub_"))
async def add_sub_start(call: CallbackQuery):
    if not await admin_check(call):
        return
    uid = int(call.data.replace("addsub_", ""))
    user = await get_user(uid)
    if not user:
        await call.answer("❌ Не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 дня", callback_data=f"subplan_{uid}_3days"),
         InlineKeyboardButton(text="Неделя", callback_data=f"subplan_{uid}_week")],
        [InlineKeyboardButton(text="Месяц", callback_data=f"subplan_{uid}_month"),
         InlineKeyboardButton(text="Год", callback_data=f"subplan_{uid}_year")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data=f"userinfo_{uid}")]
    ])
    await call.message.edit_text(f"📅 Выберите план для {user.first_name}:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("subplan_"))
async def confirm_sub_plan(call: CallbackQuery):
    if not await admin_check(call):
        return
    parts = call.data.split("_")
    uid = int(parts[1])
    plan = parts[2]
    delta = {"3days": timedelta(days=3), "week": timedelta(weeks=1), "month": timedelta(days=30), "year": timedelta(days=365)}
    now = datetime.now()
    end = now + delta.get(plan, timedelta(days=1))
    await assign_subscription(uid, plan, now, end)
    await call.answer("✅ Подписка выдана", show_alert=True)
    await call.message.answer(f"✅ Подписка {plan} до {end.strftime('%d.%m.%Y')}")

@router.callback_query(F.data.startswith("cancelsub_"))
async def cancel_sub_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    uid = int(call.data.replace("cancelsub_", ""))
    sub = await get_user_subscription(uid)
    if not sub:
        await call.answer("❌ Нет подписки", show_alert=True)
        return
    await update_subscription_status(sub.id, False)
    await call.answer("✅ Подписка отменена", show_alert=True)

# ==================== ПЛАТЕЖИ / ПОДПИСКИ ADMIN ====================

@router.callback_query(F.data == "payments_menu")
async def payments_menu_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    methods = await get_payment_methods()
    text = "💳 <b>Способы оплаты</b>\n\n"
    for m in methods:
        text += f"{'✅' if m.is_active else '❌'} {m.name}\n"
    if not methods:
        text += "Нет настроенных способов\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="add_payment_method")],
        [InlineKeyboardButton(text="📋 Платежи", callback_data="view_payments")],
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
    await message.answer('🔧 Введите реквизиты в JSON (пример: {"card": "1234 5678"}):')
    await state.set_state(PaymentMethodStates.add_details)

@router.message(PaymentMethodStates.add_details)
async def add_payment_details(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        details = json.loads(message.text)
        await add_payment_method(data['payment_name'], details)
        await message.answer("✅ Способ оплаты добавлен.")
    except json.JSONDecodeError:
        await message.answer("❌ Неверный JSON.")
        return
    await state.clear()

@router.callback_query(F.data == "view_payments")
async def view_payments_cb(call: CallbackQuery):
    payments = await get_payments()
    text = "💸 <b>Последние платежи</b>\n\n"
    for p in payments:
        user = await get_user(p.user_id)
        un = user.username if user else str(p.user_id)
        text += f"• {p.created_at.strftime('%d.%m %H:%M')} @{un}: {p.amount}₽ [{p.status}]\n"
    if not payments:
        text += "Нет платежей."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="payments_menu")]])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "subscriptions_admin")
async def subscriptions_admin_cb(call: CallbackQuery):
    if not await admin_check(call):
        return
    subs = await get_active_subscriptions()
    text = f"📅 <b>Активные подписки:</b> {len(subs)}\n\n"
    for sub in subs[:20]:
        user = await get_user(sub.user_id)
        un = user.username if user else str(sub.user_id)
        text += f"• @{un}: {sub.plan} до {sub.end_date.strftime('%d.%m.%Y')}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

# ==================== ПЛАНИРОВЩИК ====================

async def check_subscriptions_job():
    expired = await get_expired_subscriptions()
    for sub in expired:
        if sub.auto_renew:
            price = SUBSCRIPTION_PRICES.get(sub.plan, 0)
            user = await get_user(sub.user_id)
            if user and (user.balance or 0.0) >= price:
                ok = await renew_subscription(sub)
                if ok:
                    await update_balance(sub.user_id, -price, "auto_renew")
                    try:
                        await bot.send_message(sub.user_id, f"🔄 Подписка {sub.plan} автоматически продлена.")
                    except Exception:
                        pass
                    continue
            await update_subscription_status(sub.id, False)
            try:
                await bot.send_message(sub.user_id, "⚠️ Подписка истекла. Продлить: /start")
            except Exception:
                pass
        else:
            await update_subscription_status(sub.id, False)
            try:
                await bot.send_message(sub.user_id, "⏰ Срок подписки истёк. Продлить: /start")
            except Exception:
                pass

# ==================== ЗАПУСК ====================

async def main():
    await init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_subscriptions_job, CronTrigger(hour=0, minute=0), id="check_subscriptions_job")
    scheduler.add_job(reset_daily_counters, CronTrigger(hour=0, minute=0), id="reset_daily_counters")
    scheduler.start()

    dp.include_router(router)
    logger.info("✅ Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        logger.info("🛑 Бот остановлен")

if __name__ == '__main__':
    asyncio.run(main())