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
                    members = [self, bot])
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
    def clean_content(self):
        return self.content # only used in reply embeds
    @property
    def jump_url(self):
        return 'http://%i' % self.id
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
        await self._react(emoji, bot)
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
    async def edit_message(self, message_id, content):
        msg = await self._channel.fetch_message(message_id)
        if self._deleted or msg.webhook_id != self.id:
            raise NotFound()
        newmsg = Message(**msg.__dict__)
        newmsg.content = content
        msg.channel._messages[msg.channel._messages.index(msg)] = newmsg
        return newmsg
    async def send(self, username, avatar_url, **kwargs):
        if self._deleted:
            raise NotFound()
        msg = Message(**kwargs) # note: absorbs other irrelevant arguments
        msg.webhook_id = self.id
        name = username if username else self.name
        msg.author = Object(id = self.id, bot = True,
                name = name, display_name = name,
                display_avatar = avatar_url)
        await self._channel._add(msg)
        return msg

class Channel(Object):
    channels = {}
    def __init__(self, **kwargs):
        self._messages = []
        self.name = ''
        self.guild = None
        self.type = discord.ChannelType.text
        super().__init__(**kwargs)
        Channel.channels[self.id] = self
    def __getitem__(self, key):
        return self._messages[key]
    @property
    def members(self):
        return self.guild.members
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
        return discord.utils.get(self._messages, id = msgid)
    def get_partial_message(self, msgid):
        return PartialMessage(channel = self, id = msgid)
    def permissions_for(self, user):
        # TODO need channel-level permissions
        return self.guild.get_member(user.id).guild_permissions
    async def send(self, content = None, embed = None, file = None):
        msg = Message(author = bot, content = content, embed = embed)
        await self._add(msg)
        return msg

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
        super().__init__(dbfile = ':memory:')
        self.session = ClientSession()
        self.pk_ratelimit = discord.gateway.GatewayRatelimiter(count = 1000,
                per = 1.0)
    def __del__(self):
        pass # suppress 'closing database' message
    @property
    def user(self):
        return bot
    def get_user(self, id):
        return User.users.get(id)
    async def fetch_user(self, id):
        try:
            return User.users[id]
        except KeyError:
            raise NotFound()
    def get_channel(self, id):
        return Channel.channels[id]

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
        self.assertEqual(msg.reactions[0].emoji, reaction)

    def assertNotReacted(self, msg):
        self.assertEqual(len(msg.reactions), 0)


    # tests run in alphabetical order, so they are numbered to ensure order
    def test_01_swaps(self):
        chan = g['main']

        # alpha opens a swap with beta
        self.assertReacted(
                send(alpha, chan, 'gs;swap open %s sw text' % beta.mention))
        alphaid = self.get_proxid(alpha, beta)
        self.assertIsNotNone(alphaid)
        self.assertIsNotNone(self.get_proxid(beta, alpha))
        self.assertEqual(instance.fetchone(
            'select state from proxies where proxid = ?', (alphaid,))[0],
            gestalt.ProxyState.inactive)
        # simply trying to open the swap again doesn't work
        self.assertNotReacted(
                send(alpha, chan, 'gs;swap open %s' % beta.mention))
        self.assertEqual(instance.fetchone(
            'select state from proxies where proxid = ?', (alphaid,))[0],
            gestalt.ProxyState.inactive)

        # alpha tests swap; it should fail
        self.assertIsNone(send(alpha, chan, 'sw no swap').webhook_id)
        # beta opens swap
        self.assertReacted(
                send(beta, chan, 'gs;swap open %s sw text' % alpha.mention))
        self.assertIsNotNone(self.get_proxid(alpha ,beta))
        self.assertIsNotNone(self.get_proxid(beta, alpha))
        # alpha and beta test the swap; it should work now
        self.assertIsNotNone(send(alpha, chan, 'sw swap').webhook_id)
        self.assertIsNotNone(send(beta, chan, 'sw swap').webhook_id)

        # try to redundantly open another swap; it should fail
        self.assertNotReacted(
                send(alpha, chan, 'gs;swap open %s' % beta.mention))
        self.assertNotReacted(
                send(beta, chan, 'gs;swap open %s' % alpha.mention))
        self.assertEqual(instance.fetchone(
            'select count() from proxies where (userid, otherid) = (?, ?)',
            (alpha.id, beta.id))[0], 1)
        self.assertEqual(instance.fetchone(
            'select count() from proxies where (userid, otherid) = (?, ?)',
            (beta.id, alpha.id))[0], 1)

        # now, with the alpha-beta swap active, alpha opens swap with gamma
        # this one should fail due to tags conflict
        self.assertNotReacted(
                send(alpha, chan, 'gs;swap open %s sw text' % gamma.mention))
        # but this one should work
        self.assertReacted(
                send(alpha, chan, 'gs;swap open %s sww text' % gamma.mention))
        # gamma opens the swap
        self.assertReacted(
                send(gamma, chan, 'gs;swap open %s sww text' % alpha.mention))
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
        self.assertReacted(send(alpha, chan, 'gs;swap close %s' % alphaid))
        self.assertIsNone(self.get_proxid(alpha, beta))
        self.assertIsNone(self.get_proxid(beta, alpha))
        self.assertIsNotNone(self.get_proxid(alpha, gamma))
        self.assertIsNotNone(self.get_proxid(gamma, alpha))
        self.assertReacted(
                send(gamma, chan, 'gs;swap close %s' % gammaid))
        self.assertIsNone(self.get_proxid(alpha, gamma))
        self.assertIsNone(self.get_proxid(gamma, alpha))
        self.assertIsNone(send(beta, chan, 'sw no swap').webhook_id)
        self.assertIsNone(send(gamma, chan, 'sww no swap').webhook_id)

        # test self-swaps
        self.assertReacted(send(alpha, chan, 'gs;swap open %s' % alpha.mention))
        self.assertIsNotNone(self.get_proxid(alpha, alpha))
        self.assertReacted(send(alpha, chan, 'gs;p test-alpha tags me:text'))
        self.assertIsNotNone(send(alpha, chan, 'me:self-swap').webhook_id)
        self.assertEqual(chan[-1].author.name, 'test-alpha')
        self.assertNotReacted(
                send(alpha, chan, 'gs;swap open %s' % alpha.mention))
        self.assertEqual(instance.fetchone(
            'select count() from proxies where (userid, otherid) = (?, ?)',
            (alpha.id, alpha.id))[0], 1)
        self.assertReacted(send(alpha, chan, 'gs;swap close test-alpha'))
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
        self.assertReacted(send(alpha, g['main'], 'gs;c new "everyone"'))
        # make sure it worked
        self.assertIsNotNone(self.get_collid(g.default_role))
        # try to make a collective on the same role; it shouldn't work
        self.assertNotReacted(send(alpha, g['main'], 'gs;c new "everyone"'))

        proxid = self.get_proxid(alpha, g.default_role)
        self.assertIsNotNone(proxid)
        # set the tags
        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags e:text' % proxid))
        # test the proxy
        self.assertIsNotNone(send(alpha, g['main'], 'e:test').webhook_id)
        # this proxy will be used in later tests


        # now try again with a new role
        role = g._add_role('delete me')
        # add the role to alpha, then create collective
        run(g.get_member(alpha.id)._add_role(role))
        self.assertReacted(send(alpha, g['main'], 'gs;c new %s' % role.mention))
        proxid = self.get_proxid(alpha, role)
        self.assertIsNotNone(proxid)

        # set tags and test it
        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags d:text' % proxid))
        self.assertIsNotNone(send(alpha, g['main'], 'd:test').webhook_id)

        # delete the collective normally
        collid = self.get_collid(role)
        self.assertReacted(send(alpha, g['main'], 'gs;c %s delete' % collid))
        self.assertIsNone(self.get_collid(role))
        self.assertIsNone(self.get_proxid(alpha, collid))

        # recreate the collective, then delete the role
        self.assertReacted(send(alpha, g['main'], 'gs;c new %s' % role.mention))
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
        self.assertNotReacted(send(beta, g['main'], 'gs;c %s delete' % collid))
        # now change the @everyone collective name; this should work
        self.assertReacted(send(beta, g['main'], 'gs;c %s name test' % collid))

        # beta shouldn't be able to change a collective it isn't in
        role = g._add_role('no beta')
        self.assertReacted(send(alpha, g['main'], 'gs;c new %s' % role.mention))
        collid = self.get_collid(role)
        self.assertIsNotNone(collid)
        self.assertNotReacted(
                send(beta, g['main'], 'gs;c %s name test' % collid))

        # users shouldn't be able to change another's proxy
        alphaid = self.get_proxid(alpha, g.default_role)
        betaid = self.get_proxid(beta, g.default_role)
        self.assertNotReacted(
                send(alpha, g['main'], 'gs;p %s auto on' % betaid))
        self.assertNotReacted(
                send(beta, g['main'], 'gs;p %s tags no:text' % alphaid))

    def test_05_tags_auto(self):
        def assertFlags(proxid, flags):
            self.assertEqual(instance.fetchone(
                'select flags from proxies where proxid = ?',
                (proxid,))[0], flags)

        # test every combo of auto, tags, and also the switches thereof
        chan = g['main']
        proxid = self.get_proxid(alpha, g.default_role)

        self.assertIsNone(send(alpha, chan, 'no tags, no auto').webhook_id)
        self.assertIsNotNone(send(alpha, chan, 'E:Tags').webhook_id)
        self.assertEqual(chan[-1].content, 'Tags')
        self.assertReacted(send(alpha, chan, 'gs;p %s tags "= text"' % proxid))
        self.assertIsNotNone(send(alpha, chan, '= tags, no auto').webhook_id)
        self.assertEqual(chan[-1].content, 'tags, no auto')
        self.assertReacted(send(alpha, chan, 'gs;p %s auto on' % proxid))
        self.assertIsNotNone(send(alpha, chan, '= tags, auto').webhook_id)
        self.assertEqual(chan[-1].content, 'tags, auto')
        self.assertIsNotNone(send(alpha, chan, 'no tags, auto').webhook_id)
        self.assertEqual(chan[-1].content, 'no tags, auto')
        # test autoproxy both as on/off and as toggle
        self.assertReacted(send(alpha, chan, 'gs;p %s auto' % proxid))
        self.assertReacted(send(alpha, chan, 'gs;p %s tags #text#' % proxid))
        self.assertIsNotNone(send(alpha, chan, '#tags, no auto#').webhook_id)
        self.assertEqual(chan[-1].content, 'tags, no auto')
        self.assertReacted(send(alpha, chan, 'gs;p %s tags text]' % proxid))
        self.assertIsNotNone(send(alpha, chan, 'postfix tag]').webhook_id)

        # test keepproxy
        self.assertReacted(send(alpha, chan, 'gs;p %s tags [text]' % proxid))
        self.assertReacted(send(alpha, chan, 'gs;p %s keepproxy on' % proxid))
        self.assertIsNotNone(send(alpha, chan, '[message]').webhook_id)
        self.assertEqual(chan[-1].content, '[message]')
        self.assertReacted(send(alpha, chan, 'gs;p %s keepproxy' % proxid))
        self.assertIsNotNone(send(alpha, chan, '[message]').webhook_id)
        self.assertEqual(chan[-1].content, 'message')
        self.assertReacted(send(alpha, chan, 'gs;p %s tags e:text' % proxid))

        # test setting auto on override to unset other autos
        overid = self.get_proxid(alpha, None)
        self.assertReacted(send(alpha, chan, 'gs;p %s auto on' % proxid))
        self.assertIsNotNone(send(alpha, chan, 'auto on').webhook_id)
        self.assertReacted(send(alpha, chan, 'gs;p %s auto on' % overid))
        self.assertIsNone(send(alpha, chan, 'auto off').webhook_id)
        assertFlags(overid, 0)

        # test guild/global autoproxy shenanigans
        # somewhat redundant with test_global_conflicts
        self.assertReacted(send(alpha, chan, 'gs;swap open %s' % beta.mention))
        self.assertReacted(send(beta, chan, 'gs;swap open %s' % alpha.mention))
        self.assertReacted(send(alpha, chan, 'gs;swap open %s' % gamma.mention))
        self.assertReacted(send(gamma, chan, 'gs;swap open %s' % alpha.mention))
        run(g.get_member(alpha.id)._add_role(role := g._add_role('not global')))
        self.assertReacted(send(alpha, chan, 'gs;c new "not global"'))
        self.assertReacted(send(alpha, chan, 'gs;p test-beta auto on'))
        assertFlags(self.get_proxid(alpha, beta), 1)
        self.assertReacted(send(alpha, chan, 'gs;p test-gamma auto on'))
        assertFlags(self.get_proxid(alpha, gamma), 1)
        assertFlags(self.get_proxid(alpha, beta), 0)
        self.assertReacted(send(alpha, chan, 'gs;p %s auto on' % proxid))
        assertFlags(proxid, 1)
        assertFlags(self.get_proxid(alpha, gamma), 0)
        self.assertReacted(send(alpha, chan, 'gs;p "not global" auto on'))
        assertFlags(self.get_proxid(alpha, role), 1)
        assertFlags(proxid, 0)
        self.assertReacted(send(alpha, chan, 'gs;p test-beta auto on'))
        assertFlags(self.get_proxid(alpha, beta), 1)
        assertFlags(self.get_proxid(alpha, role), 0)
        send(alpha, chan, 'gs;swap close test-beta')
        send(alpha, chan, 'gs;swap close test-gamma')

        # invalid tags. these should fail
        self.assertNotReacted(send(alpha, chan, 'gs;p %s tags ' % proxid))
        self.assertNotReacted(send(alpha, chan, 'gs;p %s tags text ' % proxid))
        self.assertNotReacted(send(alpha, chan, 'gs;p %s tags txet ' % proxid))

        # test autoproxy without tags
        # also test proxies added on role add
        newrole = g._add_role('no tags')
        self.assertReacted(send(alpha, chan, 'gs;c new %s' % newrole.mention))
        run(g.get_member(alpha.id)._add_role(newrole))
        proxid = self.get_proxid(alpha, newrole)
        self.assertIsNotNone(proxid)
        self.assertReacted(send(alpha, chan, 'gs;p %s auto' % proxid))
        self.assertIsNotNone(send(alpha, chan, 'no tags, auto').webhook_id)
        self.assertReacted(send(alpha, chan, 'gs;p %s auto off' % proxid))
        self.assertIsNone(send(alpha, chan, 'no tags, no auto').webhook_id)

        # test tag precedence over auto
        self.assertReacted(send(alpha, chan, 'gs;p %s auto on' % proxid))
        self.assertEqual(send(alpha, chan, 'auto').author.name, 'no tags')
        self.assertEqual(send(alpha, chan, 'e: tags').author.name, 'test')

    def test_06_query_delete(self):
        run(g._add_member(deleteme := User(name = 'deleteme')))
        chan = g['main']
        self.assertReacted(send(deleteme, chan, 'gs;swap open %s'
            % deleteme.mention))
        self.assertReacted(send(deleteme, chan, 'gs;p deleteme tags e:text'))
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
        self.assertReacted(send(alpha, chan,
            'gs;swap open %s swap:text' % beta.mention))
        self.assertReacted(send(beta, chan, 'gs;swap open %s' % alpha.mention))
        msg = send(alpha, chan, 'swap:delete me')
        self.assertIsNotNone(msg.webhook_id)
        run(msg._react(gestalt.REACT_DELETE, gamma))
        self.assertFalse(msg._deleted)
        run(msg._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(msg._deleted)
        msg = send(alpha, chan, 'swap:delete me')
        self.assertIsNotNone(msg.webhook_id)
        run(msg._react(gestalt.REACT_DELETE, beta))
        self.assertTrue(msg._deleted)
        self.assertReacted(send(alpha, chan,
            'gs;swap close %s' % self.get_proxid(alpha, beta)))

        # test DMs
        msg1 = beta.dm_channel[-1]
        msg2 = send(beta, beta.dm_channel, 'test')
        run(msg1._react(gestalt.REACT_DELETE, beta))
        self.assertTrue(msg1._deleted)
        run(msg2._react(gestalt.REACT_DELETE, beta))
        self.assertFalse(msg2._deleted)

        # and finally normal messages
        msg = send(beta, chan, "we're just normal messages")
        buf = send(beta, beta.dm_channel, "we're just innocent messages")
        self.assertIsNone(msg.webhook_id)
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
        newhook = send(alpha, g['main'], 'e:asdhgdfjg').webhook_id
        self.assertIsNotNone(newhook)
        self.assertRowNotExists(
                'select 1 from webhooks where hookid = ?',
                (hookid,))
        self.assertRowExists(
                'select 1 from webhooks where hookid = ?',
                (newhook,))

    # this function requires the existence of at least three ongoing wars
    def test_08_global_conflicts(self):
        g2 = Guild()
        g2._add_channel('main')
        run(g2._add_member(bot))
        run(g2._add_member(alpha))

        rolefirst = g._add_role('conflict')
        rolesecond = g2._add_role('conflict')
        run(g.get_member(alpha.id)._add_role(rolefirst))
        run(g.get_member(alpha.id)._add_role(rolesecond))

        # open a swap. swaps are global so alpha will use it to test conflicts
        self.assertReacted(
                send(alpha, g['main'], 'gs;swap open %s :text' % beta.mention))
        self.assertReacted(
                send(beta, g['main'], 'gs;swap open %s :text' % alpha.mention))
        proxswap = self.get_proxid(alpha, beta)
        self.assertIsNotNone(proxswap)

        # swap should be usable in g but not g2 because beta isn't in g2
        self.assertIsNotNone(send(alpha, g['main'], ':test').webhook_id)
        self.assertIsNone(send(alpha, g2['main'], ':test').webhook_id)

        # create collectives on the two roles
        self.assertReacted(
                send(alpha, g['main'], 'gs;c new %s' % rolefirst.mention))
        self.assertReacted(
                send(alpha, g2['main'], 'gs;c new %s' % rolesecond.mention))
        proxfirst = self.get_proxid(alpha, rolefirst)
        proxsecond = self.get_proxid(alpha, rolesecond)
        self.assertIsNotNone(proxfirst)
        self.assertIsNotNone(proxsecond)

        # now alpha can test tags and auto stuff
        self.assertReacted(
                send(alpha, g['main'], 'gs;p %s tags same:text' % proxfirst))
        # this should work because the collectives are in different guilds
        self.assertReacted(
                send(alpha, g2['main'], 'gs;p %s tags same:text' % proxsecond))
        self.assertIsNotNone(send(alpha, g['main'], 'same: no auto').webhook_id)
        # alpha should be able to set both to auto; different guilds
        self.assertReacted(
                send(alpha, g['main'], 'gs;p %s auto on' % proxfirst))
        self.assertReacted(
                send(alpha, g2['main'], 'gs;p %s auto on' % proxsecond))
        self.assertIsNotNone(send(alpha, g['main'], 'auto on').webhook_id)
        self.assertIsNotNone(send(alpha, g2['main'], 'auto on').webhook_id)

        # test global tags conflict; this should fail
        self.assertNotReacted(
                send(alpha, g['main'], 'gs;p %s tags same:text' % proxswap))
        # no conflict; this should work
        self.assertReacted(
                send(alpha, g['main'], 'gs;p %s tags swap:text' % proxswap))
        # make a conflict with a collective
        self.assertNotReacted(
                send(alpha, g['main'], 'gs;p %s tags swap:text' % proxfirst))
        # now turning on auto on the swap should deactivate the other autos
        self.assertIsNotNone(send(alpha, g['main'], 'auto on').webhook_id)
        self.assertNotEqual(
                send(alpha, g['main'], 'collective has auto').author.name.index(
                    rolefirst.name), -1)
        self.assertReacted(send(alpha, g['main'], 'gs;p %s auto on' % proxswap))
        self.assertNotEqual(
                send(alpha, g['main'], 'swap has auto').author.name.index(
                    beta.name), -1)

        # test combined prefix/postfix conflicts
        # this should be exhaustive

        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags 123text456' % proxswap))
        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags 1234text56' % proxfirst))
        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags 1234text' % proxfirst))
        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags 12text3456' % proxfirst))
        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags text3456' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags 12text56' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags 1234text3456' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags 123text456' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags 123text' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags text456' % proxfirst))

        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags 123text' % proxswap))
        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags text456' % proxfirst))
        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags 12text456' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags 12text' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags 1234text' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags 1234text456' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags 123text' % proxfirst))

        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags [text]' % proxfirst))

        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags text456' % proxswap))
        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags 123text' % proxfirst))
        self.assertReacted(send(alpha, g['main'],
            'gs;p %s tags 123text56' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags text56' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags text3456' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags 123text3456' % proxfirst))
        self.assertNotReacted(send(alpha, g['main'],
            'gs;p %s tags text456' % proxfirst))

        # done. close the swap
        self.assertReacted(send(alpha, g['main'],
            'gs;swap close %s' % proxswap))

    def test_09_override(self):
        overid = self.get_proxid(alpha, None)
        # alpha has sent messages visible to bot by now, so should have one
        self.assertIsNotNone(overid)
        proxid = self.get_proxid(alpha, g.default_role)
        self.assertIsNotNone(proxid)

        chan = g['main']
        self.assertIsNotNone(send(alpha, chan, 'e: proxy').webhook_id)
        self.assertReacted(send(alpha, chan, 'gs;p %s auto on' % proxid))
        self.assertIsNotNone(send(alpha, chan, 'proxy').webhook_id)
        # set the override tags. this should activate it
        self.assertReacted(send(alpha, chan, 'gs;p %s tags x:text' % overid))
        self.assertIsNone(send(alpha, chan, 'x: not proxies').webhook_id)

        # turn autoproxy off
        self.assertReacted(send(alpha, chan, 'gs;p %s auto' % proxid))
        self.assertIsNone(send(alpha, chan, 'not proxied').webhook_id)
        self.assertIsNone(send(alpha, chan, 'x: not proxied').webhook_id)

    # by far the most ominous test
    def test_10_replacements(self):
        chan = g['main']
        before = 'I am myself. i was and am. I\'m. im. am I? I me my mine.'
        after = (
                'We are Ourselves. We were and are. We\'re. We\'re. are We? '
                'We Us Our Ours.')
        self.assertReacted(send(alpha, chan, 'gs;prefs replace off'))
        msg = send(alpha, chan, 'e:' + before)
        self.assertEqual(msg.content, before)
        self.assertReacted(send(alpha, chan, 'gs;prefs replace'))
        msg = send(alpha, chan, 'e:' + before)
        self.assertEqual(msg.content, after)
        self.assertReacted(send(alpha, chan, 'gs;prefs replace off'))
        self.assertReacted(send(alpha, chan, 'gs;prefs defaults'))
        self.assertEqual(msg.content, after)

    def test_11_avatar_url(self):
        chan = g['main']
        collid = self.get_collid(g.default_role)
        self.assertReacted(send(alpha, chan,
            'gs;c %s avatar http://a' % collid))
        self.assertReacted(send(alpha, chan,
            'gs;c %s avatar https://a' % collid))
        self.assertNotReacted(send(alpha, chan,
            'gs;c %s avatar http:/a' % collid))
        self.assertNotReacted(send(alpha, chan,
            'gs;c %s avatar https:/a' % collid))
        self.assertNotReacted(send(alpha, chan,
            'gs;c %s avatar _https://a' % collid))
        self.assertNotReacted(send(alpha, chan,
            'gs;c %s avatar foobar' % collid))
        self.assertReacted(send(alpha, chan, 'gs;c %s avatar' % collid))
        self.assertReacted(send(alpha, chan, 'gs;c %s avatar ""' % collid))

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
        self.assertReacted(send(alpha, chan, 'gs;prefs latch'))
        self.assertIsNone(send(alpha, chan, 'no proxy, no auto').webhook_id)
        self.assertIsNotNone(send(alpha, chan, 'e: proxy, no auto').webhook_id)
        self.assertIsNotNone(send(alpha, chan, 'no proxy, auto').webhook_id)
        self.assertIsNotNone(send(alpha, chan, 'e: proxy, auto').webhook_id)
        self.assertIsNone(send(alpha, chan, 'x: override').webhook_id)
        self.assertIsNone(send(alpha, chan, 'no proxy, no auto').webhook_id)

        # test \escape and \\unlatch
        proxid = self.get_proxid(alpha, g.default_role)
        self.assertIsNotNone(send(alpha, chan, 'e: proxy, no auto').webhook_id)
        self.assertIsNotNone(send(alpha, chan, 'no proxy, auto').webhook_id)
        self.assertIsNone(send(alpha, chan, '\escape').webhook_id)
        self.assertIsNotNone(send(alpha, chan, 'no proxy, auto').webhook_id)
        self.assertIsNone(send(alpha, chan, '\\\\unlatch').webhook_id)
        self.assertIsNone(send(alpha, chan, 'no proxy, no auto').webhook_id)
        self.assertReacted(send(alpha, chan, 'gs;prefs latch off'))

        self.assertReacted(send(alpha, chan, 'gs;p %s auto on' % proxid))
        self.assertIsNotNone(send(alpha, chan, 'no proxy, auto').webhook_id)
        self.assertIsNone(send(alpha, chan, '\escape').webhook_id)
        self.assertIsNotNone(send(alpha, chan, 'no proxy, auto').webhook_id)
        self.assertIsNone(send(alpha, chan, '\\\\unlatch').webhook_id)
        self.assertIsNotNone(send(alpha, chan, 'no proxy, auto').webhook_id)
        self.assertReacted(send(alpha, chan, 'gs;p %s auto off' % proxid))

    # test member joining when the guild has an @everyone collective
    def test_14_member_join(self):
        user = User(name = 'test-joining')
        run(g._add_member(user))
        self.assertIsNotNone(self.get_proxid(user, g.default_role))

    def test_15_case(self):
        proxid = self.get_proxid(alpha, g.default_role).upper()
        self.assertIsNotNone(proxid)
        collid = self.get_collid(g.default_role).upper()
        self.assertReacted(send(alpha, g['main'], 'gs;p %s auto off' % proxid))
        self.assertReacted(send(alpha, g['main'], 'gs;c %s name test' % collid))

    def test_16_replies(self):
        chan = g['main']
        msg = send(alpha, chan, 'e: no reply')
        self.assertIsNotNone(msg.webhook_id)
        self.assertEqual(len(msg.embeds), 0)
        reply = send(alpha, chan, 'e: reply', Object(cached_message = msg))
        self.assertIsNotNone(reply.webhook_id)
        self.assertEqual(len(reply.embeds), 1)
        self.assertEqual(reply.embeds[0].description,
                '**[Reply to:](%s)** no reply' % msg.jump_url)
        # again, but this time the message isn't in cache
        reply = send(alpha, chan, 'e: reply', Object(cached_message = None,
            message_id = msg.id))
        self.assertIsNotNone(reply.webhook_id)
        self.assertEqual(len(reply.embeds), 1)
        self.assertEqual(reply.embeds[0].description,
                '**[Reply to:](%s)** no reply' % msg.jump_url)

    def test_17_edit(self):
        def assert_edited_content(message, content):
            self.assertEqual(
                    run(message.channel.fetch_message(message.id)).content,
                    content)

        chan = g._add_channel('edit')
        first = send(alpha, chan, 'e: fisrt')
        self.assertIsNotNone(first.webhook_id)
        self.assertNotReacted(send(alpha, chan, 'gs;e'))
        second = send(alpha, chan, 'e: secnod')
        self.assertIsNotNone(second.webhook_id)

        msg = send(beta, chan, 'gs;edit second')
        self.assertReacted(msg, gestalt.REACT_DELETE)
        assert_edited_content(second, 'secnod')
        msg = send(beta, chan, 'gs;edit first', Object(message_id = first.id))
        self.assertReacted(msg, gestalt.REACT_DELETE)
        assert_edited_content(first, 'fisrt')

        send(alpha, chan, 'gs;edit second')
        assert_edited_content(second, 'second')
        send(alpha, chan, 'gs;edit first', Object(message_id = first.id))
        assert_edited_content(first, 'first')

        self.assertReacted(send(beta, chan,
            'gs;p %s tags e: text' % self.get_proxid(beta, g.default_role)))
        first = send(alpha, chan, 'e: edti me')
        run(send(alpha, chan, 'e: delete me').delete())
        run(send(alpha, chan, 'e: delete me too')._bulk_delete())
        self.assertIsNotNone(send(beta, chan, 'e: dont edit me').webhook_id)
        send(alpha, chan, 'gs;help this message should be ignored')
        send(alpha, chan, 'gs;edit edit me');
        assert_edited_content(first, 'edit me');

        # make sure that gs;edit on a non-webhook message doesn't cause problems
        # delete "or proxied.webhook_id != hook[1]" to see this be a problem
        # this is very important, because if this fails,
        # there could be a path to a per-channel denial of service
        first = send(alpha, chan, 'e: fisrt')
        send(alpha, chan, 'gs;help')
        second = chan[-1]
        self.assertEqual(second.author.id, bot.id)
        third = send(alpha, chan, 'gs;edit lol', Object(message_id = second.id))
        assert_edited_content(third, 'gs;edit lol')
        self.assertReacted(third, gestalt.REACT_DELETE)
        self.assertIsNotNone(send(alpha, chan, 'e: another').webhook_id)
        send(alpha, chan, 'gs;edit first', Object(message_id = first.id))
        assert_edited_content(first, 'first')

    def test_18_become(self):
        chan = g['main']
        # can't Become override
        self.assertNotReacted(send(alpha, chan, 'gs;become %s'
            % self.get_proxid(alpha, None)))
        # can't Become someone else's proxy
        proxid = self.get_proxid(alpha, g.default_role)
        self.assertNotReacted(send(beta, chan, 'gs;become %s' % proxid))

        self.assertReacted(send(alpha, chan, 'gs;become %s' % proxid))
        self.assertIsNone(send(alpha, chan, 'not proxied').webhook_id)
        self.assertIsNotNone(send(alpha, chan, 'proxied').webhook_id)

    def test_19_swap_close(self):
        chan = g['main']
        self.assertReacted(send(alpha, chan, 'gs;swap open %s' % beta.mention))
        self.assertReacted(send(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, beta)))
        self.assertReacted(send(alpha, chan, 'gs;swap open %s' % beta.mention))
        self.assertReacted(send(beta, chan, 'gs;swap open %s' % alpha.mention))
        self.assertNotReacted(send(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, None)))
        self.assertNotReacted(send(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, g.default_role)))
        self.assertNotReacted(send(alpha, chan, 'gs;swap close aaaaaa'))
        self.assertReacted(send(alpha, chan, 'gs;swap close %s'
            % self.get_proxid(alpha, beta)))

    def test_20_collective_delete(self):
        g1 = Guild()
        c1 = g1._add_channel('main')
        run(g1._add_member(bot))
        run(g1._add_member(alpha, perms = discord.Permissions(
            manage_roles = True)))
        run(g1._add_member(beta, perms = discord.Permissions(
            manage_roles = False)))
        g2 = Guild()
        c2 = g2._add_channel('main')
        run(g2._add_member(bot))
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
        run(g1._add_member(bot))
        run(g1._add_member(alpha, perms = discord.Permissions(
            manage_roles = True)))

        send(alpha, c, 'gs;c new everyone')
        send(alpha, c, 'gs;p %s tags [text'
                % self.get_proxid(alpha, g1.default_role))
        # normal message
        self.assertIsNotNone((msg := send(alpha, c, '[test')).webhook_id)
        self.assertEqual(len(msg.files), 0)
        # one file, no content
        self.assertIsNotNone((msg := send(alpha, c, '[',
            files = [File(12)])).webhook_id)
        self.assertEqual(msg.content, '')
        self.assertEqual(len(msg.files), 1)
        # two files, no content
        self.assertIsNotNone((msg := send(alpha, c, '[',
            files = [File(12), File(9)])).webhook_id)
        self.assertEqual(len(msg.files), 2)
        # two files, with content
        self.assertIsNotNone((msg := send(alpha, c, '[files!',
            files = [File(12), File(9)])).webhook_id)
        self.assertEqual(msg.content, 'files!')
        self.assertEqual(len(msg.files), 2)
        # no files or content
        self.assertIsNone(send(alpha, c, '[', files = []).webhook_id)
        # big file, no content
        self.assertIsNone(send(alpha, c, '[',
            files = [File(999999999)]).webhook_id)
        half = gestalt.MAX_FILE_SIZE[0]/2
        # files at the limit, no content
        self.assertIsNotNone(send(alpha, c, '[',
            files = [File(half), File(half)]).webhook_id)
        # files too big, no content
        self.assertIsNone(send(alpha, c, '[',
            files = [File(half), File(half+1)]).webhook_id)
        # files too big, with content
        self.assertIsNotNone((msg := send(alpha, c, '[files!',
            files = [File(half), File(half+1)])).webhook_id)
        self.assertEqual(msg.files, [])

    def test_22_names(self):
        g1 = Guild(name = 'guildy guild')
        c = g1._add_channel('main')
        run(g1._add_member(bot))
        run(g1._add_member(alpha, perms = discord.Permissions(
            manage_roles = True)))
        run(g1._add_member(beta))

        send(alpha, c, 'gs;c new everyone')
        self.assertReacted(send(alpha, c, 'gs;p "guildy guild" tags [text'))
        self.assertEqual(send(alpha, c, '[no proxid!').author.name,
                'guildy guild')
        self.assertReacted(send(alpha, c, 'gs;p "guildy guild" rename "guild"'))
        self.assertReacted(send(alpha, c, 'gs;p guild auto on'))
        self.assertEqual(send(alpha, c, 'yay!').author.name, 'guildy guild')
        self.assertReacted(send(alpha, c, 'gs;become guild'))
        self.assertIsNone(send(alpha, c, 'not proxied').webhook_id)
        self.assertIsNotNone(send(alpha, c, 'proxied').webhook_id)
        self.assertReacted(send(alpha, c, 'gs;p guild auto off'))

        send(alpha, c, 'gs;swap open %s' % beta.mention)
        send(beta, c, 'gs;swap open %s' % alpha.mention)
        self.assertReacted(send(alpha, c, 'gs;p test-beta tags b:text'))
        self.assertNotReacted(send(beta, c, 'gs;swap close test-beta'))
        self.assertReacted(send(alpha, c, 'gs;swap close test-beta'))

        self.assertNotReacted(send(beta, c, 'gs;p guild tags g:text'))
        self.assertNotReacted(send(beta, c, 'gs;p guild auto on'))
        self.assertNotReacted(send(beta, c, 'gs;become guild'))
        self.assertIsNone(send(alpha, c, 'not proxied').webhook_id)

        self.assertReacted(send(alpha, c, 'gs;c "guild" name guild!'))
        self.assertEqual(send(alpha, c, '[proxied').author.name, 'guild!')
        self.assertReacted(send(alpha, c, 'gs;c guild avatar http://newavatar'))
        self.assertEqual(send(alpha, c, '[proxied').author.display_avatar,
                'http://newavatar')
        self.assertNotReacted(send(beta, c, 'gs;c guild name guild'))
        instance.get_user_proxy(send(alpha, c, 'command'), 'guild')
        self.assertReacted(send(alpha, c, 'gs;c guild delete'))
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
        send(alpha, c, 'gs;swap open %s' % beta.mention)
        self.assertIsNotNone(self.get_proxid(beta, alpha))
        self.assertIsNotNone(instance.get_user_proxy(c[-1], 'test-beta'))
        self.assertNotReacted(send(beta, c, 'gs;p test-alpha auto on'))
        self.assertNotReacted(send(beta, c, 'gs;swap close test-alpha'))
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(c[-1], 'test-alpha')
        self.assertReacted(send(alpha, c, 'gs;swap close test-beta'))

    def test_23_pk_swap(self):
        g1 = Guild(name = 'guildy guild')
        c = g1._add_channel('main')
        run(g1._add_member(bot))
        run(g1._add_member(alpha))
        run(g1._add_member(beta))
        run(g1._add_member(gamma))
        pkhook = Webhook(c, 'pk webhook')

        instance.session._add('/systems/' + str(alpha.id), '{"id": "exmpl"}')
        instance.session._add('/members/aaaaa',
                '{"system": "exmpl", "uuid": "a-a-a-a-a", "name": "member!"}')

        self.assertReacted(send(alpha, c, 'gs;swap open %s' % beta.mention))
        # swap needs to be active
        self.assertNotReacted(send(alpha, c, 'gs;pk swap test-beta aaaaa'))
        self.assertReacted(send(beta, c, 'gs;swap open %s' % alpha.mention))
        self.assertReacted(send(alpha, c, 'gs;pk swap test-beta aaaaa'))
        # shouldn't work twice
        self.assertNotReacted(send(alpha, c, 'gs;pk swap test-beta aaaaa'))
        # should be able to send to two users
        self.assertReacted(send(alpha, c, 'gs;swap open %s' % gamma.mention))
        self.assertReacted(send(gamma, c, 'gs;swap open %s' % alpha.mention))
        self.assertReacted(send(alpha, c, 'gs;pk swap test-gamma aaaaa'))
        self.assertNotReacted(send(alpha, c, 'gs;pk swap test-gamma aaaaa'))
        self.assertIsNotNone(instance.get_user_proxy(send(gamma, c, 'a'),
            'member!'))
        # should be deleted upon swap close
        self.assertReacted(send(alpha, c, 'gs;swap close test-gamma'))
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(send(gamma, c, 'a'), 'member!')
        # handle PluralKit linked accounts
        instance.session._add('/systems/' + str(gamma.id), '{"id": "exmpl"}')
        self.assertReacted(send(beta, c, 'gs;swap open %s' % gamma.mention))
        self.assertReacted(send(gamma, c, 'gs;swap open %s' % beta.mention))
        self.assertNotReacted(send(gamma, c, 'gs;pk swap test-beta aaaaa'))

        # test using it!
        self.assertReacted(send(beta, c, 'gs;p member! tags [text]'))
        self.assertIsNone(send(beta, c, '[test]').webhook_id)
        old = run(pkhook.send('member!', '', content = 'old message'))
        new = run(pkhook.send('member!', '', content = 'new message'))
        nope = run(pkhook.send('someone else', '', content = 'irrelevant'))
        instance.session._add('/messages/' + str(old.id),
                '{"member": {"uuid": "a-a-a-a-a"}}')
        instance.session._add('/messages/' + str(new.id),
                '{"member": {"uuid": "a-a-a-a-a"}}')
        instance.session._add('/messages/' + str(nope.id),
                '{"member": {"uuid": "z-z-z-z-z"}}')
        self.assertNotReacted(send(beta, c, 'gs;pk sync'))
        self.assertNotReacted(send(beta, c, 'gs;pk sync',
            Object(cached_message = None, message_id = nope.id)))
        self.assertReacted(send(beta, c, 'gs;pk sync',
            Object(cached_message = None, message_id = new.id)))
        self.assertNotReacted(send(beta, c, 'gs;pk sync',
            Object(cached_message = None, message_id = old.id)))
        self.assertIsNotNone(send(beta, c, '[test]').webhook_id)
        self.assertEqual(c[-1].author.name, 'member!')
        # test a message with no pk entry
        instance.session._add('/messages/' + str(c[-1].id), 404)
        self.assertNotReacted(send(beta, c, 'gs;pk sync',
            Object(cached_message = c[-1])))

        # make sure other "mask" commands don't work
        self.assertNotReacted(send(beta, c, 'gs;c pk-a-a-a-a-a name nope'))
        self.assertNotReacted(send(beta, c,
            'gs;c pk-a-a-a-a-a avatar http://nope.png'))
        self.assertNotReacted(send(beta, c, 'gs;c pk-a-a-a-a-a delete'))

        # test closing specific pkswap
        # first by receipt
        instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        self.assertReacted(send(alpha, c, 'gs;pk close "test-beta\'s member!"'))
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta\'s member!')
        instance.get_user_proxy(send(beta, c, 'a'), 'test-alpha')
        instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta')

        # then by pkswap
        self.assertReacted(send(alpha, c, 'gs;pk swap test-beta aaaaa'))
        instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        self.assertReacted(send(beta, c, 'gs;pk close member!'))
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(send(beta, c, 'a'), 'member!')
        with self.assertRaises(RuntimeError):
            instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta\'s member!')
        instance.get_user_proxy(send(beta, c, 'a'), 'test-alpha')
        instance.get_user_proxy(send(alpha, c, 'a'), 'test-beta')

        self.assertNotReacted(send(beta, c, 'gs;pk close test-alpha'))


def main():
    global bot, alpha, beta, gamma, g, instance

    instance = TestBot()

    bot = User(name = 'Gestalt', bot = True)
    alpha = User(name = 'test-alpha')
    beta = User(name = 'test-beta')
    gamma = User(name = 'test-gamma')
    g = Guild()
    g._add_channel('main')
    run(g._add_member(bot))
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
# don't spam the channel with error messages
gestalt.DEFAULT_PREFS &= ~gestalt.Prefs.errors
gestalt.DEFAULT_PREFS |= gestalt.Prefs.replace

gestalt.BECOME_MAX = 1


if __name__ == '__main__':
    main()

