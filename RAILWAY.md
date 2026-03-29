# Railway Deployment

This project is set up to run the Discord bot and the FastAPI dashboard in the same Railway service.

## Start command

Railway will use:

```bash
python main.py
```

The dashboard listens on Railway's injected `PORT` automatically, and falls back to `DASHBOARD_PORT` locally.

## Required Railway variables

Set these in your Railway service variables:

- `DISCORD_TOKEN`
- `DISCORD_APP_ID`
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DISCORD_REDIRECT_URI`
- `DASHBOARD_SECRET_KEY`
- `DASHBOARD_BASE_URL`

Recommended:

- `DISCORD_INSTALL_PERMISSIONS=8`

## Important dashboard values

- `DASHBOARD_BASE_URL` should be your public Railway URL, for example:

```text
https://your-service.up.railway.app
```

- `DISCORD_REDIRECT_URI` must exactly match your Discord OAuth callback:

```text
https://your-service.up.railway.app/auth/callback
```

## Discord developer portal

In the Discord Developer Portal:

1. Add your Railway callback URL under `OAuth2 > Redirects`
2. Make sure your bot invite/install settings are enabled
3. Regenerate secrets or tokens if any were previously exposed

## Railway notes

- Generate a public Railway domain for the service
- Set the Railway healthcheck path to `/health` if you want to confirm it in the UI
- Keep this as a single service if you want the bot and dashboard to run together in one process
