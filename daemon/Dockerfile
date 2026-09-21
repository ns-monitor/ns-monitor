FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libpng-dev \
    libfreetype6-dev \
    libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Set env file
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "-u", "main.py"]
