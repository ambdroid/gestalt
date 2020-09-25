#!/usr/bin/python3.7

from asyncio import run
import unittest

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
        self.attachments = []
        self.reactions = []
        self.type = discord.MessageType.default
        super().__init__(**kwargs)
    async def delete(self):
        self.channel._messages.remove(self)
        self._deleted = True
    async def _react(self, emoji, user):
        # no more than one test user should be using the same reaction at once
        react = discord.Reaction(message = self, emoji = emoji,
                data = {"count": 1, "me": None})
        if react in self.reactions:
            raise RuntimeError("Adding a reaction more than once")
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
        self.reactions.remove(
                self.reactions[[x.emoji for x in self.reactions].index(emoji)])


class Channel(Object):
    def __init__(self, **kwargs):
        self._messages = []
        self.members = []
        self.type = discord.ChannelType.text
        super().__init__(**kwargs)
    async def _add(self, msg):
        msg.channel = self
        self._messages.append(msg)
        await instance.on_message(msg)
    async def create_webhook(self, name):
        pass
    async def fetch_message(self, id):
        return self._messages[[x.id for x in self._messages].index(id)]
    async def send(self, content = None, embed = None, file = None):
        msg = Message(author = user[0], content = content, embed = embed)
        await self._add(msg)
        return msg

class TestBot(gestalt.Gestalt):
    def get_user(self, id):
        return user[[x.id for x in user].index(id)]
    def get_channel(self, id):
        return chan[[x.id for x in chan].index(id)]

def send(user, channel, contents):
    for x in contents:
        run(channel._add(Message(author = user, content = x)))
    return channel._messages[-len(contents):]

class GestaltTest(unittest.TestCase):
    def test_help(self):
        send(user[1], chan[0], ["gs;help"])
        self.assertIsNotNone(chan[0]._messages[-1].embed)

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
            "gs;prefix delete",
            "default"])
        for i in [2, 4, 7, 8]:
            self.assertEqual(len(msgs[i].reactions), 1)
            self.assertEqual(msgs[i].reactions[0].emoji, gestalt.REACT_CONFIRM)
        for i in [0, 5, 9]:
            self.assertEqual(msgs[i].author, user[1]) # message not proxied
        for i in [1, 3, 6]:
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
    unittest.main()

'''
class GestaltTest(unittest.TestCase):

    # the swap system has an edge case that depends on one user having no entry
    # in the users database. so it comes first
    def test_aa_swaps(self):
        response = test(0,(
            ("message", "gs;swap <@!%d>" % testenv.BOTS[1], "raw_reaction_add"),
            ("message", "g no swap",                        "message")))
        
        self.assertEqual(response[0].emoji.name, gestalt.REACT_CONFIRM)
        self.assertIsNone(response[1].webhook_id)

        response = test(1,(
            ("message", "gs;swap <@!%d>" % testenv.BOTS[0], "raw_reaction_add"),
            ("message", "g swap",                           "message"),
            ("message", "gs;swap off",                      "raw_reaction_add"),
            ("message", "g no swap",                        "message"),
            ("message", "gs;prefs autoswap on",             "raw_reaction_add"),
            ))
        
        for i in [0, 2, 4]:
            self.assertEqual(response[i].emoji.name, gestalt.REACT_CONFIRM)
        self.assertIsNotNone(response[1].webhook_id)
        self.assertIsNone(response[3].webhook_id)

        response = test(0,(
            ("message", "g no swap",                        "message"),
            ("message", "gs;swap <@!%d>" % testenv.BOTS[1], "raw_reaction_add"),
            ("message", "g swap",                           "message")))

        self.assertIsNone(response[0].webhook_id)
        self.assertEqual(response[1].emoji.name, gestalt.REACT_CONFIRM)
        self.assertIsNotNone(response[2].webhook_id)

        response = test(1,(
            ("message", "g swap",                           "message"),
            ("message", "gs;swap off",                      "raw_reaction_add")
            ))

        self.assertIsNotNone(response[0].webhook_id)
        self.assertEqual(response[1].emoji.name, gestalt.REACT_CONFIRM)

    def test_prefix_auto(self): 
        # test every combo of auto, prefix, and also the switches thereof
        response = test(0,(
                ("message", "no prefix, no auto",   "message"),
                ("message", "g default prefix",     "message"),
                ("message", "gs;prefix =",          "raw_reaction_add"),
                ("message", "=prefix, no auto",     "message"),
                ("message", "gs;auto on",           "raw_reaction_add"),

                ("message", "=prefix, auto",        "message"),
                ("message", "no prefix, auto",      "message"),
                ("message", "gs;auto",              "raw_reaction_add"),
                ("message", "gs;prefix delete",     "raw_reaction_add"),
                ("message", "defaults",             "message")))

        for i in [2, 4, 7, 8]:
            self.assertEqual(response[i].emoji.name, gestalt.REACT_CONFIRM)
        for i in [0, 5, 9]:
            self.assertIsNone(response[i]) # message not proxied
        for i in [1, 3, 6]:
            self.assertIsNotNone(response[i]) # message proxied

    def test_query_delete(self):
        response = test(0,(
            ("message", "g reaction test",       "message"),
            ("react", (1, gestalt.REACT_QUERY),  "message"))) 
        self.assertNotEqual(response[1].content.find(str(testenv.BOTS[0])), -1)
        
        response = test(1,(
            ("react", (2, gestalt.REACT_DELETE), "raw_reaction_remove"),))
        # reaction removed without message delete
        self.assertIsNotNone(response[0])

        response = test(0,(
            ("react", (2, gestalt.REACT_DELETE), "raw_message_delete"),))
        self.assertIsNotNone(response[0])
'''
