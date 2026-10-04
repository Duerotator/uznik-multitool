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

Импорт / новая авторизация: `tests/test_session_onboarding.py` проверяет
исключение очереди из обычного импорта, свежесть кодов, прокси из конфигурации,
сохранность файлов, освобождение клиентов при ошибке / отмене. Только fake-клиенты:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_session_onboarding.py -v
```

Комплект утилит и папок: `tests/test_desktop_tools.py`; CLI проверяется через
`--help` из сторонней временной рабочей папки. Массовая авторизация и cleanup
на настоящих аккаунтах в эти проверки не входят.
