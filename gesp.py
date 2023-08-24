from functools import reduce, partial, cached_property
from collections import ChainMap, namedtuple
from typing import Union
import dataclasses as dc
import json
import math
import time
import re

import discord

from defs import *

ParseState = namedtuple('ParseState', ['pairs', 'stack', 'pos'])
def get_pairs(code):
    if (count := code.count('(')) != code.count(')'):
        raise ValueError('Non-matching parens')
    try:
        pairs = dict(reduce(
                lambda cur, nxt : (
                    ParseState(ChainMap({cur.stack[0]:cur.pos}, cur.pairs),
                        cur.stack[1], cur.pos + 1)
                    if nxt == ')'
                    else (
                        ParseState(cur.pairs, (cur.pos, cur.stack), cur.pos + 1)
                        if nxt == '('
                        else ParseState(cur.pairs, cur.stack, cur.pos + 1)
                        )
                    ),
                code,
                ParseState({}, (), 0)).pairs)
    except IndexError:
        pairs = {}
    if len(pairs) != count:
        raise ValueError('Non-matching parens')
    return pairs

def parse_args(args, pairs, offset):
    if not args:
        return ()
    if args[0] == '(':
        end = pairs[offset] + 1 - offset
        parsed = parse_paren(args[:end], pairs, offset)
    elif m := re.match('"([^"]+)"', args):
        (parsed, end) = (m[1], len(m[0]))
    elif m := re.match('[0-9]+', args):
        (parsed, end) = (int(m[0]), len(m[0]))
    elif m := re.match('true|false', args):
        (parsed, end) = (m[0] == 'true', len(m[0]))
    else:
        raise ValueError('Syntax error')
    stripped = args[end:].strip()
    return (parsed,) + parse_args(stripped, pairs,
            offset + len(args) - len(stripped))

Exp = namedtuple('Exp', ['op', 'args'])

def parse_paren(paren, pairs, offset):
    if m := re.fullmatch('\(([a-z-]+)(\s*)(.*)\)', paren):
        start = 1 + len(m[1]) + len(m[2])
        return Exp(m[1], parse_args(m[3], pairs, offset + start))

def parse_full(expr):
    return parse_args(expr, get_pairs(expr), 0)

user = object()
typecheck = lambda ret, *defs : lambda args : defs == args and ret
types = {
    'and': typecheck(bool, bool, bool),
    'or': typecheck(bool, bool, bool),
    'not': typecheck(bool, bool),
    'add': typecheck(int, int, int),
    'sub': typecheck(int, int, int),
    'mul': typecheck(int, int, int),
    'div': typecheck(int, int, int),
    'floor': typecheck(int, int), # obviously not strictly true but good enough
    'eq': lambda args : len(args) == 2 and args[0] == args[1] and bool,
    'neq': typecheck(bool, int, int),
    'lt': typecheck(bool, int, int),
    'gt': typecheck(bool, int, int),
    'lte': typecheck(bool, int, int),
    'gte': typecheck(bool, int, int),
    'if': lambda args : (len(args) == 3 and args[0] == bool
        and args[1] == args[2]) and args[1],
    'one': typecheck(int),
    'answer': typecheck(bool),
    'initiator': typecheck(user),
    'named': typecheck(user, int),
    'members': typecheck(set),
    'size-of': typecheck(int, set),
    'vote-approval': typecheck(bool, int),
    }

def check(ast, context = {}):
    if type(ast) == Exp:
        if result := types[ast.op](tuple(map(check, ast.args))):
            return result
        raise TypeError('Type check failed')
    return type(ast)

def comp(ast, index):
    if type(ast) in [int, str, bool]:
        return [ast]
    if ast.op in ('and', 'or'):
        a = comp(ast.args[0], index)
        b = comp(ast.args[1], index + len(a) + 2)
        jmp = [index + len(a) + len(b) + 2 - 1, ast.op]
        return a + jmp + b
    if ast.op == 'if':
        a = comp(ast.args[0], index)
        b = comp(ast.args[1], index + len(a) + 2)
        c = comp(ast.args[2], index + len(a) + 2 + len(b) + 2)
        jmp = [index + len(a) + 2 + len(b) + 2 - 1, 'jf']
        jmp2 = [index + len(a) + 2 + len(b) + len(jmp) + len(c) - 1, 'jp']
        return a + jmp + b + jmp2 + c
    elif ast.op == 'vote-approval':
        post = ['vote-approval', 'answer']
    else:
        post = [ast.op]
    return reduce(
            lambda cur, nxt : cur + comp(nxt, index + len(cur)),
            ast.args,
            []
            ) + post

