from __future__ import annotations

import contextlib
import keyword
import pkgutil
from importlib import import_module, invalidate_caches
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Optional, Union

import discord

import melaniebot.cogs
from melaniebot.core.utils import deduplicate_iterables

from . import checks, commands
from .config import Config
from .data_manager import cog_data_path
from .utils.chat_formatting import box, pagify

__all__ = ["CogManager"]


class NoSuchCog(ImportError):
    """Thrown when a cog is missing.

    Different from ImportError because some ImportErrors can happen
    inside cogs.

    """


class CogManager:
    """Directory manager for Melanie's cogs.

    This module allows you to load cogs from multiple directories and
    even from outside the bot directory. You may also set a directory
    for downloader to install new cogs to, the default being the
    :code:`cogs/` folder in the root bot directory.

    """

    CORE_PATH = Path(melaniebot.cogs.__path__[0])

    def __init__(self) -> None:
        self.config = Config.get_conf(self, 2938473984732, True)
        tmp_cog_install_path = cog_data_path(self) / "cogs"
        tmp_cog_install_path.mkdir(parents=True, exist_ok=True)
        self.config.register_global(paths=[], install_path=str(tmp_cog_install_path))

    async def paths(self) -> list[Path]:
        """Get all currently valid path directories, in order of priority.

        Returns
        -------
        List[pathlib.Path]
            A list of paths where cog packages can be found. The
            install path is highest priority, followed by the
            user-defined paths, and the core path has the lowest
            priority.

        """
        return deduplicate_iterables([await self.install_path()], await self.user_defined_paths(), [self.CORE_PATH])

    async def install_path(self) -> Path:
        """Get the install path for 3rd party cogs.

        Returns
        -------
        pathlib.Path
            The path to the directory where 3rd party cogs are stored.

        """
        return Path(await self.config.install_path()).resolve()

    async def user_defined_paths(self) -> list[Path]:
        """Get a list of user-defined cog paths.

        All paths will be absolute and unique, in order of priority.

        Returns
        -------
        List[pathlib.Path]
            A list of user-defined paths.

        """
        return list(map(Path, deduplicate_iterables(await self.config.paths())))

    async def set_install_path(self, path: Path) -> Path:
        """Set the install path for 3rd party cogs.

        Note
        ----
        The bot will not remember your old cog install path which means
        that **all previously installed cogs** will no longer be found.

        Parameters
        ----------
        path : pathlib.Path
            The new directory for cog installs.

        Returns
        -------
        pathlib.Path
            Absolute path to the new install directory.

        Raises
        ------
        ValueError
            If :code:`path` is not an existing directory.

        """
        if not path.is_dir():
            msg = "The install path must be an existing directory."
            raise ValueError(msg)
        resolved = path.resolve()
        await self.config.install_path.set(str(resolved))
        return resolved

    @staticmethod
    def _ensure_path_obj(path: Union[Path, str]) -> Path:
        """Guarantee an object will be a path object.

        Parameters
        ----------
        path : `pathlib.Path` or `str`

        Returns
        -------
        pathlib.Path

        """
        return Path(path)

    async def add_path(self, path: Union[Path, str]) -> None:
        """Add a cog path to current list.

        This will ignore duplicates.

        Parameters
        ----------
        path : `pathlib.Path` or `str`
            Path to add.

        Raises
        ------
        ValueError
            If :code:`path` does not resolve to an existing directory.

        """
        path = self._ensure_path_obj(path)

        # This makes the path absolute, will break if a bot install
        # changes OS/Computer?
        path = path.resolve()

        if not path.is_dir():
            msg = f"'{path}' is not a valid directory."
            raise ValueError(msg)

        if path == await self.install_path():
            msg = "Cannot add the install path as an additional path."
            raise ValueError(msg)
        if path == self.CORE_PATH:
            msg = "Cannot add the core path as an additional path."
            raise ValueError(msg)

        current_paths = await self.user_defined_paths()
        if path not in current_paths:
            current_paths.append(path)
            await self.set_paths(current_paths)

    async def remove_path(self, path: Union[Path, str]) -> None:
        """Remove a path from the current paths list.

        Parameters
        ----------
        path : `pathlib.Path` or `str`
            Path to remove.

        """
        path = self._ensure_path_obj(path)
        paths = await self.user_defined_paths()

        paths.remove(path)
        await self.set_paths(paths)

    async def set_paths(self, paths_: list[Path]):
        """Set the current paths list.

        Parameters
        ----------
        paths_ : `list` of `pathlib.Path`
            List of paths to set.

        """
        str_paths = list(map(str, paths_))
        await self.config.paths.set(str_paths)

    async def _find_ext_cog(self, name: str) -> ModuleSpec:
        """Attempts to find a spec for a third party installed cog.

        Parameters
        ----------
        name : str
            Name of the cog package to look for.

        Returns
        -------
        importlib.machinery.ModuleSpec
            Module spec to be used for cog loading.

        Raises
        ------
        NoSuchCog
            When no cog with the requested name was found.

        """
        if not name.isidentifier() or keyword.iskeyword(name):
            # reject package names that can't be valid python identifiers
            msg = f"No 3rd party module by the name of '{name}' was found in any available path."
            raise NoSuchCog(msg, name=name)

        real_paths = list(map(str, [await self.install_path()] + await self.user_defined_paths()))

        for finder, module_name, _ in pkgutil.iter_modules(real_paths):
            if name == module_name:
                if spec := finder.find_spec(name):
                    return spec

        msg = f"No 3rd party module by the name of '{name}' was found in any available path."
        raise NoSuchCog(msg, name=name)

    @staticmethod
    async def _find_core_cog(name: str) -> ModuleSpec:
        """Attempts to find a spec for a core cog.

        Parameters
        ----------
        name : str

        Returns
        -------
        importlib.machinery.ModuleSpec

        Raises
        ------
        RuntimeError
            When no matching spec can be found.

        """
        real_name = f".{name}"
        package = "melaniebot.cogs"

        try:
            mod = import_module(real_name, package=package)
        except ImportError as e:
            if e.name == package + real_name:
                msg = f"No core cog by the name of '{name}' could be found."
                raise NoSuchCog(msg, path=e.path, name=e.name) from e

            raise

        return mod.__spec__

    # noinspection PyUnreachableCode
    async def find_cog(self, name: str) -> Optional[ModuleSpec]:
        """Find a cog in the list of available paths.

        Parameters
        ----------
        name : str
            Name of the cog to find.

        Returns
        -------
        Optional[importlib.machinery.ModuleSpec]
            A module spec to be used for specialized cog loading, if found.

        """
        with contextlib.suppress(NoSuchCog):
            return await self._find_ext_cog(name)

        with contextlib.suppress(NoSuchCog):
            return await self._find_core_cog(name)

    async def available_modules(self) -> list[str]:
        """Finds the names of all available modules to load."""
        paths = list(map(str, await self.paths()))

        return [module_name for finder, module_name, _ in pkgutil.iter_modules(paths) if module_name.isidentifier() and not keyword.iskeyword(module_name)]

    @staticmethod
    def invalidate_caches():
        """Re-evaluate modules in the py cache.

        This is an alias for an importlib internal and should be called
        any time that a new module has been installed to a cog
        directory.

        """
        invalidate_caches()


