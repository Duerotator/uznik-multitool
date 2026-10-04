# Карта Uznik MultiTool

- `setup.bat`, `scripts/setup.ps1` — полная Windows-установка;
  `scripts/windows/install_native_tools.ps1` — FFmpeg, Tesseract, Xray через WinGet;
  `scripts/check_install.py` — автономная проверка; `docs/INSTALLATION.md` — руководство.
- `start_gui.bat`, `app/launch.pyw` — запуск desktop; `scripts/windows/create_shortcut.bat`
  и `scripts/create_shortcut.ps1` — ярлыки в корне и на рабочем столе;
  `scripts/windows/start_debug.bat` — диагностика.
- `app/main.py`, `app/ui/qt_app.py` — единственная точка входа и Qt-интерфейс,
  фиксированный Comfortable, собственные QSettings.
- `toolbox.bat`, `scripts/toolbox.py` — меню вспомогательных desktop-утилит;
  `scripts/{sessions,accounts,profiles,network}/`, `scripts/README.md` — команды.
- `create_sessions.bat`, `scripts/sessions/menu.py` — меню создания сессий.
- `config/requirements.txt`, `config/.env.example` — зависимости и пустой шаблон;
  пользовательский `.env` остаётся в корне.
- `app/core/project_layout.py`, `scripts/prepare_layout.py` — пустая структура данных;
  `imports/auth_input/` — очередь новой авторизации; `imports/` — обычный импорт.
- `assets/branding/uznik-multitool.ico` — иконка ярлыка и окна приложения.
- `app/core/` — конфигурация, клиенты MTProto, сессии, хранение, задачи и UI-контракты.
- `app/modules/accounts.py`, `session_*.py`, `account_security.py`, `direct_access.py`
  — аккаунты, импорт, проверка, безопасность и Telegram Web.
- `app/modules/profile_*.py`, `scrape_cache.py`, `app/utils/profile_generator.py`
  — парсинг, архив, применение, истории и генерация профилей.
- `app/modules/giveaway_service.py`, `raffle_*.py` — desktop-участие в розыгрышах.
- `app/modules/proxy_manager.py`, `async_proxy_manager.py`, `vpn_gateway.py`
  — пользовательские прокси и необязательный локальный Xray.
- `app/modules/chat_actions.py`, `warmup_engine.py`, `ai_companion.py`, `scenarios.py`
  — действия, прогрев и сценарии; `online_mode.py`, `sleep_scheduler.py` — расписание.
- `tests/TEST_MAP.md` — автономная проверка desktop-контракта и базовой логики.
- `data/`, `imports/`, `.env`, пользовательские шаблоны — локальные данные;
  в репозитории есть только пустая структура папок и памятки, без рабочих файлов.

Web, HTTP API, Bot API polling, deploy и удалённая синхронизация отсутствуют.
