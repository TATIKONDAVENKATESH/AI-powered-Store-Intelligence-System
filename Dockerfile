FROM python:3.11-slim

# ── System deps ────────────────────────────────────────────────────────────
# libglib2.0-0   → required by opencv-python-headless
# libsm6 libxrender1 libxext6 → required by opencv for display (headless still needs them)
# libgl1          → required by opencv for video decode
# NO ffmpeg: it is 200MB+ and opencv-python-headless does not require it for MP4 decode
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps ────────────────────────────────────────────────────────────
# Install CPU-only torch FIRST before ultralytics.
# Without this, `pip install ultralytics` pulls the full CUDA torch (~2GB).
# torch CPU wheel is ~200MB vs ~2GB for CUDA — saves ~1.8GB and 10+ minutes.
RUN pip install --no-cache-dir \
    torch==2.3.0+cpu \
    torchvision==0.18.0+cpu \
    --index-url https://download.pytorch.org/whl/cpu

# Copy requirements and install remaining deps
# (torch is already installed above so pip skips it)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ───────────────────────────────────────────────────────
# .dockerignore excludes: data/videos/, data/generated_events/, storage/, .git/
# So this COPY is fast and never copies MP4 files into the image layer.
COPY . .

# Ensure runtime directories exist (volumes will mount over these at runtime)
RUN mkdir -p storage data/videos data/generated_events config

# Default: run the API
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]