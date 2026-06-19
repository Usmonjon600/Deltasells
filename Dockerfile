# Python 3.11 asosidagi yengil imijdan foydalanamiz
FROM python:3.11-slim

# Ishchi papkani belgilaymiz
WORKDIR /app

# Pythonga byte-code yaratmaslikni va kiritish/chiqarishni keshlamaslikni aytamiz (loglar toza chiqishi uchun)
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Zaruriy kutubxonalar ro'yxatini ko'chiramiz va o'rnatamiz
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Loyiha fayllarini to'liq imij ichiga ko'chiramiz
COPY . .

# Tashqariga ochiladigan portni belgilaymiz
EXPOSE 8000

# Ilovani ishga tushirish buyrug'i
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--forwarded-allow-ips", "*"]
