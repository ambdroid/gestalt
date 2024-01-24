#!/usr/bin/python3

from datetime import timedelta
from functools import reduce
from asyncio import run
import unittest
import math
import re

import aiohttp
import discord

import gestalt
import gesp


# this test harness reimplements most relevant parts of the discord API, offline
# the alternative involves maintaining *4* separate bots
# and either threading (safety not guaranteed) or switching between them (sloow)
# meanwhile, existing discord.py testing solutions don't support multiple users
# i wish it didn't have to be this way but i promise this is the best solution


class warptime:
    real = discord.utils.utcnow
    warp = 0
    @classmethod
    def now(cls):
        return cls.real() + timedelta(seconds = cls.warp)


class Object:
    nextid = gestalt.discord.utils.time_snowflake(warptime.now())
    def __init__(self, **kwargs):
        # don't call now() each time
        self.id = ((warptime.warp * 1000) << 22) + Object.nextid
        Object.nextid += 1
        vars(self).update(kwargs)

class User(Object):
    users = {}
    def __init__(self, **kwargs):
        self._deleted = False
        self.bot = False
        self.dm_channel = None
        self.discriminator = '0001'
        super().__init__(**kwargs)
        if not self.bot:
            self.dm_channel = Channel(type = discord.ChannelType.private,
                    members = [self, instance.user], recipient = self)
        User.users[self.id] = self
    @property
    def mention(self):
        return '<@!%d>' % self.id
    @property
    def mutual_guilds(self):
        return list(filter(
            lambda guild : (guild.get_member(self.id)
                and guild.get_member(instance.user.id)),
            Guild.guilds.values()))
    def _delete(self):
        self._deleted = True
        del User.users[self.id]
        for guild in Guild.guilds.values():
            if self.id in guild._members:
                del guild._members[self.id]
    def __str__(self):
        return self.name + '#' + self.discriminator
    async def send(self, content = None, embed = None, file = None):
        await self.dm_channel.send(
                content = content, embed = embed, file = file)

# note that this class does NOT subclass Object because member IDs are user IDs
class Member:
    # don't set roles immediately; let bot process role events later
    def __init__(self, user, guild, perms = discord.Permissions.all()):
        (self.user, self.guild, self.guild_permissions) = (user, guild, perms)
        self.roles = []
    def __str__(self): return str(self.user)
    def _copy(self):
        copy = Member(self.user, self.guild, self.guild_permissions)
        copy.roles = self.roles[:]
        return copy
    def _add_role(self, role):
        before = self._copy()
        self.roles.append(role)
        role.members.append(self)
        run(instance.on_member_update(before, self))
    def _del_role(self, role, _async = False):
        before = self._copy()
        self.roles.remove(role)
        role.members.remove(self)
        coro = instance.on_member_update(before, self)
        return coro if _async else run(coro)
    @property
    def display_avatar(self):
        # TODO: guild avatars
        return Object(replace = lambda format : 'http://avatar.png')
    @property
    def id(self): return self.user.id
    @property
    def bot(self): return self.user.bot
    @property
    def name(self): return self.user.name
    @property
    def display_name(self): return self.user.name
    @property
    def mutual_guilds(self):
        return self.user.mutual_guilds

class Message(Object):
    def __init__(self, embed = None, view = None, **kwargs):
        self._deleted = False
        self._prev = None
        self.webhook_id = None
        self.attachments = []
        self.reactions = []
        self.reference = None
        self.edited_at = None
        self.embeds = [embed] if embed else []
        self.components = view.children if view else []
        self.guild = None
        super().__init__(**kwargs)
    @property
    def channel_mentions(self):
        if self.content:
            return [Channel.channels[int(mention)]
                    for mention in re.findall('(?<=\<#)[0-9]+(?=\>)',
                        self.content)]
        return []
    @property
    def clean_content(self):
        return self.content # only used in reply embeds
    @property
    def jump_url(self):
        return '%s/%i' % (self.channel.jump_url, self.id)
    @property
    def mentions(self):
        # mentions can also be in the embed but that's irrelevant here
        # also you can always force a link to someone not present
        # but it isn't included in the actual mentions property
        if self.guild and self.content:
            mentions = map(int, re.findall('(?<=\<\@\!)[0-9]+(?=\>)',
                self.content))
            if self.guild:
                mentions = map(self.guild.get_member, mentions)
            return list(mentions)
        return []
    @property
    def role_mentions(self):
        if self.content:
            return [Role.roles[int(mention)] for mention
                    in re.findall('(?<=\<\@\&)[0-9]+(?=\>)', self.content)]
        return []
    @property
    def type(self):
        return (discord.MessageType.reply if self.reference
                else discord.MessageType.default)
    async def delete(self, delay = None):
        self.channel._messages.remove(self)
        self._deleted = True
        await instance.on_raw_message_delete(
                discord.raw_models.RawMessageDeleteEvent(data = {
                    'channel_id': self.channel.id,
                    'id': self.id}))
    async def edit(self, *args, **kwargs):
        # TODO currently only used in Votes
        pass
    def _react(self, emoji, user, _async = False):
        react = discord.Reaction(message = self, emoji = emoji,
                data = {'count': 1, 'me': None})
        if react not in self.reactions:
            # FIXME when more than one user adds the same reaction
            self.reactions.append(react)
        coro = instance.on_raw_reaction_add(
                discord.raw_models.RawReactionActionEvent(data = {
                    'message_id': self.id,
                    'user_id': user.id,
                    'channel_id': self.channel.id},
                    emoji = discord.PartialEmoji(name = emoji),
                    event_type = None))
        return coro if _async else run(coro)
    async def add_reaction(self, emoji):
        await self._react(emoji, instance.user, _async = True)
    async def remove_reaction(self, emoji, member):
        del self.reactions[[x.emoji for x in self.reactions].index(emoji)]
    async def _bulk_delete(self):
        self.channel._messages.remove(self)
        self._deleted = True
        await instance.on_raw_bulk_message_delete(
                discord.raw_models.RawBulkMessageDeleteEvent(data = {
                    'ids': {self.id}, 'channel_id': self.channel.id}))

class PartialMessage:
    def __init__(self, *, channel, id):
        (self.channel, self.id, self.guild) = (channel, id, channel.guild)
        self._truemsg = discord.utils.get(channel._messages, id = id)
    @property
    def jump_url(self):
        return self._truemsg.jump_url
    async def delete(self, delay = None):
        await self._truemsg.delete()
    async def fetch(self):
        return self._truemsg
    async def remove_reaction(self, emoji, member):
        await self._truemsg.remove_reaction(emoji, member)

class Webhook(Object):
    hooks = {}
    def __init__(self, channel, name):
        super().__init__()
        self._deleted = False
        (self._channel, self.name) = (channel, name)
        self.token = 't0k3n' + str(self.id)
        Webhook.hooks[self.id] = self
    def partial(id, token, session):
        return Webhook.hooks[id]
    async def delete(self):
        self._deleted = True
        await instance.on_webhooks_update(self._channel)
    async def edit_message(self, message_id, content, allowed_mentions,
            thread = None):
        msg = await (thread or self._channel).fetch_message(message_id)
        if self._deleted or msg.webhook_id != self.id or (thread and
                thread.parent != self._channel):
            raise NotFound()
        newmsg = Message(**vars(msg))
        newmsg.content = content
        newmsg.edited_at = warptime.now()
        newmsg._prev = msg
        msg.channel._messages[msg.channel._messages.index(msg)] = newmsg
        return newmsg
    async def fetch(self):
        if self._deleted:
            raise NotFound()
        return self
    async def send(self, username, avatar_url, thread = None, **kwargs):
        if self._deleted or (thread and thread.parent != self._channel):
            raise NotFound()
        msg = Message(**kwargs) # note: absorbs other irrelevant arguments
        msg.webhook_id = self.id
        name = username if username else self.name
        msg.author = Object(id = self.id, bot = True,
                name = name, display_name = name,
                display_avatar = avatar_url)
        if msg.content:
            # simulate external emoji mangling
            # i don't understand why this happens
            msg.content = re.sub(
                    '<(:[a-zA-Z90-9_~]+:)[0-9]+>',
                    lambda match : match.group(1),
                    msg.content)
        await (thread or self._channel)._add(msg)
        return msg

class Channel(Object):
    channels = {}
    def __init__(self, **kwargs):
        self._messages = []
        self.name = ''
        self.guild = None
        super().__init__(**kwargs)
        Channel.channels[self.id] = self
    def __getitem__(self, key):
        return self._messages[key]
    @property
    def jump_url(self):
        return 'http://%i/%i' % (self.guild.id, self.id)
    @property
    def members(self):
        return self.guild.members
    @property
    def mention(self):
        return '<#%i>' % self.id
    @property
    def type(self):
        return discord.ChannelType.text
    async def _add(self, msg):
        msg.channel = self
        if self.guild:
            msg.guild = self.guild
            if not msg.author.id in Webhook.hooks:
                msg.author = self.guild.get_member(msg.author.id)
        self._messages.append(msg)
        await instance.on_message(msg)
    async def create_webhook(self, name):
        return Webhook(self, name)
    async def fetch_message(self, msgid):
        msg = discord.utils.get(self._messages, id = msgid)
        if not msg:
            raise NotFound()
        return msg
    def get_partial_message(self, msgid):
        return PartialMessage(channel = self, id = msgid)
    def permissions_for(self, user):
        # TODO need channel-level permissions
        return self.guild.get_member(user.id).guild_permissions
    async def send(self, content = None, embed = None, file = None,
            view = None, reference = None):
        msg = Message(author = instance.user, content = content, embed = embed,
                view = view)
        await self._add(msg)
        return msg

class Thread(Channel):
    threads = {}
    def __init__(self, channel, **kwargs):
        self.parent = channel
        super().__init__(**kwargs)
        self.guild = self.parent.guild
        Thread.threads[self.id] = self
    @property
    def type(self):
        return discord.ChannelType.public_thread
    async def create_webhook(self, name):
        raise NotImplementedError()

class Role(Object):
    roles = {}
    def __init__(self, **kwargs):
        self.members = []
        self.guild = None
        self.managed = False
        super().__init__(**kwargs)
        Role.roles[self.id] = self
    @property
    def mention(self):
        return '<@&%i>' % self.id
    async def delete(self):
        await self.guild._del_role(self)

class RoleEveryone:
    def __init__(self, guild):
        self.guild = guild
        self.id = guild.id
        self.name = '@everyone'
        self.managed = False
    # note that this doesn't update when guild._members updates
    @property
    def members(self): return self.guild._members.values()

class Guild(Object):
    guilds = {}
    def __init__(self, **kwargs):
        self._channels = {}     # channel id -> channel
        self._roles = {}        # role id -> role
        self._members = {}      # user id -> member
        self.name = ''
        self.premium_tier = 0
        super().__init__(**kwargs)
        self._roles[self.id] = RoleEveryone(self)
        Guild.guilds[self.id] = self
    def __getitem__(self, key):
        return discord.utils.get(self._channels.values(), name = key)
    @property
    def default_role(self):
        return self._roles[self.id]
    @property
    def members(self):
        return self._members.values()
    @property
    def roles(self):
        return list(self._roles.values())
    def _add_channel(self, name):
        chan = Channel(name = name, guild = self)
        self._channels[chan.id] = chan
        return chan
    def _add_role(self, name):
        role = Role(guild = self, name = name)
        self._roles[role.id] = role
        return role
        # bot doesn't listen to role creation events
    async def _del_role(self, role):
        if role == self.default_role:
            raise RuntimeError('Can\'t delete @everyone!')
        # event order observed experimentally
        # discord.py doesn't document these things
        for member in role.members:
            await member._del_role(role, _async = True)
        del self._roles[role.id]
        await instance.on_guild_role_delete(role)
    def _add_member(self, user, perms = discord.Permissions.all()):
        if user.id in self._members:
            raise RuntimeError('re-adding a member to a guild')
        member = self._members[user.id] = Member(user, self, perms)
        member.roles.append(self.default_role)
        # NOTE: does on_member_update get called here? probably not but idk
        if user.id == instance.user.id:
            run(instance.on_guild_join(self))
        elif self.get_member(instance.user.id):
            run(instance.on_member_join(member))
        return self # for chaining
    def _remove_member(self, user):
        del self._members[user.id]
    def get_member(self, user_id):
        return self._members.get(user_id)
    def get_role(self, role_id):
        return self._roles[role_id]

# incoming messages have Attachments, outgoing messages have Files
# but we'll pretend that they're the same for simplicity
class File(Object):
    def __init__(self, size):
        self.size = size
        super().__init__()
    def is_spoiler(self):
        return False
    async def to_file(self, spoiler):
        return self
    @property
    def url(self):
        return 'https://%i' % self.id

class Interaction:
    def __init__(self, message, user, button):
        (self.message, self.user) = (message, user)
        self.data = {'custom_id': button}
        # TODO check that the message actuall has the buttons
    @property
    def channel(self):
        return self.message.channel
    @property
    def response(self):
        return self # lol
    async def send_message(self, **kwargs):
        pass # only used for ephemeral messages; bot never sees those

invites = {}

class TestBot(gestalt.Gestalt):
    def __init__(self):
        self._user = User(name = 'Gestalt', bot = True)
        super().__init__(dbfile = ':memory:')
        self.session = ClientSession()
        self.pk_ratelimit = discord.gateway.GatewayRatelimiter(count = 1000,
                per = 1.0)
    def log(*args):
        pass
    @property
    def user(self):
        return self._user
    def get_user(self, id):
        return User.users.get(id)
    async def fetch_invite(self, code, **_):
        if code in invites:
            return Object(guild = invites[code])
        raise NotFound()
    async def fetch_user(self, id):
        try:
            return User.users[id]
        except KeyError:
            raise NotFound()
    def get_channel(self, id):
        return Channel.channels.get(id, Thread.threads.get(id))
    def get_guild(self, id):
        return Guild.guilds.get(id)
    def is_ready(self):
        return True

class ClientSession:
    class Response:
        def __init__(self, text): self._text = text
        async def text(self, encoding): return self._text
        @property
        def status(self): return 200 if type(self._text) == str else self._text
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    _data = {}
    def get(self, url, **kwargs): return self.Response(self._data[url])
    def _add(self, path, data): self._data[gestalt.PK_ENDPOINT + path] = data

class NotFound(discord.errors.NotFound):
    def __init__(self):
        pass

def send(user, channel, content, reference = None, files = [], orig = False):
    author = channel.guild.get_member(user.id) if channel.guild else user
    msg = Message(author = author, content = content, reference = reference,
            attachments = files)
    run(channel._add(msg))
    return channel[-1] if msg._deleted and not orig else msg

def interact(message, user, button):
    run(instance.on_interaction(Interaction(message, user, button)))

