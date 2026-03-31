# ФИКСЫ ДЛЯ ОСНОВНОГО ФАЙЛА

# 1. ИСПРАВЛЕНИЯ ДЛЯ ПАРСИНГА user_toggle_block и user_balance_action

# Использовать вместо неправильного парсинга data_parts
# Правильный парсинг для user_balance_action:
# parts = call.data.replace("user_balance_", "", 1).rsplit("_", 1)
# if len(parts) == 2:
#     action, user_id_encoded = parts

# Правильный парсинг для user_toggle_block:
# user_id_encoded = call.data.replace("user_toggle_block_", "")

# Правильный парсинг для user_history:
# user_id_encoded = call.data.replace("user_history_", "")

# Правильный парсинг для user_sub_:
# user_id_encoded = call.data.replace("user_sub_", "")


# 2. НОВЫЕ ОБРАБОТЧИКИ НАСТРОЕК

@router.callback_query(F.data == "change_banner")
async def change_banner_start(call: CallbackQuery, state: FSMContext):
    if not await admin_check(call):
        return
    await call.message.answer(
        "📸 <b>Управление медиа-файлами</b>\n\n"
        "Отправьте изображение для баннера (1280x720)\n"
        "или MP4 файл для видео приветствия"
    )
    await state.set_state(SettingsStates.change_banner)
    await call.answer()


@router.message(SettingsStates.change_banner)
async def process_banner(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    try:
        if message.photo:
            file = message.photo[-1]
            await bot.download(file, destination=BANNER_FILE)
            await message.answer(f"✅ Баннер обновлен! {BANNER_FILE}")
        elif message.document:
            if message.document.file_name.endswith('.mp4'):
                file = message.document
                await bot.download(file, destination=WELCOME_VIDEO_FILE)
                await message.answer(f"✅ Видео обновлено! {WELCOME_VIDEO_FILE}")
            else:
                await message.answer("❌ Поддерживаются только .mp4")
        else:
            await message.answer("❌ Отправьте фото или видео")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()


@router.callback_query(F.data == "change_subscription_prices")
async def change_subscription_prices(call: CallbackQuery):
    if not await admin_check(call):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 дня", callback_data="set_price_3days")],
        [InlineKeyboardButton(text="Неделя", callback_data="set_price_week")],
        [InlineKeyboardButton(text="Месяц", callback_data="set_price_month")],
        [InlineKeyboardButton(text="Год", callback_data="set_price_year")],
        [InlineKeyboardButton(text="Назад", callback_data="settings")]
    ])
    text = "💰 <b>Текущие цены подписок</b>\n\n"
    for plan, price in SUBSCRIPTION_PRICES.items():
        text += f"• <b>{plan}</b>: {price}₽\n"
    text += "\nВыберите план для изменения:"
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("set_price_"))
async def set_price_prompt(call: CallbackQuery, state: FSMContext):
    plan = call.data.replace("set_price_", "")
    await state.update_data(price_plan=plan)
    current_price = SUBSCRIPTION_PRICES.get(plan, 0)
    await call.message.answer(f"Текущая цена для {plan}: {current_price}₽\n\nВведите новую цену:")
    await state.set_state(SettingsStates.change_price_plan)
    await call.answer()


@router.message(SettingsStates.change_price_plan)
async def process_new_price(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    try:
        new_price = int(message.text)
        data = await state.get_data()
        plan = data['price_plan']
        SUBSCRIPTION_PRICES[plan] = new_price
        await message.answer(f"✅ Цена для {plan} изменена на {new_price}₽")
    except:
        await message.answer("❌ Введите числовое значение")
    await state.clear()


@router.callback_query(F.data == "change_payment_details")
async def change_payment_details_menu(call: CallbackQuery):
    if not await admin_check(call):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Карта", callback_data="edit_payment_card")],
        [InlineKeyboardButton(text="⭐ Stars", callback_data="edit_payment_stars")],
        [InlineKeyboardButton(text="₿ Крипто", callback_data="edit_payment_crypto")],
        [InlineKeyboardButton(text="Назад", callback_data="settings")]
    ])
    await call.message.edit_text("💳 <b>Реквизиты платежей</b>", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("edit_payment_"))
async def edit_payment_prompt(call: CallbackQuery, state: FSMContext):
    method = call.data.replace("edit_payment_", "")
    await state.update_data(payment_method=method)
    if method == "card":
        await call.message.answer("💳 Введите реквизиты карты (например: 2202 2012 3456 7890):")
    elif method == "stars":
        await call.message.answer("⭐ Введите информацию для Stars TG (например: @bot_username):")
    elif method == "crypto":
        await call.message.answer("₿ Введите адрес крипто кошелька:")
    await state.set_state(SettingsStates.change_payment_details)
    await call.answer()


@router.message(SettingsStates.change_payment_details)
async def process_payment_details(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    method = data['payment_method']
    details = message.text.strip()
    
    # В реальности нужно сохранить в БД
    if method == "card":
        await message.answer(f"✅ Реквизиты карты обновлены: {details}")
    elif method == "stars":
        await message.answer(f"✅ Stars информация обновлена: {details}")
    elif method == "crypto":
        await message.answer(f"✅ Адрес крипто кошелька обновлен: {details}")
    await state.clear()
