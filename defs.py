from functools import reduce
import enum
import re

import discord

from config import *


# from discord markdown parser
LINK_REGEX = re.compile(r'<?(https?:\/\/[^\s<]+[^<.,:;"\')\]\s])>?')
# only match links that expire since cdn has valid non expiring images
# such as avatars. hopefully used with consent...
# but reuploading is trivial so there isn't much point to blocking that
CDN_REGEX = re.compile(r'.*ex=[0-9a-f]+&is=[0-9a-f]+&hm=[0-9a-f]+&?')
MESSAGE_LINK_REGEX = re.compile(
        r'https://discord\.com/channels/[0-9]+/([0-9]+)/([0-9]+)')


QUOTE_REGEXES = [
        (['\''], ['\'']),
        (['"'], ['"']),
        (
            [
                '\N{LEFT DOUBLE QUOTATION MARK}',
                '\N{RIGHT DOUBLE QUOTATION MARK}',
                '\N{DOUBLE HIGH-REVERSED-9 QUOTATION MARK}',
                '\N{DOUBLE LOW-9 QUOTATION MARK}',
            ],
            [
                '\N{LEFT DOUBLE QUOTATION MARK}',
                '\N{RIGHT DOUBLE QUOTATION MARK}',
                '\N{DOUBLE HIGH-REVERSED-9 QUOTATION MARK}',
            ]
        ),
        (
            [
                '\N{LEFT SINGLE QUOTATION MARK}',
                '\N{RIGHT SINGLE QUOTATION MARK}',
                '\N{SINGLE HIGH-REVERSED-9 QUOTATION MARK}',
                '\N{SINGLE LOW-9 QUOTATION MARK}',
            ],
            [
                '\N{LEFT SINGLE QUOTATION MARK}',
                '\N{RIGHT SINGLE QUOTATION MARK}',
                '\N{SINGLE HIGH-REVERSED-9 QUOTATION MARK}',
            ]
        ),
        (
            [
                '\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}',
                '\N{LEFT DOUBLE ANGLE BRACKET}',
            ],
            [
                '\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}',
                '\N{RIGHT DOUBLE ANGLE BRACKET}',
            ]
        ),
        (
            [
                '\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}',
                '\N{RIGHT DOUBLE ANGLE BRACKET}',
            ],
            [
                '\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}',
                '\N{LEFT DOUBLE ANGLE BRACKET}',
            ]
        ),
        (
            [
                '\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}',
                '\N{LEFT ANGLE BRACKET}',
            ],
            [
                '\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}',
                '\N{RIGHT ANGLE BRACKET}',
            ]
        ),
        (
            [
                '\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}',
                '\N{RIGHT ANGLE BRACKET}',
            ],
            [
                '\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}',
                '\N{LEFT ANGLE BRACKET}',
            ]
        ),
        (
            [
                '\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}',
                '\N{LEFT ANGLE BRACKET}',
            ],
            [
                '\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}',
                '\N{RIGHT ANGLE BRACKET}',
            ]
        ),
        (
            [
                '\N{LEFT CORNER BRACKET}',
                '\N{LEFT WHITE CORNER BRACKET}',
            ],
            [
                '\N{RIGHT CORNER BRACKET}',
                '\N{RIGHT WHITE CORNER BRACKET}',
            ]
        )
    ]


INTENTS = discord.Intents(
        guilds = True,
        members = True,
        message_content = True,
        messages = True,
        reactions = True,
        webhooks = True,
        )


PERMS = discord.permissions.Permissions(
        add_reactions = True,
        read_messages = True,
        send_messages = True,
        send_messages_in_threads = True,
        manage_messages = True,
        embed_links = True,
        attach_files = True,
        use_external_emojis = True,
        manage_webhooks = True,
        read_message_history = True,
        )


# limits for non-Nitro users by boost level
MAX_FILE_SIZE = [
        25*1024*1024,
        25*1024*1024,
        50*1024*1024,
        100*1024*1024]


VALID_MIME_TYPES = [
        'image/jpeg',
        'image/png',
        'image/gif',
        'image/webp',
        ]


ALLOWED_CHANNELS = (
        discord.ChannelType.text,
        discord.ChannelType.private,
        discord.ChannelType.voice,
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
        )


@enum.unique
class ChannelMode(enum.IntEnum):
    default     = 0
    mandatory   = 1


@enum.unique
class Prefs(enum.IntFlag):
#   auto        = 1 << 0
#   replace     = 1 << 1
#   autoswap    = 1 << 2
    errors      = 1 << 3
    delay       = 1 << 4
    homestuck   = 1 << 5


@enum.unique
class ProxyType(enum.IntEnum):
    override    = 0
#   collective  = 1 RIP
    swap        = 2
    pkswap      = 3
    pkreceipt   = 4
    mask        = 5


ProxySymbol = {
        ProxyType.override: '\N{NO ENTRY}',
        ProxyType.swap: '\N{TWISTED RIGHTWARDS ARROWS}',
        ProxyType.pkswap: '\N{FOX FACE}',
        ProxyType.pkreceipt: '\N{RECEIPT}',
        ProxyType.mask: '\N{PERFORMING ARTS}',
}


@enum.unique
class ProxyFlags(enum.IntFlag):
#   auto        = 1 << 0
    keepproxy   = 1 << 1
    echo        = 1 << 2
    autoadd     = 1 << 3
    nomerge     = 1 << 4
    replace     = 1 << 5


@enum.unique
class ProxyState(enum.IntEnum):
    hidden      = 0
    inactive    = 1
    active      = 2


@enum.unique
class ActionType(enum.IntEnum):
    join    = 0
    invite  = 1
    remove  = 2
    server  = 3
    change  = 4
    rules   = 5


@enum.unique
class VoteType(enum.IntEnum):
#   confirm     = 0
    approval    = 1
    consensus   = 2
    create      = 3
    preinvite   = 4
#   swap        = 5
    pkswap      = 6


@enum.unique
class RuleType(enum.IntEnum):
    legacy          = -1
#   custom          = 0 not yet!
    dictator        = 1
    handsoff        = 2
    majority        = 3
#   supermajority   = 4 ?
    unanimous       = 5


BE_REGEX = re.compile(r'\\?> ?Be (.*?)\.?', re.IGNORECASE)
# convert into dict of single opening char : regex matching all ending chars
QUOTE_REGEXES = reduce(dict.__or__, map(
    lambda tup : {
        opening: re.compile('%s([^%s]*)%s(.*)' % (
            opening,
            ''.join(tup[1]),
            ('%s' if len(tup[1]) == 1 else '[%s]') % ''.join(tup[1]),
            )
            ) for opening in tup[0]
        },
    QUOTE_REGEXES))
DEFAULT_PREFS = reduce(lambda a, b : a | Prefs[b], DEFAULT_PREFS, 0)
REPLACEMENTS = [(re.compile(x, re.IGNORECASE), y) for x, y in REPLACEMENTS]
HELPMSGS = {topic: text.format(p = COMMAND_PREFIX) for topic, text
        in HELPMSGS.items()}

# singleton type representing a meaningfully NULL command argument
# it is converted to NULL when passed to sqlite but truthy to python
# (see register_adapter())
CLEAR = type('Clear', (), {})()
class UserError(Exception):
    pass

