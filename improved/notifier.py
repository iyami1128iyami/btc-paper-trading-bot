"""
Telegram Botによる通知モジュール。

通知はあくまで補助機能なので、送信に失敗してもボット本体の動作は
絶対に止めないよう、例外はすべてこの中で握りつぶしてログに残すだけにする。

TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID のいずれかが未設定の場合は、
何もせず静かに終了する(通知機能そのものを使わない人にエラーを出さないため)。
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Telegramのメッセージ本文の上限(超過分は送信前に切り詰める)
_MAX_CONTENT_LEN = 4096

_EMOJI = {
    "trade": "💰",
    "error": "🚨",
    "recovery": "✅",
    "warning": "⚠️",
    "info": "ℹ️",
}


def notify(message: str, level: str = "info") -> bool:
    """
    Telegramへ通知を送る。

    Returns:
        bool: 送信に成功したかどうか(呼び出し側は基本的に結果を無視してよい)
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    emoji = _EMOJI.get(level, "")
    text = f"{emoji} {message}".strip()
    if len(text) > _MAX_CONTENT_LEN:
        text = text[: _MAX_CONTENT_LEN - 3] + "..."

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:  # 通知自体の失敗で本体を止めないよう、意図的に広く捕捉する
        logger.warning("Telegram通知の送信に失敗しました: %s", exc)
        return False
