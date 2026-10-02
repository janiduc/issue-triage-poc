# Container image for the triage desk. Secrets are passed as environment variables at run time.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
ENV DB_PATH=/data/triage.db SIM_TRACKER_PATH=/data/simulated_tracker.json PORT=8000
VOLUME /data
EXPOSE 8000
# One worker: the SQLite database and in-memory login lockout are single-process in this PoC.
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8000", "app:create_app()"]
