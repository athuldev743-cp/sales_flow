"""
SalesFlow Lead Importer - Optimized for 1M+ Rows
Handles Excel/CSV imports with specialized SQLite memory buffering to avoid 'SQL Logic Error'.
"""
from leads.lead_db import Lead, SessionLocal, engine, init_db
from sqlalchemy import text
import pandas as pd
import sys
import os
import argparse
import time

# Ensure the parent directory is in the path so we can find 'leads.lead_db'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


DEFAULT_FILE = os.path.join(os.path.dirname(
    os.path.dirname(__file__)), "data.xlsx")
BATCH_SIZE = 5000  # Increased for better performance on 1M+ rows


def clean(val, maxlen=500) -> str:
    if val is None:
        return None
    s = str(val).strip()
    if s.lower() in ('nan', 'none', ''):
        return None
    return s[:maxlen]


def detect_columns(df) -> dict:
    mapping = {}
    for col in df.columns:
        c = col.strip().upper().replace('.', '').replace(' ', '')
        if 'SNO' in c or ('S' in c and 'NO' in c):
            mapping['serial_no'] = col
        elif 'COMPANY' in c:
            mapping['company_name'] = col
        elif 'CONTACT' in c:
            mapping['contact_name'] = col
        elif 'ADD' in c or 'ADDRESS' in c:
            mapping['address'] = col
        elif 'CITY' in c:
            mapping['city'] = col
        elif 'STATE' in c:
            mapping['state'] = col
        elif 'MOBILE' in c:
            mapping['mobile'] = col
        elif 'PHONE' in c:
            mapping['phone'] = col
        elif 'EMAIL' in c:
            mapping['email'] = col
        elif 'WEBSITE' in c or 'WEB' in c:
            mapping['website'] = col
        elif 'BUSINESS' in c or 'DETAIL' in c:
            mapping['business_details'] = col
    return mapping


def import_file(filepath: str, batch_size: int = BATCH_SIZE):
    print(f"\n{'='*55}")
    print(f"   SalesFlow Lead Importer (1M+ Row Edition)")
    print(f"{'='*55}")
    print(f"   File : {filepath}")
    print(f"   Batch: {batch_size:,} rows per commit")
    print(f"{'='*55}\n")

    if not os.path.exists(filepath):
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)

    # ── Step 1: Load ──
    t0 = time.time()
    print("Step 1/5: Loading file into memory...")
    ext = os.path.splitext(filepath)[1].lower()
    if ext in ['.xlsx', '.xls']:
        df = pd.read_excel(filepath, dtype=str)
    elif ext == '.csv':
        df = pd.read_csv(filepath, dtype=str,
                         encoding='utf-8', errors='replace')
    else:
        print(f"ERROR: Unsupported format '{ext}'. Use .xlsx or .csv")
        sys.exit(1)

    total = len(df)
    print(f"   Loaded {total:,} rows in {time.time()-t0:.1f}s")

    # ── Step 2: Column detection ──
    print("\nStep 2/5: Detecting columns...")
    col_map = detect_columns(df)
    for field, col in col_map.items():
        print(f"   {field:20} ← '{col}'")

    # ── Step 3: Init DB ──
    print("\nStep 3/5: Initialising database...")
    init_db()
    db = SessionLocal()
    existing = db.query(Lead).count()
    print(f"   Existing leads: {existing:,}")

    if existing > 0:
        ans = input(
            f"\n   DB already has {existing:,} leads. Append? [y/N]: ").strip().lower()
        if ans != 'y':
            print("   Proceeding to FTS check only...")
            db.close()
        else:
            # ── Step 4: Import ──
            print(f"\nStep 4/5: Importing {total:,} rows...")
            t1 = time.time()
            imported = 0
            batch = []

            for idx, row in df.iterrows():
                try:
                    lead = Lead(
                        serial_no=int(float(row[col_map['serial_no']])) if 'serial_no' in col_map and clean(
                            row.get(col_map['serial_no'])) else None,
                        company_name=clean(
                            row.get(col_map.get('company_name')), 500),
                        contact_name=clean(
                            row.get(col_map.get('contact_name')), 300),
                        address=clean(row.get(col_map.get('address')), 1000),
                        city=clean(row.get(col_map.get('city')), 150),
                        state=clean(row.get(col_map.get('state')), 150),
                        mobile=clean(row.get(col_map.get('mobile')), 100),
                        phone=clean(row.get(col_map.get('phone')), 100),
                        email=clean(row.get(col_map.get('email')), 500),
                        website=clean(row.get(col_map.get('website')), 500),
                        business_details=clean(
                            row.get(col_map.get('business_details')), 2000),
                        status="new",
                        source="platform",
                    )
                    batch.append(lead)
                except:
                    continue

                if len(batch) >= batch_size:
                    db.bulk_save_objects(batch)
                    db.commit()
                    imported += len(batch)
                    batch = []
                    rate = imported / (time.time() - t1)
                    print(
                        f"   [{(imported/total)*100:5.1f}%] {imported:>8,} / {total:,}  |  {rate:,.0f} rows/s", end='\r')

            if batch:
                db.bulk_save_objects(batch)
                db.commit()
            db.close()

    # ── Step 5: FTS REBUILD (FIXED) ──
    # ── Step 5: FTS REBUILD (Nuclear Fix for 1M+ Rows) ────────────────────────
    print(f"\n\nStep 5/5: Building Full-Text Search index (FTS5)...")
    t_fts = time.time()
    with engine.connect() as conn:
        # Optimization settings
        conn.execute(text("PRAGMA cache_size = -2000000;"))
        conn.execute(text("PRAGMA temp_store = MEMORY;"))

        print("   Dropping old virtual table to reset index...")
        conn.execute(text("DROP TABLE IF EXISTS leads_fts;"))

        print("   Re-creating virtual table structure...")
        conn.execute(text("""
            CREATE VIRTUAL TABLE leads_fts USING fts5(
                lead_id UNINDEXED,
                company_name,
                contact_name,
                city,
                state,
                business_details,
                content='leads',
                content_rowid='id',
                tokenize='porter unicode61'
            );
        """))

        print("   Populating search index (this will take 1-2 minutes)...")
        conn.execute(text("""
            INSERT INTO leads_fts(lead_id, company_name, contact_name, city, state, business_details)
            SELECT id, company_name, contact_name, city, state, business_details FROM leads;
        """))

        conn.commit()

    print(f"   ✅ FTS index built successfully in {time.time()-t_fts:.1f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=DEFAULT_FILE)
    parser.add_argument("--batch", default=BATCH_SIZE, type=int)
    args = parser.parse_args()
    import_file(args.file, args.batch)
