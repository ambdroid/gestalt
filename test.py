#!/usr/bin/python3.7

from asyncio import run
import unittest

import discord

import gestalt



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
        self.discriminator = "0001"
        super().__init__(**kwargs)
        self.display_name = self.nick = self.name
        # self.mention = "<@!%d>" % self.id
    async def send(self, content = None, embed = None, file = None):
        pass

class Message(Object):
    def __init__(self, **kwargs):
        self._deleted = False
        self.mentions = []
        self.attachments = []
        self.reactions = []
        self.type = discord.MessageType.default
        super().__init__(**kwargs)
    async def delete(self):
        pass
    async def add_reaction(self, emoji):
        pass
    async def remove_reaction(self, emoji, member):
        pass

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
        pass
    async def send(self, content = None, embed = None, file = None):
        msg = Message(author = user[0], content = content, embed = embed)
        await self._add(msg)
        return msg

class GestaltTest(unittest.TestCase):
    def test_help(self):
        run(chan[0]._add(Message(author = user[1], content = "gs;help")))
        self.assertIsNotNone(chan[0]._messages[-1].embed)


if __name__ == "__main__":
    user = [
            Member(name = "Gestalt", bot = True),
            Member(name = "test-1")
            ]
    chan = [Channel()]

    instance = gestalt.Gestalt(dbfile = ":memory:", purge = False)
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
