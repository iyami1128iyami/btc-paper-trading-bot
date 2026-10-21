"""
複数プロセス(gunicornのワーカーなど)が同時に取引ループを動かしてしまい、
二重発注や状態競合が起きるのを防ぐための簡易リーダー選出ロック。

現状はWEB_CONCURRENCY=1を明示的に固定しているため実害は出ていないが、
将来プランやデプロイ環境が変わってワーカー数が増えても安全なように、
コード側でも防御する。Redis(REDIS_URL)が使える場合のみ機能し、
未設定(ローカルファイルへのフォールバック運用)の場合は単一プロセット
前提とみなして常にロック取得成功として扱う。
"""

import logging
import os
import socket
import uuid

from config import REDIS_URL, SYMBOL, TRADING_MODE
from improved.notifier import notify

logger = logging.getLogger(__name__)

# 同一プロセス内の複数スレッドから呼ばれても同じIDになるよう、プロセス起動時に1回だけ生成する
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
# TRADING_MODEもキーに含める。ペーパートレード用とデモトレード用に別々のRenderサービスを
# 同じRedisで動かした場合に、お互いが同じロックを奪い合って片方が永久に取引できなくなるのを防ぐ。
_LOCK_KEY = f"btc_trading_bot:trading_loop_leader:{TRADING_MODE}:{SYMBOL}"

_redis_client = None
if REDIS_URL:
    import redis

    _redis_client = redis.from_url(
        REDIS_URL, decode_responses=True, socket_timeout=5, socket_connect_timeout=5
    )

# Redis障害中に毎サイクル通知が飛ぶのを防ぐためのフラグ(発生時・復旧時の1回だけ通知する)
_redis_error_notified = False


def try_acquire_or_renew(ttl_sec: int) -> bool:
    """
    取引ループを実行する権利(リーダー権)を取得、または既に持っていれば更新(延長)する。

    Returns:
        bool: このプロセスが取引ループを実行してよいかどうか。
            Redis未使用(REDIS_URL未設定)の場合は常にTrue。
            Redis接続エラー時は安全側に倒してFalse(今回のサイクルは見送る)。
    """
    global _redis_error_notified
    if _redis_client is None:
        return True
    try:
        current = _redis_client.get(_LOCK_KEY)
        if current == _WORKER_ID:
            # 既に自分がリーダー → 有効期限を延長するだけ
            _redis_client.expire(_LOCK_KEY, ttl_sec)
            result = True
        elif current is None:
            # 誰も持っていない(未設定 or 前のリーダーが期限切れで消えた) → 取得を試みる
            result = bool(_redis_client.set(_LOCK_KEY, _WORKER_ID, nx=True, ex=ttl_sec))
        else:
            # 他のプロセスが既にリーダー
            result = False

        if _redis_error_notified:
            notify("✅ リーダーロック用Redisへの接続が復旧しました。", level="recovery")
            _redis_error_notified = False
        return result
    except Exception as exc:
        logger.warning("リーダーロックの確認に失敗しました。安全側として今回のサイクルは見送ります: %s", exc)
        if not _redis_error_notified:
            # 障害中ずっと通知が飛び続けるのを防ぐため、発生時に1回だけ知らせる。
            notify(
                f"🚨 リーダーロック用Redisへの接続に失敗しました。復旧するまで取引を一時停止します: {exc}",
                level="error",
            )
            _redis_error_notified = True
        return False
