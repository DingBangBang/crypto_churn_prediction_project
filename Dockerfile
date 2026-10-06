# Crypto User Behaviour Clustering & Churn Prediction — single image
# Serves the Streamlit dashboard on :8501 and runs the full pipeline on first boot.
FROM python:3.11-slim

WORKDIR /app

# LightGBM/XGBoost need libgomp at runtime; keep the image slim otherwise.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source.
COPY src ./src
COPY app ./app
COPY scripts ./scripts
COPY templates ./templates
RUN mkdir -p data reports docs

# Bootstrap: run the pipeline once (if the DB is empty) then start Streamlit.
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

ENV CHURN_DB_PATH=/app/data/crypto_churn.db \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 8501

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["streamlit", "run", "app/dashboard.py", "--server.port=8501", "--server.address=0.0.0.0"]
