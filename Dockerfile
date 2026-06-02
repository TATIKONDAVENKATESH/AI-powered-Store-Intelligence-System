FROM python:3.11-slim

# Install OpenCV system deps (headless, CPU only)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libsm6 libxrender1 libxext6 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Create required directories
RUN mkdir -p storage data/videos data/generated_events config

# Default: run the API
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]