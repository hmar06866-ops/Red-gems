"""
Discord Gambling/Economy Bot
=============================
Games: Blackjack, Coinflip, Crash, Mines
Economy: /balance, /tip, /addgems, /removegems, /addpromo, /redeem

Permissions:
- All gambling + economy commands (balance, tip, blackjack, coinflip, crash, mines)
  require the role with ID: 1545072135297572885
- /addgems and /removegems are restricted to these two user IDs ONLY:
  1545039825080684554
  1544778851748417577

Setup:
1. pip install -r requirements.txt
2. Create a .env file next to this script with:
       DISCORD_TOKEN=your_token_here
3. python main.py
"""

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
# Set VERIFIED_ROLE_ID in your .env to the role required to use /redeem.
# Users without this role cannot redeem promo codes.
VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID", "0"))

# DATA_DIR should point at a mounted persistent Volume in production (e.g. on
# Railway: Settings -> Volumes -> mount path, then set DATA_DIR to that same
# path as an env var, e.g. DATA_DIR=/data). Without a real Volume, anything
# written here gets wiped on every redeploy/restart — the bot has no way to
# detect or prevent that from inside the code itself.
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATA_FILE = DATA_DIR / "economy.json"
CURRENCY_NAME = "gems"
STARTING_BALANCE = 100

# Every user's balance is stored separately in economy.json using their
# Discord user ID as the key. A .bak copy is kept alongside it so a
# corrupted or empty read never gets silently saved over real data.
BACKUP_FILE = DATA_FILE.with_suffix(".bak")

if not DATA_FILE.exists():
    DATA_FILE.write_text("{}", encoding="utf-8")

MINES_GRID_SIZE = 5  # 5 columns; 24 playable tiles + 1 Cash Out button

# ----------------------------------------------------------------------------
# ECONOMY STORAGE (simple JSON-backed, async-safe with a lock)
# ----------------------------------------------------------------------------

_data_lock = asyncio.Lock()


def _read_json(path: Path) -> dict | None:
    """Return the dict in `path`, or None if it's missing/unreadable/empty."""
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
    """
    Load economy.json. If it's missing, empty, or corrupted, fall back to the
    last known-good backup instead of silently returning {} — an empty dict
    here would otherwise get written straight back out on the next save and
    permanently wipe everyone's balance.
    """
    data = _read_json(DATA_FILE)
    if data is not None:
        return data

    backup = _read_json(BACKUP_FILE)
    if backup is not None:
        print(f"[economy] {DATA_FILE.name} was missing/corrupted — restored from backup.")
        _save_data(backup)
        return backup

    print(f"[economy] WARNING: {DATA_FILE.name} and its backup are both missing/corrupted. "
          f"Starting from an empty ledger — check for a bad deploy or ephemeral filesystem.")
    return {}


