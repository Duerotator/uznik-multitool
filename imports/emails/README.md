# Список почтовых ящиков

Поместите свои ящики в `accounts.txt` или выберите другой UTF-8 файл через
**Security → Email code source → IMAP mailboxes / POP3 mailboxes → Browse**.
Для HTTP Inbox API файл не нужен; настройки API остаются в `.env`.

Одна строка — один ящик. Рекомендуемый формат:

```text
example@rambler.ru:APP_PASSWORD;imap.rambler.ru;993
example@mail.ru:APP_PASSWORD;imap.mail.ru;993
```

Для POP3 выберите POP3 в интерфейсе и используйте POP-сервер / TLS-порт:

```text
example@rambler.ru:APP_PASSWORD;pop.rambler.ru;995
example@mail.ru:APP_PASSWORD;pop.mail.ru;995
```

Также поддерживаются `email:password` (сервер из `.env` или известного домена),
`email:password@imap.rambler.ru`, `email:password@pop.rambler.ru:995`.
При неоднозначном пароле, содержащем `;` или суффикс `@imap...`, используйте JSONL:

```json
{"email":"example@your-domain.test","login":"example@your-domain.test","password":"APP_PASSWORD","host":"mail.your-provider.test","port":993,"folder":"INBOX"}
```

Для Firstmail и других провайдеров укажите сервер из их инструкции явно:
название почтового домена не всегда совпадает с сервером. Используется только
TLS с проверкой сертификата, без незащищённого подключения и без SMTP.

Адрес ящика закрепляется за аккаунтом в `data/email_mailbox_bindings.json`.
Пароли в этот файл не записываются. Повторный запуск использует тот же ящик;
после ошибки резерв сохраняется, поскольку Telegram мог успеть применить адрес.
Не удаляйте привязки без проверки состояния аккаунта. Пул учитывает также
уже сохранённые login/recovery emails других аккаунтов.

Перед запросом кода снимается снимок UID/UIDL: существующие письма игнорируются.
Читаются только новые письма от `telegram.org` с нужной длиной кода.
IMAP работает readonly / BODY.PEEK; POP3 не отправляет DELE. Письма не удаляются.
По умолчанию проверяется INBOX; другую IMAP-папку можно указать в JSONL или `.env`.

Включите доступ почтовых программ в настройках провайдера. Для Mail.ru нужен
[пароль внешнего приложения](https://help.mail.ru/mail/login/mailer/), а не
обычный пароль сайта. Настройки Rambler:
[официальная инструкция](https://help.rambler.ru/mail/mail-pochtovye-klienty/1275/).

Файл содержит секреты: не публикуйте его и не прикладывайте к логам.
Содержимое `imports/emails/`, кроме этой памятки и `.gitkeep`, исключено из Git.
