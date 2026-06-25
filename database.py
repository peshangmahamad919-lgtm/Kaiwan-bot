import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY,
                first_name  TEXT,
                last_name   TEXT,
                username    TEXT,
                is_premium  INTEGER DEFAULT 0,
                points      INTEGER DEFAULT 0,
                ref_by      INTEGER DEFAULT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS name_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                first_name  TEXT,
                last_name   TEXT,
                seen_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS username_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                username    TEXT,
                seen_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS required_channels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER UNIQUE NOT NULL,
                username    TEXT,
                title       TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key         TEXT PRIMARY KEY,
                value       TEXT
            );
            CREATE TABLE IF NOT EXISTS lookup_costs (
                user_id     INTEGER PRIMARY KEY,
                cost        INTEGER NOT NULL
            );
        """)
        await db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('default_cost','0')")
        await db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('ref_reward','5')")
        await db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('start_points','0')")
        # migration: ئەگەر کۆن بوو و 1 بوو، ڕیسێت بکە بۆ 0
        await db.execute("UPDATE settings SET value='0' WHERE key='default_cost' AND value='1'")
        await db.commit()


# ─── Users ─────────────────────────────────────────────────────────────────────

async def register_user(uid: int, first: str, last: str, uname: str,
                        is_premium: bool = False, ref_by: int = None) -> bool:
    """True دەگەڕێنێتەوە ئەگەر یوزەری نوێ بوو."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM users WHERE id=?", (uid,)) as c:
            exists = await c.fetchone()
        if exists:
            return False
        start_pts = int(await _get_setting_raw(db, "start_points", "0"))
        await db.execute(
            "INSERT INTO users (id,first_name,last_name,username,is_premium,points,ref_by) VALUES (?,?,?,?,?,?,?)",
            (uid, first, last, uname, int(is_premium), start_pts, ref_by)
        )
        await db.execute("INSERT INTO name_history (user_id,first_name,last_name) VALUES (?,?,?)",
                         (uid, first, last))
        if uname:
            await db.execute("INSERT INTO username_history (user_id,username) VALUES (?,?)", (uid, uname))
        await db.commit()
        return True


async def get_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as c:
            row = await c.fetchone()
            if row:
                return dict(zip([d[0] for d in c.description], row))
    return None


async def user_exists(uid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM users WHERE id=?", (uid,)) as c:
            return bool(await c.fetchone())


async def get_all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM users") as c:
            return [r[0] for r in await c.fetchall()]


async def get_users_page(limit=20, offset=0):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id,first_name,username,points,created_at FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ) as c:
            return await c.fetchall()


async def get_user_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            row = await c.fetchone()
            return row[0] if row else 0


async def get_points(uid: int) -> int:
    u = await get_user(uid)
    return u["points"] if u else 0


async def add_points(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET points=points+? WHERE id=?", (amount, uid))
        await db.commit()


async def set_points(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET points=? WHERE id=?", (amount, uid))
        await db.commit()


async def deduct_points(uid: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT points FROM users WHERE id=?", (uid,)) as c:
            row = await c.fetchone()
        if not row or row[0] < amount:
            return False
        await db.execute("UPDATE users SET points=points-? WHERE id=?", (amount, uid))
        await db.commit()
        return True


async def get_referral_count(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (uid,)) as c:
            row = await c.fetchone()
            return row[0] if row else 0


# ─── Lookup cost ───────────────────────────────────────────────────────────────

async def set_user_cost(uid: int, cost: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO lookup_costs (user_id,cost) VALUES (?,?)", (uid, cost))
        await db.commit()


async def get_user_cost(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT cost FROM lookup_costs WHERE user_id=?", (uid,)) as c:
            row = await c.fetchone()
        if row:
            return row[0]
        return int(await _get_setting_raw(db, "default_cost", "1"))


# ─── Settings ──────────────────────────────────────────────────────────────────

async def _get_setting_raw(db, key: str, default=""):
    async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
        row = await c.fetchone()
    return row[0] if row else default


async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        return await _get_setting_raw(db, key, default)


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
        await db.commit()


# ─── Required channels ─────────────────────────────────────────────────────────

async def add_required_channel(channel_id: int, username: str, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO required_channels (channel_id,username,title) VALUES (?,?,?)",
            (channel_id, username, title)
        )
        await db.commit()


async def remove_required_channel(channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM required_channels WHERE channel_id=?", (channel_id,))
        await db.commit()


async def get_required_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT channel_id,username,title FROM required_channels") as c:
            return await c.fetchall()


# ─── OSINT history ─────────────────────────────────────────────────────────────

async def update_osint_user(uid: int, first: str, last: str, uname: str, is_premium: bool = False):
    """تۆمارکردنی هیستۆری یوزەری گەڕاوە — جیاواز لە بۆت یوزەر."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT first_name,last_name FROM name_history WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)) as c:
            last_n = await c.fetchone()
        new_full = f"{first or ''} {last or ''}".strip()
        old_full = f"{last_n[0] or ''} {last_n[1] or ''}".strip() if last_n else None
        if old_full != new_full:
            await db.execute("INSERT INTO name_history (user_id,first_name,last_name) VALUES (?,?,?)", (uid, first, last))

        if uname:
            async with db.execute("SELECT username FROM username_history WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)) as c:
                last_u = await c.fetchone()
            if not last_u or (last_u[0] or "").lower() != uname.lower():
                await db.execute("INSERT INTO username_history (user_id,username) VALUES (?,?)", (uid, uname))

        await db.commit()


async def get_name_history(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT first_name,last_name,seen_at FROM name_history WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (uid,)
        ) as c:
            return await c.fetchall()


async def get_username_history(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username,seen_at FROM username_history WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (uid,)
        ) as c:
            return await c.fetchall()
