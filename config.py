import os
from dotenv import load_dotenv

load_dotenv()

DB_URI = os.getenv("DATABASE_URL")

if not DB_URI:
    raise ValueError("DATABASE_URL set nahi hai. Local .env ya GitHub Secret check karein.")
