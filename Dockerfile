FROM python:3.12-slim

# System basics
RUN pip install --no-cache-dir --upgrade pip

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Default envs (override in Coolify / docker run)
ENV EXCHANGE_URL="https://owa.wefa.com/EWS/Exchange.asmx"
ENV EXCHANGE_USER="WEFASINGEN\\obchodcz"
ENV EXCHANGE_PASSWORD="change_me"
ENV MAILBOX="obchodcz@wefa.com"
ENV PORT=8000

EXPOSE 8000

CMD ["python", "app.py"]
