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
#
# راه‌اندازی روی دامنه با HTTPS (nginx + گواهی رایگان Let's Encrypt):
#
#   sudo DOMAIN=example.com LETSENCRYPT_EMAIL=you@example.com bash install-crypto-bot.sh
#
#   DOMAIN             دامنه‌ی سرویس. با دادن آن، nginx و گواهی TLS خودکار
#                      راه‌اندازی می‌شوند و برنامه فقط روی 127.0.0.1 گوش می‌دهد.
#   DOMAIN_ALIASES     دامنه‌های اضافی روی همان گواهی، با فاصله (مثلاً "www.example.com")
#   LETSENCRYPT_EMAIL  ایمیل هشدار انقضای گواهی (الزامی وقتی DOMAIN داده شود)
#
# اگر روی این سرور از قبل یک پروکسی/پنل/کانتینر دیگر جلوی ۸۰ و ۴۴۳ نشسته
# (Nginx Proxy Manager، Traefik، Apache، aaPanel و…) و خودتان دامنه و گواهی را
# آنجا تنظیم می‌کنید، با SKIP_NGINX=1 فقط برنامه پیکربندی می‌شود:
#
#   sudo DOMAIN=example.com SKIP_NGINX=1 BIND_ADDRESS=172.17.0.1 \
#        bash install-crypto-bot.sh
#
#   SKIP_NGINX=1   nginx/گواهی اصلاً دست‌کاری نمی‌شود؛ فقط PUBLIC_BASE_URL و
#                  آدرس گوش‌دادن سرویس تنظیم می‌شوند.
#   BIND_ADDRESS   آدرسی که برنامه روی آن گوش می‌دهد (پیش‌فرض 127.0.0.1).
#                  اگر پروکسی جلویی داخل داکر است، 127.0.0.1 از داخل کانتینر
#                  در دسترس نیست — آدرس گیت‌وی داکر (معمولاً 172.17.0.1) را
#                  بدهید تا هم کانتینر برسد و هم از اینترنت باز نباشد.
#
# دامنه فقط یک‌بار لازم است داده شود؛ دفعات بعد از .env خوانده می‌شود.
# ------------------------------------------------------------------------
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/for256256/crypto-bot.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/crypto-bot}"
SERVICE_NAME="${SERVICE_NAME:-crypto-bot}"
DOMAIN="${DOMAIN:-}"
DOMAIN_ALIASES="${DOMAIN_ALIASES:-}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-}"
SKIP_NGINX="${SKIP_NGINX:-}"
BIND_ADDRESS="${BIND_ADDRESS:-}"

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

DASHBOARD_PORT=$(grep -E '^DASHBOARD_PORT=' .env | tail -1 | cut -d= -f2 || true)
DASHBOARD_PORT="${DASHBOARD_PORT:-8891}"

# ---------- دامنه و HTTPS ----------
# نوشتن یک کلید در .env (جایگزینی اگر بود، افزودن اگر نبود)
set_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s#^${key}=.*#${key}=${value}#" .env
  else
    echo "${key}=${value}" >> .env
  fi
}

# دامنه اگر این بار داده نشده، از PUBLIC_BASE_URL موجود در .env خوانده می‌شود،
# تا آپدیت‌های بعدی بدون تکرار DOMAIN=… همان تنظیم را حفظ کنند.
# خواندن یک کلید از .env — || true لازم است: با set -o pipefail، نبودن کلید
# یعنی grep کد ۱ برمی‌گرداند و set -e کل اسکریپت را همین‌جا می‌کشد، یعنی هر
# نصب قدیمی (که این کلیدها را ندارد) موقع آپدیت بی‌صدا نصفه‌کاره می‌ماند.
get_env() {
  grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true
}

if [ -z "$DOMAIN" ]; then
  EXISTING_BASE=$(get_env PUBLIC_BASE_URL)
  if [ -n "${EXISTING_BASE:-}" ]; then
    DOMAIN=$(echo "$EXISTING_BASE" | sed -e 's#^https\?://##' -e 's#/.*$##')
    log "دامنه‌ی تنظیم‌شده‌ی قبلی از .env خوانده شد: ${DOMAIN}"
  fi
