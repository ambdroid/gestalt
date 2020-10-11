#!/usr/bin/python3.7

from unicodedata import lookup as emojilookup
import asyncio
import random
import signal
import string
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
# DEFAULT_PREFIX = "g "

PURGE_AGE = 3600*24*7   # 1 week
PURGE_TIMEOUT = 3600*2  # 2 hours

# hard limit for non-Nitro users
# TODO: honor increased limits in boosted servers
MAX_FILE_SIZE = 8*1024*1024

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

KEYWORDS = {
        "on": 1,
        "off": 0,
        "yes": 1,
        "no": 0
}

@enum.unique
class Prefs(enum.IntFlag):
#   auto        = 1 << 0
    replace     = 1 << 1
#   autoswap    = 1 << 2
    errors      = 1 << 3
DEFAULT_PREFS = Prefs.replace | Prefs.errors



class CommandReader:
    def __init__(self, msg, cmd):
        self.msg = msg
        self.cmd = cmd

    def read_word(self):
        # add empty strings to pad array if string empty or no split
        split = self.cmd.split(maxsplit = 1) + ["",""]
        self.cmd = split[1]
        return split[0]

    def read_quote(self):
        match = re.match('\\"[^\\"]*\\"', self.cmd)
        if match == None:
            return self.read_word()
        self.cmd = match.string[len(match[0]):].strip()
        return match[0][1:-1]

    def read_quote_reverse(self):
        self.cmd = self.cmd[::-1]
        ret = self.read_quote()[::-1]
        self.cmd = self.cmd[::-1]
        return ret

    def read_remainder(self):
        ret = self.cmd
        if len(ret) > 1 and ret[1] == ret[-1] == '"':
            ret = ret[1:-1]
        self.cmd = ""
        return ret


class ProxyControl:
    def __init__(self, cursor):
        self.cur = cursor

    def from_row(self, row):
        return Proxy(row, self.cur)

    def by_id(self, proxid):
        row = self.cur.execute("select * from proxies where proxid = ?",
                (proxid,)).fetchone()
        return None if row == None else self.from_row(row)


class Proxy:
    @enum.unique
    class type(enum.IntEnum):
        override    = 0
        collective  = 1
        swap        = 2

    def __init__(self, row, cursor):
        self.cur = cursor
        (self.proxid, self.userid, self.guildid, self.prefix, self.type,
                self.extraid, self.auto, self.active) = row

    def set_prefix(self, prefix):
        self.prefix = prefix
        self.cur.execute("update proxies set prefix = ? where proxid = ?",
                (prefix, self.proxid))

    def set_auto(self, auto):
        self.auto = auto
        self.cur.execute("update proxies set auto = ? where proxid = ?",
                (auto, self.proxid))

    def set_active(self, active):
        self.active = active
        self.cur.execute("update proxies set active = ? where proxid = ?",
                (active, self.proxid))


def is_text(message):
    return message.channel.type == discord.ChannelType.text


def is_dm(message):
    return message.channel.type == discord.ChannelType.private


