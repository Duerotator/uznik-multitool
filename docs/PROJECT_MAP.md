# Карта Uznik MultiTool

- `setup.bat`, `scripts/setup.ps1` — полная Windows-установка;
  `scripts/windows/install_native_tools.ps1` — FFmpeg, Tesseract, Xray через WinGet;
  `scripts/check_install.py` — автономная проверка; `docs/INSTALLATION.md` — руководство.
- `start_gui.bat`, `app/launch.pyw` — запуск desktop; `scripts/windows/create_shortcut.bat`
  и `scripts/create_shortcut.ps1` — ярлыки в корне и на рабочем столе;
  `scripts/windows/start_debug.bat` — диагностика.
- `app/main.py`, `app/ui/qt_app.py` — единственная точка входа и Qt-интерфейс,
  фиксированный Comfortable, собственные QSettings.
  Фоновые настройки Warmup и сна — отдельная раскрывающаяся секция `Warmup / Sleep`.
- `toolbox.bat`, `scripts/toolbox.py` — меню вспомогательных desktop-утилит;
  `scripts/{sessions,accounts,profiles,network}/`, `scripts/README.md` — команды.
- `sessions/` — пользовательские входы, запускатели, список телефонов и памятка;
  `scripts/sessions/` — меню и вход; `session_logging.py` сохраняет ошибки всех
  команд в `data/logs/session_creation.log`.
- `config/requirements.txt`, `config/.env.example` — зависимости и пустой шаблон;
  пользовательский `.env` остаётся в корне.
- `app/core/project_layout.py`, `scripts/prepare_layout.py` — пустая структура данных;
  `imports/auth_input/` — очередь новой авторизации; `imports/` — обычный импорт.
- `assets/branding/uznik-multitool.ico` — иконка ярлыка и окна приложения.
- `app/core/desktop_identity.py` — Windows AppUserModelID для окна и ярлыка.
- `app/core/` — конфигурация, клиенты MTProto, сессии, хранение, задачи и UI-контракты.
- `app/core/private_storage.py` — шифрование плана профилей и DPAPI-ключ;
  `profile_customizer.load_profile_plan` — загрузка и миграция старых JSON.
- `app/modules/accounts.py`, `session_*.py`, `account_security.py`, `direct_access.py`
  — аккаунты, импорт, проверка, безопасность и Telegram Web.
- `app/modules/device_catalog.py`, `fingerprint_generator.py`, `auth_controller.py`
  — модели/ОС новой авторизации и постоянные параметры MTProto-подключений;
  `data/fingerprints.json` — локальное хранилище, не часть репозитория.
- `app/modules/email_inbox.py`, `email_mailbox.py`, `imports/emails/README.md`
  — HTTP / TLS IMAP / POP3, список ящиков и локальные привязки без паролей.
- `app/modules/profile_*.py`, `scrape_cache.py`, `app/utils/profile_generator.py`
  — парсинг, архив, применение, истории и генерация профилей.
- `app/modules/story_publication.py` — подтверждения публикаций отдельных медиа;
  `account_cleanup.py` — очистка привязок и браузерного состояния при удалении.
- `app/modules/resumable_jobs.py`, `local_backup.py` — возобновляемые пакеты
  и зашифрованные копии; секция `Tasks / Backups`, руководство `docs/RECOVERY.md`.
- `app/modules/giveaway_service.py`, `raffle_*.py` — desktop-участие в розыгрышах.
- `app/modules/proxy_manager.py`, `async_proxy_manager.py`, `vpn_gateway.py`
  — пользовательские прокси и необязательный локальный Xray.
- `app/modules/chat_actions.py`, `warmup_engine.py`, `ai_companion.py`, `scenarios.py`
  — действия, прогрев и сценарии; `online_mode.py`, `sleep_scheduler.py` — расписание.
  `data/warmup_limits.json` — локальный бюджет и cooldown прогрева;
  `warmup_settings.py` — политика и история; настройки и подтверждённые действия
  в `data/warmup_settings.json`, `data/warmup_history.json`.
  `data/sleep_zones.json` — зоны и настройки сна (старый формат поддерживается).
- `tests/TEST_MAP.md` — автономная проверка desktop-контракта и базовой логики.
- `.github/workflows/` — публичные проверки безопасности и исходные релизы;
  `scripts/releases/build_source_release.py`, `docs/RELEASES.md` — сборка из Git,
  контрольные суммы и происхождение; `SECURITY.md` — сообщения об уязвимостях.
- `data/`, `imports/`, `.env`, пользовательские шаблоны — локальные данные;
  в репозитории есть только пустая структура папок и памятки, без рабочих файлов.

Web, HTTP API, Bot API polling, deploy и удалённая синхронизация отсутствуют.
