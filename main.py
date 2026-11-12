import asyncio
import json
import os
import uuid

import psycopg2  # type: ignore
import uvicorn
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv  # type: ignore
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from psycopg2.extras import RealDictCursor  # type: ignore
from pydantic import BaseModel

load_dotenv()

# --- НАСТРОЙКИ (берутся из .env локально или из настроек Render) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не задан BOT_TOKEN! Проверь файл .env или Environment Variables.")

RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBAPP_URL = os.getenv("WEBAPP_URL", RENDER_URL)
DATABASE_URL = os.getenv("DATABASE_URL", "")

raw_admins = os.getenv("ADMIN_CHAT_IDS", "")
ADMIN_CHAT_IDS = [int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()]

MAX_SEATS = 8
BOOKINGS_FILE = "bookings.json"

BG_CITIES = {
    "Бургас 🇧🇬", "Сарафово 🇧🇬", "Поморие 🇧🇬", "Равда 🇧🇬",
    "Несебр 🇧🇬", "Солнечный Берег 🇧🇬", "Святой Влас 🇧🇬", "Обзор 🇧🇬",
    "Бяла 🇧🇬", "Варна 🇧🇬", "Золотые Пески 🇧🇬", "Кранево 🇧🇬",
}
UA_CITIES = {
    "Одесса 🇺🇦", "Татарбунары 🇺🇦", "Измаил 🇺🇦", "Исакча 🇷🇴",
}

