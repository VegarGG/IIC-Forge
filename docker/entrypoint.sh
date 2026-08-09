#!/bin/sh
set -eu

# Docker Compose mounts private values as files under /run/secrets. Export them
# only inside the application process; never echo values or place them in the
# Compose environment where `docker inspect` would reveal them.
load_secret() {
    variable_name="$1"
    secret_name="$2"
    eval "existing_value=\${${variable_name}:-}"
    secret_path="/run/secrets/${secret_name}"
    if [ -z "$existing_value" ] && [ -f "$secret_path" ]; then
        secret_value=$(sed -e 's/[[:space:]]*$//' "$secret_path")
        if [ -z "$secret_value" ]; then
            echo "fatal: Docker secret ${secret_name} is empty" >&2
            exit 78
        fi
        export "${variable_name}=${secret_value}"
    fi
}

load_secret DEEPSEEK_API_KEY deepseek_api_key
load_secret POLYGON_API_KEY polygon_api_key
load_secret TELEGRAM_API_ID telegram_api_id
load_secret TELEGRAM_API_HASH telegram_api_hash
load_secret IIC_TELEGRAM_BOT_TOKEN telegram_bot_token
load_secret IIC_SMTP_USER smtp_user
load_secret IIC_SMTP_APP_PASSWORD smtp_app_password

exec iic-forge "$@"
