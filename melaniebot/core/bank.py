from __future__ import annotations

import asyncio
from datetime import datetime
from functools import wraps
from typing import TYPE_CHECKING, Literal, Optional, Union

import discord
from loguru import logger as log

from melaniebot.core.utils import AsyncIter
from melaniebot.core.utils.chat_formatting import humanize_number

from . import Config, commands, errors
from .errors import BankPruneError

if TYPE_CHECKING:
    from .bot import Melanie


def _(x):
    return x


__all__ = [
    "Account",
    "get_balance",
    "set_balance",
    "withdraw_credits",
    "deposit_credits",
    "can_spend",
    "transfer_credits",
    "wipe_bank",
    "get_account",
    "is_global",
    "set_global",
    "get_bank_name",
    "set_bank_name",
    "get_currency_name",
    "set_currency_name",
    "get_default_balance",
    "set_default_balance",
    "get_max_balance",
    "set_max_balance",
    "cost",
    "AbortPurchase",
    "bank_prune",
]

_MAX_BALANCE = 2**63 - 1

_SCHEMA_VERSION = 1

_DEFAULT_GLOBAL = {
    "schema_version": 0,
    "is_global": False,
    "bank_name": "Twentysix bank",
    "currency": "credits",
    "default_balance": 100,
    "max_balance": _MAX_BALANCE,
}

_DEFAULT_GUILD = {"bank_name": "Twentysix bank", "currency": "credits", "default_balance": 100, "max_balance": _MAX_BALANCE}

_DEFAULT_MEMBER = {"name": "", "balance": 0, "created_at": 0}

_DEFAULT_USER = _DEFAULT_MEMBER

_config: Config = None


_data_deletion_lock = asyncio.Lock()

_cache_is_global = None
_cache = {"bank_name": None, "currency": None, "default_balance": None, "max_balance": None}


async def _init():
    global _config
    _config = Config.get_conf(None, 384734293238749, cog_name="Bank", force_registration=True)
    _config.register_global(**_DEFAULT_GLOBAL)
    _config.register_guild(**_DEFAULT_GUILD)
    _config.register_member(**_DEFAULT_MEMBER)
    _config.register_user(**_DEFAULT_USER)
    await _migrate_config()


async def _migrate_config():
    schema_version = await _config.schema_version()

    if schema_version == _SCHEMA_VERSION:
        return

    if schema_version == 0:
        await _schema_0_to_1()
        schema_version += 1
        await _config.schema_version.set(schema_version)


async def _schema_0_to_1():
    # convert floats in bank balances to ints

    # don't use anything seen below in extensions, it's optimized and controlled for here,
    # but can't be safe in 3rd party use

    # this CANNOT use ctx manager, because ctx managers compare before and after,
    # and floats can be equal to ints: (1.0 == 1) is True
    group = _config._get_base_group(_config.USER)
    bank_user_data = await group.all()
    for user_config in bank_user_data.values():
        if "balance" in user_config:
            user_config["balance"] = int(user_config["balance"])
    await group.set(bank_user_data)

    group = _config._get_base_group(_config.MEMBER)
    bank_member_data = await group.all()
    for guild_data in bank_member_data.values():
        for member_config in guild_data.values():
            if "balance" in member_config:
                member_config["balance"] = int(member_config["balance"])
    await group.set(bank_member_data)


async def _process_data_deletion(*, requester: Literal["discord_deleted_user", "owner", "user", "user_strict"], user_id: int):
    """Bank has no reason to keep any of this data if the user doesn't want it
    kept, we won't special case any request type.
    """
    if requester not in ("discord_deleted_user", "owner", "user", "user_strict"):
        log.warning("Got unknown data request type `{req_type}` for user, deleting anyway", req_type=requester)

    async with _data_deletion_lock:
        await _config.user_from_id(user_id).clear()
        all_members = await _config.all_members()
        async for guild_id, member_dict in AsyncIter(all_members.items(), steps=20):
            if user_id in member_dict:
                await _config.member_from_ids(guild_id, user_id).clear()


