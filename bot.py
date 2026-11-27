import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DB = os.getenv("DB_PATH", "xinda_casino.db")
ADMIN_USER_ID = 1150297226799755344
TAIPEI_TZ = timezone(timedelta(hours=8))
LOTTERY_DRAW_HOUR = 8
LOTTERY_BASE_JACKPOT = 10000

START_BALANCE = 10000
DAILY_REWARD = 500
MIN_BET = 10
MAX_BET = 100000

DAILY_MILESTONES = {
    3: ("三日紅包", 500),
    7: ("七日連簽禮", 1500),
    14: ("半月金包", 3500),
    30: ("月度滿貫紅包", 10000),
}

LEVELS = [
    ("賭神", 10000),
    ("賭俠", 5000),
    ("賭聖", 1000),
    ("賭鬼", 100),
    ("賭徒", 0),
]
LEVEL_RANK = {name: idx for idx, (name, _) in enumerate(reversed(LEVELS))}

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


def universal_command(fn):
    fn = app_commands.allowed_installs(guilds=True, users=True)(fn)
    fn = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(fn)
    return fn


def guild_only_command(fn):
    fn = app_commands.allowed_installs(guilds=True, users=False)(fn)
    fn = app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)(fn)
    return fn


def scope_id(interaction: discord.Interaction) -> int:
    return interaction.guild_id or 0


def taipei_now() -> datetime:
    return datetime.now(TAIPEI_TZ)


def next_draw_date_str() -> str:
    now = taipei_now()
    if now.hour < LOTTERY_DRAW_HOUR:
        return now.date().isoformat()
    return (now.date() + timedelta(days=1)).isoformat()


def database():
    db_dir = os.path.dirname(DB)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    return sqlite3.connect(DB)


def init_db():
    with database() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            guild INTEGER,
            user INTEGER,
            balance INTEGER DEFAULT 10000,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            level TEXT DEFAULT '賭徒',
            last_daily TEXT,
            daily_streak INTEGER DEFAULT 0,
            PRIMARY KEY(guild,user)
        )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()]
        if "daily_streak" not in cols:
            conn.execute("ALTER TABLE players ADD COLUMN daily_streak INTEGER DEFAULT 0")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS lottery_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            carryover_pool INTEGER DEFAULT 0,
            last_draw_date TEXT
        )
        """)
        conn.execute("INSERT OR IGNORE INTO lottery_meta(id, carryover_pool) VALUES(1, 0)")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS lottery_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draw_date TEXT NOT NULL,
            guild INTEGER NOT NULL,
            user INTEGER NOT NULL,
            number INTEGER NOT NULL,
            bet INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS lottery_history (
            draw_date TEXT PRIMARY KEY,
            winning_number INTEGER NOT NULL,
            total_pool INTEGER NOT NULL,
            ticket_count INTEGER NOT NULL,
            summary TEXT NOT NULL,
            drawn_at TEXT NOT NULL
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS lottery_channels (
            channel_id INTEGER PRIMARY KEY
        )
        """)


def player(guild, user):
    with database() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO players(guild,user) VALUES(?,?)",
            (guild, user),
        )
        return conn.execute(
            "SELECT * FROM players WHERE guild=? AND user=?",
            (guild, user),
        ).fetchone()


def format_balance(user_id, balance):
    if user_id == ADMIN_USER_ID:
        return "無限 XDC (∞)"
    return f"{balance:,} XDC"


def change_balance(guild, user, amount):
    data = player(guild, user)
    if user == ADMIN_USER_ID:
        return True
    if data[2] + amount < 0:
        return False

    with database() as conn:
        conn.execute(
            "UPDATE players SET balance=balance+? WHERE guild=? AND user=?",
            (amount, guild, user),
        )
    return True


def add_win(guild, user):
    with database() as conn:
        conn.execute(
            "UPDATE players SET wins=wins+1 WHERE guild=? AND user=?",
            (guild, user),
        )


def add_loss(guild, user):
    with database() as conn:
        conn.execute(
            "UPDATE players SET losses=losses+1 WHERE guild=? AND user=?",
            (guild, user),
        )


def get_level(wins):
    for name, limit in LEVELS:
        if wins >= limit:
            return name
    return "賭徒"


