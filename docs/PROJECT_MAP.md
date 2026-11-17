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
- `app/ui/*_dialog.py` — диалоги desktop-операций: `vault_dialog.py` (SecretVault),
  `guard_dialog.py` (Security Guard), `task_history_dialog.py`, `profile_studio_dialog.py`,
  `profile_review_dialog.py`, `captcha_support_dialog.py`, `marketapp_dialog.py`,
  `marketapp_credentials_dialog.py`, `asset_dialog.py`, `native_money_dialog.py`,
  `carousel_funding_dialog.py`, `wallet_x_dialog.py` (Master Wallet X).
- `app/ui/finance/` — Node.js / `@ton/core` кодек `wallet_tg_codec.cjs` и сборка
  TON Connect / UI-активов (`package.json`, `package-lock.json`, `index.js`, `dist/money.js`).
- `toolbox.bat`, `scripts/toolbox.py` — меню вспомогательных desktop-утилит;
  `scripts/{sessions,accounts,profiles,network}/`, `scripts/manage_financial_vault.py`,
  `scripts/marketapp_catalog.py`, `scripts/README.md` — команды.
- `sessions/` — пользовательские входы, запускатели, список телефонов и памятка;
  `scripts/sessions/` — меню и вход; `session_logging.py` сохраняет ошибки всех
  команд в `data/logs/session_creation.log`.
- `config/requirements.txt`, `config/.env.example` — зависимости и пустой шаблон;
  пользовательский `.env` остаётся в корне.
- `app/core/project_layout.py`, `scripts/prepare_layout.py` — пустая структура данных;
  `imports/auth_input/` — очередь новой авторизации; `imports/` — обычный импорт.
- `assets/branding/uznik-multitool.ico` — иконка ярлыка и окна приложения.
- `app/core/` — конфигурация (`config.py`), клиенты MTProto (`telegram_client.py`,
  `telegram_connection.py`), сессии (`session_manager.py`), хранение (`storage.py`,
  `private_storage.py`, `secret_vault.py`, `windows_dpapi.py`), задачи (`task_runner.py`,
  `task_history.py`, `results.py`), TON-адреса (`ton_address.py`), UI-контракты и
  диагностика (`design_tokens.py`, `desktop_identity.py`, `diagnostics.py`, `logging_setup.py`,
  `ui_feedback.py`, `ui_logs.py`, `ui_operations.py`, `ui_progress.py`, `ui_state.py`),
  Passkey (`passkey_service.py`).
- `app/modules/accounts.py`, `session_*.py`, `account_security.py`, `account_secrets.py`,
  `security_guard.py`, `guard_tasks.py`, `direct_access.py` — аккаунты, импорт, проверка,
  безопасность, хранилище 2FA-секретов, аудит сессий и Telegram Web.
- `app/modules/device_catalog.py`, `fingerprint_generator.py`, `auth_controller.py`
  — модели/ОС новой авторизации и постоянные параметры MTProto-подключений;
  `data/fingerprints.json` — локальное хранилище, не часть репозитория.
- `app/modules/email_inbox.py`, `email_mailbox.py`, `imports/emails/README.md`
  — HTTP / TLS IMAP / POP3, список ящиков и локальные привязки без паролей.
- `app/modules/profile_*.py` (`profile_archive.py`, `profile_bindings.py`,
  `profile_customizer.py`, `profile_scraper.py`, `profile_snapshots.py`,
  `profile_apply.py`, `profile_studio.py`, `profile_story_review.py`),
  `story_publication.py`, `story_review_transport.py`, `gift_profile_transport.py`,
  `scrape_cache.py`, `app/utils/{profile_generator,name_generator}.py`
  — парсинг, архив, студия/применение профилей, учёт историй и рецепты (`data/profile_recipes.sqlite3`).
- `account_cleanup.py` — очистка привязок и браузерного состояния при удалении.
- `app/modules/resumable_jobs.py`, `local_backup.py` — возобновляемые пакеты
  и зашифрованные копии; секция `Tasks / Backups`, руководство `docs/RECOVERY.md`.
- `app/modules/giveaway_service.py`, `raffle_*.py`, `captcha_support.py`
  — desktop-участие в розыгрышах и поддержка ручных/OCR капч.
- `app/modules/marketapp_*.py` (`marketapp_catalog.py`, `marketapp_credentials.py`,
  `marketapp_browser.py`, `marketapp_rentals.py`), `rent_orchestrator.py` — локальный
  каталог подарков и юзернеймов MarketApp, выделенное хранилище токена и возобновляемый
  конвейер аренды (`data/rent_jobs.sqlite3`).
- `app/modules/financial_vault.py`, `financial_backup.py`, `asset_registry.py`,
  `ton_readonly.py`, `wallet_tg_codec.py`, `wallet_tg_custody.py`, `wallet_x.py`, `wordlist_en.txt`,
  `finance_money.py`, `money_*.py` (`money_workspace.py`, `money_policy.py`,
  `money_native.py`, `money_native_tl.py`, `money_native_transport.py`,
  `money_send_transport.py`, `money_chain_read.py`, `money_emulation.py`,
  `money_execution.py`, `money_credit.py`, `money_funding.py`, `money_transit.py`,
  `money_reconciliation.py`, `money_recovery.py`, `money_rewards.py`,
  `money_carousel.py`, `money_carousel_run.py`, `money_carousel_workspace.py`)
  — локальный финансовый vault, независимый Master Wallet X (`data/financial_keys/wallet_x.sqlite3`,
  `data/wallet_x.sqlite3`), read-only реестр TON-активов, кодек WalletTg, нативные переводы,
  сверка/восстановление и карусель пополнения (включая старт/возврат через Wallet X).
- `app/modules/proxy_manager.py`, `async_proxy_manager.py`, `vpn_gateway.py`,
  `webshare_source.py` — пользовательские прокси и необязательный локальный Xray.
- `app/modules/chat_actions.py`, `warmup_engine.py`, `warmup_policy.py`,
  `warmup_settings.py`, `ai_companion.py`, `scenarios.py`, `online_mode.py`,
  `sleep_scheduler.py` — действия, прогрев, политика/история и расписание.
- `tests/TEST_MAP.md` — автономная проверка desktop-контракта, модулей и финансовых
  сценариев (`tests/test_*.py` и `tests/unit/test_*.py`).
- `.github/workflows/` — публичные проверки безопасности (Bandit, TruffleHog, Python/Node
  аудит, тесты), CodeQL (`python` + `javascript-typescript`) и исходные релизы;
  `scripts/releases/build_source_release.py`, `docs/RELEASES.md` — сборка из Git,
  контрольные суммы и происхождение; `SECURITY.md` — сообщения об уязвимостях.
- `data/`, `imports/`, `.env`, `_local/`, пользовательские шаблоны — локальные данные;
  в репозитории есть только пустая структура папок и памятки, без рабочих файлов.

Web, HTTP API, Bot API polling, deploy и удалённая синхронизация отсутствуют.
