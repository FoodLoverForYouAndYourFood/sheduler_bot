import asyncio
import json
import os
import sqlite3
from html import escape
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


DB_PATH = "tasks.db"
STATUS_TODO = "todo"
STATUS_DOING = "doing"
STATUS_DONE = "done"

STATUS_EMOJI = {
    STATUS_TODO: "🟦 TODO",
    STATUS_DOING: "🟧 DOING",
    STATUS_DONE: "🟩 DONE",
}

PRIORITY_EMOJI = {
    "low": "⬇️ low",
    "medium": "➡️ medium",
    "high": "⬆️ high",
}


@dataclass
class UserConfig:
    id: int
    alias: str
    name: str


@dataclass
class BotConfig:
    report_time: str
    evening_report_time: str
    timezone: str
    users: Dict[str, UserConfig]

    @property
    def allowed_ids(self) -> set[int]:
        return {user.id for user in self.users.values()}


class TaskRepository:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                alias TEXT,
                name TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                assigned_to INTEGER,
                priority TEXT,
                deadline TEXT,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        # Миграция: добавляем столбец dod, если его нет
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "dod" not in columns:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN dod TEXT DEFAULT ''")
        self._conn.commit()

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> UserConfig:
        return UserConfig(
            id=int(row["id"]),
            alias=str(row["alias"] or "").strip(),
            name=str(row["name"] or "").strip(),
        )

    async def bootstrap_users_from_config(self, users: Dict[str, UserConfig]) -> None:
        async with self._lock:
            for user in users.values():
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO users (id, alias, name, is_admin)
                    VALUES (?, ?, ?, 1)
                    """,
                    (user.id, user.alias, user.name),
                )
            self._conn.commit()

    async def list_users(self) -> list[UserConfig]:
        async with self._lock:
            cursor = self._conn.execute("SELECT * FROM users ORDER BY name, id")
            return [self._row_to_user(row) for row in cursor.fetchall()]

    async def get_users_map(self) -> Dict[int, UserConfig]:
        users = await self.list_users()
        return {user.id: user for user in users}

    async def add_or_update_user(self, user_id: int, alias: str, name: str, is_admin: bool = False) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO users (id, alias, name, is_admin)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET alias=excluded.alias, name=excluded.name, is_admin=excluded.is_admin
                """,
                (user_id, alias, name, int(is_admin)),
            )
            self._conn.commit()

    async def is_allowed(self, user_id: int) -> bool:
        async with self._lock:
            cursor = self._conn.execute("SELECT 1 FROM users WHERE id = ? LIMIT 1", (user_id,))
            return cursor.fetchone() is not None

    async def is_admin(self, user_id: int) -> bool:
        async with self._lock:
            cursor = self._conn.execute("SELECT is_admin FROM users WHERE id = ? LIMIT 1", (user_id,))
            row = cursor.fetchone()
            return bool(row and row["is_admin"])

    async def resolve_user_by_alias(self, text: str) -> Optional[UserConfig]:
        cleaned = text.strip().lower()
        users = await self.list_users()
        for user in users:
            if cleaned in {user.alias.lower(), user.name.lower(), str(user.id)}:
                return user
        return None

    async def add_task(
        self,
        title: str,
        created_by: int,
        assigned_to: Optional[int],
        priority: str,
        deadline: Optional[str],
        dod: str,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO tasks (title, status, assigned_to, priority, deadline, dod, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    STATUS_TODO,
                    assigned_to,
                    priority,
                    deadline,
                    dod,
                    created_by,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid) # type: ignore

    async def list_by_status(self, status: str) -> list[sqlite3.Row]:
        async with self._lock:
            cursor = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = ?
                ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id
                """,
                (status,),
            )
            return cursor.fetchall()

    async def list_all(self) -> list[sqlite3.Row]:
        async with self._lock:
            cursor = self._conn.execute(
                """
                SELECT * FROM tasks
                ORDER BY
                    CASE status WHEN 'todo' THEN 1 WHEN 'doing' THEN 2 ELSE 3 END,
                    CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                    id
                """
            )
            return cursor.fetchall()

    async def list_for_user(self, user_id: int) -> list[sqlite3.Row]:
        async with self._lock:
            cursor = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE assigned_to = ?
                ORDER BY
                    CASE status WHEN 'todo' THEN 1 WHEN 'doing' THEN 2 ELSE 3 END,
                    CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                    id
                """,
                (user_id,),
            )
            return cursor.fetchall()

    async def update_status(self, task_id: int, status: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, task_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    async def update_deadline(self, task_id: int, deadline: Optional[str]) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET deadline = ?, updated_at = ? WHERE id = ?",
                (deadline, now, task_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    async def done_since(self, iso_timestamp: str) -> list[sqlite3.Row]:
        async with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM tasks WHERE status = ? AND updated_at >= ? ORDER BY updated_at DESC",
                (STATUS_DONE, iso_timestamp),
            )
            return cursor.fetchall()


class AddTaskFlow(StatesGroup):
    waiting_for_title = State()
    waiting_for_assignee = State()
    waiting_for_priority = State()
    waiting_for_deadline = State()
    waiting_for_dod = State()


def load_config(path: str = "config.json") -> BotConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    users: Dict[str, UserConfig] = {}
    for key, value in raw["users"].items():
        users[key] = UserConfig(
            id=int(value["id"]),
            alias=str(value.get("alias", key)).strip(),
            name=str(value.get("name", key)).strip(),
        )

    return BotConfig(
        report_time=raw.get("report_time", "09:00"),
        evening_report_time=raw.get("evening_report_time", "19:00"),
        timezone=raw.get("timezone", "UTC"),
        users=users,
    )


def load_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def parse_report_time(report_time: str) -> Tuple[int, int]:
    hour, minute = report_time.split(":")
    return int(hour), int(minute)


def parse_deadline(text: str) -> Optional[str]:
    cleaned = text.strip().lower()
    if cleaned in {"", "skip", "нет", "no", "-"}:
        return None
    try:
        parsed = datetime.strptime(cleaned, "%Y-%m-%d")
        return parsed.date().isoformat()
    except ValueError:
        return None


def normalize_priority(text: str) -> Optional[str]:
    cleaned = text.strip().lower()
    # Берём последнее слово — так поддерживаются варианты с эмодзи:
    # "⬆️ high", "➡ medium", "⬇ низкий" и т.п.
    base = cleaned.split()[-1] if cleaned else ""

    if base in {"h", "high", "выс", "высокий"}:
        return "high"
    if base in {"m", "med", "mid", "medium", "ср", "средний"}:
        return "medium"
    if base in {"l", "low", "низ", "низкий"}:
        return "low"
    return None


def format_task(
    row: sqlite3.Row, users: Dict[int, UserConfig], now: datetime, tz: ZoneInfo
) -> str:
    status = STATUS_EMOJI.get(row["status"], row["status"])
    priority = PRIORITY_EMOJI.get(row["priority"] or "medium", "➡️ medium")
    assignee_name = resolve_user_name(row["assigned_to"], users)
    dod = str(row["dod"] or "").strip()

    updated = None
    if row["updated_at"]:
        parsed = datetime.fromisoformat(row["updated_at"])
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        updated = parsed.astimezone(tz)

    title = escape(row["title"])
    meta_parts = []
    if assignee_name:
        meta_parts.append(f"👤 {escape(assignee_name)}")
    meta_parts.append(f"⭐ {priority}")
    if row["deadline"]:
        meta_parts.append(f"📅 до {row['deadline']}")
    if updated:
        meta_parts.append(f"🔄 {updated:%d.%m %H:%M}")

    lines = [f"#{row['id']} • {status}"]
    if meta_parts:
        lines.append(" | ".join(meta_parts))
    lines.append(f"📝 <b>{title}</b>")
    if dod:
        lines.append(f"✅ DoD:\n{escape(dod)}")

    return "\n".join(lines)


def resolve_user_name(user_id: Optional[int], users: Dict[int, UserConfig]) -> str:
    if user_id is None:
        return ""
    user = users.get(int(user_id))
    return user.name if user else ""


async def start_report_scheduler(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    repo: TaskRepository,
    config: BotConfig,
) -> None:
    tzinfo = ZoneInfo(config.timezone)

    morning_hour, morning_minute = parse_report_time(config.report_time)
    morning_trigger = CronTrigger(hour=morning_hour, minute=morning_minute, timezone=tzinfo)
    scheduler.add_job(send_daily_report, trigger=morning_trigger, args=[bot, repo, config])

    evening_hour, evening_minute = parse_report_time(config.evening_report_time)
    evening_trigger = CronTrigger(hour=evening_hour, minute=evening_minute, timezone=tzinfo)
    scheduler.add_job(send_evening_report, trigger=evening_trigger, args=[bot, repo, config])

    scheduler.start()


async def send_daily_report(bot: Bot, repo: TaskRepository, config: BotConfig) -> None:
    await send_report(bot, repo, config, title="TaskPair — утренний отчёт")


async def send_evening_report(bot: Bot, repo: TaskRepository, config: BotConfig) -> None:
    await send_report(bot, repo, config, title="TaskPair — вечерний отчёт")


async def send_report(
    bot: Bot,
    repo: TaskRepository,
    config: BotConfig,
    title: str,
    lookback_hours: int = 24,
) -> None:
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz=tz)
    since = (now - timedelta(hours=lookback_hours)).astimezone(timezone.utc).isoformat()

    users_map = await repo.get_users_map()

    todo = await repo.list_by_status(STATUS_TODO)
    doing = await repo.list_by_status(STATUS_DOING)
    done_recent = await repo.done_since(since)

    def block(title: str, rows: Iterable[sqlite3.Row]) -> str:
        content = "\n\n".join(format_task(row, users_map, now, tz) for row in rows)
        return f"{title}\n{content if content else '—'}"

    text = "\n\n".join(
        [
            title,
            block("🟦 TODO", todo),
            block("🟧 DOING", doing),
            block(f"🟩 DONE (последние {lookback_hours}ч)", done_recent),
        ]
    )

    for user in users_map.values():
        try:
            await bot.send_message(user.id, text)
        except Exception as exc:  # pragma: no cover - defensive logging
            print(f"Не удалось отправить отчёт {user.name}: {exc}")


