#!/usr/bin/python3.7

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
        self.bot = False
        self.dm_channel = None
        self.discriminator = "0001"
        super().__init__(**kwargs)
        # self.mention = "<@!%d>" % self.id
        User.users[self.id] = self
    def __str__(self):
        return self.name + "#" + self.discriminator
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
    # TODO: update this if before starts being used
    async def _add_role(self, role):
        self.roles.append(role)
        role.members.append(self)
        await inst.on_member_update(None, self)
    async def _del_role(self, role):
        self.roles.remove(role)
        role.remove(self)
        await inst.on_member_update(None, self)
    @property
    def id(self): return self.user.id
    @property
    def bot(self): return self.user.bot
    @property
    def display_name(self): return self.user.name
    def avatar_url_as(self, **kwargs):
        return "http://avatar.png" # who gives a damn


class Message(Object):
    def __init__(self, **kwargs):
        self._deleted = False
        self.mentions = []
        self.webhook_id = None
        self.attachments = []
        self.reactions = []
        self.type = discord.MessageType.default
        super().__init__(**kwargs)

        if self.content != None:
            # mentions can also be in the embed but that's irrelevant here
            for mention in re.findall("(?<=\<\@\!)[0-9]+(?=\>)", self.content):
                self.mentions.append(User.users[int(mention)])
        self.role_mentions = [] # TODO
    async def delete(self):
        self.channel._messages.remove(self)
        self._deleted = True
    async def _react(self, emoji, user):
        react = discord.Reaction(message = self, emoji = emoji,
                data = {"count": 1, "me": None})
        if react not in self.reactions:
            # FIXME when more than one user adds the same reaction
            self.reactions.append(react)
        await instance.on_raw_reaction_add(
                discord.raw_models.RawReactionActionEvent(data = {
                    "message_id": self.id,
                    "user_id": user.id,
                    "channel_id": self.channel.id},
                    emoji = discord.PartialEmoji(name = emoji),
                    event_type = None))
    async def add_reaction(self, emoji):
        await self._react(emoji, bot)
    async def remove_reaction(self, emoji, member):
        del self.reactions[[x.emoji for x in self.reactions].index(emoji)]

class Webhook(Object):
    hooks = {}
    def __init__(self, channel):
        super().__init__()
        self._channel = channel
        self.token = "t0k3n" + str(self.id)
        Webhook.hooks[self.id] = self
    def partial(id, token, adapter):
        return Webhook.hooks[id]
    async def send(self, **kwargs):
        # keep author None because it's weird with webhook messages
        msg = Message(**kwargs) # note: absorbs other irrelevant arguments
        msg.webhook_id = self.id
        msg.author = bot # so on_message doesn't complain about no author
        await self._channel._add(msg)
        return msg

class Channel(Object):
    channels = {}
    def __init__(self, **kwargs):
        self._messages = []
        self.name = ""
        self.guild = None
        self.members = []
        self.type = discord.ChannelType.text
        super().__init__(**kwargs)
        if self.guild:
            self.members = self.guild.members
        Channel.channels[self.id] = self
    def __getitem__(self, key):
        return self._messages[key]
    async def _add(self, msg):
        msg.channel = self
        if self.guild:
            msg.guild = self.guild
        self._messages.append(msg)
        await instance.on_message(msg)
    async def create_webhook(self, name):
        return Webhook(self)
    async def fetch_message(self, id):
        return self._messages[[x.id for x in self._messages].index(id)]
    async def send(self, content = None, embed = None, file = None):
        msg = Message(author = bot, content = content, embed = embed)
        await self._add(msg)
        return msg

class Role(Object):
    def __init__(self, **kwargs):
        self.members = []
        self.guild = None
        super().__init__(**kwargs)
        self.mention = "<@&%i>" % self.id

class RoleEveryone:
    def __init__(self, guild):
        self.guild = guild
        self.id = guild.id
        self.name = guild.name
    # note that this doesn't update when guild._members updates
    @property
    def members(self): return self.guild._members.values()

class Guild(Object):
    def __init__(self, **kwargs):
        self._channels = {}     # channel id -> channel
        self._roles = {}        # role id -> role
        self._members = {}      # user id -> member
        self.name = ""
        super().__init__(**kwargs)
        self._roles[self.id] = self.default_role = RoleEveryone(self)
    def __getitem__(self, key):
        return discord.utils.get(self._channels.values(), name = key)
    @property
    def members(self):
        return self._members.values()
    def _add_channel(self, name):
        chan = Channel(name = name, guild = self)
        self._channels[chan.id] = chan
        return chan
    def _add_member(self, user, perms = discord.Permissions.all()):
        self._members[user.id] = Member(user, self, perms)
        return self._members[user.id]
    def get_member(self, user_id):
        return self._members[user_id]
    def get_role(self, role_id):
        return self._roles[role_id]

