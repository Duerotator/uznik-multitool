# Проверки desktop-редакции

Перед изменениями выберите самый узкий тест из `tests/`.

```powershell
.\.venv\Scripts\python.exe .\tests\run_checks.py fast
```

Runner запускает `unittest`: границы desktop-пакета, отсутствие готовых данных,
отключённые интеграции, независимая конфигурация, пути лаунчера, логика фильтров,
архива и привязок. Он не использует Telegram-сессии, прокси или сеть.

`tests/smoke_desktop.py` дополнительно открывает Qt в offscreen-режиме с пустой
временной базой, проверяет брендинг и Comfortable и закрывает окно. Команда:

```powershell
.\.venv\Scripts\python.exe .\tests\smoke_desktop.py
```

Перед коммитом выполните `fast` и smoke, если изменился GUI или запуск.

`test_recovery_workflows.py` — частичная публикация и перестановка историй,
устойчивый random_id, отказ от пустого результата/неподдерживаемого бэкенда,
старые счётчики, резервирование профиля при сбое, продолжение только исходных
незавершённых аккаунтов, отмена, очистка привязок/браузера и статусы TaskRunner.
`test_local_backup.py` — шифрование, пароль/подмена, проверка путей, SQLite WAL,
занятая база, перенос путей между установками, атомарная замена и откат ошибки.
Все данные фиктивные, только временные каталоги; без Telegram и реальных сессий.
Smoke также проверяет отдельную секцию Tasks / Backups и подписи кнопок.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_recovery_workflows.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_local_backup.py -v
```

Исходные релизы: `test_source_release.py` проверяет архив строго из Git-коммита,
отсутствие локальных секретов, отказ при отслеживаемых runtime-данных и ссылках,
повторяемость содержимого и SHA-256. Только временный Git-репозиторий:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_source_release.py -v
```

`.github/workflows/security.yml` — Bandit, аудит установленных зависимостей и
автономные тесты на чистом Windows runner; `codeql.yml` — статический анализ.
Эти проверки не используют пользовательские аккаунты или `.env`.

`test_security_privacy.py` — отсутствие API-ключей и текстов ответов в ошибках
Webshare, корректное распознавание hostname источника прокси. Только fake HTTP:
Также проверяет защищённый план, миграцию JSON, потерю ключа, подмену содержимого
и отсутствие телефона в ошибках входа; Windows DPAPI вызывается локально,
без Telegram или пользовательских данных.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_security_privacy.py -v
```

`test_sidebar_layout.py` проверяет перенос кнопок и сохранность обработчиков;
smoke проверяет полные подписи всех секций при обычной и минимальной ширине окна.

Почтовые коды: `test_email_mailbox.py` — форматы списка, уникальные закрепления,
свежие UID/UIDL, MIME/HTML, TLS, read-only IMAP/POP3, таймауты и порядок операций
в login/recovery email, зависший IMAP/POP3 worker, общий дедлайн смены email
и понятные ошибки отсутствующего сервера. `test_sidebar_layout.py` проверяет
локальную валидацию списка в GUI и порядок Started → Failed для быстрых ошибок.
Только fake-серверы, без настоящих ящиков и Telegram.

`test_session_coordination.py` — ожидание занятой сессии, сохранение чужой
блокировки при таймауте/отмене, освобождение сессии между циклами Warmup,
безопасная ошибка занятости при смене login/recovery email. Только fake-клиенты:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_session_coordination.py -v
```

`test_background_state.py` — границы сна (включая полночь), Unknown и ошибка зоны,
миграция/обновление расписания (включая одинаковые mtime/размер и замену во время чтения),
сброс к безопасным настройкам при удалении файла, согласованность фильтров, сохранение бюджета/cooldown,
остановка цикла при FloodWait, область Warmup и сценариев, отсутствие повторов Online,
одна ручная задача, защита прогресса от старых событий и сохранение ошибок при выходе.
Windows-проверка создаёт только временный `.lnk` и проверяет AppUserModelID.

`test_warmup_policy.py` — явные каналы и шаблоны, read-only по умолчанию,
часовой/суточный бюджет, конечные циклы, сохранение успехов без повторов,
недоступные каналы, таймаут запроса и остановка после трёх неудачных циклов.
Smoke дополнительно проверяет отдельную раскрывающуюся секцию Warmup / Sleep,
отсутствие дублей в Actions, настройки Warmup и сохранение без Telegram.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_warmup_policy.py -v
```

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_background_state.py -v
```

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_email_mailbox.py -v
```

Пути после реорганизации: исходники в `app/`, настройки установки в `config/`.
`test_desktop_package.py` проверяет импортный граф и переносимость запуска;
`smoke_desktop.py` загружает GUI из `app/` с временными данными.
`test_app_layout.py` проверяет новый путь лаунчера, загрузку `.env` из корня,
структуру пользовательских файлов и меню создания сессий без Telegram-запросов,
UTF-8 BOM, пустые переменные окружения, относительные пути и запись traceback.

Импорт / новая авторизация: `tests/test_session_onboarding.py` проверяет
исключение очереди из обычного импорта, свежесть кодов, прокси из конфигурации,
сохранность файлов, освобождение клиентов при ошибке / отмене. Проверяет прямой
вход без прокси, сохранение настройки явно заданного прокси, таймауты подключения
и запроса кода, а также использование прямых сессий обоими MTProto-бэкендами.
Только fake-клиенты:

`test_fingerprint_generator.py` — каталог телефонов, планшетов и компьютеров,
совместимость модели/ОС/языкового пакета, реальные версии MTProto-библиотек,
сохранение отпечатка авторизации при смене ключа телефон → ID аккаунта,
старые записи и согласованное сохранение несколькими генераторами.
Onboarding также проверяет передачу языков и использование настроенного data-dir.
Без Telegram и без чтения пользовательских сессий:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_fingerprint_generator.py -v
```

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_session_onboarding.py -v
```

Комплект утилит и папок: `tests/test_desktop_tools.py`; CLI проверяется через
`--help` из сторонней временной рабочей папки. Массовая авторизация и cleanup
на настоящих аккаунтах в эти проверки не входят.

Установка и зависимости: `test_installation.py` — покрытие внешних импортов
requirements, отсутствие конфликтующих провайдеров, проверка ошибок и успеха
диагностики, разбор и fake-выполнение PowerShell-установщика без установки программ.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_installation.py -v
.\.venv\Scripts\python.exe -X utf8 scripts/check_install.py
```

Вторая команда импортирует реальные зависимости, проверяет AES и открывает
пустой headless Chromium; не читает `.env`, аккаунты или сессии. Нативные
программы проверяются только командой версии, не запускающей сервисы.
Для обязательного наличия всех внешних программ добавьте `--require-native`.