class Account:
    """A single account.

    This class should ONLY be instantiated by the bank itself.

    """

    def __init__(self, name: str, balance: int, created_at: datetime) -> None:
        self.name = name
        self.balance = balance
        self.created_at = created_at


def _encoded_current_time() -> int:
    """Get the current UTC time as a timestamp.

    Returns
    -------
    int
        The current UTC timestamp.

    """
    now = datetime.utcnow()
    return _encode_time(now)


def _encode_time(time: datetime) -> int:
    """Convert a datetime object to a serializable int.

    Parameters
    ----------
    time : datetime.datetime
        The datetime to convert.

    Returns
    -------
    int
        The timestamp of the datetime object.

    """
    return int(time.timestamp())


def _decode_time(time: int) -> datetime:
    """Convert a timestamp to a datetime object.

    Parameters
    ----------
    time : int
        The timestamp to decode.

    Returns
    -------
    datetime.datetime
        The datetime object from the timestamp.

    """
    return datetime.utcfromtimestamp(time)


async def get_balance(member: discord.Member) -> int:
    """Get the current balance of a member.

    Parameters
    ----------
    member : discord.Member
        The member whose balance to check.

    Returns
    -------
    int
        The member's balance

    """
    acc = await get_account(member)
    return acc.balance


async def can_spend(member: discord.Member, amount: int) -> bool:
    """Determine if a member can spend the given amount.

    Parameters
    ----------
    member : discord.Member
        The member wanting to spend.
    amount : int
        The amount the member wants to spend.

    Raises
    ------
    TypeError
        If the amount is not an `int`.

    Returns
    -------
    bool
        :code:`True` if the member has a sufficient balance to spend the
        amount, else :code:`False`.

    """
    if not isinstance(amount, int):
        msg = f"Amount must be of type int, not {type(amount)}."
        raise TypeError(msg)
    if _invalid_amount(amount):
        return False
    return await get_balance(member) >= amount


async def set_balance(member: Union[discord.Member, discord.User], amount: int) -> int:
    """Set an account balance.

    Parameters
    ----------
    member : Union[discord.Member, discord.User]
        The member whose balance to set.
    amount : int
        The amount to set the balance to.

    Returns
    -------
    int
        New account balance.

    Raises
    ------
    ValueError
        If attempting to set the balance to a negative number.
    RuntimeError
        If the bank is guild-specific and a discord.User object is provided.
    BalanceTooHigh
        If attempting to set the balance to a value greater than
        ``bank._MAX_BALANCE``.
    TypeError
        If the amount is not an `int`.

    """
    if not isinstance(amount, int):
        msg = f"Amount must be of type int, not {type(amount)}."
        raise TypeError(msg)
    if amount < 0:
        msg = "Not allowed to have negative balance."
        raise ValueError(msg)
    guild = getattr(member, "guild", None)
    max_bal = await get_max_balance(guild)
    if amount > max_bal:
        currency = await get_currency_name(guild)
        raise errors.BalanceTooHigh(user=member.display_name, max_balance=max_bal, currency_name=currency)
    group = _config.user(member) if await is_global() else _config.member(member)
    await group.balance.set(amount)

    if await group.created_at() == 0:
        time = _encoded_current_time()
        await group.created_at.set(time)

    if await group.name() == "":
        await group.name.set(member.display_name)

    return amount


def _invalid_amount(amount: int) -> bool:
    return amount < 0


async def withdraw_credits(member: discord.Member, amount: int) -> int:
    """Remove a certain amount of credits from an account.

    Parameters
    ----------
    member : discord.Member
        The member to withdraw credits from.
    amount : int
        The amount to withdraw.

    Returns
    -------
    int
        New account balance.

    Raises
    ------
    ValueError
        If the withdrawal amount is invalid or if the account has insufficient
        funds.
    TypeError
        If the withdrawal amount is not an `int`.

    """
    if not isinstance(amount, int):
        msg = f"Withdrawal amount must be of type int, not {type(amount)}."
        raise TypeError(msg)
    if _invalid_amount(amount):
        msg = f"Invalid withdrawal amount {humanize_number(amount, override_locale='en_US')} < 0"
        raise ValueError(msg)

    bal = await get_balance(member)
    if amount > bal:
        msg = f"Insufficient funds {humanize_number(amount, override_locale='en_US')} > {humanize_number(bal, override_locale='en_US')}"
        raise ValueError(msg)

    return await set_balance(member, bal - amount)


