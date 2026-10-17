FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Dependencies as their own layer, keyed only on constraints.txt, so editing
# app code does not re-download the tree. constraints.txt is the full resolved
# closure -- see its header for how to regenerate it.
COPY constraints.txt ./
RUN pip install -r constraints.txt

COPY pyproject.toml README.md ./
COPY app ./app

# Installs the app itself. `--constraint` re-checks pyproject's declared
# dependencies against the pins rather than trusting the layer above, so a
# dependency added to pyproject without regenerating constraints.txt still
# produces a working image (just an unpinned one).
RUN pip install --constraint constraints.txt '.[backfill]'

# Run unprivileged. Port 8000 is above 1024, so no capabilities are needed and
# the container works under a read-only root filesystem.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin igsn
USER 10001

EXPOSE 8000

# --proxy-headers: behind ingress-nginx the TCP peer is the ingress pod, so the
# client address and original scheme exist only in X-Forwarded-*. Without it
# /docs builds http:// URLs on an https:// site and every access log records
# the ingress IP. Trusting all forwarders is correct here because the pod is
# reachable only through the Service.
#
# Single worker by default. The publisher loop claims work with an atomic
# find_one_and_update, so extra replicas are safe -- but each one runs its own
# loop, so scale deliberately rather than by accident.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
