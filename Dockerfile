FROM python:3.14-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY core /app/core
COPY api /app/api
COPY worker /app/worker
USER 10001:10001
EXPOSE 3000
CMD ["python", "-m", "api.main"]