async def deposit_credits(member: discord.Member, amount: int) -> int:
    """Add a given amount of credits to an account.

    Parameters
    ----------
    member : discord.Member
        The member to deposit credits to.
    amount : int
        The amount to deposit.

    Returns
    -------
    int
        The new balance.

    Raises
    ------
    ValueError
        If the deposit amount is invalid.
    TypeError
        If the deposit amount is not an `int`.

    """
    if not isinstance(amount, int):
        msg = f"Deposit amount must be of type int, not {type(amount)}."
        raise TypeError(msg)
    if _invalid_amount(amount):
        msg = f"Invalid deposit amount {humanize_number(amount, override_locale='en_US')} <= 0"
        raise ValueError(msg)

    bal = await get_balance(member)
    return await set_balance(member, amount + bal)


async def transfer_credits(from_: Union[discord.Member, discord.User], to: Union[discord.Member, discord.User], amount: int):
    """Transfer a given amount of credits from one account to another.

    Parameters
    ----------
    from_: Union[discord.Member, discord.User]
        The member to transfer from.
    to : Union[discord.Member, discord.User]
        The member to transfer to.
    amount : int
        The amount to transfer.

    Returns
    -------
    int
        The new balance of the member gaining credits.

    Raises
    ------
    ValueError
        If the amount is invalid or if ``from_`` has insufficient funds.
    TypeError
        If the amount is not an `int`.
    RuntimeError
        If the bank is guild-specific and a discord.User object is provided.
    BalanceTooHigh
        If the balance after the transfer would be greater than
        ``bank._MAX_BALANCE``.

    """
    if not isinstance(amount, int):
        msg = f"Transfer amount must be of type int, not {type(amount)}."
        raise TypeError(msg)
    if _invalid_amount(amount):
        msg = f"Invalid transfer amount {humanize_number(amount, override_locale='en_US')} <= 0"
        raise ValueError(msg)

    guild = getattr(to, "guild", None)
    max_bal = await get_max_balance(guild)

    if await get_balance(to) + amount > max_bal:
        currency = await get_currency_name(guild)
        raise errors.BalanceTooHigh(user=to.display_name, max_balance=max_bal, currency_name=currency)

    await withdraw_credits(from_, amount)
    return await deposit_credits(to, amount)


async def wipe_bank(guild: Optional[discord.Guild] = None) -> None:
    """Delete all accounts from the bank.

    Parameters
    ----------
    guild : discord.Guild
        The guild to clear accounts for. If unsupplied and the bank is
        per-server, all accounts in every guild will be wiped.

    """
    if await is_global():
        await _config.clear_all_users()
    else:
        await _config.clear_all_members(guild)


async def bank_prune(bot: Melanie, guild: discord.Guild = None, user_id: int = None) -> None:
    """Prune bank accounts from the bank.

    Parameters
    ----------
    bot : Melanie
        The bot.
    guild : discord.Guild
        The guild to prune. This is required if the bank is set to local.
    user_id : int
        The id of the user whose account will be pruned.
        If supplied this will prune only this user's bank account
        otherwise it will prune all invalid users from the bank.

    Raises
    ------
    BankPruneError
        If guild is :code:`None` and the bank is Local.

    """
    global_bank = await is_global()

    if global_bank:
        _guilds = set()
        _uguilds = set()
        if user_id is None:
            async for g in AsyncIter(bot.guilds, steps=20):
                if not g.unavailable and g.large and not g.chunked:
                    _guilds.add(g)
                elif g.unavailable:
                    _uguilds.add(g)
        group = _config._get_base_group(_config.USER)

    else:
        if guild is None:
            msg = "'guild' can't be None when pruning a local bank"
            raise BankPruneError(msg)
        if user_id is None:
            _guilds = {guild} if not guild.unavailable and guild.large else set()
            _uguilds = {guild} if guild.unavailable else set()
        group = _config._get_base_group(_config.MEMBER, str(guild.id))

    if user_id is None:
        for _guild in _guilds:
            await _guild.chunk()
        accounts = await group.all()
        tmp = accounts.copy()
        members = bot.get_all_members() if global_bank else guild.members
        user_list = {str(m.id) for m in members if m.guild not in _uguilds}

    async with group.all() as bank_data:  # FIXME: use-config-bulk-update
        if user_id is None:
            for acc in tmp:
                if acc not in user_list:
                    del bank_data[acc]
        else:
            user_id = str(user_id)
            if user_id in bank_data:
                del bank_data[user_id]


