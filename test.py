#!/usr/bin/python3.7

from unicodedata import lookup as emojilookup
import asyncio

import discord

import testenv
import auth

TIMEOUT = 0.5

class TestClient(discord.Client):
    def  __init__(self, action, content, waitfor):
        super().__init__(fetch_offline_members = False)
        self.loop.create_task(self.do_action(action, content, waitfor))

    async def do_action(self, action, content, waitfor):
        await self.wait_until_ready()
        channel = self.get_channel(testenv.CHANNEL)
        if action == "message":
            await channel.send(content)
        elif action == "react":
            message = (await channel.history(limit = content[0]).flatten())[-1]
            await message.add_reaction(emojilookup(content[1]))
        if waitfor:
            try:
                self.waited = await self.wait_for(waitfor, timeout = TIMEOUT)
            except: # timeout
                self.waited = None
        else:
            self.waited = None
        await self.close()

    def run(self, token):
        super().run(token)
        return self.waited

def test(bot, action, content, waitfor):
    client = TestClient(action, content, waitfor)
    ret = client.run(auth.bots[bot])
    asyncio.set_event_loop(asyncio.new_event_loop())
    return ret


print(test(0, "message", "g hello, world!", "message"))
print(test(0, "react", (1, "BLACK QUESTION MARK ORNAMENT"), "message"))
print(test(0, "react", (2, "CROSS MARK"), "raw_message_delete"))
