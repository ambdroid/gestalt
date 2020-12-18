import discord

from defs import *

class CommandReader:
    BOOL_KEYWORDS = {
        "on": 1,
        "off": 0,
        "yes": 1,
        "no": 0,
        "true": 1,
        "false": 0,
        "0": 0,
        "1": 1
    }

    def __init__(self, msg, cmd):
        self.msg = msg
        self.cmd = cmd

    def is_empty(self):
        return self.cmd == ""

    def read_word(self):
        # add empty strings to pad array if string empty or no split
        split = self.cmd.split(maxsplit = 1) + ["",""]
        self.cmd = split[1]
        return split[0]

    def read_quote(self):
        match = re.match('\\"[^\\"]*\\"', self.cmd)
        if match == None:
            return self.read_word()
        self.cmd = match.string[len(match[0]):].strip()
        return match[0][1:-1]

    def read_quote_reverse(self):
        self.cmd = self.cmd[::-1]
        ret = self.read_quote()[::-1]
        self.cmd = self.cmd[::-1]
        return ret

    def read_bool_int(self):
        word = self.read_word().lower()
        if word not in CommandReader.BOOL_KEYWORDS:
            return None
        return CommandReader.BOOL_KEYWORDS[word]

    def read_remainder(self):
        ret = self.cmd
        if len(ret) > 1 and ret[0] == ret[-1] == '"':
            ret = ret[1:-1]
        self.cmd = ""
        return ret


