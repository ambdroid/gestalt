#!/usr/bin/python3.7

import sqlite3 as sqlite
import discord

import unicodedata

import auth


# for incoming reactions
REACT_QUESTION = b'\\N{BLACK QUESTION MARK ORNAMENT}'
REACT_DELETE = b'\\N{CROSS MARK}'

# for outgoing reacting
EMOJI_CONFIRM = unicodedata.lookup("BALLOT BOX WITH CHECK")

GLOBAL_PREFIX = 'gs;'
MAX_FILE_SIZE = 8388119
DEFAULT_PREFIX = [(0, "g "), (0, "G ")]

def is_text(message):
    return message.channel.type == discord.ChannelType.text

def is_dm(message):
    return message.channel.type == discord.ChannelType.private

def is_admin(message):
    return message.author.permissions_in(message.channel).administrator

# returns False or message with prefix chopped
def prefixed(message):
    cur.execute("select hookid, prefix from users where userid = ?",
            (message.author.id,))
    rows = cur.fetchall()
    if len(rows) == 0:
        rows = DEFAULT_PREFIX
    for i in rows:
        if message.content[:len(i[1])] == i[1]:
            return message.content[len(i[1]):]
    return False

class Gestalt(discord.Client):

    async def on_ready(self):
        print('Logged in as {0}, id {1}!'.format(self.user, self.user.id))
        await self.change_presence(status = discord.Status.online,
                activity = discord.Game(name = GLOBAL_PREFIX + "help"))

    async def do_command(self, message, cmd):
        if (cmd == "shutdown" and is_admin(message)):
            if not is_dm(message):
                await message.delete()
            await self.close()
        elif (cmd == "commit" and is_admin(message)):
            conn.commit()
            print("Manual commit.")
            await message.add_reaction(EMOJI_CONFIRM)
        elif cmd == "help":
            msgid = (await message.channel.send(
                    "By default, I will proxy any message that begins " +
                    "with `g ` or `G `. So `g hello!` will become `hello!`\n"+
                    "(But I'll leave `gello` alone, of course.)\n"+
                    "\n"+
                    "Use `"+GLOBAL_PREFIX+"prefix` to set a custom prefix.\n"+
                    "Examples:\n"+
                    "`prefix =` to proxy with `=hello!`\n"+
                    "`prefix \"h \"` to proxy with `h hello!`\n"+
                    "`prefix delete` to revert your prefix to the defaults.\n"+
                    "\n"+
                    "You can change my nick with `"+GLOBAL_PREFIX+"nick`!\n"+
                    "\n"+
                    "React with :x: to delete a message you sent.\n"+
                    "React with :question: to query who sent a message.\n"+
                    "(If you don't receive the DM, DM me first.)\n"+
                    "\n"+
                    "One last thing: if you upload a photo and put `spoiler` "+
                    "somewhere in the message body, I'll spoiler it for you.\n"+
                    "This is useful if you're on mobile."
                    )).id
            if is_text(message):
                # put this in the history db so it can be deleted if desired
                author = message.author
                authid = author.id
                authname = author.name + "#" + author.discriminator
                cur.execute("insert into history values (?, ?, ?, NULL, 0)",
                        (msgid, authid, authname))


        # discord trims whitespace so if there's any it isn't the whole message
        elif cmd[:7] == "prefix ":
            prefix = cmd[7:].strip();
            if prefix[0] == '"' and prefix[-1] == '"':
                prefix = prefix[1:-1]
            userid = message.author.id
            if prefix == "delete":
                cur.execute("delete from users where userid = ?", (userid,))
            else:
                cur.execute("insert into users values (?, null, null, ?)"+
                        "on conflict(userid) do update set prefix = ?",
                        (userid, prefix, prefix))
            await message.add_reaction(EMOJI_CONFIRM)

        elif cmd[:5] == "nick ":
            if is_dm(message):
                await message.author.send("I can only do that in a server!");
                return
            await message.guild.get_member(self.user.id).edit(
                    nick = cmd[5:].strip())
            await message.add_reaction(EMOJI_CONFIRM)


    async def on_message(self, message):
        if message.author.bot:
            return

        if not is_text(message):
            if is_dm(message):
                if prefixed(message):
                    await message.channel.send(
                            "I can't function in DMs :( I'm flattered though!")
                    return

                # in dms, global prefix is optional
                cmd = message.content
                if cmd[:len(GLOBAL_PREFIX)] == GLOBAL_PREFIX:
                    cmd = cmd[len(GLOBAL_PREFIX):]
                await self.do_command(message, cmd)
                
            return

        unprefixed = prefixed(message)
        if unprefixed:
            # time to proxy!
            msgfile = None
            if (len(message.attachments) > 0
                    and message.attachments[0].size <= MAX_FILE_SIZE):
                attach = message.attachments[0]
                # only copy the first attachment
                msgfile = await attach.to_file()
                # bonus feature: lets mobile users upload with spoilers
                if unprefixed.lower().find("spoiler") != -1:
                    msgfile.filename = "SPOILER_" + msgfile.filename

            msgid = (await message.channel.send(content = unprefixed,
                file = msgfile)).id
            authid = message.author.id
            authname = message.author.name + "#" + message.author.discriminator
            cur.execute("insert into history values (?, ?, ?, ?, 0)",
                    (msgid, authid, authname, unprefixed)) # deleted = 0
            await message.delete()

        if message.content[:len(GLOBAL_PREFIX)] == GLOBAL_PREFIX:
            await self.do_command(message, message.content[len(GLOBAL_PREFIX):])

    # on_reaction_add doesn't catch everything
    async def on_raw_reaction_add(self, payload):
        # first, make sure this is one of ours
        # db is faster than fetch
        cur.execute(
            "select authid, authname from history where msgid = ? limit 1",
            (payload.message_id,))
        row = cur.fetchone()
        if row == None:
            return

        # emoji name given in unicode, so translate to ascii
        emoji = payload.emoji.name.encode(encoding = 'ascii',
                errors = 'namereplace')

        # we can't fetch the message directly, at least not in this api binding.
        # so fetch the channel first, *then* use it to fetch the message.
        channel = self.get_channel(payload.channel_id)
        reactor = self.get_user(payload.user_id)
        message = await channel.fetch_message(payload.message_id)

        if message.author.id != self.user.id:
            return

        if emoji == REACT_QUESTION:
            try:
                await reactor.send(
                        "Message sent by " + row[1] + " id " + str(row[0]))
            except discord.Forbidden:
                pass

            await message.remove_reaction(payload.emoji.name, reactor)

        elif emoji == REACT_DELETE:
            # only sender may delete proxied message
            if payload.user_id == row[0]:
                cur.execute(
                    "update history set deleted = 1 where msgid = ?",
                    (payload.message_id,))
                await message.delete()
            else:
                await message.remove_reaction(payload.emoji.name, reactor)



instance = Gestalt()

conn = sqlite.connect("gestalt.db")
cur = conn.cursor()
cur.execute(
	"create table if not exists history("+
        "msgid integer primary key,"+
        "authid integer,"+
        "authname text,"+
        "content text,"
        "deleted integer)")
cur.execute(
	"create table if not exists users("+
        "userid integer primary key,"+
        "guildid integer,"+ # currently unused
        "hookid integer,"+  # currently unused
        "prefix text)")

try:
    instance.run(auth.token)
except RuntimeError:
    print("Runtime error.")

print("Shutting down.")
conn.commit()
conn.close()
