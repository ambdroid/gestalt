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
        await instance.on_member_update(None, self)
    async def _del_role(self, role):
        self.roles.remove(role)
        role.remove(self)
        await instance.on_member_update(None, self)
    @property
    def id(self): return self.user.id
    @property
    def bot(self): return self.user.bot
    @property
    def name(self): return self.user.name
    @property
    def display_name(self): return self.user.name
    def avatar_url_as(self, **kwargs):
        return "http://avatar.png" # who gives a damn


class Message(Object):
    def __init__(self, **kwargs):
        self._deleted = False
        self.mentions = []
        self.role_mentions = []
        self.webhook_id = None
        self.attachments = []
        self.reactions = []
        self.type = discord.MessageType.default
        super().__init__(**kwargs)

        if self.content != None:
            # mentions can also be in the embed but that's irrelevant here
            for mention in re.findall("(?<=\<\@\!)[0-9]+(?=\>)", self.content):
                self.mentions.append(User.users[int(mention)])
            for mention in re.findall("(?<=\<\@\&)[0-9]+(?=\>)", self.content):
                self.role_mentions.append(Role.roles[int(mention)])
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
    class NotFound(discord.errors.NotFound):
        def __init__(self):
            pass
    hooks = {}
    def __init__(self, channel, name):
        super().__init__()
        self._deleted = False
        (self._channel, self.name) = (channel, name)
        self.token = "t0k3n" + str(self.id)
        Webhook.hooks[self.id] = self
    def partial(id, token, adapter):
        return Webhook.hooks[id]
    async def send(self, username, **kwargs):
        if self._deleted:
            raise Webhook.NotFound()
        msg = Message(**kwargs) # note: absorbs other irrelevant arguments
        msg.webhook_id = self.id
        msg.author = Object(id = self.id, bot = True,
                name = username if username else self.name)
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
            if not msg.author.id in Webhook.hooks:
                msg.author = self.guild.get_member(msg.author.id)
        self._messages.append(msg)
        await instance.on_message(msg)
    async def create_webhook(self, name):
        return Webhook(self, name)
    async def fetch_message(self, id):
        return self._messages[[x.id for x in self._messages].index(id)]
    async def send(self, content = None, embed = None, file = None):
        msg = Message(author = bot, content = content, embed = embed)
        await self._add(msg)
        return msg

class Role(Object):
    roles = {}
    def __init__(self, **kwargs):
        self.members = []
        self.guild = None
        super().__init__(**kwargs)
        self.mention = "<@&%i>" % self.id
        Role.roles[self.id] = self
    async def delete(self):
        await self.guild._del_role(self)

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
    def _add_role(self, name):
        role = Role(guild = self, name = name)
        self._roles[role.id] = role
        return role
        # bot doesn't listen to role creation events
    async def _del_role(self, role):
        if role == self.default_role:
            raise RuntimeError("Can't delete @everyone!")
        # event order observed experimentally
        # discord.py doesn't document these things
        for member in role.members:
            member.roles.remove(role)
            await instance.on_member_update(None, member)
        del self._roles[role.id]
        await instance.on_guild_role_delete(role)
    async def _add_member(self, user, perms = discord.Permissions.all()):
        if user.id in self._members:
            raise RuntimeError("re-adding a member to a guild")
        member = self._members[user.id] = Member(user, self, perms)
        member.roles.append(self.default_role)
        # NOTE: does on_member_update get called here? probably not but idk
        await instance.on_member_join(member)
        return member
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

def send(user, channel, content):
    run(channel._add(Message(author = user, content = content)))
    return channel[-1]

