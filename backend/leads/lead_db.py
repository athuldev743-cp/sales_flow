from sqlalchemy import (
    create_engine, Column, Integer, String, Text,
    DateTime, Index, event, text
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./salesflow_leads.db")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=100000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA mmap_size=268435456")
        cursor.close()
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    serial_no = Column(Integer, nullable=True)
    company_name = Column(String(500), nullable=True)
    contact_name = Column(String(300), nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String(150), nullable=True)
    state = Column(String(150), nullable=True)
    mobile = Column(String(100), nullable=True)
    phone = Column(String(100), nullable=True)
    email = Column(Text, nullable=True)
    website = Column(String(500), nullable=True)
    business_details = Column(Text, nullable=True)
    
    # 🚨 CRITICAL: Add this line! This is why it's not loading.
    business_group = Column(String(200), nullable=True) 
    
    status = Column(String(50), default="new")
    source = Column(String(100), default="platform")
    imported_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_city", "city"),
        Index("idx_state", "state"),
        Index("idx_status", "status"),
        # 🚨 Add this index for the 1M+ search speed
        Index("idx_business_group", "business_group"), 
    )


def init_db():
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        # 1. Added business_group to the FTS table
        conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS leads_fts 
            USING fts5(
                lead_id UNINDEXED,
                company_name,
                contact_name,
                city,
                state,
                business_details,
                business_group, -- NEW
                tokenize='porter unicode61'
            )
        """))

        # 2. Updated Trigger to include business_group
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS leads_fts_insert
            AFTER INSERT ON leads BEGIN
                INSERT INTO leads_fts(
                    lead_id, company_name, contact_name,
                    city, state, business_details, business_group -- NEW
                )
                VALUES (
                    new.id, new.company_name, new.contact_name,
                    new.city, new.state, new.business_details, new.business_group -- NEW
                );
            END
        """))
        conn.commit()


def get_lead_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
