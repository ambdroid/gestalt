from unicodedata import lookup as emojilookup
import re


LOG_MESSAGE_CONTENT = True

REPLACE_DICT = {re.compile(x, re.IGNORECASE): y for x, y in {
    "\\bi\\s+am\\b": "We are",
    "\\bi\\s+was\\b": "We were",
    "\\bi'm\\b": "We're",
    "\\bim\\b": "We're",
    "\\bam\\s+i\\b": "are We",
    "\\bi\\b": "We", # also corrects I'll, I'd, I've
    "\\bme\\b": "Us",
    "\\bmy\\b": "Our",
    "\\bmine\\b": "Ours",
    }.items()}

REACT_QUERY = emojilookup("BLACK QUESTION MARK ORNAMENT")
REACT_DELETE = emojilookup("CROSS MARK")
# originally "BALLOT BOX WITH CHECK"
# but this has visibility issues on ultradark theme
REACT_CONFIRM = emojilookup("WHITE HEAVY CHECK MARK")

COMMAND_PREFIX = "gs;"

DEFAULT_PREFS = ["replace", "errors"]

PURGE_AGE = 3600*24*7   # 1 week
PURGE_TIMEOUT = 3600*2  # 2 hours

WEBHOOK_NAME = "Gestalt webhook"

HELPMSG = ("`{p}prefix`: **set a custom prefix**\n"
        "The default prefix is  `g ` or `G `. "
        "So `g hello!` will become `hello!`\n"
        "Examples:\n"
        "`{p}prefix =`: proxy with `=hello!`\n"
        "`{p}prefix \"h \"`: proxy with `h hello!`\n"
        "`{p}prefix delete`: revert your prefix to the default\n"
        "\n"
        "`{p}prefs`: **user preferences**\n"
        "- `auto`: proxy all your messages *except* those that are prefixed. "
        "(shortcut: `{p}auto`)\n"
        "- `replace`: convert singular pronouns to plural. (default: **on**)\n"
        "- `autoswap`: automatically accept any Swap. (see below)\n"
        "Use `{p}prefs [name] [on/off]`, or `{p}prefs [name]` to toggle.\n"
        "Use `{p}prefs` by itself to list your current preferences.\n"
        "Use `{p}prefs defaults` to reset your preferences.\n"
        "\n"
        "`{p}swap [user]`: **initiate or consent to a Swap**\n"
        "If the other user consents with `{p}swap [you]`, "
        "then the Swap will be active.\n"
        "While a Swap is active, the normal proxying behavior will be replaced "
        "by a webhook with the other user's nickname and avatar.\n"
        "Use `{p}swap off` to deactivate a Swap.\n"
        "\n"
        "`{p}nick`: **change this bot's nick**\n"
        "This command is open to all users.\n"
        "\n"
        "**Reactions:**\n"
        ":x: : delete a message you sent.\n"
        ":question: : query who sent a message. "
        "(If you don't receive the DM, DM this bot first.)\n"
        "\n"
        "One last thing: if you upload a photo and put `spoiler` "
        "somewhere in the message body, this bot will spoiler it for you. "
        "This is useful if you're on mobile.").format(p = COMMAND_PREFIX)
ERROR_DM = "You need to be in a server to do that!"
ERROR_MANAGE_ROLES = "You need `Manage Roles` permission to do that!"
