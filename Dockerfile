# Crypto User Behaviour Clustering & Churn Prediction — single image
# Serves the Streamlit dashboard on :8501 and runs the full pipeline on first boot.
#
# Multi-stage build:
#   * builder  -> has gcc/cython to compile hdbscan from source (no aarch64 wheel)
#   * runtime  -> slim image, only the installed packages + libgomp1
# Dependencies are installed in a few smaller RUN layers and, after installation,
# site-packages is split into several ~150-200MB buckets (scripts/split_site_packages.py)
# before being copied into the runtime stage.  A monolithic ~1GB layer is fragile
# to push over a proxied / rate-limited link (the upload is reset mid-stream and
# restarts from byte 0); several small layers can each be retried independently.

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

# Split the freshly installed site-packages into balanced buckets so the runtime
# stage produces several medium layers instead of one ~1GB layer.  Placed after
# the pip layers so they stay cached when only this step changes.
COPY scripts/split_site_packages.py /tmp/split_site_packages.py
RUN python3 /tmp/split_site_packages.py /install /split 6

# ------------------------------- runtime --------------------------------------
FROM python:3.11-slim

# libgomp1 is needed at runtime by LightGBM/XGBoost.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bring in the fully-installed Python environment (no compiler in this stage).
# Each bucket is copied as its own layer (~100-200MB) so a push only has to
# retry the small layer that failed, instead of a single ~1GB blob.
COPY --from=builder /split/sp0 /usr/local/lib/python3.11/site-packages
COPY --from=builder /split/sp1 /usr/local/lib/python3.11/site-packages
COPY --from=builder /split/sp2 /usr/local/lib/python3.11/site-packages
COPY --from=builder /split/sp3 /usr/local/lib/python3.11/site-packages
COPY --from=builder /split/sp4 /usr/local/lib/python3.11/site-packages
COPY --from=builder /split/sp5 /usr/local/lib/python3.11/site-packages
# Console scripts (streamlit, pytest, ...) and anything else pip created.
COPY --from=builder /split/rest /usr/local

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

