# Hudle Padel Watcher — disabled

This integration was disabled at the owner's request on 2026-10-03.

- The GitHub Actions workflow was verified inactive. If resumed, the current
  watcher script still exits immediately without polling or sending messages.
- `watch.py` exits before loading configuration, polling Hudle, or changing state.
- `telegram_send` is a no-op: it reads no credentials and makes no network request.
- Telegram credentials and setup instructions have been removed from this README.

## Credential exposure

A Telegram bot credential was previously published here. Removing it from the
current README does not revoke it or remove it from Git history, forks, or caches.
The owner must revoke the exposed credential through Telegram's official
@BotFather. No replacement credential is needed by this disabled integration.

## Preserved project data

Venue preferences and the historical slot/booking planner remain for reference.
No booking, account, or bot has been deleted. Re-enabling this integration would
require an explicit code change; setting environment variables cannot enable it.
