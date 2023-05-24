#!/usr/bin/python3

from asyncio import run
import unittest
import re

import aiohttp
import discord

import gestalt


# this test harness reimplements most relevant parts of the discord API, offline
# the alternative involves maintaining *4* separate bots
# and either threading (safety not guaranteed) or switching between them (sloow)
# meanwhile, existing discord.py testing solutions don't support multiple users
# i wish it didn't have to be this way but i promise this is the best solution


class Object:
    nextid = 0
    def __init__(self, **kwargs):
        Object.nextid += 1
        self.id = Object.nextid
        self.__dict__.update(kwargs)

class User(Object):
    users = {}
    def __init__(self, **kwargs):
        self._deleted = False
        self.bot = False
        self.dm_channel = None
        self.discriminator = '0001'
        super().__init__(**kwargs)
        User.users[self.id] = self
    @property
    def mention(self):
        return '<@!%d>' % self.id
    def _delete(self):
        self._deleted = True
        del User.users[self.id]
        for guild in Guild.guilds.values():
            if self.id in guild._members:
                del guild._members[self.id]
    def __str__(self):
        return self.name + '#' + self.discriminator
    async def send(self, content = None, embed = None, file = None):
        if self.dm_channel == None:
            self.dm_channel = Channel(type = discord.ChannelType.private,
                    members = [self, instance.user])
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
    async def _add_role(self, role):
        before = self._copy()
        self.roles.append(role)
        role.members.append(self)
        await instance.on_member_update(before, self)
    async def _del_role(self, role):
        before = self._copy()
        self.roles.remove(role)
        role.remove(self)
        await instance.on_member_update(before, self)
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

class Message(Object):
    def __init__(self, embed = None, **kwargs):
        self._deleted = False
        self.webhook_id = None
        self.attachments = []
        self.reactions = []
        self.reference = None
        self.embeds = [embed] if embed else []
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
        return 'http://%i/%i/%i' % (self.guild.id, self.channel.id, self.id)
    @property
    def mentions(self):
        # mentions can also be in the embed but that's irrelevant here
        if self.content:
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
    async def _react(self, emoji, user):
        react = discord.Reaction(message = self, emoji = emoji,
                data = {'count': 1, 'me': None})
        if react not in self.reactions:
            # FIXME when more than one user adds the same reaction
            self.reactions.append(react)
        await instance.on_raw_reaction_add(
                discord.raw_models.RawReactionActionEvent(data = {
                    'message_id': self.id,
                    'user_id': user.id,
                    'channel_id': self.channel.id},
                    emoji = discord.PartialEmoji(name = emoji),
                    event_type = None))
    async def add_reaction(self, emoji):
        await self._react(emoji, instance.user)
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
    async def delete(self):
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
    async def edit_message(self, message_id, content, thread = None):
        msg = await (thread or self._channel).fetch_message(message_id)
        if self._deleted or msg.webhook_id != self.id or (thread and
                thread.parent != self._channel):
            raise NotFound()
        newmsg = Message(**msg.__dict__)
        newmsg.content = content
        msg.channel._messages[msg.channel._messages.index(msg)] = newmsg
        return newmsg
    async def fetch(self):
        if self._deleted:
            raise NotFound()
        return self
    async def send(self, username, avatar_url, thread = None, **kwargs):
        if self._deleted:
            raise NotFound()
        msg = Message(**kwargs) # note: absorbs other irrelevant arguments
        msg.webhook_id = self.id
        name = username if username else self.name
        msg.author = Object(id = self.id, bot = True,
                name = name, display_name = name,
                display_avatar = avatar_url)
        if thread and thread.parent != self._channel:
            raise NotFound()
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
    async def send(self, content = None, embed = None, file = None):
        msg = Message(author = instance.user, content = content, embed = embed)
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
            before = member._copy()
            member.roles.remove(role)
            await instance.on_member_update(before, member)
        del self._roles[role.id]
        await instance.on_guild_role_delete(role)
    async def _add_member(self, user, perms = discord.Permissions.all()):
        if user.id in self._members:
            raise RuntimeError('re-adding a member to a guild')
        member = self._members[user.id] = Member(user, self, perms)
        member.roles.append(self.default_role)
        # NOTE: does on_member_update get called here? probably not but idk
        await instance.on_member_join(member)
        return member
    def get_member(self, user_id):
        return self._members[user_id] if user_id in self._members else None
    def get_role(self, role_id):
        return self._roles[role_id]

class TestBot(gestalt.Gestalt):
    def __init__(self):
        self._user = User(name = 'Gestalt', bot = True)
        super().__init__(dbfile = ':memory:')
        self.session = ClientSession()
        self.pk_ratelimit = discord.gateway.GatewayRatelimiter(count = 1000,
                per = 1.0)
    def __del__(self):
        pass # suppress 'closing database' message
    @property
    def user(self):
        return self._user
    def get_user(self, id):
        return User.users.get(id)
    async def fetch_user(self, id):
        try:
            return User.users[id]
        except KeyError:
            raise NotFound()
    def get_channel(self, id):
        return Channel.channels.get(id, Thread.threads.get(id))

