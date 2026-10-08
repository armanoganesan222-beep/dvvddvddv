import asyncio
import html
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config


BOT_TOKEN = str(getattr(config, "BOT_TOKEN", ""))
ADMIN_ID = int(getattr(config, "ADMIN_ID", 0))

if not BOT_TOKEN:
    raise RuntimeError("В config.py не найден BOT_TOKEN")
if not ADMIN_ID:
    raise RuntimeError("В config.py не найден ADMIN_ID")


DB_PATH = Path(__file__).with_name("exchange_v2.db")
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# Только одно активное состояние на пользователя.
states = {}

rates = {"TON": None, "USDT": None, "time": 0}


# =========================================================
# CONFIG HELPERS
# =========================================================

def cfg_float(*names, default=0.0):
    for name in names:
        if hasattr(config, name):
            try:
                return float(getattr(config, name))
            except (TypeError, ValueError):
                pass
    return float(default)


TON_BUY_MARKUP = cfg_float("TON_BUY_MARKUP", "TON_MARKUP", default=5)
USDT_BUY_MARKUP = cfg_float("USDT_BUY_MARKUP", "USDT_MARKUP", default=3)
TON_SELL_MARKUP = cfg_float("TON_SELL_MARKUP", "TON_SELL_MARKUP", "TON_BUY_MARKUP", "TON_MARKUP", default=3)
USDT_SELL_MARKUP = cfg_float("USDT_SELL_MARKUP", "USDT_SELL_MARKUP", "USDT_BUY_MARKUP", "USDT_MARKUP", default=3)
RATE_CACHE_SECONDS = int(cfg_float("RATE_CACHE_SECONDS", default=60))
MIN_TON = cfg_float("MIN_TON", default=1)
MIN_USDT = cfg_float("MIN_USDT", default=10)
PAYMENT_DETAILS_DEFAULT = str(getattr(
    config,
    "PAYMENT_DETAILS",
    """🏦 <b>РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ</b>

Банк: СБЕР
Номер карты: <code>2202 2092 2642 6665</code>
Получатель: Артем

⚠️ После оплаты обязательно отправьте чек."""
))


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                source_amount REAL NOT NULL,
                target TEXT NOT NULL,
                target_amount REAL NOT NULL,
                rate REAL,
                markup REAL,
                total_rub REAL,
                wallet TEXT,
                payout_details TEXT,
                deposit_wallet TEXT,
                status TEXT NOT NULL,
                receipt_file_id TEXT,
                receipt_type TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        defaults = {
            "ton_buy_markup": str(TON_BUY_MARKUP),
            "usdt_buy_markup": str(USDT_BUY_MARKUP),
            "ton_sell_markup": str(TON_SELL_MARKUP),
            "usdt_sell_markup": str(USDT_SELL_MARKUP),
            "payment_details": PAYMENT_DETAILS_DEFAULT,
            "ton_exchange_wallet": "",
            "usdt_exchange_wallet": "",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )

        # Если в старой базе реквизиты не были настроены,
        # автоматически ставим актуальные реквизиты из конфигурации.
        current_payment = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("payment_details",)
        ).fetchone()

        if not current_payment or not str(current_payment["value"]).strip():
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                ("payment_details", PAYMENT_DETAILS_DEFAULT),
            )

        conn.commit()


def get_setting(key, default=""):
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with closing(db()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        conn.commit()


def insert_order(order):
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO orders (
                id,user_id,username,kind,source,source_amount,target,target_amount,
                rate,markup,total_rub,wallet,payout_details,deposit_wallet,status,
                receipt_file_id,receipt_type,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order["id"], order["user_id"], order["username"], order["kind"],
                order["source"], order["source_amount"], order["target"], order["target_amount"],
                order.get("rate"), order.get("markup"), order.get("total_rub"),
                order.get("wallet"), order.get("payout_details"), order.get("deposit_wallet"),
                order["status"], order.get("receipt_file_id"), order.get("receipt_type"),
                order["created_at"],
            ),
        )
        conn.commit()


def get_order(order_id):
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def set_order_status(order_id, status):
    with closing(db()) as conn:
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        conn.commit()


def user_orders(user_id):
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
            (user_id,),
        ).fetchall()


def pending_orders():
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE status IN ('checking','approved') ORDER BY created_at DESC LIMIT 50"
        ).fetchall()


def stats():
    with closing(db()) as conn:
        return conn.execute(
            """
            SELECT
              COUNT(*) total,
              SUM(CASE WHEN status='checking' THEN 1 ELSE 0 END) checking,
              SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) approved,
              SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
              SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) rejected
            FROM orders
            """
        ).fetchone()


# =========================================================
# HELPERS
# =========================================================

def clear_state(user_id):
    states.pop(user_id, None)


def is_admin(user_id):
    return user_id == ADMIN_ID


def fmt(value):
    if value is None:
        return "0"
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.8f}".rstrip("0").rstrip(".")


def esc(value):
    return html.escape(str(value), quote=False)


