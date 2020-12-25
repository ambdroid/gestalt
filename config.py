LOG_MESSAGE_CONTENT = True

REPLACEMENTS = [
        ("\\bi\\s+am\\b", "We are"),
        ("\\bi\\s+was\\b", "We were"),
        ("\\bi'm\\b", "We're"),
        ("\\bim\\b", "We're"),
        ("\\bam\\s+i\\b", "are We"),
        ("\\bi\\b", "We"), # also corrects I'll, I'd, I've
        ("\\bme\\b", "Us"),
        ("\\bmy\\b", "Our"),
        ("\\bmine\\b", "Ours")
        ]

REACT_QUERY = "\N{BLACK QUESTION MARK ORNAMENT}"
REACT_DELETE = "\N{CROSS MARK}"
# originally "BALLOT BOX WITH CHECK"
# but this has visibility issues on ultradark theme
REACT_CONFIRM = "\N{WHITE HEAVY CHECK MARK}"

SYMBOL_OVERRIDE = "\N{NO ENTRY}"
SYMBOL_COLLECTIVE = "\N{LINK SYMBOL}"
SYMBOL_SWAP = "\N{TWISTED RIGHTWARDS ARROWS}"

COMMAND_PREFIX = "gs;"

DEFAULT_PREFS = ["replace", "errors"]

PURGE_AGE = 3600*24*7   # 1 week
PURGE_TIMEOUT = 3600*2  # 2 hours

WEBHOOK_NAME = "Gestalt webhook"

HELPMSG = (
        "Gestalt is a bot focused on social identity play. "
        "You can swap appearances with someone else, "
        "or share a pseudo-account with a group of users.\n"
        "\n"
        "Proxies are the central feature of Gestalt. "
        "A proxy is used by typing a customizable prefix at the beginning of "
        "your messages. "
        "Each user has access to a different set of proxies. "
        "There are three types:\n"
        "- :twisted_rightwards_arrows:**Swaps:** A Swap is a mutual agreement "
        "to swap nicknames and avatars with another user. Swaps are purely "
        "cosmetic and do not grant any access to your Discord account. "
        "A Swap is valid in any guild accessible to the other user.\n"
        "- :bee:**Collectives:** A Collective is a pseudo-account shared among "
        "users with a given role. Collectives are limited to a single guild.\n"
        "- :no_entry:**Overrides:** Each user has access to one Override. "
        "A message with the Override prefix will never be touched by Gestalt.\n"
        "\n"
        "\n"
        "**All proxies**: (shortcut `{p}p`)\n"
        "`{p}proxy`: list your proxies.\n"
        "`{p}proxy (id) prefix (prefix/\"prefix\")`: set prefix\n"
        "`{p}proxy (id) auto (on/off/blank)`: set autoproxy\n"
        "\n"
        "**Swaps**: (shortcut `{p}s`)\n"
        "`{p}swap open (@user) (prefix)`: open a Swap\n"
        "`{p}swap close (proxy id/prefix)`: unilaterally closes the Swap\n"
        "\n"
        "**Collectives**: (shortcut `{p}c`)\n"
        "`{p}collective`: list guild Collectives.\n"
        "`{p}collective new (@role/everyone)`: create a new Collective. "
        "Requires **Manage Roles**.\n"
        "`{p}collective (id) name (name)`\n"
        "`{p}collective (id) avatar (url/attachment)`\n"
        "`{p}collective (id) delete`: requires **Manage Roles**.\n"
        "\n"
        "**Preferences**:\n"
        "- `replace`: convert singular pronouns to plural. (default: **on**)\n"
        "- `errors`: if off, Gestalt will silently fail on command errors. "
        "(default: **on**)\n"
        "`{p}prefs`: list your current preferences.\n"
        "`{p}prefs [name] [on/off/blank]`: toggle.\n"
        "`{p}prefs defaults`: reset your preferences.\n"
        "\n"
        "**Other commands**:\n"
        "`{p}help`: this message!\n"
        "`{p}permcheck (server id)`: check bot permissions\n"
        "\n"
        "**Reactions**:\n"
        ":x: : delete a message you sent.\n"
        ":question: : query who sent a message. "
        "(If you don't receive the DM, DM this bot first.)\n"
        )
ERROR_DM = "You need to be in a server to do that!"
ERROR_MANAGE_ROLES = "You need `Manage Roles` permission to do that!"
ERROR_CURSED = "No."
ERROR_BLURSED = "I'm flattered, but no."
