#!/usr/bin/python3

from functools import reduce
import sqlite3 as sqlite
import asyncio
import random
import signal
import string
import math
import time
import sys
import re

from discord.ext import tasks
import aiohttp
import discord

from defs import *
import commands
import auth
import gesp


class Gestalt(discord.Client, commands.GestaltCommands, gesp.GestaltVoting):
    def __init__(self, *, dbfile):
        super().__init__(intents = INTENTS)

        sqlite.register_adapter(type(CLEAR), lambda _ : None)
        self.conn = sqlite.connect(dbfile)
        self.conn.row_factory = sqlite.Row
        self.cur = self.conn.cursor()
        self.execute(
                'create table if not exists meta('
                'singleton integer unique,'
                'motd text,'
                'check(singleton = 1))')
        self.execute('insert or ignore into meta values (1, "")')
        self.execute(
                'create table if not exists guilds('
                'guildid integer primary key,'
                'logchan integer)')
        self.execute(
                'create table if not exists channels('
                'chanid integer primary key,' # (or thread)
                'guildid integer,'
                'blacklist integer,'    # reserved
                'log integer,'          # also reserved
                'mode integer)')
        self.execute(
                'create table if not exists history('
                'msgid integer primary key,'
                'origid integer,'
                'threadid integer,'
                'chanid integer,'
                'guildid integer,'
                'authid integer,'
                'otherid integer,'
                'proxid text,'
                'maskid text)')
        # for gs;edit
        # to quickly find the last message sent by a user in a channel
        self.execute(
                'create index if not exists history_threadid_chanid_authid '
                'on history(threadid, chanid, authid)')
        self.execute(
                'create table if not exists members('
                'userid integer,'
                'guildid integer,'
                'proxid text collate nocase,'   # same collation for joining
                'latch integer,'    # 0 = off, -1 = on. positive values reserved
                'become real,'      # 1.0 except in Become mode
                'primary key(userid, guildid),'
                'check(proxid not null or become >= 1.0))')
        self.execute(
                'create table if not exists users('
                'userid integer primary key,'
                'username text,'
                'prefs integer,'
                'tag text,' # reserved
                'color text)')
        self.execute(
                'create table if not exists webhooks('
                'chanid integer primary key,'
                'hookid integer unique,'
                'token text)')
        # note that collective proxies store both the roleid and maskid
        # because collectives might not always be tied to a role
        self.execute(
                'create table if not exists proxies('
                'proxid text primary key collate nocase,'   # of form 'abcde'
                'cmdname text collate nocase,'
                'userid integer,'
                'guildid integer,'              # 0 for swaps, overrides
                'prefix text,'
                'postfix text,'
                'type integer,'                 # see enum ProxyType
                'otherid integer,'              # roleid or userid for swaps
                'maskid text collate nocase,'   # same collation for joining
                'flags integer,'                # see enum ProxyFlags
                'state integer,'                # see enum ProxyState
                'created integer,'              # unix timestamp
                'msgcount integer,'             # reserved
                'unique(maskid, userid))')
        # for fast proxy deletion on role removal
        self.execute(
                'create index if not exists proxies_userid_otherid '
                'on proxies(userid, otherid)')
        self.execute(
                'create table if not exists guildmasks('
                'maskid text collate nocase,'
                'guildid integer,'
                'roleid integer,'
                'nick text,'
                'avatar text,'
                'color text,'
                'type integer,'     # also uses enum ProxyType
                'created integer,'  # unix timestamp
                'updated integer,'  # snowflake; for future automatic pk sync
                'msgcount integer,' # reserved
                'unique(maskid, guildid),'
                'unique(guildid, roleid))')
        self.execute(
                'create index if not exists guildmasks_roleid '
                'on guildmasks(roleid) where type = %i;'
                % ProxyType.collective)
        self.execute(
                'create table if not exists masks('
                'maskid text primary key collate nocase,'
                'nick text,'
                'avatar text,'
                'color text,'
                'rules text,'
                'created integer,'
                'members integer,'
                'msgcount integer)')
        self.execute(
                'create trigger if not exists mask_proxy_create '
                'after insert on proxies when (new.type = %i) begin '
                    'update masks set members = members + 1 '
                    'where maskid = new.maskid;'
                'end' % ProxyType.mask)
        self.execute(
                'create trigger if not exists mask_proxy_delete '
                'after delete on proxies when (old.type = %i) begin '
                    'update masks set members = members - 1 '
                    'where maskid = old.maskid;'
                'end' % ProxyType.mask)
        self.execute(
                'create table if not exists votes('
                'msgid integer primary key,'
                'state text)')
        self.execute(
                'create table if not exists deleted('
                'id text unique collate nocase'
                ')')
        # make these temp to avoid interference during maintenance
        self.execute(
                'create temp trigger delete_proxy '
                'after delete on proxies begin '
                    'insert into deleted values (old.proxid);'
                'end')
        self.execute(
                'create temp trigger delete_mask '
                'after delete on masks begin '
                    'insert into deleted values (old.maskid);'
                'end')
        # NOTE: this would include masks if masks can be removed from guilds
        self.execute(
                'create temp trigger delete_guildmask '
                'after delete on guildmasks begin '
                    'insert into deleted values (old.maskid);'
                'end')

        self.ignore_delete_cache = set()
        self.load()


    def __del__(self):
        self.save()
        self.log('Closing database.')
        self.conn.commit()
        self.conn.close()


    # close on SIGINT, SIGTERM
    def handler(self):
        self.loop.create_task(self.close())
        self.conn.commit()


    def log(self, text, *args):
        print(text % args, flush = True)


    def execute(self, *args): return self.cur.execute(*args)
    def fetchone(self, *args): return self.cur.execute(*args).fetchone()
    def fetchall(self, *args): return self.cur.execute(*args).fetchall()


    # turns out there's no applicable situation that uses fetchone() yet
    # so only the fetchall() version is defined
    def fetch_valid_proxies(self, *args):
        return [proxy for proxy in self.cur.execute(*args).fetchall()
                if self.delete_invalid_proxy(proxy)]


    def has_perm(self, channel, **kwargs):
        if not channel.guild:
            return True
        member = channel.guild.get_member(self.user.id)
        return discord.Permissions(**kwargs).is_subset(
                channel.permissions_for(member))


    async def setup_hook(self):
        self.log('Logged in as %s, id %d!', self.user, self.user.id)
        self.session = aiohttp.ClientSession()
        # if it ain't broke don't fix it
        # (except it does seem slightly broken? it needs the -1 in testing)
        # (but this works and pkapi is *usually* too slow for it to matter)
        self.pk_ratelimit = discord.gateway.GatewayRatelimiter(
                count = PK_RATELIMIT - 1, per = PK_WINDOW)
        self.pk_ratelimit.shard_id = 'PluralKit'
        self.loop.add_signal_handler(signal.SIGINT, self.handler)
        self.loop.add_signal_handler(signal.SIGTERM, self.handler)
        # this could go in __init__ but that would break testing
        self.sync_loop.start()


    async def update_status(self):
        motd = self.fetchone('select motd from meta')['motd']
        await self.change_presence(status = discord.Status.online,
                activity = discord.Game(name =
                    '%shelp%s' % (COMMAND_PREFIX, (motd and ' | %s' % motd))))


    async def on_ready(self):
        self.log('In %i guild(s).', len(self.guilds))
        self.owner = (await self.application_info()).owner.id
        await self.update_status()


    async def close(self):
        await self.session.close()
        await super().close()


    @tasks.loop(seconds = SYNC_TIMEOUT)
    async def sync_loop(self):
        self.conn.commit()
        self.ignore_delete_cache.clear()


    async def try_delete(self, message, delay = None):
        if self.has_perm(message.channel, manage_messages = True):
            try:
                await message.delete(delay = delay)
            except discord.errors.NotFound:
                # task failed successfully
                # this might indicate a conflict with another proxy bot
                # pk handles this by deleting a proxied message
                # ...but if we do that too, it might mean neither bot wins
                # so just ignore it
                pass
            return True


    async def try_add_reaction(self, message, reaction):
        if self.has_perm(message.channel, add_reactions = True,
                read_message_history = True):
            try:
                await message.add_reaction(reaction)
                return True
            except discord.errors.NotFound:
                pass


    async def mark_success(self, message, success):
        await self.try_add_reaction(message,
                REACT_CONFIRM if success else REACT_DELETE)


    class InProgress:
        def __init__(self, client, message):
            (self.client, self.message) = (client, message)
        async def __aenter__(self):
            await self.client.try_add_reaction(self.message, REACT_WAIT)
        async def __aexit__(self, *args):
            try:
                await self.message.remove_reaction(REACT_WAIT, self.client.user)
            except discord.errors.NotFound:
                pass


    def in_progress(self, message):
        return self.InProgress(self, message)


    async def send_embed(self, channel, text, view = None, reference = None):
        if self.has_perm(channel, send_messages = True):
            return await channel.send(
                    embed = discord.Embed(description = text), view = view,
                    reference = reference)


    async def reply(self, replyto, text):
        msg = await self.send_embed(replyto.channel, text)
        # insert into history to allow initiator to delete message if desired
        if replyto.guild:
            await self.try_add_reaction(msg, REACT_DELETE)
            self.mkhistory(msg, replyto.author.id)
        return msg


    def gen_id(self):
        while True:
            # this bit copied from PluralKit, Apache 2.0 license
            id = ''.join(random.choices(string.ascii_lowercase, k=5))
            # IDs don't need to be globally unique but it can't hurt
            exists = self.fetchone(
                    'select exists(select 1 from proxies where proxid = ?)'
                    'or exists(select 1 from masks where maskid = ?)'
                    'or exists(select 1 from guildmasks where maskid = ?)'
                    'or exists(select 1 from deleted where id = ?)',
                    (id,) * 4)[0]
            if not exists:
                return id


    def mkproxy(self, userid, proxtype, cmdname = '', guildid = 0,
            prefix = None, postfix = None, otherid = None, maskid = None,
            flags = ProxyFlags(0), state = ProxyState.active):
        if prefix is not None and self.get_tags_conflict(userid, guildid,
                (prefix, postfix)):
            raise UserError(ERROR_TAGS)
        self.execute(
                'insert into proxies values '
                '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)',
                (proxid := self.gen_id(), cmdname, userid, guildid,
                    prefix, postfix, proxtype, otherid, maskid,
                    flags, state, int(time.time())))
        return proxid


    def mkhistory(self, message, authid, channel = None, orig = None,
            proxy = {'otherid': None, 'proxid': None, 'maskid': None}):
        if channel:
            (threadid, chanid) = ((channel.id, channel.parent.id)
                    if type(channel) == discord.Thread
                    else (0, channel.id))
            guildid = channel.guild.id
        else:
            (threadid, chanid, guildid) = (0, 0, 0)
        self.execute('insert into history values (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (message.id, orig, threadid, chanid, guildid, authid,
                    proxy['otherid'], proxy['proxid'], proxy['maskid']))


    def init_member(self, member):
        self.execute(
                'insert or ignore into members values '
                '(?, ?, NULL, 0, 1.0)',
                (member.id, member.guild.id))


    def set_autoproxy(self, member, proxid, latch = None, become = 1.0):
        self.init_member(member)
        self.execute(
                'update members set (proxid, become) = (?, ?) '
                'where (userid, guildid) = (?, ?)',
                (proxid, become, member.id, member.guild.id))
        if latch is not None:
            self.execute('update members set latch = ? '
                    'where (userid, guildid) = (?, ?)',
                    (latch, member.id, member.guild.id))


    def get_tags_conflict(self, userid, guildid, pair):
        (prefix, postfix) = pair
        return [proxy['proxid'] for proxy in self.fetch_valid_proxies(
            'select * from proxies where userid = ?', (userid,))
            if proxy['prefix'] is not None
            # if prefix is to be global, check everything
            # if not, check only the same guild
            and (guildid == 0 or (guildid and proxy['guildid'] in (0, guildid)))
            and ((prefix.startswith(proxy['prefix'])
                and postfix.endswith(proxy['postfix']))
                or (proxy['prefix'].startswith(prefix)
                    and proxy['postfix'].endswith(postfix)))]


    async def on_guild_join(self, guild):
        for member in guild.members:
            if not member.bot:
                await self.try_auto_add_member(member)


    async def on_webhooks_update(self, channel):
        if hook := await self.get_webhook(channel):
            await self.confirm_webhook_deletion(hook)


    def on_member_role_add(self, member, role):
        mask = self.fetchone(
                'select maskid, nick from guildmasks '
                'where (roleid, type) = (?, ?)',
                (role.id, ProxyType.collective)) # uses index
        if mask:
            try:
                self.mkproxy(member.id, ProxyType.collective,
                        cmdname = mask['nick'], guildid = member.guild.id,
                        otherid = role.id, maskid = mask['maskid'])
            except sqlite.IntegrityError:
                pass


    def on_member_role_remove(self, member, role):
        self.execute('delete from proxies where (userid, otherid) = (?, ?)',
                (member.id, role.id))


    async def on_guild_role_delete(self, role):
        # no need to delete proxies; on_member_update takes care of that
        self.execute('delete from guildmasks where (roleid, type) = (?, ?)',
                (role.id, ProxyType.collective))


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


    async def on_member_join(self, member):
        if member.bot:
            return

        # add @everyone collective, if necessary
        self.on_member_role_add(member, member.guild.default_role)
        await self.try_auto_add_member(member)


    async def on_raw_member_remove(self, payload):
        self.execute('delete from proxies where (userid, guildid) = (?, ?)',
                (payload.user.id, payload.guild_id))


    def get_proxy_collective(self, message, proxy, prefs, content):
        if prefs & Prefs.replace:
            # do these in order (or else, e.g. "I'm" could become "We'm")
            # which is funny but not what we want here
            # this could be a reduce() but this is more readable
            for x, y in REPLACEMENTS:
                content = x.sub(y, content)

        mask = self.fetchone('select * from guildmasks where maskid = ?',
                (proxy['maskid'],))
        return {'username': mask['nick'],
                'avatar_url': mask['avatar'],
                'color': mask['color'],
                'content': content}


    def get_proxy_swap(self, message, proxy, prefs, content):
        member = message.guild.get_member(proxy['otherid'])
        if member:
            color = self.fetchone('select color from users where userid = ?',
                    (member.id,))['color']
            return {'username': member.display_name,
                    'avatar_url': member.display_avatar.replace(
                        format = 'webp'),
                    'color': color,
                    'content': content}


    def get_proxy_pkswap(self, message, proxy, prefs, content):
        if not message.guild.get_member(proxy['otherid']):
            return
        mask = self.fetchone(
                'select * from guildmasks where (guildid, maskid) = (?, ?)',
                (message.guild.id, 'pk-' + proxy['maskid']))
        if mask:
            return {'username': mask['nick'],
                    'avatar_url': mask['avatar'],
                    'color': mask['color'],
                    'content': content}
        raise UserError('That proxy has not been synced yet.')


    def get_proxy_mask(self, message, proxy, prefs, content):
        if mask := self.fetchone(
                'select masks.nick, masks.avatar, masks.color '
                'from guildmasks left join masks using (maskid) '
                'where (guildid, maskid) = (?, ?)',
                (message.guild.id, proxy['maskid'])):
            return {'username': mask['nick'],
                    'avatar_url': mask['avatar'],
                    'color': mask['color'],
                    'content': content}


    def maybe_remove_embeds(self, message, content):
        if message.channel.permissions_for(message.author).embed_links:
            return content
        return LINK_REGEX.sub(
                lambda match: match.group(0)
                if (match.group(0).startswith('<')
                    and match.group(0).endswith('>'))
                else '<%s>' % match.group(0),
                content)


    async def get_webhook(self, channel, create = False):
        if type(channel) == discord.Thread:
            channel = channel.parent
        if row := self.fetchone('select * from webhooks where chanid = ?',
                (channel.id,)):
            return discord.Webhook.partial(row[1], row[2],
                    session = self.session)
        if create:
            hook = await channel.create_webhook(name = WEBHOOK_NAME)
            self.execute('insert into webhooks values (?, ?, ?)',
                    (channel.id, hook.id, hook.token))
            return hook


    async def confirm_webhook_deletion(self, hook):
        # this is rare so we can afford an extra call to be really sure
        try:
            await hook.fetch()
        except discord.errors.NotFound:
            self.execute('delete from webhooks where hookid = ?', (hook.id,))
            return True
        else:
            self.log('False NotFound for webhook %i', hook.id)


    async def execute_webhook(self, channel, **kwargs):
        hook = await self.get_webhook(channel, create = True)
        try:
            return (await hook.send(wait = True, **kwargs), hook)
        except discord.errors.NotFound:
            if await self.confirm_webhook_deletion(hook):
                # webhook is deleted
                hook = await self.get_webhook(channel, create = True)
                return (await hook.send(wait = True, **kwargs), hook)


    async def make_log_message(self, message, orig, proxy = None, old = None):
        logchan = self.fetchone('select logchan from guilds where guildid = ?',
                (orig.guild.id,))
        if not logchan:
            return

        embed = discord.Embed(description = message.content,
                timestamp = discord.utils.snowflake_time(orig.id))
        embed.set_author(name = '%s#%s: %s' %
                ('[Edited] ' if old else '', orig.channel.name,
                    message.author.display_name),
                icon_url = message.author.display_avatar)
        if old:
            embed.add_field(name = 'Old message', value = old.content,
                    inline = False)
        embed.set_thumbnail(url = message.author.display_avatar)
        footer = ('Sender: %s (%i) | Message ID: %i | Original Message ID: %i'
                % (str(orig.author), orig.author.id, message.id, orig.id))
        if proxy:
            footer = (('Collective ID: %s | ' % proxy['maskid']
                if proxy['type'] == ProxyType.collective else '')
                + 'Proxy ID: %s | ' % proxy['proxid']) + footer
        embed.set_footer(text = footer)
        try:
            await self.get_channel(logchan[0]).send(
                    # jump_url doesn't work in messages from webhook.send()
                    # (and .channel can be PartialMessageable)
                    # (that was annoying)
                    message.channel.get_partial_message(message.id).jump_url,
                    embed = embed)
        except:
            pass


    async def do_proxy(self, message, content, proxy, prefs):
        authid = message.author.id
        channel = message.channel
        msgfiles = []

        if message.attachments:
            totalsize = sum((x.size for x in message.attachments))
            if totalsize <= MAX_FILE_SIZE[message.guild.premium_tier]:
                # defer downloading attachments until after other checks
                msgfiles = (await attach.to_file(spoiler = attach.is_spoiler())
                        for attach in message.attachments)
        # avoid error when user proxies empty message with invalid attachments
        if msgfiles == [] and content == '':
            return

        args = (message, proxy, prefs, self.maybe_remove_embeds(message,
            content))
        proxtype = proxy['type']
        if proxtype == ProxyType.collective:
            present = self.get_proxy_collective(*args)
        elif proxtype == ProxyType.swap:
            present = self.get_proxy_swap(*args)
        elif proxtype == ProxyType.pkswap:
            present = self.get_proxy_pkswap(*args)
        elif proxtype == ProxyType.mask:
            present = self.get_proxy_mask(*args)
        else:
            raise UserError('Unknown proxy type')
        # in case e.g. it's a swap but the other user isn't in the guild
        if present == None:
            return

        # now that we know the proxy can be used here, do Become mode stuff
        if proxy['become'] is not None and proxy['become'] < 1.0:
            self.set_autoproxy(message.author, proxy['proxid'],
                    become = proxy['become'] + 1/BECOME_MAX)
            if random.random() > proxy['become']:
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
                if present['color']:
                    embed.color = discord.Color.from_str(present['color'])
                embed.set_author(
                        name = reference.author.display_name + REPLY_SYMBOL,
                        icon_url = reference.author.display_avatar)
        del present['color']

        thread = (channel if type(channel) == discord.Thread
                else discord.utils.MISSING)
        am = discord.AllowedMentions(everyone = channel.permissions_for(
            message.author).mention_everyone)
        try:
            (new, hook) = await self.execute_webhook(channel, thread = thread,
                    files = msgfiles and [i async for i in msgfiles],
                    embed = embed, allowed_mentions = am, **present)
        except discord.errors.Forbidden:
            raise UserError('I need `Manage Webhooks` permission to proxy.')

        self.mkhistory(new, message.author.id, channel = message.channel,
                orig = message.id, proxy = proxy)

        if not proxy['flags'] & ProxyFlags.echo:
            await self.try_delete(message, delay = DELETE_DELAY
                    if prefs & Prefs.delay else None)

        # try to automatically fix external emojis
        # custom emojis are of the form /<:[a-zA-Z90-9_~]+:[0-9]+>/
        # when proxied they are usually replaced with the :name: part
        # so all we have to do is count the angle brackets
        # (don't compare content != content bc not sure what else might change)
        if new.content.count('<') != present['content'].count('<'):
            try:
                new = await hook.edit_message(new.id,
                        content = present['content'], thread = thread,
                        allowed_mentions = am)
            except:
                pass

        await self.make_log_message(new, message, proxy)

        return new


    def init_user(self, user):
        self.execute('insert into users values (?, ?, ?, "", NULL)',
                (user.id, str(user), DEFAULT_PREFS))
        self.mkproxy(user.id, ProxyType.override)


    def delete_invalid_proxy(self, proxy):
        if not self.is_ready():
            return proxy
        if proxy['type'] == ProxyType.collective:
            if not (guild := self.get_guild(proxy['guildid'])):
                return # assume it's temporarily unavailable
            if not ((member := guild.get_member(proxy['userid']))
                    and proxy['otherid'] in (role.id for role in member.roles)):
                self.execute('delete from proxies where proxid = ?',
                        (proxy['proxid'],))
                self.log('Deleted proxy %s', proxy['proxid'])
                return
        return proxy


    def proxy_visible_in(self, proxy, guild):
        if proxy['state'] == ProxyState.hidden:
            return False
        if proxy['type'] == ProxyType.override:
            return True
        elif proxy['type'] == ProxyType.collective:
            return proxy['guildid'] == guild.id
        elif proxy['type'] in (ProxyType.swap, ProxyType.pkswap,
                ProxyType.pkreceipt):
            return bool(guild.get_member(proxy['otherid']))
        elif proxy['type'] == ProxyType.mask:
            return guild.id in self.mask_presence[proxy['maskid']]
        return False


    def proxy_usable_in(self, proxy, guild):
        if not self.proxy_visible_in(proxy, guild):
            return False
        if proxy['state'] != ProxyState.active:
            return False
        elif proxy['type'] == ProxyType.pkreceipt:
            return False
        return True


    def get_proxy_match(self, message):
        # this is where the magic happens
        # inactive proxies get matched but only to bypass the current autoproxy
        lower = message.content.lower()
        proxies = self.fetch_valid_proxies(
                'select * from proxies where userid = ? and guildid in (0, ?)',
                (message.author.id, message.guild.id))
        # this can't be a join because we need it even if there's no proxy set
        while not (member := self.fetchone(
            'select proxid as ap, latch, become from members '
            'where (userid, guildid) = (?, ?)',
            (message.author.id, message.guild.id))):
            self.init_member(message.author)
        if not (tags := bool(match := discord.utils.find(
            lambda proxy : (proxy['prefix'] is not None
                and lower.startswith(proxy['prefix'])
                and lower.endswith(proxy['postfix'])),
            proxies))):
            match = discord.utils.find(
                    lambda proxy : proxy['proxid'] == member['ap'],
                    proxies)
            if match and not self.proxy_usable_in(match, message.guild):
                self.set_autoproxy(message.author, None)
                return
        if match:
            return (dict(match) | dict(member),
                    (message.content[
                        len(match['prefix']) : -len(match['postfix']) or None
                        ].strip()
                        if tags and match['flags'] & ProxyFlags.keepproxy == 0
                        else message.content),
                    tags)


    async def on_user_message(self, message, user):
        authid = message.author.id
        content = message.content
        chan = self.fetchone('select * from channels where chanid = ?',
                (message.channel.id,))
        mandatory = chan and chan['mode'] == ChannelMode.mandatory
        # command prefix is optional in DMs
        if (match := COMMAND_REGEX.fullmatch(content)) or not message.guild:
            if mandatory:
                await self.try_delete(message)
                raise UserError(
                        'You cannot use commands in a Mandatory mode channel.')

            # init user if hasn't been init'd yet
            # it's impossible for the row to matter before they use a command
            if not user:
                self.init_user(message.author)
            # strip() so that e.g. 'gs; help' works (helpful with autocorrect)
            await self.do_command(message,
                    match[1].strip() if match else content)
            return

        if not user:
            return # if user isn't init'd, they can't have any proxies

        if message.guild and user['prefs'] & Prefs.homestuck and (match :=
                BE_REGEX.fullmatch(content)):
            try:
                return await self.cmd_autoproxy_set(message, match[1], True)
            except:
                pass

        (match, stripped, tags) = (self.get_proxy_match(message)
                or (None, None, None))

        # note: pkswaps with own account are intentionally allowed
        if mandatory and (not match or match['state'] != ProxyState.active
                or match['type'] == ProxyType.override or
                (match['type'], match['otherid']) == (ProxyType.swap, authid)):
            await self.try_delete(message)
            return

        if not match or match['state'] != ProxyState.active:
            return

        try:
            msg = None
            prefs = user['prefs']
            if content.startswith('\\') and not tags:
                if content.startswith('\\\\\\'):
                    self.set_autoproxy(message.author, None, latch = 0)
                elif content.startswith('\\\\') and match['latch']:
                    self.set_autoproxy(message.author, None)
                return

            latch = match['latch'] and match['proxid'] != match['ap']
            if match['type'] == ProxyType.override:
                if latch:
                    self.set_autoproxy(message.author, None)
                return
            if not self.has_perm(message.channel, manage_messages = True):
                raise UserError('I need `Manage Messages` permission to proxy.')
            msg = await self.do_proxy(message, stripped, match, prefs)
            if msg and latch:
                self.set_autoproxy(message.author, match['proxid'])
        finally:
            # if the proxy couldn't be used in this channel
            # (unsynced pkswap, swap with non-member)
            if not msg and mandatory:
                await self.try_delete(message)


    async def on_message(self, message):
        if (message.channel.type not in ALLOWED_CHANNELS
                or message.author.id == self.user.id):
            return
        # we don't know without a query if any given webhook message is ours
        # but we do know that any non-webhook message definitely isn't
        # so if it is deleted, save a db call in on_raw_message_delete
        # (this could be significant with other delete-heavy bots like PK)
        authid = message.author.id # if webhook then webhook id
        if authid != self.user.id and not message.webhook_id:
            self.ignore_delete_cache.add(message.id)
        if (message.type in (discord.MessageType.default,
            discord.MessageType.reply)
            and not message.author.bot):
            user = self.fetchone('select * from users where userid = ?',
                    (authid,))
            try:
                await self.on_user_message(message, user)
            except UserError as e:
                # an uninit'd user shouldn't ever get errors, but just in case
                if ((user and user['prefs']) or DEFAULT_PREFS) & Prefs.errors:
                    await self.reply(message, e.args[0])
            # do this after because it's less important than proxying
            if user and user['username'] != str(message.author):
                self.execute('update users set username = ? where userid = ?',
                        (str(message.author), authid))


    # these are needed for gs;edit to work
    async def on_raw_message_delete(self, payload):
        if (msgid := payload.message_id) in self.ignore_delete_cache:
            self.ignore_delete_cache.remove(msgid)
            return
        if msgid in self.votes:
            del self.votes[msgid]
        self.execute('delete from history where msgid = ?', (msgid,))


    async def on_raw_bulk_message_delete(self, payload):
        for id in payload.message_ids:
            await self.on_raw_message_delete(
                    discord.raw_models.RawMessageDeleteEvent(
                        {'id': id, 'channel_id': payload.channel_id}))


    # on_reaction_add doesn't catch everything
    async def on_raw_reaction_add(self, payload):
        if payload.user_id == self.user.id:
            return

        channel = (self.get_channel(payload.channel_id)
                or await self.fetch_channel(payload.channel_id))
        message = channel.get_partial_message(payload.message_id)
        emoji = payload.emoji.name
        if channel.guild:
            # make sure this is one of ours
            row = self.fetchone(
                'select authid, otherid, username '
                'from history left join users on userid = authid '
                'where msgid = ?',
                (payload.message_id,))
            if row == None:
                return
        else:
            if emoji == REACT_DELETE:
                # just to be sure
                if (await message.fetch()).author == self.user:
                    await message.delete()
            return


        reactor = self.get_user(payload.user_id)
        if reactor.bot:
            return

        if emoji == REACT_QUERY:
            try:
                author = str(self.get_user(row['authid'])
                        or await self.fetch_user(row['authid']))
            except discord.errors.NotFound:
                author = row['username']

            try:
                # this can fail depending on user's DM settings & prior messages
                await reactor.send(
                        'Message sent by %s (id %d)' % (
                            discord.utils.escape_markdown(author),
                            row['authid']))
                await message.remove_reaction(emoji, reactor)
            except discord.errors.Forbidden:
                pass

        elif emoji == REACT_DELETE:
            # sender or swapee may delete proxied message
            if payload.user_id in (row['authid'], row['otherid']):
                if not await self.try_delete(message):
                    await self.reply(message,
                            'I can\'t delete messages here.')
            else:
                if self.has_perm(message.channel, manage_messages = True):
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

