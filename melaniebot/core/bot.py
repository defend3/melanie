from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import platform
import time
import weakref
from collections import namedtuple
from collections.abc import Awaitable, Iterable, MutableMapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from enum import IntEnum
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Callable, Optional, TypeVar, Union, overload

import aiohttp
import aiopg
import arrow
import asyncpg
import discord
import distributed
import distributed.cfexecutor
import httpx
import melanie.core
import melanie.redis
import melanie.stats
from discord.ext import commands as dpy_commands
from discord.ext.commands import when_mentioned_or
from discord.ext.commands.view import StringView
from loguru import logger as log
from melanie import alru_cache, create_task, threaded
from melanie.curl import CurlAsyncHTTPClient
from runtimeopt.global_dask import GLOBAL_DASK
from tornado.httpclient import AsyncHTTPClient
from tornado.ioloop import IOLoop

from . import Config, bank, commands, drivers, errors, i18n, modlog
from .cog_manager import CogManager, CogManagerUI
from .core_commands import Core
from .data_manager import cog_data_path
from .dev_commands import Dev
from .events import init_events
from .global_checks import init_global_checks
from .rpc import RPCMixin
from .settings_caches import (
    DisabledCogCache,
    I18nManager,
    IgnoreManager,
    PrefixManager,
    WhitelistBlacklistManager,
)
from .utils import common_filters
from .utils._internal_utils import deprecated_removed

UserOrRole = Union[int, discord.Role, discord.Member, discord.User]
NotMessage = namedtuple("NotMessage", "guild")
DataDeletionResults = namedtuple("DataDeletionResults", "failed_modules failed_cogs unhandled")
PreInvokeCoroutine = Callable[[commands.Context], Awaitable[Any]]
CUSTOM_GROUPS = "CUSTOM_GROUPS"
COMMAND_SCOPE = "COMMAND"
SHARED_API_TOKENS = "SHARED_API_TOKENS"

T_BIC = TypeVar("T_BIC", bound=PreInvokeCoroutine)

DASK_SCHEDULER_URL = os.environ["DASK_HOST"]
REDIS_URL = os.environ["REDIS_URL"]
DB_URL = os.environ["MELANIE_DB_URL"]


def clean_repr(object: object) -> str:
    if not object:
        return "None"
    try:
        return " ".join(object.__repr__().replace(">", "").replace("<", "").split())
    except AttributeError:
        return str(type(object))


