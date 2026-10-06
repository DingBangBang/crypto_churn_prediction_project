# Crypto User Behaviour Clustering & Churn Prediction — single image
# Serves the Streamlit dashboard on :8501 and runs the full pipeline on first boot.
#
# Multi-stage build:
#   * builder  -> has gcc/cython to compile hdbscan from source (no aarch64 wheel)
#   * runtime  -> slim image, only the installed packages + libgomp1
# Dependencies are installed in a few smaller RUN layers on purpose: a monolithic
# ~1GB pip layer is fragile to push/registry timeouts, several ~200-300MB layers
# are far more resilient (each layer can be retried independently).

# ------------------------------- builder --------------------------------------
FROM python:3.11-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc g++ python3-dev cython3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Grouped installs -> smaller layers (ordering: sci stack, ML stack, UI stack).
RUN pip install --no-cache-dir --prefix=/install \
        requests pandas numpy scipy scikit-learn statsmodels
RUN pip install --no-cache-dir --prefix=/install \
        hdbscan lightgbm==4.7.0 xgboost==2.1.4 shap
RUN pip install --no-cache-dir --prefix=/install \
        plotly streamlit python-dotenv kaleido pytest

# ------------------------------- runtime --------------------------------------
FROM python:3.11-slim

# libgomp1 is needed at runtime by LightGBM/XGBoost.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bring in the fully-installed Python environment (no compiler in this stage).
COPY --from=builder /install /usr/local

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