def build_priority_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⬆️ high"), KeyboardButton(text="➡️ medium"), KeyboardButton(text="⬇️ low")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def build_assignee_keyboard(users: list[UserConfig]) -> ReplyKeyboardMarkup:
    buttons = [[KeyboardButton(text=user.name or str(user.id))] for user in users]
    return ReplyKeyboardMarkup(keyboard=buttons or [[KeyboardButton(text="skip")]], resize_keyboard=True, one_time_keyboard=True)


async def main() -> None:
    load_env()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Установите переменную окружения BOT_TOKEN с токеном Telegram-бота.")

    config = load_config()
    tz = ZoneInfo(config.timezone)
    repo = TaskRepository(DB_PATH)
    await repo.bootstrap_users_from_config(config.users)
    users_map = await repo.get_users_map()
    allowed_ids = set(users_map.keys())

    bot = Bot(token=token, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    router = Router()

    async def refresh_users_cache() -> None:
        nonlocal users_map, allowed_ids
        users_map = await repo.get_users_map()
        allowed_ids = set(users_map.keys())

    def allowed(message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in allowed_ids)

    router.message.filter(allowed)

    async def ensure_admin(message: Message) -> bool:
        if not message.from_user:
            return False
        if not await repo.is_admin(message.from_user.id):
            await message.answer("Эта команда доступна только администраторам.")
            return False
        return True

    @dp.message(lambda m: m.from_user and m.from_user.id not in allowed_ids)
    async def handle_unauthorized(message: Message) -> None:
        await message.answer("У вас нет доступа к этому боту.")

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "<b>🚀 TaskPair — рабочий трекер задач</b>\n"
            "Быстрое добавление, приоритеты, дедлайны, утренние/вечерние отчёты.\n\n"
            "<b>Основное</b>\n"
            "• /add &lt;текст&gt; — новая задача\n"
            "• /todo, /doing, /done — списки по статусам\n"
            "• /all — все задачи\n"
            "• /me — мои задачи\n"
            "• /update &lt;id&gt; &lt;todo|doing|done&gt; — сменить статус\n"
            "• /deadline &lt;id&gt; &lt;YYYY-MM-DD|clear&gt; — дедлайн\n"
            "• /report — прислать отчёт сейчас\n"
            "• /cancel — отменить ввод задачи\n\n"
            "<b>Админам</b>\n"
            "• /add_user &lt;id&gt; &lt;alias&gt; &lt;Имя&gt;\n"
            "• /add_admin &lt;id&gt; &lt;alias&gt; &lt;Имя&gt;"
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await cmd_start(message)

    @router.message(Command("add"))
    async def cmd_add(message: Message, state: FSMContext) -> None:
        args = message.text.split(maxsplit=1) # type: ignore
        if len(args) == 2 and args[1].strip():
            await state.update_data(title=args[1].strip())
            await ask_assignee(message, state, repo)
            return

        await state.set_state(AddTaskFlow.waiting_for_title)
        await message.answer("Что за задача? Отправь одним сообщением.", reply_markup=ReplyKeyboardRemove())

    @router.message(AddTaskFlow.waiting_for_title, F.text)
    async def add_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title=message.text.strip()) # type: ignore
        await ask_assignee(message, state, repo)

    @router.message(AddTaskFlow.waiting_for_assignee, F.text)
    async def add_assignee(message: Message, state: FSMContext) -> None:
        user = await repo.resolve_user_by_alias(message.text) # type: ignore
        if not user:
            await message.answer("Не понял, кому назначить. Напиши имя/букву (пример: Никита или N).")
            return
        await state.update_data(assignee_id=user.id)
        await state.set_state(AddTaskFlow.waiting_for_priority)
        await message.answer(
            "Приоритет? (⬆️ high / ➡️ medium / ⬇️ low)",
            reply_markup=build_priority_keyboard(),
        )

    @router.message(AddTaskFlow.waiting_for_priority, F.text)
    async def add_priority(message: Message, state: FSMContext) -> None:
        priority = normalize_priority(message.text) # type: ignore
        if not priority:
            await message.answer("Приоритет не понял. Используй high / medium / low.")
            return
        await state.update_data(priority=priority)
        await state.set_state(AddTaskFlow.waiting_for_deadline)
        await message.answer(
            "На какую дату задача? Введи YYYY-MM-DD или напиши skip, чтобы привязать к сегодняшнему дню.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="skip")]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

    @router.message(AddTaskFlow.waiting_for_deadline, F.text)
    async def add_deadline(message: Message, state: FSMContext) -> None:
        cleaned = message.text.strip().lower() # type: ignore
        deadline = parse_deadline(message.text) # type: ignore
        if deadline is None:
            if cleaned in {"skip", "-", "нет", "no", ""}:
                # Привязываем задачу к сегодняшнему дню
                deadline = datetime.now(tz).date().isoformat()
            else:
                await message.answer("Дата не распознана. Формат: YYYY-MM-DD или skip.")
                return

        await state.update_data(deadline=deadline)
        await state.set_state(AddTaskFlow.waiting_for_dod)
        await message.answer(
            "Definition of Done (критерии готовности). Напиши текст или skip, чтобы оставить пустым.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="skip")]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

    @router.message(AddTaskFlow.waiting_for_dod, F.text)
    async def add_dod(message: Message, state: FSMContext) -> None:
        dod_raw = message.text.strip() # type: ignore
        dod = "" if dod_raw.lower() in {"skip", "-", "нет", "no", ""} else dod_raw

        data = await state.get_data()
        title = data.get("title", "").strip()
        assignee_id = data.get("assignee_id")
        priority = data.get("priority", "medium")
        deadline = data.get("deadline")

        task_id = await repo.add_task(
            title=title,
            created_by=message.from_user.id if message.from_user else 0,
            assigned_to=assignee_id,
            priority=priority,
            deadline=deadline,
            dod=dod,
        )
        await state.clear()
        await message.answer(
            f"Добавил задачу #{task_id}: {title}",
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Ок, отменил.", reply_markup=ReplyKeyboardRemove())

    @router.message(Command("todo"))
    async def cmd_todo(message: Message) -> None:
        await send_status_list(message, STATUS_TODO, repo, tz)

    @router.message(Command("doing"))
    async def cmd_doing(message: Message) -> None:
        await send_status_list(message, STATUS_DOING, repo, tz)

    @router.message(Command("done"))
    async def cmd_done(message: Message) -> None:
        await send_status_list(message, STATUS_DONE, repo, tz)

    @router.message(Command("all"))
    async def cmd_all(message: Message) -> None:
        now = datetime.now(tz)
        rows = await repo.list_all()
        if not rows:
            await message.answer("Пока задач нет.")
            return
        users_map_local = await repo.get_users_map()
        text = "\n\n".join(format_task(row, users_map_local, now, tz) for row in rows)
        await message.answer(text)

    @router.message(Command("me"))
    async def cmd_me(message: Message) -> None:
        if not message.from_user:
            return
        now = datetime.now(tz)
        rows = await repo.list_for_user(message.from_user.id)
        if not rows:
            await message.answer("У тебя нет задач.")
            return
        users_map_local = await repo.get_users_map()
        text = "\n\n".join(format_task(row, users_map_local, now, tz) for row in rows)
        await message.answer(text)

    @router.message(Command("update"))
    async def cmd_update(message: Message) -> None:
        parts = message.text.split() # type: ignore
        if len(parts) != 3:
            await message.answer("Используй: /update &lt;id&gt; &lt;todo|doing|done&gt;")
            return
        try:
            task_id = int(parts[1])
        except ValueError:
            await message.answer("id должен быть числом.")
            return

        status = parts[2].lower()
        if status not in {STATUS_TODO, STATUS_DOING, STATUS_DONE}:
            await message.answer("Статус должен быть todo / doing / done.")
            return

        updated = await repo.update_status(task_id, status)
        if not updated:
            await message.answer(f"Задача #{task_id} не найдена.")
            return

        await message.answer(f"Обновил статус задачи #{task_id} -> {STATUS_EMOJI[status]}")

    async def handle_add_user_cmd(message: Message, make_admin: bool) -> None:
        if not await ensure_admin(message):
            return
        parts = message.text.split(maxsplit=3) # type: ignore
        if len(parts) < 4:
            await message.answer(
                "Использование: /add_user <id> <alias> <Имя>" if not make_admin else "Использование: /add_admin <id> <alias> <Имя>"
            )
            return
        try:
            user_id = int(parts[1])
        except ValueError:
            await message.answer("id должен быть числом.")
            return

        alias = parts[2].strip()
        name = parts[3].strip()
        if not alias or not name:
            await message.answer("Укажи alias и Имя.")
            return

        await repo.add_or_update_user(user_id, alias, name, is_admin=make_admin)
        await refresh_users_cache()
        role_text = "админом" if make_admin else "пользователем"
        await message.answer(f"Добавил {escape(name)} ({user_id}) {role_text}.")

    @router.message(Command("add_user"))
    async def cmd_add_user(message: Message) -> None:
        await handle_add_user_cmd(message, make_admin=False)

    @router.message(Command("add_admin"))
    async def cmd_add_admin(message: Message) -> None:
        await handle_add_user_cmd(message, make_admin=True)

    @router.message(Command("deadline"))
    async def cmd_deadline(message: Message) -> None:
        parts = message.text.split() # type: ignore
        if len(parts) != 3:
            await message.answer("Использование: /deadline <id> <YYYY-MM-DD|clear>")
            return
        try:
            task_id = int(parts[1])
        except ValueError:
            await message.answer("id должен быть числом.")
            return

        value = parts[2].strip().lower()
        deadline: Optional[str]
        if value in {"clear", "none", "skip", "-", "нет"}:
            deadline = None
        else:
            deadline = parse_deadline(value)
            if deadline is None:
                await message.answer("Дата не распознана. Формат: YYYY-MM-DD или clear.")
                return

        updated = await repo.update_deadline(task_id, deadline)
        if not updated:
            await message.answer(f"Задача #{task_id} не найдена.")
            return

        text = f"Дедлайн задачи #{task_id} обновлён: {deadline}" if deadline else f"Дедлайн задачи #{task_id} очищен."
        await message.answer(text)

    @router.message(Command("report"))
    async def cmd_report(message: Message) -> None:
        await send_daily_report(bot, repo, config)
        await message.answer("Отчёт отправлен.")

    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.timezone))
    await start_report_scheduler(scheduler, bot, repo, config)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def send_status_list(
    message: Message,
    status: str,
    repo: TaskRepository,
    tz: ZoneInfo,
) -> None:
    now = datetime.now(tz)
    rows = await repo.list_by_status(status)
    if not rows:
        await message.answer("Пусто.")
        return
    users_map = await repo.get_users_map()
    text = "\n\n".join(format_task(row, users_map, now, tz) for row in rows)
    await message.answer(text)


async def ask_assignee(message: Message, state: FSMContext, repo: TaskRepository) -> None:
    await state.set_state(AddTaskFlow.waiting_for_assignee)
    users = await repo.list_users()
    if not users:
        await state.clear()
        await message.answer("Нет доступных исполнителей. Админ может добавить их командой /add_user.")
        return
    await message.answer(
        "Кому назначить? Выбери или напиши имя/инициалы.",
        reply_markup=build_assignee_keyboard(users),
    )


if __name__ == "__main__":
    asyncio.run(main())