async def update_level(member):
    data = player(member.guild.id, member.id)
    current_level = data[5] or "賭徒"
    earned_level = get_level(data[3])

    # 等級達成或授予後永久保留，不降級
    if LEVEL_RANK.get(earned_level, 0) > LEVEL_RANK.get(current_level, 0):
        level = earned_level
        with database() as conn:
            conn.execute(
                "UPDATE players SET level=? WHERE guild=? AND user=?",
                (level, member.guild.id, member.id),
            )
    else:
        level = current_level

    roles = {r.name: r for r in member.guild.roles}
    managed = [name for name, _ in LEVELS]

    remove = [
        roles[name]
        for name in managed
        if name in roles
        and roles[name] in member.roles
        and name != level
    ]

    if remove:
        await member.remove_roles(*remove)

    if level in roles and roles[level] not in member.roles:
        await member.add_roles(roles[level])


async def win(interaction: discord.Interaction):
    add_win(scope_id(interaction), interaction.user.id)
    if isinstance(interaction.user, discord.Member):
        await update_level(interaction.user)


def make_embed(title, description):
    e = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.gold(),
    )
    e.set_footer(text="新達虛擬賭場｜新達幣 XDC")
    return e


async def check_guild(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "此管理設定需在伺服器頻道內執行。",
            ephemeral=True,
        )
        return False
    return True


async def check_admin(interaction: discord.Interaction, require_guild: bool = False):
    if require_guild and not await check_guild(interaction):
        return False
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message(
            "權限不足：管理指令僅限指定管理員使用。",
            ephemeral=True,
        )
        return False
    return True


