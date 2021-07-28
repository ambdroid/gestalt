#!/usr/bin/python3.7

from datetime import datetime, timedelta
from functools import reduce
import sqlite3 as sqlite
import asyncio
import random
import signal
import string
import enum
import math
import sys
import re

from discord.ext import tasks
import aiohttp
import discord

from defs import *
import commands
import auth


class Gestalt(discord.Client, commands.GestaltCommands):
    def __init__(self, *, dbfile):
        super().__init__(intents = INTENTS)

        self.conn = sqlite.connect(dbfile)
        self.conn.row_factory = sqlite.Row
        self.cur = self.conn.cursor()
        self.execute(
                'create table if not exists history('
                'msgid integer primary key,'
                'chanid integer,'
                'authid integer,'
                'otherid integer,'
                'maskid text,'
                'content text,'
                'deleted integer)')
        self.execute(
                'create table if not exists users('
                'userid integer primary key,'
                'username text,'
                'prefs integer)')
        self.execute(
                'create table if not exists webhooks('
                'chanid integer primary key,'
                'hookid integer,'
                'token text)')
        self.execute(
                'create table if not exists proxies('
                'proxid text primary key collate nocase,'   # of form 'abcde'
                'userid integer,'
                'guildid integer,'          # 0 for swaps, overrides
                'prefix text,'
                'postfix text,'
                'type integer,'             # see enum ProxyType
                'otherid integer,'          # userid for swaps
                'maskid text,'
                'auto integer,'             # 0/1
                'state integer,'            # see enum ProxyState
                'unique(userid, otherid),'
                'unique(userid, maskid))')
        self.execute(
                # this is a no-op to kick in the update trigger
                'create trigger if not exists proxy_tags_conflict_insert '
                'after insert on proxies when new.prefix not NULL begin '
                    'update proxies set prefix = new.prefix '
                    'where proxid = new.proxid'
                '; end')
        self.execute(
                'create trigger if not exists proxy_tags_conflict_update '
                'after update of prefix, postfix on proxies when exists('
                    'select 1 from proxies where ('
                        '('
                            'userid == new.userid'
                        ') and ('
                            'prefix not NULL'
                        ') and ('
                            'proxid != new.proxid'
                        ') and ('
                            # if prefix is to be global, check everything
                            # if not, check only the same guild
                            '('
                                'new.guildid == 0'
                            ') or ('
                                '('
                                    'new.guildid != 0'
                                ') and ('
                                    'guildid in (0, new.guildid)'
                                ')'
                            ')'
                        ') and ('
                            '('
                                '('
                                    'substr(new.prefix, 1, length(prefix))'
                                    '== prefix'
                                ') and ('
                                    'substr(new.postfix||"_",-1,'
                                    '-length(postfix)) == postfix'
                                ')'
                            ') or ('
                                '('
                                    'substr(prefix, 1, length(new.prefix))'
                                    '== new.prefix'
                                ') and ('
                                    'substr(postfix||"_",-1,'
                                    '-length(new.postfix)) == new.postfix'
                                ')'
                            ')'
                        ')'
                    ')'
                # this exception will be passed to the user
                ') begin select (raise(abort,'
                    '"Those tags conflict with another proxy."'
                ')); end')
        self.execute(
                # NB: this does not trigger if a proxy is inserted with auto = 1
                # including 'insert or replace'
                'create trigger if not exists auto_exclusive '
                'after update of auto on proxies when (new.auto = 1) begin '
                    'update proxies set auto = 0 where ('
                        '(userid = new.userid) and (proxid != new.proxid) and ('
                            # if updated proxy is global, remove all other auto
                                '(new.guildid == 0)'
                            'or'
                            # else, remove auto from global and same guild
                                '((new.guildid != 0) and '
                                '(guildid in (0, new.guildid)))'
                        ')'
                    ');'
                'end')
        self.execute(
                'create table if not exists masks('
                'maskid text primary key collate nocase,'
                'guildid integer,'
                'roleid integer,'
                'nick text,'
                'avatar text,'
                'unique(guildid, roleid))')
        self.execute('pragma secure_delete')


    def __del__(self):
        print('Closing database.')
        self.conn.commit()
        self.conn.close()


    # close on SIGINT, SIGTERM
    def handler(self):
        self.loop.create_task(self.close())
        self.conn.commit()


    def execute(self, *args): return self.cur.execute(*args)
    def fetchone(self, *args): return self.cur.execute(*args).fetchone()
    def fetchall(self, *args): return self.cur.execute(*args).fetchall()


    def has_perm(self, message, **kwargs):
        if not message.guild:
            return True
        # ClientUser.permissions_in() is broken for some reason
        member = message.guild.get_member(self.user.id)
        return discord.Permissions(**kwargs).is_subset(
                member.permissions_in(message.channel))


    async def on_ready(self):
        print('Logged in as %s, id %d!' % (self.user, self.user.id),
                flush = True)
        self.adapter = discord.AsyncWebhookAdapter(aiohttp.ClientSession())
        self.loop.add_signal_handler(signal.SIGINT, self.handler)
        self.loop.add_signal_handler(signal.SIGTERM, self.handler)
        # this could go in __init__ but that would break testing
        self.purge_loop.start()
        await self.change_presence(status = discord.Status.online,
                activity = discord.Game(name = COMMAND_PREFIX + 'help'))


    async def close(self):
        await super().close()
        await self.adapter.session.close()


    @tasks.loop(seconds = PURGE_TIMEOUT)
    async def purge_loop(self):
        when = datetime.now() - timedelta(seconds = PURGE_AGE)
        self.execute('delete from history where deleted = 1 and msgid < ?',
                # this function is undocumented for some reason?
                (discord.utils.time_snowflake(when),))
        self.conn.commit()


    async def try_add_reaction(self, message, reaction):
        if self.has_perm(message, add_reactions = True):
            await message.add_reaction(reaction)


    async def send_embed(self, replyto, text):
        if not self.has_perm(replyto, send_messages = True):
            return
        msg = await replyto.channel.send(
                embed = discord.Embed(description = text))
        # insert into history to allow initiator to delete message if desired
        if replyto.guild:
            await self.try_add_reaction(msg, REACT_DELETE)
            self.execute(
                    'insert into history values (?, 0, ?, NULL, NULL, "", 0)',
                    (msg.id, replyto.author.id))


    def gen_id(self):
        while True:
            # this bit copied from PluralKit, Apache 2.0 license
            id = ''.join(random.choices(string.ascii_lowercase, k=5))
            # IDs don't need to be globally unique but it can't hurt
            exists = self.fetchone(
                    'select exists(select 1 from proxies where proxid = ?)'
                    'or exists(select 1 from masks where maskid = ?)',
                    (id,) * 2)[0]
            if not exists:
                return id


    def on_member_role_add(self, member, role):
        collid = self.fetchone(
                'select maskid from masks where roleid = ?',
                (role.id,))
        if collid:
            self.execute(
                'insert or ignore into proxies values '
                '(?, ?, ?, NULL, NULL, ?, NULL, ?, 0, ?)',
                (self.gen_id(), member.id, member.guild.id,
                    ProxyType.collective, collid[0], ProxyState.active))


    def on_member_role_remove(self, member, role):
        collid = self.fetchone(
                'select maskid from masks where roleid = ?',
                (role.id,))
        if collid:
            self.execute('delete from proxies where (userid, maskid) = (?, ?)',
                    (member.id, collid[0]))


    async def on_guild_role_delete(self, role):
        # no need to delete proxies; on_member_update takes care of that
        self.execute('delete from masks where roleid = ?', (role.id,))


    async def on_member_update(self, before, after):
        if after.bot:
            return
        if len(before.roles) != len(after.roles):
            role = list(set(before.roles) ^ set(after.roles))[0]
            if role in after.roles:
                self.on_member_role_add(after, role)
            else:
                self.on_member_role_remove(after, role)


    # add @everyone collective, if necessary
    async def on_member_join(self, member):
        if not member.bot:
            self.on_member_role_add(member, member.guild.default_role)


    def do_proxy_collective(self, message, proxy, prefs, content):
        if prefs & Prefs.replace:
            # do these in order (or else, e.g. "I'm" could become "We'm")
            # which is funny but not what we want here
            # this could be a reduce() but this is more readable
            for x, y in REPLACEMENTS:
                content = x.sub(y, content)

        return (proxy['nick'], proxy['avatar'], content)


    def do_proxy_swap(self, message, proxy, prefs, content):
        member = message.guild.get_member(proxy['otherid'])
        if member:
            return (member.display_name, member.avatar_url_as(format = 'webp'),
                    content)


    async def do_proxy(self, message, proxy, prefs):
        authid = message.author.id
        channel = message.channel
        msgfile = None

        content = message.content if proxy['auto'] else (message.content[
            len(proxy['prefix']) : -len(proxy['postfix']) or None].strip())
        if content == '' and len(message.attachments) == 0:
            return

        if len(message.attachments) > 0:
            # only copy the first attachment
            attach = message.attachments[0]
            if attach.size <= MAX_FILE_SIZE[message.guild.premium_tier]:
                msgfile = await attach.to_file(
                        spoiler = attach.is_spoiler()
                        # lets mobile users upload with spoilers
                        or content.lower().find('spoiler') != -1)
        # avoid error when user proxies empty message with invalid attachment
        if msgfile == None and content == '':
            return

        embed = None
        if message.reference:
            reference = (message.reference.cached_message or
                    await message.channel.fetch_message(
                        message.reference.message_id))
            if reference:
                embed = discord.Embed(description = (
                    '**[Reply to:](%s)** %s' % (
                        reference.jump_url,
                        # TODO handle markdown
                        reference.clean_content[:100] + (
                            REPLY_CUTOFF if len(reference.clean_content) > 100
                            else ''))
                    if reference.content else
                    '*[(click to see attachment)](%s)*' % reference.jump_url))
                embed.set_author(
                        name = reference.author.display_name + REPLY_SYMBOL,
                        icon_url = reference.author.avatar_url)

        # this should never loop infinitely but just in case
        for ignored in range(2):
            row = self.fetchone('select * from webhooks where chanid = ?',
                    (channel.id,))
            if row == None:
                if self.has_perm(message, manage_webhooks = True):
                    hook = await channel.create_webhook(name = WEBHOOK_NAME)
                else:
                    return await self.send_embed(message,
                            'I need `Manage Webhooks` permission to proxy.')
                self.execute('insert into webhooks values (?, ?, ?)',
                        (channel.id, hook.id, hook.token))
            else:
                hook = discord.Webhook.partial(row[1], row[2],
                        adapter = self.adapter)

            try:
                args = (message, proxy, prefs, content)
                proxtype = proxy['type']
                if proxtype == ProxyType.collective:
                    present = self.do_proxy_collective(*args)
                elif proxtype == ProxyType.swap:
                    present = self.do_proxy_swap(*args)
                else:
                    raise RuntimeError('Unknown proxy type')
                # in case e.g. it's a swap but the other user isn't in the guild
                if present == None:
                    return

                msg = await hook.send(wait = True, username = present[0],
                        avatar_url = present[1], content = present[2],
                        file = msgfile, embed = embed)
            except discord.errors.NotFound:
                # webhook is deleted. delete entry and return to top of loop
                self.execute('delete from webhooks where chanid = ?',
                        (channel.id,))
                continue
            else:
                break

        # deleted = 0
        self.execute('insert into history values (?, ?, ?, ?, ?, ?, 0)',
                (msg.id, channel.id, authid, proxy['otherid'], proxy['maskid'],
                    content if LOG_MESSAGE_CONTENT else ''))

        delay = DELETE_DELAY if prefs & Prefs.delay else None
        await message.delete(delay = delay)

        return msg


    async def on_message(self, message):
        if message.type != discord.MessageType.default or message.author.bot:
            return

        authid = message.author.id
        author = self.fetchone(
                'select * from users where userid = ?',
                (authid,))
        if author == None:
            self.execute('insert into users values (?, ?, ?)',
                    (authid, str(message.author), DEFAULT_PREFS))
            self.execute('insert into proxies values'
                    '(?, ?, 0, NULL, NULL, ?, NULL, NULL, 0, ?)',
                    (self.gen_id(), authid, ProxyType.override,
                        ProxyState.active))
            prefs = DEFAULT_PREFS
        else:
            if author['username'] != str(message.author):
                self.execute(
                        'update users set username = ? where userid = ?',
                        (str(message.author), authid))
            prefs = author['prefs']

        lower = message.content.lower()
        # end of prefix or 0
        offset = len(COMMAND_PREFIX) if lower.startswith(COMMAND_PREFIX) else 0
        # command prefix is optional in DMs
        if offset or not message.guild:
            # strip() so that e.g. 'gs; help' works (helpful with autocorrect)
            try:
                await self.do_command(message, message.content[offset:].strip())
            except (RuntimeError, sqlite.IntegrityError) as e:
                if prefs & Prefs.errors:
                    await self.send_embed(message, e.args[0])
            return

        # this is where the magic happens
        # inactive proxies get matched but only to bypass the current autoproxy
        match = self.fetchone(
                'select p.*, m.nick, m.avatar from ('
                    'select * from proxies where ('
                        '('
                            'userid = ?'
                        ') and ('
                            'guildid in (0, ?)'
                        ') and ('
                            '('
                                # if no tags are set, match nothing
                                # postfix is only NULL when prefix is NULL
                                '('
                                    'prefix not NULL'
                                ') and ('
                                    'substr(?,1,length(prefix)) == prefix'
                                ') and ('
                                    # 'abcd'[-1] == 'c', add another character
                                    'substr(?||"_",-1,-length(postfix)) '
                                    '== postfix'
                                ') and ('
                                    # prevent #text# from matching #
                                    'length(?) '
                                    '>= length(prefix) + length(postfix)'
                                ')'
                            # (tags match) XOR (autoproxy enabled)
                            ') == ('
                                'auto == 0'
                            ')'
                        ')'
                    # if message matches prefix for proxy A but proxy B is auto,
                    # A wins. therefore, rank the proxy with auto = 0 higher
                    ') order by auto asc limit 1'
                ') as p left join masks as m on p.maskid = m.maskid '
                'limit 1',
                (authid, message.guild.id, lower, lower, lower))

        if match and match['state'] == ProxyState.active:
            latch = prefs & Prefs.latch and not match['auto']
            if match['type'] == ProxyType.override:
                if latch:
                    # override can't be auto'd so disable other autos instead
                    self.execute(
                            'update proxies set auto = 0 '
                            'where auto = 1 and userid = ?',
                            (authid,))
            else:
                if self.has_perm(message, manage_messages = True):
                    if await self.do_proxy(message, match, prefs) and latch:
                        self.execute(
                                'update proxies set auto = 1 where proxid = ?',
                                (match['proxid'],))
                else:
                    await self.send_embed(message,
                            'I need `Manage Messages` permission to proxy.')


    # on_reaction_add doesn't catch everything
    async def on_raw_reaction_add(self, payload):
        if payload.user_id == self.user.id:
            return

        # first, make sure this is one of ours
        row = self.fetchone(
            'select authid,'
            '(select username from users where userid = authid) username '
            'from history where msgid = ?',
            (payload.message_id,))
        if row == None:
            return

        reactor = self.get_user(payload.user_id)
        if reactor.bot:
            return
        message = (self.get_channel(payload.channel_id)
                .get_partial_message(payload.message_id))

        emoji = payload.emoji.name
        if emoji == REACT_QUERY:
            try:
                # this can fail depending on user's DM settings & prior messages
                await reactor.send(
                        'Message sent by %s, id %d'
                        % (row['username'], row['authid']))
                await message.remove_reaction(emoji, reactor)
            except discord.errors.Forbidden:
                pass

        elif emoji == REACT_DELETE:
            # only sender may delete proxied message
            if payload.user_id == row['authid']:
                if self.has_perm(message, manage_messages = True):
                    await message.delete()
                else:
                    await self.send_embed(message,
                            'I can\'t delete messages here.')
                # don't delete the entry immediately.
                # purge_loop will take care of it later.
                self.execute(
                        'update history set deleted = 1 where msgid = ?',
                        (payload.message_id,))
            else:
                if self.has_perm(message, manage_messages = True):
                    await message.remove_reaction(emoji, reactor)



def main():
    instance = Gestalt(
            dbfile = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)

    try:
        instance.run(auth.token)
    except RuntimeError:
        print('Runtime error.')

    print('Shutting down.')

if __name__ == '__main__':
    main()

