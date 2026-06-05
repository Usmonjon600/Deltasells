from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import List, Optional
import datetime, os, threading, time, urllib.request, urllib.parse, json, html

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
from .models import Product, Sale, SaleItem

# --------------- Create tables ---------------
Base.metadata.create_all(bind=engine)

# --------------- App ---------------
app = FastAPI(title="Delta Sells – CRM / Kassa")

# --------------- Pydantic Schemas ---------------

class ProductIn(BaseModel):
    id: Optional[int] = None
    name: str
    price: float
    cost_price: float
    stock: int = 0
    barcode: Optional[str] = None

class ProductOut(BaseModel):
    id: int
    name: str
    price: float
    cost_price: float
    stock: int
    barcode: Optional[str] = None
    class Config:
        from_attributes = True

class SaleItemIn(BaseModel):
    product_id: int
    quantity: int

class SaleIn(BaseModel):
    items: List[SaleItemIn]
    created_at: Optional[datetime.datetime] = None

class DashboardOut(BaseModel):
    today_revenue: float
    today_profit: float
    month_revenue: float
    month_profit: float

# --------------- PRODUCTS ---------------

@app.get("/api/products", response_model=List[ProductOut])
def list_products(q: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Product)
    if q:
        search = f"%{q}%"
        query = query.filter(
            (Product.name.ilike(search)) | (Product.barcode.ilike(search))
        )
    return query.order_by(Product.name).all()


@app.post("/api/products", response_model=ProductOut)
def create_or_update_product(data: ProductIn, db: Session = Depends(get_db)):
    if data.id:
        product = db.query(Product).get(data.id)
        if not product:
            raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
        product.name = data.name
        product.price = data.price
        product.cost_price = data.cost_price
        product.stock = data.stock
        product.barcode = data.barcode or None
    else:
        product = Product(
            name=data.name,
            price=data.price,
            cost_price=data.cost_price,
            stock=data.stock,
            barcode=data.barcode or None,
        )
        db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.delete("/api/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
    try:
        # Avval bog'langan sotuv yozuvlarini o'chiramiz
        db.query(SaleItem).filter(SaleItem.product_id == product_id).delete()
        db.delete(product)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"O'chirishda xatolik: {str(e)}")
    return {"ok": True}

# --------------- SALES ---------------

@app.post("/api/sales")
def create_sale(data: SaleIn, db: Session = Depends(get_db)):
    if not data.items:
        raise HTTPException(status_code=400, detail="Savat bo'sh")

    total_amount = 0.0
    total_profit = 0.0
    sale_items: list[SaleItem] = []

    # Get the sale creation time: if data.created_at is provided, use it. Otherwise current local time.
    sale_created_at = data.created_at or datetime.datetime.now()

    for item in data.items:
        product = db.query(Product).get(item.product_id)
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
            SaleItem(product_id=product.id, quantity=item.quantity, price=product.price, created_at=sale_created_at)
        )

    sale = Sale(total_amount=round(total_amount, 2), profit=round(total_profit, 2), created_at=sale_created_at)
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
        "created_at": sale.created_at.isoformat(),
    }

# --------------- DASHBOARD ---------------

@app.get("/api/dashboard", response_model=DashboardOut)
def dashboard(db: Session = Depends(get_db)):
    now = datetime.datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def agg(start):
        row = (
            db.query(
                func.coalesce(func.sum(Sale.total_amount), 0),
                func.coalesce(func.sum(Sale.profit), 0),
            )
            .filter(Sale.created_at >= start)
            .first()
        )
        return float(row[0]), float(row[1])

    today_rev, today_prof = agg(today_start)
    month_rev, month_prof = agg(month_start)
    return DashboardOut(
        today_revenue=round(today_rev, 2),
        today_profit=round(today_prof, 2),
        month_revenue=round(month_rev, 2),
        month_profit=round(month_prof, 2),
    )

# --------------- SALES HISTORY ---------------

@app.get("/api/sales")
def list_sales(date: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Sale).order_by(Sale.created_at.desc())
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
            "created_at": s.created_at.isoformat(),
            "items": [
                {
                    "product_name": si.product.name,
                    "quantity": si.quantity,
                    "price": si.price,
                }
                for si in s.items
            ],
        })
    return result

