#!/usr/bin/env bash
# Setup for macOS / Linux:  ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "❌ Python 3.10+ مش متسطب. نزّله من https://www.python.org/downloads/"; exit 1
fi
"$PY" - <<'EOF' || { echo "❌ محتاج Python 3.10 أو أحدث"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF

echo "▶ بيعمل بيئة Python منفصلة (.venv)…"
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "▶ بيسطب المكتبات…"
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt -q

if [ ! -f .env ]; then
  cp .env.example .env
  echo "▶ اتعمل ملف .env — ضيف فيه ANTHROPIC_API_KEY عشان قراءة الصور تشتغل"
fi

echo "▶ بيجهز بيانات تجريبية…"
python -m app demo

echo "▶ بيشغّل الاختبارات…"
python -m unittest discover tests 2>&1 | tail -3

cat <<'EOF'

✅ كله جاهز. عشان تشغّل:

    source .venv/bin/activate
    python -m app

وافتح http://localhost:8000
الحساب التجريبي:  demo@example.com  /  demo1234
EOF
