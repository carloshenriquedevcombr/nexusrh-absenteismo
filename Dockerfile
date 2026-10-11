FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ABSENTEISMO_DATA_DIR=/app/dados

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/dados \
    && chown -R app:app /app
USER app

EXPOSE 8501

# --preload: a inicialização (schema do banco, criação das chaves) roda uma
# única vez antes de abrir os workers, sem corrida entre eles.
CMD ["gunicorn", "--preload", "--workers", "2", "--bind", "0.0.0.0:8501", "--access-logfile", "-", "app:app"]