fi

# حالت پروکسی جلویی هم باید ماندگار باشد. بدون این، آپدیت ساده‌ی بعدی
# (بدون هیچ متغیری) دامنه را از .env می‌خواند ولی SKIP_NGINX را فراموش
# می‌کند و می‌رود سراغ نصب nginx و گرفتن پورت ۴۴۳ — یعنی دقیقاً همان
# سرویسی که قرار بود دست‌نخورده بماند.
if [ -z "$SKIP_NGINX" ]; then
  SKIP_NGINX=$(get_env SKIP_NGINX)
  [ -n "$SKIP_NGINX" ] && log "حالت پروکسی جلویی (SKIP_NGINX) از .env خوانده شد."
fi
if [ -z "$BIND_ADDRESS" ]; then
  BIND_ADDRESS=$(get_env BIND_ADDRESS)
fi

if [ -n "$DOMAIN" ] && [ -n "$SKIP_NGINX" ]; then
  # پروکسی جلویی مال خود کاربر است — nginx و گواهی اینجا اصلاً لمس نمی‌شوند.
  log "حالت SKIP_NGINX — پیکربندی nginx/گواهی انجام نمی‌شود."
  set_env "PUBLIC_BASE_URL" "https://${DOMAIN}"
  BIND_HOST="${BIND_ADDRESS:-127.0.0.1}"
  # تا آپدیت‌های بعدی بدون تکرار این متغیرها همین مسیر را بروند
  set_env "SKIP_NGINX" "1"
  set_env "BIND_ADDRESS" "${BIND_HOST}"
  log "PUBLIC_BASE_URL=https://${DOMAIN} و حالت پروکسی جلویی در .env ثبت شد."
  # forwarded-allow-ips روی * است چون در این حالت آدرس پروکسی جلویی را
  # نمی‌دانیم (ممکن است IP یک کانتینر داکر باشد که هر ری‌استارت عوض می‌شود).
  # امنیتش از راه در دسترس نبودن این پورت از اینترنت تأمین می‌شود، نه از
  # راه فیلترکردن IP — پس BIND_ADDRESS نباید 0.0.0.0 باشد.
  UVICORN_EXTRA="--proxy-headers --forwarded-allow-ips *"
  if [ "$BIND_HOST" = "0.0.0.0" ]; then
    echo "" >&2
    echo "هشدار: BIND_ADDRESS=0.0.0.0 یعنی داشبورد روی http://<IP سرور>:${DASHBOARD_PORT}" >&2
    echo "از کل اینترنت باز است و TLS و پروکسی جلویی دور زده می‌شوند." >&2
    echo "پورت ${DASHBOARD_PORT} را حتماً در فایروال ببندید." >&2
    echo "" >&2
  fi
