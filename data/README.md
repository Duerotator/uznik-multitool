# Локальные данные

Здесь приложение создаёт свои рабочие сессии, базу аккаунтов, группы, кэш,
привязки профилей, архивы, Passkey и логи. В Git сохраняется только пустая
структура папок и этот файл. Рабочие данные не публикуются.

- `sessions/pyrogram`, `sessions/telethon` — рабочие авторизации.
- `sessions/archive` — резервные старые сессии.
- Входной список номеров находится в `../sessions/batch_phones.txt` (от корня
  проекта — `sessions/batch_phones.txt`);
  готовые сессии хранятся в этих подпапках `data/sessions/`.
- `passkeys` — приватные ключи восстановления (как пароль).
- `profiles_archive`, `profile_snapshots` — сохранённые профили и снимки.
- `browser_profiles` — постоянные данные Telegram Web.
- `groups`, `scenarios`, `logs`, `backups` — пользовательское состояние.
- `vpn_gateway` — конфигурация своего локального Xray.
- `email_mailbox_bindings.json` — закрепления почтовых ящиков за аккаунтами,
  без паролей; список с паролями находится в `imports/emails/` от корня проекта.
- `logs/current_session.log` — действия и ошибки текущего запуска GUI;
  `logs/session_creation.log` — ошибки команд создания сессий.
