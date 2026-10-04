# Конфигурация и зависимости

- `.env.example` — пустой шаблон пользовательской конфигурации.
- `requirements.txt` — полный набор Python-зависимостей приложения:
  Kurigram, MTProto-ускорение, интерфейс, браузер, CPU OCR и Passkey.

Установка `setup.bat` читает эти файлы отсюда. Настройки пользователя по-прежнему
находятся в `.env` в корне проекта и не перезаписываются при обновлении.

```powershell
.\.venv\Scripts\python.exe -m pip install -r config/requirements.txt
```

Chromium, FFmpeg, Tesseract и Xray не являются Python-пакетами;
их установку выполняет `setup.bat`. См. [руководство](../docs/INSTALLATION.md).
Не устанавливайте оригинальный Pyrogram вместо Kurigram или вместе с ним.