def parse_amount(text, unit=None):
    if not text:
        return None
    text = text.strip().lower().replace(",", ".")
    text = re.sub(r"\s+", " ", text)

    if unit:
        aliases = {
            "TON": {"ton", "тон", "тонов"},
            "USDT": {"usdt"},
            "RUB": {"₽", "руб", "руб."},
        }
        pattern = r"^([0-9]+(?:\.[0-9]+)?)\s*(ton|тон|тонов|usdt|₽|руб|руб\.)?$"
    else:
        pattern = r"^([0-9]+(?:\.[0-9]+)?)$"

    m = re.fullmatch(pattern, text)
    if not m:
        return None

    suffix = m.group(2) if len(m.groups()) > 1 else None
    if unit and suffix:
        if suffix not in aliases[unit]:
            return None

    try:
        value = float(m.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def min_amount(currency):
    return MIN_TON if currency == "TON" else MIN_USDT


def markup(currency, side):
    key = f"{currency.lower()}_{side}_markup"
    default = TON_BUY_MARKUP if currency == "TON" else USDT_BUY_MARKUP
    if side == "sell":
        default = TON_SELL_MARKUP if currency == "TON" else USDT_SELL_MARKUP
    return float(get_setting(key, default))


def buy_rate(currency, market):
    return market * (1 + markup(currency, "buy") / 100)


def sell_rate(currency, market):
    return market * max(0, 1 - markup(currency, "sell") / 100)


def icon(currency):
    return "💎" if currency == "TON" else "💵" if currency == "USDT" else "💰"


def user_name(user):
    return f"@{user.username}" if user.username else str(user.id)


def status_text(status):
    return {
        "checking": "⏳ Проверка оплаты",
        "approved": "✅ Оплата подтверждена",
        "completed": "💸 Выполнено",
        "rejected": "❌ Отклонено",
    }.get(status, status)


# =========================================================
# RATES
# =========================================================

async def get_rates(force=False):
    now = time.time()
    if (
        not force
        and rates["TON"] is not None
        and rates["USDT"] is not None
        and now - rates["time"] < RATE_CACHE_SECONDS
    ):
        return rates["TON"], rates["USDT"]

    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=the-open-network,tether&vs_currencies=rub"
    )
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.json()

    rates["TON"] = float(data["the-open-network"]["rub"])
    rates["USDT"] = float(data["tether"]["rub"])
    rates["time"] = now
    return rates["TON"], rates["USDT"]


# =========================================================
# KEYBOARDS
# =========================================================

def home_kb(user_id):
    rows = [
        [InlineKeyboardButton(text="💎 Купить TON", callback_data="u_buy_TON")],
        [InlineKeyboardButton(text="💵 Купить USDT", callback_data="u_buy_USDT")],
        [InlineKeyboardButton(text="🔄 Размен", callback_data="u_exchange")],
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="u_orders")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="a_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_home_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="u_home")]]
    )


def buy_amount_kb(currency):
    values = ["1", "5", "10", "25"] if currency == "TON" else ["10", "25", "50", "100"]
    rows = []
    for i in range(0, len(values), 2):
        rows.append([
            InlineKeyboardButton(text=f"{values[i]} {currency}", callback_data=f"u_bq_{currency}_{values[i]}"),
            InlineKeyboardButton(text=f"{values[i+1]} {currency}", callback_data=f"u_bq_{currency}_{values[i+1]}")
        ])
    rows.append([InlineKeyboardButton(text="⌨️ Своё количество", callback_data=f"u_bc_{currency}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="u_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quote_kb(kind):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Продолжить", callback_data=f"u_continue_{kind}")],
            [InlineKeyboardButton(text="🔢 Изменить количество", callback_data=f"u_again_{kind}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="u_home")],
        ]
    )


def payment_kb(callback):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=callback)],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="u_home")],
        ]
    )


def admin_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Заявки", callback_data="a_orders")],
            [
                InlineKeyboardButton(text="💎 TON", callback_data="a_currency_TON"),
                InlineKeyboardButton(text="💵 USDT", callback_data="a_currency_USDT"),
            ],
            [InlineKeyboardButton(text="💳 Реквизиты RUB", callback_data="a_payment")],
            [InlineKeyboardButton(text="👛 Кошельки приёма", callback_data="a_wallets")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="a_stats")],
            [InlineKeyboardButton(text="🔄 Обновить курсы", callback_data="a_refresh")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="u_home")],
        ]
    )


def admin_back_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Админка", callback_data="a_home")]]
    )


def receipt_admin_kb(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"a_approve_{order_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"a_reject_{order_id}")],
        ]
    )


def complete_kb(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💸 Обмен выполнен", callback_data=f"a_complete_{order_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"a_reject_{order_id}")],
        ]
    )


def exchange_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 TON → ₽", callback_data="u_ex_TON_RUB")],
            [InlineKeyboardButton(text="₽ → TON 💎", callback_data="u_ex_RUB_TON")],
            [InlineKeyboardButton(text="💵 USDT → ₽", callback_data="u_ex_USDT_RUB")],
            [InlineKeyboardButton(text="₽ → USDT 💵", callback_data="u_ex_RUB_USDT")],
            [InlineKeyboardButton(text="💎 TON → USDT 💵", callback_data="u_ex_TON_USDT")],
            [InlineKeyboardButton(text="💵 USDT → TON 💎", callback_data="u_ex_USDT_TON")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="u_home")],
        ]
    )


