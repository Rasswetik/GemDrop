# GemDrop

Flask Mini App с игрой «Мины», профилем, SQLite и импортом каталога Portal Market.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='токен вашего бота'
export SECRET_KEY='длинная случайная строка'
python app.py
```

Сайт отвечает на `http://localhost:5000`, но авторизация доступна только через Telegram Mini App: данные `initData` проверяются на сервере. Для тестирования через Telegram используйте HTTPS-туннель на локальный порт и укажите его URL у своего бота. На Render укажите свой репозиторий и используйте `render.yaml`. Диск `/opt/render/project/src/data` сохраняет `gemdrop.sqlite3` и `portal_gifts.json` после перезапусков. На бесплатном плане Render постоянный диск недоступен; не запускайте этот проект там, если нужно сохранять балансы.

Для других администраторов измените `ADMIN_IDS`, перечислив Telegram ID через запятую. Админ-панель появляется в профиле после подтверждения Telegram ID. В разделе Portal введите ключ доступа: поддерживаются полное значение `tma ...`, `Bearer ...` либо ключ, к которому автоматически добавляется `Bearer `. Импорт получает страницы `/api/collections` и сохраняет названия, минимальные цены и ссылки на изображения в `data/portal_gifts.json`; секрет в файл не записывается. Если Portal возвращает URL PNG, он сохраняется как PNG; если возвращает иной формат превью, сохраняется исходная ссылка без искусственной конвертации. Конкретные права и формат вашего ключа можно проверить только после ввода ключа в работающей админке.

Иконка TON находится в `static/img/ton.png`. Новому пользователю выделяется 10.00 демонстрационных игровых единиц; реальные платежи, подарки и вывод TON отсутствуют.

## Загрузка проекта на GitHub

Установите [Git](https://git-scm.com/downloads) и настройте вход в GitHub. Для **существующего** репозитория запустите в папке проекта:

```bash
python upload_to_github.py https://github.com/ВАШ_ЛОГИН/gemdrop.git
```

Чтобы **создать новый** закрытый репозиторий, установите [GitHub CLI](https://cli.github.com/), выполните `gh auth login`, затем:

```bash
python upload_to_github.py --create gemdrop
```

Для открытого репозитория добавьте `--public`. В Windows CMD используйте `upload_to_github.bat --create gemdrop` или `upload_to_github.bat https://github.com/ВАШ_ЛОГИН/gemdrop.git`. Пишите обычный URL без квадратных скобок и круглых скобок Markdown. Запускатель `.bat` использует `py -3`: команда `py -V` должна показать версию Python. Скрипт загружает только исходники проекта; токен бота, файлы `.env`, базу данных, JSON каталога и архивы не добавляет в коммит. Если в Git не указаны имя и email, задайте их командами `git config --global user.name "Имя"` и `git config --global user.email "email@example.com"`.
