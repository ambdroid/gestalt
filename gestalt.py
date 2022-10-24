#!/usr/bin/python3

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
                'create table if not exists guilds('
                'guildid integer primary key,'
                'logchan integer)')
        self.execute(
                'create table if not exists history('
                'msgid integer primary key,'
                'chanid integer,'
                'authid integer,'
                'otherid integer,'
                'maskid text)')
        # for gs;edit
        # to quickly find the last message sent by a user in a channel
        self.execute(
                'create index if not exists history_chanid_authid '
                'on history(chanid, authid)')
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
                'cmdname text collate nocase,'
                'userid integer,'
                'guildid integer,'          # 0 for swaps, overrides
                'prefix text,'
                'postfix text,'
                'type integer,'             # see enum ProxyType
                'otherid integer,'          # userid for swaps
                'maskid text,'
                'auto integer,'             # 0/1
                'become real,'              # 1.0 except in Become mode
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
        print('In %i guild(s).' % len(self.guilds), flush = True)
        self.adapter = discord.AsyncWebhookAdapter(aiohttp.ClientSession())
        self.loop.add_signal_handler(signal.SIGINT, self.handler)
        self.loop.add_signal_handler(signal.SIGTERM, self.handler)
        # this could go in __init__ but that would break testing
        self.sync_loop.start()
        await self.change_presence(status = discord.Status.online,
                activity = discord.Game(name = COMMAND_PREFIX + 'help'))


    async def close(self):
        await super().close()
        await self.adapter.session.close()


    @tasks.loop(seconds = SYNC_TIMEOUT)
    async def sync_loop(self):
        self.conn.commit()


    async def mark_success(self, message, success):
        if self.has_perm(message, add_reactions = True):
            await message.add_reaction(
                    REACT_CONFIRM if success else REACT_DELETE)


    async def send_embed(self, replyto, text):
        if not self.has_perm(replyto, send_messages = True):
            return
        msg = await replyto.channel.send(
                embed = discord.Embed(description = text))
        # insert into history to allow initiator to delete message if desired
        if replyto.guild:
            if self.has_perm(msg, add_reactions = True):
                await msg.add_reaction(REACT_DELETE)
            self.execute('insert into history values (?, 0, ?, NULL, NULL)',
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
        mask = self.fetchone(
                'select maskid, nick from masks where roleid = ?',
                (role.id,))
        if mask:
            self.execute(
                'insert or ignore into proxies values '
                '(?, ?, ?, ?, NULL, NULL, ?, NULL, ?, 0, 1.0, ?)',
                (self.gen_id(), mask['nick'], member.id, member.guild.id,
                    ProxyType.collective, mask['maskid'], ProxyState.active))


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
        # not sure if more than one role can change at a time
        # but this needs to be airtight
        for role in set(before.roles) ^ set(after.roles):
            if role in after.roles:
                self.on_member_role_add(after, role)
            else:
                self.on_member_role_remove(after, role)


    # add @everyone collective, if necessary
    async def on_member_join(self, member):
        if not member.bot:
            self.on_member_role_add(member, member.guild.default_role)


    def get_proxy_collective(self, message, proxy, prefs, content):
        if prefs & Prefs.replace:
            # do these in order (or else, e.g. "I'm" could become "We'm")
            # which is funny but not what we want here
            # this could be a reduce() but this is more readable
            for x, y in REPLACEMENTS:
                content = x.sub(y, content)

        mask = self.fetchone('select nick, avatar from masks where maskid = ?',
                (proxy['maskid'],))
        return {'username': mask['nick'], 'avatar_url': mask['avatar'],
                'content': content}


    def get_proxy_swap(self, message, proxy, prefs, content):
        member = message.guild.get_member(proxy['otherid'])
        if member:
            return {'username': member.display_name,
                    'avatar_url': member.avatar_url_as(format = 'webp'),
                    'content': content}


    async def get_webhook(self, message):
        channel = message.channel
        row = self.fetchone('select * from webhooks where chanid = ?',
                (channel.id,))
        if row == None:
            hook = await channel.create_webhook(name = WEBHOOK_NAME)
            self.execute('insert into webhooks values (?, ?, ?)',
                    (channel.id, hook.id, hook.token))
        else:
            hook = discord.Webhook.partial(row[1], row[2],
                    adapter = self.adapter)
        return hook


    async def do_proxy(self, message, proxy, prefs):
        authid = message.author.id
        channel = message.channel
        msgfiles = []

        content = (message.content[
            len(proxy['prefix']) : -len(proxy['postfix']) or None].strip()
            if proxy['matchTags'] else message.content)

        if message.attachments:
            totalsize = sum((x.size for x in message.attachments))
            if totalsize <= MAX_FILE_SIZE[message.guild.premium_tier]:
                msgfiles = [await attach.to_file(spoiler = attach.is_spoiler())
                        for attach in message.attachments]
        # avoid error when user proxies empty message with invalid attachments
        if msgfiles == [] and content == '':
            return

        embed = None
        if message.reference:
            try:
                reference = (message.reference.cached_message or
                        await message.channel.fetch_message(
                            message.reference.message_id))
            except discord.errors.NotFound:
                reference = None
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

        args = (message, proxy, prefs, content)
        proxtype = proxy['type']
        if proxtype == ProxyType.collective:
            present = self.get_proxy_collective(*args)
        elif proxtype == ProxyType.swap:
            present = self.get_proxy_swap(*args)
        else:
            raise RuntimeError('Unknown proxy type')
        # in case e.g. it's a swap but the other user isn't in the guild
        if present == None:
            return

        # now that we know the proxy can be used here, do Become mode stuff
        if proxy['become'] < 1.0:
            self.execute(
                    'update proxies set become = ? where proxid = ?',
                    (proxy['become'] + 1/BECOME_MAX, proxy['proxid']))
            if random.random() > proxy['become']:
                return

        try:
            hook = await self.get_webhook(message)
            try:
                msg = await hook.send(wait = True, files = msgfiles,
                        embed = embed, **present)
            except discord.errors.NotFound:
                # webhook is deleted
                self.execute('delete from webhooks where chanid = ?',
                        (channel.id,))
                hook = await self.get_webhook(message)
                msg = await hook.send(wait = True, files = msgfiles,
                        embed = embed, **present)
        except discord.errors.Forbidden:
            return await self.send_embed(message,
                    'I need `Manage Webhooks` permission to proxy.')

        self.execute('insert into history values (?, ?, ?, ?, ?)',
                (msg.id, channel.id, authid, proxy['otherid'], proxy['maskid']))

        delay = DELETE_DELAY if prefs & Prefs.delay else 0.0
        await message.delete(delay = delay)

        logchan = self.fetchone('select logchan from guilds where guildid = ?',
                (message.guild.id,))
        if logchan:
            logchan = logchan[0]
            embed = discord.Embed(description = present['content'],
                    timestamp = discord.utils.snowflake_time(message.id))
            embed.set_author(name = '#%s: %s' %
                    (channel.name, present['username']),
                    icon_url = present['avatar_url'])
            embed.set_thumbnail(url = present['avatar_url'])
            embed.set_footer(text =
                    ('Collective ID: %s | ' % proxy['maskid']
                        if proxy['maskid'] else '') +
                    'Proxy ID: %s | '
                    'Sender: %s (%i) | '
                    'Message ID: %i | '
                    'Original Message ID: %i'
                    % (proxy['proxid'], str(message.author), authid, msg.id,
                        message.id))
            try:
                await self.get_channel(logchan).send(
                        # jump_url doesn't work in messages from webhook.send()
                        channel.get_partial_message(msg.id).jump_url,
                        embed = embed)
            except:
                pass

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
                    '(?, "", ?, 0, NULL, NULL, ?, NULL, NULL, 0, 1.0, ?)',
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
        # command prefix is optional in DMs
        if lower.startswith(COMMAND_PREFIX) or not message.guild:
            # strip() so that e.g. 'gs; help' works (helpful with autocorrect)
            try:
                await self.do_command(message,
                        message.content.removeprefix(COMMAND_PREFIX).strip())
            except (RuntimeError, sqlite.IntegrityError) as e:
                if prefs & Prefs.errors:
                    await self.send_embed(message, e.args[0])
            return

        # this is where the magic happens
        # inactive proxies get matched but only to bypass the current autoproxy
        proxies = self.fetchall(
                'select * from proxies where userid = ? and guildid in (0, ?)',
                (authid, message.guild.id))
        if not (tags := bool(match := discord.utils.find(
            lambda proxy : (proxy['prefix'] is not None
                and lower.startswith(proxy['prefix'])
                and lower.endswith(proxy['postfix'])),
            proxies))):
            match = discord.utils.find(lambda proxy : proxy['auto'] == 1,
                    proxies)

        if match and (match := dict(match))['state'] == ProxyState.active:
            match['matchTags'] = tags
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


    # these are needed for gs;edit to work
    async def on_raw_message_delete(self, payload):
        self.execute('delete from history where msgid = ?',
                (payload.message_id,))


    async def on_raw_bulk_message_delete(self, payload):
       self.cur.executemany('delete from history where msgid = ?',
               ((x,) for x in payload.message_ids))


    # on_reaction_add doesn't catch everything
    async def on_raw_reaction_add(self, payload):
        if payload.user_id == self.user.id:
            return

        # first, make sure this is one of ours
        row = self.fetchone(
            'select authid, otherid,'
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
            # sender or swapee may delete proxied message
            if payload.user_id in (row['authid'], row['otherid']):
                if self.has_perm(message, manage_messages = True):
                    await message.delete()
                else:
                    await self.send_embed(message,
                            'I can\'t delete messages here.')
                self.execute(
                        'delete from history where msgid = ?',
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

