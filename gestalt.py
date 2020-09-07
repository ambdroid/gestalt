#!/usr/bin/python3.7

from unicodedata import lookup as emojilookup
import asyncio
import signal
import enum
import time
import math
import sys
import re

import sqlite3 as sqlite
import aiohttp
import discord

import auth


WEBHOOK_NAME = "Gestalt webhook"

REPLACE_DICT = {re.compile(x, re.IGNORECASE): y for x, y in {
    "\\bi\\s+am\\b": "We are",
    "\\bi\\s+was\\b": "We were",
    "\\bi'm\\b": "We're",
    "\\bim\\b": "We're",
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
DEFAULT_PREFIX = "g "

PURGE_AGE = 3600*24*7   # 1 week
PURGE_TIMEOUT = 3600*2  # 2 hours

MAX_FILE_SIZE = 8*1024*1024
AVATAR_SIZE = 256

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
        "Use `swap [user]` to initiate a Swap. "
        "If the other user consents with `swap [you]`, "
        "then the Swap will be active.\n"
        "While a Swap is active, the normal proxying behavior will be replaced "
        "by a webhook with the other user's nickname and avatar.\n"
        "Use `swap off` to deactivate a Swap.\n"
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
ERROR_DM = "I can only do that in a server!"

KEYWORDS = {
        "on": 1,
        "off": 0,
        "yes": 1,
        "no": 0
}

@enum.unique
class Prefs(enum.IntFlag):
    auto        = 1 << 0
    replace     = 1 << 1
    autoswap    = 1 << 2
DEFAULT_PREFS = Prefs.replace



def begins(text, prefix):
    return len(prefix) if text.startswith(prefix) else 0


def is_text(message):
    return message.channel.type == discord.ChannelType.text


def is_dm(message):
    return message.channel.type == discord.ChannelType.private


class Gestalt(discord.Client):
    def __init__(self, *, dbfile, **kwargs):
        super().__init__(**kwargs)

        self.conn = sqlite.connect(dbfile)
        self.cur = self.conn.cursor()
        self.cur.execute(
                "create table if not exists history("
                "msgid integer primary key,"
                "chanid integer,"
                "authid integer,"
                "authname text,"
                "otherid text,"
                "content text,"
                "deleted integer)")
        self.cur.execute(
                "create table if not exists users("
                "userid integer primary key,"
                "prefix text,"
                "prefs integer)")
        self.cur.execute(
                "create table if not exists webhooks("
                "chanid integer primary key,"
                "hookid integer,"
                "token text)")
        self.cur.execute(
                "create table if not exists swaps("
                "userid integer,"  # initiator
                "otherid integer," # target
                "active integer,"
                "unique(userid, otherid))")
        self.cur.execute("pragma secure_delete")

        self.loop.create_task(self.purge_loop())


    def __del__(self):
        print("Closing database.")
        self.conn.commit()
        self.conn.close()


    async def purge_loop(self):
        # this is purely a db task, no need to wait until ready
        while True:
            # time.time() and PURGE_AGE in seconds, snowflake timestamp in ms
            # https://discord.com/developers/docs/reference#snowflakes
            maxid = math.floor(1000*(time.time()-PURGE_AGE)-1420070400000)<<22
            self.cur.execute(
                    "delete from history where deleted = 1 and msgid < ?",
                    (maxid,))
            self.conn.commit()
            await asyncio.sleep(PURGE_TIMEOUT)


    def handler(self):
        self.loop.create_task(self.close())
        self.conn.commit()


    async def on_ready(self):
        print('Logged in as %s, id %d!' % (self.user, self.user.id),
                flush = True)
        self.sesh = aiohttp.ClientSession()
        self.loop.add_signal_handler(signal.SIGINT, self.handler)
        self.loop.add_signal_handler(signal.SIGTERM, self.handler)
        await self.change_presence(status = discord.Status.online,
                activity = discord.Game(name = COMMAND_PREFIX + "help"))


    async def close(self):
        await super().close()
        await self.sesh.close()


    async def send_embed(self, replyto, text):
        msgid = (await replyto.channel.send(
            embed = discord.Embed(description = text))).id
        if is_text(replyto):
            self.cur.execute(
                    "insert into history values (?, 0, ?, '', 0, '', 0)",
                    (msgid, replyto.author.id))


    # discord.py commands extension throws out bot messages
    # this is incompatible with the test framework so process commands manually
    async def do_command(self, message, cmd):
        # add an empty string to take place of arg if none given
        arg = (cmd.split(maxsplit=1)+[""])[1]
        if begins(cmd, "help"):
            await self.send_embed(message, HELPMSG)

        elif begins(cmd, "prefix") and arg not in ["", '""']:
            if arg[0] == '"' and arg[-1] == '"':
                arg = arg[1:-1]

            if arg == "delete":
                arg = None

            self.cur.execute("update users set prefix = ? where userid = ?",
                    (arg, message.author.id))

            await message.add_reaction(REACT_CONFIRM)

        elif begins(cmd, "auto"):
            await self.do_command(message, "prefs " + cmd)

        elif begins(cmd, "prefs"):
            arg = arg.split() # no spaces to worry about like in prefix command
            userid = message.author.id
            if len(arg) == 0:
                # must exist due to on_message
                userprefs = self.cur.execute(
                        "select prefs from users where userid = ?",
                        (userid,)).fetchone()[0]
                text = "\n".join(["%s: **%s**" %
                        (pref.name, "on" if userprefs & pref else "off")
                        for pref in Prefs])
                await self.send_embed(message, text)
                return

            if arg[0] in ["default", "defaults"]:
                self.cur.execute(
                        "update users set prefs = ? where userid = ?",
                        (DEFAULT_PREFS, userid))
                await message.add_reaction(REACT_CONFIRM)
                return

            if not arg[0] in Prefs.__members__.keys():
                return

            bit = int(Prefs[arg[0]])
            if len(arg) == 1: # only "prefs" + name given. invert the thing
                self.cur.execute(
                        "update users set prefs = (prefs & ~?) | (~prefs & ?)"
                        "where userid = ?",
                        (bit, bit, userid))
            else:
                if arg[1] not in KEYWORDS:
                    return
                # note that KEYWORDS values are 0/1
                self.cur.execute(
                        "update users set prefs = (prefs & ~?) | ?"
                        "where userid = ?",
                        (bit, bit*KEYWORDS[arg[1]], userid))

            await message.add_reaction(REACT_CONFIRM)

        elif begins(cmd, "nick"):
            if is_dm(message):
                await message.author.send(ERROR_DM);
                return

            try:
                await message.guild.get_member(self.user.id).edit(nick = arg)
            except: # nickname too long or otherwise invalid
                return

            await message.add_reaction(REACT_CONFIRM)

        elif begins(cmd, "swap"):
            if is_dm(message):
                await message.author.send(ERROR_DM)
                return

            authid = message.author.id
            if arg == "off":
                self.cur.execute(
                        "delete from swaps where userid = ? or otherid = ?",
                        (authid, authid))
                await message.add_reaction(REACT_CONFIRM)
                return


            # discord.ext includes a MemberConverter but that needs a Context
            # and that's only available whem using discord.ext Command
            member = (message.mentions[0] if len(message.mentions) > 0 else
                    message.channel.guild.get_member_named(arg))
            if member == None:
                return

            # this whole thing *feels* like a race condition waiting to happen
            # but there's no await, so there's no opportunity for interference

            # to minimize queries, first try to mark the swap active in the
            # other direction. if it succeeds, the swap is active.
            self.cur.execute(
                    "update swaps set active = 1 where "
                    "userid = ? and otherid = ?",
                    (member.id, authid))
            if self.cur.rowcount == 1: # *must* be 0/1 due to unique constraint
                # deactivate any other active swaps first
                # (except for the one we want!)
                self.cur.execute(
                        "delete from swaps where"
                        "(userid in (?, ?) or otherid in (?, ?))"
                        "and not (userid = ? and otherid = ?)",
                        (member.id, authid)*3)
                self.cur.execute("insert or ignore into swaps values (?, ?, 1)",
                        (authid, member.id))

            else:
                userprefs = self.cur.execute(
                        "select prefs from users where userid = ?",
                        (member.id,)).fetchone()
                # it's possible that the other user isn't in the users table
                # due to not having sent a message yet
                active = userprefs != None and userprefs[0] & Prefs.autoswap
                self.cur.execute(
                        "insert or ignore into swaps values (?, ?, ?)",
                        (authid, member.id, bool(active)))

            await message.add_reaction(REACT_CONFIRM)


    async def do_proxy(self, message, proxy, prefs):
        msgfile = None
        if (len(message.attachments) > 0
                and message.attachments[0].size <= MAX_FILE_SIZE):
            # only copy the first attachment
            msgfile = await message.attachments[0].to_file()
            # lets mobile users upload with spoilers
            if proxy.lower().find("spoiler") != -1:
                msgfile.filename = "SPOILER_" + msgfile.filename

        authid = message.author.id
        channel = message.channel

        '''row = self.cur.execute(
                "select x.otherid from"
                "       (select otherid from swaps where userid = ?) x"
                "   join"
                "       (select userid from swaps where otherid = ?)"
                "   on"
                "       (x.otherid = y.userid)"
                "limit 1",
                (authid, authid)).fetchone()'''
        row = self.cur.execute(
                "select * from swaps where userid = ? and active = 1",
                (authid,)).fetchone()
        member = None
        if row != None:
            otherid = row[1]
            member = channel.guild.get_member(otherid)
            # if the guild is large, the member may not be in cache
            if member == None and channel.guild.large:
                member = await channel.guild.fetch_member(otherid)
                # put this member in the cache (is this necessary?)
                if member != None:
                    channel.guild._add_member(member)

        if member == None:
            if prefs & Prefs.replace:
                for x, y in REPLACE_DICT.items():
                    proxy = x.sub(y, proxy)

            msgid = (await channel.send(content = proxy, file = msgfile)).id
        else:
            row = self.cur.execute("select * from webhooks where chanid = ?",
                    (channel.id,)).fetchone()
            if row == None:
                hook = await channel.create_webhook(name = WEBHOOK_NAME)
                self.cur.execute("insert into webhooks values (?, ?, ?)",
                        (channel.id, hook.id, hook.token))
            else:
                hook = discord.Webhook.partial(row[1], row[2],
                        adapter = discord.AsyncWebhookAdapter(self.sesh))

            msgid = (await hook.send(wait = True, content = proxy, file=msgfile,
                    username = member.display_name,
                    avatar_url = member.avatar_url_as(
                        format = "png", size = AVATAR_SIZE))).id

        authname = message.author.name + "#" + message.author.discriminator
        otherid = 0 if member == None else member.id

        # deleted = 0
        self.cur.execute("insert into history values (?, ?, ?, ?, ?, ?, 0)",
                (msgid, channel.id, authid, authname, otherid, proxy))
        await message.delete()


    async def on_message(self, message):
        if message.type != discord.MessageType.default:
            return
        if message.author.bot and not TESTING:
            return

        # user id, no prefix, autoproxy off
        self.cur.execute("insert or ignore into users values (?, NULL, ?)",
                (message.author.id, DEFAULT_PREFS))

        # end of prefix or 0
        offset = begins(message.content.lower(), COMMAND_PREFIX)
        # command prefix is optional in DMs
        if offset != 0 or is_dm(message):
            await self.do_command(message, message.content[offset:].strip())
            return

        # guaranteed to exist due to above
        row = self.cur.execute(
                "select prefix, prefs from users where userid = ?",
                (message.author.id,)).fetchone()

        # if no prefix set, use default
        prefix = DEFAULT_PREFIX if row[0] == None else row[0]
        offset = begins(message.content.lower(), prefix)

        # don't proxy if:
        # - auto off and not prefixed
        # - auto on and prefixed
        auto = row[1] & Prefs.auto
        if not (offset == 0) == (auto == 0):
            proxy = message.content[offset:].strip()
            await self.do_proxy(message, proxy, row[1])


    # on_reaction_add doesn't catch everything
    async def on_raw_reaction_add(self, payload):
        # first, make sure this is one of ours
        row = self.cur.execute(
            "select authname, authid from history where msgid = ?",
            (payload.message_id,)).fetchone()
        if row == None:
            return

        # we can't fetch the message directly
        # so fetch the channel first, and use *that* to fetch the message.
        channel = self.get_channel(payload.channel_id)
        reactor = self.get_user(payload.user_id)
        message = await channel.fetch_message(payload.message_id)

        emoji = payload.emoji.name
        if emoji == REACT_QUERY:
            try:
                # tragically, bots cannot DM other bots :(
                sendto = channel if TESTING else reactor
                await sendto.send("Message sent by %s, id %d" % row)
            except discord.Forbidden:
                pass
            await message.remove_reaction(payload.emoji.name, reactor)

        elif emoji == REACT_DELETE:
            # only sender may delete proxied message
            if payload.user_id == row[1]:
                self.cur.execute(
                        "update history set deleted = 1,"
                        "authname = '' where msgid = ?",
                        (payload.message_id,))
                await message.delete()
            else:
                await message.remove_reaction(payload.emoji.name, reactor)



if __name__ == "__main__":
    TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
    if TESTING:
        print("Running in test mode!")

    instance = Gestalt(dbfile = ":memory:" if TESTING else (
            sys.argv[1] if len(sys.argv) > 1 else "gestalt.db"))

    try:
        instance.run(auth.token)
    except RuntimeError:
        print("Runtime error.")

    print("Shutting down.")
