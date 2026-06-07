FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p storage/instances static/uploads

EXPOSE $PORT

CMD gunicorn __init__:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
