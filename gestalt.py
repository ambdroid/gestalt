#!/usr/bin/python3.7

from unicodedata import lookup as emojilookup
import asyncio
import signal
import time
import math

import sqlite3 as sqlite
import discord

import testenv
import auth


REACT_QUERY = emojilookup("BLACK QUESTION MARK ORNAMENT")
REACT_DELETE = emojilookup("CROSS MARK")
# originally "BALLOT BOX WITH CHECK"
# but this has visibility issues on ultradark theme
REACT_CONFIRM = emojilookup("WHITE HEAVY CHECK MARK")

COMMAND_PREFIX = "gs;"
MAX_FILE_SIZE = 8*1024*1024
DEFAULT_PREFIX = "g "

PURGE_AGE = 3600*24*7   # 1 week
PURGE_TIMEOUT = 3600*2  # 2 hours

HELPMSG = ("By default, I will proxy any message that begins "
        "with `g ` or `G `. So `g hello!` will become `hello!`\n"
        "\n"
        "Use `" + COMMAND_PREFIX + "prefix` to set a custom prefix.\n"
        "Examples:\n"
        "`prefix =` lets you proxy with `=hello!`\n"
        "`prefix \"h \"` lets you proxy with `h hello!`\n"
        "`prefix delete` lets you revert your prefix to the defaults.\n"
        "\n"
        "Use `" + COMMAND_PREFIX + "auto` for autoproxy. "
        "While autoproxy is on, I will proxy all your messages *except* those "
        "that are prefixed.\n"
        "Use `auto on` and `auto off` or just `auto` to toggle.\n"
        "\n"
        "Use`" + COMMAND_PREFIX + "nick` to change my nick.\n"
        "\n"
        "React with :x: to delete a message you sent.\n"
        "React with :question: to query who sent a message.\n"
        "(If you don't receive the DM, DM me first.)\n"
        "\n"
        "One last thing: if you upload a photo and put `spoiler` "
        "somewhere in the message body, I'll spoiler it for you.\n"
        "This is useful if you're on mobile.")

AUTO_KEYWORDS = {
        "on": 1,
        "off": 0,
        "yes": 1,
        "no": 0
}



def begins(text, prefix):
    return len(prefix) if text[:len(prefix)] == prefix else 0


def is_text(message):
    return message.channel.type == discord.ChannelType.text


def is_dm(message):
    return message.channel.type == discord.ChannelType.private


def is_admin(message):
    return message.author.permissions_in(message.channel).administrator


