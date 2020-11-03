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

WEBHOOK_NAME = "Gestalt webhook"

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
# DEFAULT_PREFIX = "g "

PURGE_AGE = 3600*24*7   # 1 week
PURGE_TIMEOUT = 3600*2  # 2 hours

# limits for non-Nitro users by boost level
MAX_FILE_SIZE = [
        8*1024*1024,
        8*1024*1024,
        50*1024*1024,
        100*1024*1024]

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

@enum.unique
class Prefs(enum.IntFlag):
#   auto        = 1 << 0
    replace     = 1 << 1
#   autoswap    = 1 << 2
    errors      = 1 << 3
DEFAULT_PREFS = Prefs.replace | Prefs.errors



class CommandReader:
    BOOL_KEYWORDS = {
        "on": 1,
        "off": 0,
        "yes": 1,
        "no": 0,
        "true": 1,
        "false": 0,
        "0": 0,
        "1": 1
    }

    def __init__(self, msg, cmd):
        self.msg = msg
        self.cmd = cmd

    def is_empty(self):
        return self.cmd == ""

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

    def read_bool_int(self):
        word = self.read_word().lower()
        if word not in CommandReader.BOOL_KEYWORDS:
            return None
        return CommandReader.BOOL_KEYWORDS[word]

    def read_remainder(self):
        ret = self.cmd
        if len(ret) > 1 and ret[1] == ret[-1] == '"':
            ret = ret[1:-1]
        self.cmd = ""
        return ret


class GestaltUser:
    def __init__(self, cursor, row):
        self.cur = cursor
        (self.userid, self.username, self.prefs) = row

    def set_prefs(self, prefs):
        self.prefs = prefs
        self.cur.execute("update users set prefs = ? where userid = ?",
                (prefs, self.userid))


class Proxy:
    @enum.unique
    class type(enum.IntEnum):
        override    = 0
        collective  = 1
        swap        = 2

    def __init__(self, trans, row):
        self.trans = trans
        self.cur = trans.cur
        (self.proxid, self.userid, self.guildid, self.prefix, self.type,
                self.extraid, self.auto, self.active) = row

    # this base class version shouldn't be called normally
    async def send(self, webhook, message, content, attachment):
        return message.send(content, file = attachment)

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


class ProxyOverride(Proxy):
    async def send(self, webhook, message, content, attachment):
        raise RuntimeError("Attempted to send a message with an override proxy")


class ProxyCollective(Proxy):
    async def send(self, webhook, message, content, attachment):
        authid = message.author.id
        prefs = self.trans.user_by_id(authid).prefs
        present = self.cur.execute("select nick, avatar from collectives "
                "where roleid = ?",
                (self.extraid,)).fetchone()
        if present == None:
            return super().send(webhook, message, content, attachment)

        if prefs & Prefs.replace:
            # do these in order (or else, e.g. "I'm" could become "We'm")
            # which is funny but not what we want here
            # TODO: replace this with a reduce()?
            for x, y in REPLACE_DICT.items():
                content = x.sub(y, content)

        return await webhook.send(wait = True, content = content,
                file=attachment, username = present[0],
                avatar_url = present[1])


class ProxySwap(Proxy):
    async def send(self, webhook, message, content, attachment):
        member = message.guild.get_member(self.extraid)
        if member == None:
            return

        return await webhook.send(wait = True, content = content,
                file = attachment, username = member.display_name,
                avatar_url = member.avatar_url_as(format = "webp"))


class Translator:
    type_mapping = {
            0: ProxyOverride,
            1: ProxyCollective,
            2: ProxySwap,
    }

    def __init__(self, cursor):
        self.cur = cursor

    def user_by_id(self, userid):
        row = self.cur.execute("select * from users where userid = ?",
                (userid,)).fetchone()
        return None if row == None else GestaltUser(self.cur, row)

    def proxy_from_row(self, row):
        if row[4] not in Translator.type_mapping:
            return Proxy(self, row)
        return Translator.type_mapping[row[4]](self, row)

    def proxy_by_id(self, proxid):
        row = self.cur.execute("select * from proxies where proxid = ?",
                (proxid,)).fetchone()
        return None if row == None else self.proxy_from_row(row)


