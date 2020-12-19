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

from defs import *
import commands
import auth


class Gestalt(discord.Client, commands.GestaltCommands):
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
                "type integer,"             # see enum ProxyType
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


    async def close(self):
        await super().close()
        await self.adapter.session.close()


    async def send_embed(self, replyto, text):
        msg = await replyto.channel.send(
                embed = discord.Embed(description = text))
        # insert into history to allow initiator to delete message if desired
        if is_text(replyto):
            await msg.add_reaction(REACT_DELETE)
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
                            ProxyType.collective, role.id))


    def sync_member(self, member):
        if member.bot:
            return

        guild = member.guild
        # first, check if they have any proxies they shouldn't
        # right now, this only applies to collectives
        rows = self.cur.execute(
                "select * from proxies "
                "where (userid, guildid, type) = (?, ?, ?)",
                (member.id, guild.id, ProxyType.collective)).fetchall()
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
                        ProxyType.collective, role.id))


    async def on_guild_role_delete(self, role):
        # no need to delete proxies; on_member_update takes care of that
        self.cur.execute("delete from collectives where roleid = ?", (role.id,))


    async def on_member_update(self, before, after):
        self.sync_member(after)


    # add @everyone collective, if necessary
    async def on_member_join(self, member):
        self.sync_member(member)


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
                    (self.gen_id(), authid, ProxyType.override))
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
        if match["type"] == ProxyType.override:
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