class Gestalt(discord.Client):
    def __init__(self, *, dbfile, purge = True, **kwargs):
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
                "prefs integer)")
        self.cur.execute(
                "create table if not exists webhooks("
                "chanid integer primary key,"
                "hookid integer,"
                "token text)")
        self.cur.execute(
                "create table if not exists proxies("
                "proxid text primary key,"  # of form 'abcde'
                "userid integer,"
                "guildid integer,"          # 0 for swaps, overrides
                "prefix text,"
                "type integer,"             # see enum Proxy.type
                "extraid integer,"          # userid or roleid or NULL
                "auto integer,"             # 0/1
                "active integer,"           # 0/1
                "unique(userid, extraid))") # note that NULL bypasses unique
        self.cur.execute(
                # NB: this does not trigger if a proxy is inserted with auto = 1
                # including "insert or replace"
                "create trigger if not exists exclusive_auto "
                "after update of auto on proxies when (new.auto = 1) "
                "begin "
                    "update proxies set auto = 0 where ("
                        "(userid = new.userid) and (proxid != new.proxid) and ("
                            # if updated proxy is global, remove all other auto
                                "(new.guildid == 0)"
                            "or"
                            # else, remove auto from global and same guild
                                "((new.guildid != 0) and "
                                "(guildid in (0, new.guildid)))"
                        ")"
                    ");"
                "end")
        self.cur.execute(
                "create table if not exists collectives("
                "collid text primary key,"
                "guildid integer,"
                "roleid integer,"
                "nick text,"
                "avatar text,"
                "unique(guildid, roleid))")
        self.cur.execute("pragma secure_delete")

        self.proxctl = ProxyControl(self.cur)

        if purge:
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


    # close on SIGINT, SIGTERM
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
        msg = await replyto.channel.send(
                embed = discord.Embed(description = text))
        await msg.add_reaction(REACT_DELETE)
        # insert into history to allow initiator to delete message if desired
        if is_text(replyto):
            self.cur.execute(
                    "insert into history values (?, 0, ?, '', 0, '', 0)",
                    (msg.id, replyto.author.id))


    def gen_id(self):
        while True:
            # this bit copied from PluralKit, Apache 2.0 license
            id = "".join(random.choices(string.ascii_lowercase, k=5))
            # IDs don't need to be globally unique but it can't hurt
            proxies = self.cur.execute(
                    "select proxid from proxies where proxid = ?",
                    (id,)).fetchall()
            collectives = self.cur.execute(
                    "select collid from collectives where collid = ?",
                    (id,)).fetchall()
            if len(proxies) == len(collectives) == 0:
                return id


    def check_prefix_collisions(self, userid, guildid, prefix, existing = None):
        match = (self.cur.execute(
                "select proxid from proxies where ("
                    "(userid = ?)"
                    "and ("
                        # if prefix is to be global, check everything
                        # if not, check only the same guild
                            "(? == 0)"
                        "or"
                            "((? != 0) and (guildid in (0, ?)))"
                    ") and ("
                            "(substr(?,0,length(prefix)+1) == prefix)"
                        "or"
                            "(substr(prefix,0,length(?)+1) == ?)"
                    ")"
                ")",
                (userid,)+(guildid,)*3+(prefix,)*3)
                .fetchall())
        if len(match) == 1 and match[0][0] == existing:
            return True
        return len(match) == 0


    # called on role delete
    def sync_role(self, guild, roleid):
        role = guild.get_role(roleid)
        # check if it's deleted and act accordingly
        if role == None:
            self.cur.execute("delete from collectives where roleid = ?",
                    (roleid,))
            self.cur.execute(
                    "delete from proxies"
                    "where (type, extraid) = (?, ?)",
                    (Proxy.type.collective, roleid))
            return

        userids = [x.id for x in role.members]

        # is there anyone without the proxy who should?
        for userid in userids: 
            self.cur.execute(
                    "insert or ignore into proxies values "
                    "(?, ?, ?, NULL, ?, ?, 0, 0)",
                    (self.gen_id(), userid, guild.id,
                        Proxy.type.collective, roleid))

        # is there anyone with the proxy who shouldn't?
        rows = self.cur.execute(
                "select proxid, userid from proxies "
                "where extraid = ?",
                (roleid,)).fetchall()

        for row in rows:
            if row[1] not in userids:
                self.cur.execute("delete from proxies where proxid = ?",
                        (row[0],))


    def sync_member(self, guild, member):
        # first, check if they have any proxies they shouldn't
        # right now, this only applies to collectives
        rows = self.cur.execute(
                "select proxid, extraid from proxies "
                "where (userid, guildid, type) = (?, ?, ?)",
                (member.id, guild.id, Proxy.type.collective)).fetchall()
        roleids = [x.id for x in member.roles]
        for row in rows:
            if row[1] not in roleids:
                self.cur.execute("delete from proxies where proxid = ?",
                        (row[0],))

        # now check if they don't have any proxies they should
        for role in member.roles:
            coll = self.cur.execute(
                    "select * from collectives where roleid = ?",
                    (role.id,)).fetchone()
            if coll != None:
                self.cur.execute(
                    "insert or ignore into proxies values "
                    "(?, ?, ?, NULL, ?, ?, 0, 0)",
                    (self.gen_id(), member.id, guild.id,
                        Proxy.type.collective, role.id))


    async def on_guild_role_update(self, before, after):
        self.sync_role(after.guild, after)


    async def on_member_update(self, before, after):
        self.sync_member(after.guild, after)


    # discord.py commands extension throws out bot messages
    # this is incompatible with the test framework so process commands manually
    async def do_command(self, message, cmd):
        reader = CommandReader(message, cmd)
        arg = reader.read_word().lower()
        authid = message.author.id

        if arg == "debug":
            for table in ["proxies", "collectives"]:
                await self.send_embed(message, "```%s```" % "\n".join(
                    ["|".join([str(i) for i in x]) for x in self.cur.execute(
                        "select * from %s" % table).fetchall()]))
            return

        if arg == "help":
            await self.send_embed(message, HELPMSG)

        elif arg in ["proxy", "p"]:
            proxid = reader.read_word().lower()
            arg = reader.read_word().lower()

            if proxid == "":
                rows = self.cur.execute(
                        "select * from proxies where userid = ?"
                        "order by type asc",
                        (authid,)).fetchall()
                text = "\n".join(["`%s`%s: prefix `%s` auto **%s**" %
                        (row[0], # proxy id
                            # if attached to a guild, add " in (guild)"
                            ((" in " + self.get_guild(row[2]).name) if row[2]
                                else ""),
                            row[3], #prefix
                            "on" if row[6] else "off") # auto
                        for row in rows])
                return await self.send_embed(message, text)

            proxy = self.proxctl.by_id(proxid)
            if proxy == None or proxy.userid != authid:
                raise RuntimeError("You do not have a proxy with that ID.")

            if arg == "prefix":
                arg = reader.read_quote().lower()
                if arg.replace("text","") == "":
                    raise RuntimeError("Please provide a valid prefix.")

                arg = arg.lower()
                # adapt PluralKit [text] prefix/postfix format
                if arg.endswith("text"):
                    arg = arg[:-4]

                # check if the new prefix conflicts with anything else
                if not self.check_prefix_collisions(authid, proxy.guildid, arg,
                        proxid):
                    raise RuntimeError(
                            "That prefix conflicts with another proxy.")

                proxy.set_prefix(arg)
                if proxy.type == Proxy.type.collective:
                    proxy.set_active(1)

                await message.add_reaction(REACT_CONFIRM)

            elif arg == "auto":
                if proxy.type == Proxy.type.override:
                    raise RuntimeError("You cannot autoproxy your override.")

                arg = reader.read_word().lower()
                if arg == "":
                    proxy.set_auto(1-proxy.auto)
                else:
                    if arg not in KEYWORDS:
                        raise RuntimeError("Please specify 'on' or 'off'.")
                    proxy.set_auto(KEYWORDS[arg])

                await message.add_reaction(REACT_CONFIRM)

        elif arg in ["collective", "c"]:
            if is_dm(message):
                raise RuntimeError(ERROR_DM)
            arg = reader.read_word().lower()
            if arg in ["new", "create"]:
                if not (message.author.permissions_in(message.channel)
                        .manage_roles):
                    raise RuntimeError(ERROR_MANAGE_ROLES)

                guild = message.channel.guild
                rolename = reader.read_remainder()
                if rolename == "everyone":
                    role = guild.default_role
                # is it a role mention?
                elif re.match("\<\@\&[0-9]+\>", rolename):
                    if len(message.role_mentions) > 0:
                        role = message.role_mentions[0]
                    else:
                        # this shouldn't happen but just in case
                        raise RuntimeError("Not sure what happened here. "
                                "Try again?")
                else:
                    raise RuntimeError("Please provide a role.")
                if role.guild != guild:
                    raise RuntimeError("Uhh... That role isn't in this guild?")

                # new collective with name of role and no avatar
                self.cur.execute("insert or ignore into collectives values"
                        "(?, ?, ?, ?, NULL)",
                        (self.gen_id(), guild.id, role.id, role.name))
                if self.cur.rowcount == 1:
                    self.sync_role(guild, role.id)
                    await message.add_reaction(REACT_CONFIRM)

            else: # arg is collective ID
                collid = arg
                action = reader.read_word().lower()
                if action in ["name", "avatar"]:
                    arg = reader.read_remainder()
                    if arg == "":
                        raise RuntimeError("Please provide a " + action)

                    row = self.cur.execute(
                            "select guildid, roleid from collectives "
                            "where collid = ?",
                            (collid,)).fetchone()
                    if row == None:
                        raise RuntimeError("Invalid collective ID!")
                    guild = message.channel.guild
                    if row[0] != guild.id:
                        # TODO allow commands outside server
                        raise RuntimeError("Please try that again in %s"
                                % self.get_guild(row[0]).name)

                    role = guild.get_role(row[1])
                    if role == None:
                        raise RuntimeError("That role no longer exists?")
                    member = message.author # Member because this isn't a DM
                    if not (role in member.roles or member.permissions_in(
                        message.channel).manage_roles):
                        raise RuntimeError(
                                "You don't have access to that collective!")

                    self.cur.execute(
                            "update collectives set %s = ? "
                            "where collid = ?"
                            % ("nick" if action == "name" else "avatar"),
                            (arg, collid))
                    if self.cur.rowcount == 1:
                        await message.add_reaction(REACT_CONFIRM)

                elif action == "delete":
                    if not (message.author.permissions_in(message.channel)
                            .manage_roles):
                        raise RuntimeError(ERROR_MANAGE_ROLES)
                    row = self.cur.execute("select guildid, roleid from "
                            "collectives where collid = ?",
                            (collid,)).fetchone()
                    if row == None:
                        raise RuntimeError("Invalid collective ID!")
                    # all the more reason to delete it then, right?
                    # if guild.get_role(row[1]) == None:
                    self.cur.execute("delete from proxies where extraid = ?",
                            (row[1],))
                    self.cur.execute("delete from collectives where collid = ?",
                            (collid,))
                    if self.cur.rowcount == 1:
                        await message.add_reaction(REACT_CONFIRM)

        elif arg == "prefs":
            arg = reader.read_word()
            if len(arg) == 0:
                # must exist due to on_message
                userprefs = self.cur.execute(
                        "select prefs from users where userid = ?",
                        (authid,)).fetchone()[0]
                # list current prefs in "pref: [on/off]" format
                text = "\n".join(["%s: **%s**" %
                        (pref.name, "on" if userprefs & pref else "off")
                        for pref in Prefs])
                return await self.send_embed(message, text)

            if arg in ["default", "defaults"]:
                self.cur.execute(
                        "update users set prefs = ? where userid = ?",
                        (DEFAULT_PREFS, authid))
                return await message.add_reaction(REACT_CONFIRM)

            if not arg in Prefs.__members__.keys():
                raise RuntimeError("That preference does not exist.")

            bit = int(Prefs[arg])
            value = reader.read_word()
            if value == "": # only "prefs" + name given. invert the thing
                self.cur.execute(
                        "update users set prefs = (prefs & ~?) | (~prefs & ?)"
                        "where userid = ?",
                        (bit, bit, authid))
            else:
                if value not in KEYWORDS:
                    raise RuntimeError("Please specify 'on' or 'off'.")
                # note that KEYWORDS values are 0/1
                self.cur.execute(
                        "update users set prefs = (prefs & ~?) | ?"
                        "where userid = ?",
                        (bit, bit*KEYWORDS[value], authid))

            await message.add_reaction(REACT_CONFIRM)

        elif arg == "swap":
            arg = reader.read_word().lower()
            if arg == "open":
                if is_dm(message):
                    raise RuntimeError(ERROR_DM)

                prefix = reader.read_quote_reverse().lower()
                membername = reader.read_remainder()

                # discord.ext includes a MemberConverter
                # but that's only available whem using discord.ext Command
                member = (message.mentions[0] if len(message.mentions) > 0 else
                        message.channel.guild.get_member_named(membername))
                if member == None:
                    raise RuntimeError("User not found.")
                if membername == "": # prefix absorbed member name
                    raise RuntimeError(
                            "Please provide a prefix after the user.")
                if not self.check_prefix_collisions(authid, 0, prefix):
                    raise RuntimeError(
                            "That prefix conflicts with another proxy.")

                # first try to activate the other->author swap.
                self.cur.execute(
                        "update proxies set active = 1 where "
                        "(type, userid, extraid) = (?, ?, ?)",
                        (Proxy.type.swap, member.id, authid))
                # *must* be 0/1 due to unique constraint
                active = self.cur.rowcount == 1 
                # activate author->other swap
                self.cur.execute("insert or ignore into proxies values"
                        # auth, guild, id, prefix, type, member, auto, active
                        "(?, ?, 0, ?, ?, ?, 0, ?)",
                        (self.gen_id(), authid, prefix, Proxy.type.swap,
                            member.id, active))

                if self.cur.rowcount == 1:
                    await message.add_reaction(REACT_CONFIRM)
            elif arg == "close":
                swapname = reader.read_word()
                if swapname == "":
                    raise RuntimeError("Please provide a swap.")
                swap = self.cur.execute(
                        "select * from proxies "
                        "where (userid, proxid, type) = (?, ?, ?)",
                        (authid, swapname, Proxy.type.swap)
                        ).fetchone()
                if swap == None:
                    raise RuntimeError("You do not have a swap with that ID.")
                self.cur.execute("delete from proxies where proxid = ?",
                        (swapname,))
                self.cur.execute("delete from proxies "
                        "where (userid, extraid, type) = (?, ?, ?)",
                        (swap[5], authid, Proxy.type.swap))
                await message.add_reaction(REACT_CONFIRM)


    async def do_proxy(self, message, content, proxy):
        prefs = self.cur.execute("select prefs from users where userid = ?",
                (message.author.id,)).fetchone()[0]
        msgfile = None

        if (len(message.attachments) > 0
                and message.attachments[0].size <= MAX_FILE_SIZE):
            # only copy the first attachment
            msgfile = await message.attachments[0].to_file()
            # lets mobile users upload with spoilers
            if content.lower().find("spoiler") != -1:
                msgfile.filename = "SPOILER_" + msgfile.filename

        authid = message.author.id
        channel = message.channel

        if proxy.type == Proxy.type.swap:
            otherid = proxy.extraid
            member = channel.guild.get_member(otherid)
            # if the guild is large, the member may not be in cache
            if member == None and channel.guild.large:
                member = await channel.guild.fetch_member(otherid)
                # put this member in the cache (is this necessary?)
                if member != None:
                    channel.guild._add_member(member)
            if member == None:
                return
            present = (member.display_name,
                    member.avatar_url_as(format = "webp"))

        else: # currently only collective
            present = self.cur.execute("select nick, avatar from collectives "
                    "where roleid = ?",
                    (proxy.extraid,)).fetchone()
            if present == None:
                return

            # don't replace stuff while in a swap
            if prefs & Prefs.replace:
                # do these in order (or else, e.g. "I'm" could become "We'm")
                # which is funny but not what we want here
                # TODO: replace this with a reduce()?
                for x, y in REPLACE_DICT.items():
                    content = x.sub(y, content)

        row = self.cur.execute("select * from webhooks where chanid = ?",
                (channel.id,)).fetchone()
        if row == None:
            hook = await channel.create_webhook(name = WEBHOOK_NAME)
            self.cur.execute("insert into webhooks values (?, ?, ?)",
                    (channel.id, hook.id, hook.token))
        else:
            hook = discord.Webhook.partial(row[1], row[2],
                    adapter = discord.AsyncWebhookAdapter(self.sesh))

        msgid = (await hook.send(wait = True, content = content, file=msgfile,
                username = present[0], avatar_url = present[1])).id

        authname = str(message.author)
        # deleted = 0
        self.cur.execute("insert into history values (?, ?, ?, ?, ?, ?, 0)",
                (msgid, channel.id, authid, authname, proxy.extraid, content))
        await message.delete()


    async def on_message(self, message):
        if message.type != discord.MessageType.default or message.author.bot:
            return

        authid = message.author.id
        row = (self.cur.execute("select prefs from users where userid = ?",
            (authid,)).fetchone())
        if row == None:
            self.cur.execute("insert into users values (?, ?)",
                    (message.author.id, DEFAULT_PREFS))
            self.cur.execute("insert into proxies values"
                    "(?, ?, 0, NULL, ?, NULL, 0, 0)",
                    (self.gen_id(), authid, Proxy.type.override))
            row = (DEFAULT_PREFS,)

        # end of prefix or 0
        offset = (len(COMMAND_PREFIX)
                if message.content.lower().startswith(COMMAND_PREFIX) else 0)
        # command prefix is optional in DMs
        if offset != 0 or is_dm(message):
            # strip() so that e.g. "gs; help" works (helpful with autocorrect)
            try:
                await self.do_command(message, message.content[offset:].strip())
            except RuntimeError as e:
                if row[0] & Prefs.errors:
                    await self.send_embed(message, e.args[0])
            return

        # this is where the magic happens
        match = (self.cur.execute(
                "select * from proxies where ("
                    "((userid, active) = (?, 1))"
                    "and (guildid in (0, ?))"
                    # (prefix matches) XOR (autoproxy enabled)
                    "and ("
                        "(substr(?,0,length(prefix)+1) == prefix)"
                        "== (auto == 0)"
                    ")"
                # if message matches prefix for proxy A but proxy B is auto,
                # A wins. therefore, rank the proxy with auto = 0 higher
                ") order by auto asc limit 1",
                (message.author.id, message.guild.id, message.content.lower()))
                .fetchone())

        # return if no matches or matches override
        if match == None:
            return
        match = self.proxctl.from_row(match)
        if match.type == Proxy.type.override:
            return
        content = (message.content[0 if match.auto else len(match.prefix):]
                .strip())
        if content == "":
            return
        await self.do_proxy(message, content, match)


    # on_reaction_add doesn't catch everything
    async def on_raw_reaction_add(self, payload):
        if payload.user_id == self.user.id:
            return

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
        if reactor.bot:
            return
        message = await channel.fetch_message(payload.message_id)

        emoji = payload.emoji.name
        if emoji == REACT_QUERY:
            try:
                await reactor.send("Message sent by %s, id %d" % row)
            except discord.Forbidden:
                pass
            await message.remove_reaction(emoji, reactor)

        elif emoji == REACT_DELETE:
            # only sender may delete proxied message
            if payload.user_id == row[1]:
                self.cur.execute(
                        "update history set deleted = 1,"
                        "authname = '' where msgid = ?",
                        (payload.message_id,))
                await message.delete()
            else:
                await message.remove_reaction(emoji, reactor)



if __name__ == "__main__":
    instance = Gestalt(
            dbfile = sys.argv[1] if len(sys.argv) > 1 else "gestalt.db")

    try:
        instance.run(auth.token)
    except RuntimeError:
        print("Runtime error.")

    print("Shutting down.")