def is_text(message):
    return message.channel.type == discord.ChannelType.text


def is_dm(message):
    return message.channel.type == discord.ChannelType.private


class Gestalt(discord.Client):
    def __init__(self, *, dbfile, purge = True):
        super().__init__(intents = INTENTS)

        self.conn = sqlite.connect(dbfile)
        self.cur = self.conn.cursor()
        self.cur.execute(
                "create table if not exists history("
                "msgid integer primary key,"
                "chanid integer,"
                "authid integer,"
                "otherid text,"
                "content text,"
                "deleted integer)")
        self.cur.execute(
                "create table if not exists users("
                "userid integer primary key,"
                "username text,"
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
                # this is a no-op to kick in the update trigger
                "create trigger if not exists proxy_prefix_conflict_insert "
                "after insert on proxies begin "
                    "update proxies set prefix = new.prefix "
                    "where proxid = new.proxid"
                "; end")
        self.cur.execute(
                "create trigger if not exists proxy_prefix_conflict_update "
                "after update of prefix on proxies when (exists("
                    "select * from proxies where ("
                        "(userid == new.userid) and (proxid != new.proxid)"
                        "and ("
                            # if prefix is to be global, check everything
                            # if not, check only the same guild
                                "(new.guildid == 0)"
                            "or"
                                "((new.guildid != 0)"
                                "and (guildid in (0, new.guildid)))"
                        ") and ("
                                "(substr(prefix,0,length(new.prefix)+1)"
                                "== new.prefix)"
                            "or"
                                "(substr(new.prefix,0,length(prefix)+1)"
                                "== prefix)"
                        ")"
                    ")"
                # this exception will be passed to the user
                ")) begin select (raise(abort,"
                    "'That prefix conflicts with another proxy.'"
                ")); end")
        self.cur.execute(
                # NB: this does not trigger if a proxy is inserted with auto = 1
                # including "insert or replace"
                "create trigger if not exists auto_exclusive "
                "after update of auto on proxies when (new.auto = 1) begin "
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
        # these next three link swaps together such that:
        # - when a swap is inserted, activate the opposite if it exists
        # - when a swap is updated, activate the opposite if it exists
        # - when a swap is deleted, delete the opposite swap
        # the first two work together such that if (a,b) is inserted and (b,a)
        # exists, then first (b,a) is updated, then the update trigger kicks in
        # and updates the original (a,b) swap, resulting in both activated.
        # note that these won't work on other swaps because they rely on
        # extraid being a user id.
        self.cur.execute(
                "create trigger if not exists swap_active_insert "
                "after insert on proxies begin "
                    "update proxies set active = 1 where ("
                        "(userid, extraid) = (new.extraid, new.userid)"
                    ");"
                "end")
        self.cur.execute(
                "create trigger if not exists swap_active_update "
                "after update of active on proxies begin "
                    "update proxies set active = 1 where ("
                        "(active != 1)" # infinite recursion is bad
                        "and (userid, extraid) = (new.extraid, new.userid)"
                    ");"
                "end")
        self.cur.execute(
                "create trigger if not exists swap_delete "
                "after delete on proxies begin "
                    "delete from proxies where ("
                        "(userid, extraid) = (old.extraid, old.userid)"
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

        self.trans = Translator(self.cur)

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
        self.invite = (await self.application_info()).bot_public


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
                    "insert into history values (?, 0, ?, 0, '', 0)",
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


    # called on role delete
    def sync_role(self, guild, roleid):
        role = guild.get_role(roleid)
        # check if it's deleted and act accordingly
        if role == None:
            self.cur.execute("delete from collectives where roleid = ?",
                    (roleid,))
            self.cur.execute("delete from proxies where extraid = ?", (roleid,))
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
            for table in ["users", "proxies", "collectives"]:
                await self.send_embed(message, "```%s```" % "\n".join(
                    ["|".join([str(i) for i in x]) for x in self.cur.execute(
                        "select * from %s" % table).fetchall()]))
            return

        if arg == "help":
            await self.send_embed(message, HELPMSG)

        elif arg == "invite" and self.invite:
            await self.send_embed(message,
                    "https://discord.com/api/oauth2/authorize?"
                    "client_id=%i&permissions=%i&scope=bot"
                    % (self.user.id, PERMS.value))

        elif arg == "permcheck":
            guildid = reader.read_word()
            if (guildid == "" and (is_dm(message)
                    or not re.match("[0-9]*", guildid))):
                raise RuntimeError("Please provide a valid guild ID.")
            guildid = message.guild.id if guildid == "" else int(guildid)
            guild = self.get_guild(guildid)
            if guild == None:
                raise RuntimeError(
                        "That guild does not exist or I am not in it.")
            memberbot = guild.get_member(self.user.id)
            memberauth = guild.get_member(authid)
            if memberauth == None:
                raise RuntimeError("You are not a member of that guild.")

            text = "**%s**:\n" % guild.name
            noaccess = False
            for chan in guild.text_channels:
                if not memberauth.permissions_in(chan).view_channel:
                    noaccess = True
                    continue

                errors = []
                for p in PERMS: # p = ("name", bool)
                    if p[1] and not p in list(memberbot.permissions_in(chan)):
                        errors += [p[0]]

                # lack of access implies lack of other perms, so leave them out
                if "read_messages" in errors:
                    errors = ["read_messages"]
                errors = REACT_CONFIRM if errors == [] else ", ".join(errors)
                text += "`#%s`: %s\n" % (chan.name, errors)

            if noaccess:
                text += "Some channels you can't see are omitted."
            await self.send_embed(message, text)

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

            proxy = self.trans.proxy_by_id(proxid)
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

                proxy.set_prefix(arg)
                if proxy.type == Proxy.type.collective:
                    proxy.set_active(1)

                await message.add_reaction(REACT_CONFIRM)

            elif arg == "auto":
                if proxy.type == Proxy.type.override:
                    raise RuntimeError("You cannot autoproxy your override.")

                if reader.is_empty():
                    proxy.set_auto(1-proxy.auto)
                else:
                    arg = reader.read_bool_int()
                    if arg == None:
                        raise RuntimeError("Please specify 'on' or 'off'.")
                    proxy.set_auto(arg)

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
                if len(message.role_mentions) > 0:
                    role = message.role_mentions[0]
                elif rolename == "everyone":
                    role = guild.default_role
                else:
                    role = discord.utils.get(guild.roles, name = rolename)
                    if role == None:
                        raise RuntimeError("Please provide a role.")

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
            # user must exist due to on_message
            user = self.trans.user_by_id(authid)
            arg = reader.read_word()
            if len(arg) == 0:
                # list current prefs in "pref: [on/off]" format
                text = "\n".join(["%s: **%s**" %
                        (pref.name, "on" if user.prefs & pref else "off")
                        for pref in Prefs])
                return await self.send_embed(message, text)

            if arg in ["default", "defaults"]:
                user.set_prefs(DEFAULT_PREFS)
                return await message.add_reaction(REACT_CONFIRM)

            if not arg in Prefs.__members__.keys():
                raise RuntimeError("That preference does not exist.")

            bit = int(Prefs[arg])
            if reader.is_empty(): # only "prefs" + name given. invert the thing
                user.set_prefs(user.prefs ^ bit)
            else:
                value = reader.read_bool_int()
                if value == None:
                    raise RuntimeError("Please specify 'on' or 'off'.")
                user.set_prefs((user.prefs & ~bit) | (bit * value))

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

                # activate author->other swap
                self.cur.execute("insert or ignore into proxies values"
                        # id, auth, guild, prefix, type, member, auto, active
                        "(?, ?, 0, ?, ?, ?, 0, 0)",
                        (self.gen_id(), authid, prefix, Proxy.type.swap,
                            member.id))
                # triggers will take care of activation if necessary

                if self.cur.rowcount == 1:
                    await message.add_reaction(REACT_CONFIRM)

            elif arg == "close":
                swapname = reader.read_quote().lower()
                if swapname == "":
                    raise RuntimeError("Please provide a swap ID or prefix.")

                self.cur.execute(
                        "delete from proxies where "
                        "(userid, type) = (?, ?) and (? in (proxid, prefix))",
                        (authid, Proxy.type.swap, swapname))
                if self.cur.rowcount == 0:
                    raise RuntimeError(
                            "You do not have a swap with that ID or prefix.")
                await message.add_reaction(REACT_CONFIRM)


    async def do_proxy(self, message, content, proxy):
        authid = message.author.id
        channel = message.channel
        msgfile = None

        if (len(message.attachments) > 0
                and message.attachments[0].size
                <= MAX_FILE_SIZE[message.guild.premium_tier]):
            # only copy the first attachment
            msgfile = await message.attachments[0].to_file()
            # lets mobile users upload with spoilers
            if content.lower().find("spoiler") != -1:
                msgfile.filename = "SPOILER_" + msgfile.filename

        row = self.cur.execute("select * from webhooks where chanid = ?",
                (channel.id,)).fetchone()
        if row == None:
            hook = await channel.create_webhook(name = WEBHOOK_NAME)
            self.cur.execute("insert into webhooks values (?, ?, ?)",
                    (channel.id, hook.id, hook.token))
        else:
            hook = discord.Webhook.partial(row[1], row[2],
                    adapter = discord.AsyncWebhookAdapter(self.sesh))

        msgid = (await proxy.send(hook, message, content, msgfile)).id

        # deleted = 0
        self.cur.execute("insert into history values (?, ?, ?, ?, ?, 0)",
                (msgid, channel.id, authid, proxy.extraid, content))
        await message.delete()


    async def on_message(self, message):
        if message.type != discord.MessageType.default or message.author.bot:
            return

        authid = message.author.id
        author = self.trans.user_by_id(authid)
        if author == None:
            self.cur.execute("insert into users values (?, ?, ?)",
                    (message.author.id, str(message.author), DEFAULT_PREFS))
            self.cur.execute("insert into proxies values"
                    "(?, ?, 0, NULL, ?, NULL, 0, 0)",
                    (self.gen_id(), authid, Proxy.type.override))
            errors = DEFAULT_PREFS & Prefs.errors
        else:
            if author.username != str(message.author):
                self.cur.execute(
                        "update users set username = ? where userid = ?",
                        (str(message.author), authid))
            errors = author.prefs & Prefs.errors

        # end of prefix or 0
        offset = (len(COMMAND_PREFIX)
                if message.content.lower().startswith(COMMAND_PREFIX) else 0)
        # command prefix is optional in DMs
        if offset != 0 or is_dm(message):
            # strip() so that e.g. "gs; help" works (helpful with autocorrect)
            try:
                await self.do_command(message, message.content[offset:].strip())
            except (RuntimeError, sqlite.IntegrityError) as e:
                if errors:
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
        match = self.trans.proxy_from_row(match)
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
            "select authid from history where msgid = ?",
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
            author = self.trans.user_by_id(row[0])
            try:
                await reactor.send(
                        "Message sent by %s, id %d"
                        % (author.username, author.userid))
            except discord.Forbidden:
                pass # user doesn't accept new messages from us
            await message.remove_reaction(emoji, reactor)

        elif emoji == REACT_DELETE:
            # only sender may delete proxied message
            if payload.user_id == row[1]:
                # don't delete the entry immediately.
                # purge_loop will take care of it later.
                self.cur.execute(
                        "update history set deleted = 1 where msgid = ?",
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
