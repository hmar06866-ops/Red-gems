"""
Discord Gambling/Economy Bot
=============================
Games: Blackjack, Coinflip, Crash, Mines
Economy: /balance, /tip, /addgems, /removegems

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

DATA_FILE = Path(__file__).parent / "economy.json"
CURRENCY_NAME = "gems"
STARTING_BALANCE = 100

# Every user's balance is stored separately in economy.json using their
# Discord user ID as the key.
if not DATA_FILE.exists():
    DATA_FILE.write_text("{}", encoding="utf-8")

MINES_GRID_SIZE = 5  # 5 columns; 24 playable tiles + 1 Cash Out button

# ----------------------------------------------------------------------------
# ECONOMY STORAGE (simple JSON-backed, async-safe with a lock)
# ----------------------------------------------------------------------------

_data_lock = asyncio.Lock()


def _load_data() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def _save_data(data: dict) -> None:
    """Save each user's data safely, keyed by their Discord user ID."""
    temp_file = DATA_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, DATA_FILE)


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
        new_balance = await add_balance(interaction.user.id, amount)
        embed = discord.Embed(
            title="🪙 Coinflip — You Won!",
            description=f"The coin landed on **{result}**.\nYou won **{fmt(amount)}**!\nNew balance: {fmt(new_balance)}",
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

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return False
        return True

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

    async def end_game(self, interaction: discord.Interaction, result: str, payout: int):
        self.finished = True
        for child in self.children:
            child.disabled = True

        new_balance = await add_balance(self.player_id, payout)
        embed = self.build_embed(reveal_dealer=True)
        color_map = {"win": discord.Color.green(), "lose": discord.Color.red(), "push": discord.Color.greyple()}
        embed.color = color_map.get(result, discord.Color.blurple())
        embed.add_field(name="Result", value=f"{result.upper()} — new balance: {fmt(new_balance)}", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

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
        payout = int(amount * 1.5)
        new_balance = await add_balance(interaction.user.id, payout)
        embed = view.build_embed(reveal_dealer=True)
        embed.color = discord.Color.gold()
        embed.add_field(name="Result", value=f"BLACKJACK! You won {fmt(payout)} — new balance: {fmt(new_balance)}", inline=False)
        await interaction.response.send_message(embed=embed)
        return

    await interaction.response.send_message(embed=view.build_embed(), view=view)


# ----------------------------------------------------------------------------
# CRASH
# ----------------------------------------------------------------------------


class CrashView(discord.ui.View):
    def __init__(self, player_id: int, bet: int, crash_point: float):
        super().__init__(timeout=60)
        self.player_id = player_id
        self.bet = bet
        self.crash_point = crash_point
        self.cashed_out = False
        self.crashed = False
        self.multiplier = 1.0

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
            payout = int(self.bet * self.multiplier) - self.bet
            desc = f"✅ Cashed out at **{self.multiplier:.2f}x**\nYou won **{fmt(payout)}**"
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

        payout = int(self.bet * self.multiplier) - self.bet
        await add_balance(self.player_id, payout)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        self.stop()


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

    # Generate a crash point with a house edge, weighted toward lower multipliers.
    crash_point = round(max(1.0, (0.99 / (1 - random.random())) ** 0.5), 2)
    crash_point = min(crash_point, 50.0)  # cap for sanity

    view = CrashView(interaction.user.id, amount, crash_point)
    await interaction.response.send_message(embed=view.build_embed(), view=view)
    message = await interaction.original_response()

    while not view.cashed_out and view.multiplier < view.crash_point:
        await asyncio.sleep(1.5)
        if view.cashed_out:
            break
        view.multiplier = round(view.multiplier + random.uniform(0.08, 0.25), 2)
        if view.multiplier >= view.crash_point:
            view.multiplier = view.crash_point
            view.crashed = True
            for child in view.children:
                child.disabled = True
            await message.edit(embed=view.build_embed(), view=view)
            view.stop()
            break
        try:
            await message.edit(embed=view.build_embed(), view=view)
        except discord.HTTPException:
            break


# ----------------------------------------------------------------------------
# MINES
# ----------------------------------------------------------------------------


def mines_multiplier(picks: int, mines: int, total: int = MINES_GRID_SIZE * MINES_GRID_SIZE - 1) -> float:
    if picks == 0:
        return 1.0
    multiplier = 0.6426  # payouts buffed by 19% from 0.54
    for i in range(picks):
        multiplier *= (total - i) / (total - mines - i)
    return round(multiplier, 2)


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
        self.total_tiles = MINES_GRID_SIZE * MINES_GRID_SIZE - 1  # 24 tiles so Cash Out fits in Discord's 25-component limit
        self.mine_positions = set(random.sample(range(self.total_tiles), mine_count))
        self.picks = 0
        self.finished = False

        for i in range(self.total_tiles):
            self.add_item(MinesButton(i))

        cash_out_btn = discord.ui.Button(label="Cash Out", style=discord.ButtonStyle.success, row=MINES_GRID_SIZE - 1)
        cash_out_btn.callback = self.cash_out_callback
        self.add_item(cash_out_btn)

    def current_multiplier(self) -> float:
        return mines_multiplier(self.picks, self.mine_count)

    def build_embed(self, revealed_all: bool = False, result: str | None = None) -> discord.Embed:
        embed = discord.Embed(title="💣 Mines", color=discord.Color.blurple())
        embed.add_field(name="Bet", value=fmt(self.bet), inline=True)
        embed.add_field(name="Mines", value=str(self.mine_count), inline=True)
        embed.add_field(name="Multiplier", value=f"{self.current_multiplier():.2f}x", inline=True)
        if result:
            embed.description = result
            embed.color = discord.Color.green() if "won" in result.lower() else discord.Color.red()
        return embed

    async def end_game(self, interaction: discord.Interaction, won: bool):
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
            payout = int(self.bet * self.current_multiplier()) - self.bet
            new_balance = await add_balance(self.player_id, payout)
            result = f"✅ Cashed out at **{self.current_multiplier():.2f}x**! Won **{fmt(payout)}**. New balance: {fmt(new_balance)}"
        else:
            new_balance = await get_balance(self.player_id)
            result = f"💥 You hit a mine! Lost **{fmt(self.bet)}**. New balance: {fmt(new_balance)}"

        await interaction.response.edit_message(embed=self.build_embed(result=result), view=self)
        self.stop()

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
@app_commands.describe(amount="How many gems to bet", mines="Number of mines on the board (1-24, default 5)")
@gambling_check()
async def mines(interaction: discord.Interaction, amount: str, mines: int = 5):
    amount = parse_amount(amount)
    if amount <= 0:
        await interaction.response.send_message("Bet must be positive.", ephemeral=True)
        return

    total_tiles = MINES_GRID_SIZE * MINES_GRID_SIZE - 1
    if not (1 <= mines <= total_tiles - 1):
        await interaction.response.send_message(
            f"Mines must be between 1 and {total_tiles - 1}.", ephemeral=True
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


# ----------------------------------------------------------------------------
# ENTRYPOINT
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Create a .env file with DISCORD_TOKEN=your_token_here"
        )
    bot.run(DISCORD_TOKEN)
