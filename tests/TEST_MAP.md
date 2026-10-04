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