def _save_data(data: dict) -> None:
    """Save each user's data safely, keyed by their Discord user ID, and refresh the backup."""
    temp_file = DATA_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, DATA_FILE)

    # Refresh the backup only with data we know is non-empty, so the backup
    # itself never becomes a copy of a wiped ledger.
    if data:
        backup_temp = BACKUP_FILE.with_suffix(".tmp")
        with open(backup_temp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(backup_temp, BACKUP_FILE)


async def get_balance(user_id: int) -> int:
    async with _data_lock:
        data = _load_data()
        return data.get(str(user_id), STARTING_BALANCE)


async def set_balance(user_id: int, amount: int) -> None:
    async with _data_lock:
        data = _load_data()
        data[str(user_id)] = max(0, amount)
        _save_data(data)


async def add_balance(user_id: int, amount: int) -> int:
    async with _data_lock:
        data = _load_data()
        current = data.get(str(user_id), STARTING_BALANCE)
        new_balance = max(0, current + amount)
        data[str(user_id)] = new_balance
        _save_data(data)
        return new_balance


# ----------------------------------------------------------------------------
# PERMISSION CHECKS
# ----------------------------------------------------------------------------


def gambling_check():
    """Requires the command role or a full-access role for normal commands."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return False

        has_command_role = any(role.id == COMMAND_ROLE_ID for role in member.roles)
        has_full_access_role = any(role.id in FULL_ACCESS_ROLE_IDS for role in member.roles)

        if not (has_command_role or has_full_access_role):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return False

        return True

    return app_commands.check(predicate)


def admin_check():
    """Only full-access roles can use /addgems and /removegems."""

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return False

        has_full_access_role = any(role.id in FULL_ACCESS_ROLE_IDS for role in member.roles)

        if not has_full_access_role:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return False

        return True

    return app_commands.check(predicate)


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


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        # Permission message already sent inside the check predicate.
        return
    print(f"Unhandled command error: {error}")
    if not interaction.response.is_done():
        await interaction.response.send_message("Something went wrong running that command.", ephemeral=True)


def fmt(amount: int) -> str:
    return f"{amount:,} {CURRENCY_NAME}"


# House tax: taken out of winnings only (never out of losses/pushes/the
# original bet), applied the same way across coinflip, blackjack, crash, and
# mines so the house edge is consistent regardless of which game is played.
TAX_RATE = 0.05


def apply_tax(profit: int) -> tuple[int, int]:
    """
    Given a gross profit amount from a win, return (net_profit, tax_amount).
    Only positive profit is taxed — a loss (negative) or a push (zero) passes
    through unchanged so the tax never bites into money the player didn't
    actually win.
    """
    if profit <= 0:
        return profit, 0
    tax = round(profit * TAX_RATE)
    return profit - tax, tax


def parse_amount(value: str) -> int:
    """Parse amounts like 1000000, 1m, 1.5m, 250k, etc."""
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
# PROMO CODES
# ----------------------------------------------------------------------------

async def add_promo_code(code: str, amount: int, max_uses: int | None = None) -> bool:
    """Create/update a promo code. Returns False if the code already exists."""
    async with _data_lock:
        data = _load_data()
        promos = data.get("_promo_codes", {})
        key = code.strip().upper()
        if not key or key in promos:
            return False
        promos[key] = {
            "amount": amount,
            "max_uses": max_uses,
            "uses": 0,
            "redeemed_by": [],
        }
        data["_promo_codes"] = promos
        _save_data(data)
        return True


async def redeem_promo_code(user_id: int, code: str) -> tuple[str, int]:
    """Redeem a promo code once per user. Returns (status, amount)."""
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

        # Add the gems and mark the code as redeemed atomically.
        current = data.get(user_key, STARTING_BALANCE)
        data[user_key] = max(0, current + amount)
        promo["uses"] = uses + 1
        redeemed_by.append(user_key)
        promos[key] = promo
        data["_promo_codes"] = promos
        _save_data(data)
        return "success", amount


@bot.tree.command(name="addpromo", description="[Admin only] Create a promo code that gives gems.")
@app_commands.describe(
    code="The promo code players will redeem",
    amount="How many gems the code gives",
    max_uses="Maximum total redemptions (leave empty for unlimited)"
)
@admin_check()
async def addpromo(
    interaction: discord.Interaction,
    code: str,
    amount: str,
    max_uses: int | None = None
):
    code = code.strip().upper()
    gem_amount = parse_amount(amount)

    if not code:
        await interaction.response.send_message("Promo code cannot be empty.", ephemeral=True)
        return

    if len(code) > 50:
        await interaction.response.send_message("Promo code must be 50 characters or fewer.", ephemeral=True)
        return

    if gem_amount <= 0:
        await interaction.response.send_message("Gem amount must be positive.", ephemeral=True)
        return

    if max_uses is not None and max_uses <= 0:
        await interaction.response.send_message("Max uses must be positive.", ephemeral=True)
        return

    created = await add_promo_code(code, gem_amount, max_uses)
    if not created:
        await interaction.response.send_message(
            f"The promo code **{code}** already exists.", ephemeral=True
        )
        return

    uses_text = "unlimited" if max_uses is None else f"{max_uses:,}"
    embed = discord.Embed(
        title="🎟️ Promo Code Created",
        description=(
            f"**Code:** `{code}`\n"
            f"**Reward:** 💎 **{fmt(gem_amount)}**\n"
            f"**Max uses:** **{uses_text}**"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="redeem", description="Redeem a promo code for gems.")
@app_commands.describe(code="The promo code to redeem")
async def redeem(interaction: discord.Interaction, code: str):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return

    if VERIFIED_ROLE_ID == 0:
        await interaction.response.send_message(
            "The verified role is not configured. Ask the bot owner to set VERIFIED_ROLE_ID.",
            ephemeral=True
        )
        return

    if not any(role.id == VERIFIED_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message(
            "❌ You need the verified role to redeem promo codes.", ephemeral=True
        )
        return

    status, amount = await redeem_promo_code(interaction.user.id, code)

    messages = {
        "invalid": "❌ That promo code is invalid.",
        "already_redeemed": "❌ You have already redeemed this promo code.",
        "used_up": "❌ This promo code has reached its maximum number of uses.",
    }
    if status != "success":
        await interaction.response.send_message(messages.get(status, "❌ Unable to redeem that promo code."), ephemeral=True)
        return

    new_balance = await get_balance(interaction.user.id)
    embed = discord.Embed(
        title="🎉 Promo Code Redeemed!",
        description=(
            f"You received **{fmt(amount)}**.\n"
            f"💎 **New balance:** {fmt(new_balance)}"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


# ----------------------------------------------------------------------------
# ECONOMY COMMANDS
# ----------------------------------------------------------------------------


@bot.tree.command(name="balance", description="Check your (or someone else's) gem balance.")
@app_commands.describe(user="The user to check (defaults to yourself)")
@gambling_check()
async def balance(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user
    bal = await get_balance(target.id)

    embed = discord.Embed(
        title=f"{target.display_name}'s Balance",
        description=f"💎 **{fmt(bal)}**",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="tip", description="Send gems to another user.")
@app_commands.describe(user="The user to tip", amount="How many gems to send")
@gambling_check()
async def tip(interaction: discord.Interaction, user: discord.User, amount: str):
    amount = parse_amount(amount)
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("You can't tip yourself.", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("You can't tip a bot.", ephemeral=True)
        return

    sender_balance = await get_balance(interaction.user.id)
    if sender_balance < amount:
        await interaction.response.send_message(
            f"You don't have enough {CURRENCY_NAME}. Your balance: {fmt(sender_balance)}",
            ephemeral=True,
        )
        return

    await add_balance(interaction.user.id, -amount)
    await add_balance(user.id, amount)

    embed = discord.Embed(
        description=f"💸 {interaction.user.mention} tipped {user.mention} **{fmt(amount)}**",
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="addgems", description="[Admin only] Add gems to a user's balance.")
@app_commands.describe(user="The user to give gems to", amount="How many gems to add")
@admin_check()
async def addgems(interaction: discord.Interaction, user: discord.User, amount: str):
    amount = parse_amount(amount)
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return

    new_balance = await add_balance(user.id, amount)
    embed = discord.Embed(
        description=f"✅ Added **{fmt(amount)}** to {user.mention}. New balance: **{fmt(new_balance)}**",
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="removegems", description="[Admin only] Remove gems from a user's balance.")
@app_commands.describe(user="The user to remove gems from", amount="How many gems to remove")
@admin_check()
async def removegems(interaction: discord.Interaction, user: discord.User, amount: str):
    amount = parse_amount(amount)
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return

    new_balance = await add_balance(user.id, -amount)
    embed = discord.Embed(
        description=f"✅ Removed **{fmt(amount)}** from {user.mention}. New balance: **{fmt(new_balance)}**",
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed)


# ----------------------------------------------------------------------------
# COINFLIP
# ----------------------------------------------------------------------------


@bot.tree.command(name="coinflip", description="Bet gems on a coinflip.")
@app_commands.describe(amount="How many gems to bet", choice="Heads or tails")
@app_commands.choices(
    choice=[
        app_commands.Choice(name="Heads", value="heads"),
        app_commands.Choice(name="Tails", value="tails"),
    ]
)
@gambling_check()
async def coinflip(interaction: discord.Interaction, amount: str, choice: app_commands.Choice[str]):
    amount = parse_amount(amount)
    if amount <= 0:
        await interaction.response.send_message("Bet must be positive.", ephemeral=True)
        return

    bal = await get_balance(interaction.user.id)
    if bal < amount:
        await interaction.response.send_message(
            f"You don't have enough {CURRENCY_NAME}. Your balance: {fmt(bal)}", ephemeral=True
        )
        return

    result = random.choice(["heads", "tails"])
    won = result == choice.value

    if won:
        net_win, tax = apply_tax(amount)
        new_balance = await add_balance(interaction.user.id, net_win)
        embed = discord.Embed(
            title="🪙 Coinflip — You Won!",
            description=(
                f"The coin landed on **{result}**.\n"
                f"You won **{fmt(net_win)}** (after {int(TAX_RATE * 100)}% tax of {fmt(tax)}).\n"
                f"New balance: {fmt(new_balance)}"
            ),
            color=discord.Color.green(),
        )
    else:
        new_balance = await add_balance(interaction.user.id, -amount)
        embed = discord.Embed(
            title="🪙 Coinflip — You Lost",
            description=f"The coin landed on **{result}**.\nYou lost **{fmt(amount)}**.\nNew balance: {fmt(new_balance)}",
            color=discord.Color.red(),
        )

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


def card_value(card: str) -> int:
    rank = card[:-1]
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


def hand_value(hand: list[str]) -> int:
    total = sum(card_value(c) for c in hand)
    aces = sum(1 for c in hand if c.startswith("A"))
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def hand_str(hand: list[str]) -> str:
    return " ".join(hand)


class BlackjackView(discord.ui.View):
    def __init__(self, player_id: int, deck: list[str], player_hand: list[str], dealer_hand: list[str], bet: int):
        super().__init__(timeout=60)
        self.player_id = player_id
        self.deck = deck
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.bet = bet
        self.finished = False
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        # Auto-stand rather than leaving the hand hanging with no resolution.
        if self.finished:
            return
        while hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        player_total = hand_value(self.player_hand)
        dealer_total = hand_value(self.dealer_hand)

        if dealer_total > 21 or player_total > dealer_total:
            await self.end_game(None, "win", self.bet)
        elif dealer_total > player_total:
            await self.end_game(None, "lose", -self.bet)
        else:
            await self.end_game(None, "push", 0)

    def build_embed(self, reveal_dealer: bool = False) -> discord.Embed:
        embed = discord.Embed(title="🃏 Blackjack", color=discord.Color.blurple())
        embed.add_field(
            name=f"Your hand ({hand_value(self.player_hand)})",
            value=hand_str(self.player_hand),
            inline=False,
        )
        if reveal_dealer:
            embed.add_field(
                name=f"Dealer's hand ({hand_value(self.dealer_hand)})",
                value=hand_str(self.dealer_hand),
                inline=False,
            )
        else:
            embed.add_field(
                name="Dealer's hand",
                value=f"{self.dealer_hand[0]} 🂠",
                inline=False,
            )
        embed.set_footer(text=f"Bet: {fmt(self.bet)}")
        return embed

    async def end_game(self, interaction: discord.Interaction | None, result: str, payout: int):
        self.finished = True
        for child in self.children:
            child.disabled = True

        net_payout, tax = apply_tax(payout)
        new_balance = await add_balance(self.player_id, net_payout)
        embed = self.build_embed(reveal_dealer=True)
        color_map = {"win": discord.Color.green(), "lose": discord.Color.red(), "push": discord.Color.greyple()}
        embed.color = color_map.get(result, discord.Color.blurple())
        result_text = f"{result.upper()} — new balance: {fmt(new_balance)}"
        if tax:
            result_text += f" (won {fmt(net_payout)} after {int(TAX_RATE * 100)}% tax of {fmt(tax)})"
        embed.add_field(name="Result", value=result_text, inline=False)

        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message is not None:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return
        self.player_hand.append(self.deck.pop())
        if hand_value(self.player_hand) > 21:
            await self.end_game(interaction, "lose", -self.bet)
            return
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            return
        while hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        player_total = hand_value(self.player_hand)
        dealer_total = hand_value(self.dealer_hand)

        if dealer_total > 21 or player_total > dealer_total:
            await self.end_game(interaction, "win", self.bet)
        elif dealer_total > player_total:
            await self.end_game(interaction, "lose", -self.bet)
        else:
            await self.end_game(interaction, "push", 0)


@bot.tree.command(name="blackjack", description="Play a round of blackjack.")
@app_commands.describe(amount="How many gems to bet")
@gambling_check()
async def blackjack(interaction: discord.Interaction, amount: str):
    amount = parse_amount(amount)
    if amount <= 0:
        await interaction.response.send_message("Bet must be positive.", ephemeral=True)
        return

    bal = await get_balance(interaction.user.id)
    if bal < amount:
        await interaction.response.send_message(
            f"You don't have enough {CURRENCY_NAME}. Your balance: {fmt(bal)}", ephemeral=True
        )
        return

    deck = new_deck()
    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]

    view = BlackjackView(interaction.user.id, deck, player_hand, dealer_hand, amount)

    if hand_value(player_hand) == 21:
        gross_payout = int(amount * 1.5)
        net_payout, tax = apply_tax(gross_payout)
        new_balance = await add_balance(interaction.user.id, net_payout)
        embed = view.build_embed(reveal_dealer=True)
        embed.color = discord.Color.gold()
        embed.add_field(
            name="Result",
            value=(
                f"BLACKJACK! You won {fmt(net_payout)} (after {int(TAX_RATE * 100)}% tax of {fmt(tax)}) "
                f"— new balance: {fmt(new_balance)}"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)
        return

    await interaction.response.send_message(embed=view.build_embed(), view=view)
    view.message = await interaction.original_response()


# ----------------------------------------------------------------------------
# CRASH
# ----------------------------------------------------------------------------
#
# Tuning. Growth is multiplicative (a steady % per tick) instead of a flat
# random add-on, so the climb feels smooth at every level instead of jumping
# ~25% in one tick right out of the gate. CRASH_MAX_TICKS is a hard safety
# net: even in the unluckiest case the game is guaranteed to end on its own.

CRASH_TICK_SECONDS = 1.0
CRASH_GROWTH_MIN = 0.04       # +4% per tick, minimum
CRASH_GROWTH_MAX = 0.08       # +8% per tick, maximum
CRASH_MAX_MULTIPLIER = 20.0   # lowered from 50x so worst-case games stay short
CRASH_MAX_TICKS = 90          # hard cap (~90s) — the game always ends by here


class CrashView(discord.ui.View):
    def __init__(self, player_id: int, bet: int, crash_point: float):
        # No passive timeout: this game's lifecycle is fully owned by the
        # background loop in crash(), which always calls self.stop() when it
        # ends (crash, cash-out, or the hard tick cap). A passive timeout
        # here previously caused discord.py to stop listening for the Cash
        # Out click after 60s while the loop kept running underneath it —
        # the multiplier would keep climbing with no way to stop it.
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
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        if self.crashed:
            desc = f"💥 Crashed at **{self.crash_point:.2f}x**\nYou lost **{fmt(self.bet)}**"
            color = discord.Color.red()
        elif self.cashed_out:
            desc = (
                f"✅ Cashed out at **{self.multiplier:.2f}x**\n"
                f"You won **{fmt(self.net_profit)}** (after {int(TAX_RATE * 100)}% tax of {fmt(self.tax)})"
            )
            color = discord.Color.green()
        else:
            desc = f"📈 Current multiplier: **{self.multiplier:.2f}x**\nBet: {fmt(self.bet)}"
            color = discord.Color.blurple()

        embed = discord.Embed(title="🚀 Crash", description=desc, color=color)
        return embed

    @discord.ui.button(label="Cash Out", style=discord.ButtonStyle.success)
    async def cash_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.cashed_out or self.crashed:
            return
        self.cashed_out = True
        for child in self.children:
            child.disabled = True

        gross_profit = int(self.bet * self.multiplier) - self.bet
        self.net_profit, self.tax = apply_tax(gross_profit)
        await add_balance(self.player_id, self.net_profit)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        self.stop()


def _generate_crash_point() -> float:
    """House-edge-weighted crash point, floored and capped to sane bounds."""
    crash_point = (0.99 / (1 - random.random())) ** 0.5
    crash_point = max(1.01, min(crash_point, CRASH_MAX_MULTIPLIER))
    return round(crash_point, 2)


@bot.tree.command(name="crash", description="Watch the multiplier rise and cash out before it crashes.")
@app_commands.describe(amount="How many gems to bet")
@gambling_check()
async def crash(interaction: discord.Interaction, amount: str):
    amount = parse_amount(amount)
    if amount <= 0:
        await interaction.response.send_message("Bet must be positive.", ephemeral=True)
        return

    bal = await get_balance(interaction.user.id)
    if bal < amount:
        await interaction.response.send_message(
            f"You don't have enough {CURRENCY_NAME}. Your balance: {fmt(bal)}", ephemeral=True
        )
        return

    # Deduct the bet up front; cash_out adds back bet*multiplier (net payout handled there
    # as profit only, so nothing further is deducted here on a loss).
    await add_balance(interaction.user.id, -amount)

    crash_point = _generate_crash_point()

    view = CrashView(interaction.user.id, amount, crash_point)
    await interaction.response.send_message(embed=view.build_embed(), view=view)
    message = await interaction.original_response()

    for _ in range(CRASH_MAX_TICKS):
        if view.cashed_out:
            return

        await asyncio.sleep(CRASH_TICK_SECONDS)
        if view.cashed_out:
            return

        growth_rate = random.uniform(CRASH_GROWTH_MIN, CRASH_GROWTH_MAX)
        view.multiplier = round(view.multiplier * (1 + growth_rate), 2)

        if view.multiplier >= view.crash_point:
            view.multiplier = view.crash_point
            view.crashed = True
            for child in view.children:
                child.disabled = True
            try:
                await message.edit(embed=view.build_embed(), view=view)
            except discord.HTTPException:
                pass
            view.stop()
            return

        try:
            await message.edit(embed=view.build_embed(), view=view)
        except discord.HTTPException:
            view.stop()
            return

    # Hard safety net: should be mathematically unreachable given
    # CRASH_GROWTH_MIN and CRASH_MAX_MULTIPLIER, but guarantees the game
    # can never run forever if the tuning above ever changes.
    if not view.cashed_out and not view.crashed:
        view.crashed = True
        for child in view.children:
            child.disabled = True
        try:
            await message.edit(embed=view.build_embed(), view=view)
        except discord.HTTPException:
            pass
        view.stop()


# ----------------------------------------------------------------------------
# MINES
# ----------------------------------------------------------------------------
#
# Multiplier model (merged in from the "buffed first tile" reference):
#   - The board here has 24 playable tiles (5x5 grid minus the Cash Out
#     button slot, to stay within Discord's 25-component limit).
#   - Each mine count gets its own boosted "first tile" multiplier
#     (higher mine counts get a bigger first-click bump), then the
#     multiplier grows along a softened fair-odds curve for each
#     additional safe tile revealed.
#   - The result is capped so late-game payouts don't get out of hand.

MINES_TOTAL_TILES = MINES_GRID_SIZE * MINES_GRID_SIZE - 1  # 24 playable tiles
MINES_MAX_MULTIPLIER = 8.00  # lowered from 12.00 — hard cap on late-game payouts

# House edge baked directly into the curve (on top of the normal withdrawal
# tax), so the "buffed first tile" boost from the old version is gone.
# Multiplier is now pure hypergeometric fair-odds * (1 - edge), capped.
MINES_HOUSE_EDGE = 0.04  # 4%


def _mines_fair_multiplier(mine_count: int, revealed_count: int) -> float:
    """Pure hypergeometric fair-odds multiplier with no house edge or buff."""
    safe_tiles = MINES_TOTAL_TILES - mine_count
    revealed_count = min(revealed_count, safe_tiles)

    mult = 1.0
    for i in range(revealed_count):
        mult *= (MINES_TOTAL_TILES - i) / (safe_tiles - i)
    return mult


def mines_multiplier(picks: int, mine_count: int) -> float:
    """Fair-odds multiplier minus a flat house edge, capped."""
    if picks <= 0:
        return 1.0

    safe_tiles = MINES_TOTAL_TILES - mine_count
    picks = min(picks, safe_tiles)

    fair = _mines_fair_multiplier(mine_count, picks)
    multiplier = fair * (1 - MINES_HOUSE_EDGE)
    return min(round(multiplier, 2), MINES_MAX_MULTIPLIER)


class MinesButton(discord.ui.Button):
    def __init__(self, index: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="?", row=index // MINES_GRID_SIZE)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        view: "MinesView" = self.view
        if interaction.user.id != view.player_id:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return
        await view.reveal_tile(interaction, self)


class MinesView(discord.ui.View):
    def __init__(self, player_id: int, bet: int, mine_count: int):
        super().__init__(timeout=120)
        self.player_id = player_id
        self.bet = bet
        self.mine_count = mine_count
        self.total_tiles = MINES_TOTAL_TILES
        self.mine_positions = set(random.sample(range(self.total_tiles), mine_count))
        self.picks = 0
        self.finished = False
        self.message: discord.Message | None = None

        for i in range(self.total_tiles):
            self.add_item(MinesButton(i))

        cash_out_btn = discord.ui.Button(label="Cash Out", style=discord.ButtonStyle.success, row=MINES_GRID_SIZE - 1)
        cash_out_btn.callback = self.cash_out_callback
        self.add_item(cash_out_btn)

    def current_multiplier(self) -> float:
        return mines_multiplier(self.picks, self.mine_count)

    def next_multiplier(self) -> float:
        return mines_multiplier(self.picks + 1, self.mine_count)

    def build_embed(self, revealed_all: bool = False, result: str | None = None) -> discord.Embed:
        embed = discord.Embed(title="💣 Mines", color=discord.Color.blurple())
        embed.add_field(name="Bet", value=fmt(self.bet), inline=True)
        embed.add_field(name="Mines", value=str(self.mine_count), inline=True)
        embed.add_field(name="Multiplier", value=f"{self.current_multiplier():.2f}x", inline=True)
        if not self.finished:
            next_payout = int(self.bet * self.next_multiplier())
            embed.add_field(name="Next tile pays", value=fmt(next_payout), inline=True)
        if result:
            embed.description = result
            embed.color = discord.Color.green() if "won" in result.lower() else discord.Color.red()
        return embed

    async def end_game(self, interaction: discord.Interaction | None, won: bool, timed_out: bool = False):
        self.finished = True
        for child in self.children:
            if isinstance(child, MinesButton):
                child.disabled = True
                if child.index in self.mine_positions:
                    child.label = "💣"
                    child.style = discord.ButtonStyle.danger
                elif child.style == discord.ButtonStyle.success:
                    pass  # already revealed safe tile
                else:
                    child.style = discord.ButtonStyle.secondary
            else:
                child.disabled = True

        if won:
            gross_profit = int(self.bet * self.current_multiplier()) - self.bet
            net_profit, tax = apply_tax(gross_profit)
            # The original bet was deducted when the game started.
            # Return the stake plus the net winnings on a successful cash-out.
            payout = self.bet + net_profit
            new_balance = await add_balance(self.player_id, payout)
            prefix = "⏱️ Timed out — auto cashed out" if timed_out else "✅ Cashed out"
            result = (
                f"{prefix} at **{self.current_multiplier():.2f}x**! "
                f"Won **{fmt(net_profit)}** (after {int(TAX_RATE * 100)}% tax of {fmt(tax)}). "
                f"New balance: {fmt(new_balance)}"
            )
        elif timed_out:
            # No tiles revealed before timing out — nothing was risked yet,
            # so give the bet back instead of silently keeping it.
            new_balance = await add_balance(self.player_id, self.bet)
            result = f"⏱️ Game timed out with no picks made. Bet refunded. New balance: {fmt(new_balance)}"
        else:
            new_balance = await get_balance(self.player_id)
            result = f"💥 You hit a mine! Lost **{fmt(self.bet)}**. New balance: {fmt(new_balance)}"

        embed = self.build_embed(result=result)
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message is not None:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass
        self.stop()

    async def on_timeout(self):
        if self.finished:
            return
        # picks > 0 means they'd already earned a multiplier — auto cash out
        # at that multiplier rather than letting the money vanish. picks == 0
        # means nothing was risked, so it's a straight refund.
        await self.end_game(None, won=self.picks > 0, timed_out=True)

    async def reveal_tile(self, interaction: discord.Interaction, button: MinesButton):
        if self.finished:
            return

        if button.index in self.mine_positions:
            await self.end_game(interaction, won=False)
            return

        button.style = discord.ButtonStyle.success
        button.label = "💎"
        button.disabled = True
        self.picks += 1

        if self.picks == self.total_tiles - self.mine_count:
            # All safe tiles revealed — auto cash out.
            await self.end_game(interaction, won=True)
            return

        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def cash_out_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return
        if self.finished:
            return
        if self.picks == 0:
            await interaction.response.send_message("Reveal at least one tile before cashing out.", ephemeral=True)
            return
        await self.end_game(interaction, won=True)


@bot.tree.command(name="mines", description="Play mines — reveal tiles and cash out before you hit a mine.")
@app_commands.describe(amount="How many gems to bet", mines="Number of mines on the board (1-23, default 5)")
@gambling_check()
async def mines(interaction: discord.Interaction, amount: str, mines: int = 5):
    amount = parse_amount(amount)
    if amount <= 0:
        await interaction.response.send_message("Bet must be positive.", ephemeral=True)
        return

    if not (1 <= mines <= MINES_TOTAL_TILES - 1):
        await interaction.response.send_message(
            f"Mines must be between 1 and {MINES_TOTAL_TILES - 1}.", ephemeral=True
        )
        return

    bal = await get_balance(interaction.user.id)
    if bal < amount:
        await interaction.response.send_message(
            f"You don't have enough {CURRENCY_NAME}. Your balance: {fmt(bal)}", ephemeral=True
        )
        return

    await add_balance(interaction.user.id, -amount)

    view = MinesView(interaction.user.id, amount, mines)
    await interaction.response.send_message(embed=view.build_embed(), view=view)
    view.message = await interaction.original_response()


# ----------------------------------------------------------------------------
# ENTRYPOINT
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Create a .env file with DISCORD_TOKEN=your_token_here"
        )
    bot.run(DISCORD_TOKEN)
