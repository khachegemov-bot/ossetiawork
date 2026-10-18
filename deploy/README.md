# Настройки сервера

Копии системных файлов, чтобы сервер можно было поднять заново, не вспоминая
по памяти. Секретов здесь нет: токен бота лежит только на сервере,
в `/etc/ossetia-work/secrets.env` (права 600, владелец root), строкой `BOT_TOKEN=...`.

| Файл | Куда кладётся |
|---|---|
| `flaskbot.service` | `/etc/systemd/system/flaskbot.service` |
| `nginx-ossetiawork.conf` | `/etc/nginx/sites-available/default` |
| `sshd-01-ossetia-hardening.conf` | `/etc/ssh/sshd_config.d/01-ossetia-hardening.conf` |
| `fail2ban-jail.local` | `/etc/fail2ban/jail.local` |
| `fail2ban-filter-nginx-probe.conf` | `/etc/fail2ban/filter.d/nginx-probe.conf` |
| `backup_db.sh`, `backup_to_telegram.sh` | `/root/` |
| `crontab-root.txt` | `crontab crontab-root.txt` от root |

Приложение: `/srv/ossetia-work`, владелец — системный пользователь `ossetia`
без права входа. Файрвол: `ufw` — открыты только 22, 80, 443.
