#!/usr/bin/python3.7

from functools import reduce
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

from config import *
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

DEFAULT_PREFS = reduce(lambda a, b : a | Prefs[b], DEFAULT_PREFS, 0)
REPLACEMENTS = [(re.compile(x, re.IGNORECASE), y) for x, y in REPLACEMENTS]



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
        if len(ret) > 1 and ret[0] == ret[-1] == '"':
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

    def set_username(self, username):
        self.username = username
        self.cur.execute("update users set username = ? where userid = ?",
                (username, self.userid))


class Proxy:
    @enum.unique
    class type(enum.IntEnum):
        override    = 0
        collective  = 1
        swap        = 2


def is_text(message):
    return message.channel.type == discord.ChannelType.text


def is_dm(message):
    return message.channel.type == discord.ChannelType.private


class Gestalt(discord.Client):
    def __init__(self, *, dbfile, purge = True):
        super().__init__(intents = INTENTS)

        self.conn = sqlite.connect(dbfile)
        self.conn.row_factory = sqlite.Row
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
                "unique(userid, extraid))")
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
        self.adapter = discord.AsyncWebhookAdapter(aiohttp.ClientSession())
        self.loop.add_signal_handler(signal.SIGINT, self.handler)
        self.loop.add_signal_handler(signal.SIGTERM, self.handler)
        await self.change_presence(status = discord.Status.online,
                activity = discord.Game(name = COMMAND_PREFIX + "help"))
        self.invite = (await self.application_info()).bot_public


    async def close(self):
        await super().close()
        await self.adapter.session.close()


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


    # this is called manually in do_command(), not attached to an event
    # because users being added/removed from a role activates on_member_update()
    def sync_role(self, role):
        # is this role attached to a collective?
        if (self.cur.execute("select * from collectives where roleid = ?",
            (role.id,)).fetchone() == None):
            return

        # is there anyone with the proxy who shouldn't?
        rows = self.cur.execute("select * from proxies where extraid = ?",
                (role.id,)).fetchall()
        # currently unused
        '''
        userids = [x.id for x in role.members]
        for row in rows:
            if row["userid"] not in userids:
                self.cur.execute("delete from proxies where proxid = ?",
                        (proxy["proxid"],))
        '''

        # is there anyone without the proxy who should?
        # do this second; no need to check a proxy that's just been added
        rows = [x["userid"] for x in rows]
        for member in role.members:
            # don't just "insert or ignore"; gen_id() is expensive
            if member.id not in rows and not member.bot:
                self.cur.execute(
                        # prefix = NULL, auto = 0, active = 0
                        "insert into proxies values "
                        "(?, ?, ?, NULL, ?, ?, 0, 0)",
                        (self.gen_id(), member.id, role.guild.id,
                            Proxy.type.collective, role.id))


    def sync_member(self, member):
        if member.bot:
            return

        guild = member.guild
        # first, check if they have any proxies they shouldn't
        # right now, this only applies to collectives
        rows = self.cur.execute(
                "select * from proxies "
                "where (userid, guildid, type) = (?, ?, ?)",
                (member.id, guild.id, Proxy.type.collective)).fetchall()
        roleids = [x.id for x in member.roles]
        for row in rows:
            if row["extraid"] not in roleids:
                self.cur.execute("delete from proxies where proxid = ?",
                        (row["proxid"],))

        # now check if they don't have any proxies they should
        # do this second; no need to check a proxy that's just been added
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


    async def on_guild_role_delete(self, role):
        # no need to delete proxies; on_member_update takes care of that
        self.cur.execute("delete from collectives where roleid = ?", (role.id,))


    async def on_member_update(self, before, after):
        self.sync_member(after)


    # add @everyone collective, if necessary
    async def on_member_join(self, member):
        self.sync_member(member)


    # discord.py commands extension throws out bot messages
    # this is incompatible with the test framework so process commands manually
    async def do_command(self, message, cmd):
        reader = CommandReader(message, cmd)
        arg = reader.read_word().lower()
        authid = message.author.id

        """
        if arg == "debug":
            for table in ["users", "proxies", "collectives"]:
                await self.send_embed(message, "```%s```" % "\n".join(
                    ["|".join([str(i) for i in x]) for x in self.cur.execute(
                        "select * from %s" % table).fetchall()]))
            return
        """

        if arg == "help":
            await self.send_embed(message, HELPMSG)

        elif arg == "invite" and self.invite:
            await self.send_embed(message,
                    discord.utils.oauth_url(self.user.id, permissions = PERMS))

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
                lines = []
                # must be at least one: the override
                for proxy in rows:
                    # sanitize text to not mess up formatting
                    s = lambda x : discord.utils.escape_markdown(str(x))
                    line = "`%s`" % proxy["proxid"]
                    if proxy["type"] == Proxy.type.override:
                        line += (":no_entry: prefix **%s**"
                                %(s(proxy["prefix"]),))
                    elif proxy["type"] == Proxy.type.swap:
                        line += (":twisted_rightwards_arrows: with **%s** "
                                "prefix **%s**"
                                % (s(self.get_user(proxy["extraid"])),
                                    s(proxy["prefix"])))
                    elif proxy["type"] == Proxy.type.collective:
                        guild = self.get_guild(proxy["guildid"])
                        name = self.cur.execute(
                                "select nick from collectives "
                                "where roleid = ?",
                                (proxy["extraid"],)).fetchone()[0]
                        line += (":bee: **%s** on **%s** in **%s** "
                                "prefix **%s**"
                                % (s(name),
                                    s(guild.get_role(proxy["extraid"]).name),
                                    s(guild.name), s(proxy["prefix"])))
                    if proxy["active"] == 0:
                        line += " *(inactive)*"
                    lines.append(line)
                return await self.send_embed(message, "\n".join(lines))

            proxy = self.cur.execute("select * from proxies where proxid = ?",
                    (proxid,)).fetchone()
            if proxy == None or proxy["userid"] != authid:
                raise RuntimeError("You do not have a proxy with that ID.")

            if arg == "prefix":
                arg = reader.read_quote().lower()
                if arg.replace("text","") == "":
                    raise RuntimeError("Please provide a valid prefix.")

                arg = arg.lower()
                # adapt PluralKit [text] prefix/postfix format
                if arg.endswith("text"):
                    arg = arg[:-4]

                self.cur.execute(
                    "update proxies set prefix = ? where proxid = ?",
                    (arg, proxy["proxid"]))
                if proxy["type"] != Proxy.type.swap:
                    self.cur.execute(
                        "update proxies set active = 1 where proxid = ?",
                        (proxy["proxid"],))

                await message.add_reaction(REACT_CONFIRM)

            elif arg == "auto":
                if proxy["type"] == Proxy.type.override:
                    raise RuntimeError("You cannot autoproxy your override.")

                if reader.is_empty():
                    self.cur.execute(
                        "update proxies set auto = 1 - auto where proxid = ?",
                        (proxy["proxid"],))
                else:
                    arg = reader.read_bool_int()
                    if arg == None:
                        raise RuntimeError("Please specify 'on' or 'off'.")
                    self.cur.execute(
                        "update proxies set auto = ? where proxid = ?",
                        (arg, proxy["proxid"]))

                await message.add_reaction(REACT_CONFIRM)

        elif arg in ["collective", "c"]:
            if is_dm(message):
                raise RuntimeError(ERROR_DM)
            guild = message.guild
            arg = reader.read_word().lower()

            if arg == "":
                rows = self.cur.execute(
                        "select * from collectives where guildid = ?",
                        (guild.id,)).fetchall()
                text = "\n".join(["`%s`: %s %s" %
                        (row["collid"],
                            "**%s**" % (row["nick"] if row["nick"]
                                else "*(no name)*"),
                            # @everyone.mention shows up as @@everyone. weird!
                            # note that this is an embed; mentions don't work
                            ("@everyone" if row["roleid"] == guild.id
                                else guild.get_role(row["roleid"]).mention))
                        for row in rows])
                if not text:
                    text = "This guild does not have any collectives."
                return await self.send_embed(message, text)

            elif arg in ["new", "create"]:
                if not message.author.guild_permissions.manage_roles:
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

                if role.managed:
                    # bots, server booster, integrated subscription services
                    # requiring users to pay to participate is antithetical
                    # to community-oriented identity play
                    # TODO: return to this with RoleTags in 1.6
                    raise RuntimeError(ERROR_CURSED)

                # new collective with name of role and no avatar
                self.cur.execute("insert or ignore into collectives values"
                        "(?, ?, ?, ?, NULL)",
                        (self.gen_id(), guild.id, role.id, role.name))
                if self.cur.rowcount == 1:
                    self.sync_role(role)
                    await message.add_reaction(REACT_CONFIRM)

            else: # arg is collective ID
                collid = arg
                action = reader.read_word().lower()
                row = self.cur.execute(
                        "select * from collectives where collid = ?",
                        (collid,)).fetchone()
                if row == None:
                    raise RuntimeError("Invalid collective ID!")

                if row["guildid"] != guild.id:
                    # TODO allow commands outside server
                    raise RuntimeError("Please try that again in %s"
                            % self.get_guild(row["guildid"]).name)

                if action in ["name", "avatar"]:
                    arg = reader.read_remainder()

                    role = guild.get_role(row["roleid"])
                    if role == None:
                        raise RuntimeError("That role no longer exists?")

                    member = message.author # Member because this isn't a DM
                    if not (role in member.roles
                            or member.guild_permissions.manage_roles):
                        raise RuntimeError(
                                "You don't have access to that collective!")

                    if arg == "":
                        # allow empty avatar URL but not name
                        if action == "name":
                            raise RuntimeError("Please provide a new name.")
                        elif message.attachments:
                            arg = message.attachments[0].url

                    self.cur.execute(
                            "update collectives set %s = ? "
                            "where collid = ?"
                            % ("nick" if action == "name" else "avatar"),
                            (arg, collid))
                    if self.cur.rowcount == 1:
                        await message.add_reaction(REACT_CONFIRM)

                elif action == "delete":
                    if not message.author.guild_permissions.manage_roles:
                        raise RuntimeError(ERROR_MANAGE_ROLES)

                    # all the more reason to delete it then, right?
                    # if guild.get_role(row[1]) == None:
                    self.cur.execute("delete from proxies where extraid = ?",
                            (row["roleid"],))
                    self.cur.execute("delete from collectives where collid = ?",
                            (collid,))
                    if self.cur.rowcount == 1:
                        await message.add_reaction(REACT_CONFIRM)

        elif arg == "prefs":
            # user must exist due to on_message
            user = self.cur.execute(
                    "select * from users where userid = ?",
                    (authid,)).fetchone()
            arg = reader.read_word()
            if len(arg) == 0:
                # list current prefs in "pref: [on/off]" format
                text = "\n".join(["%s: **%s**" %
                        (pref.name, "on" if user["prefs"] & pref else "off")
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
            if reader.is_empty(): # only "prefs" + name given. invert the thing
                prefs = user["prefs"] ^ bit
            else:
                value = reader.read_bool_int()
                if value == None:
                    raise RuntimeError("Please specify 'on' or 'off'.")
                prefs = (user["prefs"] & ~bit) | (bit * value)
            self.cur.execute(
                    "update users set prefs = ? where userid = ?",
                    (prefs, authid))

            await message.add_reaction(REACT_CONFIRM)

        elif arg in ["swap", "s"]:
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

                if member.id == self.user.id:
                    raise RuntimeError(ERROR_BLURSED)
                if member.bot:
                    raise RuntimeError(ERROR_CURSED)

                # activate author->other swap
                self.cur.execute("insert or ignore into proxies values"
                        # id, auth, guild, prefix, type, member, auto, active
                        "(?, ?, 0, ?, ?, ?, 0, 0)",
                        (self.gen_id(), authid, prefix, Proxy.type.swap,
                            member.id))
                # triggers will take care of activation if necessary

                if self.cur.rowcount == 1:
                    await message.add_reaction(REACT_CONFIRM)

            elif arg in ["close", "off"]:
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

    async def do_proxy_collective(self, message, target, prefs,
            content, attach, hook):
        present = self.cur.execute("select * from collectives where roleid = ?",
                (target,)).fetchone()

        if prefs & Prefs.replace:
            # do these in order (or else, e.g. "I'm" could become "We'm")
            # which is funny but not what we want here
            # this could be a reduce() but this is more readable
            for x, y in REPLACEMENTS:
                content = x.sub(y, content)

        return await hook.send(wait = True, content = content,
                file = attach, username = present["nick"],
                avatar_url = present["avatar"])

    async def do_proxy_swap(self, message, target, prefs,
            content, attach, hook):
        member = message.guild.get_member(target)
        if member == None:
            return

        return await hook.send(wait = True, content = content,
                file = attach, username = member.display_name,
                avatar_url = member.avatar_url_as(format = "webp"))

    async def do_proxy(self, message, proxy, prefs):
        authid = message.author.id
        channel = message.channel
        msgfile = None

        content = (
                message.content[0 if proxy["auto"] else len(proxy["prefix"]):]
                .strip())
        if content == "" and len(message.attachments) == 0:
            return

        if len(message.attachments) > 0:
            # only copy the first attachment
            attach = message.attachments[0]
            if attach.size <= MAX_FILE_SIZE[message.guild.premium_tier]:
                msgfile = await attach.to_file(
                        spoiler = attach.is_spoiler()
                        # lets mobile users upload with spoilers
                        or content.lower().find("spoiler") != -1)
        # avoid error when user proxies empty message with invalid attachment
        if msgfile == None and content == "":
            return

        # this should never loop infinitely but just in case
        for ignored in range(2):
            row = self.cur.execute("select * from webhooks where chanid = ?",
                    (channel.id,)).fetchone()
            if row == None:
                try:
                    hook = await channel.create_webhook(name = WEBHOOK_NAME)
                except discord.errors.Forbidden:
                    return # welp
                self.cur.execute("insert into webhooks values (?, ?, ?)",
                        (channel.id, hook.id, hook.token))
            else:
                hook = discord.Webhook.partial(row[1], row[2],
                        adapter = self.adapter)

            try:
                func = [None,
                        self.do_proxy_collective,
                        self.do_proxy_swap
                        ][proxy["type"]]
                msg = await func(message, proxy["extraid"], prefs,
                        content, msgfile, hook)
            except discord.errors.NotFound:
                # webhook is deleted. delete entry and return to top of loop
                self.cur.execute("delete from webhooks where chanid = ?",
                        (channel.id,))
                continue
            else:
                break

        # in case e.g. it's a swap but the other user isn't in the guild
        if msg == None:
            return

        # deleted = 0
        self.cur.execute("insert into history values (?, ?, ?, ?, ?, 0)",
                (msg.id, channel.id, authid, proxy["extraid"],
                    content if LOG_MESSAGE_CONTENT else ""))

        if prefs & Prefs.delay:
            await asyncio.sleep(0.2)
        try:
            await message.delete()
        except discord.errors.Forbidden:
            pass


    async def on_message(self, message):
        if message.type != discord.MessageType.default or message.author.bot:
            return

        authid = message.author.id
        author = self.cur.execute(
                "select * from users where userid = ?",
                (authid,)).fetchone()
        if author == None:
            self.cur.execute("insert into users values (?, ?, ?)",
                    (message.author.id, str(message.author), DEFAULT_PREFS))
            self.cur.execute("insert into proxies values"
                    "(?, ?, 0, NULL, ?, 0, 0, 0)",
                    (self.gen_id(), authid, Proxy.type.override))
            prefs = DEFAULT_PREFS
        else:
            if author["username"] != str(message.author):
                self.cur.execute(
                        "update usres set username = ? where userid = ?",
                        (str(message.author), authid))
            prefs = author["prefs"]

        # end of prefix or 0
        offset = (len(COMMAND_PREFIX)
                if message.content.lower().startswith(COMMAND_PREFIX) else 0)
        # command prefix is optional in DMs
        if offset != 0 or is_dm(message):
            # strip() so that e.g. "gs; help" works (helpful with autocorrect)
            try:
                await self.do_command(message, message.content[offset:].strip())
            except (RuntimeError, sqlite.IntegrityError) as e:
                if prefs & Prefs.errors:
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
        if match["type"] == Proxy.type.override:
            return
        await self.do_proxy(message, match, prefs)


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
            author = self.cur.execute(
                    "select * from users where userid = ?",
                    (row["authid"],)).fetchone()
            try:
                # this can fail depending on user's DM settings & prior messages
                await reactor.send(
                        "Message sent by %s, id %d"
                        % (author["username"], author["userid"]))
                await message.remove_reaction(emoji, reactor)
            except discord.errors.Forbidden:
                pass

        elif emoji == REACT_DELETE:
            # only sender may delete proxied message
            if payload.user_id == row["authid"]:
                try:
                    await message.delete()
                except discord.errors.Forbidden:
                    return
                # don't delete the entry immediately.
                # purge_loop will take care of it later.
                self.cur.execute(
                        "update history set deleted = 1 where msgid = ?",
                        (payload.message_id,))
            else:
                try:
                    await message.remove_reaction(emoji, reactor)
                except discord.errors.Forbidden:
                    pass



def main():
    instance = Gestalt(
            dbfile = sys.argv[1] if len(sys.argv) > 1 else "gestalt.db")

    try:
        instance.run(auth.token)
    except RuntimeError:
        print("Runtime error.")

    print("Shutting down.")

if __name__ == "__main__":
    main()

