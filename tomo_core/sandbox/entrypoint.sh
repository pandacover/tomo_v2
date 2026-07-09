#!/bin/sh
set -eu

# A sandbox handles one inbound request; it must never claim Telegram updates.
unset TELEGRAM_BOT_TOKEN TOMO_TELEGRAM_BOT_TOKEN TOMO_TELEGRAM_GLOBAL_BOT_TOKEN

exec tomo-core sandbox inbound --once "$@"
