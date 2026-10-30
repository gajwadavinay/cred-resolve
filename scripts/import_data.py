"""Script to import data into the database."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

db_path = os.path.join("data", "processed", "smartdialer.db")
if os.path.exists(db_path):
    os.remove(db_path)
    print("Removed old DB")

from app.database import Base, engine
from app import models  # noqa: F401 — must be imported so tables register on Base.metadata
Base.metadata.create_all(bind=engine)
print("Tables created")

from app.data_ingestion import run_full_ingestion
result = run_full_ingestion()
for k, v in result.items():
    ins = v.get("inserted", 0)
    skp = v.get("skipped", 0)
    err = v.get("errors", 0)
    print(f"  {k}: inserted={ins}, skipped={skp}, errors={err}")

from app.database import get_session
from app.data_cleaning import compute_account_eligibility
db = get_session()
elig = compute_account_eligibility(db)
db.close()
print(f"Eligibility: {elig}")
print("IMPORT COMPLETE")
