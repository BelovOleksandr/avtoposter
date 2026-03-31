# ============================================
# СИСТЕМА УПРАВЛЕНИЯ ПОЛЬЗОВАТЕЛЯМИ (НОВАЯ)
# ============================================
# Эти обработчики полностью заменяют старые функции
# управления пользователями. Модель: простые user_id в callback_data


# ---------- Список пользователей ----------
@router.callback_query(F.data == "list_users")
async def list_users(call: CallbackQuery):
    """Показывает список всех пользователей"""
    if not await admin_check(call):
        return
    
    users = await get_all_users()
    if not users:
        await call.message.edit_text("👥 <b>Список пользователей пуст</b>")
        await call.answer()
        return

    buttons = []
    # Показываем максимум 20 пользователей на странице
    for u in users[:20]:
        name = u.first_name or "Без имени"
        username_display = f" (@{u.username})" if u.username else ""
        status = "🔒" if u.is_blocked else "✅"
        display = f"{status} {name}{username_display} [ID: {u.user_id}]"
        
        buttons.append([InlineKeyboardButton(
            text=display, 
            callback_data=f"userinfo_{u.user_id}"
        )])
    
    buttons.append([InlineKeyboardButton(text="🔙 В админ панель", callback_data="admin_panel")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.message.edit_text("👥 <b>Выберите пользователя для управления</b>:", reply_markup=kb)
    await call.answer()


# ---------- Карточка пользователя ----------
@router.callback_query(F.data.startswith("userinfo_"))
async def user_info(call: CallbackQuery):
    """Показывает информацию о пользователе и доступные операции"""
    if not await admin_check(call):
        return
    
    try:
        user_id = int(call.data.replace("userinfo_", ""))
    except ValueError:
        await call.answer("❌ Неверный ID пользователя", show_alert=True)
        return
    
    user = await get_user(user_id)
    if not user:
        await call.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    # Получаем информацию о подписке
    sub = await get_user_subscription(user_id)
    sub_text = f"✅ Активна до {sub.end_date.strftime('%d.%m.%Y')}" if sub else "❌ Нет подписки"
    
    # Формируем текст карточки
    text = (
        f"👤 <b>Информация о пользователе</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>ID:</b> <code>{user.user_id}</code>\n"
        f"<b>Имя:</b> {user.first_name or 'Не указано'}\n"
        f"<b>Username:</b> @{user.username or 'Не указано'}\n"
        f"<b>Статус:</b> {'🔒 Заблокирован' if user.is_blocked else '✅ Активен'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Баланс:</b> {user.balance:.2f}₽\n"
        f"📅 <b>Подписка:</b> {sub_text}\n"
        f"📝 <b>Дата регистрации:</b> {user.created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Выберите действие:</b>"
    )
    
    # Кнопки управления
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Выдать баланс", callback_data=f"addbalance_{user_id}"),
         InlineKeyboardButton(text="💸 Отнять баланс", callback_data=f"subbalance_{user_id}")],
        
        [InlineKeyboardButton(text="📅 Выдать подписку", callback_data=f"addsub_{user_id}"),
         InlineKeyboardButton(text="❌ Отменить подписку", callback_data=f"cancelsub_{user_id}")],
        
        [InlineKeyboardButton(
            text="🔓 Разблокировать" if user.is_blocked else "🔒 Заблокировать",
            callback_data=f"toggleblock_{user_id}"
        )],
        
        [InlineKeyboardButton(text="📜 История баланса", callback_data=f"history_{user_id}")],
        
        [InlineKeyboardButton(text="🔙 К списку", callback_data="list_users")]
    ])
    
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


# ---------- Выдать баланс ----------
@router.callback_query(F.data.startswith("addbalance_"))
async def add_balance_start(call: CallbackQuery, state: FSMContext):
    """Начало процесса выдачи баланса"""
    if not await admin_check(call):
        return
    
    try:
        user_id = int(call.data.replace("addbalance_", ""))
    except ValueError:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    
    user = await get_user(user_id)
    if not user:
        await call.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    await state.update_data(target_user_id=user_id, action="add")
    await call.message.answer(
        f"💰 <b>Выдать баланс пользователю {user.first_name}</b>\n\n"
        f"Введите сумму (в рублях):"
    )
    await state.set_state(UserManageStates.balance_amount)
    await call.answer()


