# Установка Uznik MultiTool

## Установка в один запуск

Нужны Windows 10/11 x64, интернет, стандартный CPython 3.12–3.14 x64,
WinGet (компонент Microsoft App Installer) и Node.js 18+ (рекомендуется
24+ LTS — для кодека WalletTg и сборки TON-активов). Рекомендуется Python 3.14;
free-threaded и 32-битные сборки не входят в проверенную конфигурацию.

Дважды нажмите `setup.bat`. Он:

1. Создаст `.venv` в папке проекта, не меняя системные Python-пакеты.
2. Установит `config/requirements.txt` и проверит совместимость зависимостей.
3. Установит npm-зависимости `app/ui/finance/` (`npm ci`) и соберёт `dist/money.js` (`npm run build`).
4. Скачает Chromium для Playwright.
5. Установит отсутствующие FFmpeg, Tesseract и Xray через WinGet.
6. Создаст `.env` из шаблона, только если файла ещё нет.
7. Проверит импорты, MTProto-ускорение, запуск пустого браузера и внешние программы.
8. Создаст ярлыки в папке проекта и на рабочем столе.

Нативные программы устанавливаются средствами Windows Package Manager и
могут потребовать подтверждения Windows. Уже найденные программы установщик
не переустанавливает. Xray не запускается: gateway требует собственной
конфигурации и отдельного включения в приложении.

Python-пакеты загружаются из PyPI, npm-пакеты кодека — из npm (`@ton/core` и
`@tonconnect/*`), браузер — средствами Playwright, нативные программы — из
каталога WinGet (ID указаны ниже). Скрипт запускается с `-ExecutionPolicy Bypass`
только для своего процесса PowerShell; постоянная политика Windows не меняется.
Служба приложения не устанавливается. Изменения касаются окружения проекта,
новых конфигурационных файлов, ярлыков и выбранных внешних программ.
Подробнее: [данные и безопасность](SECURITY_AND_DATA.md).

Проверка установки не загружает аккаунты, `.env` или сессии и не входит
в Telegram. Данные и существующие настройки не перезаписываются.

## Ручная установка

Команды выполняются в PowerShell из корня распакованного проекта:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r config/requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m playwright install chromium
npm --prefix app/ui/finance ci
npm --prefix app/ui/finance run build
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/install_native_tools.ps1
```

При использовании Python 3.12 или 3.13 замените `-3.14` в первой команде.
Активация виртуального окружения не обязательна: команды явно используют его Python.
Установите [Node.js LTS](https://nodejs.org/) (v24+), чтобы работали финансовый
кодек `app/ui/finance/wallet_tg_codec.cjs` и сборка `dist/money.js`.

Скопируйте `config/.env.example` в `.env`, **только если `.env` ещё нет**,
заполните свои Telegram API-ключи. `TELEGRAM_GLOBAL_PROXY` необязателен:
без прокси разрешено подключение с вашего IP-адреса. Если Telegram недоступен
из вашей сети, укажите рабочий прокси или добавьте и проверьте его в интерфейсе
приложения. Явно заданный неисправный прокси не обходится прямым подключением.
Завершите настройку:

```powershell
.\.venv\Scripts\python.exe scripts/prepare_layout.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_install.py --require-native
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/create_shortcut.ps1
```

Для управляемого компьютера, где внешние программы устанавливает администратор:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1 -SkipNativeTools
```

Этот режим устанавливает Python-пакеты и Chromium, но **не гарантирует
готовность функций, которым нужны FFmpeg, Tesseract или Xray**.
После ручной установки повторите проверку с `--require-native`.
Ключ `-NoOpenConfig` отключает открытие Блокнота.

## Какие зависимости используются

Все Python-зависимости устанавливаются одной командой `pip install -r config/requirements.txt`.
Транзитивные пакеты pip устанавливает автоматически.

| Область | Зависимости |
| --- | --- |
| Telegram / MTProto | Kurigram, kuricrypt, Telethon, cryptg |
| Интерфейс | PySide6 |
| Настройки и диагностика | python-dotenv, psutil, tzdata |
| HTTP, прокси, скачивание | httpx, python-socks, pysocks, gdown |
| Браузер | Playwright + отдельная загрузка Chromium |
| Изображения и QR | Pillow, NumPy, zxing-cpp |
| Локальный OCR | ddddocr, CPU ONNX Runtime, OpenCV |
| Passkey и шифрование | cbor2, cryptography |
| TON / WalletTg (Node.js) | Node.js 24+, `@ton/core`, `@tonconnect/sdk`, `@tonconnect/ui`, `buffer`, `esbuild` |
| Внешние исполняемые программы | FFmpeg, Tesseract, Xray |