# discord.py commands extension throws out bot messages
# this is incompatible with the test framework so process commands manually
async def do_command(self, message, cmd):
    reader = CommandReader(message, cmd)
    arg = reader.read_word().lower()
    authid = message.author.id

    """
    if arg == "debug":
        for table in ["users", "proxies", "collectives"]:
            await self.send_embed(message, "```%s```" % "\n".join(
                ["|".join([str(i) for i in x]) for x in self.cur.execute(
                    "select * from %s" % table).fetchall()]))
        return
    """

    if arg == "help":
        await self.send_embed(message, HELPMSG)

    elif arg == "invite" and self.invite:
        await self.send_embed(message,
                discord.utils.oauth_url(self.user.id, permissions = PERMS))

    elif arg == "permcheck":
        guildid = reader.read_word()
        if (guildid == "" and (is_dm(message)
                or not re.match("[0-9]*", guildid))):
            raise RuntimeError("Please provide a valid guild ID.")
        guildid = message.guild.id if guildid == "" else int(guildid)
        guild = self.get_guild(guildid)
        if guild == None:
            raise RuntimeError(
                    "That guild does not exist or I am not in it.")
        memberbot = guild.get_member(self.user.id)
        memberauth = guild.get_member(authid)
        if memberauth == None:
            raise RuntimeError("You are not a member of that guild.")

        text = "**%s**:\n" % guild.name
        noaccess = False
        for chan in guild.text_channels:
            if not memberauth.permissions_in(chan).view_channel:
                noaccess = True
                continue

            errors = []
            for p in PERMS: # p = ("name", bool)
                if p[1] and not p in list(memberbot.permissions_in(chan)):
                    errors += [p[0]]

            # lack of access implies lack of other perms, so leave them out
            if "read_messages" in errors:
                errors = ["read_messages"]
            errors = REACT_CONFIRM if errors == [] else ", ".join(errors)
            text += "`#%s`: %s\n" % (chan.name, errors)

        if noaccess:
            text += "Some channels you can't see are omitted."
        await self.send_embed(message, text)

    elif arg in ["proxy", "p"]:
        proxid = reader.read_word().lower()
        arg = reader.read_word().lower()

        if proxid == "":
            rows = self.cur.execute(
                    "select * from proxies where userid = ?"
                    "order by type asc",
                    (authid,)).fetchall()
            lines = []
            # must be at least one: the override
            for proxy in rows:
                # sanitize text to not mess up formatting
                s = lambda x : discord.utils.escape_markdown(str(x))
                line = "`%s`" % proxy["proxid"]
                if proxy["type"] == ProxyType.override:
                    line += (":no_entry: prefix **%s**"
                            %(s(proxy["prefix"]),))
                elif proxy["type"] == ProxyType.swap:
                    line += (":twisted_rightwards_arrows: with **%s** "
                            "prefix **%s**"
                            % (s(self.get_user(proxy["extraid"])),
                                s(proxy["prefix"])))
                elif proxy["type"] == ProxyType.collective:
                    guild = self.get_guild(proxy["guildid"])
                    name = self.cur.execute(
                            "select nick from collectives "
                            "where roleid = ?",
                            (proxy["extraid"],)).fetchone()[0]
                    line += (":bee: **%s** on **%s** in **%s** "
                            "prefix **%s**"
                            % (s(name),
                                s(guild.get_role(proxy["extraid"]).name),
                                s(guild.name), s(proxy["prefix"])))
                if proxy["active"] == 0:
                    line += " *(inactive)*"
                lines.append(line)
            return await self.send_embed(message, "\n".join(lines))

        proxy = self.cur.execute("select * from proxies where proxid = ?",
                (proxid,)).fetchone()
        if proxy == None or proxy["userid"] != authid:
            raise RuntimeError("You do not have a proxy with that ID.")

        if arg == "prefix":
            arg = reader.read_quote().lower()
            if arg.replace("text","") == "":
                raise RuntimeError("Please provide a valid prefix.")

            arg = arg.lower()
            # adapt PluralKit [text] prefix/postfix format
            if arg.endswith("text"):
                arg = arg[:-4]

            self.cur.execute(
                "update proxies set prefix = ? where proxid = ?",
                (arg, proxy["proxid"]))
            if proxy["type"] != ProxyType.swap:
                self.cur.execute(
                    "update proxies set active = 1 where proxid = ?",
                    (proxy["proxid"],))

            await message.add_reaction(REACT_CONFIRM)

        elif arg == "auto":
            if proxy["type"] == ProxyType.override:
                raise RuntimeError("You cannot autoproxy your override.")

            if reader.is_empty():
                self.cur.execute(
                    "update proxies set auto = 1 - auto where proxid = ?",
                    (proxy["proxid"],))
            else:
                arg = reader.read_bool_int()
                if arg == None:
                    raise RuntimeError("Please specify 'on' or 'off'.")
                self.cur.execute(
                    "update proxies set auto = ? where proxid = ?",
                    (arg, proxy["proxid"]))

            await message.add_reaction(REACT_CONFIRM)

    elif arg in ["collective", "c"]:
        if is_dm(message):
            raise RuntimeError(ERROR_DM)
        guild = message.guild
        arg = reader.read_word().lower()

        if arg == "":
            rows = self.cur.execute(
                    "select * from collectives where guildid = ?",
                    (guild.id,)).fetchall()
            text = "\n".join(["`%s`: %s %s" %
                    (row["collid"],
                        "**%s**" % (row["nick"] if row["nick"]
                            else "*(no name)*"),
                        # @everyone.mention shows up as @@everyone. weird!
                        # note that this is an embed; mentions don't work
                        ("@everyone" if row["roleid"] == guild.id
                            else guild.get_role(row["roleid"]).mention))
                    for row in rows])
            if not text:
                text = "This guild does not have any collectives."
            return await self.send_embed(message, text)

        elif arg in ["new", "create"]:
            if not message.author.guild_permissions.manage_roles:
                raise RuntimeError(ERROR_MANAGE_ROLES)

            guild = message.channel.guild
            rolename = reader.read_remainder()
            if len(message.role_mentions) > 0:
                role = message.role_mentions[0]
            elif rolename == "everyone":
                role = guild.default_role
            else:
                role = discord.utils.get(guild.roles, name = rolename)
                if role == None:
                    raise RuntimeError("Please provide a role.")

            if role.managed:
                # bots, server booster, integrated subscription services
                # requiring users to pay to participate is antithetical
                # to community-oriented identity play
                # TODO: return to this with RoleTags in 1.6
                raise RuntimeError(ERROR_CURSED)

            # new collective with name of role and no avatar
            self.cur.execute("insert or ignore into collectives values"
                    "(?, ?, ?, ?, NULL)",
                    (self.gen_id(), guild.id, role.id, role.name))
            if self.cur.rowcount == 1:
                self.sync_role(role)
                await message.add_reaction(REACT_CONFIRM)

        else: # arg is collective ID
            collid = arg
            action = reader.read_word().lower()
            row = self.cur.execute(
                    "select * from collectives where collid = ?",
                    (collid,)).fetchone()
            if row == None:
                raise RuntimeError("Invalid collective ID!")

            if row["guildid"] != guild.id:
                # TODO allow commands outside server
                raise RuntimeError("Please try that again in %s"
                        % self.get_guild(row["guildid"]).name)

            if action in ["name", "avatar"]:
                arg = reader.read_remainder()

                role = guild.get_role(row["roleid"])
                if role == None:
                    raise RuntimeError("That role no longer exists?")

                member = message.author # Member because this isn't a DM
                if not (role in member.roles
                        or member.guild_permissions.manage_roles):
                    raise RuntimeError(
                            "You don't have access to that collective!")

                if arg == "":
                    # allow empty avatar URL but not name
                    if action == "name":
                        raise RuntimeError("Please provide a new name.")
                    elif message.attachments:
                        arg = message.attachments[0].url

                self.cur.execute(
                        "update collectives set %s = ? "
                        "where collid = ?"
                        % ("nick" if action == "name" else "avatar"),
                        (arg, collid))
                if self.cur.rowcount == 1:
                    await message.add_reaction(REACT_CONFIRM)

            elif action == "delete":
                if not message.author.guild_permissions.manage_roles:
                    raise RuntimeError(ERROR_MANAGE_ROLES)

                # all the more reason to delete it then, right?
                # if guild.get_role(row[1]) == None:
                self.cur.execute("delete from proxies where extraid = ?",
                        (row["roleid"],))
                self.cur.execute("delete from collectives where collid = ?",
                        (collid,))
                if self.cur.rowcount == 1:
                    await message.add_reaction(REACT_CONFIRM)

    elif arg == "prefs":
        # user must exist due to on_message
        user = self.cur.execute(
                "select * from users where userid = ?",
                (authid,)).fetchone()
        arg = reader.read_word()
        if len(arg) == 0:
            # list current prefs in "pref: [on/off]" format
            text = "\n".join(["%s: **%s**" %
                    (pref.name, "on" if user["prefs"] & pref else "off")
                    for pref in Prefs])
            return await self.send_embed(message, text)

        if arg in ["default", "defaults"]:
            self.cur.execute(
                    "update users set prefs = ? where userid = ?",
                    (DEFAULT_PREFS, authid))
            return await message.add_reaction(REACT_CONFIRM)

        if not arg in Prefs.__members__.keys():
            raise RuntimeError("That preference does not exist.")

        bit = int(Prefs[arg])
        if reader.is_empty(): # only "prefs" + name given. invert the thing
            prefs = user["prefs"] ^ bit
        else:
            value = reader.read_bool_int()
            if value == None:
                raise RuntimeError("Please specify 'on' or 'off'.")
            prefs = (user["prefs"] & ~bit) | (bit * value)
        self.cur.execute(
                "update users set prefs = ? where userid = ?",
                (prefs, authid))

        await message.add_reaction(REACT_CONFIRM)

    elif arg in ["swap", "s"]:
        arg = reader.read_word().lower()
        if arg == "open":
            if is_dm(message):
                raise RuntimeError(ERROR_DM)

            prefix = reader.read_quote_reverse().lower()
            membername = reader.read_remainder()

            # discord.ext includes a MemberConverter
            # but that's only available whem using discord.ext Command
            member = (message.mentions[0] if len(message.mentions) > 0 else
                    message.channel.guild.get_member_named(membername))
            if member == None:
                raise RuntimeError("User not found.")
            if membername == "": # prefix absorbed member name
                raise RuntimeError(
                        "Please provide a prefix after the user.")

            if member.id == self.user.id:
                raise RuntimeError(ERROR_BLURSED)
            if member.bot:
                raise RuntimeError(ERROR_CURSED)

            # activate author->other swap
            self.cur.execute("insert or ignore into proxies values"
                    # id, auth, guild, prefix, type, member, auto, active
                    "(?, ?, 0, ?, ?, ?, 0, 0)",
                    (self.gen_id(), authid, prefix, ProxyType.swap,
                        member.id))
            # triggers will take care of activation if necessary

            if self.cur.rowcount == 1:
                await message.add_reaction(REACT_CONFIRM)

        elif arg in ["close", "off"]:
            swapname = reader.read_quote().lower()
            if swapname == "":
                raise RuntimeError("Please provide a swap ID or prefix.")

            self.cur.execute(
                    "delete from proxies where "
                    "(userid, type) = (?, ?) and (? in (proxid, prefix))",
                    (authid, ProxyType.swap, swapname))
            if self.cur.rowcount == 0:
                raise RuntimeError(
                        "You do not have a swap with that ID or prefix.")
            await message.add_reaction(REACT_CONFIRM)

