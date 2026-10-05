"""Командная строка CRM Lumi."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import typer
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from rich.console import Console


def _force_utf8_output() -> None:
    """Консоль Windows по умолчанию не в UTF-8, и кириллица роняет вывод.

    Переключаем потоки до того, как typer или rich что-либо напечатают.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_force_utf8_output()

from .config import DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH, load_settings
from .db import Database
from .google_sheets import GspreadSheetClient
from .host import RuntimeState, current_commit, is_supervised, runtime_kind
from .models import FIELD_COLUMNS, SHOOT_COLUMNS, SHOOTS_WORKSHEET
from .notify.claim_messages import broadcast_shoot_offer
from .notify.reminders import notify_shoot_changes, run_reminders
from .notify.telegram import (
    build_router,
    format_daily_digest,
    notify_new_lead,
    notify_stage_role,
    notify_startup,
    send_daily_digest,
    sync_bot_commands,
)
from .pipeline.daily_checks import (
    find_overdue_tasks,
    find_shoots_without_photographer_near_date,
    find_stalled_orders,
)
from .pipeline.router import role_for_event
from .pipeline.shoots import migrate_legacy_shoots, reconcile_orders_with_shoots, shoots_to_offer
from .pipeline.tasks import complete_finished_tasks, sync_shoot_tasks, sync_stage_tasks
from .roles import ROLE_NEW_LEADS, ROLE_PHOTOGRAPHER, role_names
from .service import (
    LAUNCHD_LABEL,
    LOG_DIR_NAME,
    build_launchd_plist,
    err_log_path,
    plist_path,
)
from .setup_wizard import write_config, write_env, write_microsoft_config
from .sheet_source import poll_once, poll_shoots
from .timefmt import local_now
from .todo.graph_client import GraphTodoClient
from .todo.oauth import DeviceCodeFlow, TokenManager

app = typer.Typer(help="CRM для фотоагентства: заказы, съёмки, задачи", no_args_is_help=True)
console = Console()


