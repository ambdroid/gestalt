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
    def __init__(self, **kwargs):
        self.bot = False
        self.dm_channel = None
        self.discriminator = "0001"
        super().__init__(**kwargs)
        self.display_name = self.nick = self.name
        # self.mention = "<@!%d>" % self.id
    def __str__(self):
        return self.name + "#" + self.discriminator
    def avatar_url_as(self, **kwargs):
        return "http://avatar.png" # who gives a damn
    async def send(self, content = None, embed = None, file = None):
        if self.dm_channel == None:
            self.dm_channel = Channel(type = discord.ChannelType.private,
                    members = [self, user[0]])
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
                self.mentions.append(
                        user[[x.id for x in user].index(int(mention))])
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
        await self._react(emoji, user[0])
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
        msg.author = user[0] # so on_message doesn't complain about no author
        await self._channel._add(msg)
        return msg

class Channel(Object):
    def __init__(self, **kwargs):
        self._messages = []
        self.members = []
        self.type = discord.ChannelType.text
        super().__init__(**kwargs)
        self.guild = Guild() # FIXME when guilds need fleshing out
    async def _add(self, msg):
        msg.channel = self
        self._messages.append(msg)
        await instance.on_message(msg)
    async def create_webhook(self, name):
        return Webhook(self)
    async def fetch_message(self, id):
        return self._messages[[x.id for x in self._messages].index(id)]
    async def send(self, content = None, embed = None, file = None):
        msg = Message(author = user[0], content = content, embed = embed)
        await self._add(msg)
        return msg

class Guild:
    def get_member(self, user_id):
        return user[[x.id for x in user].index(user_id)]

class TestBot(gestalt.Gestalt):
    def __del__(self):
        pass # suppress "closing database" message
    @property
    def user(self):
        return user[0]
    def get_user(self, id):
        return user[[x.id for x in user].index(id)]
    def get_channel(self, id):
        return chan[[x.id for x in chan].index(id)]

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

        msgs = send(user[1], chan[0], [
            "gs;swap <@!%d>" % user[2].id,
            "g no swap"])

        self.assertReacted(msgs[0])
        self.assertIsNone(msgs[1].webhook_id)

        msgs = send(user[2], chan[0], [
            "g no swap",
            "gs;swap <@!%d>" % user[1].id,
            "g swap",
            "gs;swap off",
            "g no swap",
            "gs;prefs autoswap on"])

        for i in [1, 3, 5]:
            self.assertReacted(msgs[i])
        self.assertIsNotNone(msgs[2].webhook_id)
        for i in [0, 4]:
            self.assertIsNone(msgs[i].webhook_id)

        msgs = send(user[1], chan[0], [
            "g no swap",
            "gs;swap <@!%d>" % user[2].id,
            "g swap"])

        self.assertIsNone(msgs[0].webhook_id)
        self.assertReacted(msgs[1])
        self.assertIsNotNone(msgs[2].webhook_id)

        msgs = send(user[2], chan[0], [
            "g swap",
            "gs;swap off",
            "g no swap"])

        self.assertIsNotNone(msgs[0].webhook_id)
        self.assertReacted(msgs[1])
        self.assertIsNone(msgs[2].webhook_id)

        msgs = send(user[1], chan[0], ["g no swap"])
        self.assertIsNone(msgs[0].webhook_id)

    def test_help(self):
        msg = send(user[1], chan[0], ["gs;help"])[0]
        self.assertIsNotNone(chan[0]._messages[-1].embed)
        self.assertReacted(msg, gestalt.REACT_DELETE)
        run(msg._react(gestalt.REACT_DELETE, user[1]))
        self.assertTrue(msg._deleted)

    def test_prefix_auto(self):
        # test every combo of auto, prefix, and also the switches thereof
        msgs = send(user[1], chan[0],[
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
            self.assertEqual(msgs[i].author, user[1]) # message not proxied
        for i in [1, 3, 6, 9]:
            self.assertEqual(msgs[i].author, user[0]) # message proxied

    def test_query_delete(self):
        msg = send(user[1], chan[0], ["g reaction test"])[0]
        run(msg._react(gestalt.REACT_QUERY, user[1]))
        self.assertNotEqual(
                user[1].dm_channel._messages[-1].content.find(str(user[0].id)),
                -1)

        run(msg._react(gestalt.REACT_DELETE, user[2]))
        self.assertEqual(len(msg.reactions), 0)
        self.assertFalse(msg._deleted)

        run(msg._react(gestalt.REACT_DELETE, user[1]))
        self.assertTrue(msg._deleted)


if __name__ == "__main__":
    user = [
            Member(name = "Gestalt", bot = True),
            Member(name = "test-1"),
            Member(name = "test-2")
            ]
    chan = [Channel()]

    instance = TestBot(dbfile = ":memory:", purge = False)
    if unittest.main(exit = False).result.wasSuccessful():
        print("But it isn't *really* OK, is it?")
