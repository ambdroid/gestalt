#!/usr/bin/python3.7

import asyncio

import discord

import gestalt
import testenv
import auth

import unittest

TIMEOUT = 3

class TestClient(discord.Client):
    def  __init__(self, actions):
        super().__init__(fetch_offline_members = False)
        self.loop.create_task(self.do_actions(actions))

    async def do_actions(self, actions):
        self.response = tuple([(await self.do_action(*x)) for x in actions])
        await self.close()

    async def do_action(self, action, content, waitfor):
        await self.wait_until_ready()
        channel = self.get_channel(testenv.CHANNEL)
        if action == "message":
            await channel.send(content)
        elif action == "react":
            # content = (message # from most recent = 1, reaction)
            message = (await channel.history(limit = content[0]).flatten())[-1]
            await message.add_reaction(content[1])
        else:
            raise ValueError("Need a valid action!")
        ret = None
        if waitfor:
            try:
                ret = await self.wait_for(waitfor, timeout = TIMEOUT)
            except: # timeout
                pass
        await asyncio.sleep(0.2)
        return ret

    def run(self, token):
        super().run(token)
        return self.response

def test(bot, actions):
    ret = TestClient(actions).run(auth.bots[bot])
    # client.close() closes the event loop, so make another
    asyncio.set_event_loop(asyncio.new_event_loop())
    return ret



class GestaltTest(unittest.TestCase):

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


unittest.main()
