import ast
import collections
import functools
import inspect
import os
import sys

from .common.logging import error
from .session import get_session, new_session
from .lang import (
    type as T,
    variable as V,
    expression as E,
    statement as S,
    block as B,
    macro as M,
)
from .lang.common.cls import Object


class ContextStack:
    annotation_map = {
        "int": T.Int,
        "long": T.Long,
        "float": T.Float,
        "double": T.Double,
        "str": T.String,
    }

    def __init__(self, globals={}):
        globals = globals.copy()
        globals.update(self.annotation_map)
        self.stack = [(globals, {})]

    def push(self, env=None):
        if env is None:
            env = {}
        self.stack.append((env, {}))

    def pop(self):
        self.stack.pop()

    def __getitem__(self, key):
        return self._find(key, self.stack)[0]

    def __setitem__(self, key, value):
        try:
            _, env = self._find(key, self.stack)
        except KeyError:
            env = self.stack[-1][0]
        env[key] = value

    def _find(self, key, stack):
        if not stack:
            raise KeyError()
        env, cache = stack[-1]
        if key not in cache:
            if key in env:
                cache[key] = (env[key], env)
            else:
                cache[key] = self._find(key, stack[:-1])
        return cache[key]

    def __enter__(self):
        self.push()

    def __exit__(self, *args):
        self.pop()


