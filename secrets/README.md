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
- `backup_encryption_key` — base64 encoding of exactly 32 random bytes

The files are mounted read-only at `/run/secrets`; the image entry point exports
them only inside the application process. Every file except this README is
ignored by Git. Never add real values to `.env.production` or the Compose file.

Generate the backup key once on the production host without printing it:

```bash
umask 077
openssl rand -base64 32 > secrets/backup_encryption_key
chmod 0600 secrets/backup_encryption_key
```

The key is required for every verification and restore. Keep it on the local
host but outside the data and Redis volumes. Off-host key escrow is outside the
approved project scope.
