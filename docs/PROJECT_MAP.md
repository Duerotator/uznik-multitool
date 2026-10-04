# Карта Uznik MultiTool

- `setup.bat`, `scripts/setup.ps1` — установка Windows-окружения и Chromium.
- `start_gui.bat`, `launch.pyw` — запуск desktop; `create_shortcut.bat` и
  `scripts/create_shortcut.ps1` — переносимый ярлык; `start_debug.bat` — диагностика.
- `main.py`, `ui/qt_app.py` — единственная точка входа и Qt-интерфейс,
  фиксированный Comfortable, собственные QSettings.
- `toolbox.bat`, `scripts/toolbox.py` — меню вспомогательных desktop-утилит;
  `scripts/{sessions,accounts,profiles,network}/`, `scripts/README.md` — команды.
- `core/project_layout.py`, `scripts/prepare_layout.py` — пустая структура данных;
  `imports/auth_input/` — очередь новой авторизации; `imports/` — обычный импорт.
- `assets/branding/uznik-multitool.ico` — иконка ярлыка и окна приложения.
- `core/` — конфигурация, клиенты MTProto, сессии, хранение, задачи и UI-контракты.
- `modules/accounts.py`, `session_*.py`, `account_security.py`, `direct_access.py`
  — аккаунты, импорт, проверка, безопасность и Telegram Web.
- `modules/profile_*.py`, `scrape_cache.py`, `utils/profile_generator.py`
  — парсинг, архив, применение, истории и генерация профилей.
- `modules/giveaway_service.py`, `raffle_*.py` — desktop-участие в розыгрышах.
- `modules/proxy_manager.py`, `async_proxy_manager.py`, `vpn_gateway.py`
  — пользовательские прокси и необязательный локальный Xray.
- `modules/chat_actions.py`, `warmup_engine.py`, `ai_companion.py`, `scenarios.py`
  — действия, прогрев и сценарии; `online_mode.py`, `sleep_scheduler.py` — расписание.
- `tests/TEST_MAP.md` — автономная проверка desktop-контракта и базовой логики.
- `data/`, `imports/`, `.env`, пользовательские шаблоны — локальные данные;
  в репозитории есть только пустая структура папок и памятки, без рабочих файлов.

Web, HTTP API, Bot API polling, deploy и удалённая синхронизация отсутствуют.
