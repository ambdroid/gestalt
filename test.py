#!/usr/bin/python3.7

from unicodedata import lookup as emojilookup
import asyncio

import discord

import testenv
import auth

TIMEOUT = 0.5

class TestClient(discord.Client):
    def  __init__(self, actions):
        super().__init__(fetch_offline_members = False)
        self.loop.create_task(self.do_actions(actions))

    async def do_actions(self, actions):
        self.waited = tuple([(await self.do_action(*x)) for x in actions])
        await self.close()

    async def do_action(self, action, content, waitfor):
        await self.wait_until_ready()
        channel = self.get_channel(testenv.CHANNEL)
        if action == "message":
            await channel.send(content)
        elif action == "react":
            # content = (message # from most recent = 1, reaction)
            message = (await channel.history(limit = content[0]).flatten())[-1]
            await message.add_reaction(emojilookup(content[1]))
        if waitfor:
            try:
                return await self.wait_for(waitfor, timeout = TIMEOUT)
            except: # timeout
                pass
        return None

    def run(self, token):
        super().run(token)
        return self.waited

def test(bot, actions):
    ret = TestClient(actions).run(auth.bots[bot])
    # client.close() closes the event loop, so make another
    asyncio.set_event_loop(asyncio.new_event_loop())
    return ret


print(test(0,(
    ("message", "g hello, world!", "message"),
    ("react", (1, "BLACK QUESTION MARK ORNAMENT"), "message"),
    ("react", (2, "CROSS MARK"), "raw_message_delete"))))
test(0,(
    ("message", "test", None),))
