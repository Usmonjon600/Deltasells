"""
Delta Sells — CRM / Kassa tizimini ishga tushirish.

Foydalanish:
    python run.py

Brauzerda ochiladi:  http://localhost:8000
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