# ---------- Отнять баланс ----------
@router.callback_query(F.data.startswith("subbalance_"))
async def sub_balance_start(call: CallbackQuery, state: FSMContext):
    """Начало процесса снятия баланса"""
    if not await admin_check(call):
        return
    
    try:
        user_id = int(call.data.replace("subbalance_", ""))
    except ValueError:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    
    user = await get_user(user_id)
    if not user:
        await call.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    await state.update_data(target_user_id=user_id, action="sub")
    await call.message.answer(
        f"💸 <b>Отнять баланс у пользователя {user.first_name}</b>\n\n"
        f"Введите сумму (в рублях):"
    )
    await state.set_state(UserManageStates.balance_amount)
    await call.answer()


# ---------- Обработка ввода суммы баланса ----------
@router.message(UserManageStates.balance_amount)
async def process_balance_amount(message: Message, state: FSMContext):
    """Обработка введённой суммы и применение операции"""
    if not await is_admin(message.from_user.id):
        return
    
    data = await state.get_data()
    user_id = data.get('target_user_id')
    action = data.get('action')
    
    if not user_id or not action:
        await message.answer("❌ Ошибка: потеряны данные сессии")
        await state.clear()
        return
    
    user = await get_user(user_id)
    if not user:
        await message.answer("❌ Пользователь не найден")
        await state.clear()
        return
    
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            await message.answer("❌ Сумма должна быть больше 0")
            return
        
        if action == "add":
            await update_balance(user_id, amount, "admin_add")
            await message.answer(
                f"✅ <b>Успешно!</b>\n\n"
                f"Пользователю {user.first_name} выдано {amount}₽\n"
                f"Новый баланс: {user.balance + amount:.2f}₽"
            )
        elif action == "sub":
            await update_balance(user_id, -amount, "admin_sub")
            await message.answer(
                f"✅ <b>Успешно!</b>\n\n"
                f"У пользователя {user.first_name} отнято {amount}₽\n"
                f"Новый баланс: {user.balance - amount:.2f}₽"
            )
        
        # Возвращаемся к карточке пользователя
        await message.answer(
            f"Вернитесь в админ-панель или нажмите /admin\n"
            f"Пользователь: {user.first_name} [ID: {user_id}]"
        )
    
    except ValueError:
        await message.answer("❌ Введите корректное число (например: 100 или 150.50)")
        return
    
    await state.clear()


# ---------- Заблокировать/Разблокировать ----------
@router.callback_query(F.data.startswith("toggleblock_"))
async def toggle_block_user(call: CallbackQuery):
    """Блокирует или разблокирует пользователя"""
    if not await admin_check(call):
        return
    
    try:
        user_id = int(call.data.replace("toggleblock_", ""))
    except ValueError:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    
    user = await get_user(user_id)
    if not user:
        await call.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    if user.is_blocked:
        await unblock_user(user_id)
        await call.answer("✅ Пользователь разблокирован", show_alert=True)
        msg_text = f"🔓 {user.first_name} разблокирован"
    else:
        await block_user(user_id)
        await call.answer("✅ Пользователь заблокирован", show_alert=True)
        msg_text = f"🔒 {user.first_name} заблокирован"
    
    await call.message.answer(msg_text)
    
    # Возвращаемся к карточке пользователя
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="👤 Вернуться к пользователю",
        callback_data=f"userinfo_{user_id}"
    )]])
    await call.message.answer("Нажмите кнопку для возврата к карточке пользователя:", reply_markup=kb)


# ---------- Выдать подписку ----------
@router.callback_query(F.data.startswith("addsub_"))
async def add_subscription_start(call: CallbackQuery, state: FSMContext):
    """Начало процесса выдачи подписки"""
    if not await admin_check(call):
        return
    
    try:
        user_id = int(call.data.replace("addsub_", ""))
    except ValueError:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    
    user = await get_user(user_id)
    if not user:
        await call.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    await state.update_data(target_user_id=user_id)
    
    # Показываем доступные планы
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 3 дня (100₽)", callback_data=f"subplan_{user_id}_3days"),
         InlineKeyboardButton(text="📆 1 неделя (200₽)", callback_data=f"subplan_{user_id}_week")],
        
        [InlineKeyboardButton(text="📊 1 месяц (500₽)", callback_data=f"subplan_{user_id}_month"),
         InlineKeyboardButton(text="📈 1 год (5000₽)", callback_data=f"subplan_{user_id}_year")],
        
        [InlineKeyboardButton(text="🔙 Отмена", callback_data=f"userinfo_{user_id}")]
    ])
    
    await call.message.edit_text(
        f"📅 <b>Выберите план подписки для {user.first_name}</b>:\n\n"
        f"Текущие цены: \n"
        f"• 3 дня: 100₽\n"
        f"• 1 неделя: 200₽\n"
        f"• 1 месяц: 500₽\n"
        f"• 1 год: 5000₽",
        reply_markup=kb
    )
    await call.answer()