class TestBot(gestalt.Gestalt):
    def __init__(self):
        super().__init__(dbfile = ":memory:", purge = False)
        self.adapter = None
        self.invite = True
    def __del__(self):
        pass # suppress "closing database" message
    @property
    def user(self):
        return bot
    def get_user(self, id):
        return User.users[id]
    def get_channel(self, id):
        return Channel.channels[id]

def send(user, channel, contents):
    auth = channel.guild.get_member(user.id) if channel.guild else user
    for x in contents:
        run(channel._add(Message(author = auth, content = x)))
    return channel._messages[-len(contents):]

class GestaltTest(unittest.TestCase):

    # ugly hack because parsing gs;p output would be uglier
    def get_proxid(self, user, role):
        row = instance.cur.execute(
                "select proxid from proxies where (userid, extraid) = (?, ?)",
                (user.id, role.id)).fetchone()
        return row[0] if row else None

    def assertRowExists(self, query, args = None):
        self.assertIsNotNone(instance.cur.execute(query, args).fetchone())

    def assertReacted(self, msg, reaction = gestalt.REACT_CONFIRM):
        self.assertEqual(msg.reactions[0].emoji, reaction)

    # the swap system has an edge case that depends on one user having no entry
    # in the users database. so it comes first
    def test_aa_swaps(self):
        # monkey patch. this probably violates the Geneva Conventions
        discord.Webhook.partial = Webhook.partial

        chan = g["main"]

        msgs = send(alpha, chan, [
            "gs;swap open <@!%d> \"sw \"" % beta.id,
            "sw no swap"])

        self.assertReacted(msgs[0])
        self.assertIsNone(msgs[1].webhook_id)

        msgs = send(beta, chan, [
            "sw no swap",
            "gs;swap open <@!%d> \"sw \"" % alpha.id,
            "sw swap",
            "gs;swap close \"sw \"",
            "sw no swap",])

        for i in [1, 3]:
            self.assertReacted(msgs[i])
        self.assertIsNotNone(msgs[2].webhook_id)
        for i in [0, 4]:
            self.assertIsNone(msgs[i].webhook_id)

        msgs = send(alpha, chan, [
            "sw no swap"])

        self.assertIsNone(msgs[0].webhook_id)

    def test_help(self):
        msg = send(alpha, g["main"], ["gs;help"])[0]
        self.assertIsNotNone(msg.embed)
        self.assertReacted(msg, gestalt.REACT_DELETE)
        run(msg._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(msg._deleted)

    def test_add_collective(self):
        msg = send(alpha, g["main"], ["gs;c new everyone"])[0]
        self.assertReacted(msg)
        self.assertRowExists("select * from collectives where roleid = ?",
                (g.id,))
        for x in [alpha, beta]:
            proxid = self.get_proxid(x, g.default_role)
            self.assertIsNotNone(proxid)
            msg = send(x, g["main"], ["gs;p %s prefix e:" % proxid])[0]
            self.assertReacted(msg)

    def test_prefix_auto(self):
        # test every combo of auto, prefix, and also the switches thereof
        proxid = self.get_proxid(alpha, g.default_role)
        msgs = send(alpha, g["main"], [
            "no prefix, no auto",
            "e:prefix",
            "gs;p %s prefix =" % proxid,
            "=prefix, no auto",
            "gs;p %s auto on" % proxid,

            "=prefix, auto",
            "no prefix, auto",
            "gs;p %s auto" % proxid,
            "gs;p %s prefix =text" % proxid,
            "=pk-style prefix",

            "gs;p %s prefix e:" % proxid])
        for i in [2, 4, 7, 8, 10]:
            self.assertReacted(msgs[i])
        for i in [0, 5]:
            self.assertEqual(len(msgs[i].reactions), 0)
            self.assertEqual(msgs[i].author.id, alpha.id) # message not proxied
        for i in [1, 3, 6, 9]:
            self.assertIsNotNone(msgs[i].webhook_id) # message proxied

    def test_query_delete(self):
        msg = send(alpha, g["main"], ["e:reaction test"])[0]
        run(msg._react(gestalt.REACT_QUERY, beta))
        self.assertNotEqual(
                beta.dm_channel._messages[-1].content.find(alpha.name), -1)

        run(msg._react(gestalt.REACT_DELETE, beta))
        self.assertEqual(len(msg.reactions), 0)
        self.assertFalse(msg._deleted)

        run(msg._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(msg._deleted)


def main():
    global bot, alpha, beta, g, instance
    bot = User(name = "Gestalt", bot = True)
    alpha = User(name = "test-alpha")
    beta = User(name = "test-beta")
    g = Guild()
    g._add_channel("main")
    g._add_member(bot)
    g._add_member(alpha)
    g._add_member(beta)

    instance = TestBot()

    if unittest.main(exit = False).result.wasSuccessful():
        print("But it isn't *really* OK, is it?")

if __name__ == "__main__":
    main()