async def get_leaderboard(positions: int = None, guild: discord.Guild = None) -> list[tuple]:
    """Gets the bank's leaderboard.

    Parameters
    ----------
    positions : `int`
        The number of positions to get
    guild : discord.Guild
        The guild to get the leaderboard of. If the bank is global and this
        is provided, get only guild members on the leaderboard

    Returns
    -------
    `list` of `tuple`
        The sorted leaderboard in the form of :code:`(user_id, raw_account)`

    Raises
    ------
    TypeError
        If the bank is guild-specific and no guild was specified

    """
    if await is_global():
        raw_accounts = await _config.all_users()
        if guild is not None:
            tmp = raw_accounts.copy()
            for acc in tmp:
                if not guild.get_member(acc):
                    del raw_accounts[acc]
    elif guild is None:
        msg = "Expected a guild, got NoneType object instead!"
        raise TypeError(msg)
    else:
        raw_accounts = await _config.all_members(guild)
    sorted_acc = sorted(raw_accounts.items(), key=lambda x: x[1]["balance"], reverse=True)
    return sorted_acc if positions is None else sorted_acc[:positions]


async def get_leaderboard_position(member: Union[discord.User, discord.Member]) -> Union[int, None]:
    """Get the leaderboard position for the specified user.

    Parameters
    ----------
    member : `discord.User` or `discord.Member`
        The user to get the leaderboard position of

    Returns
    -------
    `int`
        The position of the user on the leaderboard

    Raises
    ------
    TypeError
        If the bank is currently guild-specific and a `discord.User` object was passed in

    """
    if await is_global():
        guild = None
    else:
        guild = member.guild if hasattr(member, "guild") else None
    try:
        leaderboard = await get_leaderboard(None, guild)
    except TypeError:
        raise
    else:
        pos = discord.utils.find(lambda x: x[1][0] == member.id, enumerate(leaderboard, 1))
        return None if pos is None else pos[0]


async def get_account(member: Union[discord.Member, discord.User]) -> Account:
    """Get the appropriate account for the given user or member.

    A member is required if the bank is currently guild specific.

    Parameters
    ----------
    member : `discord.User` or `discord.Member`
        The user whose account to get.

    Returns
    -------
    Account
        The user's account.

    """
    if await is_global():
        all_accounts = await _config.all_users()
    else:
        all_accounts = await _config.all_members(member.guild)

    if member.id not in all_accounts:
        acc_data = {"name": member.display_name, "created_at": _DEFAULT_MEMBER["created_at"]}
        try:
            acc_data["balance"] = await get_default_balance(member.guild)
        except AttributeError:
            acc_data["balance"] = await get_default_balance()
    else:
        acc_data = all_accounts[member.id]

    acc_data["created_at"] = _decode_time(acc_data["created_at"])
    return Account(**acc_data)


async def is_global() -> bool:
    """Determine if the bank is currently global.

    Returns
    -------
    bool
        :code:`True` if the bank is global, otherwise :code:`False`.

    """
    global _cache_is_global

    if _cache_is_global is None:
        _cache_is_global = await _config.is_global()

    return _cache_is_global