app = FastAPI()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- РАБОТА С БАЗОЙ ДАННЫХ (PostgreSQL в облаке / JSON локально) ---
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    if not DATABASE_URL:
        return
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    trip_key TEXT NOT NULL,
                    route TEXT NOT NULL,
                    date TEXT NOT NULL,
                    seats INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    details TEXT NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS closed_trips (
                    trip_key TEXT PRIMARY KEY
                );
            """)
        conn.commit()


def load_local_json() -> dict:
    if os.path.exists(BOOKINGS_FILE):
        try:
            with open(BOOKINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "orders" in data:
                data.setdefault("closed_trips", [])
                return data
        except Exception:  # noqa: BLE001, S110
            pass
    return {"orders": [], "closed_trips": []}


def save_local_json(data: dict):
    with open(BOOKINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_data() -> dict:
    if DATABASE_URL:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM orders")
            orders = [dict(row) for row in cur.fetchall()]
            cur.execute("SELECT trip_key FROM closed_trips")
            closed = [row["trip_key"] for row in cur.fetchall()]
        return {"orders": orders, "closed_trips": closed}
    return load_local_json()


def db_add_order(order: dict):
    if DATABASE_URL:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO orders (id, trip_key, route, date, seats, name, phone, details)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        order["id"], order["trip_key"], order["route"], order["date"],
                        order["seats"], order["name"], order["phone"], order["details"],
                    ),
                )
            conn.commit()
    else:
        data = load_local_json()
        data["orders"].append(order)
        save_local_json(data)


def db_delete_order(order_id: str):
    if DATABASE_URL:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM orders WHERE id = %s", (order_id,))
            conn.commit()
    else:
        data = load_local_json()
        data["orders"] = [o for o in data["orders"] if o["id"] != order_id]
        save_local_json(data)


def db_set_trip_closed(trip_key: str, closed: bool):
    if DATABASE_URL:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                if closed:
                    cur.execute(
                        "INSERT INTO closed_trips (trip_key) VALUES (%s) ON CONFLICT DO NOTHING",
                        (trip_key,),
                    )
                else:
                    cur.execute("DELETE FROM closed_trips WHERE trip_key = %s", (trip_key,))
            conn.commit()
    else:
        data = load_local_json()
        if closed and trip_key not in data["closed_trips"]:
            data["closed_trips"].append(trip_key)
        elif not closed:
            data["closed_trips"] = [k for k in data["closed_trips"] if k != trip_key]
        save_local_json(data)


def db_reset_all():
    if DATABASE_URL:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM orders")
                cur.execute("DELETE FROM closed_trips")
            conn.commit()
    else:
        save_local_json({"orders": [], "closed_trips": []})


# --- БИЗНЕС-ЛОГИКА ---
def get_trip_key(from_city: str, to_city: str, date: str) -> str:
    if from_city in BG_CITIES and to_city in UA_CITIES:
        direction = "BG_UA"
    elif from_city in UA_CITIES and to_city in BG_CITIES:
        direction = "UA_BG"
    else:
        direction = f"{from_city}_{to_city}"
    return f"{direction}|{date}"


def get_remaining_seats(data: dict, trip_key: str) -> int:
    if trip_key in data.get("closed_trips", []):
        return 0
    booked = sum(
        o.get("seats", 1)
        for o in data.get("orders", [])
        if o.get("trip_key") == trip_key
    )
    return max(0, MAX_SEATS - booked)


def build_manifest_view(data: dict):
    orders = sorted(data.get("orders", []), key=lambda x: x.get("date", ""))
    if not orders:
        return "📋 <b>Список пассажиров пуст.</b>\nАктивных броней пока нет.", None

    lines = ["📋 <b>АКТИВНЫЕ БРОНИ ПАССАЖИРОВ:</b>\n"]
    buttons = []

    for idx, o in enumerate(orders, start=1):
        lines.append(
            f"{idx}. <b>{o['date']}</b> | {o['route']}\n"
            f"   👤 {o['name']} ({o['seats']} мест.) — <code>{o['phone']}</code>\n"
            f"   📍 {o['details']}"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"❌ Вычеркнуть: {o['name']} ({o['date']} • {o['seats']} м.)",
                    callback_data=f"del_list|{o['id']}",
                )
            ]
        )

    lines.append("\n<i>Нажмите на кнопку ниже, чтобы вычеркнуть пассажира и вернуть места:</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return "\n".join(lines), kb


class OrderData(BaseModel):
    order_type: str
    from_city: str
    to_city: str
    date: str
    seats: int = 1
    details: str
    name: str
    phone: str
    tg_username: str = "Не указан"


# --- ВЕБ-МАРШРУТЫ ---
@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/")
async def get_index():
    return FileResponse("index.html")


@app.get("/style.css")
async def get_css():
    return FileResponse("style.css")


@app.get("/banner.jpg")
async def get_banner():
    if os.path.exists("banner.jpg"):
        return FileResponse("banner.jpg")
    return Response(status_code=404)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    await dp.feed_raw_update(bot, update)
    return {"ok": True}


@app.get("/api/seats")
async def check_seats(from_city: str, to_city: str, date: str):
    data = load_data()
    key = get_trip_key(from_city, to_city, date)
    remaining = get_remaining_seats(data, key)
    return {"remaining": remaining, "max": MAX_SEATS}


@app.post("/api/order")
async def create_order(order: OrderData):
    route_str = f"{order.from_city} → {order.to_city}"
    trip_key = get_trip_key(order.from_city, order.to_city, order.date)
    order_id = uuid.uuid4().hex[:6]

    if order.order_type == "ticket":
        data = load_data()
        remaining = get_remaining_seats(data, trip_key)

        if remaining <= 0 or order.seats > remaining:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "К сожалению, на выбранную дату мест больше нет. Выберите другой день."
                },
            )

        new_order = {
            "id": order_id,
            "trip_key": trip_key,
            "route": route_str,
            "date": order.date,
            "seats": order.seats,
            "name": order.name,
            "phone": order.phone,
            "details": order.details,
        }
        db_add_order(new_order)
        updated_data = load_data()
        new_remaining = get_remaining_seats(updated_data, trip_key)

        header = "🚐 <b>VIBUS — НОВАЯ БРОНЬ БИЛЕТА</b>"
        extra_label = "Детали и адрес"
        seats_info = f"\n💺 <b>Свободно мест осталось:</b> {new_remaining} из {MAX_SEATS}"
    else:
        header = "📦 <b>VIBUS — ЗАЯВКА НА ПОСЫЛКУ</b>"
        extra_label = "Служба и описание"
        seats_info = ""

    text = (
        f"{header}\n\n"
        f"📍 <b>Маршрут:</b> {route_str}\n"
        f"📅 <b>Дата:</b> {order.date}\n"
        f"ℹ️ <b>{extra_label}:</b> {order.details}\n"
        f"👤 <b>Клиент:</b> {order.name}\n"
        f"📞 <b>Телефон:</b> {order.phone}\n"
        f"💬 <b>Telegram:</b> {order.tg_username}"
        f"{seats_info}"
    )

    admin_kb = None
    if order.order_type == "ticket":
        admin_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"❌ Отменить бронь (+{order.seats} мест)",
                        callback_data=f"cancel|{order_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🚫 Закрыть весь рейс (мест нет)",
                        callback_data=f"full|{trip_key}",
                    )
                ],
            ]
        )

    for admin_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML",
                reply_markup=admin_kb,
            )
        except Exception as e:  # noqa: BLE001
            print(f"Ошибка отправки админу {admin_id}: {e}")

    return {"status": "ok"}


# --- ОБРАБОТЧИКИ БОТА ---
@dp.callback_query(F.data.startswith("cancel|"))
async def cancel_single_booking(callback: CallbackQuery):
    order_id = callback.data.split("|", 1)[1]
    data = load_data()

    target = next((o for o in data["orders"] if o["id"] == order_id), None)
    if not target:
        await callback.answer("Эта бронь уже была отменена.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    db_delete_order(order_id)

    new_text = (
        f"{callback.message.html_text}\n\n"
        f"❌ <b>БРОНЬ ОТМЕНЕНА (возвращено мест: {target['seats']})</b>"
    )
    await callback.message.edit_text(text=new_text, parse_mode="HTML", reply_markup=None)
    await callback.answer(
        f"Пассажир {target['name']} вычеркнут! Возвращено мест: {target['seats']}.",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("del_list|"))
async def delete_from_manifest(callback: CallbackQuery):
    order_id = callback.data.split("|", 1)[1]
    data = load_data()

    target = next((o for o in data["orders"] if o["id"] == order_id), None)
    if not target:
        await callback.answer("Пассажир уже удалён из списка.", show_alert=True)
    else:
        db_delete_order(order_id)
        await callback.answer(
            f"❌ {target['name']} вычеркнут (+{target['seats']} мест)",
            show_alert=True,
        )

    updated_data = load_data()
    text, kb = build_manifest_view(updated_data)
    await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("full|"))
async def mark_trip_full(callback: CallbackQuery):
    trip_key = callback.data.split("|", 1)[1]
    db_set_trip_closed(trip_key, True)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Открыть рейс снова",
                    callback_data=f"open|{trip_key}",
                )
            ]
        ]
    )
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Рейс закрыт! В приложении теперь пишет: Мест нет.", show_alert=True)


@dp.callback_query(F.data.startswith("open|"))
async def reopen_trip(callback: CallbackQuery):
    trip_key = callback.data.split("|", 1)[1]
    db_set_trip_closed(trip_key, False)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚫 Закрыть весь рейс (мест нет)",
                    callback_data=f"full|{trip_key}",
                )
            ]
        ]
    )
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Запись на рейс снова открыта!", show_alert=True)


@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    data = load_data()
    text, kb = build_manifest_view(data)
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "admin_list")
async def cb_admin_list(callback: CallbackQuery):
    data = load_data()
    text, kb = build_manifest_view(data)
    await callback.message.answer(text=text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    db_reset_all()
    await message.answer("🔄 Все брони и закрытые рейсы сброшены — на все даты снова доступно по 8 мест.")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    print(f"👉 Нажал /start: {message.from_user.first_name} | ID: {message.from_user.id}")
    rows = [
        [
            InlineKeyboardButton(
                text="🚐 Забронировать место / Посылку",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        ]
    ]

    if message.from_user.id in ADMIN_CHAT_IDS:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📋 Список пассажиров (вычеркнуть / места)",
                    callback_data="admin_list",
                )
            ]
        )

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await message.answer(
        "👋 Добро пожаловать в <b>VIBUS — VIP Перевозки</b>!\n"
        "🇺🇦 <b>Украина ↔ Болгария</b> 🇧🇬\n\n"
        "• Ежедневные рейсы без пересадок\n"
        "• Трансфер с адреса на адрес (Mercedes Sprinter, до 8 мест)\n"
        "• Передача посылок (Новая Почта, Meest, Speedy, Econt)\n\n"
        "Нажмите кнопку ниже, чтобы оформить заявку за 30 секунд:",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def main():
    init_db()
    port = int(os.getenv("PORT", 8000))  # noqa: PLW1508
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)

    if RENDER_URL:
        await bot.set_webhook(f"{RENDER_URL}/webhook", drop_pending_updates=True)
        await server.serve()
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        await asyncio.gather(
            server.serve(),
            dp.start_polling(bot, handle_signals=False),
        )


if __name__ == "__main__":
    asyncio.run(main())