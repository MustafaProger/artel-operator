FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium
COPY operator_app ./operator_app
COPY operators ./operators
ENV OPERATOR_DATA_DIR=/app/data
EXPOSE 8790
CMD ["python", "-m", "uvicorn", "operator_app.main:app", "--host", "0.0.0.0", "--port", "8790", "--workers", "1"]