def exchange_amount_kb(source):
    if source == "RUB":
        vals, suffix = ["500", "1000", "5000", "10000"], "₽"
    elif source == "TON":
        vals, suffix = ["1", "5", "10", "25"], "TON"
    else:
        vals, suffix = ["10", "25", "50", "100"], "USDT"
    rows = []
    for i in range(0, 4, 2):
        rows.append([
            InlineKeyboardButton(text=f"{vals[i]} {suffix}", callback_data=f"u_eq_{source}_{vals[i]}"),
            InlineKeyboardButton(text=f"{vals[i+1]} {suffix}", callback_data=f"u_eq_{source}_{vals[i+1]}")
        ])
    rows.append([InlineKeyboardButton(text="⌨️ Своё количество", callback_data=f"u_ec_{source}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="u_exchange")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def exchange_quote_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Продолжить", callback_data="u_ex_continue")],
            [InlineKeyboardButton(text="🔢 Изменить количество", callback_data="u_ex_again")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="u_home")],
        ]
    )


# =========================================================
# HOME
# =========================================================

@dp.message(CommandStart())
async def start(message: Message):
    clear_state(message.from_user.id)
    await message.answer(
        "💱 <b>ОБМЕННИК</b>\n\nВыберите действие:",
        reply_markup=home_kb(message.from_user.id),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "u_home")
async def user_home(callback: CallbackQuery):
    clear_state(callback.from_user.id)
    await callback.message.edit_text(
        "💱 <b>ОБМЕННИК</b>\n\nВыберите действие:",
        reply_markup=home_kb(callback.from_user.id),
        parse_mode="HTML",
    )
    await callback.answer()


# =========================================================
# BUY
# =========================================================

@dp.callback_query(F.data.in_({"u_buy_TON", "u_buy_USDT"}))
async def buy_start(callback: CallbackQuery):
    currency = callback.data.rsplit("_", 1)[1]
    states[callback.from_user.id] = {"step": "buy_amount", "currency": currency}
    await callback.message.edit_text(
        f"{icon(currency)} <b>Купить {currency}</b>\n\n"
        f"Введите количество {currency} или выберите кнопку.\n"
        f"Минимум: <b>{fmt(min_amount(currency))} {currency}</b>",
        reply_markup=buy_amount_kb(currency),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("u_bc_"))
async def buy_custom(callback: CallbackQuery):
    currency = callback.data.rsplit("_", 1)[1]
    states[callback.from_user.id] = {"step": "buy_amount", "currency": currency}
    await callback.message.edit_text(
        f"{icon(currency)} <b>Количество {currency}</b>\n\n"
        f"Напишите, например: <code>10</code> или <code>10 {currency}</code>.",
        reply_markup=back_home_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("u_bq_"))
async def buy_quick(callback: CallbackQuery):
    _, _, currency, value = callback.data.split("_")
    await make_buy_quote(callback.from_user.id, currency, float(value), callback.message, edit=True)
    await callback.answer()