На Windows устанавливается `opencv-python`; на Linux —
`opencv-python-headless`. Не устанавливайте оба провайдера `cv2` одновременно.
GPU/CUDA для штатного OCR не нужны. Дополнительный SDK SolveCaptcha не нужен:
приложение использует HTTP API напрямую, если задан собственный ключ.

### Kurigram — не оригинальный Pyrogram

Telegram-клиент — **[Kurigram](https://pypi.org/project/Kurigram/)**.
Название модуля остаётся `pyrogram` для совместимости; это не означает, что
нужно устанавливать пакет `Pyrogram`. Версия клиента закреплена в requirements.

Для ускорения MTProto устанавливаются **kuricrypt** (для Kurigram) и
**cryptg** (для Telethon) с готовыми Windows x64 wheels, в том числе
для CPython 3.14. Это криптографические расширения, а не замена Kurigram.
Установка требует готовый wheel и не пытается компилировать расширения из исходников.

Не добавляйте оригинальные `Pyrogram`, `pyrofork`, `TgCrypto` или `TgCrypto-pyrofork`
в это окружение: они могут перекрыть используемые модули. Установщик обнаруживает
конфликт и останавливается, ничего автоматически не удаляя.
Если конфликт уже появился, закройте приложение и **в окружении этого проекта**:

```powershell
.\.venv\Scripts\python.exe -m pip uninstall Pyrogram pyrofork TgCrypto TgCrypto-pyrofork
.\.venv\Scripts\python.exe -m pip install --force-reinstall -r config/requirements.txt
```

Затем повторите `setup.bat`. Не выполняйте эти команды в окружении другого проекта.

### Внешние программы

Установщик использует точные идентификаторы каталога WinGet:

```powershell
winget install --id Gyan.FFmpeg --exact --source winget
winget install --id UB-Mannheim.TesseractOCR --exact --source winget
winget install --id XTLS.Xray-core --exact --source winget
```

При ручной установке Node.js, FFmpeg и Xray должны быть доступны в `PATH`.
Tesseract также находится в стандартном каталоге `Program Files/Tesseract-OCR`.
Для нестандартного расположения Xray укажите `XRAY_EXECUTABLE` в `.env`.
После изменения `PATH` закройте и заново откройте окно терминала / папку проекта.

## Проверка и устранение проблем

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/check_install.py --require-native
.\.venv\Scripts\python.exe scripts/network/check_setup.py
```

Первая команда проверяет установку, вторая — наличие настроек без вывода секретов.
Обе не подключаются к Telegram. Проверка без `--require-native` допускает
отсутствие внешних программ, но показывает их состояние.

- **Нет Python / несовместимая версия:** установите CPython 3.12–3.14 x64.
  Если `.venv` уже создана другой версией, сохраните её под другим именем и
  повторите установку. Аккаунты находятся в `data/`, не в `.venv`.
- **Нет Node.js или `node_modules` в `app/ui/finance/`:** установите Node.js 24+ LTS
  и выполните `npm --prefix app/ui/finance ci`. Без этого кодек WalletTg недоступен.
- **WinGet недоступен:** установите или обновите Microsoft App Installer либо
  установите внешние программы вручную.
- **Ошибка загрузки пакета:** проверьте доступ к PyPI, интернет и настройки
  системного прокси; не удаляйте данные аккаунтов.
- **Chromium отсутствует:** выполните `.venv\Scripts\python.exe -m playwright install chromium`.
- **OCR / ONNX не импортируется:** проверьте разрядность Python и повторите
  установку зависимостей. Не смешивайте GPU и CPU варианты ONNX Runtime.
- **Окно приложения не открывается:** запустите `scripts/windows/start_debug.bat`.
  Журналы находятся в `data/logs/`.
- **Перемещена папка проекта:** пересоздайте ярлык через
  `scripts/windows/create_shortcut.bat`.

Ошибки подключения аккаунтов, лимиты Telegram и ответы внешних сервисов
проверяются отдельно: успешная установка не означает успешную авторизацию.