async def set_global(global_: bool) -> bool:
    """Set global status of the bank.

    .. important::

        All accounts are reset when you switch!

    Parameters
    ----------
    global_ : bool
        :code:`True` will set bank to global mode.

    Returns
    -------
    bool
        New bank mode, :code:`True` is global.

    Raises
    ------
    RuntimeError
        If bank is becoming global and a `discord.Member` was not provided.

    """
    if (await is_global()) is global_:
        return global_

    global _cache_is_global

    if await is_global():
        await _config.clear_all_users()
    else:
        await _config.clear_all_members()

    await _config.is_global.set(global_)
    _cache_is_global = global_
    return global_


async def get_bank_name(guild: discord.Guild = None) -> str:
    """Get the current bank name.

    Parameters
    ----------
    guild : `discord.Guild`, optional
        The guild to get the bank name for (required if bank is
        guild-specific).

    Returns
    -------
    str
        The bank's name.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        global _cache
        if _cache["bank_name"] is None:
            _cache["bank_name"] = await _config.bank_name()
        return _cache["bank_name"]
    elif guild is not None:
        return await _config.guild(guild).bank_name()
    else:
        msg = "Guild parameter is required and missing."
        raise RuntimeError(msg)


async def set_bank_name(name: str, guild: discord.Guild = None) -> str:
    """Set the bank name.

    Parameters
    ----------
    name : str
        The new name for the bank.
    guild : `discord.Guild`, optional
        The guild to set the bank name for (required if bank is
        guild-specific).

    Returns
    -------
    str
        The new name for the bank.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        await _config.bank_name.set(name)
        global _cache
        _cache["bank_name"] = name
    elif guild is not None:
        await _config.guild(guild).bank_name.set(name)
    else:
        msg = "Guild must be provided if setting the name of a guild-specific bank."
        raise RuntimeError(msg)
    return name


async def get_currency_name(guild: discord.Guild = None) -> str:
    """Get the currency name of the bank.

    Parameters
    ----------
    guild : `discord.Guild`, optional
        The guild to get the currency name for (required if bank is
        guild-specific).

    Returns
    -------
    str
        The currency name.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        global _cache
        if _cache["currency"] is None:
            _cache["currency"] = await _config.currency()
        return _cache["currency"]
    elif guild is not None:
        return await _config.guild(guild).currency()
    else:
        msg = "Guild must be provided."
        raise RuntimeError(msg)


async def set_currency_name(name: str, guild: discord.Guild = None) -> str:
    """Set the currency name for the bank.

    Parameters
    ----------
    name : str
        The new name for the currency.
    guild : `discord.Guild`, optional
        The guild to set the currency name for (required if bank is
        guild-specific).

    Returns
    -------
    str
        The new name for the currency.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        await _config.currency.set(name)
        global _cache
        _cache["currency"] = name
    elif guild is not None:
        await _config.guild(guild).currency.set(name)
    else:
        msg = "Guild must be provided if setting the currency name of a guild-specific bank."
        raise RuntimeError(msg)
    return name


async def get_max_balance(guild: discord.Guild = None) -> int:
    """Get the max balance for the bank.

    Parameters
    ----------
    guild : `discord.Guild`, optional
        The guild to get the max balance for (required if bank is
        guild-specific).

    Returns
    -------
    int
        The maximum allowed balance.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        if _cache["max_balance"] is None:
            _cache["max_balance"] = await _config.max_balance()
        return _cache["max_balance"]
    elif guild is not None:
        return await _config.guild(guild).max_balance()
    else:
        msg = "Guild must be provided."
        raise RuntimeError(msg)


async def set_max_balance(amount: int, guild: discord.Guild = None) -> int:
    """Set the maximum balance for the bank.

    Parameters
    ----------
    amount : int
        The new maximum balance.
    guild : `discord.Guild`, optional
        The guild to set the max balance for (required if bank is
        guild-specific).

    Returns
    -------
    int
        The new maximum balance.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.
    ValueError
        If the amount is less than 0 or higher than 2 ** 63 - 1.
    TypeError
        If the amount is not an `int`.

    """
    if not isinstance(amount, int):
        msg = f"Amount must be of type int, not {type(amount)}."
        raise TypeError(msg)
    if not (0 < amount <= _MAX_BALANCE):
        msg = f"Amount must be greater than zero and less than {humanize_number(_MAX_BALANCE, override_locale='en_US')}."
        raise ValueError(msg)

    if await is_global():
        await _config.max_balance.set(amount)
        global _cache
        _cache["max_balance"] = amount
    elif guild is not None:
        await _config.guild(guild).max_balance.set(amount)
    else:
        msg = "Guild must be provided if setting the maximum balance of a guild-specific bank."
        raise RuntimeError(msg)
    return amount


async def get_default_balance(guild: discord.Guild = None) -> int:
    """Get the current default balance amount.

    Parameters
    ----------
    guild : `discord.Guild`, optional
        The guild to get the default balance for (required if bank is
        guild-specific).

    Returns
    -------
    int
        The bank's default balance.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        if _cache["default_balance"] is None:
            _cache["default_balance"] = await _config.default_balance()
        return _cache["default_balance"]
    elif guild is not None:
        return await _config.guild(guild).default_balance()
    else:
        msg = "Guild is missing and required!"
        raise RuntimeError(msg)