class GestaltTest(unittest.TestCase):

    # ugly hack because parsing gs;p output would be uglier
    def get_proxid(self, user, other = None, name = None):
        if name:
            row = instance.fetchall(
                    'select proxid from proxies '
                    'where (userid, cmdname) = (?, ?)',
                    (user.id, name))
            self.assertLess(len(row), 2)
            row = row and row[0]
        elif other == None:
            row = instance.fetchone(
                    'select proxid from proxies where (userid, type) = (?, ?)',
                    (user.id, gestalt.ProxyType.override))
        elif type(other) == str:
            row = instance.fetchone(
                    'select proxid from proxies '
                    'where (userid, maskid) = (?, ?)',
                    (user.id, other))
        else:
            row = instance.fetchone(
                    'select proxid from proxies '
                    'where (userid, otherid) is (?, ?)',
                    (user.id, other.id))
        return row[0] if row else None

    def assertRowExists(self, *args):
        self.assertIsNotNone(instance.fetchone(*args))

    def assertRowNotExists(self, *args):
        self.assertIsNone(instance.fetchone(*args))

    def assertReacted(self, msg, reaction = gestalt.REACT_CONFIRM):
        self.assertNotEqual(len(msg.reactions), 0)
        self.assertEqual(msg.reactions[0].emoji, reaction)

    def assertNotReacted(self, msg):
        self.assertEqual(len(msg.reactions), 0)

    def assertCommand(self, *args, **kwargs):
        msg = send(*args, **kwargs)
        self.assertReacted(msg)
        return msg

    def assertNotCommand(self, *args, **kwargs):
        msg = send(*args, **kwargs)
        self.assertNotReacted(msg)
        return msg

    def assertVote(self, *args, **kwargs):
        msg = self.assertNotCommand(*args, **kwargs)
        last = msg.channel[-1]
        self.assertTrue(bool(last.components))
        self.assertIn(last.id, instance.votes)
        return msg

    def assertNotVote(self, *args, **kwargs):
        msg = self.assertNotCommand(*args, **kwargs)
        last = msg.channel[-1]
        self.assertFalse(bool(last.components))
        self.assertNotIn(last.id, instance.votes)
        return msg

    def assertProxied(self, *args, **kwargs):
        msg = send(*args, **kwargs, orig = True)
        self.assertTrue(msg._deleted)
        self.assertGreater(msg.channel[-1].id, msg.id)
        self.assertIsNotNone(msg.channel[-1].webhook_id)
        return msg.channel[-1]

    def assertNotProxied(self, *args, **kwargs):
        msg = send(*args, **kwargs, orig = True)
        self.assertEqual(msg, msg.channel[-1])
        self.assertIsNone(msg.webhook_id)
        return msg

    def assertDeleted(self, *args, **kwargs):
        # test that the message was deleted with none others sent
        msg = send(*args, **kwargs, orig = True)
        self.assertTrue(msg._deleted)
        self.assertLess(msg.channel[-1].id, msg.id)

    def assertEditedContent(self, message, content):
        self.assertEqual(
                run(message.channel.fetch_message(message.id)).content,
                content)

    def desc(self, msg):
        self.assertNotEqual(msg.embeds, [])
        return msg.embeds[0].description

    def assertReload(self):
        attrs = ('votes', 'mask_presence')
        origs = [getattr(instance, attr) for attr in attrs]
        instance.save()
        [setattr(instance, attr, None) for attr in attrs]
        instance.load()
        for orig, attr in zip(origs, attrs):
            self.assertEqual(orig, getattr(instance, attr))
            self.assertIsNot(orig, getattr(instance, attr))


    # tests run in alphabetical order, so they are numbered to ensure order
    def test_01_swaps(self):
        chan = g['main']

        # alpha opens a swap with beta
        self.assertCommand(
                alpha, chan, 'gs;swap open %s sw text' % beta.mention)
        alphaid = self.get_proxid(alpha, beta)
        self.assertIsNotNone(alphaid)
        self.assertIsNone(self.get_proxid(beta, alpha))
        self.assertEqual(instance.fetchone(
            'select state from proxies where proxid = ?', (alphaid,))[0],
            gestalt.ProxyState.inactive)
        # simply trying to open the swap again doesn't work
        self.assertNotCommand(alpha, chan, 'gs;swap open %s' % beta.mention)
        self.assertEqual(instance.fetchone(
            'select state from proxies where proxid = ?', (alphaid,))[0],
            gestalt.ProxyState.inactive)

        # alpha tests swap; it should fail
        self.assertNotProxied(alpha, chan, 'sw no swap')
        # beta opens swap
        self.assertCommand(
                beta, chan, 'gs;swap open %s sw text' % alpha.mention)
        self.assertIsNotNone(self.get_proxid(alpha ,beta))
        self.assertIsNotNone(self.get_proxid(beta, alpha))
        # alpha and beta test the swap; it should work now
        self.assertProxied(alpha, chan, 'sw swap')
        self.assertProxied(beta, chan, 'sw swap')

        # try to redundantly open another swap; it should fail
        self.assertNotCommand(alpha, chan, 'gs;swap open %s' % beta.mention)
        self.assertNotCommand(beta, chan, 'gs;swap open %s' % alpha.mention)
        self.assertEqual(instance.fetchone(
            'select count() from proxies where (userid, otherid) = (?, ?)',
            (alpha.id, beta.id))[0], 1)
        self.assertEqual(instance.fetchone(
            'select count() from proxies where (userid, otherid) = (?, ?)',
            (beta.id, alpha.id))[0], 1)

        # now, with the alpha-beta swap active, alpha opens swap with gamma
        # this one should fail due to tags conflict
        self.assertNotCommand(
                alpha, chan, 'gs;swap open %s sw text' % gamma.mention)
        # but this one should work
        self.assertCommand(
                alpha, chan, 'gs;swap open %s sww text' % gamma.mention)
        # gamma opens the swap
        self.assertCommand(
                gamma, chan, 'gs;swap open %s sww text' % alpha.mention)
        self.assertIsNotNone(self.get_proxid(alpha, gamma))
        gammaid = self.get_proxid(gamma, alpha)
        self.assertIsNotNone(gammaid)
        self.assertTrue(
                send(alpha, chan, 'sww swap').author.name.index(gamma.name)
                != -1)
        self.assertTrue(
                send(gamma, chan, 'sww swap').author.name.index(alpha.name)
                != -1)
        # the alpha-beta swap should still work
        self.assertIsNotNone(alphaid)
        self.assertIsNotNone(self.get_proxid(beta, alpha))
        self.assertTrue(
                send(alpha, chan, 'sw swap').author.name.index(beta.name) != -1)
        self.assertTrue(
                send(beta, chan, 'sw swap').author.name.index(alpha.name) != -1)

        # close the swaps
        self.assertCommand(alpha, chan, 'gs;swap close %s' % alphaid)
        self.assertIsNone(self.get_proxid(alpha, beta))
        self.assertIsNone(self.get_proxid(beta, alpha))
        self.assertIsNotNone(self.get_proxid(alpha, gamma))
        self.assertIsNotNone(self.get_proxid(gamma, alpha))
        self.assertCommand(gamma, chan, 'gs;swap close %s' % gammaid)
        self.assertIsNone(self.get_proxid(alpha, gamma))
        self.assertIsNone(self.get_proxid(gamma, alpha))
        self.assertNotProxied(beta, chan, 'sw no swap')
        self.assertNotProxied(gamma, chan, 'sww no swap')

        # test self-swaps
        self.assertCommand(alpha, chan, 'gs;swap open %s' % alpha.mention)
        self.assertIsNotNone(self.get_proxid(alpha, alpha))
        self.assertCommand(alpha, chan, 'gs;p test-alpha tags me:text')
        self.assertProxied(alpha, chan, 'me:self-swap')
        self.assertEqual(chan[-1].author.name, 'test-alpha')
        self.assertNotCommand(alpha, chan, 'gs;swap open %s' % alpha.mention)
        self.assertEqual(instance.fetchone(
            'select count() from proxies where (userid, otherid) = (?, ?)',
            (alpha.id, alpha.id))[0], 1)
        self.assertCommand(alpha, chan, 'gs;swap close test-alpha')
        self.assertIsNone(self.get_proxid(alpha, alpha))

    def test_02_help(self):
        send(alpha, g['main'], 'GS; help')
        msg = g['main'][-1]
        self.assertEqual(len(msg.embeds), 1)
        self.assertReacted(msg, gestalt.REACT_DELETE)
        msg._react(gestalt.REACT_DELETE, alpha)
        self.assertTrue(msg._deleted)

    def test_03_add_delete_collective(self):
        """
        # create an @everyone collective
        self.assertCommand(alpha, g['main'], 'gs;c new "everyone"')
        # make sure it worked
        self.assertIsNotNone(self.get_collid(g.default_role))
        # try to make a collective on the same role; it shouldn't work
        self.assertNotCommand(alpha, g['main'], 'gs;c new "everyone"')

        proxid = self.get_proxid(alpha, g.default_role)
        self.assertIsNotNone(proxid)
        # set the tags
        self.assertCommand(alpha, g['main'], 'gs;p %s tags e:text' % proxid)
        # test the proxy
        self.assertProxied(alpha, g['main'], 'e:test')
        # this proxy will be used in later tests


        # now try again with a new role
        role = g._add_role('delete me')
        # add the role to alpha, then create collective
        member = g.get_member(alpha.id)
        member._add_role(role)
        self.assertCommand(alpha, g['main'], 'gs;c new %s' % role.mention)
        proxid = self.get_proxid(alpha, role)
        self.assertIsNotNone(proxid)

        # set tags and test it
        self.assertCommand(alpha, g['main'], 'gs;p %s tags d:text' % proxid)
        self.assertProxied(alpha, g['main'], 'd:test')

        # try removing the role from alpha
        member._del_role(role)
        self.assertIsNone(self.get_proxid(alpha, role))
        member._add_role(role)
        self.assertIsNotNone(self.get_proxid(alpha, role))

        # delete the collective normally
        collid = self.get_collid(role)
        self.assertCommand(alpha, g['main'], 'gs;c %s delete' % collid)
        self.assertIsNone(self.get_collid(role))
        self.assertIsNone(self.get_proxid(alpha, collid))

        # recreate the collective, then delete the role
        self.assertCommand(alpha, g['main'], 'gs;c new %s' % role.mention)
        collid = self.get_collid(role)
        self.assertIsNotNone(collid)
        proxid = self.get_proxid(alpha, collid)
        self.assertIsNotNone(proxid)
        run(role.delete())
        self.assertIsNone(self.get_proxid(alpha, collid))
        self.assertIsNone(self.get_collid(role))
        """

    def test_04_permissions(self):
        # users shouldn't be able to change another's proxy
        alphaid = self.get_proxid(alpha, None)
        self.assertNotCommand(beta, g['main'], 'gs;p %s tags no:text' % alphaid)

    def test_05_tags_auto(self):
        # test every combo of auto, tags, and also the switches thereof
        chan = g['main']
        self.assertVote(alpha, chan, 'gs;m new test')
        interact(chan[-1], alpha, 'yes')
        proxid = self.get_proxid(alpha, name = 'test')

        self.assertCommand(alpha, chan, 'gs;p test tags e:text')
        self.assertNotProxied(alpha, chan, 'no tags, no auto')
        self.assertProxied(alpha, chan, 'E:Tags')
        self.assertEqual(chan[-1].content, 'Tags')
        self.assertCommand(alpha, chan, 'gs;p %s tags "= text"' % proxid)
        self.assertProxied(alpha, chan, '= tags, no auto')
        self.assertEqual(chan[-1].content, 'tags, no auto')
        self.assertCommand(alpha, chan, 'gs;ap %s' % proxid)
        self.assertProxied(alpha, chan, '= tags, auto')
        self.assertEqual(chan[-1].content, 'tags, auto')
        self.assertProxied(alpha, chan, 'no tags, auto')
        self.assertEqual(chan[-1].content, 'no tags, auto')
        self.assertCommand(alpha, chan, 'gs;ap off')
        self.assertCommand(alpha, chan, 'gs;p %s tags #text#' % proxid)
        self.assertProxied(alpha, chan, '#tags, no auto#')
        self.assertEqual(chan[-1].content, 'tags, no auto')
        self.assertCommand(alpha, chan, 'gs;p %s tags text]' % proxid)
        self.assertProxied(alpha, chan, 'postfix tag]')

        # test keepproxy
        self.assertCommand(alpha, chan, 'gs;p %s tags [text]' % proxid)
        self.assertCommand(alpha, chan, 'gs;p %s keepproxy on' % proxid)
        self.assertProxied(alpha, chan, '[message]')
        self.assertEqual(chan[-1].content, '[message]')
        self.assertCommand(alpha, chan, 'gs;p %s keepproxy off' % proxid)
        self.assertProxied(alpha, chan, '[message]')
        self.assertEqual(chan[-1].content, 'message')
        self.assertCommand(alpha, chan, 'gs;p %s tags e:text' % proxid)

        # test echo
        self.assertCommand(alpha, chan, 'gs;p %s echo on' % proxid)
        (msg, proxied) = (send(alpha, chan, 'e:echo!'), chan[-1])
        self.assertFalse(msg._deleted)
        self.assertGreater(proxied.id, msg.id)
        self.assertIsNotNone(proxied.webhook_id)
        self.assertEqual(proxied.content, 'echo!')

        # echo and keepproxy
        self.assertCommand(alpha, chan, 'gs;p %s keepproxy on' % proxid)
        (msg, proxied) = (send(alpha, chan, 'e:echo!'), chan[-1])
        self.assertFalse(msg._deleted)
        self.assertGreater(proxied.id, msg.id)
        self.assertIsNotNone(proxied.webhook_id)
        self.assertEqual(proxied.content, 'e:echo!')
        self.assertCommand(alpha, chan, 'gs;p %s keepproxy off' % proxid)
        self.assertCommand(alpha, chan, 'gs;p %s echo off' % proxid)

        # invalid tags. these should fail
        self.assertNotCommand(alpha, chan, 'gs;p %s tags ' % proxid)
        self.assertNotCommand(alpha, chan, 'gs;p %s tags text ' % proxid)
        self.assertNotCommand(alpha, chan, 'gs;p %s tags txet ' % proxid)

        # test autoproxy without tags
        # also test proxies added on role add
        self.assertVote(alpha, chan, 'gs;m new notags')
        interact(chan[-1], alpha, 'no')
        proxid = self.get_proxid(alpha, name = 'notags')
        self.assertCommand(alpha, chan, 'gs;ap %s' % proxid)
        self.assertProxied(alpha, chan, 'no tags, auto')
        self.assertCommand(alpha, chan, 'gs;ap off')
        self.assertNotProxied(alpha, chan, 'no tags, no auto')

        # test tag precedence over auto
        self.assertCommand(alpha, chan, 'gs;ap %s' % proxid)
        self.assertEqual(send(alpha, chan, 'auto').author.name, 'notags')
        self.assertEqual(send(alpha, chan, 'e: tags').author.name, 'test')

        self.assertCommand(alpha, chan, 'gs;ap off')
        self.assertCommand(alpha, chan, 'gs;account config errors on')
        self.assertNotProxied(alpha, chan, '>be test.')
        self.assertEqual(chan[-1].author.id, alpha.id) # no message on error
        self.assertCommand(alpha, chan, 'gs;account config errors off')
        self.assertCommand(alpha, chan, 'gs;account config Homestuck on')
        self.assertProxied(alpha, chan, '>be test.')
        self.assertTrue(
                chan[-1].content.startswith('\\> [__Be test.__]('))
        self.assertProxied(alpha, chan, '==>')

    def test_06_query_delete(self):
        g._add_member(deleteme := User(name = 'deleteme'))
        chan = g['main']
        self.assertCommand(deleteme, chan, 'gs;swap open %s'
            % deleteme.mention)
        self.assertCommand(deleteme, chan, 'gs;p deleteme tags e:text')
        msg = send(deleteme, chan, 'e:reaction test')
        msg._react(gestalt.REACT_QUERY, beta)
        token = discord.utils.escape_markdown(str(deleteme))
        self.assertIn(token, beta.dm_channel[-1].content)

        msg._react(gestalt.REACT_DELETE, beta)
        self.assertEqual(len(msg.reactions), 0)
        self.assertFalse(msg._deleted)

        msg._react(gestalt.REACT_DELETE, deleteme)
        self.assertTrue(msg._deleted)

        msg = send(deleteme, chan, 'e:bye!')
        deleteme._delete()
        self.assertIsNone(instance.get_user(deleteme.id))
        with self.assertRaises(NotFound):
            run(instance.fetch_user(deleteme.id))
        send(beta, beta.dm_channel, 'buffer')
        msg._react(gestalt.REACT_QUERY, beta)
        self.assertIn(token, beta.dm_channel[-1].content)

        # in swaps, sender or swapee may delete message
        self.assertCommand(alpha, chan,
                'gs;swap open %s swap:text' % beta.mention)
        self.assertCommand(beta, chan, 'gs;swap open %s' % alpha.mention)
        msg = self.assertProxied(alpha, chan, 'swap:delete me')
        msg._react(gestalt.REACT_DELETE, gamma)
        self.assertFalse(msg._deleted)
        msg._react(gestalt.REACT_DELETE, alpha)
        self.assertTrue(msg._deleted)
        msg = self.assertProxied(alpha, chan, 'swap:delete me')
        msg._react(gestalt.REACT_DELETE, beta)
        self.assertTrue(msg._deleted)
        self.assertCommand(alpha, chan,
                'gs;swap close %s' % self.get_proxid(alpha, beta))

        # test DMs
        msg1 = beta.dm_channel[-1]
        msg2 = send(beta, beta.dm_channel, 'test')
        msg1._react(gestalt.REACT_DELETE, beta)
        self.assertTrue(msg1._deleted)
        msg2._react(gestalt.REACT_DELETE, beta)
        self.assertFalse(msg2._deleted)

        # and finally normal messages
        msg = self.assertNotProxied(beta, chan, "we're just normal messages")
        buf = send(beta, beta.dm_channel, "we're just innocent messages")
        msg._react(gestalt.REACT_QUERY, beta)
        msg._react(gestalt.REACT_DELETE, beta)
        self.assertFalse(msg._deleted)
        self.assertEqual(len(msg.reactions), 2)
        self.assertEqual(beta.dm_channel[-1], buf)

    def test_07_webhook_shenanigans(self):
        # test what happens when a webhook is deleted
        hookid = send(alpha, g['main'], 'e:reiuskudfvb').webhook_id
        self.assertIsNotNone(hookid)
        self.assertEqual(run(instance.get_webhook(g['main'])).id, hookid)
        # webhook was deleted offline. this should be rare
        Webhook.hooks[hookid]._deleted = True
        msg = send(alpha, g['main'], 'e:asdhgdfjg')
        newhook = msg.webhook_id
        self.assertIsNotNone(newhook)
        self.assertNotEqual(hookid, newhook)
        self.assertEqual(run(instance.get_webhook(g['main'])).id, newhook)

        send(alpha, g['main'], 'gs;e nice edit')
        self.assertEditedContent(msg, 'nice edit')
        Webhook.hooks[newhook]._deleted = True
        self.assertReacted(send(alpha, g['main'], 'gs;e evil edit!'),
                gestalt.REACT_DELETE)
        self.assertEditedContent(msg, 'nice edit')
        self.assertIsNone(run(instance.get_webhook(g['main'])))

        # test on_webhooks_update()
        hookid = send(alpha, g['main'], 'e:reiuskudfvb').webhook_id
        self.assertIsNotNone(run(instance.get_webhook(g['main'])))
        run(Webhook.hooks[hookid].delete())
        self.assertIsNone(run(instance.get_webhook(g['main'])))

    # this function requires the existence of at least three ongoing wars
    # it's also a bit outdated due to predating per-guild autoproxy settings
    def test_08_global_conflicts(self):
        g2 = Guild()
        g2._add_channel('main')
        g2._add_member(instance.user)
        g2._add_member(alpha)

        # open a swap. swaps are global so alpha will use it to test conflicts
        self.assertCommand(
                alpha, g['main'], 'gs;swap open %s :text' % beta.mention)
        self.assertCommand(
                beta, g['main'], 'gs;swap open %s :text' % alpha.mention)
        proxswap = self.get_proxid(alpha, beta)
        self.assertIsNotNone(proxswap)

        # swap should be usable in g but not g2 because beta isn't in g2
        self.assertProxied(alpha, g['main'], ':test')
        self.assertNotProxied(alpha, g2['main'], ':test')

        self.assertVote(alpha, g['main'], 'gs;m new conflict')
        interact(g['main'][-1], alpha, 'no')
        proxfirst = self.get_proxid(alpha, name = 'conflict')
        self.assertIsNotNone(proxfirst)
        self.assertVote(alpha, g2['main'], 'gs;m new conflict1')
        interact(g2['main'][-1], alpha, 'no')
        proxsecond = self.get_proxid(alpha, name = 'conflict1')
        self.assertIsNotNone(proxsecond)
        self.assertCommand(alpha, g2['main'], 'gs;p conflict1 rename conflict')

        # now alpha can test tags and auto stuff
        self.assertCommand(
                alpha, g['main'], 'gs;p %s tags same:text' % proxfirst)
        # this shouldn't work even though the masks are in different guilds
        self.assertNotCommand(
                alpha, g2['main'], 'gs;p %s tags same:text' % proxsecond)
        self.assertProxied(alpha, g['main'], 'same: no auto')
        # alpha should be able to set both to auto; different guilds
        self.assertCommand(alpha, g['main'], 'gs;ap %s' % proxfirst)
        self.assertCommand(alpha, g2['main'], 'gs;ap %s' % proxsecond)
        self.assertProxied(alpha, g['main'], 'auto on')
        self.assertProxied(alpha, g2['main'], 'auto on')

        # test global tags conflict; this should fail
        self.assertNotCommand(
                alpha, g['main'], 'gs;p %s tags same:text' % proxswap)
        # no conflict; this should work
        self.assertCommand(
                alpha, g['main'], 'gs;p %s tags swap:text' % proxswap)
        # make a conflict with a mask
        self.assertNotCommand(
                alpha, g['main'], 'gs;p %s tags swap:text' % proxfirst)
        # now turning on auto on the swap should deactivate the other autos
        self.assertProxied(alpha, g['main'], 'auto on')
        self.assertNotEqual(
                send(alpha, g['main'], 'mask has auto').author.name.index(
                    'conflict'), -1)
        self.assertCommand(alpha, g['main'], 'gs;ap %s' % proxswap)
        self.assertNotEqual(
                send(alpha, g['main'], 'swap has auto').author.name.index(
                    beta.name), -1)

        # test combined prefix/postfix conflicts
        # this should be exhaustive

        self.assertCommand(alpha, g['main'],
                'gs;p %s tags 123text456' % proxswap)
        self.assertCommand(alpha, g['main'],
                'gs;p %s tags 1234text56' % proxfirst)
        self.assertCommand(alpha, g['main'],
                'gs;p %s tags 1234text' % proxfirst)
        self.assertCommand(alpha, g['main'],
                'gs;p %s tags 12text3456' % proxfirst)
        self.assertCommand(alpha, g['main'],
                'gs;p %s tags text3456' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags 12text56' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags 1234text3456' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags 123text456' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags 123text' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags text456' % proxfirst)

        self.assertCommand(alpha, g['main'],
                'gs;p %s tags 123text' % proxswap)
        self.assertCommand(alpha, g['main'],
                'gs;p %s tags text456' % proxfirst)
        self.assertCommand(alpha, g['main'],
                'gs;p %s tags 12text456' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags 12text' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags 1234text' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags 1234text456' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags 123text' % proxfirst)

        self.assertCommand(alpha, g['main'], 'gs;p %s tags [text]' % proxfirst)

        self.assertCommand(alpha, g['main'],
                'gs;p %s tags text456' % proxswap)
        self.assertCommand(alpha, g['main'],
                'gs;p %s tags 123text' % proxfirst)
        self.assertCommand(alpha, g['main'],
                'gs;p %s tags 123text56' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags text56' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags text3456' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags 123text3456' % proxfirst)
        self.assertNotCommand(alpha, g['main'],
                'gs;p %s tags text456' % proxfirst)

        # done. close the swap
        self.assertCommand(alpha, g['main'], 'gs;swap close %s' % proxswap)

    def test_09_override(self):
        overid = self.get_proxid(alpha, None)
        # alpha has sent messages visible to bot by now, so should have one
        self.assertIsNotNone(overid)
        proxid = self.get_proxid(alpha, name = 'test')
        self.assertIsNotNone(proxid)

        chan = g['main']
        self.assertProxied(alpha, chan, 'e: proxy')
        self.assertCommand(alpha, chan, 'gs;ap %s' % proxid)
        self.assertProxied(alpha, chan, 'proxy')
        # set the override tags. this should activate it
        self.assertCommand(alpha, chan, 'gs;p %s tags x:text' % overid)
        self.assertNotProxied(alpha, chan, 'x: not proxies')

        # turn autoproxy off
        self.assertCommand(alpha, chan, 'gs;ap off')
        self.assertNotProxied(alpha, chan, 'not proxied')
        self.assertNotProxied(alpha, chan, 'x: not proxied')

    # by far the most ominous test
    def test_10_replacements(self):
        """
        chan = g['main']
        before = 'I am myself. i was and am. I\'m. im. am I? I me my mine.'
        after = (
                'We are Ourselves. We were and are. We\'re. We\'re. are We? '
                'We Us Our Ours.')
        self.assertCommand(alpha, chan, 'gs;account config replace off')
        self.assertEqual(send(alpha, chan, 'e:' + before).content, before)
        self.assertCommand(alpha, chan, 'gs;a config replace on')
        self.assertEqual(send(alpha, chan, 'e:' + before).content, after)
        self.assertCommand(alpha, chan, 'gs;a config replace off')
        self.assertEqual(send(alpha, chan, 'e:' + before).content, before)
        self.assertCommand(alpha, chan, 'gs;a config defaults')
        self.assertEqual(send(alpha, chan, 'e:' + before).content, after)
        """

    def test_11_avatar_url(self):
        chan = g['main']
        self.assertVote(alpha, chan, 'gs;m new url')
        interact(chan[-1], alpha, 'no')
        self.assertCommand(alpha, chan, 'gs;m url avatar http://avatar.gov')
        self.assertCommand(alpha, chan, 'gs;m url avatar https://avatar.gov')
        self.assertNotCommand(alpha, chan, 'gs;m url avatar http:/avatar.gov')
        self.assertNotCommand(alpha, chan, 'gs;m url avatar _http://avatar.gov')
        self.assertNotCommand(alpha, chan, 'gs;m url avatar foobar')
        self.assertCommand(alpha, chan, 'gs;m url avatar <http://avatar.gov>')
        avatar = File(1024)
        self.assertCommand(alpha, chan, 'gs;m url avatar', files = [avatar])
        self.assertCommand(alpha, chan, 'gs;p url tags url:text')
        self.assertProxied(alpha, chan, 'url:test')
        self.assertEqual(chan[-1].author.display_avatar, avatar.url)
        self.assertCommand(alpha, chan, 'gs;m url leave')

    def test_12_username_change(self):
        chan = g['main']
        old = (alpha.name, alpha.discriminator)
        # first, make sure the existing entry is up to date
        # these are full name#discriminator, not just name
        self.assertEqual(instance.fetchone(
            'select username from users where userid = ?',
            (alpha.id,))[0], str(alpha))
        alpha.name = 'changed name'
        # run(instance.on_member_update(None, g.get_member(alpha.id)))
        send(alpha, chan, 'this should trigger an update')
        self.assertEqual(instance.fetchone(
            'select username from users where userid = ?',
            (alpha.id,))[0], str(alpha))
        alpha.discriminator = '9999'
        # run(instance.on_member_update(None, g.get_member(alpha.id)))
        send(alpha, chan, 'this should trigger an update')
        self.assertEqual(instance.fetchone(
            'select username from users where userid = ?',
            (alpha.id,))[0], str(alpha))
        # done, set things back
        (alpha.name, alpha.discriminator) = old
        # run(instance.on_member_update(None, g.get_member(alpha.id)))

    def test_13_latch(self):
        chan = g['main']
        self.assertCommand(alpha, chan, 'gs;ap latch')
        self.assertNotProxied(alpha, chan, 'no proxy, no auto')
        self.assertProxied(alpha, chan, 'e: proxy, no auto')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertProxied(alpha, chan, 'e: proxy, auto')
        self.assertNotProxied(alpha, chan, 'x: override')
        self.assertNotProxied(alpha, chan, 'no proxy, no auto')

        # test \escape and \\unlatch
        self.assertProxied(alpha, chan, 'e: proxy, no auto')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertNotProxied(alpha, chan, '\escape')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertNotProxied(alpha, chan, '\\\\unlatch')
        self.assertNotProxied(alpha, chan, 'no proxy, no auto')
        self.assertCommand(alpha, chan, 'gs;ap off')

        self.assertCommand(alpha, chan, 'gs;ap test')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertNotProxied(alpha, chan, '\escape')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertNotProxied(alpha, chan, '\\\\unlatch')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertNotProxied(alpha, chan, '\\\\\\unautoproxy')
        self.assertNotProxied(alpha, chan, 'no proxy, no auto')

    # test member joining when the guild has an @everyone collective
    def test_14_member_join(self):
        """
        user = User(name = 'test-joining')
        g._add_member(user)
        self.assertIsNotNone(self.get_proxid(user, g.default_role))
        """

    def test_15_case(self):
        self.assertIsNotNone(self.get_proxid(alpha, None).upper())
        self.assertCommand(alpha, g['main'], 'gs;swap open %s' % beta.mention)
        self.assertCommand(beta, g['main'], 'gs;swap open %s' % alpha.mention)
        proxid = self.get_proxid(alpha, beta).upper()
        self.assertCommand(alpha, g['main'], 'gs;p %s keepproxy on' % proxid)
        self.assertCommand(alpha, g['main'], 'gs;swap close %s' % proxid)

    def test_16_replies(self):
        chan = g['main']
        msg = self.assertProxied(alpha, chan, 'e: no reply')
        self.assertEqual(len(msg.embeds), 0)
        reply = self.assertProxied(alpha, chan, 'e: reply',
                Object(cached_message = msg))
        self.assertEqual(len(reply.embeds), 1)
        self.assertEqual(self.desc(reply),
                '**[Reply to:](%s)** no reply' % msg.jump_url)
        # again, but this time the message isn't in cache
        reply = self.assertProxied(alpha, chan, 'e: reply',
                Object(cached_message = None, message_id = msg.id))
        self.assertEqual(len(reply.embeds), 1)
        self.assertEqual(self.desc(reply),
                '**[Reply to:](%s)** no reply' % msg.jump_url)

    def test_17_edit(self):
        chan = g._add_channel('edit')
        first = self.assertProxied(alpha, chan, 'e: fisrt')
        self.assertNotCommand(alpha, chan, 'gs;e')
        second = self.assertProxied(alpha, chan, 'e: secnod')

        # test edit attempt by non-author
        msg = send(beta, chan, 'gs;edit second')
        self.assertReacted(msg, gestalt.REACT_DELETE)
        self.assertEditedContent(second, 'secnod')
        msg = send(beta, chan, 'gs;edit first', Object(message_id = first.id))
        self.assertReacted(msg, gestalt.REACT_DELETE)
        self.assertEditedContent(first, 'fisrt')

        # test successful edits
        self.assertDeleted(alpha, chan, 'gs;edit second')
        self.assertEditedContent(second, 'second')
        self.assertDeleted(alpha, chan, 'gs;edit first',
                Object(message_id = first.id))
        self.assertEditedContent(first, 'first')
        self.assertDeleted(alpha, chan, 'gs;edit\nnewline')
        self.assertEditedContent(second, 'newline')
        self.assertDeleted(alpha, chan, 'gs;edit "quote" unquote')
        self.assertEditedContent(second, '"quote" unquote')

        # make sure that the correct most recent msgid is pulled from db
        self.assertVote(beta, chan, 'gs;m new edit')
        interact(chan[-1], beta, 'no')
        self.assertCommand(beta, chan, 'gs;p edit tags e: text')
        first = self.assertProxied(alpha, chan, 'e: edti me')
        run(send(alpha, chan, 'e: delete me').delete())
        run(send(alpha, chan, 'e: delete me too')._bulk_delete())
        send(alpha, chan, 'e: manually delete me')._react(
            gestalt.REACT_DELETE, alpha)
        self.assertProxied(beta, chan, 'e: dont edit me')
        send(alpha, chan, 'gs;help this message should be ignored')
        self.assertDeleted(alpha, chan, 'gs;edit edit me');
        self.assertEditedContent(first, 'edit me');

        # test new user
        new = User(name = 'new')
        g._add_member(new)
        self.assertFalse(send(new, chan, 'gs;edit hoohoo')._deleted)

        # test old message
        msg = self.assertProxied(alpha, chan, 'e: edit me')
        self.assertDeleted(alpha, chan, 'gs;edit edti me')
        self.assertEditedContent(msg, 'edti me')
        warptime.warp += gestalt.TIMEOUT_EDIT - 1
        self.assertDeleted(alpha, chan, 'gs;edit editme')
        self.assertEditedContent(msg, 'editme')
        warptime.warp += 2
        self.assertFalse(send(alpha, chan, 'gs;edit edit me')._deleted)
        self.assertEditedContent(msg, 'editme')
        self.assertDeleted(alpha, chan, 'gs;edit edit me',
                Object(message_id = msg.id))
        self.assertEditedContent(msg, 'edit me')

        # make sure that gs;edit on a non-webhook message doesn't cause problems
        # delete "or proxied.webhook_id != hook[1]" to see this be a problem
        # this is very important, because if this fails,
        # there could be a path to a per-channel denial of service
        # ...except none of that is true anymore lmao
        first = send(alpha, chan, 'e: fisrt')
        send(alpha, chan, 'gs;help')
        second = chan[-1]
        self.assertEqual(second.author.id, instance.user.id)
        third = send(alpha, chan, 'gs;edit lol', Object(message_id = second.id))
        self.assertEditedContent(third, 'gs;edit lol')
        self.assertReacted(third, gestalt.REACT_DELETE)
        self.assertProxied(alpha, chan, 'e: another')
        send(alpha, chan, 'gs;edit first', Object(message_id = first.id))
        self.assertEditedContent(first, 'first')

    def test_18_become(self):
        chan = g['main']
        # can't Become override
        self.assertNotCommand(alpha, chan, 'gs;become %s'
                % self.get_proxid(alpha, None))
        # can't Become someone else's proxy
        self.assertCommand(alpha, chan, 'gs;swap open %s' % beta.mention)
        self.assertCommand(beta, chan, 'gs;swap open %s' % alpha.mention)
        proxid = self.get_proxid(alpha, beta)
        self.assertNotCommand(beta, chan, 'gs;become %s' % proxid)

        self.assertCommand(alpha, chan, 'gs;become %s' % proxid)
        self.assertNotProxied(alpha, chan, 'not proxied')
        self.assertProxied(alpha, chan, 'proxied')
        self.assertCommand(alpha, chan, 'gs;swap close %s' % proxid)

    def test_19_swap_close(self):
        chan = g['main']
        self.assertCommand(alpha, chan, 'gs;swap open %s' % beta.mention)
        self.assertCommand(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, beta))
        self.assertCommand(alpha, chan, 'gs;swap open %s' % beta.mention)
        self.assertCommand(beta, chan, 'gs;swap open %s' % alpha.mention)
        self.assertNotCommand(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, None))
        self.assertVote(alpha, chan, 'gs;m new close')
        interact(chan[-1], alpha, 'no')
        self.assertNotCommand(alpha, chan, 'gs;swap close close')
        instance.get_user_proxy(chan[-1], 'close')
        self.assertCommand(alpha, chan, 'gs;m close leave')
        self.assertNotCommand(alpha, chan, 'gs;swap close aaaaaa')
        self.assertCommand(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, beta))

    def test_20_collective_delete(self):
        """
        g1 = Guild()
        c1 = g1._add_channel('main')
        g1._add_member(instance.user)
        g1._add_member(alpha, perms = discord.Permissions(manage_roles = True))
        g1._add_member(beta, perms = discord.Permissions(manage_roles = False))
        g2 = Guild()
        c2 = g2._add_channel('main')
        g2._add_member(instance.user)
        g2._add_member(beta, perms = discord.Permissions(manage_roles = True))

        send(beta, c1, 'gs;c new everyone')
        self.assertIsNone(self.get_collid(g1.default_role))
        send(alpha, c1, 'gs;c new everyone')
        self.assertIsNotNone(self.get_collid(g1.default_role))
        send(beta, c2, 'gs;c new everyone')
        self.assertIsNotNone(self.get_collid(g2.default_role))
        send(beta, c2, 'gs;c %s delete' % self.get_collid(g1.default_role))
        self.assertIsNotNone(self.get_collid(g1.default_role))
        send(alpha, c1, 'gs;c %s delete' % self.get_collid(g1.default_role))
        self.assertIsNone(self.get_collid(g1.default_role))
        """

    def test_21_attachments(self):
        g1 = Guild()
        c = g1._add_channel('main')
        g1._add_member(instance.user)
        g1._add_member(alpha)

        self.assertVote(alpha, c, 'gs;m new attachments')
        interact(c[-1], alpha, 'no')
        self.assertCommand(alpha, c, 'gs;p attachments tags [text')
        # normal message
        msg = self.assertProxied(alpha, c, '[test')
        self.assertEqual(len(msg.files), 0)
        # one file, no content
        msg = self.assertProxied(alpha, c, '[', files = [File(12)])
        self.assertEqual(msg.content, '')
        self.assertEqual(len(msg.files), 1)
        # two files, no content
        msg = self.assertProxied(alpha, c, '[', files = [File(12), File(9)])
        self.assertEqual(len(msg.files), 2)
        # two files, with content
        msg = self.assertProxied(alpha, c, '[files!', files = [File(12),
            File(9)])
        self.assertEqual(msg.content, 'files!')
        self.assertEqual(len(msg.files), 2)
        # no files or content
        self.assertNotProxied(alpha, c, '[', files = [])
        # big file, no content
        self.assertNotProxied(alpha, c, '[', files = [File(999999999)])
        half = gestalt.MAX_FILE_SIZE[0]/2
        # files at the limit, no content
        self.assertProxied(alpha, c, '[', files = [File(half), File(half)])
        # files too big, no content
        self.assertNotProxied(alpha, c, '[', files = [File(half), File(half+1)])
        # files too big, with content
        msg = self.assertProxied(alpha, c, '[files!', files = [File(half),
            File(half+1)])
        self.assertEqual(msg.files, [])

        self.assertCommand(alpha, c, 'gs;m attachments leave')

    # mostly redundant now
    def test_22_names(self):
        g1 = Guild(name = 'guildy guild')
        c = g1._add_channel('main')
        g1._add_member(instance.user)
        g1._add_member(alpha)
        g1._add_member(beta)

        self.assertVote(alpha, c, 'gs;m new guildy')
        interact(c[-1], alpha, 'no')
        self.assertCommand(alpha, c, 'gs;p "guildy" tags [text')
        self.assertEqual(send(alpha, c, '[no proxid!').author.name, 'guildy')
        self.assertCommand(alpha, c, 'gs;p "guildy" rename "guild"')
        self.assertCommand(alpha, c, 'gs;ap guild')
        self.assertEqual(send(alpha, c, 'yay!').author.name, 'guildy')
        self.assertCommand(alpha, c, 'gs;become guild')
        self.assertNotProxied(alpha, c, 'not proxied')
        self.assertProxied(alpha, c, 'proxied')
        self.assertCommand(alpha, c, 'gs;ap off')

        send(alpha, c, 'gs;swap open %s' % beta.mention)
        send(beta, c, 'gs;swap open %s' % alpha.mention)
        self.assertCommand(alpha, c, 'gs;p test-beta tags b:text')
        self.assertNotCommand(beta, c, 'gs;swap close test-beta')
        self.assertCommand(alpha, c, 'gs;swap close test-beta')

        self.assertVote(alpha, c, 'gs;m guild invite %s' % beta.mention)
        interact(c[-1], beta, 'yes')
        self.assertNotCommand(beta, c, 'gs;p guild tags g:text')
        self.assertCommand(beta, c, 'gs;p guildy tags g:text')

        self.assertCommand(alpha, c, 'gs;m "guild" name guild!')
        self.assertEqual(send(alpha, c, '[proxied').author.name, 'guild!')
        self.assertCommand(alpha, c, 'gs;m guild avatar http://newavatar')
        self.assertEqual(send(alpha, c, '[proxied').author.display_avatar,
                'http://newavatar')
        instance.get_user_proxy(send(alpha, c, 'command'), 'guild')
        self.assertCommand(alpha, c, 'gs;m guild leave %s' % beta.mention)
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(c[-1], 'guild')
        self.assertCommand(beta, c, 'gs;m guildy leave')

        # With names, users can infer *and control* names of hidden proxies.
        # We must ensure that hidden proxies can't be used in commands.
        # If not, an (ugh) "Sybil" attack is possible:
        # 1. user A on account a1 opens a swap with user B; B agrees
        # 2. B uses gs;p to view their proxies, giving A the proxid
        # 3. A renames acount a2 to the proxid and opens a swap with B
        # 4. A renames account a3 to the same as a1 and opens a swap with B
        # 5. B can no longer close the swap with a1:
        #   - The name and proxid of the swap with a1 are both ambiguous.
        #   - B cannot see the proxids of the swaps with a2 & a3.
        # It may be possible to escape the softlock via confirming the swaps;
        # however, B may not realize this, and feel unable to revoke consent.
        # Furthermore, if A controls the guild, they can isolate a2 & a3
        # to a channel hidden to B, giving them no idea of what's happening.
        # also swaps used to make a hidden proxy for the other user so this is
        # a bit anachronistic now but there'll probably be more uses for hidden
        # proxies in the future so yeah
        # send(alpha, c, 'gs;swap open %s' % beta.mention)
        proxid = instance.mkproxy(beta.id, gestalt.ProxyType.swap,
                cmdname = 'test-alpha', state = gestalt.ProxyState.hidden)
        # self.assertIsNotNone(self.get_proxid(beta, alpha))
        # self.assertIsNotNone(instance.get_user_proxy(c[-1], 'test-beta'))
        self.assertNotCommand(beta, c, 'gs;ap test-alpha')
        # self.assertNotCommand(send(beta, c, 'gs;swap close test-alpha'))
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(c[-1], 'test-alpha')
        # self.assertCommand(alpha, c, 'gs;swap close test-beta')
        instance.execute('delete from proxies where proxid = ?', (proxid,))

    def test_23_pk_swap(self):
        g1 = Guild(name = 'guildy guild')
        c = g1._add_channel('main')
        g1._add_member(instance.user)
        g1._add_member(alpha)
        g1._add_member(beta)
        g1._add_member(gamma)
        pkhook = Webhook(c, 'pk webhook')

        instance.session._add('/systems/' + str(alpha.id), '{"id": "exmpl"}')
        instance.session._add('/members/aaaaa',
                '{"system": "exmpl", "uuid": "a-a-a-a-a", "name": "member!", '
                '"color": "123456"}')

        self.assertVote(alpha, c, 'gs;pk swap %s aaaaa' % beta.mention)
        interact(c[-1], alpha, 'yes')
        self.assertVote(alpha, c, 'gs;pk swap %s aaaaa' % beta.mention)
        interact(c[-1], beta, 'no')
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        self.assertVote(alpha, c, 'gs;pk swap %s aaaaa' % beta.mention)
        interact(c[-1], beta, 'yes')
        instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        # shouldn't work twice
        self.assertNotVote(alpha, c, 'gs;pk swap %s aaaaa' % beta.mention)
        # should be able to send to two users
        self.assertCommand(alpha, c, 'gs;swap open %s' % gamma.mention)
        self.assertCommand(gamma, c, 'gs;swap open %s' % alpha.mention)
        msg = self.assertVote(alpha, c, 'gs;pk swap %s aaaaa' % gamma.mention)
        self.assertVote(alpha, c, 'gs;pk swap %s aaaaa' % gamma.mention)
        interact(msg, gamma, 'yes')
        interact(c[-1], gamma, 'yes')
        instance.get_user_proxy(send(gamma, c, 'a'), 'member!')
        # should NOT be deleted upon swap close
        self.assertCommand(alpha, c, 'gs;swap close test-gamma')
        #with self.assertRaises(gestalt.UserError):
        instance.get_user_proxy(send(gamma, c, 'a'), 'member!')
        # handle PluralKit linked accounts
        instance.session._add('/systems/' + str(gamma.id), '{"id": "exmpl"}')
        self.assertCommand(beta, c, 'gs;swap open %s' % gamma.mention)
        self.assertCommand(gamma, c, 'gs;swap open %s' % beta.mention)
        self.assertNotVote(gamma, c, 'gs;pk swap %s aaaaa' % beta.mention)

        # test sending to self
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(send(alpha, c, 'a'), 'member!')
        self.assertCommand(alpha, c, 'gs;pk swap %s aaaaa' % alpha.mention)
        instance.get_user_proxy(send(alpha, c, 'a'), 'member!')
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(send(alpha, c, 'a'),
                    'test-alpha\'s member!')
        self.assertCommand(alpha, c, 'gs;pk close member!')

        # test using it!
        self.assertCommand(beta, c, 'gs;p member! tags [text]')
        self.assertNotProxied(beta, c, '[test]')
        old = run(pkhook.send('member!', '', content = 'old message'))
        new = run(pkhook.send('member!', '', content = 'new message'))
        nope = run(pkhook.send('someone else', '', content = 'irrelevant'))
        instance.session._add('/messages/' + str(old.id),
                '{"member": {"uuid": "a-a-a-a-a"}}')
        instance.session._add('/messages/' + str(new.id),
                '{"member": {"uuid": "a-a-a-a-a", "color": "123456"}}')
        instance.session._add('/messages/' + str(nope.id),
                '{"member": {"uuid": "z-z-z-z-z"}}')
        self.assertNotCommand(beta, c, 'gs;pk sync')
        self.assertNotCommand(beta, c, 'gs;pk sync',
            Object(cached_message = None, message_id = nope.id))
        self.assertCommand(beta, c, 'gs;pk sync',
            Object(cached_message = None, message_id = new.id))
        self.assertNotCommand(beta, c, 'gs;pk sync',
            Object(cached_message = None, message_id = old.id))
        msg = self.assertProxied(beta, c, '[test]',
            Object(cached_message = None, message_id = new.id))
        self.assertEqual(msg.author.name, 'member!')
        self.assertEqual(str(msg.embeds[0].color), '#123456')
        # test other member not being present
        g1._remove_member(alpha)
        msg = self.assertNotProxied(beta, c, '[test]')
        g1._add_member(alpha)
        # test a message with no pk entry
        instance.session._add('/messages/' + str(c[-1].id), 404)
        self.assertNotCommand(beta, c, 'gs;pk sync',
                Object(cached_message = c[-1]))
        # change the color
        instance.session._add('/messages/' + str(new.id),
                '{"member": {"uuid": "a-a-a-a-a", "color": "654321"}}')
        self.assertCommand(beta, c, 'gs;pk sync',
            Object(cached_message = None, message_id = new.id))
        msg = self.assertProxied(beta, c, '[test]',
            Object(cached_message = None, message_id = new.id))
        self.assertEqual(str(msg.embeds[0].color), '#654321')
        # delete the color
        instance.session._add('/messages/' + str(new.id),
                '{"member": {"uuid": "a-a-a-a-a", "color": null}}')
        self.assertCommand(beta, c, 'gs;pk sync',
            Object(cached_message = None, message_id = new.id))
        msg = self.assertProxied(beta, c, '[test]',
            Object(cached_message = None, message_id = new.id))
        self.assertEqual(msg.embeds[0].color, None)

        # test closing specific pkswap
        # first by receipt
        instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta\'s member!')
        self.assertCommand(alpha, c, 'gs;pk close "test-beta\'s member!"')
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta\'s member!')

        # then by pkswap
        self.assertVote(alpha, c, 'gs;pk swap %s aaaaa' % beta.mention)
        interact(c[-1], beta, 'yes')
        instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        self.assertCommand(beta, c, 'gs;pk close member!')
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta\'s member!')

        self.assertNotCommand(beta, c, 'gs;pk close test-alpha')

    def test_24_logs(self):
        g1 = Guild(name = 'logged guild')
        c = g1._add_channel('main')
        log = g1._add_channel('log')
        g1._add_member(instance.user)
        g1._add_member(alpha)

        self.assertVote(alpha, c, 'gs;m new logged')
        interact(c[-1], alpha, 'no')
        self.assertCommand(alpha, c, 'gs;p logged tags g:text')
        self.assertCommand(alpha, c, 'gs;log channel %s ' % log.mention)

        # just check that the log messages exist for now
        self.assertEqual(len(log._messages), 0)
        msg = self.assertProxied(alpha, c, 'g:proxied message')
        self.assertEqual(len(log._messages), 1)
        send(alpha, c, 'gs;edit edited message')
        self.assertEqual(len(log._messages), 2)
        send(alpha, c, 'g:this is truly a panopticon',
            Object(cached_message = msg))
        self.assertEqual(len(log._messages), 3)

        self.assertCommand(alpha, c, 'gs;log disable')
        self.assertProxied(alpha, c, 'g:secret message!')
        self.assertEqual(len(log._messages), 3)
        send(alpha, c, 'gs;edit spooky message')
        self.assertEqual(len(log._messages), 3)

        self.assertCommand(alpha, c, 'gs;m logged leave')

    def test_25_threads(self):
        g1 = Guild(name = 'thready guild')
        c = g1._add_channel('main')
        g1._add_member(instance.user)
        g1._add_member(alpha)
        g1._add_member(beta)
        th = Thread(c, name = 'the best thread')

        self.assertCommand(alpha, c, 'gs;swap open %s' % beta.mention)
        self.assertCommand(beta, c, 'gs;swap open %s' % alpha.mention)
        self.assertCommand(alpha, c, 'gs;p test-beta tags beta:text')
        self.assertCommand(beta, c, 'gs;p test-alpha tags alpha:text')

        msg = self.assertProxied(alpha, th,
                "beta:that's one small step for a swap")
        newer = send(alpha, c, 'beta:newer message')
        send(alpha, th, 'gs;help')
        cmd = th[-1]
        send(alpha, c, 'gs;help')
        send(beta, th, 'alpha:unrelated message')
        send(alpha, th, 'gs;edit one giant leap for proxykind')
        self.assertEditedContent(msg, 'one giant leap for proxykind')

        msg = send(alpha, th, 'beta:newerer message')
        send(alpha, c, 'gs;edit older message')
        self.assertEditedContent(newer, 'older message')
        send(alpha, th, 'gs;edit ancient message',
            Object(message_id = msg.id))
        self.assertEditedContent(msg, 'ancient message')

        msg = send(alpha, th, 'beta: delete me')
        msg._react(gestalt.REACT_DELETE, alpha)
        self.assertTrue(msg._deleted)
        self.assertFalse(cmd._deleted)
        cmd._react(gestalt.REACT_DELETE, alpha)
        self.assertTrue(cmd._deleted)

        c2 = g1._add_channel('general')
        th2 = Thread(c2, name = 'epic thread')
        self.assertReacted(send(alpha, th2, 'gs;edit no messages yet'),
                gestalt.REACT_DELETE)
        msg = self.assertProxied(beta, th2,
                'alpha:what if the first proxied message is in a thread')
        msg = self.assertProxied(alpha, c2, 'beta:everything still works right')
        self.assertEqual(run(instance.get_webhook(c2)).id, msg.webhook_id)
        # get_webhook() converts Thread to parent, so need to query directly
        self.assertRowNotExists(
                'select 1 from webhooks where chanid = ?',
                (th2.id,))

        log = g1._add_channel('log')
        self.assertCommand(alpha, th, 'gs;log channel %s ' % log.mention)
        self.assertEqual(len(log._messages), 0)
        msg = self.assertProxied(alpha, th, 'beta:logg')
        self.assertEqual(len(log._messages), 1)
        self.assertEqual(log[0].content, 'http://%i/%i/%i' % (g1.id, th.id,
            msg.id))
        send(alpha, th, 'gs;edit log')
        self.assertEqual(len(log._messages), 2)
        self.assertEqual(log[1].content, 'http://%i/%i/%i' % (g1.id, th.id,
            msg.id))

        self.assertCommand(beta, c, 'gs;swap close test-alpha')

    def test_26_colors(self):
        g1 = Guild(name = 'colorful guild')
        c = g1._add_channel('main')
        g1._add_member(instance.user)
        g1._add_member(alpha)
        g1._add_member(beta)

        # pk swap colors are already tested
        self.assertCommand(alpha, c, 'gs;swap open %s beta:text'
                % beta.mention)
        self.assertCommand(beta, c, 'gs;swap open %s' % alpha.mention)

        # masks
        self.assertVote(alpha, c, 'gs;m new colorful')
        interact(c[-1], alpha, 'no')
        self.assertCommand(alpha, c, 'gs;p colorful tags c:text')
        target = self.assertProxied(alpha, c, 'c:message')
        msg = self.assertProxied(alpha, c, 'c:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertIsNone(msg.embeds[0].color)
        self.assertCommand(alpha, c, 'gs;m colorful colour rose')
        msg = self.assertProxied(alpha, c, 'c:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertEqual(str(msg.embeds[0].color).upper(),
                gestalt.NAMED_COLORS['rose'])
        self.assertNotCommand(beta, c, 'gs;m colorful color john')
        msg = self.assertProxied(alpha, c, 'c:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertEqual(str(msg.embeds[0].color).upper(),
                gestalt.NAMED_COLORS['rose'])
        self.assertCommand(alpha, c, 'gs;m colorful color -clear')
        msg = self.assertProxied(alpha, c, 'c:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertIsNone(msg.embeds[0].color)

        # swaps
        target = self.assertProxied(alpha, c, 'beta:message')
        msg = self.assertProxied(alpha, c, 'beta:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertIsNone(msg.embeds[0].color)
        self.assertCommand(beta, c, 'gs;account colour #888888')
        msg = self.assertProxied(alpha, c, 'beta:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertEqual(str(msg.embeds[0].color), '#888888')
        self.assertCommand(beta, c, 'gs;account color -clear')
        msg = self.assertProxied(alpha, c, 'beta:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertIsNone(msg.embeds[0].color)

        self.assertCommand(beta, c, 'gs;swap close test-alpha')
        self.assertCommand(alpha, c, 'gs;m colorful leave')

    def test_27_mandatory(self):
        g1 = Guild(name = 'mandatory guild')
        main = g1._add_channel('main')
        cmds = g1._add_channel('cmds')
        g1._add_member(instance.user)
        g1._add_member(alpha)
        g1._add_member(beta)

        self.assertNotCommand(alpha, cmds, 'gs;channel %s mode irl' %
            Guild()._add_channel('channel').mention)
        # channel needs to be in the same guild
        self.assertNotCommand(alpha, cmds, 'gs;channel %s mode mandatory' %
            Guild()._add_channel('channel').mention)

        self.assertCommand(alpha, cmds, 'gs;swap open %s alpha:text'
                % alpha.mention)
        self.assertProxied(alpha, main, 'alpha:self swap')
        self.assertCommand(alpha, cmds, 'gs;channel %s mode mandatory'
                % main.mention)
        # self-swap should be deleted
        self.assertDeleted(alpha, main, 'alpha:self swap')
        self.assertCommand(alpha, cmds, 'gs;swap open %s beta:text'
                % beta.mention)
        # inactive swap should be deleted
        self.assertDeleted(alpha, main, 'beta:test')
        self.assertCommand(beta, cmds, 'gs;swap open %s alpha:text'
                % alpha.mention)
        # swap with present member should be fine
        self.assertProxied(alpha, main, 'beta:test')
        g1._remove_member(beta)
        # swap with non-present member should be deleted
        self.assertDeleted(alpha, main, 'beta:test')
        g1._add_member(beta)
        self.assertCommand(alpha, cmds, 'gs;ap test-beta')
        # proxy escape should still be deleted
        self.assertDeleted(alpha, main, r'\test')
        self.assertDeleted(alpha, main, r'\\test')
        self.assertCommand(alpha, cmds, 'gs;ap off')

        # message without a proxy should be deleted
        self.assertDeleted(alpha, main, 'no proxy')

        self.assertCommand(alpha, cmds, 'gs;p %s tags x:text'
                % self.get_proxid(alpha, None))
        # message with override should be deleted
        self.assertDeleted(alpha, main, 'x:test')

        self.assertVote(alpha, cmds, 'gs;m new mandatory')
        interact(cmds[-1], alpha, 'no')
        self.assertCommand(alpha, cmds, 'gs;p mandatory tags c:text')
        # mask should be fine
        self.assertProxied(alpha, main, 'c:test')
        self.assertCommand(alpha, cmds, 'gs;m mandatory leave')

        pkhook = Webhook(cmds, 'pk webhook')
        instance.session._add('/systems/' + str(alpha.id), '{"id": "exmpl"}')
        instance.session._add('/members/aaaaa',
                '{"system": "exmpl", "uuid": "a-a-a-a-a", "name": "member!", '
                '"color": "123456"}')
        pkmsg = run(pkhook.send('member!', '', content = 'new message'))
        instance.session._add('/messages/' + str(pkmsg.id),
                '{"member": {"uuid": "a-a-a-a-a"}}')
        self.assertVote(alpha, cmds, 'gs;pk swap %s aaaaa' % beta.mention)
        interact(cmds[-1], beta, 'yes')
        self.assertCommand(beta, cmds, 'gs;p member! tags memb:text')
        # unsynced pkswap should be deleted
        self.assertDeleted(beta, main, 'memb:not synced')
        self.assertCommand(beta, cmds, 'gs;pk sync',
            Object(cached_message = None, message_id = pkmsg.id))
        # synced pkswap should be fine
        self.assertProxied(beta, main, 'memb:synced')
        g1._remove_member(alpha)
        # swap with non-present member should be deleted
        self.assertDeleted(beta, main, 'memb:not present')
        g1._add_member(alpha)

        # command should be deleted
        self.assertDeleted(alpha, main, 'gs;swap close test-beta')

        # change it back and everything should be fine
        self.assertCommand(alpha, cmds, 'gs;channel %s mode default'
                % main.mention)
        self.assertCommand(alpha, main, 'gs;swap close test-beta')
        self.assertCommand(alpha, main, 'gs;swap close test-alpha')
        self.assertCommand(alpha, main, 'gs;pk close "test-beta\'s member!"')

    def test_28_emojis(self):
        """
        g1 = Guild(name = 'emoji guild')
        c = g1._add_channel('main')
        g1._add_member(instance.user)
        g1._add_member(alpha)

        self.assertCommand(alpha, c, 'gs;swap open %s a:text' % alpha.mention)
        self.assertIsNone(self.assertProxied(alpha, c, 'a:no emojis').edited_at)
        msg = self.assertProxied(alpha, c, 'a:<:emoji:1234>')
        self.assertIsNotNone(msg.edited_at)
        self.assertEqual(msg._prev.content, ':emoji:')
        self.assertEqual(msg.content, '<:emoji:1234>')
        self.assertCommand(alpha, c, 'gs;swap close test-alpha')
        """

    def test_29_autoproxy_new(self):
        g1 = Guild(name = 'auto guild')
        c1 = g1._add_channel('main')
        g1._add_member(instance.user)
        g1._add_member(alpha)
        g2 = Guild(name = 'manual guild')
        c2 = g2._add_channel('main')
        g2._add_member(instance.user)
        g2._add_member(alpha)

        # check handling an ap'd swap when other member leaves guild
        g1._add_member(beta)
        self.assertCommand(alpha, c1, 'gs;swap open %s b:text' % beta.mention)
        self.assertCommand(beta, c1, 'gs;swap open %s a:text' % alpha.mention)
        self.assertCommand(alpha, c1, 'gs;autoproxy test-beta')
        self.assertProxied(alpha, c1, 'beta')
        g1._remove_member(beta)
        self.assertNotProxied(alpha, c1, 'beta')

        g1._add_member(beta)
        token = discord.utils.escape_markdown(str(beta))
        send(alpha, c1, 'gs;ap')
        self.assertNotIn(token, self.desc(c1[-1]))
        self.assertCommand(alpha, c1, 'gs;ap test-beta')
        send(alpha, c1, 'gs;ap')
        self.assertIn(token, self.desc(c1[-1]))
        g1._remove_member(beta)
        send(alpha, c1, 'gs;ap')
        self.assertNotIn(token, self.desc(c1[-1]))
        g1._add_member(beta)

        # check ap's in different guilds not conflicting
        self.assertVote(alpha, c2, 'gs;m new automask')
        interact(c2[-1], alpha, 'no')
        self.assertCommand(alpha, c1, 'gs;ap test-beta')
        self.assertCommand(alpha, c2, 'gs;ap automask')
        send(alpha, c1, 'gs;ap')
        self.assertIn(token, self.desc(c1[-1]))
        send(alpha, c2, 'gs;ap')
        self.assertIn('automask', self.desc(c2[-1]))
        self.assertEqual(self.assertProxied(alpha, c1, 'beta').author.name,
                'test-beta')
        self.assertEqual(self.assertProxied(alpha, c2, 'manual').author.name,
                'automask')

        # check that proxies are checked
        self.assertNotCommand(alpha, c1, 'gs;ap "manual guild"')
        self.assertNotCommand(alpha, c2, 'gs;ap test-beta')
        self.assertCommand(alpha, c1, 'gs;swap close test-beta')
        self.assertCommand(alpha, c1, 'gs;swap open %s b:text' % beta.mention)
        self.assertNotCommand(alpha, c1, 'gs;ap test-beta')
        self.assertCommand(beta, c1, 'gs;swap open %s a:text' % alpha.mention)
        self.assertCommand(alpha, c1, 'gs;ap test-beta')
        self.assertNotCommand(alpha, c1, 'gs;ap %s'
                % (overid := self.get_proxid(alpha, None)))
        instance.session._add('/systems/' + str(alpha.id), '{"id": "exmpl"}')
        instance.session._add('/members/aaaaa',
                '{"system": "exmpl", "uuid": "a-a-a-a-a", "name": "member!", '
                '"color": "123456"}')
        self.assertVote(alpha, c1, 'gs;pk swap %s aaaaa' % beta.mention)
        interact(c1[-1], beta, 'yes')
        self.assertCommand(beta, c1, 'gs;ap member!')
        self.assertNotCommand(alpha, c1, 'gs;ap "test-beta\'s member!"')
        g1._remove_member(alpha)
        self.assertNotCommand(beta, c1, 'gs;ap member!')
        g1._add_member(alpha)
        self.assertCommand(alpha, c1, 'gs;ap %s'
                % self.get_proxid(alpha, beta))
        self.assertNotCommand(alpha, c1, 'gs;ap %s'
                % self.get_proxid(beta, alpha))

        # check that checks are checks
        self.assertEqual(gestalt.REACT_CONFIRM, gestalt.REACT_CONFIRM)

        # check all state transitions
        g2._add_member(beta)
        member = g2.get_member(alpha.id)
        for prox in [None, 'automask']:
            for latch in [-1, 0]:
                for become in [0.0, 1.0]:
                    def test(cmd):
                        instance.set_autoproxy(member, prox, latch = latch,
                                become = become)
                        self.assertCommand(alpha, c2, cmd)
                        send(alpha, c2, 'gs;ap')
                        return self.desc(c2[-1])

                    if not (prox or become == 1.0):
                        with self.assertRaises(gestalt.sqlite.IntegrityError):
                            test('')
                        continue

                    text = test('gs;ap test-beta')
                    self.assertIn(token, text)
                    self.assertNotIn('Become', text)
                    self.assertNotIn(' latch', text)
                    self.assertEqual(
                            self.assertProxied(alpha, c2, 'beta').author.name,
                            'test-beta')

                    text = test('gs;ap latch')
                    self.assertIn('However,', text)
                    self.assertNotIn('Become', text)
                    self.assertNotProxied(alpha, c2, 'not proxied')

                    text = test('gs;ap off')
                    self.assertIn('no autoproxy', text)
                    self.assertNotProxied(alpha, c2, 'not proxied')

                    text = test('gs;become test-beta')
                    self.assertIn(token, text)
                    self.assertIn('Become', text)
                    if latch:
                        self.assertIn(' latch', text)
                    else:
                        self.assertNotIn(' latch', text)
                    self.assertNotProxied(alpha, c2, 'not proxied')
                    self.assertProxied(alpha, c2, 'proxied')

        # test actually using latch against the command output
        self.assertCommand(alpha, c1, 'gs;p %s tags x:text' % overid)
        self.assertCommand(alpha, c1, 'gs;ap off')
        self.assertCommand(alpha, c1, 'gs;ap l')
        self.assertNotProxied(alpha, c1, 'not proxied')
        self.assertProxied(alpha, c1, 'b: beta')
        self.assertProxied(alpha, c1, 'beta')
        send(alpha, c1, 'gs;ap')
        text = self.desc(c1[-1])
        self.assertIn(token, text)
        self.assertIn(' latch', text)
        self.assertNotIn('Become', text)
        self.assertNotProxied(alpha, c1, 'x:nope')
        self.assertNotProxied(alpha, c1, 'nope')
        send(alpha, c1, 'gs;ap')
        text = self.desc(c1[-1])
        self.assertIn('However,', text)
        self.assertNotIn('Become', text)
        self.assertProxied(alpha, c1, 'b: beta')
        self.assertNotProxied(alpha, c1, '\escape')
        self.assertProxied(alpha, c1, 'beta')
        self.assertNotProxied(alpha, c1, '\\\\unlatch')
        send(alpha, c1, 'gs;ap')
        text = self.desc(c1[-1])
        self.assertIn('However,', text)
        self.assertNotIn('Become', text)

        # check ap reaction to proxy deletion
        self.assertCommand(alpha, c1, 'gs;ap test-beta')
        self.assertCommand(alpha, c1, 'gs;swap close test-beta')
        send(alpha, c1, 'gs;ap')
        self.assertIn('no autoproxy', self.desc(c1[-1]))

        self.assertCommand(alpha, c2, 'gs;m automask leave')

    def test_30_proxy_list(self):
        g1 = Guild(name = 'gestalt guild')
        c1 = g1._add_channel('main')
        g1._add_member(instance.user)
        g1._add_member(alpha)
        g1._add_member(beta)
        g2 = Guild(name = 'boring guild')
        c2 = g2._add_channel('main')
        g2._add_member(instance.user)
        g2._add_member(alpha)

        self.assertVote(alpha, c1, 'gs;m new listed')
        interact(c1[-1], alpha, 'no')
        self.assertCommand(alpha, c1, 'gs;swap open %s' % beta.mention)
        token = discord.utils.escape_markdown(str(beta))
        for cmd in ['gs;proxy list', 'gs;proxy list -all']:
            send(alpha, c1, cmd)
            text = self.desc(c1[-1])
            self.assertIn('listed', text)
            self.assertIn(token, text)

        send(alpha, c2, 'gs;proxy list')
        text = self.desc(c2[-1])
        self.assertNotIn('listed', text)
        self.assertNotIn(token, text)
        send(alpha, c2, 'gs;proxy list -all')
        text = self.desc(c2[-1])
        self.assertIn('listed', text)
        self.assertIn(token, text)

        send(alpha, alpha.dm_channel, 'gs;proxy list')
        msg = alpha.dm_channel[-1]
        self.assertEqual(msg.author, instance.user)
        text = self.desc(msg)
        self.assertIn('listed', text)
        self.assertIn(token, text)

        self.assertCommand(alpha, c1, 'gs;swap close test-beta')
        self.assertCommand(alpha, c1, 'gs;m listed leave')

    def test_31_quotes(self):
        c = alpha.dm_channel
        send(alpha, c, 'gs;p') # create override
        self.assertCommand(alpha, c, 'gs;p %s rename "1 1"'
                % self.get_proxid(alpha, None))

        self.assertCommand(alpha, c, 'gs;p "1 1" rename "2 2"')
        self.assertCommand(alpha, c, 'gs;p \'2 2\' rename \'3 3\'')
        self.assertNotCommand(alpha, c, 'gs;p \'3 3" rename "4 4"')
        self.assertCommand(alpha, c, 'gs;p \u201c3 3\u201f rename "4 4"')
        self.assertCommand(alpha, c, 'gs;p "4 4" rename \u201c5 5\u201f')
        self.assertNotCommand(alpha, c, 'gs;p \u300c5 5\u300c rename "6 6"')
        self.assertCommand(alpha, c, 'gs;p \u300c5 5\u300f rename "6 6"')
        self.assertNotCommand(alpha, c, 'gs;p \u201c6 6\u2018 rename "7 7"')
        self.assertNotCommand(alpha, c, 'gs;p "6 6"" "7 7"')
        self.assertNotCommand(alpha, c, 'gs;p "6 6""" "7 7"')
        self.assertCommand(alpha, c, 'gs;p "6 6" rename "unclosed string')
        self.assertCommand(alpha, c, 'gs;p \'"unclosed string\' rename "quote')
        self.assertNotCommand(alpha, c, 'gs;p "quote rename "override')
        self.assertCommand(alpha, c, 'gs;p "quote rename override')

    def test_32_message_perms(self):
        g = Guild(name = 'literally 1984')
        c = g._add_channel('main')
        g._add_member(alpha, perms = discord.Permissions(embed_links = False,
            mention_everyone = False))
        g._add_member(instance.user)

        self.assertCommand(alpha, c, 'gs;swap open %s a:text' % alpha.mention)

        self.assertEqual(self.assertProxied(alpha, c,
            'a: hi https://discord.com').content,
            'hi <https://discord.com>')
        self.assertEqual(self.assertProxied(alpha, c,
            'a: hi <https://discord.com>').content,
            'hi <https://discord.com>')
        # try edits
        msg = self.assertProxied(alpha, c, 'a: hi')
        self.assertDeleted(alpha, c, 'gs;e hi https://discord.com')
        self.assertEditedContent(msg, 'hi <https://discord.com>')

        g._remove_member(alpha)
        g._add_member(alpha)
        self.assertEqual(self.assertProxied(alpha, c,
            'a: hi https://discord.com').content,
            'hi https://discord.com')

        self.assertCommand(alpha, c, 'gs;swap close test-alpha')

    def test_33_consistency(self):
        """
        g = Guild(name = 'inconsistent guild')
        c = g._add_channel('main')
        g._add_member(alpha)
        g._add_member(instance.user)
        role = g._add_role('remove')
        member = g.get_member(alpha.id)
        member._add_role(role)
        self.assertCommand(alpha, c, 'gs;c new remove')
        self.assertCommand(alpha, c, 'gs;p remove tags r:text')
        # remove the role with no fired event
        # pretend this happens when the bot is offline or something
        member.roles.remove(role)
        role.members.remove(member)
        self.assertNotCommand(alpha, c, 'gs;p remove tags r:text')

        c = alpha.dm_channel
        member._add_role(role)
        self.assertCommand(alpha, c, 'gs;p remove tags r:text')
        g._remove_member(member)
        self.assertNotCommand(alpha, c, 'gs;p remove tags r:text')
        g._add_member(alpha)
        self.assertNotCommand(alpha, c, 'gs;p remove tags r:text')
        """

    def test_34_gesp(self):
        self.assertEqual(gesp.eval('(add 1 1)'), 2)
        self.assertEqual(gesp.eval('(sub 5 10)'), -5)
        self.assertEqual(gesp.eval('(div 10 5)'), 2)
        self.assertEqual(gesp.eval('(if (and (eq 1 1) (eq 2 2)) 1 2)'), 1)
        self.assertEqual(gesp.eval('(if (and (eq 1 5) (eq 2 2)) 1 2)'), 2)
        self.assertEqual(gesp.eval('(if (and (eq 1 1) (eq 2 5)) 1 2)'), 2)
        self.assertEqual(gesp.eval('(if (and (eq 1 5) (eq 2 5)) 1 2)'), 2)
        self.assertEqual(gesp.eval('(if (or (eq 1 1) (eq 2 2)) 1 2)'), 1)
        self.assertEqual(gesp.eval('(if (or (eq 1 5) (eq 2 2)) 1 2)'), 1)
        self.assertEqual(gesp.eval('(if (or (eq 1 1) (eq 2 5)) 1 2)'), 1)
        self.assertEqual(gesp.eval('(if (or (eq 1 5) (eq 2 5)) 1 2)'), 2)
        self.assertEqual(gesp.eval('(add (if (eq 1 1) 2 3) 2)'), 4)
        self.assertEqual(gesp.eval(
            '(and (eq 1 0) (vote-approval 12 (members)))'), False)
        self.assertEqual(gesp.eval(
            '(or (eq 1 1) (vote-approval 12 (members)))'), True)
        self.assertEqual(gesp.eval('(eq (eq 1 1) true)'), True)
        self.assertEqual(gesp.eval('(add (one) (one))'), 2)
        self.assertEqual(gesp.eval('(add  (add  1  1)  1 )'), 3)
        self.assertEqual(gesp.eval('(add (add 1 1)(add 1 1))'), 4)
        self.assertEqual(gesp.eval('(if true "foo" "bar")'), 'foo')
        with self.assertRaises(TypeError):
            gesp.eval('(add 1)')
        with self.assertRaises(TypeError):
            gesp.eval('(if true 0 false)')
        gesp.eval('(if true 0 0)')
        self.assertEqual(
                gesp.check(gesp.parse_full('(in (initiator) (members))')[0]),
                bool)

    def test_35_voting(self):
        for rules in gesp.Rules.table.values():
            if rules == gesp.RulesLegacy:
                continue
            for atype in gesp.ActionType:
                self.assertEqual(gesp.check(rules().for_action(atype)), bool)

        gesp.ActionChange('mask', which = 'nick', value = 'newname')
        with self.assertRaises(ValueError):
            gesp.ActionChange('mask', which = 'name', value = 'newname')

        g = Guild(name = 'democratic guild')
        c = g._add_channel('main')
        g._add_member(alpha)
        g._add_member(beta)
        g._add_member(gamma)
        g._add_member(instance.user)

        instance.execute('insert into masks values '
                '("mask", "", NULL, NULL, ?, 0, 0, 0)',
                (gesp.RulesDictator(user = alpha.id).to_json(),))
        instance.load()
        gesp.ActionJoin('mask', alpha.id).execute(instance)
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(alpha, c, 'msg')),
            gesp.ActionChange('mask', 'nick', 'mask!')))
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(beta, c, 'msg')),
            gesp.ActionJoin('mask', beta.id)))
        self.assertIsNone(self.get_proxid(beta, 'mask'))
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(alpha, c, 'msg')),
            gesp.ActionInvite('mask', beta.id)))
        self.assertIsNotNone(self.get_proxid(beta, 'mask'))

        instance.execute('insert into masks values '
                '("mask2", "", NULL, NULL, ?, 0, 0, 0)',
                (gesp.RulesUnanimous().to_json(),))
        instance.load()
        gesp.ActionJoin('mask2', alpha.id).execute(instance)
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(beta, c, 'msg')),
            gesp.ActionJoin('mask2', beta.id)))
        interact(c[-1], alpha, 'abstain')
        self.assertIsNone(self.get_proxid(beta, 'mask2'))
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(beta, c, 'msg')),
            gesp.ActionJoin('mask2', beta.id)))
        interact(c[-1], alpha, 'yes')
        self.assertIsNotNone(self.get_proxid(beta, 'mask2'))
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(gamma, c, 'msg')),
            gesp.ActionJoin('mask2', gamma.id)))
        interact(c[-1], alpha, 'yes')
        self.assertIsNone(self.get_proxid(gamma, 'mask2'))
        interact(c[-1], beta, 'yes')
        self.assertIsNotNone(self.get_proxid(gamma, 'mask2'))

        gesp.ActionRemove('mask2', gamma.id).execute(instance)
        self.assertIsNone(self.get_proxid(gamma, 'mask2'))
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(gamma, c, 'msg')),
            gesp.ActionJoin('mask2', gamma.id)))
        interact(c[-1], alpha, 'yes')
        self.assertIsNone(self.get_proxid(gamma, 'mask2'))
        self.assertReload()
        interact(c[-1], beta, 'yes')
        self.assertIsNotNone(self.get_proxid(gamma, 'mask2'))

        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(beta, c, 'msg')),
            gesp.ActionRemove('mask2', alpha.id)))
        self.assertIsNotNone(self.get_proxid(alpha, 'mask2'))
        interact(c[-1], beta, 'yes')
        self.assertIsNotNone(self.get_proxid(alpha, 'mask2'))
        interact(c[-1], gamma, 'yes')
        self.assertIsNone(self.get_proxid(alpha, 'mask2'))

        users = [User(name = str(i)) for i in range(6)]
        instance.execute('insert into masks values '
                '("mask3", "", NULL, NULL, ?, 0, 0, 0)',
                (gesp.RulesHandsOff(user = alpha.id).to_json(),))
        instance.load()
        gesp.ActionJoin('mask3', alpha.id).execute(instance)
        g._add_member(users[0])
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(alpha, c, 'msg')),
            gesp.ActionInvite('mask3', users[0].id)))
        self.assertIsNotNone(self.get_proxid(users[0], 'mask3'))
        for candidate, i in zip(users[1:], range(len(users)-1)):
            g._add_member(candidate)
            run(instance.initiate_action(
                gesp.ProgramContext.from_message(send(candidate, c, 'msg')),
                gesp.ActionJoin('mask3', candidate.id)))
            for j in range(math.ceil(i/2)): # i+1 current voting members
                interact(c[-1], users[j+1], 'yes')
            self.assertIsNone(self.get_proxid(candidate, 'mask3'))
            interact(c[-1], users[0], 'yes')
            self.assertIsNotNone(self.get_proxid(candidate, 'mask3'))

        instance.execute('insert into masks values '
                '("mask4", "", NULL, NULL, ?, 0, 0, 0)',
                (gesp.RulesMajority().to_json(),))
        instance.load()
        gesp.ActionJoin('mask4', users[0].id).execute(instance)
        for candidate, i in zip(users[1:], range(len(users)-1)):
            run(instance.initiate_action(
                gesp.ProgramContext.from_message(send(candidate, c, 'msg')),
                gesp.ActionJoin('mask4', candidate.id)))
            for j in range(math.ceil(i/2)): # i+1 current voting members
                interact(c[-1], users[j+1], 'yes')
            self.assertIsNone(self.get_proxid(candidate, 'mask4'))
            interact(c[-1], users[0], 'yes')
            self.assertIsNotNone(self.get_proxid(candidate, 'mask4'))

        # ActionRules has the most complicated serialization
        instance.execute('insert into masks values '
                '("mask5", "", NULL, NULL, ?, 0, 0, 0)',
                (gesp.RulesMajority().to_json(),))
        instance.load()
        gesp.ActionJoin('mask5', alpha.id).execute(instance)
        # single user exception
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(beta, c, 'msg')),
            gesp.ActionJoin('mask5', beta.id)))
        self.assertIsNone(self.get_proxid(beta, 'mask5'))
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(alpha, c, 'msg')),
            gesp.ActionInvite('mask5', beta.id)))
        self.assertIsNotNone(self.get_proxid(beta, 'mask5'))
        run(instance.initiate_action(
            gesp.ProgramContext.from_message(send(alpha, c, 'msg')),
            gesp.ActionRules('mask5', gesp.RulesDictator(user = alpha.id))))
        interact(c[-1], alpha, 'yes')
        self.assertEqual(type(instance.get_rules('mask5')), gesp.RulesMajority)
        self.assertReload()
        interact(c[-1], beta, 'yes')
        self.assertEqual(type(instance.get_rules('mask5')), gesp.RulesDictator)

        instance.execute('insert into masks values '
                '("mask6", "", NULL, NULL, ?, 0, 0, 0)',
                (gesp.RulesDictator(user = alpha.id).to_json(),))
        instance.load()
        gesp.ActionJoin('mask6', alpha.id).execute(instance)
        run(instance.initiate_vote(gesp.VotePreinvite(
            mask = 'mask6', user = beta.id, context =
            gesp.ProgramContext.from_message(send(alpha, c, 'msg')))))
        self.assertReload()
        self.assertIsNone(self.get_proxid(beta, 'mask6'))
        interact(c[-1], alpha, 'yes')
        self.assertIsNone(self.get_proxid(beta, 'mask6'))
        interact(c[-1], beta, 'yes')
        self.assertIsNotNone(self.get_proxid(beta, 'mask6'))

    def test_36_masks(self):
        mkguild = lambda name, *members : (g :=
                reduce(Guild._add_member, members, Guild(name = name)),
                g._add_channel('main'))
        (g, c) = mkguild('dramatic guild', instance.user, alpha)
        g._add_member(beta)

        # TODO this is why unit tests usually don't have shared state
        # (i've been putting that off ok)
        instance.execute('delete from votes')
        instance.execute('delete from masks')
        instance.execute('delete from proxies where type = ?',
                (gestalt.ProxyType.mask,))
        instance.load()

        cmd = self.assertVote(alpha, alpha.dm_channel, 'gs;m new mask')
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(cmd, 'mask')
        interact(alpha.dm_channel[-1], beta, 'no')
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(cmd, 'mask')
        self.assertReload()
        interact(alpha.dm_channel[-1], alpha, 'no')
        instance.get_user_proxy(cmd, 'mask')
        maskid = instance.fetchone(
                'select maskid from proxies where cmdname = "mask"')[0]
        self.assertCommand(alpha, c, 'gs;p mask tags mask:text')
        self.assertNotProxied(alpha, c, 'mask:test')
        self.assertNotVote(beta, c, 'gs;m %s add' % maskid)
        self.assertNotProxied(alpha, c, 'mask:test')
        self.assertCommand(alpha, c, 'gs;m mask add')
        self.assertProxied(alpha, c, 'mask:test')

        self.assertCommand(alpha, c, 'gs;ap mask')
        self.assertProxied(alpha, c, 'autoproxy')
        self.assertEqual(c[-1].author.name, 'mask')
        send(alpha, c, 'gs;ap')
        self.assertIn('**mask**', self.desc(c[-1]))
        send(alpha, c, 'gs;proxy list')
        self.assertIn('**mask**', self.desc(c[-1]))
        self.assertCommand(alpha, c, 'gs;ap off')

        (_, c2) = mkguild('other guild', instance.user, alpha)
        send(alpha, c2, 'gs;proxy list')
        self.assertNotIn('**mask**', self.desc(c2[-1]))
        self.assertNotProxied(alpha, c2, 'mask:test')

        self.assertCommand(alpha, c2, 'gs;swap open %s' % alpha.mention)
        self.assertNotCommand(alpha, c2, 'gs;p test-alpha autoadd on')
        self.assertCommand(alpha, c2, 'gs;p mask autoadd on')
        self.assertVote(alpha, c2, 'gs;m new onlyhere')
        interact(c2[-1], alpha, 'no')
        self.assertCommand(alpha, c2, 'gs;p onlyhere tags onlyhere:text')
        self.assertVote(alpha, alpha.dm_channel, 'gs;m new nowhere')
        interact(alpha.dm_channel[-1], alpha, 'no')
        self.assertCommand(alpha, c2, 'gs;p nowhere tags nowhere:text')
        # test both user and gestalt joining a guild
        (_, c3) = mkguild('other guild', instance.user, alpha)
        (_, c4) = mkguild('late guild', alpha, instance.user)
        self.assertProxied(alpha, c3, 'mask:test')
        self.assertProxied(alpha, c2, 'mask:test')
        self.assertProxied(alpha, c4, 'mask:test')
        self.assertNotProxied(alpha, c3, 'onlyhere:test')
        self.assertProxied(alpha, c2, 'onlyhere:test')
        self.assertNotProxied(alpha, c4, 'onlyhere:test')
        self.assertNotProxied(alpha, c3, 'nowhere:test')
        self.assertNotProxied(alpha, c2, 'nowhere:test')
        self.assertNotProxied(alpha, c4, 'nowhere:test')

        self.assertVote(alpha, c, 'gs;m new maask')
        interact(c[-1], alpha, 'yes')
        self.assertCommand(alpha, c, 'gs;p maask tags maask:text')
        self.assertProxied(alpha, c, 'maask:text')
        self.assertProxied(alpha, c2, 'maask:text')
        self.assertProxied(alpha, c3, 'maask:text')
        # test invite, remove, and that (auto)add fails according to rules
        maaskid = instance.fetchone(
                'select maskid from proxies where cmdname = "maask"')[0]
        self.assertNotVote(beta, c, 'gs;m maask invite %s' % beta.mention)
        self.assertNotVote(beta, c, 'gs;m %s invite %s'
                % (maaskid, beta.mention))
        self.assertVote(alpha, c, 'gs;m maask invite %s' % beta.mention)
        interact(c[-1], alpha, 'yes')
        self.assertFalse(instance.is_member_of(maaskid, beta.id))
        interact(c[-1], beta, 'yes')
        self.assertTrue(instance.is_member_of(maaskid, beta.id))
        self.assertNotVote(alpha, c, 'gs;m maask invite %s' % beta.mention)
        self.assertCommand(beta, c, 'gs;p maask autoadd true')
        (gb, cb) = mkguild('beta guild', instance.user, beta)
        self.assertCommand(beta, cb, 'gs;p maask tags maask:text')
        self.assertNotVote(beta, cb, 'gs;m maask add')
        self.assertCommand(beta, cb, 'gs;p maask autoadd true')
        self.assertNotProxied(beta, cb, 'maask:text')
        self.assertProxied(beta, c, 'maask:text')
        self.assertCommand(alpha, c, 'gs;m maask remove %s' % beta.mention)
        self.assertFalse(instance.is_member_of(maaskid, beta.id))
        self.assertNotVote(alpha, c, 'gs;m maask remove %s' % beta.mention)
        # test join, rules
        self.assertNotVote(beta, c, 'gs;m %s join' % maaskid)
        self.assertFalse(instance.is_member_of(maaskid, beta.id))
        self.assertCommand(alpha, c, 'gs;m maask rules unanimous')
        self.assertEqual(type(instance.get_rules(maaskid)), gesp.RulesUnanimous)
        self.assertVote(beta, c, 'gs;m %s join' % maaskid)
        self.assertFalse(instance.is_member_of(maaskid, beta.id))
        interact(c[-1], alpha, 'yes')
        self.assertTrue(instance.is_member_of(maaskid, beta.id))
        self.assertNotVote(beta, c, 'gs;m %s join' % maaskid)
        self.assertVote(alpha, c, 'gs;m maask rules handsoff')
        self.assertEqual(type(instance.get_rules(maaskid)), gesp.RulesUnanimous)
        interact(c[-1], alpha, 'yes')
        self.assertEqual(type(instance.get_rules(maaskid)), gesp.RulesUnanimous)
        interact(c[-1], alpha, 'abstain')
        self.assertEqual(type(instance.get_rules(maaskid)), gesp.RulesUnanimous)
        interact(c[-1], beta, 'yes')
        self.assertEqual(type(instance.get_rules(maaskid)), gesp.RulesUnanimous)
        interact(c[-1], alpha, 'yes')
        self.assertEqual(type(instance.get_rules(maaskid)), gesp.RulesHandsOff)
        # test the handsoff clause that the dictator can't be removed
        # this is the only time that (candidate) is used in the default rules
        # comment out action.add_context(context) to see this fail
        self.assertNotVote(beta, c, 'gs;m maask remove %s' % alpha.mention)
        self.assertTrue(instance.is_member_of(maaskid, alpha.id))
        interact(c[-1], beta, 'yes')
        self.assertTrue(instance.is_member_of(maaskid, alpha.id))
        # test leave, nominate
        self.assertNotCommand(alpha, c, 'gs;m maask leave')
        self.assertNotCommand(alpha, c, 'gs;m maask leave %s' % alpha.mention)
        self.assertCommand(alpha, c, 'gs;m maask leave %s' % beta.mention)
        self.assertFalse(instance.is_member_of(maaskid, alpha.id))
        self.assertNotCommand(beta, c, 'gs;m maask leave %s' % alpha.mention)
        self.assertVote(beta, c, 'gs;m maask invite %s' % alpha.mention)
        vote = c[-1]
        self.assertNotCommand(beta, c, 'gs;m maask nominate %s' % alpha.mention)
        interact(vote, alpha, 'yes')
        self.assertTrue(instance.is_member_of(maaskid, alpha.id))
        self.assertNotCommand(beta, c, 'gs;m maask nominate %s' % beta.mention)
        self.assertCommand(beta, c, 'gs;m maask nominate %s' % alpha.mention)
        self.assertNotCommand(beta, c, 'gs;m maask nominate %s' % alpha.mention)
        self.assertCommand(beta, c, 'gs;m maask leave')
        self.assertVote(beta, c, 'gs;m %s join' % maaskid)
        self.assertFalse(instance.is_member_of(maaskid, beta.id))
        self.assertCommand(alpha, c, 'gs;m maask leave')
        self.assertNotVote(alpha, c, 'gs;m %s join' % maaskid)
        self.assertNotVote(beta, c, 'gs;m %s join' % maaskid)

        self.assertNotVote(beta, c, 'gs;m %s name newname' % maskid)
        self.assertNotVote(beta, c, 'gs;m %s avatar https://newname'
                % maskid)
        self.assertNotVote(beta, c, 'gs;m %s color #012345' % maskid)
        self.assertProxied(alpha, c, 'mask:text')
        self.assertEqual(c[-1].author.display_name, 'mask')
        self.assertCommand(alpha, c, 'gs;m %s name newname' % maskid)
        self.assertCommand(alpha, c, 'gs;m %s avatar https://image' % maskid)
        self.assertCommand(alpha, c, 'gs;m mask colour #012345')
        self.assertProxied(alpha, c, 'mask:text',
                Object(cached_message = c[-1]))
        self.assertEqual(c[-1].author.display_name, 'newname')
        self.assertEqual(c[-1].author.display_avatar, 'https://image')
        self.assertEqual(str(c[-1].embeds[0].color), '#012345')

        dm = alpha.dm_channel
        self.assertVote(alpha, dm, 'gs;m new dm')
        interact(dm[-1], alpha, 'no')
        self.assertCommand(alpha, dm, 'gs;m dm leave')
        self.assertVote(alpha, dm, 'gs;m new dm')
        interact(dm[-1], alpha, 'no')
        maskid = instance.fetchone(
                'select maskid from proxies where cmdname = "dm"')[0]
        self.assertNotVote(beta, beta.dm_channel, 'gs;m %s join' % maskid)
        self.assertFalse(instance.is_member_of(maskid, beta.id))
        self.assertNotVote(alpha, dm, 'gs;m dm invite %s' % beta.mention)
        self.assertVote(alpha, c, 'gs;m dm invite %s' % beta.mention)
        interact(c[-1], beta, 'yes')
        self.assertTrue(instance.is_member_of(maskid, beta.id))
        self.assertNotVote(alpha, dm, 'gs;m dm remove %s' % beta.mention)
        invites['1nv1t3'] = g
        self.assertNotVote(alpha, dm, 'gs;m dm add')
        self.assertNotVote(alpha, dm, 'gs;m dm add not_an_invite')
        self.assertCommand(alpha, dm, 'gs;m dm add 1nv1t3')
        self.assertCommand(alpha, dm, 'gs;m dm nick dmmask')
        self.assertCommand(alpha, dm, 'gs;m dm avatar http://dmmask.png')
        self.assertCommand(alpha, dm, 'gs;m dm color #999999')
        self.assertNotCommand(alpha, dm, 'gs;m dm nominate %s' % beta.mention)
        self.assertNotCommand(alpha, dm, 'gs;m dm leave %s' % beta.mention)

        # test that votes in dms are an error
        self.assertCommand(alpha, dm, 'gs;m dm rules unanimous')
        self.assertTrue(instance.is_member_of(maskid, beta.id))
        invites['1nv1t3_2'] = mkguild('another guild', instance.user, alpha)[0]
        self.assertNotVote(alpha, dm, 'gs;m dm add 1nv1t3_2')
        self.assertNotIn(invites['1nv1t3_2'].id, instance.mask_presence[maskid])
        self.assertNotVote(alpha, dm, 'gs;m dm nick badname')
        self.assertNotVote(alpha, dm, 'gs;m dm avatar http://badavatar.png')
        self.assertNotVote(alpha, dm, 'gs;m dm color #666666')
        self.assertVote(alpha, c, 'gs;m dm nick newname')

        # test different case maskid
        g._add_member(gamma)
        self.assertFalse(instance.is_member_of(maskid, gamma.id))
        self.assertVote(alpha, c, 'gs;m %s invite %s' % (maskid.upper(),
            gamma.mention))
        preinvite = c[-1]
        interact(preinvite, gamma, 'yes')
        # now the vote is happening
        self.assertNotEqual(c[-1], preinvite)
        interact(c[-1], alpha, 'yes')
        self.assertFalse(instance.is_member_of(maskid, gamma.id))
        interact(c[-1], beta, 'yes')
        self.assertTrue(instance.is_member_of(maskid, gamma.id))

        # test message deletion
        self.assertVote(alpha, c, 'gs;m new delete')
        msg = c[-1]
        msg._react(gestalt.REACT_DELETE, alpha)
        self.assertTrue(msg._deleted)
        self.assertNotIn(msg.id, instance.votes)

        # test conflicts
        self.assertVote(alpha, c, 'gs;m new rugpull')
        interact(c[-1], alpha, 'yes')
        send(alpha, c, 'gs;m rugpull invite %s' % beta.mention)
        vote = c[-1]
        self.assertCommand(alpha, c, 'gs;m rugpull leave')
        interact(vote, beta, 'yes')
        with self.assertRaises(gestalt.UserError):
            instance.get_user_proxy(send(beta, c, 'msg'), 'rugpull')

        self.assertVote(alpha, c, 'gs;m new rugpull')
        interact(c[-1], alpha, 'yes')
        self.assertVote(alpha, c, 'gs;m rugpull invite %s' % beta.mention)
        interact(c[-1], beta, 'yes')
        self.assertVote(alpha, c, 'gs;m rugpull invite %s' % gamma.mention)
        interact(c[-1], gamma, 'yes')
        self.assertCommand(alpha, c, 'gs;m rugpull rules majority')
        self.assertVote(alpha, c, 'gs;m rugpull rules dictator')
        vote = c[-1]
        maskid = instance.votes[vote.id].action.mask
        self.assertCommand(alpha, c, 'gs;m rugpull leave')
        interact(vote, beta, 'yes')
        self.assertIn(vote.id, instance.votes)
        interact(vote, gamma, 'yes')
        self.assertNotIn(vote.id, instance.votes)
        self.assertEqual(type(instance.get_rules(maskid)), gesp.RulesMajority)

        # make sure that deleted masks don't leave trash in guildmasks
        # this doesn't cause any side effects that i know of but it's bad vibes
        self.assertVote(alpha, c, 'gs;m new ghost')
        interact(c[-1], alpha, 'no')
        maskid = instance.fetchone(
                'select maskid from proxies where cmdname = "ghost"')[0]
        self.assertVote(alpha, c, 'gs;m ghost invite %s' % beta.mention)
        interact(c[-1], beta, 'yes')
        self.assertCommand(alpha, c, 'gs;m ghost rules unanimous')
        self.assertVote(alpha, c2, 'gs;m ghost add')
        interact(c2[-1], beta, 'yes')
        self.assertCommand(beta, c, 'gs;m ghost leave')
        self.assertCommand(alpha, c, 'gs;m ghost leave')
        self.assertIsNone(instance.fetchone(
            'select 1 from guildmasks where maskid = ?',
            (maskid,)))
        # TODO maybe ex-members shouldn't vote? eh.
        interact(c2[-1], alpha, 'yes')
        self.assertIsNone(instance.fetchone(
            'select 1 from guildmasks where maskid = ?',
            (maskid,)))

    def test_37_nomerge(self):
        g = Guild(name = 'merge guild')
        c = g._add_channel('main')
        g._add_member(alpha)
        g._add_member(beta)
        g._add_member(instance.user)

        self.assertVote(alpha, c, 'gs;m new nomerge')
        interact(c[-1], alpha, 'no')
        self.assertVote(alpha, c, 'gs;m nomerge invite %s' % beta.mention)
        interact(c[-1], beta, 'yes')
        self.assertVote(alpha, c, 'gs;m new other')
        interact(c[-1], alpha, 'no')
        self.assertCommand(alpha, c, 'gs;p other tags other:text')

        pad = lambda msg : msg.author.display_name.endswith(
                gestalt.MERGE_PADDING)

        self.assertCommand(alpha, c, 'gs;ap nomerge')
        self.assertCommand(beta, c, 'gs;ap nomerge')
        self.assertFalse(pad(self.assertProxied(alpha, c, 'test')))
        self.assertFalse(pad(self.assertProxied(beta, c, 'test')))
        self.assertCommand(alpha, c, 'gs;p nomerge nomerge on')
        self.assertTrue(pad(self.assertProxied(alpha, c, 'test')))
        self.assertTrue(pad(self.assertProxied(alpha, c, 'test')))
        self.assertFalse(pad(self.assertProxied(beta, c, 'test')))
        self.assertTrue(pad(self.assertProxied(alpha, c, 'test')))
        self.assertFalse(pad(self.assertProxied(alpha, c, 'other:test')))
        self.assertFalse(pad(self.assertProxied(alpha, c, 'test')))
        self.assertTrue(pad(self.assertProxied(beta, c, 'test')))
        self.assertTrue(pad(self.assertProxied(beta, c, 'test')))
        self.assertFalse(pad(self.assertProxied(alpha, c, 'other:test')))
        self.assertFalse(pad(self.assertProxied(beta, c, 'test')))
        c[-1]._react(gestalt.REACT_DELETE, beta)
        c[-1]._react(gestalt.REACT_DELETE, alpha)
        self.assertTrue(pad(self.assertProxied(beta, c, 'test')))

    def test_38_legacy(self):
        g = Guild(name = 'legacy guild')
        c = g._add_channel('main')
        role = g._add_role('leg')
        g._add_member(alpha)
        g._add_member(instance.user)

        instance.execute('insert into masks values '
                '("legacy", "", NULL, NULL, ?, 0, 0, 0)',
                (gesp.RulesLegacy(role = role.id, guild = g.id).to_json(),))
        instance.load()
        # outsider can't join
        gesp.ActionServer('legacy', g.id).execute(instance)
        self.assertNotCommand(beta, beta.dm_channel, 'gs;m legacy join')
        self.assertNotCommand(beta, beta.dm_channel, 'gs;m legacy nick legacy')
        self.assertIsNone(self.get_proxid(beta, 'legacy'))
        g._add_member(beta, perms = discord.Permissions.none())
        # unprivileged member can't join
        self.assertNotCommand(beta, c, 'gs;m legacy join')
        self.assertNotCommand(beta, c, 'gs;m legacy nick legacy')
        self.assertNotVote(beta, c, 'gs;m legacy invite %s' % alpha.mention)
        self.assertIsNone(self.get_proxid(beta, 'legacy'))
        g._remove_member(beta)
        g._add_member(beta)
        # privileged member can join
        self.assertCommand(beta, c, 'gs;m legacy join')
        self.assertCommand(beta, c, 'gs;m legacy nick legacy')
        self.assertVote(beta, c, 'gs;m legacy invite %s' % alpha.mention)
        interact(c[-1], alpha, 'yes')
        self.assertIsNotNone(self.get_proxid(beta, 'legacy'))
        self.assertIsNotNone(self.get_proxid(alpha, 'legacy'))
        g._remove_member(beta)
        self.assertNotCommand(beta, beta.dm_channel, 'gs;m legacy nick legacy')
        g._add_member(beta, perms = discord.Permissions.none())
        # unprivileged member can only change appearance
        self.assertCommand(beta, c, 'gs;m legacy nick legacy')
        self.assertNotCommand(beta, c, 'gs;m legacy rules dictator')
        g2 = Guild()._add_member(beta)._add_member(instance.user)
        c2 = g2._add_channel('main')
        self.assertNotCommand(beta, c2, 'gs;m legacy rules dictator')
        g2._add_member(alpha)
        self.assertNotCommand(alpha, c2, 'gs;m legacy add')
        self.assertCommand(alpha, c2, 'gs;m legacy rules dictator')
        self.assertCommand(alpha, c2, 'gs;m legacy add')
        self.assertNotCommand(alpha, c2, 'gs;m legacy rules legacy')

    def test_39_mask_view(self):
        for avatar in (None, 'http://avatar.png'):
            for color in (None, '#b536da'):
                for created in (None, 12):
                    maskid = instance.gen_id()
                    instance.execute('insert into masks values '
                            '(?, "mask!", ?, ?, ?, ?, 999999, 9999999999)',
                            (maskid, avatar, color,
                                gesp.RulesUnanimous().to_json(), created))
                    instance.load()
                    send(alpha, alpha.dm_channel, 'gs;mask %s' % maskid)
                    self.assertEqual(str(alpha.dm_channel[-1].embeds[0].color),
                            str(color))


def main():
    global alpha, beta, gamma, g, instance

    instance = TestBot()

    alpha = User(name = 'test-alpha')
    beta = User(name = 'test-beta')
    gamma = User(name = 'test-gamma')
    g = Guild(name = 'default guild')
    g._add_channel('main')
    g._add_member(instance.user)
    g._add_member(alpha)
    g._add_member(beta)
    g._add_member(gamma)

    if unittest.main(exit = False).result.wasSuccessful():
        print('But it isn\'t *really* OK, is it?')


# monkey patch. this probably violates the Geneva Conventions
discord.utils.utcnow = warptime.now
discord.Webhook.partial = Webhook.partial
discord.Thread = Thread
# don't spam the channel with error messages
gestalt.DEFAULT_PREFS &= ~gestalt.Prefs.errors
#gestalt.DEFAULT_PREFS |= gestalt.Prefs.replace
gestalt.commands.DEFAULT_PREFS = gestalt.DEFAULT_PREFS

gestalt.BECOME_MAX = 1


if __name__ == '__main__':
    main()

