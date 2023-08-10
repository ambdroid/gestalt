from collections import ChainMap, namedtuple
from functools import reduce
import json
import re

import discord

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

typecheck = lambda ret, *defs : lambda args : (
        len(defs) == len(args)
        and not any((a != b for a, b in zip(args, defs)))) and ret
types = {
    'and': typecheck(bool, bool, bool),
    'or': typecheck(bool, bool, bool),
    'not': typecheck(bool, bool),
    'add': typecheck(int, int, int),
    'sub': typecheck(int, int, int),
    'mul': typecheck(int, int, int),
    'div': typecheck(int, int, int),
    'eq': lambda args : len(args) == 2 and args[0] == args[1] and bool,
    'neq': typecheck(bool, int, int),
    'lt': typecheck(bool, int, int),
    'gt': typecheck(bool, int, int),
    'lte': typecheck(bool, int, int),
    'gte': typecheck(bool, int, int),
    'if': lambda args : (len(args) == 3 and args[0] == bool
        and args[1] == args[2]) and args[1],
    'one': lambda args : len(args) == 0 and int,
    'int': lambda args : len(args) == 1 and args[0],
    'ask': lambda args : len(args) == 1 and args[0] == bool and bool,
    'ans': lambda args : len(args) == 0 and bool,
    }

def check(ast, context = {}):
    if type(ast) == Exp:
        if result := types[ast.op](list(map(check, ast.args))):
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
    if ast.op == 'int':
        (pre, post) = (['int'], [])
    elif ast.op == 'ask':
        (pre, post) = (['ask'], ['popans'])
    else:
        (pre, post) = ([], [ast.op])
    return pre + reduce(
            lambda cur, nxt : cur + comp(nxt, index + len(pre) + len(cur)),
            ast.args,
            []
            ) + post

ProgramState = namedtuple('ProgramState', ['program', 'pc', 'stack', 'context'])
def run(state):
    (_, pc, stack, context) = state
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
        elif op in ('int', 'ask'):
            return ProgramState(state.program, pc + 1, stack, context)
        elif op == 'ans':
            stack.append(context['answer'])
        elif op == 'popans':
            del context['answer']
        else:
            stack.append(op)
        pc += 1
    if len(stack) != 1:
        raise RuntimeError('Program finished with invalid stack')
    return stack[0]

def eval(program):
    exp = parse_full(program)[0]
    check(exp)
    return run(ProgramState(comp(exp, 0), 0, [], {}))

class GestaltVoting:
    def votes_load(self):
        self.votes = {row['msgid']: ProgramState(**json.loads(row['state']))
                for row in self.fetchall('select * from votes')}


    def votes_save(self):
        self.execute('delete from votes')
        self.cur.executemany('insert into votes values (?, ?)',
                ((msgid, json.dumps(state._asdict()))
                    for msgid, state in self.votes.items()))


    async def program_finished(self, channel, result):
        if type(result) == ProgramState:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                custom_id = 'yes',
                style = discord.ButtonStyle.green,
                label = 'Yes'
                ))
            view.add_item(discord.ui.Button(
                custom_id = 'no',
                style = discord.ButtonStyle.red,
                label = 'No'
                ))
            if msg := await self.send_embed(channel, 'Buttons', view):
                self.votes[msg.id] = result
        else:
            await self.send_embed(channel, str(result))


    # the docs discourage using this
    # but it's easier than using the callbacks across reboots
    async def on_interaction(self, interaction):
        if (msgid := interaction.message.id) in self.votes:
            await interaction.response.send_message(
                    embed = discord.Embed(description = 'You voted ' + interaction.data['custom_id']),
                    ephemeral = True)
            (*rest, context) = self.votes[msgid]
            state = ProgramState(*rest, context
                | {'answer': interaction.data['custom_id'] == 'yes'})
            del self.votes[msgid]
            await self.program_finished(interaction.channel, run(state))