class BaseTranslator:
    def __init__(self, ctx={}, session=None):
        self.ctx = ContextStack(ctx)
        self.sess = session
        self.source = None
        self.err_handled = False

    def translate(self, source):
        lines = source.split("\n")
        indents = min(len(line) - len(line.lstrip()) for line in lines if line.lstrip())
        self.source = "\n".join(line[indents:] for line in lines)
        self.sess = self.sess or new_session()
        self.err_handled = False

        node = ast.parse(self.source)
        with self.sess:
            return self._run_node(node)

    def _run_node(self, node):
        typename = type(node).__name__
        fn = getattr(self, typename)
        try:
            return fn(node)
        except Exception:
            if not self.err_handled and hasattr(node, 'lineno'):
                line = self.source.split('\n')[node.lineno]
                src = f"{node.lineno} {line}"
                error(src, file=sys.stderr)
                error(" " * (len(f"{node.lineno} ") + node.col_offset) + "^", file=sys.stderr)
                self.err_handled = True
            raise

    def _run_nodes(self, nodes, env=None, block=None):
        block = block or B.EmptyBlock()
        with block:
            self.ctx.push(env)
            for node in nodes:
                res = self._run_node(node)
                if isinstance(res, B.Block):
                    block.add_statement(S.BlockStatement(res))
                elif isinstance(res, S.Statement):
                    block.add_statement(res)
                elif isinstance(res, list):
                    for s in res:
                        block.add_statement(s)
            self.ctx.pop()
        return block

    @staticmethod
    def _try_get_doc(node):
        if isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Str):
            doc = node.body[0].value.s
            body = node.body[1:]
        else:
            doc = ""
            body = node.body
        return doc, body

    # ============= blocks =============
    def Module(self, node):
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.ClassDef)):
                name = child.name
                self.ctx[name] = V.Name(name)
        return self._run_nodes(node.body)

    def ClassDef(self, node):
        # raise NotImplementedError("class")
        members = self._resolve_members(node)
        _cls, _self = self._create_objects(node.name, members)
        block = B.Class(node.name, members)
        with block:
            self.ctx.push()
            blk = private_block = B.AccessBlock("private")
            with private_block:
                for member in members.values():
                    if member['private'] and member['type'] == "property":
                        declaration = self._run_node(member['node'])
                        if member['static']:
                            declaration.qualifiers.append("static")
                        blk.add_statement(declaration)
                for member in members.values():
                    if member['private'] and member['type'] == "method":
                        for stmt in self._run_nodes([member['node']], env={"cls": _cls}).statements:
                            blk.add_statement(stmt)
            blk = public_block = B.AccessBlock("public")
            with public_block:
                for member in members.values():
                    if not member['private'] and member['type'] == "property":
                        declaration = self._run_node(member['node'])
                        if member['static']:
                            declaration.qualifiers.append("static")
                        blk.add_statement(declaration)
                for member in members.values():
                    if not member['private'] and member['type'] == "method":
                        for stmt in self._run_nodes([member['node']], env={"cls": _cls}).statements:
                            blk.add_statement(stmt)
            self.ctx.pop()
            block.add_statement(S.BlockStatement(private_block))
            block.add_statement(S.BlockStatement(public_block))
        return block

    def FunctionDef(self, node):
        assert isinstance(node, ast.FunctionDef)
        static = bool({"staticmethod", "classmethod"} & set(node.decorator_list))
        name = node.name
        args = [self._run_node(arg) for arg in node.args.args if arg.arg not in ["self", "cls"]]
        inputs = [(v.type, v.name) for v in args]
        returns = self._run_node(node.returns) if node.returns is not None else T.Void

        new_env = {v.name: v for v in args}
        doc, body = self._try_get_doc(node)
        if name == "__init__":
            assert not static
            # initialization_list = self._resolve_initialization_list(node)
            initialization_list = []
            block = B.Constructor(name, inputs, returns, None, initialization_list=initialization_list, static=False, doc=doc)
        else:
            block = B.Function(name, inputs, returns, None, static=static, doc=doc)
        block = self._run_nodes(body, new_env, block)
        return block

    def If(self, node):
        condition = self._run_node(node.test)
        block = self._run_nodes(node.body, block=B.If(condition, None))

        orelse = node.orelse
        if not orelse:
            return block

        else_block = self._run_nodes(orelse, block=B.Else(None))
        return [S.BlockStatement(block), S.BlockStatement(else_block)]

    def While(self, node):
        condition = self._run_node(node.test)
        block = self._run_nodes(node.body, block=B.While(condition, None))
        return block

    def For(self, node):
        if node.iter.func.id != "range":
            raise SyntaxError("Only support for-range")
        args = [self._run_node(x) for x in node.iter.args]
        if len(args) == 1:
            start, stop, step = 0, args[0], 1
        elif len(args) == 2:
            start, stop, step = args[0], args[1], 1
        else:
            start, stop, step = args
        try:
            target = self._run_node(node.target)
            declare = False
            env = {}
        except NameError:
            if isinstance(node.target, ast.Name):
                type = self._determine_type(start, stop)
                target = V.Variable(node.target.id, type)
                declare = True
                env = {node.target.id: target}
            else:
                raise
        return self._run_nodes(node.body, env, block=B.For(target, start, stop, step, None, declare))

    @staticmethod
    def _determine_type(start, end):
        int_limit = 1 << 31
        if isinstance(start, ast.Num):
            start = start.n
        if isinstance(end, ast.Num):
            end = end.n
        if (
                (isinstance(start, V.Variable) and start.type is T.Long) or
                (isinstance(end, V.Variable) and end.type is T.Long) or
                (isinstance(start, int) and not -int_limit < start < int_limit) or
                (isinstance(end, int) and not -int_limit < end < int_limit)):
            return T.Long
        else:
            return T.Int

    # ============= statements =============
    def Import(self, node):
        import importlib
        for target in node.names:
            name = target.name
            alias = target.asname or name
            module = importlib.import_module(name)
            self.ctx[alias] = module

    def ImportFrom(self, node):
        import importlib
        module = importlib.import_module(node.module)
        for target in node.names:
            if target.name == "*":
                for name, obj in inspect.getmembers(module):
                    self.ctx[name] = obj
            else:
                self.ctx[target.asname or target.name] = getattr(module, target.name)

    def Pass(self, node):
        return S.SingleLineComment("pass")

    def Return(self, node):
        value = self._run_node(node.value)
        return S.ReturnValue(value)

    def Assign(self, node):
        target = self._run_node(node.targets[0])
        value = self._run_node(node.value)
        return S.Assign(target, value)

    def AugAssign(self, node):
        op_map = {
            ast.Add: S.InplaceAdd,
            ast.Sub: S.InplaceSubtract,
            ast.Mult: S.InplaceMultiply,
            ast.Div: S.InplaceDivide,
        }
        target = self._run_node(node.target)
        op = type(node.op)
        value = self._run_node(node.value)
        return op_map[op](target, value)

    def AnnAssign(self, node):
        if isinstance(node.target, ast.Attribute):
            varname = node.target.attr
        else:
            varname = node.target.id
        value = self._run_node(node.value) if node.value is not None else None
        type = self._run_node(node.annotation)
        if isinstance(type, E.Const) and type.value.lower() == "const":
            target = value
            ret = S.SingleLineComment(f"const {varname} = {value.value}")
        else:
            target = V.variable(varname, type)
            ret = S.VariableDeclaration(target, value)
        self.ctx[varname] = target
        return ret

    def Expr(self, node):
        expr = self._run_node(node)
        return S.ExpressionStatement(expr)

    def Break(self, node):
        return S.Break()

    def Continue(self, node):
        return S.Continue()

    # ============= expressions =============
    def Name(self, node):
        ctx = self.ctx
        name = node.id
        try:
            return ctx[name]
        except KeyError as e:
            raise NameError(f"Can't find name `{name}`") from e

    def Constant(self, node):
        return E.Const(node.value)

    def NameConstant(self, node):
        return E.Const(node.value)

    def Num(self, node):
        return E.Const(node.n)

    def Str(self, node):
        return E.Const(node.s)

    def Compare(self, node):
        op_mapping = {
            ast.Eq: E.CompareEQ,
            ast.NotEq: E.CompareNE,
            ast.Gt: E.CompareGT,
            ast.GtE: E.CompareGE,
            ast.Lt: E.CompareLT,
            ast.LtE: E.CompareLE,
        }
        op = op_mapping[type(node.ops[0])]
        target = self._run_node(node.left)
        value = self._run_node(node.comparators[0])
        return op(target, value)

    def Expression(self, node):
        return self._run_node(node.value)

    def Subscript(self, node):
        obj = self._run_node(node.value)
        index = self._run_node(node.slice)
        return obj[index]

    def Index(self, node):
        return self._run_node(node.value)

    def Slice(self, node):
        return slice(node.lower, node.upper, node.step)

    def ExtSlice(self, node):
        return tuple(map(self._run_node, node.dims))

    def Attribute(self, node):
        obj = self._run_node(node.value)
        return getattr(obj, node.attr)

    def UnaryOp(self, node):
        op_map = {
            ast.USub: E.UnaryNegative,
        }
        op = op_map[type(node.op)]
        return op(self._run_node(node.operand))

    def BinOp(self, node):
        op_map = {
            ast.Add: E.BinaryAdd,
            ast.Sub: E.BinarySubtract,
            ast.Mult: E.BinaryMultiply,
            ast.Div: E.BinaryDivide,
            ast.Mod: E.BinaryModulo,
            ast.LShift: E.BinaryLShift,
            ast.RShift: E.BinaryRShift,
            ast.And: E.LogicalAnd,
            ast.Or: E.LogicalOr,
            ast.BitXor: E.BinaryXor,
            ast.BitAnd: E.BinaryAnd,
            ast.BitOr: E.BinaryOr,
        }
        op = op_map[type(node.op)]
        left = self._run_node(node.left)
        right = self._run_node(node.right)
        return op(left, right)

    def Call(self, node):
        func = self._run_node(node.func)
        args = tuple(self._run_node(x) for x in node.args)
        return E.CallFunction(func, args)

    def Tuple(self, node):
        return tuple(map(self._run_node, node.elts))

    def List(self, node):
        return list(map(self._run_node, node.elts))

    def IfExp(self, node):
        return E.IIf(
            self._run_node(node.test),
            self._run_node(node.body),
            self._run_node(node.orelse),
        )

    # ============= others =============
    def arg(self, node):
        return V.variable(node.arg, self._run_node(node.annotation))

    # functions

    def _create_objects(self, name, members):
        _cls = Object(name, {key: value for key, value in members.items() if value['static']})
        _self = Object(name, members)
        return _cls, _self

    def _resolve_members(self, node):
        # TODO: operator override
        # TODO: desctructor
        # TODO: overload
        members = {}
        for child in node.body:
            if isinstance(child, ast.FunctionDef):
                if child.name == "__init__":
                    members.update(self._resolve_self_properties(child))
                else:
                    members[child.name] = {
                        "name": child.name,
                        "type": "method",
                        "static": "classmethod" in child.decorator_list or "staticmethod" in child.decorator_list,
                        "private": child.name.startswith("__") and not child.name.endswith("__"),
                        "node": child,
                    }
            elif isinstance(child, ast.AnnAssign):
                members[child.target.id] = {
                    "name": child.target.id,
                    "type": "property",
                    "static": True,
                    "private": child.target.id.startswith("__"),
                    "node": child,
                    "annotation": child.annotation,
                    "value": self._run_node(child.value)
                }
            else:
                raise TypeError(f"Wrong type of member of class: {child}")
        return members

    def _resolve_self_properties(self, node):
        members = {}
        for child in node.body:
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Attribute) and child.target.value.id == "self":
                name = child.target.attr
                members[name] = {
                    "name": name,
                    "type": "property",
                    "private": name.startswith("__"),
                    "static": False,
                    "value": self._run_node(child.value),
                    "node": ast.AnnAssign(target=ast.Name(id=name), value=child.value, annotation=child.annotation),
                }
        print(members)
        return members
