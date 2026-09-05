import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, ForeignKey, func, extract, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

from bot import bot, dp
from aiogram.types import Update

# === Настройки безопасности ===
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 дней

# === База данных (Railway предоставит DATABASE_URL) ===
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///finance.db")

# Для SQLite нужно особым образом передавать параметры
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# === Модели ===
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_premium = Column(Boolean, default=False)
    premium_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    amount = Column(Float)  # положительное — расход, отрицательное — доход
    category = Column(String, default="Разное")
    comment = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Budget(Base):
    __tablename__ = "budgets"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    month = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)

# Создаём таблицы (если их ещё нет)
Base.metadata.create_all(bind=engine)

# === Схемы Pydantic ===
class UserCreate(BaseModel):
    email: EmailStr
    password: str

class UserOut(BaseModel):
    id: int
    email: EmailStr
    is_premium: bool
    premium_until: Optional[datetime] = None
    class Config:
        from_attributes = True

class TransactionCreate(BaseModel):
    amount: float
    category: Optional[str] = "Разное"
    comment: Optional[str] = None

class TransactionOut(TransactionCreate):
    id: int
    created_at: datetime
    class Config:
        from_attributes = True

# === Зависимости БД ===
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# === Безопасность ===
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось проверить учётные данные",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception
    return user

# === Lifespan: установка вебхука при старте ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    if domain:
        webhook_url = f"https://{domain}/webhook"
        await bot.set_webhook(url=webhook_url)
        print(f"✅ Вебхук установлен: {webhook_url}")
    else:
        print("⚠️ RAILWAY_PUBLIC_DOMAIN не задан — вебхук не установлен")
    yield
    await bot.session.close()

# === FastAPI приложение ===
app = FastAPI(lifespan=lifespan)

# === Эндпоинты ===
@app.get("/")
def root():
    return {"status": "ok", "message": "Finance App is running"}

@app.post("/auth/register", response_model=UserOut)
def register(user: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == user.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email уже зарегистрирован")
    hashed = get_password_hash(user.password)
    db_user = User(email=user.email, hashed_password=hashed)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

@app.post("/auth/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Неверный email или пароль")
    token = create_access_token({"sub": str(user.id)})
    return {"access_token": token, "token_type": "bearer"}

@app.get("/api/profile")
def get_profile(current_user: User = Depends(get_current_user)):
    return {
        "email": current_user.email,
        "is_premium": current_user.is_premium,
        "premium_until": current_user.premium_until,
    }

@app.post("/api/premium/activate")
def activate_premium(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.is_premium = True
    current_user.premium_until = datetime.utcnow() + timedelta(days=30)
    db.commit()
    return {"message": "Premium activated", "until": current_user.premium_until}

@app.post("/api/transactions", response_model=TransactionOut)
def create_transaction(
    transaction: TransactionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    db_transaction = Transaction(
        user_id=current_user.id,
        amount=transaction.amount,
        category=transaction.category,
        comment=transaction.comment
    )
    db.add(db_transaction)
    db.commit()
    db.refresh(db_transaction)
    return db_transaction

@app.get("/api/transactions", response_model=List[TransactionOut])
def get_transactions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    transactions = db.query(Transaction).filter(
        Transaction.user_id == current_user.id
    ).order_by(Transaction.created_at.desc()).all()
    return transactions

@app.delete("/api/transactions/{id}")
def delete_transaction(
    id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    transaction = db.query(Transaction).filter(
        Transaction.id == id,
        Transaction.user_id == current_user.id
    ).first()
    if not transaction:
        raise HTTPException(status_code=404, detail="Транзакция не найдена")
    db.delete(transaction)
    db.commit()
    return {"detail": "Транзакция удалена"}

@app.get("/api/stats")
def get_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    transactions = db.query(Transaction).filter(Transaction.user_id == current_user.id).all()
    expenses = sum(t.amount for t in transactions if t.amount > 0)
    income = sum(abs(t.amount) for t in transactions if t.amount < 0)
    return {"expenses": expenses, "income": income}

@app.get("/api/budget")
def get_budget(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = datetime.now()
    month, year = now.month, now.year
    budget = db.query(Budget).filter_by(user_id=current_user.id, month=month, year=year).first()
    spent = db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.user_id == current_user.id,
        Transaction.amount > 0,
        func.extract('month', Transaction.created_at) == month,
        func.extract('year', Transaction.created_at) == year
    ).scalar()
    if not budget:
        return {"budget": None, "spent": spent, "remaining": None}
    remaining = budget.amount - spent
    return {"budget": budget.amount, "spent": spent, "remaining": remaining}

@app.post("/api/budget")
def set_budget(payload: dict, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    amount = payload.get("amount")
    if amount is None or amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    now = datetime.now()
    month, year = now.month, now.year
    budget = db.query(Budget).filter_by(user_id=current_user.id, month=month, year=year).first()
    if budget:
        budget.amount = amount
    else:
        budget = Budget(user_id=current_user.id, amount=amount, month=month, year=year)
        db.add(budget)
    db.commit()
    return {"message": "Бюджет установлен"}

# === Webhook для Telegram ===
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("🔥 Получен апдейт:", data)
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}