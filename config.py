DEFAULT_DB = 'gestalt.db'

PK_ENDPOINT = 'https://api.pluralkit.me/v2'
# see https://pluralkit.me/api/#rate-limiting; 2/s but may change
PK_RATELIMIT = 2
PK_WINDOW = 1.0

DELETE_DELAY = 0.4

REPLACEMENTS = [
        ('\\bam\\b', 'are'),
        ('\\bmyself\\b', 'Ourselves'),
        ('\\bi\\s+was\\b', 'We were'),
        ('\\bi\'m\\b', 'We\'re'),
        ('\\bim\\b', 'We\'re'),
        ('\\bam\\s+i\\b', 'are We'),
        ('\\bi\\b', 'We'), # also corrects I'll, I'd, I've
        ('\\bme\\b', 'Us'),
        ('\\bmy\\b', 'Our'),
        ('\\bmine\\b', 'Ours')
        ]

REACT_QUERY = '\N{BLACK QUESTION MARK ORNAMENT}'
REACT_DELETE = '\N{CROSS MARK}'
# originally 'BALLOT BOX WITH CHECK'
# but this has visibility issues on ultradark theme
REACT_CONFIRM = '\N{WHITE HEAVY CHECK MARK}'
REACT_WAIT = '\N{HOURGLASS}'

SYMBOL_OVERRIDE = '\N{NO ENTRY}'
SYMBOL_COLLECTIVE = '\N{LINK SYMBOL}'
SYMBOL_SWAP = '\N{TWISTED RIGHTWARDS ARROWS}'
SYMBOL_PKSWAP = '\N{FOX FACE}'
SYMBOL_RECEIPT = '\N{RECEIPT}'

# exactly what PK uses
REPLY_SYMBOL = (
        '\N{THREE-PER-EM SPACE}'
        '\N{LEFTWARDS ARROW WITH HOOK}'
        '\N{VARIATION SELECTOR-16}'
        )
REPLY_CUTOFF = '\N{HORIZONTAL ELLIPSIS}'

COMMAND_PREFIX = 'gs;'

DEFAULT_PREFS = ['errors']

BECOME_MAX = 50

SYNC_TIMEOUT = 3600 # in seconds

WEBHOOK_NAME = 'Gestalt webhook'

