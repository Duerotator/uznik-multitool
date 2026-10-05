"""Bounded Telegram startup and safe, credential-free network diagnostics."""
from __future__ import annotations

CONNECT_TIMEOUT = 30.0
REQUEST_TIMEOUT = 45.0
CLEANUP_TIMEOUT = 5.0


def is_connection_error(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, OSError, ConnectionError))


def connection_error(proxy: object | None) -> RuntimeError:
    route = "прокси" if proxy else "локальный IP-адрес (прямое подключение)"
    return RuntimeError(
        f"Не удалось подключиться к Telegram: ваш {route} не отвечает или недоступен. "
        "Проверьте доступ к Telegram, сеть и настройки TELEGRAM_GLOBAL_PROXY в .env."
    )