# incoming messages have Attachments, outgoing messages have Files
# but we'll pretend that they're the same for simplicity
class File:
    def __init__(self, size):
        self.size = size
    def is_spoiler(self):
        return False
    async def to_file(self, spoiler):
        return self

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

def send(user, channel, content, reference = None, files = []):
    msg = Message(author = user, content = content, reference = reference,
            attachments = files)
    run(channel._add(msg))
    return channel[-1] if msg._deleted else msg

class GestaltTest(unittest.TestCase):

    # ugly hack because parsing gs;p output would be uglier
    def get_proxid(self, user, other):
        if other == None:
            return instance.fetchone(
                    'select proxid from proxies where (userid, type) = (?, ?)',
                    (user.id, gestalt.ProxyType.override))[0]
        elif type(other) in [str, Role, RoleEveryone]:
            collid = other if type(other) == str else self.get_collid(other)
            row = instance.fetchone(
                    'select proxid from proxies '
                    'where (userid, maskid) = (?, ?)',
                    (user.id, collid))
        else:
            row = instance.fetchone(
                    'select proxid from proxies '
                    'where (userid, otherid) is (?, ?)',
                    (user.id, other.id))
        return row[0] if row else None

    def get_collid(self, role):
        row = instance.fetchone(
                'select maskid from masks where roleid = ?',
                (role.id,))
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

    def assertProxied(self, *args, **kwargs):
        msg = send(*args, **kwargs)
        self.assertIsNotNone(msg.webhook_id)
        return msg

    def assertNotProxied(self, *args, **kwargs):
        msg = send(*args, **kwargs)
        self.assertIsNone(msg.webhook_id)
        return msg

    def assertEditedContent(self, message, content):
        self.assertEqual(
                run(message.channel.fetch_message(message.id)).content,
                content)


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
        send(alpha, g['main'], 'GS;help')
        msg = g['main'][-1]
        self.assertEqual(len(msg.embeds), 1)
        self.assertReacted(msg, gestalt.REACT_DELETE)
        run(msg._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(msg._deleted)

    def test_03_add_delete_collective(self):
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
        run(g.get_member(alpha.id)._add_role(role))
        self.assertCommand(alpha, g['main'], 'gs;c new %s' % role.mention)
        proxid = self.get_proxid(alpha, role)
        self.assertIsNotNone(proxid)

        # set tags and test it
        self.assertCommand(alpha, g['main'], 'gs;p %s tags d:text' % proxid)
        self.assertProxied(alpha, g['main'], 'd:test')

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

    def test_04_permissions(self):
        collid = self.get_collid(g.default_role)
        self.assertIsNotNone(collid)
        # beta does not have manage_roles permission; this should fail
        self.assertNotCommand(beta, g['main'], 'gs;c %s delete' % collid)
        # now change the @everyone collective name; this should work
        self.assertCommand(beta, g['main'], 'gs;c %s name test' % collid)

        # beta shouldn't be able to change a collective it isn't in
        role = g._add_role('no beta')
        self.assertCommand(alpha, g['main'], 'gs;c new %s' % role.mention)
        collid = self.get_collid(role)
        self.assertIsNotNone(collid)
        self.assertNotCommand(beta, g['main'], 'gs;c %s name test' % collid)

        # users shouldn't be able to change another's proxy
        alphaid = self.get_proxid(alpha, g.default_role)
        betaid = self.get_proxid(beta, g.default_role)
        self.assertNotCommand(alpha, g['main'], 'gs;p %s auto on' % betaid)
        self.assertNotCommand(beta, g['main'], 'gs;p %s tags no:text' % alphaid)

    def test_05_tags_auto(self):
        def assertFlags(proxid, flags):
            self.assertEqual(instance.fetchone(
                'select flags from proxies where proxid = ?',
                (proxid,))[0], flags)

        # test every combo of auto, tags, and also the switches thereof
        chan = g['main']
        proxid = self.get_proxid(alpha, g.default_role)

        self.assertNotProxied(alpha, chan, 'no tags, no auto')
        self.assertProxied(alpha, chan, 'E:Tags')
        self.assertEqual(chan[-1].content, 'Tags')
        self.assertCommand(alpha, chan, 'gs;p %s tags "= text"' % proxid)
        self.assertProxied(alpha, chan, '= tags, no auto')
        self.assertEqual(chan[-1].content, 'tags, no auto')
        self.assertCommand(alpha, chan, 'gs;p %s auto on' % proxid)
        self.assertProxied(alpha, chan, '= tags, auto')
        self.assertEqual(chan[-1].content, 'tags, auto')
        self.assertProxied(alpha, chan, 'no tags, auto')
        self.assertEqual(chan[-1].content, 'no tags, auto')
        # test autoproxy both as on/off and as toggle
        self.assertCommand(alpha, chan, 'gs;p %s auto' % proxid)
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
        self.assertCommand(alpha, chan, 'gs;p %s keepproxy' % proxid)
        self.assertProxied(alpha, chan, '[message]')
        self.assertEqual(chan[-1].content, 'message')
        self.assertCommand(alpha, chan, 'gs;p %s tags e:text' % proxid)

        # test setting auto on override to unset other autos
        overid = self.get_proxid(alpha, None)
        self.assertCommand(alpha, chan, 'gs;p %s auto on' % proxid)
        self.assertProxied(alpha, chan, 'auto on')
        self.assertCommand(alpha, chan, 'gs;p %s auto on' % overid)
        self.assertNotProxied(alpha, chan, 'auto off')
        assertFlags(overid, 0)

        # test guild/global autoproxy shenanigans
        # somewhat redundant with test_global_conflicts
        self.assertCommand(alpha, chan, 'gs;swap open %s' % beta.mention)
        self.assertCommand(beta, chan, 'gs;swap open %s' % alpha.mention)
        self.assertCommand(alpha, chan, 'gs;swap open %s' % gamma.mention)
        self.assertCommand(gamma, chan, 'gs;swap open %s' % alpha.mention)
        run(g.get_member(alpha.id)._add_role(role := g._add_role('not global')))
        self.assertCommand(alpha, chan, 'gs;c new "not global"')
        self.assertCommand(alpha, chan, 'gs;p test-beta auto on')
        assertFlags(self.get_proxid(alpha, beta), 1)
        self.assertCommand(alpha, chan, 'gs;p test-gamma auto on')
        assertFlags(self.get_proxid(alpha, gamma), 1)
        assertFlags(self.get_proxid(alpha, beta), 0)
        self.assertCommand(alpha, chan, 'gs;p %s auto on' % proxid)
        assertFlags(proxid, 1)
        assertFlags(self.get_proxid(alpha, gamma), 0)
        self.assertCommand(alpha, chan, 'gs;p "not global" auto on')
        assertFlags(self.get_proxid(alpha, role), 1)
        assertFlags(proxid, 0)
        self.assertCommand(alpha, chan, 'gs;p test-beta auto on')
        assertFlags(self.get_proxid(alpha, beta), 1)
        assertFlags(self.get_proxid(alpha, role), 0)
        send(alpha, chan, 'gs;swap close test-beta')
        send(alpha, chan, 'gs;swap close test-gamma')

        # invalid tags. these should fail
        self.assertNotCommand(alpha, chan, 'gs;p %s tags ' % proxid)
        self.assertNotCommand(alpha, chan, 'gs;p %s tags text ' % proxid)
        self.assertNotCommand(alpha, chan, 'gs;p %s tags txet ' % proxid)

        # test autoproxy without tags
        # also test proxies added on role add
        newrole = g._add_role('no tags')
        self.assertCommand(alpha, chan, 'gs;c new %s' % newrole.mention)
        run(g.get_member(alpha.id)._add_role(newrole))
        proxid = self.get_proxid(alpha, newrole)
        self.assertIsNotNone(proxid)
        self.assertCommand(alpha, chan, 'gs;p %s auto' % proxid)
        self.assertProxied(alpha, chan, 'no tags, auto')
        self.assertCommand(alpha, chan, 'gs;p %s auto off' % proxid)
        self.assertNotProxied(alpha, chan, 'no tags, no auto')

        # test tag precedence over auto
        self.assertCommand(alpha, chan, 'gs;p %s auto on' % proxid)
        self.assertEqual(send(alpha, chan, 'auto').author.name, 'no tags')
        self.assertEqual(send(alpha, chan, 'e: tags').author.name, 'test')

    def test_06_query_delete(self):
        run(g._add_member(deleteme := User(name = 'deleteme')))
        chan = g['main']
        self.assertCommand(deleteme, chan, 'gs;swap open %s'
            % deleteme.mention)
        self.assertCommand(deleteme, chan, 'gs;p deleteme tags e:text')
        msg = send(deleteme, chan, 'e:reaction test')
        run(msg._react(gestalt.REACT_QUERY, beta))
        self.assertNotEqual(beta.dm_channel[-1].content.find(str(deleteme)), -1)

        run(msg._react(gestalt.REACT_DELETE, beta))
        self.assertEqual(len(msg.reactions), 0)
        self.assertFalse(msg._deleted)

        run(msg._react(gestalt.REACT_DELETE, deleteme))
        self.assertTrue(msg._deleted)

        msg = send(deleteme, chan, 'e:bye!')
        deleteme._delete()
        self.assertIsNone(instance.get_user(deleteme.id))
        with self.assertRaises(NotFound):
            run(instance.fetch_user(deleteme.id))
        send(beta, beta.dm_channel, 'buffer')
        run(msg._react(gestalt.REACT_QUERY, beta))
        self.assertNotEqual(beta.dm_channel[-1].content.find(str(deleteme)), -1)

        # in swaps, sender or swapee may delete message
        self.assertCommand(alpha, chan,
                'gs;swap open %s swap:text' % beta.mention)
        self.assertCommand(beta, chan, 'gs;swap open %s' % alpha.mention)
        msg = self.assertProxied(alpha, chan, 'swap:delete me')
        run(msg._react(gestalt.REACT_DELETE, gamma))
        self.assertFalse(msg._deleted)
        run(msg._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(msg._deleted)
        msg = self.assertProxied(alpha, chan, 'swap:delete me')
        run(msg._react(gestalt.REACT_DELETE, beta))
        self.assertTrue(msg._deleted)
        self.assertCommand(alpha, chan,
                'gs;swap close %s' % self.get_proxid(alpha, beta))

        # test DMs
        msg1 = beta.dm_channel[-1]
        msg2 = send(beta, beta.dm_channel, 'test')
        run(msg1._react(gestalt.REACT_DELETE, beta))
        self.assertTrue(msg1._deleted)
        run(msg2._react(gestalt.REACT_DELETE, beta))
        self.assertFalse(msg2._deleted)

        # and finally normal messages
        msg = self.assertNotProxied(beta, chan, "we're just normal messages")
        buf = send(beta, beta.dm_channel, "we're just innocent messages")
        run(msg._react(gestalt.REACT_QUERY, beta))
        run(msg._react(gestalt.REACT_DELETE, beta))
        self.assertFalse(msg._deleted)
        self.assertEqual(len(msg.reactions), 2)
        self.assertEqual(beta.dm_channel[-1], buf)

    def test_07_webhook_shenanigans(self):
        # test what happens when a webhook is deleted
        hookid = send(alpha, g['main'], 'e:reiuskudfvb').webhook_id
        self.assertIsNotNone(hookid)
        self.assertRowExists(
                'select 1 from webhooks where hookid = ?',
                (hookid,))
        Webhook.hooks[hookid]._deleted = True
        msg = send(alpha, g['main'], 'e:asdhgdfjg')
        newhook = msg.webhook_id
        self.assertIsNotNone(newhook)
        self.assertNotEqual(hookid, newhook)
        self.assertRowNotExists(
                'select 1 from webhooks where hookid = ?',
                (hookid,))
        self.assertRowExists(
                'select 1 from webhooks where hookid = ?',
                (newhook,))

        send(alpha, g['main'], 'gs;e nice edit')
        self.assertEditedContent(msg, 'nice edit')
        Webhook.hooks[newhook]._deleted = True
        self.assertReacted(send(alpha, g['main'], 'gs;e evil edit!'),
                gestalt.REACT_DELETE)
        self.assertEditedContent(msg, 'nice edit')
        self.assertRowNotExists(
                'select 1 from webhooks where hookid = ?',
                (newhook,))

    # this function requires the existence of at least three ongoing wars
    def test_08_global_conflicts(self):
        g2 = Guild()
        g2._add_channel('main')
        run(g2._add_member(instance.user))
        run(g2._add_member(alpha))

        rolefirst = g._add_role('conflict')
        rolesecond = g2._add_role('conflict')
        run(g.get_member(alpha.id)._add_role(rolefirst))
        run(g.get_member(alpha.id)._add_role(rolesecond))

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

        # create collectives on the two roles
        self.assertCommand(
                alpha, g['main'], 'gs;c new %s' % rolefirst.mention)
        self.assertCommand(
                alpha, g2['main'], 'gs;c new %s' % rolesecond.mention)
        proxfirst = self.get_proxid(alpha, rolefirst)
        proxsecond = self.get_proxid(alpha, rolesecond)
        self.assertIsNotNone(proxfirst)
        self.assertIsNotNone(proxsecond)

        # now alpha can test tags and auto stuff
        self.assertCommand(
                alpha, g['main'], 'gs;p %s tags same:text' % proxfirst)
        # this should work because the collectives are in different guilds
        self.assertCommand(
                alpha, g2['main'], 'gs;p %s tags same:text' % proxsecond)
        self.assertProxied(alpha, g['main'], 'same: no auto')
        # alpha should be able to set both to auto; different guilds
        self.assertCommand(
                alpha, g['main'], 'gs;p %s auto on' % proxfirst)
        self.assertCommand(
                alpha, g2['main'], 'gs;p %s auto on' % proxsecond)
        self.assertProxied(alpha, g['main'], 'auto on')
        self.assertProxied(alpha, g2['main'], 'auto on')

        # test global tags conflict; this should fail
        self.assertNotCommand(
                alpha, g['main'], 'gs;p %s tags same:text' % proxswap)
        # no conflict; this should work
        self.assertCommand(
                alpha, g['main'], 'gs;p %s tags swap:text' % proxswap)
        # make a conflict with a collective
        self.assertNotCommand(
                alpha, g['main'], 'gs;p %s tags swap:text' % proxfirst)
        # now turning on auto on the swap should deactivate the other autos
        self.assertProxied(alpha, g['main'], 'auto on')
        self.assertNotEqual(
                send(alpha, g['main'], 'collective has auto').author.name.index(
                    rolefirst.name), -1)
        self.assertCommand(alpha, g['main'], 'gs;p %s auto on' % proxswap)
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
        proxid = self.get_proxid(alpha, g.default_role)
        self.assertIsNotNone(proxid)

        chan = g['main']
        self.assertProxied(alpha, chan, 'e: proxy')
        self.assertCommand(alpha, chan, 'gs;p %s auto on' % proxid)
        self.assertProxied(alpha, chan, 'proxy')
        # set the override tags. this should activate it
        self.assertCommand(alpha, chan, 'gs;p %s tags x:text' % overid)
        self.assertNotProxied(alpha, chan, 'x: not proxies')

        # turn autoproxy off
        self.assertCommand(alpha, chan, 'gs;p %s auto' % proxid)
        self.assertNotProxied(alpha, chan, 'not proxied')
        self.assertNotProxied(alpha, chan, 'x: not proxied')

    # by far the most ominous test
    def test_10_replacements(self):
        chan = g['main']
        before = 'I am myself. i was and am. I\'m. im. am I? I me my mine.'
        after = (
                'We are Ourselves. We were and are. We\'re. We\'re. are We? '
                'We Us Our Ours.')
        self.assertCommand(alpha, chan, 'gs;account config replace off')
        msg = send(alpha, chan, 'e:' + before)
        self.assertEqual(msg.content, before)
        self.assertCommand(alpha, chan, 'gs;a config replace')
        msg = send(alpha, chan, 'e:' + before)
        self.assertEqual(msg.content, after)
        self.assertCommand(alpha, chan, 'gs;a config replace off')
        self.assertCommand(alpha, chan, 'gs;a config defaults')
        self.assertEqual(msg.content, after)

    def test_11_avatar_url(self):
        chan = g['main']
        collid = self.get_collid(g.default_role)
        self.assertCommand(alpha, chan, 'gs;c %s avatar http://a' % collid)
        self.assertCommand(alpha, chan, 'gs;c %s avatar https://a' % collid)
        self.assertNotCommand(alpha, chan, 'gs;c %s avatar http:/a' % collid)
        self.assertNotCommand(alpha, chan, 'gs;c %s avatar https:/a' % collid)
        self.assertNotCommand(alpha, chan, 'gs;c %s avatar _https://a' % collid)
        self.assertNotCommand(alpha, chan, 'gs;c %s avatar foobar' % collid)
        self.assertCommand(alpha, chan, 'gs;c %s avatar' % collid)
        self.assertCommand(alpha, chan, 'gs;c %s avatar ""' % collid)

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
        self.assertCommand(alpha, chan, 'gs;a config latch')
        self.assertNotProxied(alpha, chan, 'no proxy, no auto')
        self.assertProxied(alpha, chan, 'e: proxy, no auto')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertProxied(alpha, chan, 'e: proxy, auto')
        self.assertNotProxied(alpha, chan, 'x: override')
        self.assertNotProxied(alpha, chan, 'no proxy, no auto')

        # test \escape and \\unlatch
        proxid = self.get_proxid(alpha, g.default_role)
        self.assertProxied(alpha, chan, 'e: proxy, no auto')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertNotProxied(alpha, chan, '\escape')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertNotProxied(alpha, chan, '\\\\unlatch')
        self.assertNotProxied(alpha, chan, 'no proxy, no auto')
        self.assertCommand(alpha, chan, 'gs;a config latch off')

        self.assertCommand(alpha, chan, 'gs;p %s auto on' % proxid)
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertNotProxied(alpha, chan, '\escape')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertNotProxied(alpha, chan, '\\\\unlatch')
        self.assertProxied(alpha, chan, 'no proxy, auto')
        self.assertCommand(alpha, chan, 'gs;p %s auto off' % proxid)

    # test member joining when the guild has an @everyone collective
    def test_14_member_join(self):
        user = User(name = 'test-joining')
        run(g._add_member(user))
        self.assertIsNotNone(self.get_proxid(user, g.default_role))

    def test_15_case(self):
        proxid = self.get_proxid(alpha, g.default_role).upper()
        self.assertIsNotNone(proxid)
        collid = self.get_collid(g.default_role).upper()
        self.assertCommand(alpha, g['main'], 'gs;p %s auto off' % proxid)
        self.assertCommand(alpha, g['main'], 'gs;c %s name test' % collid)

    def test_16_replies(self):
        chan = g['main']
        msg = self.assertProxied(alpha, chan, 'e: no reply')
        self.assertEqual(len(msg.embeds), 0)
        reply = self.assertProxied(alpha, chan, 'e: reply',
                Object(cached_message = msg))
        self.assertEqual(len(reply.embeds), 1)
        self.assertEqual(reply.embeds[0].description,
                '**[Reply to:](%s)** no reply' % msg.jump_url)
        # again, but this time the message isn't in cache
        reply = self.assertProxied(alpha, chan, 'e: reply',
                Object(cached_message = None, message_id = msg.id))
        self.assertEqual(len(reply.embeds), 1)
        self.assertEqual(reply.embeds[0].description,
                '**[Reply to:](%s)** no reply' % msg.jump_url)

    def test_17_edit(self):
        chan = g._add_channel('edit')
        first = self.assertProxied(alpha, chan, 'e: fisrt')
        self.assertNotCommand(alpha, chan, 'gs;e')
        second = self.assertProxied(alpha, chan, 'e: secnod')

        msg = send(beta, chan, 'gs;edit second')
        self.assertReacted(msg, gestalt.REACT_DELETE)
        self.assertEditedContent(second, 'secnod')
        msg = send(beta, chan, 'gs;edit first', Object(message_id = first.id))
        self.assertReacted(msg, gestalt.REACT_DELETE)
        self.assertEditedContent(first, 'fisrt')

        send(alpha, chan, 'gs;edit second')
        self.assertEditedContent(second, 'second')
        send(alpha, chan, 'gs;edit first', Object(message_id = first.id))
        self.assertEditedContent(first, 'first')

        self.assertCommand(beta, chan,
            'gs;p %s tags e: text' % self.get_proxid(beta, g.default_role))
        first = send(alpha, chan, 'e: edti me')
        run(send(alpha, chan, 'e: delete me').delete())
        run(send(alpha, chan, 'e: delete me too')._bulk_delete())
        self.assertProxied(beta, chan, 'e: dont edit me')
        send(alpha, chan, 'gs;help this message should be ignored')
        send(alpha, chan, 'gs;edit edit me');
        self.assertEditedContent(first, 'edit me');

        # make sure that gs;edit on a non-webhook message doesn't cause problems
        # delete "or proxied.webhook_id != hook[1]" to see this be a problem
        # this is very important, because if this fails,
        # there could be a path to a per-channel denial of service
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
        proxid = self.get_proxid(alpha, g.default_role)
        self.assertNotCommand(beta, chan, 'gs;become %s' % proxid)

        self.assertCommand(alpha, chan, 'gs;become %s' % proxid)
        self.assertNotProxied(alpha, chan, 'not proxied')
        self.assertProxied(alpha, chan, 'proxied')

    def test_19_swap_close(self):
        chan = g['main']
        self.assertCommand(alpha, chan, 'gs;swap open %s' % beta.mention)
        self.assertCommand(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, beta))
        self.assertCommand(alpha, chan, 'gs;swap open %s' % beta.mention)
        self.assertCommand(beta, chan, 'gs;swap open %s' % alpha.mention)
        self.assertNotCommand(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, None))
        self.assertNotCommand(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, g.default_role))
        self.assertNotCommand(alpha, chan, 'gs;swap close aaaaaa')
        self.assertCommand(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, beta))

    def test_20_collective_delete(self):
        g1 = Guild()
        c1 = g1._add_channel('main')
        run(g1._add_member(instance.user))
        run(g1._add_member(alpha, perms = discord.Permissions(
            manage_roles = True)))
        run(g1._add_member(beta, perms = discord.Permissions(
            manage_roles = False)))
        g2 = Guild()
        c2 = g2._add_channel('main')
        run(g2._add_member(instance.user))
        run(g2._add_member(beta, perms = discord.Permissions(
            manage_roles = True)))

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

    def test_21_attachments(self):
        g1 = Guild()
        c = g1._add_channel('main')
        run(g1._add_member(instance.user))
        run(g1._add_member(alpha, perms = discord.Permissions(
            manage_roles = True)))

        send(alpha, c, 'gs;c new everyone')
        send(alpha, c, 'gs;p %s tags [text'
                % self.get_proxid(alpha, g1.default_role))
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

    def test_22_names(self):
        g1 = Guild(name = 'guildy guild')
        c = g1._add_channel('main')
        run(g1._add_member(instance.user))
        run(g1._add_member(alpha, perms = discord.Permissions(
            manage_roles = True)))
        run(g1._add_member(beta))

        send(alpha, c, 'gs;c new everyone')
        self.assertCommand(alpha, c, 'gs;p "guildy guild" tags [text')
        self.assertEqual(send(alpha, c, '[no proxid!').author.name,
                'guildy guild')
        self.assertCommand(alpha, c, 'gs;p "guildy guild" rename "guild"')
        self.assertCommand(alpha, c, 'gs;p guild auto on')
        self.assertEqual(send(alpha, c, 'yay!').author.name, 'guildy guild')
        self.assertCommand(alpha, c, 'gs;become guild')
        self.assertNotProxied(alpha, c, 'not proxied')
        self.assertProxied(alpha, c, 'proxied')
        self.assertCommand(alpha, c, 'gs;p guild auto off')

        send(alpha, c, 'gs;swap open %s' % beta.mention)
        send(beta, c, 'gs;swap open %s' % alpha.mention)
        self.assertCommand(alpha, c, 'gs;p test-beta tags b:text')
        self.assertNotCommand(beta, c, 'gs;swap close test-beta')
        self.assertCommand(alpha, c, 'gs;swap close test-beta')

        self.assertNotCommand(beta, c, 'gs;p guild tags g:text')
        self.assertNotCommand(beta, c, 'gs;p guild auto on')
        self.assertNotCommand(beta, c, 'gs;become guild')
        self.assertNotProxied(alpha, c, 'not proxied')

        self.assertCommand(alpha, c, 'gs;c "guild" name guild!')
        self.assertEqual(send(alpha, c, '[proxied').author.name, 'guild!')
        self.assertCommand(alpha, c, 'gs;c guild avatar http://newavatar')
        self.assertEqual(send(alpha, c, '[proxied').author.display_avatar,
                'http://newavatar')
        self.assertNotCommand(beta, c, 'gs;c guild name guild')
        instance.get_user_proxy(send(alpha, c, 'command'), 'guild')
        self.assertCommand(alpha, c, 'gs;c guild delete')
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(c[-1], 'guild')

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
        self.assertNotCommand(beta, c, 'gs;p test-alpha auto on')
        # self.assertNotCommand(send(beta, c, 'gs;swap close test-alpha'))
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(c[-1], 'test-alpha')
        # self.assertCommand(alpha, c, 'gs;swap close test-beta')
        instance.execute('delete from proxies where proxid = ?', (proxid,))

    def test_23_pk_swap(self):
        g1 = Guild(name = 'guildy guild')
        c = g1._add_channel('main')
        run(g1._add_member(instance.user))
        run(g1._add_member(alpha))
        run(g1._add_member(beta))
        run(g1._add_member(gamma))
        pkhook = Webhook(c, 'pk webhook')

        instance.session._add('/systems/' + str(alpha.id), '{"id": "exmpl"}')
        instance.session._add('/members/aaaaa',
                '{"system": "exmpl", "uuid": "a-a-a-a-a", "name": "member!", '
                '"color": "123456"}')

        self.assertCommand(alpha, c, 'gs;swap open %s' % beta.mention)
        # swap needs to be active
        self.assertNotCommand(alpha, c, 'gs;pk swap test-beta aaaaa')
        self.assertCommand(beta, c, 'gs;swap open %s' % alpha.mention)
        self.assertCommand(alpha, c, 'gs;pk swap test-beta aaaaa')
        # shouldn't work twice
        self.assertNotCommand(alpha, c, 'gs;pk swap test-beta aaaaa')
        # should be able to send to two users
        self.assertCommand(alpha, c, 'gs;swap open %s' % gamma.mention)
        self.assertCommand(gamma, c, 'gs;swap open %s' % alpha.mention)
        self.assertCommand(alpha, c, 'gs;pk swap test-gamma aaaaa')
        self.assertNotCommand(alpha, c, 'gs;pk swap test-gamma aaaaa')
        self.assertIsNotNone(instance.get_user_proxy(send(gamma, c, 'a'),
            'member!'))
        # should be deleted upon swap close
        self.assertCommand(alpha, c, 'gs;swap close test-gamma')
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(send(gamma, c, 'a'), 'member!')
        # handle PluralKit linked accounts
        instance.session._add('/systems/' + str(gamma.id), '{"id": "exmpl"}')
        self.assertCommand(beta, c, 'gs;swap open %s' % gamma.mention)
        self.assertCommand(gamma, c, 'gs;swap open %s' % beta.mention)
        self.assertNotCommand(gamma, c, 'gs;pk swap test-beta aaaaa')

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

        # make sure other "mask" commands don't work
        self.assertNotCommand(beta, c, 'gs;c pk-a-a-a-a-a name nope')
        self.assertNotCommand(beta, c,
            'gs;c pk-a-a-a-a-a avatar http://nope.png')
        self.assertNotCommand(beta, c, 'gs;c pk-a-a-a-a-a delete')

        # test closing specific pkswap
        # first by receipt
        instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        self.assertCommand(alpha, c, 'gs;pk close "test-beta\'s member!"')
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta\'s member!')
        instance.get_user_proxy(send(beta, c, 'a'), 'test-alpha')
        instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta')

        # then by pkswap
        self.assertCommand(alpha, c, 'gs;pk swap test-beta aaaaa')
        instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        self.assertCommand(beta, c, 'gs;pk close member!')
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta\'s member!')
        instance.get_user_proxy(send(beta, c, 'a'), 'test-alpha')
        instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta')

        self.assertNotCommand(beta, c, 'gs;pk close test-alpha')
        self.assertCommand(beta, c, 'gs;swap close test-alpha')

    def test_24_logs(self):
        g1 = Guild(name = 'logged guild')
        c = g1._add_channel('main')
        log = g1._add_channel('log')
        run(g1._add_member(instance.user))
        run(g1._add_member(alpha))

        self.assertCommand(alpha, c, 'gs;c new everyone')
        self.assertCommand(alpha, c, 'gs;p "logged guild" tags g:text')
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

    def test_25_threads(self):
        g1 = Guild(name = 'thready guild')
        c = g1._add_channel('main')
        run(g1._add_member(instance.user))
        run(g1._add_member(alpha))
        run(g1._add_member(beta))
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
        run(msg._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(msg._deleted)
        self.assertFalse(cmd._deleted)
        run(cmd._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(cmd._deleted)

        c2 = g1._add_channel('general')
        th2 = Thread(c2, name = 'epic thread')
        self.assertReacted(send(alpha, th2, 'gs;edit no messages yet'),
                gestalt.REACT_DELETE)
        msg = self.assertProxied(beta, th2,
                'alpha:what if the first proxied message is in a thread')
        msg = self.assertProxied(alpha, c2, 'beta:everything still works right')
        self.assertRowExists(
                'select 1 from webhooks where (chanid, hookid) = (?, ?)',
                (c2.id, msg.webhook_id))
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
        run(g1._add_member(instance.user))
        run(g1._add_member(alpha))
        run(g1._add_member(beta))

        # pk swap colors are already tested
        role = g1._add_role('role')
        self.assertCommand(alpha, c, 'gs;c new %s' % role.mention)
        run(g1.get_member(alpha.id)._add_role(role))
        self.assertCommand(alpha, c, 'gs;p role tags c:text')
        self.assertCommand(alpha, c, 'gs;swap open %s beta:text'
                % beta.mention)
        self.assertCommand(beta, c, 'gs;swap open %s' % alpha.mention)

        # collectives
        target = self.assertProxied(alpha, c, 'c:message')
        msg = self.assertProxied(alpha, c, 'c:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertIsNone(msg.embeds[0].color)
        self.assertCommand(alpha, c, 'gs;c role color rose')
        msg = self.assertProxied(alpha, c, 'c:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertEqual(str(msg.embeds[0].color).upper(),
                gestalt.NAMED_COLORS['rose'])
        self.assertNotCommand(beta, c, 'gs;c role color john')
        msg = self.assertProxied(alpha, c, 'c:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertEqual(str(msg.embeds[0].color).upper(),
                gestalt.NAMED_COLORS['rose'])
        self.assertCommand(alpha, c, 'gs;c role color -clear')
        msg = self.assertProxied(alpha, c, 'c:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertIsNone(msg.embeds[0].color)

        # swaps
        target = self.assertProxied(alpha, c, 'beta:message')
        msg = self.assertProxied(alpha, c, 'beta:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertIsNone(msg.embeds[0].color)
        self.assertCommand(beta, c, 'gs;account color #888888')
        msg = self.assertProxied(alpha, c, 'beta:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertEqual(str(msg.embeds[0].color), '#888888')
        self.assertCommand(beta, c, 'gs;account color -clear')
        msg = self.assertProxied(alpha, c, 'beta:reply',
            Object(cached_message = None, message_id = target.id))
        self.assertIsNone(msg.embeds[0].color)

        # also test that updating collectives is safe
        self.assertNotCommand(alpha, c, 'gs;c role type 5')

        self.assertCommand(beta, c, 'gs;swap close test-alpha')


def main():
    global alpha, beta, gamma, g, instance

    instance = TestBot()

    alpha = User(name = 'test-alpha')
    beta = User(name = 'test-beta')
    gamma = User(name = 'test-gamma')
    g = Guild()
    g._add_channel('main')
    run(g._add_member(instance.user))
    run(g._add_member(alpha))
    run(g._add_member(beta, perms = discord.Permissions(
        # these don't actually matter other than beta not having manage_roles
        add_reactions = True,
        read_messages = True,
        send_messages = True)))
    run(g._add_member(gamma))

    if unittest.main(exit = False).result.wasSuccessful():
        print('But it isn\'t *really* OK, is it?')


# monkey patch. this probably violates the Geneva Conventions
discord.Webhook.partial = Webhook.partial
discord.Thread = Thread
# don't spam the channel with error messages
gestalt.DEFAULT_PREFS &= ~gestalt.Prefs.errors
gestalt.DEFAULT_PREFS |= gestalt.Prefs.replace

gestalt.BECOME_MAX = 1


if __name__ == '__main__':
    main()