elif [ -n "$DOMAIN" ]; then
  if [ -z "$LETSENCRYPT_EMAIL" ] && [ ! -d "/etc/letsencrypt/live/${DOMAIN}" ]; then
    echo "برای صدور گواهی TLS باید LETSENCRYPT_EMAIL هم داده شود:" >&2
    echo "  sudo DOMAIN=${DOMAIN} LETSENCRYPT_EMAIL=you@example.com bash install-crypto-bot.sh" >&2
    exit 1
  fi

  # صاحب یک پورت را برمی‌گرداند (خالی یعنی آزاد است).
  port_owner() {
    ss -ltnp 2>/dev/null | awk -v p=":$1\$" '$4 ~ p {print $0}' \
      | grep -o 'users:((\"[^\"]*' | head -1 | sed 's/.*((\"//' || true
  }

  # اگر چیز دیگری از قبل روی ۸۰ یا ۴۴۳ نشسته، نباید کورکورانه nginx نصب/ریستارت
  # کنیم — یا nginx بالا نمی‌آید، یا بدتر، سرویس دیگری را از کار می‌اندازیم.
  # nginx خودش استثناست: در آن حالت فقط یک vhost کنار بقیه اضافه می‌شود.
  #
  # ۴۴۳ جداگانه چک می‌شود و نه فقط برای احتیاط: certbot --nginx یک بلاک
  # «listen 443 ssl» به کانفیگ اضافه می‌کند. اگر ۴۴۳ دست پروسه‌ی دیگری باشد،
  # nginx موقع ریلود نمی‌تواند سوکت را بگیرد و کل nginx پایین می‌آید — یعنی
  # سایت‌های دیگری هم که روی ۸۰ سرو می‌شدند با هم از کار می‌افتند.
  PORT80_PROC=$(port_owner 80)
  PORT443_PROC=$(port_owner 443)

  BUSY_PORT=""
  BUSY_PROC=""
  if [ -n "${PORT80_PROC:-}" ] && [ "$PORT80_PROC" != "nginx" ]; then
    BUSY_PORT="۸۰"; BUSY_PROC="$PORT80_PROC"
  elif [ -n "${PORT443_PROC:-}" ] && [ "$PORT443_PROC" != "nginx" ]; then
    BUSY_PORT="۴۴۳"; BUSY_PROC="$PORT443_PROC"
  fi

  if [ -n "$BUSY_PORT" ]; then
    echo "" >&2
    echo "پورت ${BUSY_PORT} روی این سرور در اختیار «${BUSY_PROC}» است، نه nginx." >&2
    echo "" >&2
    echo "TradingView فقط پورت ۸۰ و ۴۴۳ را برای وبهوک قبول می‌کند، پس نمی‌شود" >&2
    echo "این سرویس را روی پورت دیگری گذاشت. باید همان ${BUSY_PROC} درخواست‌های" >&2
    echo "دامنه‌ی ${DOMAIN} را به http://127.0.0.1:${DASHBOARD_PORT} پروکسی کند" >&2
    echo "و گواهی TLS همان دامنه را هم خودش نگه دارد." >&2
    echo "" >&2
    echo "اسکریپت اینجا متوقف شد تا به سرویس دیگر دست نزند." >&2
    echo "" >&2
    echo "بعد از اینکه در «${BUSY_PROC}» دامنه را به این سرویس وصل کردید،" >&2
    echo "همین دستور را با SKIP_NGINX=1 اجرا کنید تا فقط برنامه تنظیم شود:" >&2
    echo "  sudo DOMAIN=${DOMAIN} SKIP_NGINX=1 bash install-crypto-bot.sh" >&2
    echo "(اگر پروکسی جلویی داخل داکر است: BIND_ADDRESS=172.17.0.1 را هم اضافه کنید)" >&2
    exit 1
  fi

  log "نصب nginx و certbot…"
  NGINX_WAS_INSTALLED=1
  if ! command -v nginx >/dev/null 2>&1; then
    NGINX_WAS_INSTALLED=0
  fi
  if ! command -v nginx >/dev/null 2>&1 || ! command -v certbot >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y nginx certbot python3-certbot-nginx
  fi

  CERT_DOMAINS="-d ${DOMAIN}"
  SERVER_NAMES="${DOMAIN}"
  for alias in $DOMAIN_ALIASES; do
    CERT_DOMAINS="${CERT_DOMAINS} -d ${alias}"
    SERVER_NAMES="${SERVER_NAMES} ${alias}"
  done

  log "نوشتن پیکربندی nginx برای ${SERVER_NAMES}…"
  # فقط بلاک HTTP نوشته می‌شود؛ بلاک HTTPS و ریدایرکت را خود certbot اضافه
  # می‌کند. اجرای دوباره‌ی اسکریپت همین مسیر را تکرار می‌کند و certbot گواهی
  # موجود را دوباره در همین فایل می‌نشاند — پس ایدمپوتنت است.
  cat > "/etc/nginx/sites-available/${SERVICE_NAME}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${SERVER_NAMES};

    # بارگذاری فایل بکاپ تنظیمات
    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:${DASHBOARD_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        # بدون این هدر، برنامه فکر می‌کند درخواست http است و آدرس وبهوکی که
        # به کاربر نشان می‌دهد http درمی‌آید.
        proxy_set_header X-Forwarded-Proto \$scheme;
        # بعضی فراخوانی‌های صرافی کند هستند؛ مهلت پیش‌فرض ۶۰ ثانیه کم است.
        proxy_read_timeout 120s;
    }
}
EOF
  ln -sf "/etc/nginx/sites-available/${SERVICE_NAME}" "/etc/nginx/sites-enabled/${SERVICE_NAME}"
  # سایت default فقط وقتی برداشته می‌شود که nginx را همین الان خودمان نصب کرده
  # باشیم. اگر nginx از قبل بوده، ممکن است سرویس دیگری روی همین سرور از همان
  # فایل استفاده کند و حذفش آن را از کار می‌اندازد. vhost ما بر اساس
  # server_name انتخاب می‌شود، پس ماندن default هیچ تداخلی ایجاد نمی‌کند.
  if [ "$NGINX_WAS_INSTALLED" -eq 0 ]; then
    rm -f /etc/nginx/sites-enabled/default
  fi
  nginx -t
  systemctl enable nginx >/dev/null 2>&1 || true
  systemctl reload nginx || systemctl restart nginx

  log "گرفتن/تمدید گواهی TLS برای ${SERVER_NAMES}…"
  # اگر اینجا شکست خورد، تقریباً همیشه یعنی درخواست ACME به سرور نرسیده —
  # روی کلادفلر ابر را موقتاً خاکستری (DNS only) کنید و دوباره اجرا کنید.
  if certbot --nginx ${CERT_DOMAINS} \
       --non-interactive --agree-tos --redirect --keep-until-expiring \
       ${LETSENCRYPT_EMAIL:+--email "$LETSENCRYPT_EMAIL"}; then
    log "گواهی TLS نصب شد ✅"
  else
    echo "" >&2
    echo "صدور گواهی TLS ناموفق بود." >&2
    echo "شایع‌ترین علت: درخواست Let's Encrypt به سرور نرسیده است." >&2
    echo "در پنل کلادفلر رکورد ${DOMAIN} را موقتاً روی «DNS only» (ابر خاکستری)" >&2
    echo "بگذارید، همین دستور را دوباره اجرا کنید، بعد ابر را نارنجی کنید." >&2
    exit 1
  fi

  set_env "PUBLIC_BASE_URL" "https://${DOMAIN}"
  log "PUBLIC_BASE_URL=https://${DOMAIN} در .env ثبت شد."

  # پشت nginx، برنامه نباید مستقیم از بیرون در دسترس باشد؛ وگرنه همان داشبورد
  # روی http://IP:PORT بدون TLS و بدون کلادفلر هم باز می‌ماند.
  BIND_HOST="127.0.0.1"
  UVICORN_EXTRA="--proxy-headers --forwarded-allow-ips 127.0.0.1"

  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
    log "باز کردن پورت‌های ۸۰ و ۴۴۳ در ufw…"
    ufw allow 'Nginx Full' >/dev/null 2>&1 || true
    ufw delete allow "${DASHBOARD_PORT}" >/dev/null 2>&1 || true
  fi
