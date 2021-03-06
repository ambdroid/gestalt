import discord

from defs import *


def escape(text):
    return discord.utils.escape_markdown(
            discord.utils.escape_mentions(str(text)))


# [text] -> ['[',']']
def parse_tags(tags):
    split = tags.lower().split("text")
    if len(split) != 2 or not "".join(split):
        raise RuntimeError(
                "Please provide valid tags around `text` (e.g. `[text]`).")
    return split


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

    def read_bool_int(self):
        word = self.read_word().lower()
        if word in CommandReader.BOOL_KEYWORDS:
            return CommandReader.BOOL_KEYWORDS[word]

    def read_remainder(self):
        ret = self.cmd
        if len(ret) > 1 and ret[0] == ret[-1] == '"':
            ret = ret[1:-1]
        self.cmd = ""
        return ret

    # discord.ext includes a MemberConverter
    # but that's only available whem using discord.ext Command
    def read_member(self):
        # even if the member is in a mention, consume the text of the mention
        name = self.read_quote()
        if self.msg.mentions:
            return self.msg.mentions[0]
        return self.msg.guild.get_member_named(name)

    def read_role(self):
        name = self.read_quote()
        if self.msg.role_mentions:
            return self.msg.role_mentions[0]
        guild = self.msg.guild
        if name == "everyone":
            return guild.default_role
        return discord.utils.get(guild.roles, name = name)