class Gestalt(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loop.create_task(self.purge_loop())


    async def purge_loop(self):
        # this is purely a db task, no need to wait until ready
        while True:
            # time.time() and PURGE_AGE in seconds, snowflake timestamp in ms
            # https://discord.com/developers/docs/reference#snowflakes
            maxid = math.floor(1000*(time.time()-PURGE_AGE)-1420070400000)<<22
            cur.execute("delete from history where deleted = 1 and msgid < ?",
                    (maxid,))
            conn.commit()
            await asyncio.sleep(PURGE_TIMEOUT)


    def handler(self):
        self.loop.create_task(self.close())
        conn.commit()


    async def on_ready(self):
        print('Logged in as %s, id %d!' % (self.user, self.user.id),
                flush = True)
        self.loop.add_signal_handler(signal.SIGINT, self.handler)
        self.loop.add_signal_handler(signal.SIGTERM, self.handler)
        await self.change_presence(status = discord.Status.online,
                activity = discord.Game(name = COMMAND_PREFIX + "help"))


    async def do_command(self, message, cmd):
        # add an empty string to take place of arg if none given
        arg = (cmd.split(maxsplit=1)+[""])[1]
        if begins(cmd, "help"):
            msgid = (await message.channel.send(HELPMSG)).id
            if is_text(message):
                # put this in the history db so it can be deleted if desired
                cur.execute("insert into history values (?, 0, ?, '', '', 0)",
                        (msgid, message.author.id))

        elif begins(cmd, "prefix") and arg not in ["", '""']:
            if arg[0] == '"' and arg[-1] == '"':
                arg = arg[1:-1]

            if arg == "delete":
                arg = None

            cur.execute("update users set prefix = ? where userid = ?",
                    (arg, message.author.id))

            conn.commit()
            await message.add_reaction(REACT_CONFIRM)

        elif begins(cmd, "auto"):
            userid = message.author.id
            if arg == "":
                cur.execute("update users set auto = (1-auto) where userid = ?",
                        (userid,))
            else:
                if arg not in AUTO_KEYWORDS:
                    return
                cur.execute("update users set auto = ? where userid = ?",
                        (AUTO_KEYWORDS[arg], userid))
            conn.commit()
            await message.add_reaction(REACT_CONFIRM)

        elif begins(cmd, "nick"):
            if is_dm(message):
                await message.author.send("I can only do that in a server!");
                return
            try:
                await message.guild.get_member(self.user.id).edit(nick = arg)
                await message.add_reaction(REACT_CONFIRM)
            except:
                pass


    async def on_message(self, message):
        if ((message.author.bot and not message.author.id in testenv.BOTS)
                or message.is_system()):
            return

        cur.execute("insert or ignore into users values (?, NULL, 0)",
                (message.author.id,))

        # end of prefix or 0
        offset = begins(message.content.lower(), COMMAND_PREFIX)
        # command prefix is optional in DMs
        if offset != 0 or is_dm(message):
            await self.do_command(message, message.content[offset:])
            return

        # guaranteed to exist due to above
        row = cur.execute("select prefix, auto from users where userid = ?",
                (message.author.id,)).fetchone()

        # if no prefix set, use default
        prefix = DEFAULT_PREFIX if row[0] == None else row[0]
        offset = begins(message.content.lower(), prefix)

        # don't proxy if:
        # - auto off and not prefixed
        # - auto on and prefixed
        auto = row[1]
        if not (offset == 0) == (auto == 0):
            proxy = message.content[offset:].strip()
            msgfile = None
            if (len(message.attachments) > 0
                    and message.attachments[0].size <= MAX_FILE_SIZE):
                # only copy the first attachment
                msgfile = await message.attachments[0].to_file()
                # lets mobile users upload with spoilers
                if proxy.lower().find("spoiler") != -1:
                    msgfile.filename = "SPOILER_" + msgfile.filename

            msgid = (await message.channel.send(content = proxy,
                file = msgfile)).id
            authid = message.author.id
            chanid = message.channel.id
            authname = message.author.name + "#" + message.author.discriminator
            cur.execute("insert into history values (?, ?, ?, ?, ?, 0)",
                    (msgid, chanid, authid, authname, proxy)) # deleted = 0
            await message.delete()


    # on_reaction_add doesn't catch everything
    async def on_raw_reaction_add(self, payload):
        # first, make sure this is one of ours
        row = cur.execute(
            "select authname, authid from history where msgid = ?",
            (payload.message_id,)).fetchone()
        if row == None:
            return

        # we can't fetch the message directly
        # so fetch the channel first, and use *that* to fetch the message.
        channel = self.get_channel(payload.channel_id)
        reactor = self.get_user(payload.user_id)
        message = await channel.fetch_message(payload.message_id)

        if message.author.id != self.user.id:
            raise ValueError("Something is terribly wrong.")

        emoji = payload.emoji.name
        if emoji == REACT_QUERY:
            sendto = channel if reactor.id in testenv.BOTS else reactor
            try:
                await sendto.send("Message sent by %s, id %d" % row)
            except discord.Forbidden:
                pass
            await message.remove_reaction(payload.emoji.name, reactor)

        elif emoji == REACT_DELETE:
            # only sender may delete proxied message
            if payload.user_id == row[1]:
                cur.execute(
                        "update history set deleted = 1,"
                        "authname = '' where msgid = ?",
                        (payload.message_id,))
                await message.delete()
            else:
                await message.remove_reaction(payload.emoji.name, reactor)



instance = Gestalt()

conn = sqlite.connect("gestalt.db")
cur = conn.cursor()
cur.execute(
	"create table if not exists history("
        "msgid integer primary key,"
        "chanid integer,"
        "authid integer,"
        "authname text,"
        "content text,"
        "deleted integer)")
cur.execute(
	"create table if not exists users("
        "userid integer primary key,"
        "prefix text,"
        "auto integer)")
cur.execute("pragma secure_delete")

try:
    instance.run(auth.token)
except RuntimeError:
    print("Runtime error.")

print("Shutting down.")
conn.commit()
conn.close()
