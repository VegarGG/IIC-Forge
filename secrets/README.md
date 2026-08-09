# Local Docker secrets

Create this directory on the production host with mode `0700`. Create each
file below with mode `0600`, containing exactly one value and an optional final
newline:

- `deepseek_api_key`
- `polygon_api_key`
- `telegram_api_id`
- `telegram_api_hash`
- `telegram_bot_token`
- `smtp_user`
- `smtp_app_password`

The files are mounted read-only at `/run/secrets`; the image entry point exports
them only inside the application process. Every file except this README is
ignored by Git. Never add real values to `.env.production` or the Compose file.