class GestaltCommands:
    async def cmd_debug(self, message):
        for table in ["users", "proxies", "masks"]:
            await self.send_embed(message, "```%s```" % "\n".join(
                ["|".join([str(i) for i in x]) for x in self.fetchall(
                    "select * from %s" % table)]))


    async def cmd_help(self, message):
        await self.send_embed(message, HELPMSG)


    async def cmd_invite(self, message):
        if (await self.application_info()).bot_public:
            await self.send_embed(message,
                    discord.utils.oauth_url(self.user.id, permissions = PERMS))


    async def cmd_permcheck(self, message, guildid):
        guildid = message.guild.id if guildid == "" else int(guildid)
        guild = self.get_guild(guildid)
        if guild == None:
            raise RuntimeError(
                    "That guild does not exist or I am not in it.")
        if guild.get_member(message.author.id) == None:
            raise RuntimeError("You are not a member of that guild.")

        memberauth = guild.get_member(message.author.id)
        memberbot = guild.get_member(self.user.id)
        lines = ["**%s**:" % guild.name]
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
            lines.append("`#%s`: %s" % (chan.name, errors))

        if noaccess:
            lines.append("Some channels you can't see are omitted.")
        await self.send_embed(message, "\n".join(lines))


    async def cmd_proxy_list(self, message):
        rows = self.fetchall(
                "select p.*, m.roleid, m.nick from ("
                    "select * from proxies where userid = ?"
                    "order by type asc"
                ") as p left join masks as m "
                "on p.maskid = m.maskid",
                (message.author.id,))

        lines = []
        omit = False
        # must be at least one: the override
        for proxy in rows:
            if proxy["state"] == ProxyState.hidden:
                continue
            # don't show non-global proxies in other servers
            if message.guild and proxy["guildid"] not in [0, message.guild.id]:
                omit = True
                continue
            line = "`%s`" % proxy["proxid"]
            if proxy["type"] == ProxyType.override:
                line += SYMBOL_OVERRIDE
            elif proxy["type"] == ProxyType.swap:
                user = self.get_user(proxy["otherid"])
                if not user:
                    continue
                line += ("%s with **%s**" % (SYMBOL_SWAP, escape(user)))
            elif proxy["type"] == ProxyType.collective:
                guild = self.get_guild(proxy["guildid"])
                if not guild:
                    continue
                line += ("%s **%s** on **%s** in **%s**"
                        % (SYMBOL_COLLECTIVE, escape(proxy["nick"]),
                            escape(guild.get_role(proxy["roleid"]).name),
                            escape(guild.name)))
            if proxy["prefix"] is not None:
                line += (" tags `%s`"
                        % (proxy["prefix"] + "text" + proxy["postfix"])
                        # hack because escaping ` doesn't work in code blocks
                        .replace("`", "\N{REVERSED PRIME}"))
            if proxy["state"] == ProxyState.inactive:
                line += " *(inactive)*"
            if proxy["auto"] == 1:
                line += " auto **on**"
            lines.append(line)

        if omit:
            lines.append("Proxies in other servers have been omitted.")
        await self.send_embed(message, "\n".join(lines))


    async def cmd_proxy_tags(self, message, proxid, tags):
        exists = self.fetchone(
                "select 1 from proxies where (userid, proxid) = (?, ?)",
                (message.author.id, proxid))
        if not exists:
            raise RuntimeError("You do not have a proxy with that ID.")

        (prefix, postfix) = parse_tags(tags)

        self.execute(
            "update proxies set prefix = ?, postfix = ? where proxid = ?",
            (prefix, postfix, proxid))

        await self.try_add_reaction(message, REACT_CONFIRM)


    async def cmd_proxy_auto(self, message, proxid, auto):
        proxy = self.fetchone(
                "select * from proxies where (userid, proxid) = (?, ?)",
                (message.author.id, proxid))
        if proxy == None:
            raise RuntimeError("You do not have a proxy with that ID.")

        if auto == None:
            auto = 1 - proxy["auto"]
        # triggers will take care of unsetting other autos as necessary
        self.execute(
                "update proxies set auto = ? where proxid = ?",
                (auto, proxid))

        if proxy["type"] == ProxyType.override:
            # ...but override can't actually be auto'd, that makes no sense
            self.execute(
                    "update proxies set auto = 0 where proxid = ?",
                    (proxid,))

        await self.try_add_reaction(message, REACT_CONFIRM)


    async def cmd_collective_list(self, message):
        rows = self.fetchall(
                "select * from masks where guildid = ?",
                (message.guild.id,))

        if len(rows) == 0:
            text = "This guild does not have any collectives."
        else:
            guild = message.guild
            text = "\n".join(["`%s`: %s %s" %
                    (row["maskid"],
                        "**%s**" % escape(row["nick"]),
                        # @everyone.mention shows up as @@everyone. weird!
                        # note that this is an embed; mentions don't work
                        ("@everyone" if row["roleid"] == guild.id
                            else guild.get_role(row["roleid"]).mention))
                    for row in rows])

        await self.send_embed(message, text)


    async def cmd_collective_new(self, message, role):
        # new collective with name of role and no avatar
        collid = self.gen_id()
        self.execute("insert or ignore into masks values"
                "(?, ?, ?, ?, NULL)",
                (collid, role.guild.id, role.id, role.name))
        # if there wasn't already a collective on that role
        if self.cur.rowcount == 1:
            for member in role.members:
                if not member.bot:
                    self.execute(
                            # tags = NULL, auto = 0
                            "insert into proxies values "
                            "(?, ?, ?, NULL, NULL, ?, NULL, ?, 0, ?)",
                            (self.gen_id(), member.id, role.guild.id,
                                ProxyType.collective, collid,
                                ProxyState.active))

            await self.try_add_reaction(message, REACT_CONFIRM)


    async def cmd_collective_update(self, message, collid, name, value):
        self.execute(
                "update masks set %s = ? "
                "where maskid = ?"
                % ("nick" if name == "name" else "avatar"),
                (value, collid))
        if self.cur.rowcount == 1:
            await self.try_add_reaction(message, REACT_CONFIRM)


    async def cmd_collective_delete(self, message, coll):
        self.execute("delete from proxies where maskid = ?", (coll["maskid"],))
        self.execute("delete from masks where maskid = ?", (coll["maskid"],))
        if self.cur.rowcount == 1:
            await self.try_add_reaction(message, REACT_CONFIRM)


    async def cmd_prefs_list(self, message, user):
        # list current prefs in "pref: [on/off]" format
        text = "\n".join(["%s: **%s**" %
                (pref.name, "on" if user["prefs"] & pref else "off")
                for pref in Prefs])
        await self.send_embed(message, text)


    async def cmd_prefs_default(self, message):
        self.execute(
                "update users set prefs = ? where userid = ?",
                (DEFAULT_PREFS, message.author.id))
        await self.try_add_reaction(message, REACT_CONFIRM)


    async def cmd_prefs_update(self, message, user, name, value):
        bit = int(Prefs[name])
        if value == None: # only "prefs" + name given. invert the thing
            prefs = user["prefs"] ^ bit
        else:
            prefs = (user["prefs"] & ~bit) | (bit * value)
        self.execute(
                "update users set prefs = ? where userid = ?",
                (prefs, message.author.id))

        await self.try_add_reaction(message, REACT_CONFIRM)


    def make_or_activate_swap(self, authid, targetid, tags):
        (prefix, postfix) = parse_tags(tags) if tags else (None, None)
        # to support future features, look at proxies from target, not author
        swap = self.fetchone(
                "select state from proxies "
                "where (userid, otherid) = (?, ?)",
                (targetid, authid))
        if not swap:
            # create swap. author's is inactive, target's is hidden
            # id, auth, guild, prefix, postfix, type, member, mask, auto, state
            self.execute("insert or ignore into proxies values"
                    "(?, ?, 0, ?, ?, ?, ?, NULL, 0, ?),"
                    "(?, ?, 0, NULL, NULL, ?, ?, NULL, 0, ?)",
                    (self.gen_id(), authid, prefix, postfix,
                        ProxyType.swap, targetid, ProxyState.inactive)
                    + (self.gen_id(), targetid, ProxyType.swap, authid,
                        ProxyState.hidden))
            return bool(self.cur.rowcount)
        elif swap[0] == ProxyState.inactive:
            # target is initiator. author can activate swap
            self.execute(
                    "update proxies set prefix = ?, postfix = ?, state = ?"
                    "where (userid, otherid) = (?, ?)",
                    (prefix, postfix, ProxyState.active, authid, targetid))
            self.execute(
                    "update proxies set state = ? "
                    "where (userid, otherid) = (?, ?)",
                    (ProxyState.active, targetid, authid))
            return bool(self.cur.rowcount)

        return False


    async def cmd_swap_open(self, message, member, tags):
        if self.make_or_activate_swap(message.author.id, member.id, tags):
            await self.try_add_reaction(message, REACT_CONFIRM)


    async def cmd_swap_close(self, message, proxy):
        self.execute(
                "delete from proxies "
                "where proxid = ? or (userid, otherid) = (?, ?)",
                (proxy["proxid"], proxy["otherid"], proxy["userid"]))
        if self.cur.rowcount:
            await self.try_add_reaction(message, REACT_CONFIRM)


    # discord.py commands extension throws out bot messages
    # this is incompatible with the test framework so process commands manually

    # parse, convert, and validate arguments, then call the relevant function
    async def do_command(self, message, cmd):
        reader = CommandReader(message, cmd)
        arg = reader.read_word().lower()
        authid = message.author.id

        if arg == "help":
            return await self.cmd_help(message)

        elif arg == "invite":
            return await self.cmd_invite(message)

        elif arg == "permcheck":
            guildid = reader.read_word()
            if re.search("[^0-9]", guildid) or not (guildid or message.guild):
                raise RuntimeError("Please provide a valid guild ID.")
            return await self.cmd_permcheck(message, guildid)

        elif arg in ["proxy", "p"]:
            proxid = reader.read_word()
            arg = reader.read_word().lower()

            if proxid == "":
                return await self.cmd_proxy_list(message)

            if arg == "tags":
                arg = reader.read_remainder()
                return await self.cmd_proxy_tags(message, proxid, arg)

            elif arg == "auto":
                if reader.is_empty():
                    val = None
                else:
                    val = reader.read_bool_int()
                    if val == None:
                        raise RuntimeError("Please specify 'on' or 'off'.")
                return await self.cmd_proxy_auto(message, proxid, val)

        elif arg in ["collective", "c"]:
            if not message.guild:
                raise RuntimeError(ERROR_DM)
            guild = message.guild
            arg = reader.read_word()

            if arg == "":
                return await self.cmd_collective_list(message)

            elif arg.lower() in ["new", "create"]:
                if not message.author.guild_permissions.manage_roles:
                    raise RuntimeError(ERROR_MANAGE_ROLES)

                role = reader.read_role()
                if role == None:
                    raise RuntimeError("Please provide a role.")

                if role.managed:
                    # bots, server booster, integrated subscription services
                    # requiring users to pay to participate is antithetical
                    # to community-oriented identity play
                    raise RuntimeError(ERROR_CURSED)

                return await self.cmd_collective_new(message, role)

            else: # arg is collective ID
                collid = arg
                action = reader.read_word().lower()
                row = self.fetchone(
                        "select * from masks "
                        "where (maskid, guildid) = (?, ?)",
                        (collid, guild.id))
                if row == None:
                    raise RuntimeError(
                            "This guild has no collective with that ID.")

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

                    # allow empty avatar URL but not name
                    if action == "name" and not arg:
                        raise RuntimeError("Please provide a new name.")
                    if action == "avatar":
                        if message.attachments and not arg:
                            arg = message.attachments[0].url
                        elif arg and not re.match("http(s?)://.*", arg):
                            raise RuntimeError("Invalid avatar URL!")

                    return await self.cmd_collective_update(message, collid,
                            action, arg)

                elif action == "delete":
                    if not message.author.guild_permissions.manage_roles:
                        raise RuntimeError(ERROR_MANAGE_ROLES)
                    # all the more reason to delete it then, right?
                    # if guild.get_role(row[1]) == None:

                    return await self.cmd_collective_delete(message, row)

        elif arg == "prefs":
            # user must exist due to on_message
            user = self.fetchone(
                    "select * from users where userid = ?",
                    (authid,))
            arg = reader.read_word()
            if len(arg) == 0:
                return await self.cmd_prefs_list(message, user)

            if arg in ["default", "defaults"]:
                return await self.cmd_prefs_default(message)

            if not arg in Prefs.__members__.keys():
                raise RuntimeError("That preference does not exist.")

            if reader.is_empty():
                value = None
            else:
                value = reader.read_bool_int()
                if value == None:
                    raise RuntimeError("Please specify 'on' or 'off'.")

            return await self.cmd_prefs_update(message, user, arg, value)

        elif arg in ["swap", "s"]:
            arg = reader.read_word().lower()
            if arg == "open":
                if not message.guild:
                    raise RuntimeError(ERROR_DM)

                member = reader.read_member()
                if member == None:
                    raise RuntimeError("User not found.")
                tags = reader.read_remainder() or None

                if member.id == self.user.id:
                    raise RuntimeError(ERROR_BLURSED)
                if member.bot:
                    raise RuntimeError(ERROR_CURSED)

                return await self.cmd_swap_open(message, member, tags)

            elif arg in ["close", "off"]:
                proxid = reader.read_quote().lower()
                if proxid == "":
                    raise RuntimeError("Please provide a swap ID.")
                proxy = self.fetchone(
                        "select * from proxies "
                        "where (userid, proxid) = (?, ?)",
                        (authid, proxid))
                if not proxy:
                    raise RuntimeError(
                            "You do not have a swap with that ID.")

                return await self.cmd_swap_close(message, proxy)



        elif CMD_DEBUG and arg == "debug":
            return await self.cmd_debug(message)