HELPMSGS = {
        '':
        'Gestalt is a bot focused on social identity play. '
        'You can swap appearances with someone else, '
        'or share a pseudo-account with a group of users.\n'
        '\n'
        'The central feature of Gestalt is the proxy. '
        'In Gestalt, a proxy is a borrowed or shared nickname and avatar. '
        'You can use a proxy by typing customizable tags around your messages. '
        'Every user has access to a different set of proxies. '
        'There are three types:\n'
        '- :twisted_rightwards_arrows:**Swaps:** A Swap is a mutual agreement '
        'to swap nicknames and avatars with another user. Swaps are cosmetic '
        'and do not grant any access to your Discord account. '
        'A Swap is valid in any guild accessible to the other user.\n'
        '- :link:**Collectives:** A Collective is a pseudo-account shared '
        'among users with a given role. '
        'Collectives are limited to a single guild.\n'
        '- :no_entry:**Overrides:** Every user has one Override. '
        'A message with the Override tags will never be touched by Gestalt. '
        'You can think of it as a safeword, or as a way to help Gestalt '
        'cooperate with other proxy bots.\n'
        '\n'
        '**Help Topics**: (view with `{p}help (topic)`)\n'
        '- proxy\n'
        '- swap\n'
        '- pluralkit\n'
        '- collective\n'
        '- prefs\n'
        '- utility\n'
        '- server',

        'proxy':
        '**All proxies**: (shortcut `{p}p`)\n'
        'You can use a proxy via either tags or enabling autoproxy. '
        'While an applicable proxy has autoproxy enabled, all your messages '
        'will be sent via that proxy, unless you use another proxy\'s tags.\n'
        'Because proxies in Gestalt work differently than in other proxy bots, '
        'their behavior may be confusing. For example, you may have one '
        'Collective autoproxy enabled per server, but if you enable autoproxy '
        'on a Swap, then the Collective autoproxies will be disabled because '
        'Collectives are limited to a single server but Swaps are not.\n'
        '\n'
        'See also: `{p}prefs latch`.\n'
        '\n'
        '`{p}proxy`: list your proxies.\n'
        '`{p}proxy (id/name) tags [tags]`: set tags\n'
        '`{p}proxy (id/name) auto (on/off/blank)`: set or toggle autoproxy\n'
        '`{p}proxy (id/name) rename (new name)`: rename proxy\n'
        '`{p}proxy (id/name) keepproxy (on/off/blank)`: set or toggle '
        'keepproxy\n'
        '`{p}become (id/name)`: enables autoproxy, and with every message, '
        'the chance that your message will be proxied increases from 0%\n',

        'swap':
        '**Swaps**: (shortcut `{p}s`)\n'
        'To open a Swap, both users must use the `{p}open` command.\n'
        '`{p}swap open (@user) [optional tags]`: open a Swap\n'
        '`{p}swap close (id/name)`: unilaterally closes the Swap\n',

        'pluralkit':
        '**PluralKit Swaps**: (shortcut `{p}pk`)\n'
        'If you have a system registered with PluralKit, then you may "send" '
        'system members to anyone with whom you have an open Swap.\n'
        'Although a PluralKit swap does not need your Swap partner to agree '
        '(unlike a normal Swap), the proxy will not be usable immediately. '
        'Due to PluralKit API constraints and the fact that members may be '
        'customized per server, PluralKit swaps need to be synced in a server '
        'in order to be used there.\n'
        'When you "send" a member, a receipt will be added to your proxies. '
        'This receipt can be used to close the PluralKit swap without closing '
        'the whole Swap.\n'
        '\n'
        '`{p}pluralkit swap (swap name/id) (5-letter PluralKit ID)`: open a '
        'PluralKit swap.\n'
        '`{p}pluralkit close (receipt name/id)`: close a PluralKit swap.\n'
        '`{p}pluralkit sync`: sync a PluralKit swap by replying to a proxied '
        'message.',

        'collective':
        '**Collectives**: (shortcut `{p}c`)\n'
        'The name and avatar of a Collective may be changed by anyone in the '
        'Colective.\n'
        'You may use your associated proxy ID or name in place of a collective '
        'ID in commands.\n'
        '\n'
        '`{p}collective`: list server Collectives.\n'
        '`{p}collective (collective id) name (name)`: rename Collective\n'
        '`{p}collective (collective id) avatar (link/attacjment)`: set '
        'Collective avatar.\n'
        '`{p}collective new (@role/everyone)`: create a new Collective. '
        'Requires `Manage Roles` permission.\n'
        '`{p}collective (collective id) delete`: requires `Manage Roles`.',

        'prefs':
        '**Preferences**:\n'
        '- `replace`: convert singular pronouns to plural. (default: **off**)\n'
        '- `errors`: if off, Gestalt will silently fail on command errors. '
        '(default: **on**)\n'
        '- `delay`: if on, Gestalt will wait a fraction of a second before '
        'deleting original messages. This may resolve some client issues. '
        '(default: **off**)\n'
        '- `latch`: if on, using a proxy instantly enables autoproxy for it. '
        '(default: **off**)\n'
        '`{p}prefs`: list your current preferences.\n'
        '`{p}prefs [name] [on/off/blank]`: toggle.\n'
        '`{p}prefs defaults`: reset your preferences.',

        'server':
        '**Server Commands**:\n'
        '`{p}permcheck (server id)`: check that Gestalt has the permissions '
        'it needs in each channel\n'
        '`{p}log channel #channel`: set log channel\n'
        '`{p}log disable`: disable log channel\n',

        'utility':
        '**Utilities**:\n'
        '`{p}edit (message)`/`{p}e (message)`: edit your last message or a '
        'replied message\n'
        '`{p}invite`: get Gestalt\'s invite link\n'
        'Reactions:\n'
        ':x: : delete a message you sent. In the case of Swaps, either the '
        'swapper or swappee may delete the message.\n'
        ':question: : query who sent a message. '
        '(If you don\'t receive the DM, DM this bot first.)\n'
        }

# parody of PluralKit/PluralKit.Bot/Commands/Help.cs
EXPLAIN = (
        '> **About Gestalt**\n'
        'Gestalt detects messages enclosed in specific tags associated with a '
        'profile, then replaces that message under a "pseudo-account" of that '
        'profile using Discord webhooks.\n'
        '\n'
        'This is useful for multiple bodies sharing one person (aka. '
        '*hiveminds*), people who wish to role-play as each other without '
        'having to share Discord accounts, or anyone else who may want to be '
        'really weird about identity from the same Discord account.\n'
        '\n'
        'Due to Discord limitations, these messages will show up with the '
        '`[BOT]` tag - however, they are not bots. Unless they are.'
        )

ERROR_DM = 'You need to be in a server to do that!'
ERROR_TAGS = 'Those tags conflict with another proxy.'
ERROR_MANAGE_ROLES = 'You need `Manage Roles` permission to do that!'
ERROR_CURSED = 'No.'
ERROR_BLURSED = 'I\'m flattered, but no.'
ERROR_PKAPI = 'A PluralKit API error occured.'