ProgramState = namedtuple('ProgramState', ['program', 'pc', 'stack'])

def run(state, context = None):
    (_, pc, stack) = state
    while pc < len(state.program):
        op = state.program[pc]
        if op == 'jp':
            pc = stack.pop()
        elif op == 'jf':
            dest = stack.pop()
            if not stack.pop():
                pc = dest
        elif op == 'or':
            dest = stack.pop()
            if stack.pop():
                stack.append(True)
                pc = dest
        elif op == 'and':
            dest = stack.pop()
            if not stack.pop():
                stack.append(False)
                pc = dest
        elif op == 'not':
            stack.append(not stack.pop())
        elif op == 'add':
            stack.append(stack.pop() + stack.pop())
        elif op == 'sub':
            stack.append(-(stack.pop() - stack.pop()))
        elif op == 'mul':
            stack.append(stack.pop() * stack.pop())
        elif op == 'div':
            a = stack.pop()
            stack.append(stack.pop() / a)
        elif op == 'floor':
            stack.append(math.floor(stack.pop()))
        elif op == 'eq':
            stack.append(stack.pop() == stack.pop())
        elif op == 'neq':
            stack.append(stack.pop() != stack.pop())
        elif op == 'lt':
            stack.append(stack.pop() > stack.pop())
        elif op == 'gt':
            stack.append(stack.pop() < stack.pop())
        elif op == 'lte':
            stack.append(stack.pop() >= stack.pop())
        elif op == 'gte':
            stack.append(stack.pop() <= stack.pop())
        elif op == 'one':
            stack.append(1)
        elif op == 'answer':
            stack.append(context.answer)
        elif op == 'initiator':
            stack.append(context.initiator)
        elif op == 'named':
            stack.append(context.named[stack.pop()])
        elif op == 'members':
            stack.append(context.members)
        elif op == 'size-of':
            stack.append(len(stack.pop()))
        elif op == 'vote-approval':
            return partial(VoteApproval,
                    eligible = stack.pop(),
                    state = ProgramState(state.program, pc + 1, stack),
                    context = context,
                    )
        else:
            stack.append(op)
        pc += 1
    if len(stack) != 1:
        raise RuntimeError('Program finished with invalid stack')
    return stack[0]

def eval(program):
    exp = parse_full(program)[0]
    check(exp)
    return run(ProgramState(comp(exp, 0), 0, []))


# this comes up a lot
def excluding(_dict, key):
    return {k: v for k, v in _dict.items() if k != key}


# there is probably a better way to do this
# but i can't get __init_subclass__ to work otherwise
def serializable(name, parents, attrs):
    class Inner:
        _type = None
        table = {}
        def get_type(self):
            return self._type
        @classmethod
        def class_dict(cls, _dict):
            return cls(**_dict)
        @classmethod
        def from_dict(cls, _dict):
            return cls.table[_dict['_type']].class_dict(
                    excluding(_dict, '_type'))
        @classmethod
        def from_json(cls, js):
            return cls.from_dict(json.loads(js))
        def to_dict(self):
            # dc.asdict() converts child dc's to dicts too, unwanted
            # (that wouldn't include the type)
            return vars(self) | {'_type': self._type}
        def to_json(self):
            return json.dumps(self.to_dict(),
                    default =
                        lambda val : val.to_dict() if dc.is_dataclass(val)
                        else list(val))
    def init(cls, _type):
        cls._type = _type.value
        super(cls, cls).table[_type.value] = cls
    return type(name, (Inner,), attrs | {'__init_subclass__' : init})


@dc.dataclass
class VotableAction(metaclass = serializable):
    mask: str
    def execute(self, bot):
        raise NotImplementedError()


@dc.dataclass
class Rules(metaclass = serializable):
    named: list[int] = dc.field(default_factory = lambda : [])
    def for_action(self, atype):
        raise NotImplementedError()
    @cached_property
    def compiled(self):
        return {atype: comp(self.for_action(atype), 0) for atype in ActionType}
    def to_dict(self):
        return excluding(super().to_dict(), 'compiled')


@dc.dataclass
class ActionJoin(VotableAction, _type = ActionType.join):
    candidate: int
    def execute(self, bot):
        nick = bot.fetchone('select nick from masks where maskid = ?',
                (self.mask,))
        if nick:
            bot.mkproxy(self.candidate, ProxyType.mask, cmdname = nick[0],
                    maskid = self.mask)


class ActionInvite(ActionJoin, _type = ActionType.invite):
    pass


@dc.dataclass
class ActionRemove(VotableAction, _type = ActionType.remove):
    candidate: int
    def execute(self, bot):
        bot.execute('delete from proxies '
                'where (userid, maskid, type) = (?, ?, ?)',
                (self.candidate, self.mask, ProxyType.mask))


