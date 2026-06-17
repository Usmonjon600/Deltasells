from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, text, case
from pydantic import BaseModel
from typing import List, Optional
import datetime, os, threading, time, urllib.request, urllib.parse, json, html, logging

# --------------- LOGGING SETUP ---------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "scheduler.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("delta_sells")

# Load .env file manually
def load_dotenv(dotenv_path=".env"):
    if os.path.exists(dotenv_path):
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

load_dotenv()
load_dotenv("../.env")

from .database import engine, get_db, Base, SessionLocal
from .models import User, Product, Sale, SaleItem, Expense, OwnerUsage, InternalTransfer, Debt, Settings

# --------------- Create tables ---------------
Base.metadata.create_all(bind=engine)

# Add payment_type, cash_amount, card_amount columns to sales table if not exists (SQLite migration)
try:
    with engine.connect() as conn:
        # Check payment_type column
        try:
            conn.execute(text("ALTER TABLE sales ADD COLUMN payment_type VARCHAR DEFAULT 'naqd'"))
            conn.commit()
        except Exception:
            pass
        
        # Check cash_amount column
        try:
            conn.execute(text("ALTER TABLE sales ADD COLUMN cash_amount FLOAT DEFAULT 0.0"))
            conn.commit()
        except Exception:
            pass

        # Check card_amount column
        try:
            conn.execute(text("ALTER TABLE sales ADD COLUMN card_amount FLOAT DEFAULT 0.0"))
            conn.commit()
        except Exception:
            pass

        # Perform backfill/data-cleanup for existing rows
        # If cash_amount = 0 and card_amount = 0, initialize based on payment_type
        try:
            conn.execute(text("UPDATE sales SET cash_amount = total_amount WHERE payment_type = 'naqd' AND cash_amount = 0 AND card_amount = 0"))
            conn.execute(text("UPDATE sales SET card_amount = total_amount WHERE payment_type = 'karta' AND cash_amount = 0 AND card_amount = 0"))
            conn.commit()
        except Exception as ex:
            logger.error(f"Error backfilling cash/card amount: {ex}")

        # Check expense_type column
        try:
            conn.execute(text("ALTER TABLE expenses ADD COLUMN expense_type VARCHAR DEFAULT 'naqd'"))
            conn.commit()
        except Exception:
            pass

        # Check transfer_direction column for internal_transfers
        try:
            conn.execute(text("ALTER TABLE internal_transfers ADD COLUMN transfer_direction VARCHAR DEFAULT 'card_to_cash'"))
            conn.commit()
        except Exception:
            pass

        logger.info("Database migrations (payment_type, cash_amount, card_amount, expense_type, transfer_direction) completed successfully.")
except Exception as e:
    logger.info(f"Database migration info: {e}")

# --------------- App ---------------
app = FastAPI(title="Delta Sells – CRM / Kassa")