# --------------- REPORTS ---------------

@app.get("/api/reports/calendar")
def calendar_report(year: int, month: int, db: Session = Depends(get_db)):
    try:
        start_date = datetime.date(year, month, 1)
        if month == 12:
            end_date = datetime.date(year + 1, 1, 1)
        else:
            end_date = datetime.date(year, month + 1, 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Noto'g'ri yil yoki oy")

    rows = (
        db.query(
            func.date(Sale.created_at).label("day"),
            func.sum(Sale.total_amount).label("sales"),
            func.sum(Sale.profit).label("profit")
        )
        .filter(Sale.created_at >= start_date)
        .filter(Sale.created_at < end_date)
        .group_by(func.date(Sale.created_at))
        .all()
    )

    report = {}
    for r in rows:
        report[str(r.day)] = {
            "sales": float(r.sales or 0),
            "profit": float(r.profit or 0)
        }
    return report

@app.get("/api/reports/insights")
def smart_insights(db: Session = Depends(get_db)):
    thirty_days_ago = datetime.datetime.now() - datetime.timedelta(days=30)

    # A) Low Stock Alert
    low_stock = db.query(Product).filter(Product.stock < 5).order_by(Product.stock.asc()).all()
    low_stock_items = [{"id": p.id, "name": p.name, "stock": p.stock} for p in low_stock]

    # B) Top Selling
    best_sellers_raw = (
        db.query(
            Product.id,
            Product.name,
            func.sum(SaleItem.quantity).label("total_qty")
        )
        .join(SaleItem, Product.id == SaleItem.product_id)
        .filter(SaleItem.created_at >= thirty_days_ago)
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
    slow_movers_raw = (
        db.query(
            Product.id,
            Product.name,
            func.sum(SaleItem.quantity).label("total_qty")
        )
        .join(SaleItem, Product.id == SaleItem.product_id)
        .filter(SaleItem.created_at >= thirty_days_ago)
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
        .filter(SaleItem.created_at >= thirty_days_ago)
        .distinct()
    )
    dead_stock = (
        db.query(Product)
        .filter(~Product.id.in_(sold_product_ids))
        .order_by(Product.stock.desc())
        .all()
    )
    dead_stock_items = [{"id": p.id, "name": p.name, "stock": p.stock} for p in dead_stock]

    return {
        "low_stock": {
            "items": low_stock_items,
            "recommendation": "Ushbu mahsulot tez orada tugaydi. Yetkazib beruvchiga buyurtma berishni unutmang."
        },
        "best_sellers": {
            "items": best_sellers_items,
            "recommendation": "Ushbu mahsulot juda xaridorgir! Ombordagi qoldig'ini doimiy nazorat qiling va zaxirani ko'paytiring."
        },
        "slow_movers": {
            "items": slow_movers_items,
            "recommendation": "Sotuv tezligi past. Ushbu mahsulot uchun kichik reklama yoki qo'shimcha rag'bat joriy qilishni ko'rib chiqing."
        },
        "dead_stock": {
            "items": dead_stock_items,
            "recommendation": "Ushbu mahsulot omborda joy egallab turibdi. Pulni muzlatib qo'ymaslik uchun uni chegirma (aksiya) bilan tezroq naqd pulga aylantirishni tavsiya qilaman."
        }
    }

# --------------- TELEGRAM NOTIFICATIONS ---------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8862987031:AAHSpHcsyin9UUGx2tR4J8ln9c9vMLdlZl4")
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "1355229966")

def send_telegram_notification(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        print("Telegram bot credentials not configured.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_ADMIN_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            if not res_data.get("ok"):
                print("Telegram API returned error:", res_data)
    except Exception as e:
        print("Failed to send Telegram notification:", str(e))

def run_insights_job():
    db = SessionLocal()
    try:
        thirty_days_ago = datetime.datetime.now() - datetime.timedelta(days=30)
        
        # A) Low Stock Alert
        low_stock = db.query(Product).filter(Product.stock < 5).order_by(Product.stock.asc()).all()
        
        # B) Top Selling
        best_sellers_raw = (
            db.query(
                Product.id,
                Product.name,
                func.sum(SaleItem.quantity).label("total_qty")
            )
            .join(SaleItem, Product.id == SaleItem.product_id)
            .filter(SaleItem.created_at >= thirty_days_ago)
            .group_by(Product.id)
            .order_by(func.sum(SaleItem.quantity).desc())
            .limit(5)
            .all()
        )
        
        # C) Slow Movers
        slow_movers_raw = (
            db.query(
                Product.id,
                Product.name,
                func.sum(SaleItem.quantity).label("total_qty")
            )
            .join(SaleItem, Product.id == SaleItem.product_id)
            .filter(SaleItem.created_at >= thirty_days_ago)
            .group_by(Product.id)
            .having(func.sum(SaleItem.quantity) <= 2)
            .order_by(func.sum(SaleItem.quantity).asc())
            .all()
        )
        
        # D) Dead Stock
        sold_product_ids = (
            db.query(SaleItem.product_id)
            .filter(SaleItem.created_at >= thirty_days_ago)
            .distinct()
        )
        dead_stock = (
            db.query(Product)
            .filter(~Product.id.in_(sold_product_ids))
            .order_by(Product.stock.desc())
            .all()
        )
        
        # Format Telegram message
        lines = ["<b>🔔 DOʻKONINGIZDAN AQLLI BILDIRISHNOMA 🔔</b>\n"]
        
        # Low Stock
        if low_stock:
            lines.append("⚠️ <b>KAM QOLGAN MAHSULOTLAR:</b>")
            for p in low_stock:
                lines.append(f"- {html.escape(p.name)} ({p.stock} dona qoldi)")
            lines.append("💡 <i>Tavsiya: Tez orada tugaydi, buyurtma bering.</i>\n")
        else:
            lines.append("⚠️ <b>KAM QOLGAN MAHSULOTLAR:</b>")
            lines.append("Barcha mahsulotlar zaxirasi yetarli! 🌟\n")
            
        # Best Sellers
        if best_sellers_raw:
            lines.append("🔥 <b>TOP SOTILAYOTGANLAR (30 KUN):</b>")
            for r in best_sellers_raw:
                lines.append(f"- {html.escape(r.name)} (Oxirgi 30 kunda {int(r.total_qty)} ta sotildi)")
            lines.append("💡 <i>Tavsiya: Zaxirani koʻpaytiring!</i>\n")
            
        # Slow Movers
        if slow_movers_raw:
            lines.append("📉 <b>SUST SOTILAYOTGANLAR (30 KUN):</b>")
            for r in slow_movers_raw:
                lines.append(f"- {html.escape(r.name)} (Oxirgi 30 kunda {int(r.total_qty)} ta sotildi)")
            lines.append("💡 <i>Tavsiya: Sotuv tezligi past. Reklama yoki chegirma joriy etishni koʻrib chiqing.</i>\n")

        # Dead Stock
        if dead_stock:
            lines.append("💤 <b>UMUMAN SOTILMAYOTGANLAR (30 KUN):</b>")
            for p in dead_stock[:5]:
                lines.append(f"- {html.escape(p.name)} (30 kundan beri 0 sotuv, qoldiq: {p.stock} dona)")
            if len(dead_stock) > 5:
                lines.append(f"... va yana {len(dead_stock) - 5} ta mahsulot")
            lines.append("💡 <i>Tavsiya: Pulni muzlatmaslik uchun chegirma bilan soting.</i>\n")
            
        msg = "\n".join(lines)
        send_telegram_notification(msg)
    except Exception as e:
        print("Error in run_insights_job:", str(e))
    finally:
        db.close()

from apscheduler.schedulers.background import BackgroundScheduler

def check_stock_and_send_report():
    try:
        run_insights_job()
    except Exception as e:
        print("Scheduler job error:", e)

scheduler = BackgroundScheduler()
scheduler.add_job(check_stock_and_send_report, 'cron', hour=9, minute=0)

@app.on_event("startup")
def on_startup():
    scheduler.start()
    print("APScheduler started and configured to run daily at 09:00 AM.")

@app.post("/api/reports/insights/telegram")
def trigger_telegram_notification(db: Session = Depends(get_db)):
    try:
        run_insights_job()
        return {"ok": True, "message": "Telegram xabari yuborildi"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --------------- Serve Frontend ---------------

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
