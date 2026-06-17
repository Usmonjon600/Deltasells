from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from .database import Base
import datetime

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    pincode = Column(String, nullable=True)
    shop_name = Column(String, nullable=False)
    
    products = relationship("Product", back_populates="user")
    sales = relationship("Sale", back_populates="user")
    expenses = relationship("Expense", back_populates="user")
    owner_usages = relationship("OwnerUsage", back_populates="user")
    transfers = relationship("InternalTransfer", back_populates="user")
    debts = relationship("Debt", back_populates="user")
    settings = relationship("Settings", back_populates="user", uselist=False)

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, default=1)
    name = Column(String, nullable=False)
    price = Column(Float, nullable=False)  # selling price
    cost_price = Column(Float, nullable=False)  # purchase cost
    stock = Column(Integer, default=0)
    barcode = Column(String, nullable=True, unique=True)
    image_url = Column(String, nullable=True)
    user = relationship("User", back_populates="products")
    sale_items = relationship("SaleItem", back_populates="product")
    owner_usages = relationship("OwnerUsage", back_populates="product")

class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, default=1)
    total_amount = Column(Float, nullable=False)
    profit = Column(Float, nullable=False)
    payment_type = Column(String, nullable=False, default="naqd")
    cash_amount = Column(Float, nullable=False, default=0.0)
    card_amount = Column(Float, nullable=False, default=0.0)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    user = relationship("User", back_populates="sales")
    items = relationship("SaleItem", back_populates="sale")

class SaleItem(Base):
    __tablename__ = "sale_items"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, default=1)
    sale_id = Column(Integer, ForeignKey("sales.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)  # price per unit at time of sale
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    sale = relationship("Sale", back_populates="items")
    product = relationship("Product", back_populates="sale_items")

class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, default=1)
    title = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    expense_type = Column(String, nullable=False, default="naqd")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    user = relationship("User", back_populates="expenses")

class OwnerUsage(Base):
    __tablename__ = "owner_usages"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, default=1)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    total_cost_price = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", back_populates="owner_usages")
    product = relationship("Product", back_populates="owner_usages")

class InternalTransfer(Base):
    __tablename__ = "internal_transfers"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, default=1)
    amount = Column(Float, nullable=False)
    transfer_direction = Column(String, nullable=False, default="card_to_cash")
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", back_populates="transfers")

class Debt(Base):
    __tablename__ = "debts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, default=1)
    customer_name = Column(String, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    debt_amount = Column(Float, nullable=False)
    profit = Column(Float, nullable=False, default=0.0)
    status = Column(String, nullable=False, default="to'lanmagan")
    payment_type = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)
    
    user = relationship("User")
    product = relationship("Product")

class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    language = Column(String, default="uz")
    theme = Column(String, default="dark")
    zoom = Column(String, default="100")
    
    user = relationship("User", back_populates="settings")
