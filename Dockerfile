# Container image for the triage desk. Secrets are passed as environment variables at run time.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
COPY requirements-embeddings.txt .
RUN pip install --no-cache-dir -r requirements.txt -r requirements-embeddings.txt gunicorn
COPY . .
# APP_ENV=production: refuses a weak SECRET_KEY, secure session cookie, no demo accounts
# (set ADMIN_PASSWORD for the first administrator, or SEED_DEMO_USERS=true for a demo).
ENV APP_ENV=production DB_PATH=/data/triage.db SIM_TRACKER_PATH=/data/simulated_tracker.json PORT=8000
VOLUME /data
EXPOSE 8000
# One worker: the SQLite database and in-memory login lockout are single-process in this PoC.
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8000", "app:create_app()"]