async def set_default_balance(amount: int, guild: discord.Guild = None) -> int:
    """Set the default balance amount.

    Parameters
    ----------
    amount : int
        The new default balance.
    guild : `discord.Guild`, optional
        The guild to set the default balance for (required if bank is
        guild-specific).

    Returns
    -------
    int
        The new default balance.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.
    ValueError
        If the amount is less than 0 or higher than the max allowed balance.
    TypeError
        If the amount is not an `int`.

    """
    if not isinstance(amount, int):
        msg = f"Amount must be of type int, not {type(amount)}."
        raise TypeError(msg)
    max_bal = await get_max_balance(guild)

    if not (0 <= amount <= max_bal):
        msg = f"Amount must be greater than or equal zero and less than or equal {humanize_number(max_bal, override_locale='en_US')}."
        raise ValueError(msg)

    if await is_global():
        await _config.default_balance.set(amount)
        global _cache
        _cache["default_balance"] = amount
    elif guild is not None:
        await _config.guild(guild).default_balance.set(amount)
    else:
        msg = "Guild is missing and required."
        raise RuntimeError(msg)

    return amount


class AbortPurchase(Exception):
    pass


def cost(amount: int):
    """Decorates a coroutine-function or command to have a cost.

    If the command raises an exception, the cost will be refunded.

    You can intentionally refund by raising `AbortPurchase` (this error
    will be consumed and not show to users)

    Other exceptions will propagate and will be handled by Melanie's
    (and/or any other configured) error handling.

    """
    # TODO: Add documentation for input/output/exceptions
    if not isinstance(amount, int) or amount < 0:
        msg = "This decorator requires an integer cost greater than or equal to zero"
        raise ValueError(msg)

    def deco(coro_or_command):
        is_command = isinstance(coro_or_command, commands.Command)
        if not is_command and not asyncio.iscoroutinefunction(coro_or_command):
            msg = "@bank.cost() can only be used on commands or `async def` functions"
            raise TypeError(msg)

        coro = coro_or_command.callback if is_command else coro_or_command

        @wraps(coro)
        async def wrapped(*args, **kwargs):
            context: commands.Context = next((arg for arg in args if isinstance(arg, commands.Context)), None)

            if not context.guild and not await is_global():
                msg = "Can't pay for this command in DM without a global bank."
                raise commands.UserFeedbackCheckFailure(msg)
            try:
                await withdraw_credits(context.author, amount)
            except Exception:
                credits_name = await get_currency_name(context.guild)
                msg = f"You need at least {humanize_number(amount)} {credits_name} to use this command."
                raise commands.UserFeedbackCheckFailure(msg)
            else:
                try:
                    return await coro(*args, **kwargs)
                except AbortPurchase:
                    await deposit_credits(context.author, amount)
                except Exception:
                    await deposit_credits(context.author, amount)
                    raise

        if not is_command:
            return wrapped
        wrapped.__module__ = coro_or_command.callback.__module__
        coro_or_command.callback = wrapped
        return coro_or_command

    return deco
