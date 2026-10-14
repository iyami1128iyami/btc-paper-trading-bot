"""
Telegram Botによる通知モジュール。

通知はあくまで補助機能なので、送信に失敗してもボット本体の動作は
絶対に止めないよう、例外はすべてこの中で握りつぶしてログに残すだけにする。

TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID のいずれかが未設定の場合は、
何もせず静かに終了する(通知機能そのものを使わない人にエラーを出さないため)。

デフォルトではバックグラウンドスレッドで送信する(取引ループを
Telegram側の応答待ちでブロックしないため)。ただし、通知の直後に
プロセスを異常終了させる箇所(起動失敗など)ではblocking=Trueを指定し、
プロセスが落ちる前に確実に送信を完了させる。
"""

import logging
import os
import threading

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


def _send(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
        resp.raise_for_status()
        return True
    except Exception as exc:  # 通知自体の失敗で本体を止めないよう、意図的に広く捕捉する
        logger.warning("Telegram通知の送信に失敗しました: %s", exc)
        return False


def notify(message: str, level: str = "info", blocking: bool = False) -> bool | None:
    """
    Telegramへ通知を送る。

    Args:
        blocking: Trueの場合、送信完了を待ってから返る。
            この直後にプロセスを終了・異常終了させる場面(起動失敗など)で、
            バックグラウンドスレッドごと通知が消えてしまうのを防ぐために使う。
            デフォルト(False)は非同期送信で、取引ループの応答速度を優先する。

    Returns:
        blocking=Trueの場合: 送信に成功したかどうかのbool
        blocking=False(デフォルト)の場合: 送信はバックグラウンドに委ねるためNone
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False if blocking else None

    emoji = _EMOJI.get(level, "")
    text = f"{emoji} {message}".strip()
    if len(text) > _MAX_CONTENT_LEN:
        text = text[: _MAX_CONTENT_LEN - 3] + "..."

    if blocking:
        return _send(text)
    threading.Thread(target=_send, args=(text,), daemon=True).start()
    return None