# --------------- AUTHENTICATION ---------------
SECRET_KEY = os.getenv("JWT_SECRET", "super-secret-key-delta-sells-2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

import bcrypt

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

class UserCreate(BaseModel):
    username: str
    password: str
    pincode: Optional[str] = None
    shop_name: str

class UserLogin(BaseModel):
    username: str
    password: str

class QuickLogin(BaseModel):
    pincode: str

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except Exception:
        return False

def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def create_access_token(data: dict, expires_delta: Optional[datetime.timedelta] = None):
    to_encode = data.copy()
    expire = datetime.datetime.utcnow() + (expires_delta or datetime.timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token yaroqsiz",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    from .models import User
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
    return user

@app.post("/api/auth/register")
def register_user(data: UserCreate, db: Session = Depends(get_db)):
    from .models import User
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=400, detail="Bunday login allaqachon mavjud")
    hashed_password = get_password_hash(data.password)
    user = User(username=data.username, password_hash=hashed_password, pincode=data.pincode, shop_name=data.shop_name)
    db.add(user)
    db.commit()
    db.refresh(user)
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer", "shop_name": user.shop_name}

@app.post("/api/auth/login")
def login(data: UserLogin, db: Session = Depends(get_db)):
    from .models import User
    user = db.query(User).filter(User.username == data.username).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Login yoki parol noto'g'ri")
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer", "shop_name": user.shop_name}

@app.post("/api/auth/quick-login")
def quick_login(data: QuickLogin, db: Session = Depends(get_db)):
    from .models import User
    if not data.pincode:
        raise HTTPException(status_code=400, detail="PIN kiritilmadi")
    user = db.query(User).filter(User.pincode == data.pincode).first()
    if not user:
        raise HTTPException(status_code=400, detail="PIN xato")
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer", "shop_name": user.shop_name}

@app.get("/api/auth/me")
def get_me(current_user = Depends(get_current_user)):
    return {"username": current_user.username, "shop_name": current_user.shop_name}

class UserSecurityUpdate(BaseModel):
    current_password: str
    new_username: Optional[str] = None
    new_password: Optional[str] = None
    new_pincode: Optional[str] = None

@app.put("/api/user/update-security")
def update_security(data: UserSecurityUpdate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    if not verify_password(data.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Joriy parol noto'g'ri")
    
    if data.new_username and data.new_username != current_user.username:
        from .models import User
        if db.query(User).filter(User.username == data.new_username).first():
            raise HTTPException(status_code=400, detail="Bu login allaqachon mavjud")
        current_user.username = data.new_username
        
    if data.new_password:
        current_user.password_hash = get_password_hash(data.new_password)
        
    if data.new_pincode is not None:
        current_user.pincode = data.new_pincode
        
    db.commit()
    return {"ok": True, "message": "Xavfsizlik ma'lumotlari yangilandi"}

# --------------- Pydantic Schemas ---------------

class ProductIn(BaseModel):
    id: Optional[int] = None
    name: str
    price: float
    cost_price: float
    stock: int = 0
    barcode: Optional[str] = None
    image_url: Optional[str] = None

class ProductOut(BaseModel):
    id: int
    name: str
    price: float
    cost_price: float
    stock: int
    barcode: Optional[str] = None
    image_url: Optional[str] = None
    class Config:
        from_attributes = True

class SaleItemIn(BaseModel):
    product_id: int
    quantity: int

class SaleIn(BaseModel):
    items: List[SaleItemIn]
    payment_type: Optional[str] = "naqd"
    cash_amount: Optional[float] = 0.0
    card_amount: Optional[float] = 0.0
    created_at: Optional[datetime.datetime] = None

class SaleItemUpdate(BaseModel):
    quantity: int

class ExpenseIn(BaseModel):
    title: str
    amount: float
    expense_type: Optional[str] = "naqd"
    created_at: Optional[datetime.datetime] = None

class ExpenseReturnIn(BaseModel):
    expense_id: int
    product_id: int
    quantity: int
    refund_amount: float

class ExpenseExchangeIn(BaseModel):
    expense_id: int
    returned_product_id: int
    returned_qty: int
    new_product_id: int
    new_qty: int
    additional_payment: float = 0.0

class ExpenseUpdateIn(BaseModel):
    title: str
    amount: float
    expense_type: Optional[str] = "naqd"
    created_at: Optional[datetime.datetime] = None

class ExpenseOut(BaseModel):
    id: int
    title: str
    amount: float
    expense_type: str
    created_at: datetime.datetime
    class Config:
        from_attributes = True

class OwnerUsageIn(BaseModel):
    product_id: int
    quantity: int

class OwnerUsageUpdateIn(BaseModel):
    product_id: int
    new_quantity: int

class InternalTransferIn(BaseModel):
    amount: float
    transfer_direction: str  # "card_to_cash" or "cash_to_card"
    description: Optional[str] = None

class DebtIn(BaseModel):
    customer_name: str
    product_id: int
    quantity: int
    debt_amount: float

class DebtUpdateIn(BaseModel):
    customer_name: str
    product_id: int
    quantity: int
    debt_amount: float

class DebtPayIn(BaseModel):
    payment_type: str  # "naqd" or "karta"

class SettingsIn(BaseModel):
    language: str
    theme: str
    zoom: str

class SettingsOut(BaseModel):
    language: str
    theme: str
    zoom: str

class DashboardOut(BaseModel):
    today_revenue: float
    today_profit: float
    month_revenue: float
    month_profit: float
    today_cash_revenue: float
    today_card_revenue: float
    month_cash_revenue: float
    month_card_revenue: float

class StatsReportOut(BaseModel):
    today_total_revenue: float
    month_total_revenue: float
    today_cash_treasury: float
    today_card_revenue: float
    month_net_profit: float

# --------------- PRODUCTS ---------------

@app.get("/api/products", response_model=List[ProductOut])
def list_products(q: Optional[str] = None, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    query = db.query(Product).filter(Product.user_id == current_user.id)
    if q:
        search = f"%{q}%"
        query = query.filter(
            (Product.name.ilike(search)) | (Product.barcode.ilike(search))
        )
    return query.order_by(Product.name).all()


@app.post("/api/products", response_model=ProductOut)
def create_or_update_product(data: ProductIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    try:
        if data.id:
            product = db.query(Product).filter(Product.id == data.id, Product.user_id == current_user.id).first()
            if not product:
                raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
            product.name = data.name
            product.price = data.price
            product.cost_price = data.cost_price
            product.stock = data.stock
            product.barcode = data.barcode or None
            product.image_url = data.image_url or None
        else:
            product = Product(
                user_id=current_user.id,
                name=data.name,
                price=data.price,
                cost_price=data.cost_price,
                stock=data.stock,
                barcode=data.barcode or None,
                image_url=data.image_url or None,
            )
            db.add(product)
        db.commit()
        db.refresh(product)
        return product
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Bu shtrix-kodga ega mahsulot allaqachon mavjud!")


@app.delete("/api/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    product = db.query(Product).filter(Product.id == product_id, Product.user_id == current_user.id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
    try:
        sale_items = db.query(SaleItem).filter(SaleItem.product_id == product_id).all()
        for item in sale_items:
            sale = item.sale
            if sale:
                amount_to_sub = item.quantity * item.price
                profit_to_sub = item.quantity * (item.price - product.cost_price)
                sale.total_amount = round(sale.total_amount - amount_to_sub, 2)
                sale.profit = round(sale.profit - profit_to_sub, 2)
            db.delete(item)
        db.flush()
        
        # Clean up empty sales
        empty_sales = db.query(Sale).filter(~Sale.items.any()).all()
        for s in empty_sales:
            db.delete(s)
            
        db.delete(product)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"O'chirishda xatolik: {str(e)}")
    return {"ok": True}

# --------------- SALES ---------------

@app.post("/api/sales")
def create_sale(data: SaleIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    if not data.items:
        raise HTTPException(status_code=400, detail="Savat bo'sh")

    total_amount = 0.0
    total_profit = 0.0
    sale_items: list[SaleItem] = []

    # Get the sale creation time: if data.created_at is provided, use it. Otherwise current local time.
    sale_created_at = data.created_at or datetime.datetime.now()

    for item in data.items:
        product = db.query(Product).filter(Product.id == item.product_id, Product.user_id == current_user.id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Mahsulot #{item.product_id} topilmadi")
        if product.stock < item.quantity:
            raise HTTPException(
                status_code=400,
                detail=f"'{product.name}' uchun omborda yetarli emas (qoldiq: {product.stock})",
            )
        line_total = product.price * item.quantity
        line_profit = (product.price - product.cost_price) * item.quantity
        total_amount += line_total
        total_profit += line_profit

        product.stock -= item.quantity

        sale_items.append(
            SaleItem(user_id=current_user.id, product_id=product.id, quantity=item.quantity, price=product.price, created_at=sale_created_at)
        )

    pay_type = data.payment_type or "naqd"
    cash_amt = 0.0
    card_amt = 0.0

    if pay_type == "naqd":
        cash_amt = round(total_amount, 2)
        card_amt = 0.0
    elif pay_type == "karta":
        cash_amt = 0.0
        card_amt = round(total_amount, 2)
    elif pay_type == "aralash":
        cash_amt = round(data.cash_amount or 0.0, 2)
        card_amt = round(data.card_amount or 0.0, 2)
        if abs((cash_amt + card_amt) - round(total_amount, 2)) > 0.02:
            raise HTTPException(
                status_code=400,
                detail=f"Aralash to'lov summasi jami summaga mos kelmadi. Naqd: {cash_amt}, Karta: {card_amt}, Jami: {round(total_amount, 2)}"
            )
    else:
        pay_type = "naqd"
        cash_amt = round(total_amount, 2)
        card_amt = 0.0

    sale = Sale(
        user_id=current_user.id,
        total_amount=round(total_amount, 2),
        profit=round(total_profit, 2),
        payment_type=pay_type,
        cash_amount=cash_amt,
        card_amount=card_amt,
        created_at=sale_created_at
    )
    db.add(sale)
    db.flush()

    for si in sale_items:
        si.sale_id = sale.id
        db.add(si)

    db.commit()
    db.refresh(sale)
    return {
        "id": sale.id,
        "total_amount": sale.total_amount,
        "profit": sale.profit,
        "payment_type": sale.payment_type,
        "cash_amount": sale.cash_amount,
        "card_amount": sale.card_amount,
        "created_at": sale.created_at.isoformat(),
    }

# --------------- DASHBOARD ---------------

@app.get("/api/dashboard", response_model=DashboardOut)
def dashboard(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    now = datetime.datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def get_cash_revenue(start):
        sale_rev = db.query(func.coalesce(func.sum(Sale.cash_amount), 0)).filter(Sale.user_id == current_user.id, Sale.created_at >= start).scalar() or 0
        return float(sale_rev)

    def get_card_revenue(start):
        sale_rev = db.query(func.coalesce(func.sum(Sale.card_amount), 0)).filter(Sale.user_id == current_user.id, Sale.created_at >= start).scalar() or 0
        return float(sale_rev)

    def get_sales_profit(start):
        sale_prof = db.query(func.coalesce(func.sum(Sale.profit), 0)).filter(Sale.user_id == current_user.id, Sale.created_at >= start).scalar() or 0
        debt_prof = db.query(func.coalesce(func.sum(Debt.profit), 0)).filter(Debt.user_id == current_user.id, Debt.created_at >= start).scalar() or 0
        return float(sale_prof) + float(debt_prof)

    today_prof = get_sales_profit(today_start)
    month_prof = get_sales_profit(month_start)

    today_cash_sales = get_cash_revenue(today_start)
    today_card_sales = get_card_revenue(today_start)
    month_cash_sales = get_cash_revenue(month_start)
    month_card_sales = get_card_revenue(month_start)

    # Cash expenses
    today_exp_cash = float(db.query(func.coalesce(func.sum(Expense.amount), 0)).filter(Expense.user_id == current_user.id, Expense.created_at >= today_start, Expense.expense_type == 'naqd').scalar() or 0)
    month_exp_cash = float(db.query(func.coalesce(func.sum(Expense.amount), 0)).filter(Expense.user_id == current_user.id, Expense.created_at >= month_start, Expense.expense_type == 'naqd').scalar() or 0)

    # Card expenses
    today_exp_card = float(db.query(func.coalesce(func.sum(Expense.amount), 0)).filter(Expense.user_id == current_user.id, Expense.created_at >= today_start, Expense.expense_type == 'karta').scalar() or 0)
    month_exp_card = float(db.query(func.coalesce(func.sum(Expense.amount), 0)).filter(Expense.user_id == current_user.id, Expense.created_at >= month_start, Expense.expense_type == 'karta').scalar() or 0)

    transfers_card_to_cash = float(db.query(func.coalesce(func.sum(InternalTransfer.amount), 0)).filter(
        InternalTransfer.user_id == current_user.id, InternalTransfer.transfer_direction == 'card_to_cash'
    ).scalar() or 0)
    
    transfers_cash_to_card = float(db.query(func.coalesce(func.sum(InternalTransfer.amount), 0)).filter(
        InternalTransfer.user_id == current_user.id, InternalTransfer.transfer_direction == 'cash_to_card'
    ).scalar() or 0)

    net_cash_from_transfers = transfers_card_to_cash - transfers_cash_to_card

    today_cash_rev = today_cash_sales - today_exp_cash + net_cash_from_transfers
    month_cash_rev = month_cash_sales - month_exp_cash + net_cash_from_transfers
    today_card_rev = today_card_sales - today_exp_card - net_cash_from_transfers
    month_card_rev = month_card_sales - month_exp_card - net_cash_from_transfers

    today_rev = today_cash_rev + today_card_rev
    month_rev = month_cash_rev + month_card_rev

    # Subtract owner usages cost price from profit
    today_owner_cost = float(db.query(func.coalesce(func.sum(OwnerUsage.total_cost_price), 0)).filter(OwnerUsage.user_id == current_user.id, OwnerUsage.created_at >= today_start).scalar() or 0)
    month_owner_cost = float(db.query(func.coalesce(func.sum(OwnerUsage.total_cost_price), 0)).filter(OwnerUsage.user_id == current_user.id, OwnerUsage.created_at >= month_start).scalar() or 0)

    today_prof -= today_owner_cost
    month_prof -= month_owner_cost

    return DashboardOut(
        today_revenue=round(today_rev, 2),
        today_profit=round(today_prof, 2),
        month_revenue=round(month_rev, 2),
        month_profit=round(month_prof, 2),
        today_cash_revenue=round(today_cash_rev, 2),
        today_card_revenue=round(today_card_rev, 2),
        month_cash_revenue=round(month_cash_rev, 2),
        month_card_revenue=round(month_card_rev, 2),
    )

@app.get("/api/reports/stats", response_model=StatsReportOut)
def reports_stats(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    now = datetime.datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # 1. Bugungi Jami Tushum
    sale_today_rev = float(db.query(func.coalesce(func.sum(Sale.total_amount), 0)).filter(Sale.user_id == current_user.id, Sale.created_at >= today_start).scalar() or 0)
    today_total_revenue = sale_today_rev

    # 2. Oylik Jami Tushum
    sale_month_rev = float(db.query(func.coalesce(func.sum(Sale.total_amount), 0)).filter(Sale.user_id == current_user.id, Sale.created_at >= month_start).scalar() or 0)
    month_total_revenue = sale_month_rev

    # 3. Bugungi Naqd G'azna (Naqd tushum - Naqd chiqimlar) + barcha o'tkazmalar yig'indisi
    sale_today_cash = float(db.query(func.coalesce(func.sum(Sale.cash_amount), 0)).filter(Sale.user_id == current_user.id, Sale.created_at >= today_start).scalar() or 0)
    today_cash_sales = sale_today_cash
    today_cash_expenses = float(db.query(func.coalesce(func.sum(Expense.amount), 0)).filter(
        Expense.user_id == current_user.id, Expense.created_at >= today_start, Expense.expense_type == 'naqd'
    ).scalar() or 0)
    
    transfers_card_to_cash = float(db.query(func.coalesce(func.sum(InternalTransfer.amount), 0)).filter(
        InternalTransfer.user_id == current_user.id, InternalTransfer.transfer_direction == 'card_to_cash'
    ).scalar() or 0)
    
    transfers_cash_to_card = float(db.query(func.coalesce(func.sum(InternalTransfer.amount), 0)).filter(
        InternalTransfer.user_id == current_user.id, InternalTransfer.transfer_direction == 'cash_to_card'
    ).scalar() or 0)

    net_cash_from_transfers = transfers_card_to_cash - transfers_cash_to_card

    today_cash_treasury = today_cash_sales - today_cash_expenses + net_cash_from_transfers

    # 4. Bugungi Karta To'lovlari (Karta tushum - Karta chiqimlar) - barcha o'tkazmalar yig'indisi
    sale_today_card = float(db.query(func.coalesce(func.sum(Sale.card_amount), 0)).filter(Sale.user_id == current_user.id, Sale.created_at >= today_start).scalar() or 0)
    today_card_sales = sale_today_card
    today_card_expenses = float(db.query(func.coalesce(func.sum(Expense.amount), 0)).filter(
        Expense.user_id == current_user.id, Expense.created_at >= today_start, Expense.expense_type == 'karta'
    ).scalar() or 0)
    today_card_revenue = today_card_sales - today_card_expenses - net_cash_from_transfers

    # 5. Oylik Sof Foyda (Oylik Foyda - Oylik o'zim olganlarim)
    sale_month_prof = float(db.query(func.coalesce(func.sum(Sale.profit), 0)).filter(Sale.user_id == current_user.id, Sale.created_at >= month_start).scalar() or 0)
    debt_month_prof = float(db.query(func.coalesce(func.sum(Debt.profit), 0)).filter(Debt.user_id == current_user.id, Debt.created_at >= month_start).scalar() or 0)
    month_sales_profit = sale_month_prof + debt_month_prof
    month_owner_cost = float(db.query(func.coalesce(func.sum(OwnerUsage.total_cost_price), 0)).filter(
        OwnerUsage.user_id == current_user.id, OwnerUsage.created_at >= month_start
    ).scalar() or 0)
    month_net_profit = month_sales_profit - month_owner_cost

    return StatsReportOut(
        today_total_revenue=round(today_total_revenue, 2),
        month_total_revenue=round(month_total_revenue, 2),
        today_cash_treasury=round(today_cash_treasury, 2),
        today_card_revenue=round(today_card_revenue, 2),
        month_net_profit=round(month_net_profit, 2)
    )

# --------------- SALES HISTORY ---------------

@app.get("/api/sales")
def list_sales(date: Optional[str] = None, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    query = db.query(Sale).filter(Sale.user_id == current_user.id).order_by(Sale.created_at.desc())
    if date:
        # Filter by specific date 'YYYY-MM-DD'
        query = query.filter(func.date(Sale.created_at) == date)
    
    sales = query.limit(100).all()
    result = []
    for s in sales:
        result.append({
            "id": s.id,
            "total_amount": s.total_amount,
            "profit": s.profit,
            "payment_type": s.payment_type,
            "cash_amount": s.cash_amount,
            "card_amount": s.card_amount,
            "note": s.note,
            "created_at": s.created_at.isoformat(),
            "items": [
                {
                    "item_id": si.id,
                    "product_name": si.product.name if si.product else "O'chirilgan mahsulot",
                    "quantity": si.quantity,
                    "price": si.price,
                }
                for si in s.items
            ],
        })
    return result

@app.delete("/api/sales/{sale_id}")
def delete_sale(sale_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    sale = db.query(Sale).filter(Sale.id == sale_id, Sale.user_id == current_user.id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="Sotuv topilmadi")
    try:
        db.query(SaleItem).filter(SaleItem.sale_id == sale_id).delete()
        db.delete(sale)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}

@app.put("/api/sales/items/{item_id}")
def update_sale_item(item_id: int, data: SaleItemUpdate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    if data.quantity <= 0:
        raise HTTPException(status_code=400, detail="Miqdor noldan katta bo'lishi kerak")
        
    item = db.query(SaleItem).filter(SaleItem.id == item_id, SaleItem.user_id == current_user.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Sotilgan mahsulot topilmadi")
        
    sale = item.sale
    product = item.product
    
    if not product:
        raise HTTPException(status_code=400, detail="O'chirilgan mahsulot miqdorini tahrirlab bo'lmaydi. Iltimos, faqat o'chirishni ishlating.")
    
    qty_diff = data.quantity - item.quantity
    
    # Check if there is enough stock if we are increasing quantity
    if qty_diff > 0 and product.stock < qty_diff:
        raise HTTPException(
            status_code=400,
            detail=f"Omborda yetarli mahsulot yo'q (Qoldiq: {product.stock}, kerakli qo'shimcha: {qty_diff})"
        )
        
    try:
        # Update stock
        product.stock -= qty_diff
        
        # Calculate amount and profit difference
        amount_diff = qty_diff * item.price
        profit_diff = qty_diff * (item.price - product.cost_price)
        
        # Update sale total and profit
        sale.total_amount = round(sale.total_amount + amount_diff, 2)
        sale.profit = round(sale.profit + profit_diff, 2)
        
        # Update item quantity
        item.quantity = data.quantity
        
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Xatolik yuz berdi: {str(e)}")
        
    return {"ok": True, "sale_id": sale.id}

@app.delete("/api/sales/items/{item_id}")
def delete_sale_item(item_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    item = db.query(SaleItem).filter(SaleItem.id == item_id, SaleItem.user_id == current_user.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Sotilgan mahsulot topilmadi")
        
    sale = item.sale
    product = item.product
    
    try:
        if product:
            # Return stock
            product.stock += item.quantity
            profit_to_sub = item.quantity * (item.price - product.cost_price)
        else:
            profit_to_sub = item.quantity * item.price
            
        # Subtract from sale total and profit
        amount_to_sub = item.quantity * item.price
        
        sale.total_amount = round(sale.total_amount - amount_to_sub, 2)
        sale.profit = round(sale.profit - profit_to_sub, 2)
        
        # Delete item
        db.delete(item)
        db.flush()
        
        # Check if sale has other items left
        remaining_items_count = db.query(SaleItem).filter(SaleItem.sale_id == sale.id).count()
        if remaining_items_count == 0:
            db.delete(sale)
            sale_deleted = True
        else:
            db.add(sale)
            sale_deleted = False
            
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Xatolik yuz berdi: {str(e)}")
        
    return {"ok": True, "sale_deleted": sale_deleted}

# --------------- REPORTS ---------------

@app.get("/api/reports/calendar")
def calendar_report(year: int, month: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    try:
        start_date = datetime.date(year, month, 1)
        if month == 12:
            end_date = datetime.date(year + 1, 1, 1)
        else:
            end_date = datetime.date(year, month + 1, 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Noto'g'ri yil yoki oy")

    sales_query = (
        db.query(
            func.date(Sale.created_at).label("day"),
            func.sum(Sale.total_amount).label("sales"),
            func.sum(Sale.profit).label("profit"),
            func.sum(Sale.cash_amount).label("cash_sales"),
            func.sum(Sale.card_amount).label("card_sales")
        )
        .filter(Sale.user_id == current_user.id).filter(Sale.created_at >= start_date)
        .filter(Sale.created_at < end_date)
        .group_by(func.date(Sale.created_at))
        .all()
    )

    expense_rows = (
        db.query(
            func.date(Expense.created_at).label("day"),
            func.sum(case((Expense.expense_type == 'naqd', Expense.amount), else_=0)).label("cash_amount"),
            func.sum(case((Expense.expense_type == 'karta', Expense.amount), else_=0)).label("card_amount"),
            func.sum(Expense.amount).label("amount")
        )
        .filter(Expense.user_id == current_user.id).filter(Expense.created_at >= start_date)
        .filter(Expense.created_at < end_date)
        .group_by(func.date(Expense.created_at))
        .all()
    )

    report = {}
    for r in sales_query:
        day_str = str(r.day)
        report[day_str] = {
            "sales": round(float(r.sales or 0), 2),
            "profit": round(float(r.profit or 0), 2),
            "expenses": 0.0,
            "revenue": 0.0,
            "cash_revenue": round(float(r.cash_sales or 0), 2),
            "card_revenue": round(float(r.card_sales or 0), 2)
        }

    for r in expense_rows:
        day_str = str(r.day)
        if day_str not in report:
            report[day_str] = {
                "sales": 0.0, "profit": 0.0, "expenses": 0.0, "revenue": 0.0,
                "cash_revenue": 0.0, "card_revenue": 0.0
            }
        
        cash_exp = float(r.cash_amount or 0)
        card_exp = float(r.card_amount or 0)
        expense_amt = float(r.amount or 0)
        
        report[day_str]["expenses"] = round(expense_amt, 2)
        report[day_str]["cash_revenue"] = round(report[day_str]["cash_revenue"] - cash_exp, 2)
        report[day_str]["card_revenue"] = round(report[day_str]["card_revenue"] - card_exp, 2)

    for day_str in report:
        report[day_str]["revenue"] = round(report[day_str]["cash_revenue"] + report[day_str]["card_revenue"], 2)

    owner_rows = (
        db.query(
            func.date(OwnerUsage.created_at).label("day"),
            func.sum(OwnerUsage.total_cost_price).label("cost")
        )
        .filter(OwnerUsage.user_id == current_user.id).filter(OwnerUsage.created_at >= start_date)
        .filter(OwnerUsage.created_at < end_date)
        .group_by(func.date(OwnerUsage.created_at))
        .all()
    )

    debt_rows = (
        db.query(
            func.date(Debt.created_at).label("day"),
            func.sum(Debt.profit).label("profit")
        )
        .filter(Debt.user_id == current_user.id).filter(Debt.created_at >= start_date)
        .filter(Debt.created_at < end_date)
        .group_by(func.date(Debt.created_at))
        .all()
    )

    for r in owner_rows:
        day_str = str(r.day)
        if day_str not in report:
            report[day_str] = {
                "sales": 0.0, "profit": 0.0, "expenses": 0.0, "revenue": 0.0,
                "cash_revenue": 0.0, "card_revenue": 0.0
            }
        report[day_str]["profit"] = round(report[day_str]["profit"] - float(r.cost or 0), 2)

    for r in debt_rows:
        day_str = str(r.day)
        if day_str not in report:
            report[day_str] = {
                "sales": 0.0, "profit": 0.0, "expenses": 0.0, "revenue": 0.0,
                "cash_revenue": 0.0, "card_revenue": 0.0
            }
        report[day_str]["profit"] = round(report[day_str]["profit"] + float(r.profit or 0), 2)

    return report

@app.get("/api/reports/insights")
def smart_insights(lang: str = "uz", db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    thirty_days_ago = datetime.datetime.now() - datetime.timedelta(days=30)

    # A) Low Stock Alert
    low_stock = db.query(Product).filter(Product.user_id == current_user.id, Product.stock < 5).order_by(Product.stock.asc()).all()
    low_stock_items = [{"id": p.id, "name": p.name, "stock": p.stock} for p in low_stock]

    # B) Top Selling
    best_sellers_raw = (
        db.query(
            Product.id,
            Product.name,
            func.sum(SaleItem.quantity).label("total_qty")
        )
        .join(SaleItem, Product.id == SaleItem.product_id)
        .filter(SaleItem.user_id == current_user.id, SaleItem.created_at >= thirty_days_ago)
        .group_by(Product.id)
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(5)
        .all()
    )
    best_sellers_items = [
        {"id": r.id, "name": r.name, "quantity_sold": int(r.total_qty or 0)}
        for r in best_sellers_raw
    ]

    # C) Slow Movers
    best_seller_ids = [b["id"] for b in best_sellers_items]
    slow_movers_raw = (
        db.query(
            Product.id,
            Product.name,
            func.sum(SaleItem.quantity).label("total_qty")
        )
        .join(SaleItem, Product.id == SaleItem.product_id)
        .filter(SaleItem.user_id == current_user.id, SaleItem.created_at >= thirty_days_ago)
        .filter(~Product.id.in_(best_seller_ids))
        .group_by(Product.id)
        .having(func.sum(SaleItem.quantity) <= 2)
        .order_by(func.sum(SaleItem.quantity).asc())
        .all()
    )
    slow_movers_items = [
        {"id": r.id, "name": r.name, "quantity_sold": int(r.total_qty or 0)}
        for r in slow_movers_raw
    ]

    # D) Dead Stock
    sold_product_ids = (
        db.query(SaleItem.product_id)
        .filter(SaleItem.user_id == current_user.id, SaleItem.created_at >= thirty_days_ago)
        .distinct()
    )
    dead_stock = (
        db.query(Product)
        .filter(Product.user_id == current_user.id, Product.stock > 0, ~Product.id.in_(sold_product_ids))
        .order_by(Product.stock.desc())
        .all()
    )
    dead_stock_items = [{"id": p.id, "name": p.name, "stock": p.stock} for p in dead_stock]

    # Translations for recommendations
    recommendations = {
        "uz": {
            "low_stock": "Ushbu mahsulot tez orada tugaydi. Yetkazib beruvchiga buyurtma berishni unutmang.",
            "best_sellers": "Ushbu mahsulot juda xaridorgir! Ombordagi qoldig'ini doimiy nazorat qiling va zaxirani ko'paytiring.",
            "slow_movers": "Sotuv tezligi past. Ushbu mahsulot uchun kichik reklama yoki qo'shimcha rag'bat joriy qilishni ko'rib chiqing.",
            "dead_stock": "Ushbu mahsulot omborda joy egallab turibdi. Pulni muzlatib qo'ymaslik uchun uni chegirma (aksiya) bilan tezroq naqd pulga aylantirishni tavsiya qilaman."
        },
        "ru": {
            "low_stock": "Этот товар скоро закончится. Не забудьте заказать его у поставщика.",
            "best_sellers": "Этот товар пользуется большим спросом! Постоянно контролируйте остаток на складе и увеличивайте запасы.",
            "slow_movers": "Низкая скорость продаж. Попробуйте запустить небольшую рекламу или дополнительное продвижение для этого товара.",
            "dead_stock": "Этот товар залежался на складе. Чтобы не замораживать средства, рекомендуем быстрее продать его с помощью скидки или акции."
        },
        "en": {
            "low_stock": "This product will run out soon. Don't forget to order from the supplier.",
            "best_sellers": "This product is highly popular! Keep a close eye on stock levels and increase inventory.",
            "slow_movers": "Sales volume is low. Consider running a small promotion or offering incentives for this product.",
            "dead_stock": "This item is sitting in warehouse. To avoid frozen capital, we recommend liquidating it quickly with discounts or promotions."
        }
    }

    selected_rec = recommendations.get(lang, recommendations["uz"])

    return {
        "low_stock": {
            "items": low_stock_items,
            "recommendation": selected_rec["low_stock"]
        },
        "best_sellers": {
            "items": best_sellers_items,
            "recommendation": selected_rec["best_sellers"]
        },
        "slow_movers": {
            "items": slow_movers_items,
            "recommendation": selected_rec["slow_movers"]
        },
        "dead_stock": {
            "items": dead_stock_items,
            "recommendation": selected_rec["dead_stock"]
        }
    }

# --------------- TELEGRAM NOTIFICATIONS ---------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_ID  = os.getenv("TELEGRAM_ADMIN_ID", "")

def send_telegram_message(text: str) -> bool:
    """
    Telegram API orqali xabar jo'natadi.
    Muvaffaqiyatli bo'lsa True, xato bo'lsa False qaytaradi.
    Hech qachon exception ko'tarmaydi — xato faqat log faylga yoziladi.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        logger.warning("Telegram sozlamalari (.env) to'liq emas. Xabar yuborilmadi.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_ADMIN_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            if res_data.get("ok"):
                logger.info("Telegram xabari muvaffaqiyatli yuborildi.")
                return True
            else:
                logger.error(f"Telegram API xatosi: {res_data}")
                return False
    except urllib.error.URLError as e:
        logger.error(f"Telegram: Internet ulanish xatosi — {e.reason}")
        return False
    except Exception as e:
        logger.error(f"Telegram: Kutilmagan xato — {e}")
        return False


# Eski nom bilan moslik uchun alias
def send_telegram_notification(text: str):
    send_telegram_message(text)


def build_morning_report(user_id: int, db) -> str:
    """
    Foydalanuvchi bazasini tahlil qilib, ertalabki hisobot matnini tuzadi.
    """
    thirty_days_ago = datetime.datetime.now() - datetime.timedelta(days=30)

    # Do'kon nomi
    user = db.query(User).filter(User.id == user_id).first()
    shop_name = user.shop_name if user else "Delta Sells"
    now_str = datetime.datetime.now().strftime("%d.%m.%Y")

    # ──────────────────────────────────────────
    # A) Kam qolgan mahsulotlar (stock < 5)
    # ──────────────────────────────────────────
    low_stock_prods = (
        db.query(Product)
        .filter(Product.user_id == user_id, Product.stock < 5)
        .order_by(Product.stock.asc())
        .all()
    )

    # ──────────────────────────────────────────
    # B) Top-5 sotilgan mahsulotlar (30 kun)
    # ──────────────────────────────────────────
    best_sellers = (
        db.query(
            Product.name,
            func.sum(SaleItem.quantity).label("total_qty")
        )
        .join(SaleItem, Product.id == SaleItem.product_id)
        .filter(SaleItem.user_id == user_id, SaleItem.created_at >= thirty_days_ago)
        .group_by(Product.id)
        .order_by(func.sum(SaleItem.quantity).desc())
        .limit(5)
        .all()
    )

    # ──────────────────────────────────────────
    # C) 30 kun mobaynida umuman sotilmaganlar
    # ──────────────────────────────────────────
    sold_ids_subq = (
        db.query(SaleItem.product_id)
        .filter(SaleItem.user_id == user_id, SaleItem.created_at >= thirty_days_ago)
        .distinct()
    )
    dead_stock_prods = (
        db.query(Product)
        .filter(Product.user_id == user_id, Product.stock > 0, ~Product.id.in_(sold_ids_subq))
        .order_by(Product.stock.desc())
        .all()
    )

    # ──────────────────────────────────────────
    # Xabar matni
    # ──────────────────────────────────────────
    lines = [
        f"🔔 <b>ERTALABKI AQLLI HISOBOT (Soat 09:00)</b> 🔔",
        f"🏪 <b>{html.escape(shop_name.upper())}</b>  |  📅 {now_str}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # ── A: Kam qolganlar ──
    lines.append("")
    lines.append("⚠️ <b>KAM QOLGAN MAHSULOTLAR:</b>")
    if low_stock_prods:
        for p in low_stock_prods:
            icon = "🔴" if p.stock == 0 else "🟡"
            lines.append(f"  {icon} {html.escape(p.name)} — omborda <b>{p.stock} dona</b> qoldi")
        lines.append("💡 <i>Tavsiya: Ushbu mahsulotlar tez orada tugaydi, ta'minotchiga buyurtma bering!</i>")
    else:
        lines.append("  ✅ Barcha mahsulotlar zaxirasi yetarli!")

    # ── B: Top sotuvlar ──
    lines.append("")
    lines.append("🔥 <b>ENG KO'P SOTILAYOTGANLAR (TOP-5):</b>")
    if best_sellers:
        for i, r in enumerate(best_sellers, 1):
            lines.append(f"  {i}. {html.escape(r.name)} — oxirgi 30 kunda <b>{int(r.total_qty or 0)} ta</b> sotildi")
        lines.append("💡 <i>Tavsiya: Ushbu mahsulotlar juda xaridorgir, zaxirasini doimiy to'ldirib turing.</i>")
    else:
        lines.append("  📊 Oxirgi 30 kunda sotuv qayd etilmagan.")

    # ── C: Muzlagan pullar ──
    lines.append("")
    lines.append("💤 <b>SOTILMAY TURGAN MAHSULOTLAR:</b>")
    if dead_stock_prods:
        for p in dead_stock_prods[:7]:
            lines.append(f"  💤 {html.escape(p.name)} — 30 kundan beri <b>0 sotuv</b>")
        if len(dead_stock_prods) > 7:
            lines.append(f"  ... va yana <b>{len(dead_stock_prods) - 7} ta</b> mahsulot")
        lines.append("💡 <i>Tavsiya: Pulni muzlatmaslik uchun ushbu tovarlarga chegirma e'lon qiling yoki almashtiring.</i>")
    else:
        lines.append("  🎉 Barcha mahsulotlar sotilmoqda!")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🤖 <i>Delta Sells CRM tizimi tomonidan avtomatik yuborildi.</i>")

    return "\n".join(lines)


def run_morning_report_for_user(user_id: int) -> bool:
    """
    Bitta foydalanuvchi uchun ertalabki hisobotni tayyorlab Telegramga yuboradi.
    Barcha xatolar try/except bilan ushlanadi — asosiy server ta'sirlanmaydi.
    """
    db = SessionLocal()
    try:
        logger.info(f"Ertalabki hisobot tayyorlanmoqda (user_id={user_id})...")
        message = build_morning_report(user_id, db)
        success = send_telegram_message(message)
        if success:
            logger.info(f"user_id={user_id} uchun Telegram hisobot yuborildi.")
            return True
        else:
            logger.warning(f"user_id={user_id} uchun Telegram hisobot yuborilmadi (yuqoridagi xatoga qarang).")
            return False
    except Exception as e:
        logger.error(f"run_morning_report_for_user xatosi (user_id={user_id}): {e}", exc_info=True)
        return False
    finally:
        db.close()


def run_insights_job(user_id: int):
    """Eski funksiya — run_morning_report_for_user ga yo'naltiriladi."""
    run_morning_report_for_user(user_id)


def daily_telegram_cron_job():
    """
    APScheduler tomonidan har kuni soat 09:00 da chaqiriladigan asosiy funksiya.
    Bazadagi barcha foydalanuvchilar uchun ertalabki hisobotni yuboradi.
    """
    logger.info("═══ ERTALABKI CRON JOB BOSHLANDI (09:00) ═══")
    db = SessionLocal()
    try:
        users = db.query(User).all()
        logger.info(f"Jami {len(users)} ta foydalanuvchi topildi.")
        for u in users:
            run_morning_report_for_user(u.id)
    except Exception as e:
        logger.error(f"daily_telegram_cron_job global xatosi: {e}", exc_info=True)
    finally:
        db.close()
    logger.info("═══ ERTALABKI CRON JOB TUGADI ═══")


# ──────────────────────────────────────────────────────────
#  APScheduler — Cron trigger: har kuni soat 09:00 (mahalliy vaqt)
# ──────────────────────────────────────────────────────────
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()
scheduler.add_job(
    daily_telegram_cron_job,
    trigger="cron",
    hour=9,
    minute=0,
    id="morning_insights_job",
    replace_existing=True,
    misfire_grace_time=300,   # 5 daqiqa kech qolsa ham ishlatadi
)

@app.on_event("startup")
def on_startup():
    try:
        db = SessionLocal()
        try:
            db.execute(text("ALTER TABLE sales ADD COLUMN note TEXT"))
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
    except Exception as e:
        logger.error(f"DB schema update error: {e}", exc_info=True)
        
    try:
        scheduler.start()
        logger.info("✅ APScheduler muvaffaqiyatli ishga tushdi. Har kuni soat 09:00 da xabar yuboriladi.")
    except Exception as e:
        logger.error(f"APScheduler ishga tushmadi: {e}", exc_info=True)

@app.on_event("shutdown")
def on_shutdown():
    try:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler to'xtatildi.")
    except Exception:
        pass


@app.post("/api/reports/insights/telegram")
def trigger_telegram_notification(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """
    Veb-panelidagi '✈️ Yuborish' tugmasi bosilganda bir martalik hisobot yuboradi.
    """
    try:
        success = run_morning_report_for_user(current_user.id)
        if success:
            return {"ok": True, "message": "Telegram hisoboti yuborildi"}
        else:
            raise HTTPException(
                status_code=500,
                detail="Telegramga xabar yuborishda xatolik yuz berdi. Sozlamalar yoki tarmoq ulanishini tekshiring."
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Manual Telegram trigger xatosi: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/expenses", response_model=ExpenseOut)
def create_expense(data: ExpenseIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    expense_created_at = data.created_at or datetime.datetime.now()
    if expense_created_at.tzinfo is not None:
        expense_created_at = expense_created_at.replace(tzinfo=None)
        
    exp_type = data.expense_type or "naqd"
    if exp_type not in ["naqd", "karta"]:
        raise HTTPException(status_code=400, detail="Noto'g'ri chiqim turi")

    expense = Expense(user_id=current_user.id, 
        title=data.title,
        amount=data.amount,
        expense_type=exp_type,
        created_at=expense_created_at
    )
    db.add(expense)
    db.commit()
    db.refresh(expense)
    return expense

@app.get("/api/expenses", response_model=List[ExpenseOut])
def list_expenses(date: Optional[str] = None, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    query = db.query(Expense).filter(Expense.user_id == current_user.id).order_by(Expense.created_at.desc())
    if date:
        query = query.filter(func.date(Expense.created_at) == date)
    return query.all()

@app.delete("/api/expenses/{expense_id}")
def delete_expense(expense_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    expense = db.query(Expense).filter(Expense.id == expense_id, Expense.user_id == current_user.id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Chiqim topilmadi")
    db.delete(expense)
    db.commit()
    return {"ok": True}

@app.post("/api/expenses/return")
def return_expense(data: ExpenseReturnIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    expense = db.query(Expense).filter(Expense.id == data.expense_id, Expense.user_id == current_user.id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Chiqim topilmadi")
    
    product = db.query(Product).filter(Product.id == data.product_id, Product.user_id == current_user.id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
    
    if product.stock < data.quantity:
        raise HTTPException(status_code=400, detail="Qaytarilayotgan miqdor ombor qoldig'idan ko'p bo'lishi mumkin emas")

    # Update product stock
    product.stock -= data.quantity
    
    # Update expense amount
    expense.amount -= data.refund_amount
    if expense.amount < 0:
        expense.amount = 0
        
    db.commit()
    return {"ok": True, "message": "Yuk qaytarildi"}

@app.put("/api/expenses/{expense_id}", response_model=ExpenseOut)
def update_expense(expense_id: int, data: ExpenseUpdateIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    expense = db.query(Expense).filter(Expense.id == expense_id, Expense.user_id == current_user.id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Chiqim topilmadi")
    
    exp_type = data.expense_type or "naqd"
    if exp_type not in ["naqd", "karta"]:
        raise HTTPException(status_code=400, detail="Noto'g'ri chiqim turi")

    expense.title = data.title
    expense.amount = data.amount
    expense.expense_type = exp_type
    if data.created_at:
        # Avoid overriding tzinfo
        if data.created_at.tzinfo is not None:
            expense.created_at = data.created_at.replace(tzinfo=None)
        else:
            expense.created_at = data.created_at

    db.commit()
    db.refresh(expense)
    return expense

@app.post("/api/expenses/exchange")
def exchange_expense(data: ExpenseExchangeIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    # Verify both products
    returned_product = db.query(Product).filter(Product.id == data.returned_product_id, Product.user_id == current_user.id).first()
    new_product = db.query(Product).filter(Product.id == data.new_product_id, Product.user_id == current_user.id).first()
    
    if not returned_product or not new_product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
        
    if returned_product.stock < data.returned_qty:
        raise HTTPException(status_code=400, detail="Qaytarilayotgan miqdor ombor qoldig'idan ko'p bo'lishi mumkin emas")
        
    # Decrement returned product, increment new product (supplier logic)
    returned_product.stock -= data.returned_qty
    new_product.stock += data.new_qty
    
    # If there's an additional payment, log it as an expense
    if data.additional_payment > 0:
        new_expense = Expense(
            user_id=current_user.id,
            title="Tovar almashtirish (Obmen) uchun qo'shimcha to'lov",
            amount=data.additional_payment,
            expense_type="naqd",
            created_at=datetime.datetime.now()
        )
        db.add(new_expense)
        
    db.commit()
    return {"ok": True, "message": "Mahsulotlar muvaffaqiyatli almashtirildi"}

# --------------- OWNER USAGES ---------------

@app.post("/api/owner-usages")
def create_owner_usage(data: OwnerUsageIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    if data.quantity <= 0:
        raise HTTPException(status_code=400, detail="Miqdor noldan katta bo'lishi kerak")
    
    product = db.query(Product).filter(Product.id == data.product_id, Product.user_id == current_user.id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
        
    if product.stock < data.quantity:
        raise HTTPException(
            status_code=400,
            detail=f"'{product.name}' uchun omborda yetarli emas (qoldiq: {product.stock})",
        )
        
    try:
        # Decrement product stock
        product.stock -= data.quantity
        
        # Calculate total cost price
        total_cost_price = round(product.cost_price * data.quantity, 2)
        
        # Create owner usage entry
        usage = OwnerUsage(
            user_id=current_user.id,
            product_id=product.id,
            quantity=data.quantity,
            total_cost_price=total_cost_price,
            created_at=datetime.datetime.now()
        )
        db.add(usage)
        db.commit()
        db.refresh(usage)
        return {
            "id": usage.id,
            "product_name": product.name,
            "quantity": usage.quantity,
            "total_cost_price": usage.total_cost_price,
            "created_at": usage.created_at.isoformat()
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Xatolik yuz berdi: {str(e)}")

@app.get("/api/owner-usages")
def list_owner_usages(date: Optional[str] = None, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    query = db.query(OwnerUsage).filter(OwnerUsage.user_id == current_user.id)
    if date:
        query = query.filter(func.date(OwnerUsage.created_at) == date)
    usages = query.order_by(OwnerUsage.created_at.desc()).all()
    
    result = []
    for u in usages:
        result.append({
            "id": u.id,
            "product_id": u.product_id,
            "product_name": u.product.name if u.product else "O'chirilgan mahsulot",
            "quantity": u.quantity,
            "total_cost_price": u.total_cost_price,
            "created_at": u.created_at.isoformat()
        })
    return result

@app.put("/api/owner-usages/{usage_id}")
def update_owner_usage(usage_id: int, data: OwnerUsageUpdateIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    if data.new_quantity <= 0:
        raise HTTPException(status_code=400, detail="Miqdor noldan katta bo'lishi kerak")
        
    usage = db.query(OwnerUsage).filter(OwnerUsage.id == usage_id, OwnerUsage.user_id == current_user.id).first()
    if not usage:
        raise HTTPException(status_code=404, detail="Yozuv topilmadi")
        
    # Check if we are changing the product
    if data.product_id == usage.product_id:
        product = db.query(Product).filter(Product.id == usage.product_id, Product.user_id == current_user.id).first()
        if not product:
            raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
            
        qty_diff = data.new_quantity - usage.quantity
        if qty_diff > 0 and product.stock < qty_diff:
            raise HTTPException(
                status_code=400,
                detail=f"Omborda yetarli mahsulot yo'q (Qoldiq: {product.stock}, qo'shimcha kerakli: {qty_diff})"
            )
            
        try:
            product.stock -= qty_diff
            usage.quantity = data.new_quantity
            usage.total_cost_price = round(product.cost_price * data.new_quantity, 2)
            db.commit()
            return {
                "id": usage.id,
                "product_name": product.name,
                "quantity": usage.quantity,
                "total_cost_price": usage.total_cost_price,
                "created_at": usage.created_at.isoformat()
            }
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Xatolik yuz berdi: {str(e)}")
    else:
        # We are switching products!
        old_product = db.query(Product).filter(Product.id == usage.product_id, Product.user_id == current_user.id).first()
        new_product = db.query(Product).filter(Product.id == data.product_id, Product.user_id == current_user.id).first()
        
        if not new_product:
            raise HTTPException(status_code=404, detail="Yangi mahsulot topilmadi")
            
        # Check stock on new product
        if new_product.stock < data.new_quantity:
            raise HTTPException(
                status_code=400,
                detail=f"Yangi mahsulot '{new_product.name}' uchun omborda yetarli emas (qoldiq: {new_product.stock})"
            )
            
        try:
            # Refund old product
            if old_product:
                old_product.stock += usage.quantity
                
            # Decrement new product
            new_product.stock -= data.new_quantity
            
            # Update usage fields
            usage.product_id = data.product_id
            usage.quantity = data.new_quantity
            usage.total_cost_price = round(new_product.cost_price * data.new_quantity, 2)
            
            db.commit()
            return {
                "id": usage.id,
                "product_name": new_product.name,
                "quantity": usage.quantity,
                "total_cost_price": usage.total_cost_price,
                "created_at": usage.created_at.isoformat()
            }
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Xatolik yuz berdi: {str(e)}")

@app.delete("/api/owner-usages/{usage_id}")
def delete_owner_usage(usage_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    usage = db.query(OwnerUsage).filter(OwnerUsage.id == usage_id, OwnerUsage.user_id == current_user.id).first()
    if not usage:
        raise HTTPException(status_code=404, detail="Yozuv topilmadi")
        
    product = db.query(Product).filter(Product.id == usage.product_id, Product.user_id == current_user.id).first()
    try:
        if product:
            product.stock += usage.quantity
        db.delete(usage)
        db.commit()
        return {"ok": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Xatolik yuz berdi: {str(e)}")

# --------------- INTERNAL TRANSFERS ---------------

@app.post("/api/transfers/card-to-cash")
def card_to_cash_transfer(data: InternalTransferIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="O'tkaziladigan summa noldan katta bo'lishi kerak")
    
    try:
        transfer = InternalTransfer(
            user_id=current_user.id,
            amount=data.amount,
            transfer_direction="card_to_cash",
            description=data.description,
            created_at=datetime.datetime.now()
        )
        db.add(transfer)
        db.commit()
        db.refresh(transfer)
        return {
            "ok": True,
            "id": transfer.id,
            "amount": transfer.amount,
            "transfer_direction": transfer.transfer_direction,
            "description": transfer.description,
            "created_at": transfer.created_at.isoformat()
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Xatolik yuz berdi: {str(e)}")

@app.post("/api/transfers/cash-to-card")
def cash_to_card_transfer(data: InternalTransferIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="O'tkaziladigan summa noldan katta bo'lishi kerak")
    
    try:
        transfer = InternalTransfer(
            user_id=current_user.id,
            amount=data.amount,
            transfer_direction="cash_to_card",
            description=data.description,
            created_at=datetime.datetime.now()
        )
        db.add(transfer)
        db.commit()
        db.refresh(transfer)
        return {
            "ok": True,
            "id": transfer.id,
            "amount": transfer.amount,
            "transfer_direction": transfer.transfer_direction,
            "description": transfer.description,
            "created_at": transfer.created_at.isoformat()
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Xatolik yuz berdi: {str(e)}")

# --------------- DEBTS ---------------

@app.post("/api/debts")
def create_debt(data: DebtIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    if data.quantity <= 0:
        raise HTTPException(status_code=400, detail="Miqdor noldan katta bo'lishi kerak")
    
    product = db.query(Product).filter(Product.id == data.product_id, Product.user_id == current_user.id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
        
    if product.stock < data.quantity:
        raise HTTPException(status_code=400, detail="Omborda yetarli mahsulot yo'q")
        
    profit = (product.price - product.cost_price) * data.quantity
    
    # Decrement stock
    product.stock -= data.quantity
    
    debt = Debt(
        user_id=current_user.id,
        customer_name=data.customer_name,
        product_id=product.id,
        quantity=data.quantity,
        debt_amount=data.debt_amount,
        profit=profit,
        status="to'lanmagan"
    )
    db.add(debt)
    db.commit()
    db.refresh(debt)
    return {"status": "ok", "id": debt.id}

@app.get("/api/debts")
def list_debts(date: Optional[str] = None, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    query = db.query(Debt).filter(Debt.user_id == current_user.id)
    if date:
        query = query.filter(func.date(Debt.created_at) == date)
    else:
        query = query.filter(Debt.status == "to'lanmagan")
    debts = query.order_by(Debt.created_at.desc()).all()
    result = []
    for d in debts:
        result.append({
            "id": d.id,
            "customer_name": d.customer_name,
            "product_id": d.product_id,
            "product_name": d.product.name if d.product else "O'chirilgan mahsulot",
            "quantity": d.quantity,
            "debt_amount": d.debt_amount,
            "profit": d.profit,
            "status": d.status,
            "payment_type": d.payment_type,
            "created_at": d.created_at.isoformat(),
            "paid_at": d.paid_at.isoformat() if d.paid_at else None
        })
    return result

@app.put("/api/debts/{debt_id}")
def update_debt(debt_id: int, data: DebtUpdateIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    debt = db.query(Debt).filter(Debt.id == debt_id, Debt.user_id == current_user.id).first()
    if not debt:
        raise HTTPException(status_code=404, detail="Qarz topilmadi")
    if debt.status == "to'landi":
        raise HTTPException(status_code=400, detail="To'langan qarzni tahrirlab bo'lmaydi")
        
    product = db.query(Product).filter(Product.id == debt.product_id, Product.user_id == current_user.id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
        
    # Revert old quantity
    product.stock += debt.quantity
    
    if product.stock < data.quantity:
        # put back the quantity we just added in memory
        product.stock -= debt.quantity
        raise HTTPException(status_code=400, detail="Omborda yetarli mahsulot yo'q")
        
    # Apply new quantity
    product.stock -= data.quantity
    
    debt.customer_name = data.customer_name
    debt.quantity = data.quantity
    debt.debt_amount = data.debt_amount
    debt.profit = (product.price - product.cost_price) * data.quantity
    
    db.commit()
    return {"status": "ok"}

@app.post("/api/debts/{debt_id}/pay")
def pay_debt(debt_id: int, data: DebtPayIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    debt = db.query(Debt).filter(Debt.id == debt_id, Debt.user_id == current_user.id).first()
    if not debt:
        raise HTTPException(status_code=404, detail="Qarz topilmadi")
    if debt.status == "to'landi":
        raise HTTPException(status_code=400, detail="Bu qarz allaqachon to'langan")
        
    if data.payment_type not in ["naqd", "karta"]:
        raise HTTPException(status_code=400, detail="Noto'g'ri to'lov turi")
        
    debt.status = "to'landi"
    debt.payment_type = data.payment_type
    debt.paid_at = datetime.datetime.utcnow()
    
    # Create Sale and SaleItem with profit=0
    sale = Sale(
        user_id=current_user.id,
        total_amount=debt.debt_amount,
        profit=0.0,
        payment_type=data.payment_type,
        cash_amount=debt.debt_amount if data.payment_type == 'naqd' else 0.0,
        card_amount=debt.debt_amount if data.payment_type == 'karta' else 0.0,
        note=f"Qarz hisobidan to'lov ({debt.customer_name})"
    )
    db.add(sale)
    db.flush()
    
    item = SaleItem(
        user_id=current_user.id,
        sale_id=sale.id,
        product_id=debt.product_id,
        quantity=debt.quantity,
        price=debt.debt_amount / debt.quantity if debt.quantity > 0 else 0.0
    )
    db.add(item)
    
    db.commit()
    return {"status": "ok"}

# --------------- SETTINGS ---------------

@app.get("/api/settings", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    settings = db.query(Settings).filter(Settings.user_id == current_user.id).first()
    if not settings:
        settings = Settings(user_id=current_user.id)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings

@app.post("/api/settings")
def update_settings(data: SettingsIn, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    settings = db.query(Settings).filter(Settings.user_id == current_user.id).first()
    if not settings:
        settings = Settings(user_id=current_user.id)
        db.add(settings)
    
    settings.language = data.language
    settings.theme = data.theme
    settings.zoom = data.zoom
    db.commit()
    return {"status": "ok"}

# --------------- Serve Frontend ---------------

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
os.makedirs(os.path.join(FRONTEND_DIR, "locales"), exist_ok=True)

app.mount("/locales", StaticFiles(directory=os.path.join(FRONTEND_DIR, "locales")), name="locales")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
