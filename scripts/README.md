# Вспомогательные инструменты

Двойной клик по **toolbox.bat** открывает меню. Все утилиты используют
собственные настройки проекта из `.env`, запускаются из любой рабочей папки.
Все пользовательские входы сессий находятся в папке **sessions/** в корне;
там есть меню, запуск одиночного/пакетного входа и `batch_phones.txt`.
Диагностика и пересоздание ярлыка — `windows/start_debug.bat` и
`windows/create_shortcut.bat`. Код backend расположен в `app/`, зависимости —
`config/requirements.txt`.
Для команд ниже используется `.venv\\Scripts\\python.exe`.

| Папка / утилита | Назначение |
| --- | --- |
| sessions/create_session.py | Ручной вход по своему номеру, новый локальный клиент с fingerprint и проверенным прокси |
| sessions/menu.py | Меню создания сессий и открытия папок `imports/auth_input/` и `sessions/` |
| sessions/batch_create_sessions.py | Последовательный вход по списку номеров из `sessions/batch_phones.txt`; коды/2FA вводятся вручную |
| sessions/process_auth_input.py | Новая авторизация из очереди .session; пробует автоматически прочитать свежий код через исходную сессию |
| accounts/cleanup.py | Предпросмотр или очистка чатов/контактов с явным выбором аккаунтов |
| accounts/dedupe.py | Предпросмотр дубликатов, удаление записей только с --execute; сессии сохраняются, база резервируется |
| accounts/security.py | Регистрация / восстановление Passkey для явно перечисленных аккаунтов |
| profiles/download_avatar_pack.py | Загрузка по своему URL либо сортировка своих изображений |
| network/check_setup.py | Локальная диагностика без подключения к сети и показа секретов |
| prepare_layout.py | Создание недостающих пустых папок и шаблонов без перезаписи существующих |
| create_shortcut.ps1 | Создание / обновление ярлыка с фирменной иконкой |
| setup.ps1 | Полная установка окружения, браузера и внешних инструментов; -SkipNativeTools для ручной установки внешних программ |
| windows/install_native_tools.ps1 | Установка отсутствующих FFmpeg, Tesseract и Xray через WinGet, без запуска gateway |
| check_install.py | Автономная проверка зависимостей, MTProto-ускорения, пустого Chromium и исполняемых программ |

Примеры (замените значения на свои):

```powershell
.\.venv\Scripts\python.exe scripts/sessions/create_session.py --phone YOUR_PHONE
.\.venv\Scripts\python.exe scripts/sessions/process_auth_input.py
.\.venv\Scripts\python.exe scripts/sessions/process_auth_input.py --execute
.\.venv\Scripts\python.exe scripts/sessions/batch_create_sessions.py --phones sessions/batch_phones.txt
.\.venv\Scripts\python.exe scripts/accounts/cleanup.py --accounts YOUR_ACCOUNT_ID
.\.venv\Scripts\python.exe scripts/accounts/security.py register-passkey --accounts YOUR_ACCOUNT_ID
.\.venv\Scripts\python.exe scripts/profiles/download_avatar_pack.py --organize-only
```

У cleanup и security без `--execute` изменения не выполняются.
Cleanup даже в dry-run **читает Telegram**, поэтому нужны свои ключи и прокси;
предпросмотр остальных утилит без `--execute` выполняется локально.
`--group inbox` означает весь список приложения, а не только группу без имени.
Не запускайте очистку, пока не убедились в выбранных аккаунтах.

Коды и 2FA никогда не заданы в исходниках, пароль вводится скрыто.
Сторонние исходные сессии не удаляются при ошибке авторизации.
Ручная установка и диагностика: [docs/INSTALLATION.md](../docs/INSTALLATION.md).