class Melanie(commands.GroupMixin, RPCMixin, dpy_commands.bot.Bot, AbstractAsyncContextManager):
    async def waits_uptime_for(self, dur: int = 10) -> float:
        await self.wait_until_ready()
        event = asyncio.Event()
        while not self.uptime:
            await asyncio.sleep(0)
        current = time.time() - arrow.get(self.uptime).timestamp()
        if dur > current:
            delta = dur - current
            log.warning("Need to wait for {} seconds", delta)
            self.ioloop.call_later(delta, event.set)
            await event.wait()

        return True

    def __repr__(self) -> str:
        return f"<Melaniebot PID:{os.getpid()} @ {id(self)}"

    def to_cluster(self, func, *args) -> asyncio.Future:
        return self.ioloop.run_in_executor(self.dask_exec, func, *args)

    def event(self, coro):
        if not asyncio.iscoroutinefunction(coro):
            msg = "event registered must be a coroutine function"
            raise TypeError(msg)
        setattr(self, coro.__name__, coro)
        log.opt(colors=True).success("<magenta>{}</magenta> registered as an event", coro.__name__)
        return coro

    def __init__(
        self,
        *args,
        redis,
        keydb,
        aiopgpool,
        stats_pool,
        aiohttpx,
        httpx_session,
        asyncpg_pool,
        aio_connector,
        bot_dir: Path = Path.cwd(),
        cli_flags=None,
        **kwargs,
    ) -> None:
        self._shutdown_mode = ExitCodes.CRITICAL
        self._dask: distributed.Client = None
        self.loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        self.v2 = None
        self.aio_connector: aiohttp.TCPConnector = aio_connector
        self.keydb: melanie.redis.MelanieRedis = keydb
        self.ioloop: IOLoop = IOLoop.current()

        self.dask_exec: distributed.cfexecutor.ClientExecutor = None
        self.dev_bot = False
        self.redis: melanie.redis.MelanieRedis = redis
        self.pgpool: aiopg.Pool = aiopgpool
        self.aiohttpx: aiohttp.ClientSession = aiohttpx
        self.data: melanie.stats.MelanieDataPool = None
        self.htx: httpx.AsyncClient = httpx_session
        self.asyncpg: asyncpg.Pool = asyncpg_pool
        self._cli_flags = cli_flags
        self.cli_flags = cli_flags
        self._shutup_group = set()
        self._config = Config.get_core_conf(force_registration=False)
        self.rpc_enabled = cli_flags.rpc
        self.rpc_port = cli_flags.rpc_port
        self._last_exception = None
        self._config.register_global(
            token=None,
            prefix=[],
            packages=[],
            owner=None,
            whitelist=[],
            blacklist=[],
            locale="en-US",
            regional_format=None,
            embeds=True,
            color=15158332,
            fuzzy=False,
            custom_info=None,
            help__page_char_limit=1000,
            help__max_pages_in_guild=2,
            help__delete_delay=0,
            help__use_menus=False,
            help__show_hidden=False,
            help__show_aliases=True,
            help__verify_checks=True,
            help__verify_exists=False,
            help__tagline="",
            help__use_tick=False,
            description="melaniebot",
            help__react_timeout=30,
            invite_public=False,
            invite_perm=0,
            invite_commands_scope=False,
            disabled_commands=[],
            disabled_command_msg="That command is disabled.",
            extra_owner_destinations=[],
            owner_opt_out_list=[],
            last_system_info__python_version=[3, 7],
            last_system_info__machine=None,
            last_system_info__system=None,
            schema_version=0,
            datarequests__allow_user_requests=True,
            datarequests__user_requests_are_strict=True,
        )

        self._config.register_guild(
            prefix=[],
            whitelist=[],
            blacklist=[],
            admin_role=[],
            mod_role=[],
            embeds=None,
            ignored=False,
            use_bot_color=False,
            fuzzy=False,
            disabled_commands=[],
            autoimmune_ids=[],
            delete_delay=-1,
            locale=None,
            regional_format=None,
        )

        self._config.register_channel(embeds=None, ignored=False)
        self._config.register_user(embeds=None)

        self._config.init_custom("COG_DISABLE_SETTINGS", 2)
        self._config.register_custom("COG_DISABLE_SETTINGS", disabled=None)

        self._config.init_custom(CUSTOM_GROUPS, 2)
        self._config.register_custom(CUSTOM_GROUPS)

        # GUILD_ID=0 for global setting
        self._config.init_custom(COMMAND_SCOPE, 2)
        self._config.register_custom(COMMAND_SCOPE, embeds=None)
        self._config.init_custom(SHARED_API_TOKENS, 2)
        self._config.register_custom(SHARED_API_TOKENS)
        self._prefix_cache = PrefixManager(self._config, cli_flags)
        self._disabled_cog_cache = DisabledCogCache(self._config)
        self._ignored_cache = IgnoreManager(self._config)
        self._whiteblacklist_cache = WhitelistBlacklistManager(self._config)
        self._i18n_cache = I18nManager(self._config)
        self._bypass_cooldowns = True

        #     user_data = await v2.static_login(self.)

        async def prefix_manager(bot, message) -> list[str]:
            prefixes = await self._prefix_cache.get_prefixes(message.guild)
            if cli_flags.mentionable:
                return when_mentioned_or(*prefixes)(bot, message)
            return prefixes

        if "command_prefix" not in kwargs:
            kwargs["command_prefix"] = prefix_manager

        if "owner_id" in kwargs:
            msg = "Melanie doesn't accept owner_id kwarg, use owner_ids instead."
            raise RuntimeError(msg)

        if "intents" not in kwargs:
            intents = discord.Intents.all()
            for intent_name in cli_flags.disable_intent:
                setattr(intents, intent_name, False)
            kwargs["intents"] = intents

        self._owner_id_overwrite = cli_flags.owner

        if "owner_ids" in kwargs:
            kwargs["owner_ids"] = set(kwargs["owner_ids"])
        else:
            kwargs["owner_ids"] = set()
        kwargs["owner_ids"].update(cli_flags.co_owner)

        if "command_not_found" not in kwargs:
            kwargs["command_not_found"] = "Command {} not found.\n{}"

        if "allowed_mentions" not in kwargs:
            kwargs["allowed_mentions"] = discord.AllowedMentions(everyone=False, roles=False)

        message_cache_size = 1200
        kwargs["max_messages"] = message_cache_size
        self._max_messages = message_cache_size

        self._uptime = None
        self._checked_time_accuracy = None
        self._color = discord.Embed.Empty  # This is needed or color ends up 0x000000

        self._main_dir = bot_dir
        self._cog_mgr = CogManager()
        self._use_team_features = cli_flags.use_team_features
        # to prevent multiple calls to app info in `is_owner()`
        self._app_owners_fetched = False
        super().__init__(
            *args,
            connector=self.aio_connector,
            case_insensitive=True,
            help_command=None,
            chunk_guilds_at_startup=True,
            **kwargs,
        )
        # Do not manually use the help formatter attribute here, see `send_help_for`,
        # for a documented API. The internals of this object are still subject to change.
        self._help_formatter = commands.help.RedHelpFormatter()
        self.add_command(commands.help.red_help)
        self._permissions_hooks: list[commands.CheckPredicate] = []
        self._red_ready = asyncio.Event()
        self._red_before_invoke_objs: set[PreInvokeCoroutine] = set()
        self._deletion_requests: MutableMapping[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self.cb = self.ioloop.add_callback

    async def wait_until_ready(self):
        await self._ready.wait()

    @property
    def aio(self) -> aiohttp.ClientSession:
        return self.aiohttpx

    @property
    def curl(self) -> CurlAsyncHTTPClient:
        """Aliased to AsyncHTTPClient().

        Returns
        -------
            CurlAsyncHTTPClient

        """
        return AsyncHTTPClient()

    @staticmethod
    def new_dask():
        log.warning("Creating new dask Client.")
        if os.getenv("DASK_LOCAL"):
            return None
        client = distributed.Client(DASK_SCHEDULER_URL, asynchronous=True, name=f"melaniebot PID:{os.getpid()}")
        GLOBAL_DASK["client"] = client
        return client

    @property
    def dask(self) -> distributed.Client:
        if self._dask:
            status = self._dask.status
            if status in ("connecting", "newly-created"):
                log.warning("Dask client is alive, but is reporting status {}", status)
            elif status == "closed":
                log.error("Recreating Dask client")
                self._dask = self.new_dask()
        else:
            self._dask = self.new_dask()
        return self._dask

    async def login_v2(self):
        self.v2 = None

    def set_help_formatter(self, formatter: commands.help.HelpFormatterABC):
        """Set's Melanie's help formatter.

        .. warning::
            This method is provisional.


        The formatter must implement all methods in
        ``commands.help.HelpFormatterABC``

        Cogs which set a help formatter should inform users of this.
        Users should not use multiple cogs which set a help formatter.

        This should specifically be an instance of a formatter.
        This allows cogs to pass a config object or similar to the
        formatter prior to the bot using it.

        See ``commands.help.HelpFormatterABC`` for more details.

        Raises
        ------
        RuntimeError
            If the default formatter has already been replaced
        TypeError
            If given an invalid formatter

        """
        if not isinstance(formatter, commands.help.HelpFormatterABC):
            msg = "Help formatters must inherit from `commands.help.HelpFormatterABC` and implement the required interfaces."
            raise TypeError(msg)

        # do not switch to isinstance, we want to know that this has not been overridden,
        # even with a subclass.
        if isinstance(self._help_formatter, commands.help.RedHelpFormatter):
            self._help_formatter = formatter
        else:
            msg = "The formatter has already been overridden."
            raise RuntimeError(msg)

    def reset_help_formatter(self):
        """Resets Melanie's help formatter.

        .. warning::
            This method is provisional.


        This exists for use in ``cog_unload`` for cogs which replace the formatter
        as well as for a rescue command in core_commands.

        """
        self._help_formatter = commands.help.RedHelpFormatter()

    def add_dev_env_value(self, name: str, value: Callable[[commands.Context], Any]):
        """Add a custom variable to the dev environment (``;debug``, ``;eval``,
        and ``;repl`` commands). If dev mode is disabled, nothing will happen.

        Example
        -------

        .. code-block:: python

            class MyCog(commands.Cog):
                def __init__(self, bot):
                    self.bot = bot
                    bot.add_dev_env_value("mycog", lambda ctx: self)
                    bot.add_dev_env_value("mycogdata", lambda ctx: self.settings[ctx.guild.id])

                def cog_unload(self):
                    self.bot.remove_dev_env_value("mycog")
                    self.bot.remove_dev_env_value("mycogdata")

        Once your cog is loaded, the custom variables ``mycog`` and ``mycogdata``
        will be included in the environment of dev commands.

        Parameters
        ----------
        name: str
            The name of your custom variable.
        value: Callable[[commands.Context], Any]
            The function returning the value of the variable.
            It must take a `commands.Context` as its sole parameter

        Raises
        ------
        TypeError
            ``value`` argument isn't a callable.
        ValueError
            The passed callable takes no or more than one argument.
        RuntimeError
            The name of the custom variable is either reserved by a variable
            from the default environment or already taken by some other custom variable.

        """
        signature = inspect.signature(value)
        if len(signature.parameters) != 1:
            msg = "Callable must take exactly one argument for context"
            raise ValueError(msg)
        dev = self.get_cog("Dev")
        if dev is None:
            return
        if name in {"bot", "ctx", "channel", "author", "guild", "message", "asyncio", "aiohttp", "discord", "commands", "_", "__name__", "__builtins__"}:
            msg = f"The name {name} is reserved for default environement."
            raise RuntimeError(msg)
        if name in dev.env_extensions:
            msg = f"The name {name} is already used."
            raise RuntimeError(msg)
        dev.env_extensions[name] = value

    def remove_dev_env_value(self, name: str):
        """Remove a custom variable from the dev environment.

        Parameters
        ----------
        name: str
            The name of the custom variable.

        Raises
        ------
        KeyError
            The custom variable was never set.

        """
        dev = self.get_cog("Dev")
        if dev is None:
            return
        del dev.env_extensions[name]

    def get_command(self, name: str) -> Optional[commands.Command]:
        com = super().get_command(name)
        assert com is None or isinstance(com, commands.Command)
        return com

    def get_cog(self, name: str) -> Optional[commands.Cog]:
        cog = super().get_cog(name)
        assert cog is None or isinstance(cog, commands.Cog)
        return cog

    @property
    def _before_invoke(self):  # DEP-WARN
        return self._red_before_invoke_method

    @_before_invoke.setter
    def _before_invoke(self, val):  # DEP-WARN
        """Prevent this from being overwritten in super().__init__."""

    async def _red_before_invoke_method(self, ctx):
        await self.wait_until_red_ready()
        if self._red_before_invoke_objs:
            return_exceptions = isinstance(ctx.command, commands.commands._RuleDropper)
            await asyncio.gather(*(coro(ctx) for coro in self._red_before_invoke_objs), return_exceptions=return_exceptions)

    async def cog_disabled_in_guild(self, cog: commands.Cog, guild: Optional[discord.Guild]) -> bool:
        """Check if a cog is disabled in a guild.

        Parameters
        ----------
        cog: commands.Cog
        guild: Optional[discord.Guild]

        Returns
        -------
        bool

        """
        if guild is None:
            return False
        return await self._disabled_cog_cache.cog_disabled_in_guild(cog.qualified_name, guild.id)

    async def cog_disabled_in_guild_raw(self, cog_name: str, guild_id: int) -> bool:
        """Check if a cog is disabled in a guild without the cog or guild object.

        Parameters
        ----------
        cog_name: str
            This should be the cog's qualified name, not necessarily the classname
        guild_id: int

        Returns
        -------
        bool

        """
        return await self._disabled_cog_cache.cog_disabled_in_guild(cog_name, guild_id)

    def remove_before_invoke_hook(self, coro: PreInvokeCoroutine) -> None:
        """Functional method to remove a `before_invoke` hook."""
        self._red_before_invoke_objs.discard(coro)

    def before_invoke(self, coro: T_BIC) -> T_BIC:
        """Overridden decorator method for Melanie's ``before_invoke`` behavior.

        This can safely be used purely functionally as well.

        3rd party cogs should remove any hooks which they register at unload
        using `remove_before_invoke_hook`

        Below behavior shared with discord.py:

        .. note::
            The ``before_invoke`` hooks are
            only called if all checks and argument parsing procedures pass
            without error. If any check or argument parsing procedures fail
            then the hooks are not called.

        Parameters
        ----------
        coro: Callable[[commands.Context], Awaitable[Any]]
            The coroutine to register as the pre-invoke hook.

        Raises
        ------
        TypeError
            The coroutine passed is not actually a coroutine.

        """
        if not asyncio.iscoroutinefunction(coro):
            msg = "The pre-invoke hook must be a coroutine."
            raise TypeError(msg)

        self._red_before_invoke_objs.add(coro)
        return coro

    async def ready2(self):
        from discord.state import ConnectionState

        state: ConnectionState = self._connection
        await state._delay_ready()

    async def before_identify_hook(self, shard_id, *, initial=False):
        """A hook that is called before IDENTIFYing a session.

        Same as in discord.py, but also dispatches "on_red_identify" bot
        event.

        """
        self.dispatch("red_before_identify", shard_id, initial)
        return await super().before_identify_hook(shard_id, initial=initial)

    @property
    def db_counter(self):
        return drivers.get_driver_class().stat_counter

    @property
    def uptime(self) -> datetime:
        """Allow access to the value, but we don't want cog creators setting it."""
        return self._uptime

    @property
    def max_messages(self) -> Optional[int]:
        return self._max_messages

    async def add_to_blacklist(self, users_or_roles: Iterable[UserOrRole], *, guild: Optional[discord.Guild] = None):
        """Add users or roles to the global or local blocklist.

        Parameters
        ----------
        users_or_roles : Iterable[Union[int, discord.Role, discord.Member, discord.User]]
            The items to add to the blocklist.
            Roles and role IDs should only be passed when updating a local blocklist.
        guild : Optional[discord.Guild]
            The guild, whose local blocklist should be modified.
            If not passed, the global blocklist will be modified.

        Raises
        ------
        TypeError
            The values passed were not of the proper type.

        """
        to_add: set[int] = {getattr(uor, "id", uor) for uor in users_or_roles}
        await self._whiteblacklist_cache.add_to_blacklist(guild, to_add)

    async def remove_from_blacklist(self, users_or_roles: Iterable[UserOrRole], *, guild: Optional[discord.Guild] = None):
        """Remove users or roles from the global or local blocklist.

        Parameters
        ----------
        users_or_roles : Iterable[Union[int, discord.Role, discord.Member, discord.User]]
            The items to remove from the blocklist.
            Roles and role IDs should only be passed when updating a local blocklist.
        guild : Optional[discord.Guild]
            The guild, whose local blocklist should be modified.
            If not passed, the global blocklist will be modified.

        Raises
        ------
        TypeError
            The values passed were not of the proper type.

        """
        to_remove: set[int] = {getattr(uor, "id", uor) for uor in users_or_roles}
        await self._whiteblacklist_cache.remove_from_blacklist(guild, to_remove)

    async def get_blacklist(self, guild: Optional[discord.Guild] = None) -> set[int]:
        """Get the global or local blocklist.

        Parameters
        ----------
        guild : Optional[discord.Guild]
            The guild to get the local blocklist for.
            If this is not passed, the global blocklist will be returned.

        Returns
        -------
        Set[int]
            The IDs of the blocked users/roles.

        """
        return await self._whiteblacklist_cache.get_blacklist(guild)

    async def clear_blacklist(self, guild: Optional[discord.Guild] = None):
        """Clears the global or local blocklist.

        Parameters
        ----------
        guild : Optional[discord.Guild]
            The guild, whose local blocklist should be cleared.
            If not passed, the global blocklist will be cleared.

        """
        await self._whiteblacklist_cache.clear_blacklist(guild)

    async def add_to_whitelist(self, users_or_roles: Iterable[UserOrRole], *, guild: Optional[discord.Guild] = None):
        """Add users or roles to the global or local allowlist.

        Parameters
        ----------
        users_or_roles : Iterable[Union[int, discord.Role, discord.Member, discord.User]]
            The items to add to the allowlist.
            Roles and role IDs should only be passed when updating a local allowlist.
        guild : Optional[discord.Guild]
            The guild, whose local allowlist should be modified.
            If not passed, the global allowlist will be modified.

        Raises
        ------
        TypeError
            The passed values were not of the proper type.

        """
        to_add: set[int] = {getattr(uor, "id", uor) for uor in users_or_roles}
        await self._whiteblacklist_cache.add_to_whitelist(guild, to_add)

    async def remove_from_whitelist(self, users_or_roles: Iterable[UserOrRole], *, guild: Optional[discord.Guild] = None):
        """Remove users or roles from the global or local allowlist.

        Parameters
        ----------
        users_or_roles : Iterable[Union[int, discord.Role, discord.Member, discord.User]]
            The items to remove from the allowlist.
            Roles and role IDs should only be passed when updating a local allowlist.
        guild : Optional[discord.Guild]
            The guild, whose local allowlist should be modified.
            If not passed, the global allowlist will be modified.

        Raises
        ------
        TypeError
            The passed values were not of the proper type.

        """
        to_remove: set[int] = {getattr(uor, "id", uor) for uor in users_or_roles}
        await self._whiteblacklist_cache.remove_from_whitelist(guild, to_remove)

    async def get_whitelist(self, guild: Optional[discord.Guild] = None):
        """Get the global or local allowlist.

        Parameters
        ----------
        guild : Optional[discord.Guild]
            The guild to get the local allowlist for.
            If this is not passed, the global allowlist will be returned.

        Returns
        -------
        Set[int]
            The IDs of the allowed users/roles.

        """
        return await self._whiteblacklist_cache.get_whitelist(guild)

    async def __aenter__(self):
        return self

    async def __aexit__(self, __exc_type: type[BaseException] | None, __exc_value: BaseException | None, __traceback: TracebackType | None) -> bool | None:
        if __exc_type:
            log.exception("Error closed while bot finished")

    async def clear_whitelist(self, guild: Optional[discord.Guild] = None):
        """Clears the global or local allowlist.

        Parameters
        ----------
        guild : Optional[discord.Guild]
            The guild, whose local allowlist should be cleared.
            If not passed, the global allowlist will be cleared.

        """
        await self._whiteblacklist_cache.clear_whitelist(guild)

    async def allowed_by_whitelist_blacklist(
        self,
        who: Optional[Union[discord.Member, discord.User]] = None,
        *,
        who_id: Optional[int] = None,
        guild: Optional[discord.Guild] = None,
        guild_id: Optional[int] = None,
        role_ids: Optional[list[int]] = None,
    ) -> bool:
        """This checks if a user or member is allowed to run things, as considered
        by Melanie's allowlist and blocklist.

        If given a user object, this function will check the global lists

        If given a member, this will additionally check guild lists

        If omiting a user or member, you must provide a value for ``who_id``

        You may also provide a value for ``guild_id`` in this case

        If providing a member by guild and member ids,
        you should supply ``role_ids`` as well

        Parameters
        ----------
        who : Optional[Union[discord.Member, discord.User]]
            The user or member object to check

        Other Parameters
        ----------------
        who_id : Optional[int]
            The id of the user or member to check
            If not providing a value for ``who``, this is a required parameter.
        guild : Optional[discord.Guild]
            When used in conjunction with a provided value for ``who_id``, checks
            the lists for the corresponding guild as well.
            This is ignored when ``who`` is passed.
        guild_id : Optional[int]
            When used in conjunction with a provided value for ``who_id``, checks
            the lists for the corresponding guild as well. This should not be used
            as it has unfixable bug that can cause it to raise an exception when
            the guild with the given ID couldn't have been found.
            This is ignored when ``who`` is passed.

            .. deprecated-removed:: 3.4.8 30
                Use ``guild`` parameter instead.

        role_ids : Optional[List[int]]
            When used with both ``who_id`` and ``guild_id``, checks the role ids provided.
            This is required for accurate checking of members in a guild if providing ids.
            This is ignored when ``who`` is passed.

        Raises
        ------
        TypeError
            Did not provide ``who`` or ``who_id``

        Returns
        -------
        bool
            `True` if user is allowed to run things, `False` otherwise

        """
        # Contributor Note:
        # All config calls are delayed until needed in this section
        # All changes should be made keeping in mind that this is also used as a global check

        mocked = False  # used for an accurate delayed role id expansion later.

        if not who:
            if not who_id:
                msg = "Must provide a value for either `who` or `who_id`"
                raise TypeError(msg)
            mocked = True
            who = discord.Object(id=who_id)
            if guild_id:
                deprecated_removed("`guild_id` parameter", "3.4.8", 30, "Use `guild` parameter instead.", stacklevel=2)
                if guild:
                    msg = "`guild_id` should not be passed when `guild` is already passed."
                    raise ValueError(msg)
        else:
            guild = getattr(who, "guild", None)

        if await self.is_owner(who):
            return True

        global_whitelist = await self.get_whitelist()
        if global_whitelist:
            if who.id not in global_whitelist:
                return False
        else:
            # blacklist is only used when whitelist doesn't exist.
            global_blacklist = await self.get_blacklist()
            if who.id in global_blacklist:
                return False

        if mocked and guild_id:
            guild = self.get_guild(guild_id)
            if guild is None:
                # this is an AttributeError due to backwards-compatibility concerns
                msg = "Couldn't get the guild with the given ID. `guild` parameter needs to be used over the deprecated `guild_id` to resolve this."
                raise AttributeError(msg)

        if guild:
            if guild.owner_id == who.id:
                return True

            # The delayed expansion of ids to check saves time in the DM case.
            # Converting to a set reduces the total lookup time in section
            if mocked:
                ids = {i for i in (who.id, *(role_ids or [])) if i != guild.id}
            else:
                # DEP-WARN
                # This uses member._roles (getattr is for the user case)
                # If this is removed upstream (undocumented)
                # there is a silent failure potential, and role blacklist/whitelists will break.
                ids = {i for i in (who.id, *(getattr(who, "_roles", []))) if i != guild.id}

            guild_whitelist = await self.get_whitelist(guild)
            if guild_whitelist:
                if ids.isdisjoint(guild_whitelist):
                    return False
            else:
                guild_blacklist = await self.get_blacklist(guild)
                if not ids.isdisjoint(guild_blacklist):
                    return False

        return True

    async def message_eligible_as_command(self, message: discord.Message) -> bool:
        """Runs through the things which apply globally about commands to
        determine if a message may be responded to as a command.

        This can't interact with permissions as permissions is hyper-local
        with respect to command objects, create command objects for this
        if that's needed.

        This also does not check for prefix or command conflicts,
        as it is primarily designed for non-prefix based response handling
        via on_message_no_cmd

        Parameters
        ----------
        message
            The message object to check

        Returns
        -------
        bool
            Whether or not the message is eligible to be treated as a command.

        """
        if message.author.bot:
            return False

        if guild := message.guild:
            channel = message.channel
            assert isinstance(channel, discord.abc.GuildChannel)  # nosec
            if not channel.permissions_for(guild.me).send_messages:
                return False
            if not (await self.ignored_channel_or_guild(message)):
                return False

        return bool(await self.allowed_by_whitelist_blacklist(message.author))

    async def ignored_channel_or_guild(self, ctx: Union[commands.Context, discord.Message]) -> bool:
        """This checks if the bot is meant to be ignoring commands in a channel or
        guild, as considered by Melanie's whitelist and blacklist.

        Parameters
        ----------
        ctx :
            Context object of the command which needs to be checked prior to invoking
            or a Message object which might be intended for use as a command.

        Returns
        -------
        bool
            `True` if commands are allowed in the channel, `False` otherwise

        """
        perms = ctx.channel.permissions_for(ctx.author)
        surpass_ignore = (
            isinstance(ctx.channel, discord.abc.PrivateChannel) or perms.manage_guild or await self.is_owner(ctx.author) or await self.is_admin(ctx.author)
        )
        if surpass_ignore:
            return True
        guild_ignored = await self._ignored_cache.get_ignored_guild(ctx.guild)
        chann_ignored = await self._ignored_cache.get_ignored_channel(ctx.channel)
        return not (guild_ignored or chann_ignored and not perms.manage_channels)

    async def get_valid_prefixes(self, guild: Optional[discord.Guild] = None) -> list[str]:
        """This gets the valid prefixes for a guild.

        If not provided a guild (or passed None) it will give the DM prefixes.

        This is just a fancy wrapper around ``get_prefix``

        Parameters
        ----------
        guild : Optional[discord.Guild]
            The guild you want prefixes for. Omit (or pass None) for the DM prefixes

        Returns
        -------
        List[str]
            If a guild was specified, the valid prefixes in that guild.
            If a guild was not specified, the valid prefixes for DMs

        """
        return await self.get_prefix(NotMessage(guild))

    async def set_prefixes(self, prefixes: list[str], guild: Optional[discord.Guild] = None):
        """Set global/server prefixes.

        If ``guild`` is not provided (or None is passed), this will set the global prefixes.

        Parameters
        ----------
        prefixes : List[str]
            The prefixes you want to set. Passing empty list will reset prefixes for the ``guild``
        guild : Optional[discord.Guild]
            The guild you want to set the prefixes for. Omit (or pass None) to set the global prefixes

        Raises
        ------
        TypeError
            If ``prefixes`` is not a list of strings
        ValueError
            If empty list is passed to ``prefixes`` when setting global prefixes

        """
        await self._prefix_cache.set_prefixes(guild=guild, prefixes=prefixes)

    async def get_embed_color(self, location: discord.abc.Messageable) -> discord.Color:
        """Get the embed color for a location. This takes into account all related
        settings.

        Parameters
        ----------
        location : `discord.abc.Messageable`
            Location to check embed color for.

        Returns
        -------
        discord.Color
            Embed color for the provided location.

        """
        return 3092790

    async def get_or_fetch_user(self, user_id: int) -> discord.User:
        """Retrieves a `discord.User` based on their ID. You do not have to share
        any guilds with the user to get this information, however many
        operations do require that you do.

        .. warning::

            This method may make an API call if the user is not found in the bot cache. For general usage, consider ``bot.get_user`` instead.

        Parameters
        ----------
        user_id: int
            The ID of the user that should be retrieved.

        Raises
        ------
        Errors
            Please refer to `discord.Client.fetch_user`.

        Returns
        -------
        discord.User
            The user you requested.

        """
        if (user := self.get_user(user_id)) is not None:
            return user
        return await self.fetch_user(user_id)

    async def on_error(self, event_method, *args, **kwargs):
        log.opt(exception=True).exception("Event '{}' raises an unexpected exception: Args: {} Kwargs: {}", event_method, args, kwargs)

    async def get_or_fetch_member(self, guild: discord.Guild, member_id: int) -> discord.Member:
        """Retrieves a `discord.Member` from a guild and a member ID.

        .. warning::

            This method may make an API call if the user is not found in the bot cache. For general usage, consider ``discord.Guild.get_member`` instead.

        Parameters
        ----------
        guild: discord.Guild
            The guild which the member should be retrieved from.
        member_id: int
            The ID of the member that should be retrieved.

        Raises
        ------
        Errors
            Please refer to `discord.Guild.fetch_member`.

        Returns
        -------
        discord.Member
            The user you requested.

        """
        if (member := guild.get_member(member_id)) is not None:
            return member
        return await guild.fetch_member(member_id)

    get_embed_colour = get_embed_color

    def reset_pgcache(self):
        driver = drivers.get_driver_class()
        driver.clear_cache()

    async def pre_flight(self, cli_flags):  # sourcery no-metrics
        """This should only be run once, prior to connecting to discord."""
        self.description = await self._config.description()
        init_global_checks(self)
        init_events(self, cli_flags)

        if self._owner_id_overwrite is None:
            self._owner_id_overwrite = await self._config.owner()
        if self._owner_id_overwrite is not None:
            self.owner_ids.add(self._owner_id_overwrite)

        i18n_locale = await self._config.locale()
        i18n.set_locale(i18n_locale)
        i18n_regional_format = await self._config.regional_format()
        i18n.set_regional_format(i18n_regional_format)
        ## Connections
        ## Connections
        self.add_cog(Core(self))
        self.add_cog(CogManagerUI())
        if cli_flags.dev:
            self.add_cog(Dev())
        await modlog._init(self)
        await bank._init()

        packages = []

        last_system_info = await self._config.last_system_info()

        cog_data_path(raw_name="Downloader") / "lib"

        if cli_flags.no_cogs is False:
            packages.extend(await self._config.packages())

        if cli_flags.load_cogs:
            packages.extend(cli_flags.load_cogs)

        machine = platform.machine()
        system = platform.system()
        if last_system_info["machine"] is None:
            await self._config.last_system_info.machine.set(machine)
        elif last_system_info["machine"] != machine:
            await self._config.last_system_info.machine.set(machine)

        if last_system_info["system"] is None:
            await self._config.last_system_info.system.set(system)
        elif last_system_info["system"] != system:
            await self._config.last_system_info.system.set(system)

        if packages:
            # Load permissions first, for security reasons
            try:
                packages.remove("permissions")
            except ValueError:
                pass
            else:
                packages.insert(0, "permissions")

            log.info("Loading packages...")

        async def load_packages():
            while not self.user:
                await asyncio.sleep(0.1)
            to_remove = []
            for package in packages:
                await asyncio.sleep(0.1)
                try:
                    spec = await self._cog_mgr.find_cog(package)
                    if spec is None:
                        log.exception("Failed to load package {} (package was not found in any cog path)", package)
                        await self.remove_loaded_package(package)
                        to_remove.append(package)
                        continue
                    await asyncio.wait_for(self.load_extension(spec), 30)
                    log.success("Loaded {}", package)
                except asyncio.TimeoutError:
                    log.exception("Failed to load package {} (timeout)", package)
                    to_remove.append(package)
                except asyncio.CancelledError as e:
                    raise e from e
                except Exception:
                    log.exception("Failed to load package {}", str(package))
                    await self.remove_loaded_package(package)
                    to_remove.append(package)
            for package in to_remove:
                packages.remove(package)
            if packages:
                log.info("Loaded packages: " + ", ".join(packages))
            if self.rpc_enabled:
                await self.rpc.initialize(self.rpc_port)

        create_task(load_packages())

    async def start(self, *args, **kwargs):
        """Overridden start which ensures cog load and other pre-connection tasks
        are handled.
        """
        cli_flags = kwargs.pop("cli_flags")
        self.__token = args[0]
        await self.pre_flight(cli_flags=cli_flags)
        return await super().start(*args, **kwargs)

    async def send_help_for(self, ctx: commands.Context, help_for: Union[commands.Command, commands.GroupMixin, str], *, from_help_command: bool = False):
        """Invokes Melanie's helpformatter for a given context and object."""
        return await self._help_formatter.send_help(ctx, help_for, from_help_command=from_help_command)

    async def embed_requested(
        self,
        channel: Union[discord.abc.GuildChannel, discord.abc.PrivateChannel],
        user: discord.abc.User,
        command: Optional[commands.Command] = None,
    ) -> bool:
        """Determine if an embed is requested for a response.

        Parameters
        ----------
        channel : `discord.abc.GuildChannel` or `discord.abc.PrivateChannel`
            The channel to check embed settings for.
        user : `discord.abc.User`
            The user to check embed settings for.
        command : `melaniebot.core.commands.Command`, optional
            The command ran.

        Returns
        -------
        bool
            :code:`True` if an embed is requested

        """

        async def get_command_setting(guild_id: int) -> Optional[bool]:
            if command is None:
                return None
            scope = self._config.custom(COMMAND_SCOPE, command.qualified_name, guild_id)
            return await scope.embeds()

        if isinstance(channel, discord.abc.PrivateChannel):
            if (user_setting := await self._config.user(user).embeds()) is not None:
                return user_setting
        else:
            if (channel_setting := await self._config.channel(channel).embeds()) is not None:
                return channel_setting

            if (command_setting := await get_command_setting(channel.guild.id)) is not None:
                return command_setting

            if (guild_setting := await self._config.guild(channel.guild).embeds()) is not None:
                return guild_setting

        # XXX: maybe this should be checked before guild setting?
        if (global_command_setting := await get_command_setting(0)) is not None:
            return global_command_setting

        global_setting = await self._config.embeds()
        return global_setting

    async def is_owner(self, user: Union[discord.User, discord.Member]) -> bool:
        """Determines if the user should be considered a bot owner.

        This takes into account CLI flags and application ownership.

        By default,
        application team members are not considered owners,
        while individual application owners are.

        Parameters
        ----------
        user: Union[discord.User, discord.Member]

        Returns
        -------
        bool

        """
        if user.id in self.owner_ids:
            return True

        ret = False
        if not self._app_owners_fetched:
            app = await self.application_info()
            if app.team:
                if self._use_team_features:
                    ids = {m.id for m in app.team.members}
                    self.owner_ids.update(ids)
                    ret = user.id in ids
            elif self._owner_id_overwrite is None:
                owner_id = app.owner.id
                self.owner_ids.add(owner_id)
                ret = user.id == owner_id
            self._app_owners_fetched = True

        return ret

    async def is_admin(self, member: discord.Member) -> bool:
        """Checks if a member is an admin of their guild."""
        with contextlib.suppress(AttributeError):
            member_snowflakes = member._roles  # DEP-WARN
            for snowflake in await self._config.guild(member.guild).admin_role():
                if member_snowflakes.has(snowflake):  # Dep-WARN
                    return True
        return False

    async def is_mod(self, member: discord.Member) -> bool:
        """Checks if a member is a mod or admin of their guild."""
        with contextlib.suppress(AttributeError):
            member_snowflakes = member._roles  # DEP-WARN
            for snowflake in await self._config.guild(member.guild).admin_role():
                if member_snowflakes.has(snowflake):  # DEP-WARN
                    return True
            for snowflake in await self._config.guild(member.guild).mod_role():
                if member_snowflakes.has(snowflake):  # DEP-WARN
                    return True
        return False

    async def get_admin_roles(self, guild: discord.Guild) -> list[discord.Role]:
        """Gets the admin roles for a guild."""
        ret: list[discord.Role] = []
        for snowflake in await self._config.guild(guild).admin_role():
            if r := guild.get_role(snowflake):
                ret.append(r)
        return ret

    async def get_mod_roles(self, guild: discord.Guild) -> list[discord.Role]:
        """Gets the mod roles for a guild."""
        ret: list[discord.Role] = []
        for snowflake in await self._config.guild(guild).mod_role():
            if r := guild.get_role(snowflake):
                ret.append(r)
        return ret

    @alru_cache(ttl=60)
    async def get_admin_role_ids(self, guild_id: int) -> list[int]:
        """Gets the admin role ids for a guild id."""
        return await self._config.guild(discord.Object(id=guild_id)).admin_role()

    @alru_cache(ttl=60)
    async def get_mod_role_ids(self, guild_id: int) -> list[int]:
        """Gets the mod role ids for a guild id."""
        return await self._config.guild(discord.Object(id=guild_id)).mod_role()

    @overload
    async def get_shared_api_tokens(self, service_name: str = ...) -> dict[str, str]:
        ...

    @overload
    async def get_shared_api_tokens(self, service_name: None = ...) -> dict[str, dict[str, str]]:
        ...

    async def get_shared_api_tokens(self, service_name: Optional[str] = None) -> Union[dict[str, dict[str, str]], dict[str, str]]:
        """Gets the shared API tokens for a service, or all of them if no argument
        specified.

        Parameters
        ----------
        service_name: str, optional
            The service to get tokens for. Leave empty to get tokens for all services.

        Returns
        -------
        Dict[str, Dict[str, str]] or Dict[str, str]
            A Mapping of token names to tokens.
            This mapping exists because some services have multiple tokens.
            If ``service_name`` is `None`, this method will return
            a mapping with mappings for all services.

        """
        if service_name is None:
            return await self._config.custom(SHARED_API_TOKENS).all()
        else:
            return await self._config.custom(SHARED_API_TOKENS, service_name).all()

    async def set_shared_api_tokens(self, service_name: str, **tokens: str):
        """Sets shared API tokens for a service.

        In most cases, this should not be used. Users should instead be using the
        ``set api`` command

        This will not clear existing values not specified.

        Parameters
        ----------
        service_name: str
            The service to set tokens for
        **tokens
            token_name -> token

        Examples
        --------
        Setting the api_key for youtube from a value in a variable ``my_key``

        >>> await ctx.bot.set_shared_api_tokens("youtube", api_key=my_key)

        """
        async with self._config.custom(SHARED_API_TOKENS, service_name).all() as group:
            group.update(tokens)
        self.dispatch("red_api_tokens_update", service_name, MappingProxyType(group))

    async def remove_shared_api_tokens(self, service_name: str, *token_names: str):
        """Removes shared API tokens.

        Parameters
        ----------
        service_name: str
            The service to remove tokens for
        *token_names: str
            The name of each token to be removed

        Examples
        --------
        Removing the api_key for youtube

        >>> await ctx.bot.remove_shared_api_tokens("youtube", "api_key")

        """
        async with self._config.custom(SHARED_API_TOKENS, service_name).all() as group:
            for name in token_names:
                group.pop(name, None)
        self.dispatch("red_api_tokens_update", service_name, MappingProxyType(group))

    async def remove_shared_api_services(self, *service_names: str):
        """Removes shared API services, as well as keys and tokens associated with
        them.

        Parameters
        ----------
        *service_names: str
            The services to remove.

        Examples
        --------
        Removing the youtube service

        >>> await ctx.bot.remove_shared_api_services("youtube")

        """
        async with self._config.custom(SHARED_API_TOKENS).all() as group:
            for service in service_names:
                group.pop(service, None)
        # dispatch needs to happen *after* it actually updates
        for service in service_names:
            self.dispatch("red_api_tokens_update", service, MappingProxyType({}))

    async def get_context(self, message: discord.Message, *, cls=commands.Context):
        view = StringView(message.content)
        ctx = cls(prefix=None, view=view, bot=self, message=message)
        if self._skip_check(message.author.id, self.user.id):
            return ctx
        prefix = await self.get_prefix(message)
        try:
            if fun_cog := self.get_cog("Fun"):
                if custom_prefix := fun_cog.prefix_cache.get(message.author.id):
                    prefix.extend(custom_prefix)
        except (OSError, ImportError):
            log.warning("Not using custom prefix")
        if message.author.id in self.owner_ids:
            if "---" not in prefix:
                prefix.append("---")

        elif "---" in prefix:
            prefix.remove("---")

        invoked_prefix = prefix
        if isinstance(prefix, str):
            if not view.skip_string(prefix):
                return ctx
        else:
            try:
                # if the context class' __init__ consumes something from the view this
                # will be wrong.  That seems unreasonable though.
                if message.content.startswith(tuple(prefix)):
                    invoked_prefix = discord.utils.find(view.skip_string, prefix)
                else:
                    return ctx
            except TypeError as e:
                if not isinstance(prefix, list):
                    msg = f"get_prefix must return either a string or a list of string, not {prefix.__class__.__name__}"
                    raise TypeError(msg) from e
                # It's possible a bad command_prefix got us here.
                for value in prefix:
                    if not isinstance(value, str):
                        msg = f"Iterable command_prefix or list returned from get_prefix must contain only strings, not {value.__class__.__name__}"
                        raise TypeError(msg) from e

                # Getting here shouldn't happen
                raise

        if self.strip_after_prefix:
            view.skip_ws()
        invoker = view.get_word()
        ctx.invoked_with = invoker
        ctx.prefix = invoked_prefix
        ctx.command = self.all_commands.get(invoker)
        return ctx

    async def process_commands(self, message: discord.Message):
        if not message.author.bot:
            ctx = await self.get_context(message)
            await self.invoke(ctx)
        else:
            ctx = None
        if ctx is None or ctx.valid is False:
            self.dispatch("message_no_cmd", message)

    @staticmethod
    def list_packages():
        """Lists packages present in the cogs folder."""
        return os.listdir("cogs")

    async def save_packages_status(self, packages):
        await self._config.packages.set(packages)

    async def add_loaded_package(self, pkg_name: str):
        async with self._config.packages() as curr_pkgs:
            if pkg_name not in curr_pkgs:
                curr_pkgs.append(pkg_name)

    async def remove_loaded_package(self, pkg_name: str):
        async with self._config.packages() as curr_pkgs:
            while pkg_name in curr_pkgs:
                curr_pkgs.remove(pkg_name)

    async def load_extension(self, spec: ModuleSpec):
        # NB: this completely bypasses `discord.ext.commands.Bot._load_from_module_spec`
        name = spec.name.split(".")[-1]
        if name in self.extensions:
            raise errors.PackageAlreadyLoaded(spec)

        asyncio.get_running_loop()
        task = threaded(spec.loader.load_module)
        lib = await task()
        if not hasattr(lib, "setup"):
            del lib
            msg = f"extension {name} does not have a setup function"
            raise discord.ClientException(msg)
        try:

            async def run_setup():
                if asyncio.iscoroutinefunction(lib.setup):
                    await lib.setup(self)
                else:
                    lib.setup(self)

            self.ioloop.call_later(0.5, run_setup)

        except Exception:
            self._remove_module_references(lib.__name__)
            self._call_module_finalizers(lib, name)
            raise
        else:
            self._BotBase__extensions[name] = lib

    @alru_cache(ttl=30)
    async def fetch_invite(self, url, *, with_counts=True):
        return await super().fetch_invite(url, with_counts=with_counts)

    def remove_cog(self, cogname: str):
        cog = self.get_cog(cogname)
        if cog is None:
            return
        for cls in inspect.getmro(cog.__class__):
            try:
                hook = getattr(cog, f"_{cls.__name__}__permissions_hook")
            except AttributeError:
                pass
            else:
                self.remove_permissions_hook(hook)

        super().remove_cog(cogname)

        cog.requires.reset()

        for meth in self.rpc_handlers.pop(cogname.upper(), ()):
            self.unregister_rpc_handler(meth)

    async def is_automod_immune(self, to_check: Union[discord.Message, commands.Context, discord.abc.User, discord.Role]) -> bool:
        """Checks if the user, message, context, or role should be considered
        immune from automated moderation actions.

        This will return ``False`` in direct messages.

        Parameters
        ----------
        to_check : `discord.Message` or `commands.Context` or `discord.abc.User` or `discord.Role`
            Something to check if it would be immune

        Returns
        -------
        bool
            ``True`` if immune

        """
        guild = getattr(to_check, "guild", None)
        if not guild:
            return False

        if isinstance(to_check, discord.Role):
            ids_to_check = [to_check.id]
        else:
            author = getattr(to_check, "author", to_check)
            try:
                ids_to_check = [r.id for r in author.roles]
            except AttributeError:
                # webhook messages are a user not member,
                # cheaper than isinstance
                if author.bot and author.discriminator == "0000":
                    return True  # webhooks require significant permissions to enable.
            else:
                ids_to_check.append(author.id)

        immune_ids = await self._config.guild(guild).autoimmune_ids()

        return any(i in immune_ids for i in ids_to_check)

    @staticmethod
    async def send_filtered(destination: discord.abc.Messageable, filter_mass_mentions=True, filter_invite_links=True, filter_all_links=False, **kwargs):
        """This is a convenience wrapper around.

        discord.abc.Messageable.send

        It takes the destination you'd like to send to, which filters to apply
        (defaults on mass mentions, and invite links) and any other parameters
        normally accepted by destination.send

        This should realistically only be used for responding using user provided
        input. (unfortunately, including usernames)
        Manually crafted messages which don't take any user input have no need of this

        Returns
        -------
        discord.Message
            The message that was sent.

        """
        content = kwargs.pop("content", None)

        if content:
            if filter_mass_mentions:
                content = common_filters.filter_mass_mentions(content)
            if filter_invite_links:
                content = common_filters.filter_invites(content)
            if filter_all_links:
                content = common_filters.filter_urls(content)

        return await destination.send(content=content, **kwargs)

    def add_cog(self, cog: commands.Cog):
        if not isinstance(cog, commands.Cog):
            msg = f"The {cog.__class__.__name__} cog in the {cog.__module__} package does not inherit from the commands.Cog base class. The cog author must update the cog to adhere to this requirement."
            raise RuntimeError(msg)
        if cog.__cog_name__ in self.cogs:
            msg = f"There is already a cog named {cog.__cog_name__} loaded."
            raise RuntimeError(msg)
        if not hasattr(cog, "requires"):
            commands.Cog.__init__(cog)

        added_hooks = []

        try:
            for cls in inspect.getmro(cog.__class__):
                try:
                    hook = getattr(cog, f"_{cls.__name__}__permissions_hook")
                except AttributeError:
                    pass
                else:
                    self.add_permissions_hook(hook)
                    added_hooks.append(hook)

            super().add_cog(cog)
            self.dispatch("cog_add", cog)
            if "permissions" not in self.extensions:
                cog.requires.ready_event.set()
        except Exception:
            for hook in added_hooks:
                try:
                    self.remove_permissions_hook(hook)
                except Exception:
                    # This shouldn't be possible
                    log.exception("A hook got extremely screwed up, and could not be removed properly during another error in cog load.")
            del cog
            raise

    def add_command(self, command: commands.Command) -> None:
        if not isinstance(command, commands.Command):
            msg = "Commands must be instances of `melaniebot.core.commands.Command`"
            raise RuntimeError(msg)

        super().add_command(command)

        permissions_not_loaded = "permissions" not in self.extensions
        self.dispatch("command_add", command)
        if permissions_not_loaded:
            command.requires.ready_event.set()
        if isinstance(command, commands.Group):
            for subcommand in command.walk_commands():
                self.dispatch("command_add", subcommand)
                if permissions_not_loaded:
                    subcommand.requires.ready_event.set()

    def remove_command(self, name: str) -> Optional[commands.Command]:
        command = super().remove_command(name)
        if command is None:
            return None
        command.requires.reset()
        if isinstance(command, commands.Group):
            for subcommand in command.walk_commands():
                subcommand.requires.reset()
        return command

    def clear_permission_rules(self, guild_id: Optional[int], **kwargs) -> None:
        """Clear all permission overrides in a scope.

        Parameters
        ----------
        guild_id : Optional[int]
            The guild ID to wipe permission overrides for. If
            ``None``, this will clear all global rules and leave all
            guild rules untouched.

        **kwargs
            Keyword arguments to be passed to each required call of
            ``commands.Requires.clear_all_rules``

        """
        for cog in self.cogs.values():
            cog.requires.clear_all_rules(guild_id, **kwargs)
        for command in self.walk_commands():
            command.requires.clear_all_rules(guild_id, **kwargs)

    def add_permissions_hook(self, hook: commands.CheckPredicate) -> None:
        """Add a permissions hook.

        Permissions hooks are check predicates which are called before
        calling `Requires.verify`, and they can optionally return an
        override: ``True`` to allow, ``False`` to deny, and ``None`` to
        default to normal behaviour.

        Parameters
        ----------
        hook
            A command check predicate which returns ``True``, ``False``
            or ``None``.

        """
        self._permissions_hooks.append(hook)

    def remove_permissions_hook(self, hook: commands.CheckPredicate) -> None:
        """Remove a permissions hook.

        Parameters are the same as those in `add_permissions_hook`.

        Raises
        ------
        ValueError
            If the permissions hook has not been added.

        """
        self._permissions_hooks.remove(hook)

    async def verify_permissions_hooks(self, ctx: commands.Context) -> Optional[bool]:
        """Run permissions hooks.

        Parameters
        ----------
        ctx : commands.Context
            The context for the command being invoked.

        Returns
        -------
        Optional[bool]
            ``False`` if any hooks returned ``False``, ``True`` if any
            hooks return ``True`` and none returned ``False``, ``None``
            otherwise.

        """
        hook_results = []
        for hook in self._permissions_hooks:
            result = await discord.utils.maybe_coroutine(hook, ctx)
            if result is not None:
                hook_results.append(result)
        if hook_results:
            if all(hook_results):
                ctx.permission_state = commands.PermState.ALLOWED_BY_HOOK
                return True
            else:
                ctx.permission_state = commands.PermState.DENIED_BY_HOOK
                return False

    async def get_owner_notification_destinations(self) -> list[discord.abc.Messageable]:
        """Gets the users and channels to send to."""
        await self.wait_until_red_ready()
        destinations = []
        opt_outs = await self._config.owner_opt_out_list()
        for user_id in self.owner_ids:
            if user_id not in opt_outs:
                user = self.get_user(user_id)
                if user and not user.bot:  # user.bot is possible with flags and teams
                    destinations.append(user)
                else:
                    log.warning("Owner with ID {} is missing in user cache, ignoring owner notification destination.", user_id)

        channel_ids = await self._config.extra_owner_destinations()
        for channel_id in channel_ids:
            if channel := self.get_channel(channel_id):
                destinations.append(channel)
            else:
                log.warning("Channel with ID {} is not available, ignoring owner notification destination.", channel_id)

        return destinations

    async def send_to_owners(self, content=None, **kwargs):
        """This sends something to all owners and their configured extra
        destinations.

        This takes the same arguments as discord.abc.Messageable.send

        This logs failing sends

        """
        destinations = await self.get_owner_notification_destinations()

        async def wrapped_send(location, content=None, **kwargs):
            if await self.redis.get(f"no_owner_dm:{location.id}"):
                return log.warning("No owner ")
            try:
                await location.send(content, **kwargs)
            except Exception:
                log.exception("I could not send an owner notification to {} ({})", location, location.id)

        sends = [wrapped_send(d, content, **kwargs) for d in destinations]
        await asyncio.gather(*sends)

    async def wait_until_red_ready(self):
        """Wait until our post connection startup is done."""
        await self._red_ready.wait()

    async def _delete_delay(self, ctx: commands.Context):
        """Currently used for:

        * delete delay

        """
        guild = ctx.guild
        if guild is None:
            return
        message = ctx.message
        delay = await self._config.guild(guild).delete_delay()

        if delay == -1:
            return

        async def _delete_helper(m):
            with contextlib.suppress(discord.HTTPException):
                await m.delete()
                log.debug(f"Deleted command msg {m.id}")

        await asyncio.sleep(delay)
        await _delete_helper(message)

    @property
    def driver(self):
        return drivers.get_driver_class()

    async def close(self):
        """Logs out of Discord and closes all connections."""
        await super().close()
        await drivers.get_driver_class().teardown()
        log.warning("Driver closed")

        with contextlib.suppress(AttributeError):
            if self.rpc_enabled:
                await self.rpc.close()

    async def shutdown(self, *, restart: bool = False):
        """Gracefully quit Melanie.

        The program will exit with code :code:`0` by default.

        Parameters
        ----------
        restart : bool
            If :code:`True`, the program will exit with code :code:`26`. If the
            launcher sees this, it will attempt to restart the bot.

        """
        self._shutdown_mode = ExitCodes.RESTART if restart else ExitCodes.SHUTDOWN

        await self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return


class ExitCodes(IntEnum):
    # This needs to be an int enum to be used
    # with sys.exit
    CRITICAL = 1
    SHUTDOWN = 0
    RESTART = 26