def _(x):
    return x


class CogManagerUI(commands.Cog):
    """Commands to interface with Melanie's cog manager."""

    @commands.command()
    @checks.is_owner()
    async def paths(self, ctx: commands.Context):
        """Lists current cog paths in order of priority."""
        cog_mgr = ctx.bot._cog_mgr
        install_path = await cog_mgr.install_path()
        core_path = cog_mgr.CORE_PATH
        cog_paths = await cog_mgr.user_defined_paths()

        partial = [f"{i}. {p}" for i, p in enumerate(cog_paths, start=1)]
        msg = f"Install Path: {install_path}\nCore Path: {core_path}\n\n" + "\n".join(partial)
        await ctx.send(box(msg))

    @commands.command()
    @checks.is_owner()
    async def addpath(self, ctx: commands.Context, *, path: Path):
        """Add a path to the list of available cog paths."""
        if not path.is_dir():
            await ctx.send("That path does not exist or does not point to a valid directory.")
            return

        try:
            await ctx.bot._cog_mgr.add_path(path)
        except ValueError as e:
            await ctx.send(str(e))
        else:
            await ctx.send("Path successfully added.")

    @commands.command()
    @checks.is_owner()
    async def removepath(self, ctx: commands.Context, path_number: int):
        """Removes a path from the available cog paths given the `path_number`
        from `;paths`.
        """
        path_number -= 1
        if path_number < 0:
            await ctx.send("Path numbers must be positive.")
            return

        cog_paths = await ctx.bot._cog_mgr.user_defined_paths()
        try:
            to_remove = cog_paths.pop(path_number)
        except IndexError:
            await ctx.send("That is an invalid path number.")
            return

        await ctx.bot._cog_mgr.remove_path(to_remove)
        await ctx.send("Path successfully removed.")

    @commands.command()
    @checks.is_owner()
    async def reorderpath(self, ctx: commands.Context, from_: int, to: int):
        """Reorders paths internally to allow discovery of different cogs."""
        # Doing this because in the paths command they're 1 indexed
        from_ -= 1
        to -= 1
        if from_ < 0 or to < 0:
            await ctx.send("Path numbers must be positive.")
            return

        all_paths = await ctx.bot._cog_mgr.user_defined_paths()
        try:
            to_move = all_paths.pop(from_)
        except IndexError:
            await ctx.send("Invalid 'from' index.")
            return

        try:
            all_paths.insert(to, to_move)
        except IndexError:
            await ctx.send("Invalid 'to' index.")
            return

        await ctx.bot._cog_mgr.set_paths(all_paths)
        await ctx.send("Paths reordered.")

    @commands.command()
    @checks.is_owner()
    async def installpath(self, ctx: commands.Context, path: Path = None):
        """Returns the current install path or sets it if one is provided. The
        provided path must be absolute or relative to the bot's directory and
        it must already exist.

        No installed cogs will be transferred in the process.

        """
        if path:
            if not path.is_absolute():
                path = (ctx.bot._main_dir / path).resolve()
            try:
                await ctx.bot._cog_mgr.set_install_path(path)
            except ValueError:
                await ctx.send("That path does not exist.")
                return

        install_path = await ctx.bot._cog_mgr.install_path()
        await ctx.send(f"The bot will install new cogs to the `{install_path}` directory.")

    @commands.command()
    @checks.is_owner()
    async def cogs(self, ctx: commands.Context):
        """Lists all loaded and available cogs."""
        loaded = set(ctx.bot.extensions.keys())

        all_cogs = set(await ctx.bot._cog_mgr.available_modules())

        unloaded = all_cogs - loaded

        loaded = sorted(loaded, key=str.lower)
        unloaded = sorted(unloaded, key=str.lower)

        if await ctx.embed_requested():
            loaded = f"**{len(loaded)} loaded:**\n" + ", ".join(loaded)
            unloaded = f"**{len(unloaded)} unloaded:**\n" + ", ".join(unloaded)

            for page in pagify(loaded, delims=[", ", "\n"], page_length=1800):
                if page.startswith(", "):
                    page = page[2:]
                e = discord.Embed(description=page, colour=discord.Colour.dark_green())
                await ctx.send(embed=e)

            for page in pagify(unloaded, delims=[", ", "\n"], page_length=1800):
                if page.startswith(", "):
                    page = page[2:]
                e = discord.Embed(description=page, colour=discord.Colour.dark_red())
                await ctx.send(embed=e)
        else:
            loaded_count = f"**{len(loaded)} loaded:**\n"
            loaded = ", ".join(loaded)
            unloaded_count = f"**{len(unloaded)} unloaded:**\n"
            unloaded = ", ".join(unloaded)
            loaded_count_sent = False
            unloaded_count_sent = False
            for page in pagify(loaded, delims=[", ", "\n"], page_length=1800):
                if page.startswith(", "):
                    page = page[2:]
                if not loaded_count_sent:
                    await ctx.send(loaded_count + box(page, lang="css"))
                    loaded_count_sent = True
                else:
                    await ctx.send(box(page, lang="css"))

            for page in pagify(unloaded, delims=[", ", "\n"], page_length=1800):
                if page.startswith(", "):
                    page = page[2:]
                if not unloaded_count_sent:
                    await ctx.send(unloaded_count + box(page, lang="ldif"))
                    unloaded_count_sent = True
                else:
                    await ctx.send(box(page, lang="ldif"))
