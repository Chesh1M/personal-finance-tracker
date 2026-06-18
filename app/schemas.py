from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


# --- Category ---

class CategoryBase(BaseModel):
    name: str
    display_name: str
    is_transfer: bool = False

class CategoryCreate(CategoryBase):
    pass

class CategoryRead(CategoryBase):
    id: int
    class Config:
        from_attributes = True


# --- StatementUpload ---

class StatementUploadBase(BaseModel):
    filename: str
    bank_source: str
    status: str = "pending"

class StatementUploadCreate(StatementUploadBase):
    raw_text: Optional[str] = None

class StatementUploadRead(StatementUploadBase):
    id: int
    upload_date: datetime
    class Config:
        from_attributes = True


# --- Transaction ---

class TransactionBase(BaseModel):
    date: date
    description: str
    amount: float
    type: str
    is_transfer: bool = False
    account_type: Optional[str] = None
    is_reviewed: bool = False

class TransactionCreate(TransactionBase):
    statement_id: Optional[int] = None
    category_id: Optional[int] = None
    hash: str

class TransactionRead(TransactionBase):
    id: int
    statement_id: Optional[int]
    category_id: Optional[int]
    hash: str
    class Config:
        from_attributes = True


# --- PortfolioPosition ---

class PortfolioPositionBase(BaseModel):
    ticker: str
    description: Optional[str] = None
    quantity: float
    avg_cost_price: float
    cost_basis: float
    currency: str = "USD"
    last_synced_date: Optional[date] = None

class PortfolioPositionCreate(PortfolioPositionBase):
    pass

class PortfolioPositionRead(PortfolioPositionBase):
    id: int
    class Config:
        from_attributes = True


# --- TradeLog ---

class TradeLogBase(BaseModel):
    ticker: str
    trade_type: str
    quantity: float
    price: float
    date: date
    currency: str = "USD"
    notes: Optional[str] = None

class TradeLogCreate(TradeLogBase):
    pass

class TradeLogRead(TradeLogBase):
    id: int
    class Config:
        from_attributes = True
