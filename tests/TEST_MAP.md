# Проверки desktop-редакции

Перед изменениями выберите самый узкий тест из `tests/` или запустите полный прогон:

```powershell
.\.venv\Scripts\python.exe .\tests\run_checks.py fast
.\.venv\Scripts\python.exe .\tests\run_checks.py unit
```

Режим `fast` запускает все автономные `unittest`-проверки (`tests/test_*.py` и
`tests/unit/test_*.py`); режим `unit` — только модульные тесты из `tests/unit/`.
Проверки контролируют границы desktop-пакета, отсутствие готовых данных,
отключённые интеграции, независимую конфигурацию, пути лаунчера, логику фильтров,
архива, привязок, финансового хранилища, WalletTg-кодека и MarketApp-клиента.
Они не используют реальные Telegram-сессии, кошельки, прокси или сеть.

`tests/smoke_desktop.py` дополнительно открывает Qt в offscreen-режиме с пустой
временной базой, проверяет брендинг и базовый макет и закрывает окно. Команда:

```powershell
.\.venv\Scripts\python.exe .\tests\smoke_desktop.py
```

Перед коммитом выполните `fast` и smoke, если изменился GUI или запуск.

## Основные проверки в `tests/`

- `test_recovery_workflows.py` — частичная публикация и перестановка историй,
  устойчивый `random_id`, отказ от пустого результата/неподдерживаемого бэкенда,
  старые счётчики, резервирование профиля при сбое, продолжение только исходных
  незавершённых аккаунтов, отмена, очистка привязок/браузера и статусы `TaskRunner`.
- `test_local_backup.py` — шифрование, пароль/подмена, проверка путей, SQLite WAL,
  занятая база, перенос путей между установками, атомарная замена и откат ошибки.
  Все данные фиктивные, только временные каталоги; без Telegram и реальных сессий.
- `test_source_release.py` — архив строго из Git-коммита, отсутствие локальных
  секретов, отказ при отслеживаемых runtime-данных (`_local/`, `node_modules/`,
  `unknown_errors.txt`, `.env*`, ссылки), повторяемость содержимого и SHA-256.
- `test_security_privacy.py` — отсутствие API-ключей и текстов ответов в ошибках
  Webshare, распознавание hostname источника прокси, защищённый план, миграция JSON,
  потеря ключа, подмена содержимого, отсутствие телефона в ошибках входа и локальный
  Windows DPAPI без Telegram.
- `test_sidebar_layout.py` — перенос кнопок, сохранность обработчиков, локальная
  валидация почтового списка и порядок `Started -> Failed` для быстрых ошибок.
- `test_email_mailbox.py` — форматы списка ящиков, уникальные закрепления,
  свежие UID/UIDL, MIME/HTML, TLS, read-only IMAP/POP3, таймауты, зависший worker,
  общий дедлайн смены email и fake-серверы без настоящих ящиков.
- `test_session_coordination.py` — ожидание занятой сессии, сохранение чужой
  блокировки при таймауте/отмене, освобождение сессии между циклами Warmup и
  безопасная ошибка занятости при смене email (только fake-клиенты).
- `test_background_state.py` — границы сна, `Unknown` и ошибка зоны, обновление
  расписания, сброс при удалении файла, фильтры, бюджет/cooldown, остановка при
  FloodWait, область Warmup и сценариев, одна ручная задача, защита прогресса и
  сохранение ошибок при выходе, а также AppUserModelID на временном `.lnk`.
- `test_warmup_policy.py` — явные каналы и шаблоны, read-only по умолчанию,
  часовой/суточный бюджет, конечные циклы, сохранение успехов без повторов,
  недоступные каналы, таймаут запроса и остановка после трёх неудачных циклов.
- `test_desktop_package.py` — границы desktop-пакета, отсутствие серверных модулей,
  полнота first-party импортов в `app/` и `scripts/`, пустая конфигурация,
  переносимость лаунчеров, правила `.gitignore` и запрет на личные VPN/2FA данные.
- `test_app_layout.py` — путь лаунчера, загрузка `.env` из корня, структура
  пользовательских файлов, UTF-8 BOM, относительные пути и запись traceback.
- `test_session_onboarding.py` — исключение очереди `auth_input` из обычного импорта,
  свежесть кодов, прокси из конфигурации, таймауты и оба MTProto-бэкенда (fake).
- `test_fingerprint_generator.py` — каталог устройств, совместимость модели/ОС/языка,
  версии MTProto-библиотек, сохранение отпечатка при смене ключа и конкурентная запись.
- `test_desktop_tools.py` — структура папок, правила `.gitignore`, `--help` утилит
  из сторонней рабочей папки и сценарии `scripts/{sessions,accounts,profiles}/`.
- `test_installation.py` — покрытие внешних импортов `config/requirements.txt`,
  отсутствие конфликтующих провайдеров, диагностика и fake-проверка PowerShell-установщика.
- `test_desktop_features.py` — базовые проверки desktop-функций.

## Модульные проверки в `tests/unit/`

Все тесты в `tests/unit/` используют синтетические фикстуры (включая публичный
контракт `tests/fixtures/wallet_tg_rev00.b64`), временные каталоги, `MockTransport`
и fake-клиенты — без реальных аккаунтов, RPC-транзакций и паролей:

- `test_phase2_fixes.py` — регрессионные проверки Phase 2
  (редакция секретов в логах, DPAPI-шифрование passkey, `FloodWait` и ссылки
  розыгрышей, `OperationProgress.indeterminate`, кириллические сценарии,
  сохранение сессий frozen-аккаунтов, защита от перезаписи `WalletTg`,
  fail-closed VPN-шлюз, парсинг писем/SpamBot и `SecretVault`).
- `test_giveaway_service.py` — распознавание провайдеров розыгрышей и участие.
- `test_passkey_restore.py` — восстановление сессий через Passkey и область выборки.
- `test_profile_apply.py` — применение профилей, рецепты, привязки и публикации историй.
- `test_financial_vault.py`, `test_financial_backup.py` — шифрование DPAPI, контекст
  записей, портативные резервные копии и CLI `scripts/manage_financial_vault.py`.
- `test_marketapp_catalog.py` — парсинг, фильтрация и CLI `scripts/marketapp_catalog.py`.
- `test_wallet_tg_codec.py`, `test_wallet_tg_custody.py` — Node/@ton/core кодек
  (`app/ui/finance/wallet_tg_codec.cjs`), подписи, ротация и хранение мнемоник.
- `test_money_policy.py`, `test_money_credit.py`, `test_money_funding.py`,
  `test_money_chain_read.py`, `test_money_emulation.py`, `test_money_deployment.py`,
  `test_money_execution.py`, `test_money_native.py`, `test_money_reconciliation.py`,
  `test_money_recovery.py`, `test_money_rewards.py`, `test_native_money_workspace.py`
  — политика лимитов, чтение/эмуляция транзакций, одноразовые claims, сверка и
  защита выполнения нативных переводов.
- `test_money_carousel.py`, `test_money_carousel_run.py`,
  `test_money_carousel_workspace.py` — планирование, запуск и возобновление
  карусели пополнения.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests/unit -v
.\.venv\Scripts\python.exe -X utf8 scripts/check_install.py
```

Последняя команда импортирует реальные зависимости, проверяет ускорение MTProto
и открывает пустой headless Chromium; не читает `.env`, аккаунты или сессии.
Для обязательного наличия всех внешних программ добавьте `--require-native`.
