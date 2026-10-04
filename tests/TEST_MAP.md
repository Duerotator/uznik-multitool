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

`test_sidebar_layout.py` проверяет перенос кнопок и сохранность обработчиков;
smoke проверяет полные подписи всех секций при обычной и минимальной ширине окна.

Почтовые коды: `test_email_mailbox.py` — форматы списка, уникальные закрепления,
свежие UID/UIDL, MIME/HTML, TLS, read-only IMAP/POP3, таймауты и порядок операций
в login/recovery email. Только fake-серверы, без настоящих ящиков и Telegram.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_email_mailbox.py -v
```

Пути после реорганизации: исходники в `app/`, настройки установки в `config/`.
`test_desktop_package.py` проверяет импортный граф и переносимость запуска;
`smoke_desktop.py` загружает GUI из `app/` с временными данными.
`test_app_layout.py` проверяет новый путь лаунчера, загрузку `.env` из корня,
структуру пользовательских файлов и меню создания сессий без Telegram-запросов.

Импорт / новая авторизация: `tests/test_session_onboarding.py` проверяет
исключение очереди из обычного импорта, свежесть кодов, прокси из конфигурации,
сохранность файлов, освобождение клиентов при ошибке / отмене. Только fake-клиенты:

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
