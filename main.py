import os
import json
import random
import asyncio
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

COMMAND_ROLE_ID = 1545072135297572885
FULL_ACCESS_ROLE_IDS = {1544778851748417577, 1545039825080684554}
VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID", "0"))

DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATA_FILE = DATA_DIR / "economy.json"
CURRENCY_NAME = "gems"
STARTING_BALANCE = 100
BACKUP_FILE = DATA_FILE.with_suffix(".bak")

if not DATA_FILE.exists():
    DATA_FILE.write_text("{}", encoding="utf-8")

MINES_GRID_SIZE = 5  # 5x5 grid (24 playable tiles + 1 Cash Out button)

# ----------------------------------------------------------------------------
# ECONOMY STORAGE
# ----------------------------------------------------------------------------

_data_lock = asyncio.Lock()


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _load_data() -> dict:
    data = _read_json(DATA_FILE)
    if data is not None:
        return data

    backup = _read_json(BACKUP_FILE)
    if backup is not None:
        print(f"[economy] {DATA_FILE.name} was missing/corrupted — restored from backup.")
        _save_data(backup)
        return backup

    print(f"[economy] WARNING: {DATA_FILE.name} and backup missing. Starting fresh ledger.")
    return {}


def _save_data(data: dict) -> None:
    temp_file = DATA_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, DATA_FILE)

    if data:
        backup_temp = BACKUP_FILE.with_suffix(".tmp")
        with open(backup_temp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(backup_temp, BACKUP_FILE)


def _get_user_entry(data: dict, user_id: int | str) -> dict:
    val = data.get(str(user_id))
    if isinstance(val, dict):
        return {"balance": val.get("balance", STARTING_BALANCE), "wagered": val.get("wagered", 0)}
    if isinstance(val, int):
        return {"balance": val, "wagered": 0}
    return {"balance": STARTING_BALANCE, "wagered": 0}


async def get_balance(user_id: int) -> int:
    async with _data_lock:
        data = _load_data()
        entry = _get_user_entry(data, user_id)
        return entry["balance"]


async def set_balance(user_id: int, amount: int) -> None:
    async with _data_lock:
        data = _load_data()
        entry = _get_user_entry(data, user_id)
        entry["balance"] = max(0, amount)
        data[str(user_id)] = entry
        _save_data(data)


async def add_balance(user_id: int, amount: int) -> int:
    async with _data_lock:
        data = _load_data()
        entry = _get_user_entry(data, user_id)
        new_balance = max(0, entry["balance"] + amount)
        entry["balance"] = new_balance
        data[str(user_id)] = entry
        _save_data(data)
        return new_balance


async def add_wager_stat(user_id: int, amount: int) -> int:
    async with _data_lock:
        data = _load_data()
        entry = _get_user_entry(data, user_id)
        new_wagered = entry["wagered"] + max(0, amount)
        entry["wagered"] = new_wagered
        data[str(user_id)] = entry
        _save_data(data)
        return new_wagered


async def get_wager_stat(user_id: int) -> int:
    async with _data_lock:
        data = _load_data()
        entry = _get_user_entry(data, user_id)
        return entry["wagered"]


# ----------------------------------------------------------------------------
# PERMISSION CHECKS & TAX
# ----------------------------------------------------------------------------

TAX_RATE = 0.07  # 7% Tax Rate


def apply_tax(profit: int) -> tuple[int, int]:
    if profit <= 0:
        return profit, 0
    tax = round(profit * TAX_RATE)
    return profit - tax, tax


def gambling_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return False

        has_command_role = any(role.id == COMMAND_ROLE_ID for role in member.roles)
        has_full_access_role = any(role.id in FULL_ACCESS_ROLE_IDS for role in member.roles)

        if not (has_command_role or has_full_access_role):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return False

        return True

    return app_commands.check(predicate)


def admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return False

        if not any(role.id in FULL_ACCESS_ROLE_IDS for role in member.roles):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return False

        return True

    return app_commands.check(predicate)


def fmt(amount: int) -> str:
    return f"{amount:,} {CURRENCY_NAME}"


def parse_amount(value: str) -> int:
    value = value.strip().lower().replace(",", "")
    multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    if value and value[-1] in multipliers:
        try:
            return int(float(value[:-1]) * multipliers[value[-1]])
        except ValueError:
            return 0
    try:
        return int(value)
    except ValueError:
        return 0


# ----------------------------------------------------------------------------
# BOT SETUP
# ----------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Economy data directory: {DATA_DIR.resolve()}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"Sync failed: {e}")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


# ----------------------------------------------------------------------------
# PROMO CODES
# ----------------------------------------------------------------------------

async def add_promo_code(code: str, amount: int, max_uses: int | None = None) -> bool:
    async with _data_lock:
        data = _load_data()
        promos = data.get("_promo_codes", {})
        key = code.strip().upper()
        if not key or key in promos:
            return False
        promos[key] = {"amount": amount, "max_uses": max_uses, "uses": 0, "redeemed_by": []}
        data["_promo_codes"] = promos
        _save_data(data)
        return True


async def redeem_promo_code(user_id: int, code: str) -> tuple[str, int]:
    async with _data_lock:
        data = _load_data()
        promos = data.get("_promo_codes", {})
        key = code.strip().upper()
        promo = promos.get(key)

        if not isinstance(promo, dict):
            return "invalid", 0

        redeemed_by = promo.setdefault("redeemed_by", [])
        user_key = str(user_id)

        if user_key in redeemed_by:
            return "already_redeemed", 0

        max_uses = promo.get("max_uses")
        uses = int(promo.get("uses", 0))
        if max_uses is not None and uses >= int(max_uses):
            return "used_up", 0

        amount = int(promo.get("amount", 0))
        if amount <= 0:
            return "invalid", 0

        entry = _get_user_entry(data, user_key)
        entry["balance"] = max(0, entry["balance"] + amount)
        data[user_key] = entry
        promo["uses"] = uses + 1
        redeemed_by.append(user_key)
        promos[key] = promo
        data["_promo_codes"] = promos
        _save_data(data)
        return "success", amount


@bot.tree.command(name="addpromo", description="[Admin only] Create a promo code that gives gems.")
@admin_check()
async def addpromo(interaction: discord.Interaction, code: str, amount: str, max_uses: int | None = None):
    code = code.strip().upper()
    gem_amount = parse_amount(amount)

    if not code or len(code) > 50 or gem_amount <= 0:
        await interaction.response.send_message("Invalid code or amount.", ephemeral=True)
        return

    if await add_promo_code(code, gem_amount, max_uses):
        embed = discord.Embed(
            title="🎟️ Promo Code Created",
            description=f"**Code:** `{code}`\n**Reward:** 💎 **{fmt(gem_amount)}**",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message(f"Code `{code}` already exists.", ephemeral=True)


@bot.tree.command(name="redeem", description="Redeem a promo code for gems.")
async def redeem(interaction: discord.Interaction, code: str):
    if not isinstance(interaction.user, discord.Member) or not any(
        role.id == COMMAND_ROLE_ID for role in interaction.user.roles
    ):
        await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
        return

    status, amount = await redeem_promo_code(interaction.user.id, code)
    if status == "success":
        new_bal = await get_balance(interaction.user.id)
        embed = discord.Embed(
            title="🎉 Promo Redeemed!",
            description=f"Received **{fmt(amount)}**.\nNew Balance: 💎 **{fmt(new_bal)}**",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message(f"❌ Failed to redeem ({status}).", ephemeral=True)


# ----------------------------------------------------------------------------
# ECONOMY COMMANDS
# ----------------------------------------------------------------------------

@bot.tree.command(name="balance", description="Check gem balance and wager statistics.")
@gambling_check()
async def balance(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user
    bal = await get_balance(target.id)
    wagered = await get_wager_stat(target.id)
    embed = discord.Embed(
        title=f"{target.display_name}'s Profile",
        description=f"💎 **Balance:** {fmt(bal)}\n🎰 **Total Wagered:** {fmt(wagered)}",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="tip", description="Send gems to another user.")
@gambling_check()
async def tip(interaction: discord.Interaction, user: discord.User, amount: str):
    amt = parse_amount(amount)
    if amt <= 0 or user.id == interaction.user.id or user.bot:
        await interaction.response.send_message("Invalid tip details.", ephemeral=True)
        return

    sender_bal = await get_balance(interaction.user.id)
    if sender_bal < amt:
        await interaction.response.send_message("Insufficient gems.", ephemeral=True)
        return

    await add_balance(interaction.user.id, -amt)
    await add_balance(user.id, amt)
    await interaction.response.send_message(embed=discord.Embed(description=f"💸 {interaction.user.mention} tipped {user.mention} **{fmt(amt)}**", color=discord.Color.green()))


@bot.tree.command(name="addgems", description="[Admin only] Add gems.")
@admin_check()
async def addgems(interaction: discord.Interaction, user: discord.User, amount: str):
    amt = parse_amount(amount)
    if amt <= 0:
        return
    new_bal = await add_balance(user.id, amt)
    await interaction.response.send_message(embed=discord.Embed(description=f"✅ Added **{fmt(amt)}** to {user.mention}. New balance: **{fmt(new_bal)}**", color=discord.Color.green()))


@bot.tree.command(name="removegems", description="[Admin only] Remove gems.")
@admin_check()
async def removegems(interaction: discord.Interaction, user: discord.User, amount: str):
    amt = parse_amount(amount)
    if amt <= 0:
        return
    new_bal = await add_balance(user.id, -amt)
    await interaction.response.send_message(embed=discord.Embed(description=f"✅ Removed **{fmt(amt)}** from {user.mention}. New balance: **{fmt(new_bal)}**", color=discord.Color.orange()))


# ----------------------------------------------------------------------------
# COINFLIP
# ----------------------------------------------------------------------------

@bot.tree.command(name="coinflip", description="Bet gems on a coinflip.")
@app_commands.choices(choice=[app_commands.Choice(name="Heads", value="heads"), app_commands.Choice(name="Tails", value="tails")])
@gambling_check()
async def coinflip(interaction: discord.Interaction, amount: str, choice: app_commands.Choice[str]):
    amt = parse_amount(amount)
    if amt <= 0:
        return
    bal = await get_balance(interaction.user.id)
    if bal < amt:
        await interaction.response.send_message("Insufficient gems.", ephemeral=True)
        return

    await add_wager_stat(interaction.user.id, amt)

    res = random.choice(["heads", "tails"])
    if res == choice.value:
        net_win, tax = apply_tax(amt)
        new_bal = await add_balance(interaction.user.id, net_win)
        embed = discord.Embed(title="🪙 Coinflip — You Won!", description=f"Landed on **{res}**.\nWon **{fmt(net_win)}** (after {int(TAX_RATE * 100)}% tax of {fmt(tax)}).\nBalance: {fmt(new_bal)}", color=discord.Color.green())
    else:
        new_bal = await add_balance(interaction.user.id, -amt)
        embed = discord.Embed(title="🪙 Coinflip — You Lost", description=f"Landed on **{res}**.\nLost **{fmt(amt)}**.\nBalance: {fmt(new_bal)}", color=discord.Color.red())

    await interaction.response.send_message(embed=embed)


# ----------------------------------------------------------------------------
# BLACKJACK
# ----------------------------------------------------------------------------

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]


def new_deck():
    deck = [f"{r}{s}" for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def hand_value(hand: list[str]) -> int:
    total = sum(11 if c[:-1] == "A" else 10 if c[:-1] in ("J", "Q", "K") else int(c[:-1]) for c in hand)
    aces = sum(1 for c in hand if c.startswith("A"))
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


class BlackjackView(discord.ui.View):
    def __init__(self, player_id: int, deck: list[str], player_hand: list[str], dealer_hand: list[str], bet: int):
        super().__init__(timeout=60)
        self.player_id, self.deck, self.player_hand, self.dealer_hand, self.bet = player_id, deck, player_hand, dealer_hand, bet
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.player_id

    def build_embed(self, reveal: bool = False) -> discord.Embed:
        embed = discord.Embed(title="🃏 Blackjack", color=discord.Color.blurple())
        embed.add_field(name=f"Your hand ({hand_value(self.player_hand)})", value=" ".join(self.player_hand), inline=False)
        dealer_val = " ".join(self.dealer_hand) if reveal else f"{self.dealer_hand[0]} 🂠"
        embed.add_field(name="Dealer's hand", value=dealer_val, inline=False)
        embed.set_footer(text=f"Bet: {fmt(self.bet)}")
        return embed

    async def end_game(self, interaction: discord.Interaction | None, result: str, payout: int):
        self.finished = True
        for child in self.children:
            child.disabled = True
        net_payout, tax = apply_tax(payout)
        new_bal = await add_balance(self.player_id, net_payout)
        embed = self.build_embed(reveal=True)
        text = f"{result.upper()} — New balance: {fmt(new_bal)}"
        if tax:
            text += f" (Won {fmt(net_payout)} after {int(TAX_RATE * 100)}% tax of {fmt(tax)})"
        embed.add_field(name="Result", value=text, inline=False)
        if interaction and not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(name="blackjack", description="Play a round of blackjack.")
@gambling_check()
async def blackjack(interaction: discord.Interaction, amount: str):
    amt = parse_amount(amount)
    if amt <= 0:
        return
    bal = await get_balance(interaction.user.id)
    if bal < amt:
        await interaction.response.send_message("Insufficient gems.", ephemeral=True)
        return

    await add_wager_stat(interaction.user.id, amt)

    deck = new_deck()
    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]
    view = BlackjackView(interaction.user.id, deck, player_hand, dealer_hand, amt)

    if hand_value(player_hand) == 21:
        gross = int(amt * 1.5)
        net, tax = apply_tax(gross)
        new_bal = await add_balance(interaction.user.id, net)
        embed = view.build_embed(reveal=True)
        embed.add_field(name="Result", value=f"BLACKJACK! Won {fmt(net)} (after {int(TAX_RATE * 100)}% tax). Balance: {fmt(new_bal)}")
        await interaction.response.send_message(embed=embed)
        return

    await interaction.response.send_message(embed=view.build_embed(), view=view)


# ----------------------------------------------------------------------------
# CRASH
# ----------------------------------------------------------------------------

CRASH_TICK_SECONDS = 1.0
CRASH_GROWTH_MIN = 0.04
CRASH_GROWTH_MAX = 0.08
CRASH_MAX_MULTIPLIER = 20.0
CRASH_MAX_TICKS = 90


class CrashView(discord.ui.View):
    def __init__(self, player_id: int, bet: int, crash_point: float):
        super().__init__(timeout=None)
        self.player_id = player_id
        self.bet = bet
        self.crash_point = crash_point
        self.cashed_out = False
        self.crashed = False
        self.multiplier = 1.0
        self.net_profit = 0
        self.tax = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.player_id

    def build_embed(self) -> discord.Embed:
        if self.crashed:
            return discord.Embed(title="🚀 Crash", description=f"💥 Crashed at **{self.crash_point:.2f}x**\nLost **{fmt(self.bet)}**", color=discord.Color.red())
        if self.cashed_out:
            return discord.Embed(title="🚀 Crash", description=f"✅ Cashed out at **{self.multiplier:.2f}x**\nWon **{fmt(self.net_profit)}** (after {int(TAX_RATE * 100)}% tax of {fmt(self.tax)})", color=discord.Color.green())
        return discord.Embed(title="🚀 Crash", description=f"📈 Multiplier: **{self.multiplier:.2f}x**\nBet: {fmt(self.bet)}", color=discord.Color.blurple())

    @discord.ui.button(label="Cash Out", style=discord.ButtonStyle.success)
    async def cash_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.cashed_out or self.crashed:
            return
        self.cashed_out = True
        for child in self.children:
            child.disabled = True
        gross = int(self.bet * self.multiplier) - self.bet
        self.net_profit, self.tax = apply_tax(gross)
        await add_balance(self.player_id, self.bet + self.net_profit)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        self.stop()


@bot.tree.command(name="crash", description="Watch the multiplier rise and cash out before it crashes.")
@gambling_check()
async def crash(interaction: discord.Interaction, amount: str):
    amt = parse_amount(amount)
    if amt <= 0:
        return
    bal = await get_balance(interaction.user.id)
    if bal < amt:
        await interaction.response.send_message("Insufficient gems.", ephemeral=True)
        return

    await add_wager_stat(interaction.user.id, amt)
    await add_balance(interaction.user.id, -amt)
    crash_point = round(max(1.01, min((0.99 / (1 - random.random())) ** 0.5, CRASH_MAX_MULTIPLIER)), 2)

    view = CrashView(interaction.user.id, amt, crash_point)
    await interaction.response.send_message(embed=view.build_embed(), view=view)
    msg = await interaction.original_response()

    for _ in range(CRASH_MAX_TICKS):
        if view.cashed_out:
            return
        await asyncio.sleep(CRASH_TICK_SECONDS)
        if view.cashed_out:
            return

        view.multiplier = round(view.multiplier * (1 + random.uniform(CRASH_GROWTH_MIN, CRASH_GROWTH_MAX)), 2)

        if view.multiplier >= crash_point:
            view.crashed = True
            for child in view.children:
                child.disabled = True
            await msg.edit(embed=view.build_embed(), view=view)
            view.stop()
            return

        try:
            await msg.edit(embed=view.build_embed(), view=view)
        except discord.HTTPException:
            pass


# ----------------------------------------------------------------------------
# MINES GAME (1x to 2x Scaling, Dynamic Multiplier per Mine Count)
# ----------------------------------------------------------------------------

class MineButton(discord.ui.Button):
    def __init__(self, index: int, row: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="❓", row=row)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        view: MinesView = self.view
        if interaction.user.id != view.player_id or view.finished:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return
        await view.reveal_tile(interaction, self)


class CashOutMinesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.success, label="💰 Cash Out", row=4)

    async def callback(self, interaction: discord.Interaction):
        view: MinesView = self.view
        if interaction.user.id != view.player_id or view.finished:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return
        await view.cash_out(interaction)


class MinesView(discord.ui.View):
    def __init__(self, player_id: int, bet: int, mines_count: int):
        super().__init__(timeout=120)
        self.player_id = player_id
        self.bet = bet
        self.mines_count = mines_count
        self.total_tiles = 24  # 24 playable tiles on 5x5 grid (1 button reserved for Cash Out)
        self.safe_tiles_count = self.total_tiles - mines_count
        self.revealed_count = 0
        self.finished = False
        self.cashed_out = False
        self.crashed = False

        self.mine_indices = set(random.sample(range(self.total_tiles), mines_count))
        self.revealed_indices = set()

        self.buttons: list[MineButton] = []
        for idx in range(self.total_tiles):
            row = idx // 5
            btn = MineButton(idx, row)
            self.buttons.append(btn)
            self.add_item(btn)

        self.cash_out_btn = CashOutMinesButton()
        self.add_item(self.cash_out_btn)

    def calculate_multiplier(self) -> float:
        """
        Scales multiplier from 1.0x to 2.0x based on safe tiles revealed.
        More mines = smaller safe_tiles_count = bigger increase per tile revealed.
        Reaches EXACTLY 2.0x when the final safe tile is revealed.
        """
        if self.revealed_count == 0:
            return 1.00
        mult = 1.0 + (self.revealed_count / self.safe_tiles_count)
        return round(mult, 2)

    def build_embed(self) -> discord.Embed:
        mult = self.calculate_multiplier()

        if self.crashed:
            return discord.Embed(
                title="💣 Mines — BOOM!",
                description=f"You hit a mine!\nYou lost **{fmt(self.bet)}**.",
                color=discord.Color.red(),
            )
        if self.cashed_out:
            gross_profit = int(self.bet * mult) - self.bet
            net_profit, tax = apply_tax(gross_profit)
            return discord.Embed(
                title="💣 Mines — Cashed Out!",
                description=(
                    f"Cashed out at **{mult:.2f}x**!\n"
                    f"You won **{fmt(net_profit)}** (after {int(TAX_RATE * 100)}% tax of {fmt(tax)})."
                ),
                color=discord.Color.green(),
            )

        current_payout = int(self.bet * mult)
        return discord.Embed(
            title="💣 Mines",
            description=(
                f"**Mines:** {self.mines_count} | **Safe Tiles Remaining:** {self.safe_tiles_count - self.revealed_count}\n"
                f"**Current Multiplier:** {mult:.2f}x\n"
                f"**Current Cash Out Value:** {fmt(current_payout)}"
            ),
            color=discord.Color.blurple(),
        )

    async def reveal_tile(self, interaction: discord.Interaction, button: MineButton):
        if button.index in self.revealed_indices:
            await interaction.response.send_message("Tile already revealed!", ephemeral=True)
            return

        self.revealed_indices.add(button.index)

        if button.index in self.mine_indices:
            # Hit Mine
            self.finished = True
            self.crashed = True
            for btn in self.buttons:
                btn.disabled = True
                if btn.index in self.mine_indices:
                    btn.style = discord.ButtonStyle.danger
                    btn.label = "💣"
            self.cash_out_btn.disabled = True
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            # Safe Tile
            self.revealed_count += 1
            button.style = discord.ButtonStyle.success
            button.label = "💎"
            button.disabled = True

            # If all safe tiles revealed -> reach 2.0x and automatically win
            if self.revealed_count == self.safe_tiles_count:
                await self.do_cash_out(interaction)
            else:
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def cash_out(self, interaction: discord.Interaction):
        if self.revealed_count == 0:
            await interaction.response.send_message("Reveal at least 1 safe tile before cashing out!", ephemeral=True)
            return
        await self.do_cash_out(interaction)

    async def do_cash_out(self, interaction: discord.Interaction):
        self.finished = True
        self.cashed_out = True

        mult = self.calculate_multiplier()
        gross_profit = int(self.bet * mult) - self.bet
        net_profit, tax = apply_tax(gross_profit)

        # Refund original bet + net winnings
        await add_balance(self.player_id, self.bet + net_profit)

        for btn in self.buttons:
            btn.disabled = True
            if btn.index in self.mine_indices:
                btn.style = discord.ButtonStyle.danger
                btn.label = "💣"

        self.cash_out_btn.disabled = True
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


@bot.tree.command(name="mines", description="Play Mines! Reveal safe tiles and cash out before hitting a mine.")
@app_commands.describe(amount="How many gems to bet", mines="Number of mines (1-23)")
@gambling_check()
async def mines(interaction: discord.Interaction, amount: str, mines: int):
    amt = parse_amount(amount)
    if amt <= 0:
        await interaction.response.send_message("Bet must be positive.", ephemeral=True)
        return

    if mines < 1 or mines > 23:
        await interaction.response.send_message("Mines count must be between 1 and 23.", ephemeral=True)
        return

    bal = await get_balance(interaction.user.id)
    if bal < amt:
        await interaction.response.send_message(f"You don't have enough {CURRENCY_NAME}. Balance: {fmt(bal)}", ephemeral=True)
        return

    await add_wager_stat(interaction.user.id, amt)
    await add_balance(interaction.user.id, -amt)
    view = MinesView(interaction.user.id, amt, mines)
    await interaction.response.send_message(embed=view.build_embed(), view=view)


# ----------------------------------------------------------------------------
# RUN BOT
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN is missing from environment.")
    else:
        bot.run(DISCORD_TOKEN)