class GestaltTest(unittest.TestCase):

    # ugly hack because parsing gs;p output would be uglier
    def get_proxid(self, user, other):
        if type(other) != int:
            other = other.id
        row = instance.cur.execute(
                "select proxid from proxies where (userid, extraid) = (?, ?)",
                (user.id, other)).fetchone()
        return row[0] if row else None

    def get_collid(self, role):
        row = instance.cur.execute(
                "select collid from collectives where roleid = ?",
                (role.id,)).fetchone()
        return row[0] if row else None

    def assertRowExists(self, query, args = None):
        self.assertIsNotNone(instance.cur.execute(query, args).fetchone())

    def assertRowNotExists(self, query, args = None):
        self.assertIsNone(instance.cur.execute(query, args).fetchone())

    def assertReacted(self, msg, reaction = gestalt.REACT_CONFIRM):
        self.assertEqual(msg.reactions[0].emoji, reaction)

    def assertNotReacted(self, msg):
        self.assertEqual(len(msg.reactions), 0)


    def test_swaps(self):
        chan = g["main"]

        # alpha opens a swap with beta
        self.assertReacted(
                send(alpha, chan, 'gs;swap open <@!%d> "sw "' % beta.id))
        self.assertIsNotNone(self.get_proxid(alpha, beta))
        self.assertIsNone(self.get_proxid(beta, alpha))
        # alpha tests swap; it should fail
        self.assertIsNone(send(alpha, chan, "sw no swap").webhook_id)
        # beta opens swap
        self.assertReacted(
                send(beta, chan, 'gs;swap open <@!%d> "sw "' % alpha.id))
        self.assertIsNotNone(self.get_proxid(alpha ,beta))
        self.assertIsNotNone(self.get_proxid(beta, alpha))
        # alpha and beta test the swap; it should work now
        self.assertIsNotNone(send(alpha, chan, "sw swap").webhook_id)
        self.assertIsNotNone(send(beta, chan, "sw swap").webhook_id)

        # now, with the alpha-beta swap active, alpha opens swap with gamma
        # this one should fail due to prefix conflict
        self.assertNotReacted(
                send(alpha, chan, 'gs;swap open <@!%d> "sw "' % gamma.id))
        # but this one should work
        self.assertReacted(
                send(alpha, chan, 'gs;swap open <@!%d> "sww "' % gamma.id))
        # gamma opens the swap
        self.assertReacted(
                send(gamma, chan, 'gs;swap open <@!%d> "sww "' % alpha.id))
        self.assertIsNotNone(self.get_proxid(alpha, gamma))
        gammaid = self.get_proxid(gamma, alpha)
        self.assertIsNotNone(gammaid)
        self.assertTrue(
                send(alpha, chan, "sww swap").author.name.index(gamma.name)
                != -1)
        self.assertTrue(
                send(gamma, chan, "sww swap").author.name.index(alpha.name)
                != -1)
        # the alpha-beta swap should still work
        self.assertIsNotNone(self.get_proxid(alpha, beta))
        self.assertIsNotNone(self.get_proxid(beta, alpha))
        self.assertTrue(
                send(alpha, chan, "sw swap").author.name.index(beta.name) != -1)
        self.assertTrue(
                send(beta, chan, "sw swap").author.name.index(alpha.name) != -1)

        # close the swaps
        self.assertReacted(send(alpha, chan, 'gs;swap close "sw "'))
        self.assertReacted(
                send(gamma, chan, 'gs;swap close %s' % gammaid))
        self.assertIsNone(self.get_proxid(alpha, beta))
        self.assertIsNone(self.get_proxid(beta, alpha))
        self.assertIsNone(self.get_proxid(alpha, gamma))
        self.assertIsNone(self.get_proxid(gamma, alpha))
        self.assertIsNone(send(beta, chan, "sw no swap").webhook_id)
        self.assertIsNone(send(gamma, chan, "sww no swap").webhook_id)

    def test_help(self):
        msg = send(alpha, g["main"], "gs;help")
        self.assertIsNotNone(msg.embed)
        self.assertReacted(msg, gestalt.REACT_DELETE)
        run(msg._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(msg._deleted)

    def test_add_delete_collective(self):
        # create an @everyone collective
        self.assertReacted(send(alpha, g["main"], "gs;c new everyone"))
        # make sure it worked
        self.assertIsNotNone(self.get_collid(g.default_role))
        # try to make a collective on the same role; it shouldn't work
        self.assertNotReacted(send(alpha, g["main"], "gs;c new everyone"))

        proxid = self.get_proxid(alpha, g.default_role)
        self.assertIsNotNone(proxid)
        # set the prefix
        self.assertReacted(send(alpha, g["main"], "gs;p %s prefix e:" % proxid))
        # test the proxy
        self.assertIsNotNone(send(alpha, g["main"], "e:test").webhook_id)
        # this proxy will be used in later tests


        # now try again with a new role
        role = g._add_role("delete me")
        # add the role to alpha, then create collective
        run(g.get_member(alpha.id)._add_role(role))
        self.assertReacted(send(alpha, g["main"], "gs;c new %s" % role.mention))
        proxid = self.get_proxid(alpha, role)
        self.assertIsNotNone(proxid)

        # set prefix and test it
        self.assertReacted(send(alpha, g["main"], "gs;p %s prefix d:" % proxid))
        self.assertIsNotNone(send(alpha, g["main"], "d:test").webhook_id)

        # delete the collective normally
        collid = self.get_collid(role)
        self.assertReacted(send(alpha, g["main"], "gs;c %s delete" % collid))
        self.assertIsNone(self.get_collid(role))
        self.assertIsNone(self.get_proxid(alpha, role))

        # recreate the collective, then delete the role
        self.assertReacted(send(alpha, g["main"], "gs;c new %s" % role.mention))
        proxid = self.get_proxid(alpha, role)
        self.assertIsNotNone(proxid)
        run(role.delete())
        self.assertIsNone(self.get_proxid(alpha, role))
        self.assertIsNone(self.get_collid(role))

    def test_permissions(self):
        collid = self.get_collid(g.default_role)
        self.assertIsNotNone(collid)
        # beta does not have manage_roles permission; this should fail
        self.assertNotReacted(send(beta, g["main"], "gs;c %s delete" % collid))
        # now change the @everyone collective name; this should work
        self.assertReacted(send(beta, g["main"], "gs;c %s name test" % collid))

        # beta shouldn't be able to change a collective it isn't in
        role = g._add_role("no beta")
        self.assertReacted(send(alpha, g["main"], "gs;c new %s" % role.mention))
        collid = self.get_collid(role)
        self.assertIsNotNone(collid)
        self.assertNotReacted(send(beta, g["main"], "gs;c %s name test" % collid))

    def test_prefix_auto(self):
        # test every combo of auto, prefix, and also the switches thereof
        chan = g["main"]
        proxid = self.get_proxid(alpha, g.default_role)

        self.assertIsNone(send(alpha, chan, "no prefix, no auto").webhook_id)
        self.assertIsNotNone(send(alpha, chan, "e:prefix").webhook_id)
        self.assertReacted(send(alpha, chan, "gs;p %s prefix =" % proxid))
        self.assertIsNotNone(send(alpha, chan, "=prefix, no auto").webhook_id)
        self.assertReacted(send(alpha, chan, "gs;p %s auto on" % proxid))
        self.assertIsNone(send(alpha, chan, "=prefix, auto").webhook_id)
        self.assertIsNotNone(send(alpha, chan, "no prefix, auto").webhook_id)
        # test autoproxy both as on/off and as toggle
        self.assertReacted(send(alpha, chan, "gs;p %s auto" % proxid))
        self.assertReacted(send(alpha, chan, "gs;p %s prefix #text" % proxid))
        self.assertIsNotNone(send(alpha, chan, "#pk-style prefix").webhook_id)
        self.assertReacted(send(alpha, chan, "gs;p %s prefix e:" % proxid))

        # invalid prefixes. these should fail
        self.assertNotReacted(send(alpha, chan, "gs;p %s prefix " % proxid))
        self.assertNotReacted(send(alpha, chan, 'gs;p %s prefix "" ' % proxid))
        self.assertNotReacted(
                send(alpha, chan, 'gs;p %s prefix "text"' % proxid))

    def test_query_delete(self):
        msg = send(alpha, g["main"], "e:reaction test")
        run(msg._react(gestalt.REACT_QUERY, beta))
        self.assertNotEqual(beta.dm_channel[-1].content.find(alpha.name), -1)

        run(msg._react(gestalt.REACT_DELETE, beta))
        self.assertEqual(len(msg.reactions), 0)
        self.assertFalse(msg._deleted)

        run(msg._react(gestalt.REACT_DELETE, alpha))
        self.assertTrue(msg._deleted)

    def test_webhook_shenanigans(self):
        # test what happens when a webhook is deleted
        hookid = send(alpha, g["main"], "e:reiuskudfvb").webhook_id
        self.assertIsNotNone(hookid)
        self.assertRowExists(
                "select * from webhooks where hookid = ?",
                (hookid,))
        Webhook.hooks[hookid]._deleted = True
        self.assertIsNotNone(send(alpha, g["main"], "e:asdhgdfjg").webhook_id)
        self.assertRowNotExists(
                "select * from webhooks where hookid = ?",
                (hookid,))

    # this function requires the existence of at least three ongoing wars
    def test_global_conflicts(self):
        g2 = Guild()
        g2._add_channel("main")
        run(g2._add_member(bot))
        run(g2._add_member(alpha))

        rolefirst = g._add_role("conflict")
        rolesecond = g2._add_role("conflict")
        run(g.get_member(alpha.id)._add_role(rolefirst))
        run(g.get_member(alpha.id)._add_role(rolesecond))

        # open a swap. swaps are global so alpha will use it to test conflicts
        self.assertReacted(
                send(alpha, g["main"], "gs;swap open <@!%i> :" % beta.id))
        self.assertReacted(
                send(beta, g["main"], "gs;swap open <@!%i> :" % alpha.id))
        proxswap = self.get_proxid(alpha, beta)
        self.assertIsNotNone(proxswap)

        # create collectives on the two roles
        self.assertReacted(
                send(alpha, g["main"], "gs;c new %s" % rolefirst.mention))
        self.assertReacted(
                send(alpha, g2["main"], "gs;c new %s" % rolesecond.mention))
        proxfirst = self.get_proxid(alpha, rolefirst)
        proxsecond = self.get_proxid(alpha, rolesecond)
        self.assertIsNotNone(proxfirst)
        self.assertIsNotNone(proxsecond)

        # now alpha can test prefix and auto stuff
        self.assertReacted(
                send(alpha, g["main"], "gs;p %s prefix same:" % proxfirst))
        # this should work because the collectives are in different guilds
        self.assertReacted(
                send(alpha, g2["main"], "gs;p %s prefix same:" % proxsecond))
        self.assertIsNotNone(send(alpha, g["main"], "same: no auto").webhook_id)
        # alpha should be able to set both to auto; different guilds
        self.assertReacted(
                send(alpha, g["main"], "gs;p %s auto on" % proxfirst))
        self.assertReacted(
                send(alpha, g2["main"], "gs;p %s auto on" % proxsecond))
        self.assertIsNotNone(send(alpha, g["main"], "auto on").webhook_id)

        # test global prefix conflict; this should fail
        self.assertNotReacted(
                send(alpha, g["main"], "gs;p %s prefix same:" % proxswap))
        # no conflict; this should work
        self.assertReacted(
                send(alpha, g["main"], "gs;p %s prefix swap:" % proxswap))
        # make a conflict with a collective
        self.assertNotReacted(
                send(alpha, g["main"], "gs;p %s prefix swap:" % proxfirst))
        # now turning on auto on the swap should deactivate the other autos
        self.assertIsNotNone(send(alpha, g["main"], "auto on").webhook_id)
        self.assertNotEqual(
                send(alpha, g["main"], "collective has auto").author.name.index(
                    rolefirst.name), -1)
        self.assertReacted(send(alpha, g["main"], "gs;p %s auto on" % proxswap))
        self.assertNotEqual(
                send(alpha, g["main"], "swap has auto").author.name.index(
                    beta.name), -1)
        # test other prefix conflicts
        self.assertNotReacted(
                send(alpha, g["main"], "gs;p %s prefix sw" % proxfirst))
        self.assertNotReacted(
                send(alpha, g["main"], "gs;p %s prefix swap::" % proxfirst))

        # done. close the swap
        self.assertReacted(send(alpha, g["main"], "gs;swap close swap:"))

    def test_override(self):
        overid = self.get_proxid(alpha, 0)
        # alpha has sent messages visible to bot by now, so should have one
        self.assertIsNotNone(overid)
        proxid = self.get_proxid(alpha, g.default_role)
        self.assertIsNotNone(proxid)

        chan = g["main"]
        self.assertIsNotNone(send(alpha, chan, "e: proxy").webhook_id)
        self.assertReacted(send(alpha, chan, "gs;p %s auto on" % proxid))
        self.assertIsNotNone(send(alpha, chan, "proxy").webhook_id)
        # set the override prefix. this should activate it
        self.assertReacted(send(alpha, chan, "gs;p %s prefix x:" % overid))
        self.assertIsNone(send(alpha, chan, "x: not proxies").webhook_id)

        # turn autoproxy off
        self.assertReacted(send(alpha, chan, "gs;p %s auto" % proxid))
        self.assertIsNone(send(alpha, chan, "not proxied").webhook_id)
        self.assertIsNone(send(alpha, chan, "x: not proxied").webhook_id)


def main():
    global bot, alpha, beta, gamma, g, instance

    instance = TestBot()

    bot = User(name = "Gestalt", bot = True)
    alpha = User(name = "test-alpha")
    beta = User(name = "test-beta")
    gamma = User(name = "test-gamma")
    g = Guild()
    g._add_channel("main")
    run(g._add_member(bot))
    run(g._add_member(alpha))
    run(g._add_member(beta, perms = discord.Permissions(
        # these don't actually matter other than beta not having manage_roles
        add_reactions = True,
        read_messages = True,
        send_messages = True)))
    run(g._add_member(gamma))

    if unittest.main(exit = False).result.wasSuccessful():
        print("But it isn't *really* OK, is it?")


# monkey patch. this probably violates the Geneva Conventions
discord.Webhook.partial = Webhook.partial
# don't spam the channel with error messages
gestalt.DEFAULT_PREFS &= ~gestalt.Prefs.errors


if __name__ == "__main__":
    main()

