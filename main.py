import hashlib
import hmac
import json
import os
import time as time_module
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
                        Time, create_engine, event, select)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'restaurant.db'}")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
DEV_TELEGRAM_ID = int(os.getenv("DEV_TELEGRAM_ID", "111111"))

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200), default="Гость")
    phone: Mapped[str | None] = mapped_column(String(40))
    role: Mapped[str] = mapped_column(String(20), default="client")


class DiningTable(Base):
    __tablename__ = "tables"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_number: Mapped[int] = mapped_column(Integer, unique=True)
    seats_count: Mapped[int] = mapped_column(Integer, default=4)


class MenuItem(Base):
    __tablename__ = "menu_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(80), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(default=True)


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("tables.id"), index=True)
    booking_date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped[User] = relationship()
    table: Mapped[DiningTable] = relationship()


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id", ondelete="SET NULL"))
    delivery_type: Mapped[str] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(String(500))
    total_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped[User] = relationship()
    booking: Mapped[Booking | None] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    menu_item_id: Mapped[int | None] = mapped_column(ForeignKey("menu_items.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(160))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    quantity: Mapped[int] = mapped_column(Integer)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped[User] = relationship()


class BookingInput(BaseModel):
    table_id: int
    booking_date: date
    start_time: time
    end_time: time


class OrderLineInput(BaseModel):
    menu_item_id: int
    quantity: int = Field(ge=1, le=50)


class OrderInput(BaseModel):
    delivery_type: Literal["dine_in", "delivery"]
    address: str | None = Field(default=None, max_length=500)
    booking_id: int | None = None
    items: list[OrderLineInput] = Field(min_length=1, max_length=50)


class ReviewInput(BaseModel):
    rating: int = Field(ge=1, le=5)
    text: str = Field(min_length=3, max_length=2000)


class MenuInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    description: str = Field(default="", max_length=2000)


class BookingStatusInput(BaseModel):
    status: Literal["confirmed", "cancelled"]


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(connection, _record):
    if DATABASE_URL.startswith("sqlite"):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    if DATABASE_URL.startswith("sqlite"):
        with engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TRIGGER IF NOT EXISTS prevent_booking_overlap_insert
                BEFORE INSERT ON bookings
                WHEN NEW.status IN ('pending', 'confirmed')
                BEGIN
                    SELECT RAISE(ABORT, 'booking_conflict')
                    WHERE EXISTS (
                        SELECT 1 FROM bookings b
                        WHERE b.table_id = NEW.table_id
                          AND b.booking_date = NEW.booking_date
                          AND b.status IN ('pending', 'confirmed')
                          AND NEW.start_time < b.end_time
                          AND NEW.end_time > b.start_time
                    );
                END;
            """)
            connection.exec_driver_sql("""
                CREATE TRIGGER IF NOT EXISTS prevent_booking_overlap_update
                BEFORE UPDATE OF table_id, booking_date, start_time, end_time, status ON bookings
                WHEN NEW.status IN ('pending', 'confirmed')
                BEGIN
                    SELECT RAISE(ABORT, 'booking_conflict')
                    WHERE EXISTS (
                        SELECT 1 FROM bookings b
                        WHERE b.id != NEW.id
                          AND b.table_id = NEW.table_id
                          AND b.booking_date = NEW.booking_date
                          AND b.status IN ('pending', 'confirmed')
                          AND NEW.start_time < b.end_time
                          AND NEW.end_time > b.start_time
                    );
                END;
            """)
        with SessionLocal() as db:
            if db.scalar(select(DiningTable.id).limit(1)) is None:
                db.add_all([DiningTable(table_number=number, seats_count=seats) for number, seats in
                            [(1, 2), (2, 4), (3, 4), (4, 6), (5, 6), (6, 8)]])
            if db.scalar(select(MenuItem.id).limit(1)) is None:
                db.add_all([
                    MenuItem(title="Классический кальян", category="Кальяны", price=1200, description="Чаша на выбор"),
                    MenuItem(title="Фруктовая тарелка", category="Закуски", price=650, description="Сезонные фрукты"),
                    MenuItem(title="Лимонад домашний", category="Напитки", price=350, description="Цитрус и мята"),
                    MenuItem(title="Чайник чая", category="Напитки", price=500, description="Чай на выбор"),
                ])
            db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Lounge Bar Demo", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def get_db():
    with SessionLocal() as db:
        yield db


def validate_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="На сервере не настроен BOT_TOKEN")
    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = values.pop("hash")
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Некорректные данные Telegram")
    if abs(time_module.time() - auth_date) > 86400:
        raise HTTPException(status_code=401, detail="Сессия Telegram устарела")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Не удалось проверить пользователя Telegram")
    if not isinstance(user.get("id"), int):
        raise HTTPException(status_code=401, detail="В данных Telegram отсутствует ID пользователя")
    return user


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    x_telegram_init_data: str | None = Header(default=None),
    x_dev_telegram_id: str | None = Header(default=None),
) -> User:
    if x_telegram_init_data:
        telegram_user = validate_init_data(x_telegram_init_data)
        telegram_id = telegram_user["id"]
        username = telegram_user.get("username")
        first_name = telegram_user.get("first_name", "")
        name = " ".join(part for part in [first_name, telegram_user.get("last_name", "")] if part).strip() or "Гость"
    elif DEV_MODE and x_dev_telegram_id:
        try:
            telegram_id = int(x_dev_telegram_id)
        except ValueError:
            raise HTTPException(status_code=401, detail="Некорректный dev ID")
        if telegram_id != DEV_TELEGRAM_ID:
            raise HTTPException(status_code=401, detail="Dev ID не совпадает с DEV_TELEGRAM_ID")
        username, name = "local_demo", "Локальный демо-пользователь"
    else:
        raise HTTPException(status_code=401, detail="Откройте приложение через Telegram")

    user = db.scalar(select(User).where(User.telegram_id == telegram_id))
    expected_role = "admin" if telegram_id == ADMIN_TELEGRAM_ID and ADMIN_TELEGRAM_ID else "client"
    if user is None:
        user = User(telegram_id=telegram_id, username=username, name=name, role=expected_role)
        db.add(user)
    else:
        user.username = username or user.username
        user.name = name or user.name
        user.role = expected_role
    db.commit()
    db.refresh(user)
    request.state.telegram_id = telegram_id
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin" or not ADMIN_TELEGRAM_ID or user.telegram_id != ADMIN_TELEGRAM_ID:
        raise HTTPException(status_code=403, detail="Доступ только для администратора")
    return user


def money(value: Decimal) -> float:
    return float(value)


def booking_dict(booking: Booking) -> dict:
    return {
        "id": booking.id, "user_name": booking.user.name, "username": booking.user.username,
        "table_id": booking.table_id, "table_number": booking.table.table_number,
        "booking_date": booking.booking_date.isoformat(), "start_time": booking.start_time.strftime("%H:%M"),
        "end_time": booking.end_time.strftime("%H:%M"), "status": booking.status,
    }


def order_dict(order: Order) -> dict:
    return {
        "id": order.id, "user_name": order.user.name, "username": order.user.username,
        "booking_id": order.booking_id, "booking": booking_dict(order.booking) if order.booking else None,
        "delivery_type": order.delivery_type, "address": order.address, "total_price": money(order.total_price),
        "status": order.status, "created_at": order.created_at.isoformat(),
        "items": [{"title": line.title, "unit_price": money(line.unit_price), "quantity": line.quantity}
                  for line in order.items],
    }


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "telegram_id": user.telegram_id, "name": user.name,
            "username": user.username, "role": user.role}


@app.get("/api/menu")
def get_menu(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    items = db.scalars(select(MenuItem).where(MenuItem.is_active.is_(True)).order_by(MenuItem.category, MenuItem.title)).all()
    return [{"id": item.id, "title": item.title, "category": item.category,
             "price": money(item.price), "description": item.description} for item in items]


@app.get("/api/offers")
def get_offers(_user: User = Depends(get_current_user)):
    return [
        {"title": "Счастливые часы", "description": "Скидка 20% на меню с 14:00 до 17:00 по будням."},
        {"title": "Вечер для компании", "description": "При бронировании от 4 гостей чайник чая в подарок."},
        {"title": "День рождения", "description": "Скидка 10% в день рождения. Покажите документ администратору."},
    ]


@app.get("/api/tables")
def get_tables(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tables = db.scalars(select(DiningTable).order_by(DiningTable.table_number)).all()
    return [{"id": table.id, "table_number": table.table_number, "seats_count": table.seats_count} for table in tables]


@app.post("/api/bookings", status_code=status.HTTP_201_CREATED)
def create_booking(payload: BookingInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.booking_date < date.today():
        raise HTTPException(status_code=400, detail="Нельзя выбрать дату в прошлом")
    if payload.start_time >= payload.end_time:
        raise HTTPException(status_code=400, detail="Время окончания должно быть позже начала")
    duration = datetime.combine(payload.booking_date, payload.end_time) - datetime.combine(payload.booking_date, payload.start_time)
    if duration > timedelta(hours=8):
        raise HTTPException(status_code=400, detail="Бронь может длиться не более 8 часов")
    if db.get(DiningTable, payload.table_id) is None:
        raise HTTPException(status_code=404, detail="Столик не найден")
    conflict = db.scalar(select(Booking.id).where(
        Booking.table_id == payload.table_id,
        Booking.booking_date == payload.booking_date,
        Booking.status.in_(["pending", "confirmed"]),
        Booking.start_time < payload.end_time,
        Booking.end_time > payload.start_time,
    ).limit(1))
    if conflict:
        raise HTTPException(status_code=409, detail="Столик уже занят на это время")
    booking = Booking(user_id=user.id, table_id=payload.table_id, booking_date=payload.booking_date,
                      start_time=payload.start_time, end_time=payload.end_time, status="pending")
    db.add(booking)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if "booking_conflict" in str(error.orig):
            raise HTTPException(status_code=409, detail="Столик уже занят на это время")
        raise
    db.refresh(booking)
    return {"id": booking.id, "status": booking.status, "message": "Бронь создана и ожидает подтверждения"}


@app.get("/api/bookings/mine")
def my_bookings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bookings = db.scalars(select(Booking).where(Booking.user_id == user.id).order_by(Booking.booking_date.desc())).all()
    return [booking_dict(booking) for booking in bookings]


@app.post("/api/orders", status_code=status.HTTP_201_CREATED)
def create_order(payload: OrderInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.delivery_type == "delivery" and not (payload.address and payload.address.strip()):
        raise HTTPException(status_code=400, detail="Для доставки укажите адрес")
    if payload.delivery_type == "delivery" and payload.booking_id is not None:
        raise HTTPException(status_code=400, detail="Доставку нельзя привязать к брони столика")
    booking = None
    if payload.booking_id is not None:
        booking = db.get(Booking, payload.booking_id)
        if booking is None or booking.user_id != user.id or booking.status == "cancelled":
            raise HTTPException(status_code=400, detail="Бронь не найдена или недоступна")
    item_ids = {line.menu_item_id for line in payload.items}
    menu_items = db.scalars(select(MenuItem).where(MenuItem.id.in_(item_ids), MenuItem.is_active.is_(True))).all()
    by_id = {item.id: item for item in menu_items}
    if len(by_id) != len(item_ids):
        raise HTTPException(status_code=400, detail="Одна или несколько позиций меню недоступны")
    total = sum((by_id[line.menu_item_id].price * line.quantity for line in payload.items), Decimal("0"))
    order = Order(user_id=user.id, booking_id=booking.id if booking else None,
                  delivery_type=payload.delivery_type,
                  address=payload.address.strip() if payload.delivery_type == "delivery" else None,
                  total_price=total, status="new")
    order.items = [OrderItem(menu_item_id=by_id[line.menu_item_id].id,
                             title=by_id[line.menu_item_id].title,
                             unit_price=by_id[line.menu_item_id].price,
                             quantity=line.quantity) for line in payload.items]
    db.add(order)
    db.commit()
    db.refresh(order)
    return {"id": order.id, "total_price": money(order.total_price), "status": order.status}


@app.post("/api/reviews", status_code=status.HTTP_201_CREATED)
def create_review(payload: ReviewInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    review = Review(user_id=user.id, rating=payload.rating, text=payload.text.strip())
    db.add(review)
    db.commit()
    return {"message": "Спасибо за отзыв!"}


@app.get("/api/admin/bookings")
def admin_bookings(status_filter: str | None = None, booking_date: date | None = None,
                   _admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    query = select(Booking).order_by(Booking.booking_date.desc(), Booking.start_time)
    if status_filter in {"pending", "confirmed", "cancelled"}:
        query = query.where(Booking.status == status_filter)
    if booking_date:
        query = query.where(Booking.booking_date == booking_date)
    return [booking_dict(item) for item in db.scalars(query).all()]


@app.patch("/api/admin/bookings/{booking_id}")
def update_booking(booking_id: int, payload: BookingStatusInput,
                  _admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Бронь не найдена")
    booking.status = payload.status
    db.commit()
    return {"id": booking.id, "status": booking.status}


@app.get("/api/admin/orders")
def admin_orders(_admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    orders = db.scalars(select(Order).order_by(Order.created_at.desc())).all()
    return [order_dict(order) for order in orders]


@app.post("/api/admin/menu", status_code=status.HTTP_201_CREATED)
def admin_add_menu(payload: MenuInput, _admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = MenuItem(title=payload.title.strip(), category=payload.category.strip(), price=payload.price,
                    description=payload.description.strip())
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "title": item.title, "category": item.category,
            "price": money(item.price), "description": item.description}


@app.delete("/api/admin/menu/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_menu(item_id: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(MenuItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    item.is_active = False
    db.commit()
    return None
