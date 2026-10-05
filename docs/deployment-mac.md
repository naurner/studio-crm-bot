# Установка на домашний Mac

Сервис — локальный процесс на всегда включённом Mac владельца (см.
дизайн-документ, §2 «Хостинг»). Терминал нужен только при первой установке,
дальше всё управление — из Telegram (🖥 Хост).

Два способа держать бота включённым. Оба дают автозапуск и кнопку «🔄 Обновить
с GitHub» в боте:

- **Docker** — ничего не ставится в систему, кроме Docker Desktop; Python и
  библиотеки живут в контейнере. Удобно, если захочется перенести бота на
  сервер.
- **launchd** — родной для macOS автозапуск, без Docker. Нужен Python из
  Homebrew.

Держать оба сразу нельзя: получится два бота на один токен, и Telegram будет
отдавать сообщения то одному, то другому.

## 1. Общая часть

```bash
brew install git gh
gh auth login
cd ~
git clone git@github.com:naurner/CRM_Lumi.git crm-lumi
cd crm-lumi
```

**Папка проекта — в домашней директории (`~/crm-lumi`), а не в «Документах».**
Docker Desktop не читает содержимое файлов в `~/Documents`: размеры он видит,
а чтение обрывается с `OSError: [Errno 5] Input/output error`, и контейнер
уходит в цикл перезапуска (мешают защита macOS для «Документов» и синхронизация
с iCloud). Если проект уже там — перенести и запустить заново:

```bash
cd ~/Documents/crm-lumi && docker compose down && mv ~/Documents/crm-lumi ~/crm-lumi && cd ~/crm-lumi && docker compose up -d
```

**Секреты** в git не хранятся, переносятся на Mac отдельно (AirDrop, флешка):
JSON-ключ сервисного аккаунта Google → `~/crm-lumi/config/service_account.json`.
`config.yaml` и `.env` создают мастера настройки.

## 2. Вариант A: Docker

Поставить Docker Desktop (https://www.docker.com/products/docker-desktop/),
запустить его и в настройках включить «Start Docker Desktop when you sign in» —
иначе после перезагрузки Mac контейнер не поднимется.

Настройка (мастера запускаются в контейнере, отвечать на вопросы — как обычно):

```bash
cd ~/crm-lumi && docker compose run --rm crmlumi python -m crmlumi setup
```

```bash
cd ~/crm-lumi && docker compose run --rm crmlumi python -m crmlumi todo-setup
```

`todo-setup` можно пропустить — бот работает и без To Do.

Запуск:

```bash
cd ~/crm-lumi && docker compose up -d
```

Дальше терминал не нужен: контейнер стартует вместе с Docker Desktop и
поднимается после падения (`restart: unless-stopped`).

**Часовой пояс.** По нему считаются вечерние расписания фотографам, тихие часы
и напоминания за 2 часа до съёмки. По умолчанию `Asia/Bishkek`; поменять —
дописать строку в `.env` рядом с токенами, например `TZ=Europe/Moscow`, и
перезапустить: `docker compose up -d`.

Полезное:

```bash
docker compose logs -f          # живой лог
docker compose restart          # перезапуск
docker compose down             # остановить бота
docker compose up -d --build    # пересобрать образ (после смены зависимостей)
```

## 3. Вариант B: launchd (без Docker)

```bash
brew install python
cd ~/crm-lumi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Ставить именно `pip install .`, без `-e`: editable-установка на Python 3.14
из Homebrew не находит пакет (`No module named crmlumi`).

```bash
python -m crmlumi setup
python -m crmlumi todo-setup
python -m crmlumi install-service
```

`install-service` сам пишет `~/Library/LaunchAgents/com.crmlumi.run.plist` и
запускает бота в фоне. Он поднимается после перезагрузки и после падения.
Логи — `~/crm-lumi/logs/`, их хвост виден в боте: 🖥 Хост → 📜 Лог.

Если бот в этот момент запущен вручную в другом окне терминала (`run`), команда
попросит сначала остановить его, чтобы не было двух ботов сразу.

Часовой пояс берётся из системного — отдельно настраивать нечего.

## 4. Обновление

В Telegram: 🖥 Хост → 🔄 Обновить с GitHub.

- **launchd:** бот делает `git pull`, `pip install .` и перезапускается сам.
- **Docker:** бот делает `git pull` и перезапускается; код берётся из папки
  проекта, поэтому пересбор не нужен. Если в обновлении изменился
  `pyproject.toml` (новые библиотеки), бот не перезапустится, а попросит
  выполнить на Mac `docker compose up -d --build` — зависимости лежат в образе.

Если обновление из бота не прошло (например, из фонового процесса нет доступа к
GitHub по SSH), то же вручную:

```bash
cd ~/crm-lumi && git pull && docker compose up -d --build
```

```bash
cd ~/crm-lumi && git pull && .venv/bin/python -m pip install . && launchctl kickstart -k gui/$(id -u)/com.crmlumi.run
```

## 5. Выключить автозапуск

```bash
cd ~/crm-lumi && docker compose down
```

```bash
cd ~/crm-lumi && .venv/bin/python -m crmlumi uninstall-service
```

## 6. Переезд с launchd на Docker

Сначала выключить старый автозапуск, иначе будут два бота:

```bash
cd ~/crm-lumi && .venv/bin/python -m crmlumi uninstall-service && docker compose up -d
```

`config/`, `.env` и база `data/crmlumi.sqlite` остаются на месте — контейнер
видит ту же папку, так что заказы, роли и настройки не потеряются.
