#!/usr/bin/env bash
# ------------------------------------------------------------------------
# نصب/به‌روزرسانی یک‌خطی کریپتو بات (Toobit Futures) روی سرور اوبونتو.
#
# نصب اولیه یا به‌روزرسانی نسخه‌ی نصب‌شده (دانلود کد جدید + ریستارت سرویس)
# — هر دو با همین یک دستور، بدون نیاز به دانلود/آپلود دستی فایل:
#
#   curl -fsSL https://raw.githubusercontent.com/for256256/crypto-bot/main/install-crypto-bot.sh | sudo bash
#
# متغیرهای قابل تنظیم (اختیاری، قبل از دستور بالا export شوند):
#   REPO_URL     آدرس ریپازیتوری گیت (پیش‌فرض: for256256/crypto-bot روی گیت‌هاب)
#   REPO_BRANCH  برنچ نصب (پیش‌فرض: main)
#   INSTALL_DIR  مسیر نصب روی سرور (پیش‌فرض: /opt/crypto-bot)
#   SERVICE_NAME نام سرویس systemd (پیش‌فرض: crypto-bot)
# ------------------------------------------------------------------------
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/for256256/crypto-bot.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/crypto-bot}"
SERVICE_NAME="${SERVICE_NAME:-crypto-bot}"

if [ "$(id -u)" -ne 0 ]; then
  echo "این اسکریپت باید با sudo/root اجرا شود: sudo bash install-crypto-bot.sh" >&2
  exit 1
fi

log() { echo -e "\033[1;36m==>\033[0m $*"; }

log "بررسی پیش‌نیازها (git, python3, venv)…"
if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y git python3 python3-venv python3-pip
fi
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y python3-venv
fi

FRESH_INSTALL=1
if [ -d "$INSTALL_DIR/.git" ]; then
  FRESH_INSTALL=0
  log "نصب قبلی پیدا شد — به‌روزرسانی کد از برنچ ${REPO_BRANCH}…"
  git -C "$INSTALL_DIR" fetch origin "$REPO_BRANCH"
  git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
  git -C "$INSTALL_DIR" reset --hard "origin/${REPO_BRANCH}"
else
  if [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    BACKUP_DIR="${INSTALL_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    log "پوشه‌ی ${INSTALL_DIR} از قبل وجود دارد ولی یک نصب گیت معتبر نیست (احتمالاً از یک تلاش ناموفق قبلی) — به ${BACKUP_DIR} منتقل می‌شود…"
    mv "$INSTALL_DIR" "$BACKUP_DIR"
  fi
  log "دانلود کد از ${REPO_URL} (برنچ ${REPO_BRANCH}) در ${INSTALL_DIR}…"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

log "ساخت/به‌روزرسانی محیط مجازی پایتون و نصب پکیج‌ها…"
python3 -m venv venv
./venv/bin/pip install --upgrade pip >/dev/null
./venv/bin/pip install -r requirements.txt

mkdir -p config

if [ ! -f .env ]; then
  log "ساخت .env با رمز و توکن تصادفی…"
  RAND_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")
  RAND_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
  RAND_SESSION_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  cp .env.example .env
  sed -i "s#^DASHBOARD_PASSWORD=.*#DASHBOARD_PASSWORD=${RAND_PASSWORD}#" .env
  sed -i "s#^WEBHOOK_TOKEN=.*#WEBHOOK_TOKEN=${RAND_TOKEN}#" .env
  sed -i "s#^SESSION_SECRET_KEY=.*#SESSION_SECRET_KEY=${RAND_SESSION_KEY}#" .env
  echo "  رمز داشبورد: ${RAND_PASSWORD}"
  echo "  توکن وبهوک: ${RAND_TOKEN}"
  echo "  (این مقادیر فقط همین یک‌بار نمایش داده می‌شوند — در ${INSTALL_DIR}/.env هم ذخیره شده‌اند)"
else
  log ".env از قبل موجود است — دست‌نخورده باقی می‌ماند."
fi

# نصب‌های قدیمی‌تر SESSION_SECRET_KEY را در .env ندارند — بدون آن جلسه‌ی
# لاگین کار نمی‌کند، پس اگر غایب بود همین یک خط اضافه/تولید می‌شود.
if ! grep -q '^SESSION_SECRET_KEY=.\+' .env 2>/dev/null; then
  log "افزودن SESSION_SECRET_KEY به .env موجود…"
  RAND_SESSION_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  if grep -q '^SESSION_SECRET_KEY=' .env; then
    sed -i "s#^SESSION_SECRET_KEY=.*#SESSION_SECRET_KEY=${RAND_SESSION_KEY}#" .env
  else
    echo "SESSION_SECRET_KEY=${RAND_SESSION_KEY}" >> .env
  fi
fi

DASHBOARD_PORT=$(grep -E '^DASHBOARD_PORT=' .env | tail -1 | cut -d= -f2)
DASHBOARD_PORT="${DASHBOARD_PORT:-8891}"

log "نصب/به‌روزرسانی سرویس systemd (${SERVICE_NAME})…"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Crypto Bot - Toobit Futures Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${DASHBOARD_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"

sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}"; then
  SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  log "سرویس ${SERVICE_NAME} فعال است ✅"
  echo "داشبورد: http://${SERVER_IP:-<IP-سرور>}:${DASHBOARD_PORT}"
  if [ "$FRESH_INSTALL" -eq 0 ]; then
    echo "به‌روزرسانی و ریستارت با موفقیت انجام شد."
  fi
else
  echo "سرویس بالا نیامد — لاگ را بررسی کنید: journalctl -u ${SERVICE_NAME} -e" >&2
  exit 1
fi