# ---------- Подтверждение плана подписки ----------
@router.callback_query(F.data.startswith("subplan_"))
async def confirm_subscription_plan(call: CallbackQuery):
    """Применяет выбранный план подписки"""
    if not await admin_check(call):
        return
    
    # subplan_{user_id}_{plan}
    parts = call.data.split("_")
    if len(parts) != 3:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    
    try:
        user_id = int(parts[1])
        plan = parts[2]
    except (ValueError, IndexError):
        await call.answer("❌ Ошибка", show_alert=True)
        return
    
    user = await get_user(user_id)
    if not user:
        await call.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    if plan not in ["3days", "week", "month", "year"]:
        await call.answer("❌ Неверный план", show_alert=True)
        return
    
    # Вычисляем дату окончания подписки
    now = datetime.now()
    if plan == "3days":
        end_date = now + timedelta(days=3)
        plan_text = "3 дня"
    elif plan == "week":
        end_date = now + timedelta(weeks=1)
        plan_text = "неделе"
    elif plan == "month":
        end_date = now + timedelta(days=30)
        plan_text = "месяцу"
    else:  # year
        end_date = now + timedelta(days=365)
        plan_text = "году"
    
    # Выдаём подписку
    await assign_subscription(user_id, plan, now, end_date)
    
    await call.answer("✅ Подписка выдана", show_alert=True)
    await call.message.answer(
        f"✅ <b>Подписка выдана!</b>\n\n"
        f"Пользователь: {user.first_name}\n"
        f"План: {plan_text}\n"
        f"Активна до: {end_date.strftime('%d.%m.%Y %H:%M')}"
    )
    
    # Возвращаемся к карточке
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="👤 К пользователю",
        callback_data=f"userinfo_{user_id}"
    )]])
    await call.message.answer("", reply_markup=kb)


# ---------- Отменить подписку ----------
@router.callback_query(F.data.startswith("cancelsub_"))
async def cancel_subscription_user(call: CallbackQuery):
    """Отменяет подписку пользователя"""
    if not await admin_check(call):
        return
    
    try:
        user_id = int(call.data.replace("cancelsub_", ""))
    except ValueError:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    
    user = await get_user(user_id)
    if not user:
        await call.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    sub = await get_user_subscription(user_id)
    if not sub:
        await call.answer("❌ У пользователя нет активной подписки", show_alert=True)
        return
    
    # Отключаем подписку
    await update_subscription_status(sub.id, False)
    
    await call.answer("✅ Подписка отменена", show_alert=True)
    await call.message.answer(
        f"❌ <b>Подписка отменена!</b>\n\n"
        f"Пользователь: {user.first_name}\n"
        f"Был план: {sub.plan}"
    )


# ---------- История баланса ----------
@router.callback_query(F.data.startswith("history_"))
async def show_balance_history(call: CallbackQuery):
    """Показывает историю операций с балансом"""
    if not await admin_check(call):
        return
    
    try:
        user_id = int(call.data.replace("history_", ""))
    except ValueError:
        await call.answer("❌ Ошибка", show_alert=True)
        return
    
    user = await get_user(user_id)
    if not user:
        await call.answer("❌ Пользователь не найден", show_alert=True)
        return
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(BalanceHistory)
            .where(BalanceHistory.user_id == user_id)
            .order_by(BalanceHistory.created_at.desc())
            .limit(15)
        )
        history = result.scalars().all()
    
    if not history:
        text = f"📜 <b>История баланса</b> ({user.first_name})\n\nЕщё нет операций"
    else:
        text = f"📜 <b>История баланса</b> ({user.first_name})\n━━━━━━━━━━━━━━━━━\n"
        for h in history:
            sign = "➕" if h.amount > 0 else "➖"
            text += f"{sign} {abs(h.amount):.2f}₽ | {h.reason}\n"
            text += f"   {h.created_at.strftime('%d.%m.%Y %H:%M')}\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🔙 К пользователю",
        callback_data=f"userinfo_{user_id}"
    )]])
    
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()