async def make_buy_quote(user_id, currency, amount, message, edit=False):
    if amount < min_amount(currency):
        text = f"❌ Минимум: <b>{fmt(min_amount(currency))} {currency}</b>"
        if edit:
            await message.edit_text(text, reply_markup=buy_amount_kb(currency), parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")
        return
    try:
        ton, usdt = await get_rates()
    except Exception:
        if edit:
            await message.edit_text("❌ Не удалось получить курс.", reply_markup=back_home_kb())
        else:
            await message.answer("❌ Не удалось получить курс.")
        return
    market = ton if currency == "TON" else usdt
    rate = buy_rate(currency, market)
    total = amount * rate
    states[user_id] = {
        "step": "buy_quote", "currency": currency, "amount": amount,
        "market": market, "rate": rate, "markup": markup(currency, "buy"), "total_rub": total,
    }
    text = (
        f"{icon(currency)} <b>Покупка {currency}</b>\n\n"
        f"📦 Количество: <b>{fmt(amount)} {currency}</b>\n"
        f"📈 Рыночный курс: <b>{market:.2f} ₽</b>\n"
        f"📊 Наценка: <b>+{markup(currency,'buy'):.2f}%</b>\n"
        f"💱 Ваш курс: <b>{rate:.2f} ₽</b>\n\n"
        f"💰 <b>К оплате: {total:.2f} ₽</b>\n\n"
        f"Курс зафиксирован для заявки."
    )
    if edit:
        await message.edit_text(text, reply_markup=quote_kb("buy"), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=quote_kb("buy"), parse_mode="HTML")


@dp.callback_query(F.data == "u_continue_buy")
async def buy_continue(callback: CallbackQuery):
    state = states.get(callback.from_user.id)
    if not state or state.get("step") != "buy_quote":
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    state["step"] = "buy_wallet"
    currency = state["currency"]
    await callback.message.edit_text(
        f"📥 <b>Кошелёк для получения {currency}</b>\n\nОтправьте адрес кошелька текстом.",
        reply_markup=back_home_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "u_again_buy")
async def buy_again(callback: CallbackQuery):
    currency = states.get(callback.from_user.id, {}).get("currency", "TON")
    states[callback.from_user.id] = {"step": "buy_amount", "currency": currency}
    await callback.message.edit_text(
        f"{icon(currency)} <b>Купить {currency}</b>\n\nВведите количество:",
        reply_markup=buy_amount_kb(currency),
        parse_mode="HTML",
    )
    await callback.answer()


# =========================================================
# EXCHANGE MENU
# =========================================================

@dp.callback_query(F.data == "u_exchange")
async def exchange_menu(callback: CallbackQuery):
    clear_state(callback.from_user.id)
    await callback.message.edit_text(
        "🔄 <b>РАЗМЕН</b>\n\nВыберите направление:",
        reply_markup=exchange_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("u_ex_") & ~F.data.in_({"u_ex_continue", "u_ex_again"}))
async def exchange_start(callback: CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) != 4:
        await callback.answer("Ошибка направления", show_alert=True)
        return
    source, target = parts[2], parts[3]
    states[callback.from_user.id] = {"step": "exchange_amount", "source": source, "target": target}
    unit = "₽" if source == "RUB" else source
    await callback.message.edit_text(
        f"🔄 <b>{source} → {target}</b>\n\n"
        f"Сколько <b>{unit}</b> хотите обменять?\n\n"
        "Выберите кнопку или введите своё количество.",
        reply_markup=exchange_amount_kb(source),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("u_ec_"))
async def exchange_custom(callback: CallbackQuery):
    source = callback.data.rsplit("_", 1)[1]
    current = states.get(callback.from_user.id, {})
    target = current.get("target")
    if not target:
        await callback.answer("Сначала выберите направление", show_alert=True)
        return
    states[callback.from_user.id] = {"step": "exchange_amount", "source": source, "target": target}
    unit = "₽" if source == "RUB" else source
    await callback.message.edit_text(
        f"🔄 <b>{source} → {target}</b>\n\n"
        f"Напишите количество <b>{unit}</b>.\nНапример: <code>10</code>",
        reply_markup=back_home_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("u_eq_"))
async def exchange_quick(callback: CallbackQuery):
    _, _, source, value = callback.data.split("_")
    state = states.get(callback.from_user.id)
    if not state:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    await make_exchange_quote(callback.from_user.id, source, state["target"], float(value), callback.message)
    await callback.answer()


async def make_exchange_quote(user_id, source, target, amount, message):
    if amount <= 0:
        await message.answer("❌ Количество должно быть больше 0.")
        return
    if source == "RUB" and amount < 100:
        await message.answer("❌ Минимум: 100 ₽")
        return
    if source in {"TON", "USDT"} and amount < min_amount(source):
        await message.answer(f"❌ Минимум: {fmt(min_amount(source))} {source}")
        return

    try:
        ton, usdt = await get_rates()
    except Exception:
        await message.answer("❌ Не удалось получить курс.")
        return

    market = {"TON": ton, "USDT": usdt, "RUB": 1.0}

    if source == "RUB":
        rate_target = buy_rate(target, market[target])
        target_amount = amount / rate_target
        rate_text = f"1 {target} = {rate_target:.2f} ₽"
        rate_value = rate_target
        rate_markup = markup(target, "buy")
    elif target == "RUB":
        rate_source = sell_rate(source, market[source])
        target_amount = amount * rate_source
        rate_text = f"1 {source} = {rate_source:.2f} ₽"
        rate_value = rate_source
        rate_markup = markup(source, "sell")
    else:
        source_rate = sell_rate(source, market[source])
        target_rate = buy_rate(target, market[target])
        target_amount = amount * source_rate / target_rate
        rate_text = f"1 {source} ≈ {source_rate / target_rate:.8f} {target}"
        rate_value = source_rate / target_rate
        rate_markup = None

    states[user_id] = {
        "step": "exchange_quote", "source": source, "target": target,
        "amount": amount, "target_amount": target_amount,
        "market_source": market[source], "market_target": market[target],
        "rate": rate_value, "markup": rate_markup,
    }

    await message.answer(
        f"🔄 <b>{source} → {target}</b>\n\n"
        f"📤 Отдаёте: <b>{fmt(amount)} {'₽' if source == 'RUB' else source}</b>\n"
        f"📥 Получаете: <b>{fmt(target_amount)} {'₽' if target == 'RUB' else target}</b>\n\n"
        f"💱 {rate_text}\n"
        f"Курс зафиксирован для заявки.",
        reply_markup=exchange_quote_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "u_ex_again")
async def exchange_again(callback: CallbackQuery):
    state = states.get(callback.from_user.id)
    if not state:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    source, target = state["source"], state["target"]
    states[callback.from_user.id] = {"step": "exchange_amount", "source": source, "target": target}
    await callback.message.edit_text(
        f"🔄 <b>{source} → {target}</b>\n\nВведите количество:",
        reply_markup=exchange_amount_kb(source),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "u_ex_continue")
async def exchange_continue(callback: CallbackQuery):
    state = states.get(callback.from_user.id)
    if not state or state.get("step") != "exchange_quote":
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    source, target = state["source"], state["target"]

    if target == "RUB":
        state["step"] = "exchange_payout"
        await callback.message.edit_text(
            "💳 <b>Реквизиты для получения ₽</b>\n\nОтправьте номер карты/СБП и имя получателя.",
            reply_markup=back_home_kb(),
            parse_mode="HTML",
        )
    else:
        state["step"] = "exchange_target_wallet"
        await callback.message.edit_text(
            f"📥 <b>Кошелёк для получения {target}</b>\n\nОтправьте адрес кошелька текстом.",
            reply_markup=back_home_kb(),
            parse_mode="HTML",
        )
    await callback.answer()


# =========================================================
# PAYMENT BUTTONS
# =========================================================

@dp.callback_query(F.data == "u_buy_paid")
async def buy_paid(callback: CallbackQuery):
    state = states.get(callback.from_user.id)
    if not state or state.get("step") != "buy_payment":
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    state["step"] = "buy_receipt"
    await callback.message.edit_text(
        "📎 <b>Отправьте чек</b>\n\nФото или документ с подтверждением оплаты.",
        reply_markup=back_home_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "u_exchange_paid")
async def exchange_paid(callback: CallbackQuery):
    state = states.get(callback.from_user.id)
    if not state or state.get("step") != "exchange_payment":
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    state["step"] = "exchange_receipt"
    await callback.message.edit_text(
        "📎 <b>Отправьте подтверждение перевода</b>\n\nФото или документ.",
        reply_markup=back_home_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


# =========================================================
# RECEIPT HANDLERS
# =========================================================

@dp.message(F.photo)
async def receipt_photo(message: Message):
    state = states.get(message.from_user.id)
    if not state or state.get("step") not in {"buy_receipt", "exchange_receipt"}:
        await message.answer("Сейчас чек не ожидается.", reply_markup=home_kb(message.from_user.id))
        return
    await finish_receipt(message, message.photo[-1].file_id, "photo")


@dp.message(F.document)
async def receipt_document(message: Message):
    state = states.get(message.from_user.id)
    if not state or state.get("step") not in {"buy_receipt", "exchange_receipt"}:
        await message.answer("Сейчас документ не ожидается.", reply_markup=home_kb(message.from_user.id))
        return
    await finish_receipt(message, message.document.file_id, "document")


async def finish_receipt(message: Message, file_id, file_type):
    user_id = message.from_user.id
    state = states.get(user_id)
    if not state:
        return

    is_buy = state.get("step") == "buy_receipt"
    kind = "buy" if is_buy else "exchange"

    order_id = uuid.uuid4().hex[:8].upper()
    source = state["currency"] if is_buy else state["source"]
    target = state["currency"] if is_buy else state["target"]
    source_amount = state["amount"]
    target_amount = state["amount"] if is_buy else state["target_amount"]

    order = {
        "id": order_id,
        "user_id": user_id,
        "username": user_name(message.from_user),
        "kind": kind,
        "source": source,
        "source_amount": source_amount,
        "target": target,
        "target_amount": target_amount,
        "rate": state.get("rate"),
        "markup": state.get("markup"),
        "total_rub": state.get("total_rub"),
        "wallet": state.get("wallet"),
        "payout_details": state.get("payout_details"),
        "deposit_wallet": state.get("deposit_wallet"),
        "status": "checking",
        "receipt_file_id": file_id,
        "receipt_type": file_type,
        "created_at": int(time.time()),
    }

    try:
        insert_order(order)
        await send_admin_order(order_id)
    except Exception as exc:
        try:
            with closing(db()) as conn:
                conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
                conn.commit()
        except Exception:
            pass
        await message.answer(
            "❌ Не удалось отправить чек администратору.\n"
            "Заявка не создана. Попробуйте отправить чек ещё раз."
        )
        print("SEND ORDER ERROR:", repr(exc))
        return

    clear_state(user_id)
    await message.answer(
        f"✅ <b>Заявка #{order_id} создана.</b>\n\n"
        "Чек успешно отправлен администратору.\n"
        "Статус: <b>Проверка оплаты</b>.",
        reply_markup=home_kb(user_id),
        parse_mode="HTML",
    )


async def send_admin_order(order_id):
    order = get_order(order_id)
    if not order:
        raise RuntimeError("Order not found")

    if order["kind"] == "buy":
        details = (
            f"🆕 <b>ПОКУПКА #{esc(order['id'])}</b>\n\n"
            f"👤 {esc(order['username'])}\n"
            f"🆔 <code>{order['user_id']}</code>\n\n"
            f"{icon(order['source'])} Купить: <b>{fmt(order['source_amount'])} {esc(order['source'])}</b>\n"
            f"💰 Оплата: <b>{float(order['total_rub']):.2f} ₽</b>\n"
            f"📈 Рыночный/зафиксированный курс: <b>{float(order['rate']):.2f} ₽</b>\n"
            f"📊 Наценка: <b>+{float(order['markup'] or 0):.2f}%</b>\n"
            f"📍 Кошелёк клиента:\n<code>{esc(order['wallet'])}</code>"
        )
    else:
        details = (
            f"🆕 <b>РАЗМЕН #{esc(order['id'])}</b>\n\n"
            f"👤 {esc(order['username'])}\n"
            f"🆔 <code>{order['user_id']}</code>\n\n"
            f"📤 Отдаёт: <b>{fmt(order['source_amount'])} {esc(order['source'])}</b>\n"
            f"📥 Получает: <b>{fmt(order['target_amount'])} {esc(order['target'])}</b>\n"
        )
        if order["wallet"]:
            details += f"📍 Кошелёк: <code>{esc(order['wallet'])}</code>\n"
        if order["payout_details"]:
            details += f"💳 Реквизиты RUB: <code>{esc(order['payout_details'])}</code>\n"
        if order["deposit_wallet"]:
            details += f"👛 Кошелёк приёма: <code>{esc(order['deposit_wallet'])}</code>\n"

    # Сначала отправляем сам чек.
    if order["receipt_type"] == "photo":
        await bot.send_photo(ADMIN_ID, order["receipt_file_id"])
    else:
        await bot.send_document(ADMIN_ID, order["receipt_file_id"])

    # Потом отдельное сообщение с текстом + кнопками. Никакого длинного caption.
    await bot.send_message(
        ADMIN_ID,
        details,
        reply_markup=receipt_admin_kb(order_id),
        parse_mode="HTML",
    )


# =========================================================
# TEXT ROUTER — ЕДИНСТВЕННЫЙ ОБРАБОТЧИК ТЕКСТА
# =========================================================

@dp.message(F.text)
async def text_router(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    if text == "/admin":
        await admin_command(message)
        return

    state = states.get(user_id)
    if not state:
        await message.answer("Используйте кнопки меню.", reply_markup=home_kb(user_id))
        return

    step = state.get("step")

    if step == "buy_amount":
        currency = state["currency"]
        amount = parse_amount(text, currency)
        if amount is None:
            await message.answer(
                f"❌ Введите только количество {currency}.\n\n"
                f"Пример: <code>10</code> или <code>10 {currency}</code>",
                parse_mode="HTML",
            )
            return
        await make_buy_quote(user_id, currency, amount, message)
        return

    if step == "buy_wallet":
        if len(text) < 10:
            await message.answer("❌ Адрес кошелька слишком короткий.")
            return
        state["wallet"] = text
        state["step"] = "buy_payment"
        await message.answer(
            f"🧾 <b>ЗАЯВКА</b>\n\n"
            f"{icon(state['currency'])} {fmt(state['amount'])} {state['currency']}\n"
            f"💰 К оплате: <b>{state['total_rub']:.2f} ₽</b>\n\n"
            f"📍 Кошелёк:\n<code>{esc(text)}</code>\n\n"
            f"{get_setting('payment_details', PAYMENT_DETAILS_DEFAULT)}\n\n"
            "После оплаты нажмите «Я оплатил».",
            reply_markup=payment_kb("u_buy_paid"),
            parse_mode="HTML",
        )
        return

    if step == "exchange_amount":
        source = state["source"]
        amount = parse_amount(text, source)
        if amount is None:
            await message.answer(
                f"❌ Введите количество {('₽' if source == 'RUB' else source)}.\n\n"
                "Пример: <code>1000</code>",
                parse_mode="HTML",
            )
            return
        await make_exchange_quote(user_id, source, state["target"], amount, message)
        return

    if step == "exchange_target_wallet":
        if len(text) < 10:
            await message.answer("❌ Адрес кошелька слишком короткий.")
            return
        state["wallet"] = text
        source, target = state["source"], state["target"]

        if source == "RUB":
            state["step"] = "exchange_payment"
            await message.answer(
                f"🧾 <b>{source} → {target}</b>\n\n"
                f"📤 Отдаёте: <b>{fmt(state['amount'])} ₽</b>\n"
                f"📥 Получите: <b>{fmt(state['target_amount'])} {target}</b>\n\n"
                f"📍 Кошелёк получения:\n<code>{esc(text)}</code>\n\n"
                f"{get_setting('payment_details', PAYMENT_DETAILS_DEFAULT)}\n\n"
                "После оплаты нажмите «Я оплатил».",
                reply_markup=payment_kb("u_exchange_paid"),
                parse_mode="HTML",
            )
            return

        deposit = get_setting(f"{source.lower()}_exchange_wallet")
        if not deposit:
            clear_state(user_id)
            await message.answer(
                f"❌ Администратор не настроил кошелёк для приёма {source}.",
                reply_markup=home_kb(user_id),
            )
            return

        state["deposit_wallet"] = deposit
        state["step"] = "exchange_payment"
        await message.answer(
            f"🔄 <b>{source} → {target}</b>\n\n"
            f"📤 Отправляете: <b>{fmt(state['amount'])} {source}</b>\n"
            f"📥 Получите: <b>{fmt(state['target_amount'])} {target}</b>\n\n"
            f"👛 Кошелёк для отправки {source}:\n<code>{esc(deposit)}</code>\n\n"
            f"📍 Ваш кошелёк {target}:\n<code>{esc(text)}</code>\n\n"
            "После перевода нажмите «Я отправил».",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Я отправил", callback_data="u_exchange_paid")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="u_home")],
                ]
            ),
            parse_mode="HTML",
        )
        return

    if step == "exchange_payout":
        state["payout_details"] = text
        source = state["source"]
        deposit = get_setting(f"{source.lower()}_exchange_wallet")
        if not deposit:
            clear_state(user_id)
            await message.answer("❌ Администратор не настроил кошелёк приёма.", reply_markup=home_kb(user_id))
            return
        state["deposit_wallet"] = deposit
        state["step"] = "exchange_payment"
        await message.answer(
            f"🔄 <b>{source} → ₽</b>\n\n"
            f"📤 Отправляете: <b>{fmt(state['amount'])} {source}</b>\n"
            f"📥 Получите: <b>{fmt(state['target_amount'])} ₽</b>\n\n"
            f"👛 Кошелёк для отправки {source}:\n<code>{esc(deposit)}</code>\n\n"
            f"💳 Ваши реквизиты:\n<code>{esc(text)}</code>\n\n"
            "После перевода нажмите «Я отправил».",
            reply_markup=payment_kb("u_exchange_paid"),
            parse_mode="HTML",
        )
        return

    # Админские текстовые состояния.
    if is_admin(user_id) and step == "admin_payment":
        set_setting("payment_details", text)
        clear_state(user_id)
        await message.answer("✅ Реквизиты RUB сохранены.", reply_markup=admin_kb())
        return

    if is_admin(user_id) and step == "admin_ton_wallet":
        set_setting("ton_exchange_wallet", text)
        clear_state(user_id)
        await message.answer("✅ Кошелёк TON сохранён.", reply_markup=admin_kb())
        return

    if is_admin(user_id) and step == "admin_usdt_wallet":
        set_setting("usdt_exchange_wallet", text)
        clear_state(user_id)
        await message.answer("✅ Кошелёк USDT сохранён.", reply_markup=admin_kb())
        return

    if is_admin(user_id) and step.startswith("admin_markup_"):
        _, currency, side = step.split("_")
        try:
            value = float(text.replace(",", ".").replace("%", "").strip())
        except ValueError:
            value = -1
        if not 0 <= value <= 100:
            await message.answer("❌ Введите процент от 0 до 100.")
            return
        set_setting(f"{currency.lower()}_{side}_markup", value)
        clear_state(user_id)
        await message.answer("✅ Наценка сохранена.", reply_markup=admin_kb())
        return

    await message.answer("Используйте кнопки меню.", reply_markup=home_kb(user_id))


# =========================================================
# ADMIN COMMAND
# =========================================================

@dp.message(F.text == "/admin")
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён.")
        return
    clear_state(message.from_user.id)
    await message.answer("⚙️ <b>АДМИН-ПАНЕЛЬ</b>", reply_markup=admin_kb(), parse_mode="HTML")


# =========================================================
# ADMIN HOME / ORDERS
# =========================================================

@dp.callback_query(F.data == "a_home")
async def admin_home(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    clear_state(callback.from_user.id)
    await callback.message.edit_text("⚙️ <b>АДМИН-ПАНЕЛЬ</b>", reply_markup=admin_kb(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "a_orders")
async def admin_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    rows = pending_orders()
    text = "📋 <b>ЗАЯВКИ</b>\n\n"
    if not rows:
        text += "Новых заявок нет."
    else:
        for row in rows:
            text += (
                f"#{row['id']} — {fmt(row['source_amount'])} {row['source']} → "
                f"{fmt(row['target_amount'])} {row['target']}\n"
                f"{status_text(row['status'])}\n\n"
            )
    await callback.message.edit_text(text, reply_markup=admin_back_kb(), parse_mode="HTML")
    await callback.answer()


# =========================================================
# ADMIN CURRENCY / MARKUP
# =========================================================

@dp.callback_query(F.data.startswith("a_currency_"))
async def admin_currency(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    currency = callback.data.rsplit("_", 1)[1]
    try:
        ton, usdt = await get_rates()
        market = ton if currency == "TON" else usdt
        buy = buy_rate(currency, market)
        sell = sell_rate(currency, market)
        text = (
            f"{icon(currency)} <b>{currency}</b>\n\n"
            f"📈 Рынок: <b>{market:.2f} ₽</b>\n"
            f"🟢 Покупка: +{markup(currency,'buy'):.2f}% → <b>{buy:.2f} ₽</b>\n"
            f"🔴 Выкуп: -{markup(currency,'sell'):.2f}% → <b>{sell:.2f} ₽</b>"
        )
    except Exception:
        text = f"{icon(currency)} <b>{currency}</b>\n\n❌ Не удалось получить курс."

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Наценка покупки", callback_data=f"a_markup_{currency}_buy")],
            [InlineKeyboardButton(text="🔴 Наценка выкупа", callback_data=f"a_markup_{currency}_sell")],
            [InlineKeyboardButton(text="⬅️ Админка", callback_data="a_home")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("a_markup_"))
async def admin_markup(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, _, currency, side = callback.data.split("_")
    states[callback.from_user.id] = {"step": f"admin_markup_{currency}_{side}"}
    side_text = "покупки" if side == "buy" else "выкупа"
    await callback.message.edit_text(
        f"✏️ <b>Наценка {currency} — {side_text}</b>\n\nВведите процент от 0 до 100.",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


# =========================================================
# ADMIN PAYMENT / WALLETS / STATS / REFRESH
# =========================================================

@dp.callback_query(F.data == "a_payment")
async def admin_payment(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        f"💳 <b>РЕКВИЗИТЫ RUB</b>\n\n{get_setting('payment_details', PAYMENT_DETAILS_DEFAULT)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="a_set_payment")],
            [InlineKeyboardButton(text="⬅️ Админка", callback_data="a_home")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "a_set_payment")
async def admin_set_payment(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    states[callback.from_user.id] = {"step": "admin_payment"}
    await callback.message.edit_text("💳 Отправьте новые реквизиты одним сообщением.", reply_markup=admin_back_kb())
    await callback.answer()


@dp.callback_query(F.data == "a_wallets")
async def admin_wallets(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    ton = get_setting("ton_exchange_wallet") or "не установлен"
    usdt = get_setting("usdt_exchange_wallet") or "не установлен"
    await callback.message.edit_text(
        "👛 <b>КОШЕЛЬКИ ПРИЁМА</b>\n\n"
        f"💎 TON:\n<code>{esc(ton)}</code>\n\n"
        f"💵 USDT:\n<code>{esc(usdt)}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Изменить TON", callback_data="a_set_wallet_TON")],
            [InlineKeyboardButton(text="💵 Изменить USDT", callback_data="a_set_wallet_USDT")],
            [InlineKeyboardButton(text="⬅️ Админка", callback_data="a_home")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("a_set_wallet_"))
async def admin_set_wallet(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    currency = callback.data.rsplit("_", 1)[1]
    states[callback.from_user.id] = {"step": f"admin_{currency.lower()}_wallet"}
    await callback.message.edit_text(f"👛 Отправьте адрес кошелька приёма {currency}.", reply_markup=admin_back_kb())
    await callback.answer()


@dp.callback_query(F.data == "a_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    row = stats()
    await callback.message.edit_text(
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"Всего: <b>{row['total'] or 0}</b>\n"
        f"⏳ Проверка: <b>{row['checking'] or 0}</b>\n"
        f"✅ Подтверждено: <b>{row['approved'] or 0}</b>\n"
        f"💸 Выполнено: <b>{row['completed'] or 0}</b>\n"
        f"❌ Отклонено: <b>{row['rejected'] or 0}</b>",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "a_refresh")
async def admin_refresh(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    try:
        ton, usdt = await get_rates(force=True)
        await callback.answer(f"TON {ton:.2f} ₽ • USDT {usdt:.2f} ₽", show_alert=True)
    except Exception:
        await callback.answer("❌ Ошибка курса", show_alert=True)


# =========================================================
# ADMIN ACTIONS
# =========================================================

@dp.callback_query(F.data.startswith("a_approve_"))
async def admin_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = callback.data.replace("a_approve_", "", 1)
    order = get_order(order_id)
    if not order or order["status"] != "checking":
        await callback.answer("Заявка уже обработана или не найдена", show_alert=True)
        return
    set_order_status(order_id, "approved")
    await bot.send_message(
        order["user_id"],
        f"✅ <b>Оплата по заявке #{esc(order_id)} подтверждена.</b>\n\n"
        "Администратор выполняет обмен.",
        parse_mode="HTML",
    )
    await callback.message.edit_reply_markup(reply_markup=complete_kb(order_id))
    await callback.answer("Оплата подтверждена")


@dp.callback_query(F.data.startswith("a_complete_"))
async def admin_complete(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = callback.data.replace("a_complete_", "", 1)
    order = get_order(order_id)
    if not order or order["status"] != "approved":
        await callback.answer("Сначала подтвердите оплату", show_alert=True)
        return
    set_order_status(order_id, "completed")
    await bot.send_message(
        order["user_id"],
        f"💸 <b>Заявка #{esc(order_id)} выполнена.</b>\n\nОбмен завершён.",
        parse_mode="HTML",
    )
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Готово")


@dp.callback_query(F.data.startswith("a_reject_"))
async def admin_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = callback.data.replace("a_reject_", "", 1)
    order = get_order(order_id)
    if not order or order["status"] not in {"checking", "approved"}:
        await callback.answer("Заявка уже обработана", show_alert=True)
        return
    set_order_status(order_id, "rejected")
    await bot.send_message(
        order["user_id"],
        f"❌ <b>Заявка #{esc(order_id)} отклонена.</b>",
        parse_mode="HTML",
    )
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отклонено")


# =========================================================
# MY ORDERS
# =========================================================

@dp.callback_query(F.data == "u_orders")
async def my_orders(callback: CallbackQuery):
    rows = user_orders(callback.from_user.id)
    if not rows:
        await callback.message.edit_text("📋 <b>МОИ ЗАЯВКИ</b>\n\nЗаявок пока нет.", reply_markup=back_home_kb(), parse_mode="HTML")
        await callback.answer()
        return
    text = "📋 <b>МОИ ЗАЯВКИ</b>\n\n"
    for row in rows:
        text += (
            f"<b>#{esc(row['id'])}</b> — "
            f"{fmt(row['source_amount'])} {esc(row['source'])} → "
            f"{fmt(row['target_amount'])} {esc(row['target'])}\n"
            f"{status_text(row['status'])}\n\n"
        )
    await callback.message.edit_text(text, reply_markup=back_home_kb(), parse_mode="HTML")
    await callback.answer()


# =========================================================
# START
# =========================================================

init_db()


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    print("Exchange bot started")
    await dp.start_polling(bot)


asyncio.run(main())
