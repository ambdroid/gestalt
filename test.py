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

# for simplicity, all Users are Members, the same across all guilds
class Member(Object):
    users = {}
    def __init__(self, **kwargs):
        self.bot = False
        self.dm_channel = None
        self.discriminator = "0001"
        super().__init__(**kwargs)
        self.display_name = self.nick = self.name
        # self.mention = "<@!%d>" % self.id
        Member.users[self.id] = self
    def __str__(self):
        return self.name + "#" + self.discriminator
    def avatar_url_as(self, **kwargs):
        return "http://avatar.png" # who gives a damn
    async def send(self, content = None, embed = None, file = None):
        if self.dm_channel == None:
            self.dm_channel = Channel(type = discord.ChannelType.private,
                    members = [self, bot])
        await self.dm_channel.send(
                content = content, embed = embed, file = file)


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
                self.mentions.append(Member.users[int(mention)])
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
        self.members = []
        self.type = discord.ChannelType.text
        super().__init__(**kwargs)
        self.guild = Guild() # FIXME when guilds need fleshing out
        Channel.channels[self.id] = self
    async def _add(self, msg):
        msg.channel = self
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

class Guild:
    def get_member(self, user_id):
        return Member.users[user_id]

class TestBot(gestalt.Gestalt):
    def __init__(self):
        super().__init__(dbfile = ":memory:", purge = False)
        self.invite = True
    def __del__(self):
        pass # suppress "closing database" message
    @property
    def user(self):
        return bot
    def get_user(self, id):
        return Member.users[id]
    def get_channel(self, id):
        return Channel.channels[id]

def send(user, channel, contents):
    for x in contents:
        run(channel._add(Message(author = user, content = x)))
    return channel._messages[-len(contents):]

class GestaltTest(unittest.TestCase):

    def assertReacted(self, msg, reaction = gestalt.REACT_CONFIRM):
        self.assertEqual(msg.reactions[0].emoji, reaction)

    # the swap system has an edge case that depends on one user having no entry
    # in the users database. so it comes first
    def test_aa_swaps(self):
        # monkey patch. this probably violates the Geneva Conventions
        instance.sesh = None
        discord.AsyncWebhookAdapter.__init__ = lambda self, adapter : None
        discord.Webhook.partial = Webhook.partial

        msgs = send(alpha, chan, [
            "gs;swap <@!%d>" % beta.id,
            "g no swap"])

        self.assertReacted(msgs[0])
        self.assertIsNone(msgs[1].webhook_id)

        msgs = send(beta, chan, [
            "g no swap",
            "gs;swap <@!%d>" % alpha.id,
            "g swap",
            "gs;swap off",
            "g no swap",
            "gs;prefs autoswap on"])

        for i in [1, 3, 5]:
            self.assertReacted(msgs[i])
        self.assertIsNotNone(msgs[2].webhook_id)
        for i in [0, 4]:
            self.assertIsNone(msgs[i].webhook_id)

        msgs = send(alpha, chan, [
            "g no swap",
            "gs;swap <@!%d>" % beta.id,
            "g swap"])

        self.assertIsNone(msgs[0].webhook_id)
        self.assertReacted(msgs[1])
        self.assertIsNotNone(msgs[2].webhook_id)

        msgs = send(beta, chan, [
            "g swap",
            "gs;swap off",
            "g no swap"])

        self.assertIsNotNone(msgs[0].webhook_id)
        self.assertReacted(msgs[1])
        self.assertIsNone(msgs[2].webhook_id)

        msgs = send(alpha, chan, ["g no swap"])
        self.assertIsNone(msgs[0].webhook_id)

    def test_help(self):
        msg = send(alpha, chan, ["gs;help"])[0]
        self.assertIsNotNone(chan._messages[-1].embed)
        self.assertReacted(msg, gestalt.REACT_DELETE)
        run(msg._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(msg._deleted)

    def test_prefix_auto(self):
        # test every combo of auto, prefix, and also the switches thereof
        msgs = send(alpha, chan,[
            "no prefix, no auto",
            "g default prefix",
            "gs;prefix =",
            "=prefix, no auto",
            "gs;auto on",

            "=prefix, auto",
            "no prefix, auto",
            "gs;auto",
            "gs;prefix =text",
            "=pk-style prefix",

            "gs;prefix delete",
            "default"])
        for i in [2, 4, 7, 8, 10]:
            self.assertReacted(msgs[i])
        for i in [0, 5, 11]:
            self.assertEqual(len(msgs[i].reactions), 0)
            self.assertEqual(msgs[i].author, alpha) # message not proxied
        for i in [1, 3, 6, 9]:
            self.assertEqual(msgs[i].author, bot) # message proxied

    def test_query_delete(self):
        msg = send(alpha, chan, ["g reaction test"])[0]
        run(msg._react(gestalt.REACT_QUERY, beta))
        self.assertNotEqual(
                beta.dm_channel._messages[-1].content.find(alpha.name), -1)

        run(msg._react(gestalt.REACT_DELETE, beta))
        self.assertEqual(len(msg.reactions), 0)
        self.assertFalse(msg._deleted)

        run(msg._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(msg._deleted)


if __name__ == "__main__":
    bot = Member(name = "Gestalt", bot = True)
    alpha = Member(name = "test-alpha")
    beta = Member(name = "test-beta")
    chan = Channel()

    instance = TestBot()
    if unittest.main(exit = False).result.wasSuccessful():
        print("But it isn't *really* OK, is it?")