@tasks.loop(seconds=30)
async def morning_lottery_scheduler():
    now = taipei_now()
    if now.hour < LOTTERY_DRAW_HOUR:
        return

    today_str = now.date().isoformat()
    channel_ids = []
    notifications = []

    with database() as conn:
        meta = conn.execute(
            "SELECT carryover_pool, last_draw_date FROM lottery_meta WHERE id=1"
        ).fetchone()
        carryover = meta[0] if meta else 0
        last_draw_date = meta[1] if meta else None

        if last_draw_date == today_str:
            return

        due_tickets = conn.execute(
            "SELECT id, guild, user, number, bet FROM lottery_tickets WHERE draw_date <= ?",
            (today_str,),
        ).fetchall()

        winning_number = random.randint(1, 99)
        ticket_stakes = sum(t[4] for t in due_tickets)
        total_pool = LOTTERY_BASE_JACKPOT + carryover + ticket_stakes
        jackpot_tickets = [t for t in due_tickets if t[3] == winning_number]
        jackpot_share = total_pool // len(jackpot_tickets) if jackpot_tickets else 0

        winner_lines = []
        for ticket_id, guild_id, user_id, num, bet in due_tickets:
            tier = None
            prize = 0
            if num == winning_number:
                tier = "頭獎"
                prize = max(jackpot_share, bet * 50)
            elif num % 10 == winning_number % 10:
                tier = "二獎"
                prize = bet * 3

            player(guild_id, user_id)
            if prize > 0:
                if user_id != ADMIN_USER_ID:
                    conn.execute(
                        "UPDATE players SET balance=balance+?, wins=wins+1 WHERE guild=? AND user=?",
                        (prize, guild_id, user_id),
                    )
                else:
                    conn.execute(
                        "UPDATE players SET wins=wins+1 WHERE guild=? AND user=?",
                        (guild_id, user_id),
                    )
                winner_lines.append(f"• <@{user_id}>（{tier} {num:02d} 號）：獲得 **{prize:,} XDC**")
            else:
                conn.execute(
                    "UPDATE players SET losses=losses+1 WHERE guild=? AND user=?",
                    (guild_id, user_id),
                )

            notifications.append((user_id, num, bet, tier, prize))

        if jackpot_tickets:
            next_carryover = 0
        elif ticket_stakes > 0:
            next_carryover = min(1000000, carryover + (ticket_stakes // 2))
        else:
            next_carryover = carryover

        summary_text = (
            "\n".join(winner_lines[:15])
            if winner_lines
            else "本期無人中獎，獎池將累積至明日早上 08:00！"
        )

        conn.execute("DELETE FROM lottery_tickets WHERE draw_date <= ?", (today_str,))
        conn.execute(
            "UPDATE lottery_meta SET carryover_pool=?, last_draw_date=? WHERE id=1",
            (next_carryover, today_str),
        )
        conn.execute(
            "INSERT OR REPLACE INTO lottery_history(draw_date, winning_number, total_pool, ticket_count, summary, drawn_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                today_str,
                winning_number,
                total_pool,
                len(due_tickets),
                summary_text,
                now.isoformat(),
            ),
        )
        channel_ids = [
            r[0] for r in conn.execute("SELECT channel_id FROM lottery_channels").fetchall()
        ]

    embed = make_embed(
        f"早上八點開獎公告｜{today_str} 08:00",
        f"本期開出幸運號碼：**{winning_number:02d}**\n"
        f"本期總獎池：**{total_pool:,} XDC**（參與彩券 {len(due_tickets)} 張）\n\n"
        f"**中獎名單**\n{summary_text}",
    )

    for ch_id in channel_ids:
        channel = bot.get_channel(ch_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(ch_id)
            except Exception:
                channel = None
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

    for user_id, num, bet, tier, prize in notifications:
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            result_line = (
                f"恭喜獲得 **{tier}**！獎金 **{prize:,} XDC** 已匯入您的錢包。"
                if tier and prize > 0
                else "本次未中獎，歡迎使用 `/早安樂透` 繼續參與明日早上 08:00 開獎！"
            )
            await user.send(
                embed=make_embed(
                    f"早安樂透開獎通知｜{today_str} 08:00",
                    f"今日開出號碼：**{winning_number:02d}**\n"
                    f"您的投注號碼：**{num:02d}**（{bet:,} XDC）\n"
                    f"{result_line}",
                )
            )
        except Exception:
            pass


@morning_lottery_scheduler.before_loop
async def before_lottery_loop():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    if not morning_lottery_scheduler.is_running():
        morning_lottery_scheduler.start()
    print(f"新達賭場已啟動（支援伺服器內外使用與 08:00 開獎）：{bot.user}")


@bot.event
async def on_member_join(member):
    if member.bot:
        return

    player(member.guild.id, member.id)
    role = discord.utils.get(member.guild.roles, name="賭徒")

    if role:
        await member.add_roles(role)


@bot.tree.command(name="賭場設定", description="建立五種固定等級")
@guild_only_command
async def setup(interaction: discord.Interaction):
    if not await check_admin(interaction, require_guild=True):
        return

    for name, _ in LEVELS:
        if not discord.utils.get(interaction.guild.roles, name=name):
            await interaction.guild.create_role(name=name)

    await interaction.response.send_message(
        "新達賭場等級建立完成！\n"
        "賭神、賭俠、賭聖、賭鬼、賭徒"
    )


@bot.tree.command(name="設定開獎頻道", description="設定此頻道為每日早上八點開獎公告頻道")
@guild_only_command
async def bind_lottery_channel(interaction: discord.Interaction):
    if not await check_admin(interaction, require_guild=True):
        return

    with database() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO lottery_channels(channel_id) VALUES(?)",
            (interaction.channel_id,),
        )

    await interaction.response.send_message(
        f"已將 <#{interaction.channel_id}> 設為每日 **08:00（台灣時間）早安樂透** 自動開獎公告頻道！"
    )


@bot.tree.command(name="個人資料", description="查看玩家資料")
@universal_command
async def profile(interaction: discord.Interaction, 玩家: discord.User = None):
    target = 玩家 or interaction.user
    data = player(scope_id(interaction), target.id)
    streak = data[7] if len(data) > 7 and data[7] is not None else 0

    await interaction.response.send_message(
        embed=make_embed(
            "玩家資料",
            f"玩家：{target.mention}\n"
            f"新達幣：{format_balance(target.id, data[2])}\n"
            f"勝場：{data[3]:,}\n"
            f"敗場：{data[4]:,}\n"
            f"等級：{data[5]}\n"
            f"連續簽到：{streak:,} 天",
        )
    )


@bot.tree.command(name="錢包", description="查看新達幣")
@universal_command
async def wallet(interaction: discord.Interaction):
    data = player(scope_id(interaction), interaction.user.id)
    await interaction.response.send_message(
        f"你的新達幣：**{format_balance(interaction.user.id, data[2])}**"
    )


@bot.tree.command(name="簽到", description="每日領取新達幣")
@universal_command
async def daily(interaction: discord.Interaction):
    gid = scope_id(interaction)
    data = player(gid, interaction.user.id)
    today_obj = taipei_now().date()
    today = today_obj.isoformat()
    yesterday = (today_obj - timedelta(days=1)).isoformat()

    if data[6] == today:
        await interaction.response.send_message("今天已經簽到過了。", ephemeral=True)
        return

    prev_streak = data[7] if len(data) > 7 and data[7] is not None else 0
    next_streak = prev_streak + 1 if data[6] == yesterday else 1

    milestone = DAILY_MILESTONES.get(next_streak)
    bonus = milestone[1] if milestone else 0
    total_reward = DAILY_REWARD + bonus

    with database() as conn:
        if interaction.user.id == ADMIN_USER_ID:
            conn.execute(
                "UPDATE players SET last_daily=?, daily_streak=? WHERE guild=? AND user=?",
                (today, next_streak, gid, interaction.user.id),
            )
        else:
            conn.execute(
                "UPDATE players SET balance=balance+?, last_daily=?, daily_streak=? "
                "WHERE guild=? AND user=?",
                (total_reward, today, next_streak, gid, interaction.user.id),
            )

    bonus_msg = (
        f"\n恭喜達成連續簽到 **{next_streak} 天**「{milestone[0]}」，額外獲得紅包 **+{bonus:,} XDC**！"
        if milestone
        else ""
    )

    await interaction.response.send_message(
        f"簽到成功！連續簽到第 **{next_streak}** 天，獲得 **{total_reward:,}** 新達幣。{bonus_msg}"
    )


@bot.tree.command(name="排行榜", description="新達幣排行榜")
@universal_command
async def leaderboard(interaction: discord.Interaction):
    gid = scope_id(interaction)
    with database() as conn:
        rows = conn.execute(
            "SELECT user,balance FROM players "
            "WHERE guild=? ORDER BY (user = ?) DESC, balance DESC LIMIT 10",
            (gid, ADMIN_USER_ID),
        ).fetchall()

    result = ""
    for i, row in enumerate(rows, 1):
        result += f"{i}. <@{row[0]}>：{format_balance(row[0], row[1])}\n"

    title = "新達幣富豪榜" if interaction.guild else "新達幣富豪榜（伺服器外／私訊全區）"
    await interaction.response.send_message(
        embed=make_embed(title, result or "暫無資料")
    )


@bot.tree.command(name="早安樂透", description="投注每日早上 08:00（台灣時間）開獎的早安樂透")
@universal_command
async def morning_lottery(
    interaction: discord.Interaction,
    號碼: app_commands.Range[int, 1, 99],
    下注: app_commands.Range[int, 10, 100000],
):
    gid = scope_id(interaction)
    if not change_balance(gid, interaction.user.id, -下注):
        await interaction.response.send_message("新達幣不足。", ephemeral=True)
        return

    draw_date = next_draw_date_str()
    now_iso = taipei_now().isoformat()

    with database() as conn:
        conn.execute(
            "INSERT INTO lottery_tickets(draw_date, guild, user, number, bet, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (draw_date, gid, interaction.user.id, 號碼, 下注, now_iso),
        )
        meta = conn.execute("SELECT carryover_pool FROM lottery_meta WHERE id=1").fetchone()
        carryover = meta[0] if meta else 0
        stakes, count = conn.execute(
            "SELECT COALESCE(SUM(bet), 0), COUNT(*) FROM lottery_tickets WHERE draw_date=?",
            (draw_date,),
        ).fetchone()

    total_pool = LOTTERY_BASE_JACKPOT + carryover + stakes
    await interaction.response.send_message(
        embed=make_embed(
            "早安樂透投注成功｜每日 08:00 開獎",
            f"開獎日期：**{draw_date} 早上 08:00（台灣時間 UTC+8）**\n"
            f"選擇號碼：**{號碼:02d}** · 投注金額：**{下注:,} XDC**\n"
            f"目前本期總獎池：**{total_pool:,} XDC**（共 {count} 張彩券）\n\n"
            f"• **頭獎（號碼完全相符）**：均分本期獎池，保底 **50 倍**\n"
            f"• **二獎（個位數相同）**：獲得投注額 **3 倍** 獎金\n"
            f"• 開獎後將自動結算並透過私訊通知中獎結果。",
        )
    )


@bot.tree.command(name="開獎查詢", description="查看每日早上 08:00 開獎獎池與最新中獎號碼")
@universal_command
async def lottery_status(interaction: discord.Interaction):
    draw_date = next_draw_date_str()
    with database() as conn:
        meta = conn.execute("SELECT carryover_pool FROM lottery_meta WHERE id=1").fetchone()
        carryover = meta[0] if meta else 0
        stakes, count = conn.execute(
            "SELECT COALESCE(SUM(bet), 0), COUNT(*) FROM lottery_tickets WHERE draw_date=?",
            (draw_date,),
        ).fetchone()
        last_draw = conn.execute(
            "SELECT draw_date, winning_number, total_pool, ticket_count, summary "
            "FROM lottery_history ORDER BY draw_date DESC LIMIT 1"
        ).fetchone()

    total_pool = LOTTERY_BASE_JACKPOT + carryover + stakes
    if last_draw:
        last_block = (
            f"**最近一期開獎（{last_draw[0]} 08:00）**\n"
            f"中獎號碼：**{last_draw[1]:02d}** · 總獎池 **{last_draw[2]:,} XDC**\n"
            f"{last_draw[4]}"
        )
    else:
        last_block = "**最近一期開獎**：尚無開獎紀錄，第一期將於明日 08:00 開獎！"

    await interaction.response.send_message(
        embed=make_embed(
            "早安樂透｜每日早上 08:00 開獎",
            f"**下期開獎時間**：{draw_date} 08:00（台灣時間 UTC+8）\n"
            f"**本期累積獎池**：{total_pool:,} XDC（已投注 {count} 張彩券）\n\n"
            f"{last_block}\n\n"
            f"使用 `/早安樂透 號碼:1~99 下注:金額` 即可參與（伺服器外與私訊皆可投注）。",
        )
    )


@bot.tree.command(name="老虎機", description="玩老虎機")
@universal_command
async def slots(interaction: discord.Interaction, 下注: app_commands.Range[int, 10, 100000]):
    gid = scope_id(interaction)
    if not change_balance(gid, interaction.user.id, -下注):
        await interaction.response.send_message("新達幣不足。")
        return

    symbols = ["🍒", "🍋", "⭐", "💎", "7️⃣"]
    result = [random.choice(symbols) for _ in range(3)]

    if len(set(result)) == 1:
        prize = 下注 * 10
    elif len(set(result)) == 2:
        prize = 下注 * 2
    else:
        prize = 0

    if prize:
        change_balance(gid, interaction.user.id, prize)
        await win(interaction)
        text = f"中獎！獲得 {prize:,} XDC"
    else:
        add_loss(gid, interaction.user.id)
        text = f"沒有中獎，損失 {下注:,} XDC"

    await interaction.response.send_message(
        embed=make_embed("老虎機", f"{' '.join(result)}\n{text}")
    )


@bot.tree.command(name="輪盤", description="玩輪盤")
@universal_command
@app_commands.choices(選擇=[
    app_commands.Choice(name="紅", value="紅"),
    app_commands.Choice(name="黑", value="黑"),
])
async def roulette(
    interaction: discord.Interaction,
    選擇: app_commands.Choice[str],
    下注: app_commands.Range[int, 10, 100000],
):
    gid = scope_id(interaction)
    if not change_balance(gid, interaction.user.id, -下注):
        await interaction.response.send_message("新達幣不足。")
        return

    result = random.choice(["紅", "黑"])

    if result == 選擇.value:
        prize = 下注 * 2
        change_balance(gid, interaction.user.id, prize)
        await win(interaction)
        text = f"猜中！獲得 {prize:,} XDC"
    else:
        add_loss(gid, interaction.user.id)
        text = f"猜錯了，損失 {下注:,} XDC"

    await interaction.response.send_message(
        embed=make_embed("輪盤", f"結果：{result}\n{text}")
    )


@bot.tree.command(name="骰寶", description="猜大小")
@universal_command
@app_commands.choices(選擇=[
    app_commands.Choice(name="大", value="大"),
    app_commands.Choice(name="小", value="小"),
])
async def dice(
    interaction: discord.Interaction,
    選擇: app_commands.Choice[str],
    下注: app_commands.Range[int, 10, 100000],
):
    gid = scope_id(interaction)
    if not change_balance(gid, interaction.user.id, -下注):
        await interaction.response.send_message("新達幣不足。")
        return

    numbers = [random.randint(1, 6) for _ in range(3)]
    total = sum(numbers)
    result = "大" if total >= 11 else "小"

    if result == 選擇.value:
        prize = 下注 * 2
        change_balance(gid, interaction.user.id, prize)
        await win(interaction)
        text = f"猜中！獲得 {prize:,} XDC"
    else:
        add_loss(gid, interaction.user.id)
        text = f"猜錯了，損失 {下注:,} XDC"

    await interaction.response.send_message(
        embed=make_embed("骰寶", f"骰子：{numbers}\n總點數：{total}\n{text}")
    )


@bot.tree.command(name="刮刮樂", description="玩虛擬刮刮樂")
@universal_command
async def scratch(interaction: discord.Interaction, 下注: app_commands.Range[int, 10, 100000]):
    gid = scope_id(interaction)
    if not change_balance(gid, interaction.user.id, -下注):
        await interaction.response.send_message("新達幣不足。")
        return

    symbols = ["🍒", "⭐", "💎", "🍀", "💰"]
    cards = [random.choice(symbols) for _ in range(3)]

    if len(set(cards)) == 1:
        prize = 下注 * 8
    elif len(set(cards)) == 2:
        prize = 下注 * 2
    else:
        prize = 0

    if prize:
        change_balance(gid, interaction.user.id, prize)
        await win(interaction)
    else:
        add_loss(gid, interaction.user.id)

    await interaction.response.send_message(
        embed=make_embed("刮刮樂", f"{' '.join(cards)}\n獎金：{prize:,} XDC")
    )


@bot.tree.command(name="猜數字", description="猜中數字獲得五倍")
@universal_command
async def guess(
    interaction: discord.Interaction,
    號碼: app_commands.Range[int, 1, 6],
    下注: app_commands.Range[int, 10, 100000],
):
    gid = scope_id(interaction)
    if not change_balance(gid, interaction.user.id, -下注):
        await interaction.response.send_message("新達幣不足。")
        return

    result = random.randint(1, 6)

    if result == 號碼:
        prize = 下注 * 5
        change_balance(gid, interaction.user.id, prize)
        await win(interaction)
    else:
        prize = 0
        add_loss(gid, interaction.user.id)

    await interaction.response.send_message(
        embed=make_embed("猜數字", f"開出：{result}\n獎金：{prize:,} XDC")
    )


@bot.tree.command(name="擲硬幣", description="猜正反面")
@universal_command
@app_commands.choices(選擇=[
    app_commands.Choice(name="正面", value="正面"),
    app_commands.Choice(name="反面", value="反面"),
])
async def coin(
    interaction: discord.Interaction,
    選擇: app_commands.Choice[str],
    下注: app_commands.Range[int, 10, 100000],
):
    gid = scope_id(interaction)
    if not change_balance(gid, interaction.user.id, -下注):
        await interaction.response.send_message("新達幣不足。")
        return

    result = random.choice(["正面", "反面"])

    if result == 選擇.value:
        prize = 下注 * 2
        change_balance(gid, interaction.user.id, prize)
        await win(interaction)
    else:
        prize = 0
        add_loss(gid, interaction.user.id)

    await interaction.response.send_message(
        embed=make_embed("擲硬幣", f"結果：{result}\n獎金：{prize:,} XDC")
    )


@bot.tree.command(name="二十一點", description="簡易二十一點")
@universal_command
async def blackjack(interaction: discord.Interaction, 下注: app_commands.Range[int, 10, 100000]):
    gid = scope_id(interaction)
    if not change_balance(gid, interaction.user.id, -下注):
        await interaction.response.send_message("新達幣不足。")
        return

    def draw():
        return random.randint(1, 13)

    def score(cards):
        total = sum(min(x, 10) for x in cards)
        aces = cards.count(1)
        while total <= 11 and aces:
            total += 10
            aces -= 1
        return total

    user_cards = [draw(), draw()]
    dealer_cards = [draw(), draw()]

    while score(user_cards) < 17:
        user_cards.append(draw())

    while score(dealer_cards) < 17:
        dealer_cards.append(draw())

    u = score(user_cards)
    d = score(dealer_cards)

    if u <= 21 and (d > 21 or u > d):
        prize = 下注 * 2
        change_balance(gid, interaction.user.id, prize)
        await win(interaction)
    elif u == d:
        prize = 下注
        change_balance(gid, interaction.user.id, prize)
    else:
        prize = 0
        add_loss(gid, interaction.user.id)

    await interaction.response.send_message(
        embed=make_embed(
            "二十一點",
            f"你的點數：{u}\n莊家點數：{d}\n返還：{prize:,} XDC",
        )
    )


@bot.tree.command(name="百家樂", description="簡易百家樂")
@universal_command
@app_commands.choices(選擇=[
    app_commands.Choice(name="莊", value="莊"),
    app_commands.Choice(name="閒", value="閒"),
    app_commands.Choice(name="和", value="和"),
])
async def baccarat(
    interaction: discord.Interaction,
    選擇: app_commands.Choice[str],
    下注: app_commands.Range[int, 10, 100000],
):
    gid = scope_id(interaction)
    if not change_balance(gid, interaction.user.id, -下注):
        await interaction.response.send_message("新達幣不足。")
        return

    p = random.randint(0, 9)
    b = random.randint(0, 9)
    result = "閒" if p > b else "莊" if b > p else "和"

    if result == 選擇.value:
        prize = 下注 * (8 if result == "和" else 2)
        change_balance(gid, interaction.user.id, prize)
        await win(interaction)
    else:
        prize = 0
        add_loss(gid, interaction.user.id)

    await interaction.response.send_message(
        embed=make_embed(
            "百家樂",
            f"閒家：{p}\n莊家：{b}\n結果：{result}\n獎金：{prize:,} XDC",
        )
    )


@bot.tree.command(name="授予賭神", description="管理員授予永久賭神")
@universal_command
async def grant_god(interaction: discord.Interaction, 玩家: discord.User):
    if not await check_admin(interaction):
        return

    gid = scope_id(interaction)
    player(gid, 玩家.id)

    with database() as conn:
        conn.execute(
            "UPDATE players SET level='賭神' WHERE guild=? AND user=?",
            (gid, 玩家.id),
        )

    if interaction.guild and isinstance(玩家, discord.Member):
        role = discord.utils.get(interaction.guild.roles, name="賭神")
        if role:
            await 玩家.add_roles(role)

    await interaction.response.send_message(
        f"{玩家.mention} 已獲得永久賭神！"
    )


@bot.tree.command(name="發放新達幣", description="管理員發放新達幣")
@universal_command
async def give(
    interaction: discord.Interaction,
    玩家: discord.User,
    數量: app_commands.Range[int, 1, 10000000],
):
    if not await check_admin(interaction):
        return

    change_balance(scope_id(interaction), 玩家.id, 數量)

    await interaction.response.send_message(
        f"已發放 {數量:,} XDC 給 {玩家.mention}"
    )


@bot.tree.command(name="扣除新達幣", description="管理員扣除新達幣")
@universal_command
async def take(
    interaction: discord.Interaction,
    玩家: discord.User,
    數量: app_commands.Range[int, 1, 10000000],
):
    if not await check_admin(interaction):
        return

    if 玩家.id == ADMIN_USER_ID:
        await interaction.response.send_message(f"{玩家.mention} 擁有無限新達幣，無需扣除。")
        return

    if not change_balance(scope_id(interaction), 玩家.id, -數量):
        await interaction.response.send_message("玩家餘額不足。")
        return

    await interaction.response.send_message(
        f"已扣除 {玩家.mention} 的 {數量:,} XDC"
    )


@bot.tree.command(name="新達賭場", description="查看所有指令")
@universal_command
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=make_embed(
            "新達虛擬賭場",
            """
**玩家與早安樂透指令（伺服器內／私訊／伺服器外皆可用）**
`/個人資料` `/錢包` `/簽到` `/排行榜` `/早安樂透` `/開獎查詢`

**賭場設施**
`/老虎機` `/輪盤` `/骰寶` `/刮刮樂`
`/猜數字` `/擲硬幣` `/二十一點` `/百家樂`

**每日早上 08:00 開獎（台灣時間 UTC+8）**
使用 `/早安樂透` 選擇 `1–99` 號碼投注，每天 08:00 自動開獎。

**管理員**
`/賭場設定` `/設定開獎頻道` `/授予賭神`
`/發放新達幣` `/扣除新達幣`

**等級制度**
賭神：10,000 勝｜賭俠：5,000 勝｜賭聖：1,000 勝｜賭鬼：100 勝｜賭徒：無限制
""",
        )
    )


if __name__ == "__main__":
    init_db()
    if not TOKEN:
        raise SystemExit("請設定環境變數 DISCORD_TOKEN")
    bot.run(TOKEN)
