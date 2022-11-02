from functools import reduce
import enum
import re

import discord

from config import *


INTENTS = discord.Intents(
        guilds = True,
        members = True,
        messages = True,
        reactions = True)


PERMS = discord.permissions.Permissions(
        add_reactions = True,
        read_messages = True,
        send_messages = True,
        manage_messages = True,
        embed_links = True,
        attach_files = True,
        use_external_emojis = True,
        manage_webhooks = True)


# limits for non-Nitro users by boost level
MAX_FILE_SIZE = [
        8*1024*1024,
        8*1024*1024,
        50*1024*1024,
        100*1024*1024]


@enum.unique
class Prefs(enum.IntFlag):
#   auto        = 1 << 0
    replace     = 1 << 1
#   autoswap    = 1 << 2
    errors      = 1 << 3
    delay       = 1 << 4
    latch       = 1 << 5


@enum.unique
class ProxyType(enum.IntEnum):
    override    = 0
    collective  = 1
    swap        = 2


@enum.unique
class ProxyFlags(enum.IntFlag):
    auto        = 1 << 0
    keepproxy   = 1 << 1


@enum.unique
class ProxyState(enum.IntEnum):
    hidden      = 0
    inactive    = 1
    active      = 2


DEFAULT_PREFS = reduce(lambda a, b : a | Prefs[b], DEFAULT_PREFS, 0)
REPLACEMENTS = [(re.compile(x, re.IGNORECASE), y) for x, y in REPLACEMENTS]
HELPMSGS = {topic: text.format(p = COMMAND_PREFIX) for topic, text
        in HELPMSGS.items()}

