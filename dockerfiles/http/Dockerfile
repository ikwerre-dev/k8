FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y lz4 tar && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 2006

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "2006"]