else
  BIND_HOST="0.0.0.0"
  UVICORN_EXTRA=""
fi

log "نصب/به‌روزرسانی سرویس systemd (${SERVICE_NAME})…"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Crypto Bot - Toobit Futures Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/uvicorn app.main:app --host ${BIND_HOST} --port ${DASHBOARD_PORT} ${UVICORN_EXTRA}
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
  if [ -n "$DOMAIN" ]; then
    echo "داشبورد: https://${DOMAIN}"
    echo "(برنامه فقط روی 127.0.0.1:${DASHBOARD_PORT} گوش می‌دهد و از بیرون فقط از راه nginx در دسترس است)"
    echo "یادآوری: آدرس وبهوک TradingView حالا https://${DOMAIN}/webhook/tradingview/<شناسه‌ی حساب> است —"
    echo "         آدرس تازه را از داشبورد هر حساب کپی و در Alertهای TradingView جایگزین کنید."
  else
    echo "داشبورد: http://${SERVER_IP:-<IP-سرور>}:${DASHBOARD_PORT}"
  fi
  if [ "$FRESH_INSTALL" -eq 0 ]; then
    echo "به‌روزرسانی و ریستارت با موفقیت انجام شد."
  fi
else
  echo "سرویس بالا نیامد — لاگ را بررسی کنید: journalctl -u ${SERVICE_NAME} -e" >&2
  exit 1
fi