@app.command()
def setup(
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
) -> None:
    """Первичная настройка: Google Таблица и Telegram."""
    console.print("[bold]Настройка CRM Lumi[/]\n")
    console.print("[bold]Шаг 1 из 2.[/] Google Таблица.\n")
    console.print(
        "Понадобится ID Google Таблицы (из адресной строки, между /d/ и /edit) "
        "и файл ключа сервисного аккаунта Google (JSON), на чей email расшарена "
        "таблица (доступ Editor).\n"
    )
    sheet_id = typer.prompt("ID Google Таблицы").strip()
    worksheet_name = typer.prompt("Имя листа с заказами", default="Заказы").strip()
    service_account_file = typer.prompt(
        "Путь к JSON-ключу сервисного аккаунта",
        default="config/service_account.json",
    ).strip()

    if not Path(service_account_file).exists():
        console.print(
            f"\n[red]Файл не найден:[/] {service_account_file}\n"
            "Создай сервисный аккаунт в Google Cloud Console, скачай ключ, "
            "и расшарь таблицу на его email (роль Editor)."
        )
        raise typer.Exit(code=1)

    write_config(
        config_path,
        sheet_id=sheet_id,
        worksheet_name=worksheet_name,
        service_account_file=service_account_file,
    )

    console.print("\n[bold]Шаг 2 из 2.[/] Telegram.")
    console.print("Открой в Telegram бота @BotFather, отправь /newbot,")
    console.print("придумай имя — он выдаст токен вида 1234567:AAF...\n")
    token = typer.prompt("Токен бота").strip()

    console.print("\nТеперь напиши своему новому боту /start,")
    console.print("затем открой @userinfobot — он покажет твой числовой chat id.\n")
    owner_chat_id = typer.prompt("Твой chat id (это будешь ты, владелец)").strip()

    write_env(
        env_path,
        {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_OWNER_CHAT_ID": owner_chat_id},
    )

    console.print(
        f"\n[green]Готово.[/] Настройки записаны в {config_path} и {env_path}\n"
        "Запускай: [bold]python -m crmlumi run[/]"
    )


@app.command("todo-setup")
def todo_setup(
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
) -> None:
    """Подключить Microsoft To Do: вход владельца и создание 6 списков."""
    console.print("[bold]Настройка Microsoft To Do[/]\n")
    console.print(
        "Понадобится Client ID приложения, зарегистрированного в Azure AD "
        "(тип — 'Mobile and desktop applications', публичный клиент, без секрета, "
        "разрешён 'Allow public client flows').\n"
    )
    client_id = typer.prompt("Client ID").strip()

    flow = DeviceCodeFlow(client_id)
    info = flow.start()
    console.print(f"\n{info.message}\n")
    console.print("Жду подтверждения...")
    result = flow.wait_for_result()
    console.print("[green]Вход выполнен.[/]\n")

    graph = GraphTodoClient(lambda: result["access_token"])
    lists: dict[str, dict[str, str]] = {}
    for role_name in role_names():
        existing_id = graph.find_list_by_name(role_name)
        list_id = existing_id or graph.create_list(role_name)
        lists[role_name] = {"list_id": list_id, "invite_link": ""}
        console.print(f"  {role_name}: {list_id}")

    write_microsoft_config(config_path, client_id=client_id, lists=lists)
    write_env(env_path, {"MS_OWNER_REFRESH_TOKEN": result["refresh_token"]})

    console.print(
        "\n[green]Списки созданы.[/] Ссылки-приглашения на них добавляются прямо в "
        "Telegram: 🖥 Хост → 🔗 Ссылки To Do."
    )


@app.command()
def run(
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
) -> None:
    """Запустить бота и опрос Google Таблицы."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for noisy in ("httpx", "httpcore", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    kind = runtime_kind()
    if kind == "docker":
        # launchd пишет лог сам, а в контейнере вывод уходит в docker logs —
        # дублируем в файл, чтобы работала кнопка «🖥 Хост → 📜 Лог».
        log_path = err_log_path(Path.cwd())
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_log = logging.FileHandler(log_path, encoding="utf-8")
        file_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(file_log)

    settings = load_settings(config_path, env_path)
    if not settings.is_configured:
        console.print(
            "[red]Не настроено.[/] Сначала запусти: [bold]python -m crmlumi setup[/]"
        )
        raise typer.Exit(code=1)
    if not settings.is_telegram_configured:
        console.print(
            "[red]Не настроен Telegram.[/] Сначала запусти: [bold]python -m crmlumi setup[/]"
        )
        raise typer.Exit(code=1)

    async def main() -> None:
        db = Database(settings.db_path)
        await db.connect()
        sheet = GspreadSheetClient(
            settings.google.sheet_id,
            settings.google.worksheet_name,
            settings.google.service_account_file,
        )
        bot = Bot(token=settings.telegram_bot_token)
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(build_router())
        owner_chat_id = int(settings.telegram_owner_chat_id)
        runtime = RuntimeState(
            project_dir=Path.cwd(), supervised=is_supervised(), kind=kind
        )

        graph: GraphTodoClient | None = None
        if settings.is_todo_configured:
            token_manager = TokenManager(
                client_id=settings.microsoft.client_id,
                refresh_token=settings.ms_owner_refresh_token,
                on_refresh_token_changed=lambda new_token: write_env(
                    env_path, {"MS_OWNER_REFRESH_TOKEN": new_token}
                ),
            )
            graph = GraphTodoClient(token_manager.get_access_token)

        photographer_list = settings.microsoft.lists.get(ROLE_PHOTOGRAPHER)
        photographer_list_id = photographer_list.list_id if photographer_list else None
        invite_links = {
            role: info.invite_link
            for role, info in settings.microsoft.lists.items()
            if info.invite_link
        }
        todo_list_ids = {
            role: info.list_id for role, info in settings.microsoft.lists.items() if info.list_id
        }
        # Один изменяемый словарь на весь процесс: владелец правит сроки в боте.
        deadlines = dict(settings.deadlines)
        shoots_sheet = GspreadSheetClient(
            settings.google.sheet_id,
            SHOOTS_WORKSHEET,
            settings.google.service_account_file,
            create_header=list(SHOOT_COLUMNS.values()),
        )

        async def route_event(event) -> None:
            logging.info("%s: заказ %s (%s)", event.kind, event.order_id, event.order.school)
            role = role_for_event(event)
            if role == ROLE_NEW_LEADS:
                await notify_new_lead(bot, db, event.order)
            elif role is not None and role != ROLE_PHOTOGRAPHER:
                # Фотографам рассылаются съёмки — это делает сверка ниже.
                await notify_stage_role(bot, db, event.order, role)

        async def reconcile() -> None:
            """Сводки и фото-этапы заказов, рассылка незанятых съёмок."""
            written = await reconcile_orders_with_shoots(db, sheet)
            if written:
                runtime.poll_now.set()
            for order, shoot in await shoots_to_offer(db, today=local_now().date()):
                await broadcast_shoot_offer(bot, db, order, shoot)

        async def sync_todo() -> None:
            tz = local_now().tzinfo
            try:
                created = await sync_stage_tasks(
                    db, graph, todo_list_ids, deadlines=deadlines, tz=tz
                )
                if photographer_list_id:
                    created += await sync_shoot_tasks(
                        db, graph, photographer_list_id,
                        tz=tz, pre_shoot_hours=settings.reminders.pre_shoot_hours,
                    )
                completed = await complete_finished_tasks(db, graph)
            except Exception as exc:
                # Без трейсбека: при отвале Microsoft это повторялось бы каждые 30 с.
                logging.warning("Не удалось обновить задачи в To Do: %s", exc)
                runtime.last_todo_error = str(exc) or exc.__class__.__name__
            else:
                runtime.last_todo_error = None
                if created or completed:
                    logging.info("To Do: изменено задач %s, закрыто %s", created, completed)

        async def poll_loop() -> None:
            console.print("[green]Слежу за таблицей.[/] Остановить — Ctrl+C\n")
            while True:
                # Разовый сбой сети/Google/Microsoft не должен ронять бота —
                # ошибка видна в «🖥 Хост → 📊 Статус», повторим на следующем круге.
                try:
                    shoot_events = await poll_shoots(shoots_sheet, db)
                    events = await poll_once(sheet, db)
                except Exception as exc:
                    logging.exception("Не удалось опросить таблицу")
                    runtime.last_poll_error = str(exc) or exc.__class__.__name__
                else:
                    runtime.last_poll_at = datetime.now(timezone.utc)
                    runtime.last_poll_error = None
                    for event in events:
                        try:
                            await route_event(event)
                        except Exception:
                            logging.exception(
                                "Не удалось обработать %s по заказу %s", event.kind, event.order_id
                            )
                    try:
                        await notify_shoot_changes(bot, db, shoot_events)
                    except Exception:
                        logging.exception("Не удалось сообщить об изменениях съёмок")
                    # Если бот только что писал в таблицу, сверку откладываем до
                    # следующего (немедленного) круга — чтобы не затереть свежую правку.
                    if not runtime.poll_now.is_set():
                        try:
                            await reconcile()
                        except Exception:
                            logging.exception("Не удалось сверить заказы и съёмки")
                    if graph is not None and todo_list_ids:
                        await sync_todo()
                # Бот сам записал что-то в таблицу — опрашиваем сразу, не дожидаясь интервала.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        runtime.poll_now.wait(), timeout=settings.poll.interval_s
                    )
                runtime.poll_now.clear()

        async def reminder_loop() -> None:
            while True:
                try:
                    await run_reminders(
                        bot, db, owner_chat_id,
                        now=local_now(), settings=settings.reminders, deadlines=deadlines,
                    )
                except Exception:
                    logging.exception("Не удалось разослать напоминания")
                await asyncio.sleep(60)

        async def run_daily_check(now: datetime) -> None:
            shoots_without_photographer = await find_shoots_without_photographer_near_date(
                db, now=local_now(), near_days=settings.daily_check.near_photographer_days
            )
            stalled_orders = await find_stalled_orders(
                db, now=now, stalled_days=settings.daily_check.stalled_days
            )
            overdue_tasks = []
            if graph is not None:
                overdue_tasks = find_overdue_tasks(graph, todo_list_ids, now=now)
            text = format_daily_digest(overdue_tasks, shoots_without_photographer, stalled_orders)
            if text is not None:
                await send_daily_digest(bot, owner_chat_id, text)

        async def daily_check_loop() -> None:
            while True:
                now = datetime.now(timezone.utc)
                today = now.date().isoformat()
                last_run = await db.get_last_daily_check_date()
                if last_run != today and now.hour >= settings.daily_check.hour_utc:
                    try:
                        await run_daily_check(now)
                    except Exception:
                        logging.exception("Не удалось собрать ежедневную сводку")
                    await db.set_last_daily_check_date(today)
                await asyncio.sleep(1800)

        try:
            try:
                added = await asyncio.to_thread(sheet.ensure_columns, list(FIELD_COLUMNS.values()))
                if added:
                    logging.info("В лист заказов добавлены колонки: %s", ", ".join(added))
            except Exception:
                logging.exception("Не удалось дописать колонки в лист заказов")
            if shoots_sheet.created:
                # Лист «Съёмки» появился только что — переносим дату/локацию/фотографа
                # из строк заказов, чтобы старые заказы не потеряли расписание.
                migrated = await migrate_legacy_shoots(
                    db, await asyncio.to_thread(sheet.fetch_rows), shoots_sheet
                )
                logging.info("Создан лист «%s», перенесено съёмок: %s", SHOOTS_WORKSHEET, migrated)
            await sync_bot_commands(bot, db, owner_chat_id)
            commit = await asyncio.to_thread(current_commit, runtime.project_dir)
            await notify_startup(bot, db, owner_chat_id, commit)

            workers = [
                asyncio.create_task(
                    dp.start_polling(
                        bot,
                        db=db,
                        owner_chat_id=owner_chat_id,
                        sheet=sheet,
                        ms_client_id=settings.microsoft.client_id,
                        graph=graph,
                        invite_links=invite_links,
                        runtime=runtime,
                        config_path=config_path,
                        shoots_sheet=shoots_sheet,
                        deadlines=deadlines,
                        reminder_settings=settings.reminders,
                    )
                ),
                asyncio.create_task(poll_loop()),
                asyncio.create_task(daily_check_loop()),
                asyncio.create_task(reminder_loop()),
            ]
            restart_wait = asyncio.create_task(runtime.restart_requested.wait())
            done, _ = await asyncio.wait(
                [*workers, restart_wait], return_when=asyncio.FIRST_COMPLETED
            )
            if restart_wait in done:
                # Выходим сами — launchd (KeepAlive) поднимет процесс уже с новым кодом.
                logging.info("Перезапуск по запросу владельца")
                # Пауза, чтобы polling успел запросить следующие апдейты: так Telegram
                # засчитает нажатие «Перезапустить» обработанным и не пришлёт его
                # снова после рестарта (иначе — бесконечный перезапуск).
                await asyncio.sleep(3)
                with contextlib.suppress(RuntimeError):
                    await dp.stop_polling()
            for task in [*workers, restart_wait]:
                task.cancel()
            await asyncio.gather(*workers, restart_wait, return_exceptions=True)
            for task in done:
                if task is not restart_wait and not task.cancelled() and task.exception():
                    raise task.exception()
        finally:
            await db.close()
            await bot.session.close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\nОстановлено.")


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def _require_macos() -> None:
    if sys.platform != "darwin":
        console.print("[red]Автозапуск через launchd есть только на macOS.[/]")
        raise typer.Exit(code=1)


@app.command("install-service")
def install_service(config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Включить автозапуск на Mac: бот работает в фоне и сам поднимается после перезагрузки."""
    _require_macos()
    project_dir = Path.cwd()
    if not config_path.exists():
        console.print(
            f"[red]Не найден {config_path}.[/] Запускай из папки проекта "
            "после [bold]python -m crmlumi setup[/]."
        )
        raise typer.Exit(code=1)

    domain = f"gui/{os.getuid()}"
    target = plist_path(Path.home())
    _launchctl("bootout", f"{domain}/{LAUNCHD_LABEL}")

    # Прежний фоновый процесс после bootout завершается не мгновенно — ждём его,
    # а всё, что осталось через 15 секунд, запущено вручную.
    for _ in range(15):
        manual = subprocess.run(
            ["pgrep", "-f", "crmlumi run"], capture_output=True, text=True, check=False
        )
        if not manual.stdout.strip():
            break
        time.sleep(1)
    if manual.stdout.strip():
        console.print(
            "[red]Бот уже запущен вручную в другом окне терминала[/] "
            f"(PID {', '.join(manual.stdout.split())}).\n"
            "Останови его там (Ctrl+C) и повтори эту команду — иначе будут два бота сразу."
        )
        raise typer.Exit(code=1)

    (project_dir / LOG_DIR_NAME).mkdir(exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        build_launchd_plist(python_executable=sys.executable, project_dir=project_dir)
    )

    for _ in range(3):
        result = _launchctl("bootstrap", domain, str(target))
        if result.returncode == 0:
            break
        time.sleep(2)
    else:
        console.print(f"[red]launchd не принял задание:[/] {result.stderr.strip()}")
        raise typer.Exit(code=1)

    console.print(
        "[green]Готово — бот работает в фоне и сам запустится после перезагрузки Mac.[/]\n"
        "Терминал можно закрыть. Обновление, перезапуск и лог — в Telegram: 🖥 Хост."
    )


@app.command("uninstall-service")
def uninstall_service() -> None:
    """Выключить автозапуск на Mac и остановить фоновый бот."""
    _require_macos()
    _launchctl("bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
    target = plist_path(Path.home())
    if target.exists():
        target.unlink()
    console.print("Автозапуск выключен, фоновый бот остановлен.")