@dc.dataclass
class ActionServer(VotableAction, _type = ActionType.server):
    server: int
    def execute(self, bot):
        if bot.get_guild(self.server):
            bot.execute('insert or ignore into guildmasks values'
                    '(?, ?, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL)',
                    (self.mask, self.server, ProxyType.mask, int(time.time())))


@dc.dataclass
class ActionChange(VotableAction, _type = ActionType.change):
    which: str
    value: str
    def __post_init__(self):
        if self.which not in ('nick', 'avatar', 'color'):
            raise ValueError(self.which)
    def execute(self, bot):
        bot.execute('update masks set %s = ? where maskid = ?' % self.which,
                (self.value, self.mask))


@dc.dataclass
class ActionRules(VotableAction, _type = ActionType.rules):
    newrules: Rules
    @classmethod
    def class_dict(cls, _dict):
        return super().class_dict(_dict | {
            'newrules': Rules.from_dict(_dict['newrules'])})
    def execute(self, bot):
        bot.execute('update masks set rules = ? where maskid = ?',
                (self.newrules.to_json(), self.mask))
        bot.rules[self.mask] = self.newrules


@dc.dataclass
class RulesDictator(Rules, _type = RuleType.dictator):
    rule = parse_full(
            '(eq'
                '(initiator)'
                '(named 0)'
            ')'
            )[0]
    user: dc.InitVar[int] = None
    def __post_init__(self, user = None):
        if user:
            self.named = [user]
    def for_action(self, atype):
        return self.rule


class RulesHandsOff(RulesDictator, _type = RuleType.handsoff):
    rule_voting = parse_full(
            '(vote-approval'
                '(add'
                    '(floor'
                        '(div'
                            '(sub'
                                '(size-of'
                                    '(members)'
                                ')'
                            '1)'
                        '2)'
                    ')'
                '1)'
            ')'
            )[0]
    def for_action(self, atype):
        return (self.rule
                if atype == ActionType.rules
                else Exp('or', (self.rule, self.rule_voting)))


class RulesMajority(Rules, _type = RuleType.majority):
    rule = parse_full(
            '(vote-approval'
                '(add'
                    '(floor'
                        '(div'
                            '(size-of'
                                '(members)'
                            ')'
                        '2)'
                    ')'
                '1)'
            ')'
            )[0]
    def for_action(self, atype):
        return self.rule


class RulesUnanimous(Rules, _type = RuleType.unanimous):
    rule = parse_full(
            '(vote-approval'
                '(size-of'
                    '(members)'
                ')'
            ')'
            )[0]
    rule_remove = parse_full(
            '(vote-approval'
                '(sub'
                    '(size-of'
                        '(members)'
                    ')'
                '1)'
            ')'
            )[0]
    def for_action(self, atype):
        return self.rule_remove if atype == ActionType.remove else self.rule


@dc.dataclass
class ProgramContext:
    initiator: int
    channel: int
    named: list[int]
    members: frozenset[int]
    candidate: int = None
    answer: bool = None
    def from_dict(_dict):
        return ProgramContext(
                **(_dict | {'members': frozenset(_dict['members'])}))
    def to_dict(self):
        return vars(self)


@dc.dataclass
class Vote(metaclass = serializable):
    action: VotableAction
    state: ProgramState
    # why is ProgramContext stored in the Vote, you might ask
    # (and by you i mean me, because i kept confusing myself about this)
    # well, the context is passed around in function calls
    # because it doesn't cleanly fit in VotableAction or ProgramState
    # but when it's time for a Vote, the context needs to be at rest
    context: ProgramContext
    eligible: Union[frozenset[int], int]
    yes: set[int] = dc.field(default_factory = lambda : set())
    no: set[int] = dc.field(default_factory = lambda : set())
    async def on_interaction(self, interaction):
        if (userid := interaction.user.id) in (
                self.context.members
                if isinstance(self.eligible, int)
                else self.eligible):
            button = interaction.data['custom_id']
            if button == 'abstain':
                if userid in self.yes:
                    self.yes.remove(userid)
                    desc = 'You removed your yes vote.'
                elif userid in self.no:
                    self.no.remove(userid)
                    desc = 'You removed your no vote.'
                else:
                    desc = 'You were already abstaining.'
            else:
                which = self.yes if button == 'yes' else self.no
                if userid in which:
                    desc = 'You were already voting %s.' % button
                else:
                    which.add(userid)
                    desc = 'You voted %s.' % button
        else:
            desc = 'You are not eligible for this vote.'
        await interaction.response.send_message(
                embed = discord.Embed(description = desc),
                ephemeral = True)
        # TODO update message w/tally
        return self.maybe_done()
    def maybe_done(self):
        raise NotImplementedError()
    def view(self, disabled = False):
        raise NotImplementedError()
    @classmethod
    def from_dict(cls, _dict):
        return super().from_dict(_dict | {
            'action': VotableAction.from_dict(_dict['action']),
            'state': ProgramState(*_dict['state']),
            'context': ProgramContext.from_dict(_dict['context']),
            'yes': set(_dict['yes']),
            'no': set(_dict['no']),
            } | ({'eligible': frozenset(_dict['eligible'])}
                if isinstance(_dict['eligible'], list) else {}))


