FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data HOST=0.0.0.0 PORT=8000

# fonts-dejavu: Arabic glyphs for the sample invoices; libgl/libglib: OpenCV runtime
# tesseract-ocr(-ara): free offline OCR for photos / scanned PDFs
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core libglib2.0-0 libgl1 \
    tesseract-ocr tesseract-ocr-ara \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

RUN useradd --create-home appuser && mkdir -p /data && chown appuser /data
USER appuser
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "-m", "app"]