@dc.dataclass
class VoteConfirm(Vote, _type = VoteType.confirm):
    user: dc.InitVar[int] = None
    def __post_init__(self, user = None):
        if user:
            self.eligible = frozenset([user])
    def maybe_done(self):
        if self.yes or self.no:
            self.context.answer = bool(self.yes)
            return True
    def view(self, disabled = False):
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            custom_id = 'yes',
            style = discord.ButtonStyle.green,
            label = 'Yes',
            disabled = disabled,
            ))
        view.add_item(discord.ui.Button(
            custom_id = 'no',
            style = discord.ButtonStyle.red,
            label = 'No',
            disabled = disabled,
            ))
        return view


class VoteApproval(Vote, _type = VoteType.approval):
    def maybe_done(self):
        if len(self.yes) == self.eligible:
            # no other possibility except vote simply expiring
            # however, this might change so that expiry = False
            # therefore still pass it through context for forward compatibility
            self.context.answer = True
            return True
    def view(self, disabled = False):
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            custom_id = 'yes',
            style = discord.ButtonStyle.green,
            label = 'Yes',
            disabled = disabled,
            ))
        view.add_item(discord.ui.Button(
            custom_id = 'abstain',
            style = discord.ButtonStyle.grey,
            label = 'Abstain',
            disabled = disabled,
            ))
        return view


class VoteConsensus(Vote, _type = VoteType.consensus):
    def maybe_done(self):
        if (len(self.yes) + len(self.no) == self.eligible
                if isinstance(self.eligible, int)
                else self.yes | self.no == self.eligible):
            raise NotImplementedError()
    def view(self, disabled = False):
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            custom_id = 'yes',
            style = discord.ButtonStyle.green,
            label = 'Yes',
            disabled = disabled,
            ))
        view.add_item(discord.ui.Button(
            custom_id = 'no',
            style = discord.ButtonStyle.red,
            label = 'No',
            disabled = disabled,
            ))
        if isinstance(self.eligible, int):
            view.add_item(discord.ui.Button(
                custom_id = 'abstain',
                style = discord.ButtonStyle.grey,
                label = 'Abstain',
                disabled = disabled,
                ))
        return view


class GestaltVoting:
    def load(self):
        self.votes = {row['msgid']: Vote.from_json(row['state'])
                for row in self.fetchall('select * from votes')}
        # if rules got saved then they've passed all checks already
        # so no need to worry about exceptions
        self.rules = {row['maskid']: Rules.from_json(row['rules'])
                for row in self.fetchall('select maskid, rules from masks')}


    def save(self):
        self.execute('delete from votes')
        self.cur.executemany('insert into votes values (?, ?)',
                ((msgid, vote.to_json()) for msgid, vote in self.votes.items()))


    async def initiate_action(self, message, action):
        rule = self.rules[action.mask]
        context = ProgramContext(
                initiator = message.author.id,
                channel = message.channel.id,
                named = rule.named,
                members = {
                    row[0] for row in
                    self.fetchall(
                        # TODO index shenanigans
                        'select userid from proxies where maskid = ?',
                        (action.mask,))
                    }
                )
        await self.step_program(
                ProgramState(rule.compiled[action.get_type()], 0, []),
                context, action)


    async def step_program(self, program, context, action):
        result = run(program, context)
        if isinstance(result, partial):
            vote = result(action)
            if msg := await self.send_embed(
                    self.get_channel(context.channel), 'Vote',
                    vote.view()):
                self.votes[msg.id] = vote
        elif result == True:
            action.execute(self)


    # the docs discourage using this
    # but it's easier than using the callbacks across reboots
    async def on_interaction(self, interaction):
        if (msgid := interaction.message.id) in self.votes:
            if await self.votes[msgid].on_interaction(interaction):
                vote = self.votes[msgid]
                del self.votes[msgid]
                await self.step_program(vote.state, vote.context, vote.action)

