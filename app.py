import os
import re
import subprocess
import threading
import tempfile

from flask import Flask, request, jsonify, render_template

try:
    from discord_runtime import DiscordBot, DiscordError
    PS_DISCORD_AVAILABLE = True
except ImportError:
    try:
        # これまでの ps_discord.py というファイル名も利用できる。
        from ps_discord import DiscordBot, DiscordError
        PS_DISCORD_AVAILABLE = True
    except ImportError:
        DiscordBot = None
        DiscordError = Exception
        PS_DISCORD_AVAILABLE = False


app = Flask(__name__)

discord_bot = None
discord_bot_lock = threading.Lock()


def get_discord_bot():
    global discord_bot
    with discord_bot_lock:
        return discord_bot


def set_discord_bot(bot):
    global discord_bot
    with discord_bot_lock:
        discord_bot = bot


@app.route("/")
def index():
    return render_template("index.html")


# ====================================================
# Lexer
# ====================================================

def tokenize(source_code):
    token_specification = [
        ("COMMENT", r"#.*"),
        ("STRING", r'"[^"\\]*(?:\\.[^"\\]*)*"'),
        ("NUMBER", r"\d+(?:\.\d+)?"),
        ("KEYWORDS", r"\b(if|elif|else|while|for|in|break|continue|func|return)\b"),
        ("LOGIC", r"\b(and|or|not)\b"),
        ("ID", r"[a-zA-Z_]\w*"),
        ("OP", r"==|!=|<=|>=|[-+*/%=<>]"),
        ("LPAREN", r"\("),
        ("RPAREN", r"\)"),
        ("LBRACK", r"\["),
        ("RBRACK", r"\]"),
        ("COMMA", r","),
        ("NL", r"\n"),
        ("SKIP", r"[ \t]+"),
        ("MISMATCH", r"."),
    ]

    tok_regex = "|".join(
        f"(?P<{name}>{regex})"
        for name, regex in token_specification
    )

    tokens = []

    for line_num, line in enumerate(source_code.split("\n"), 1):
        if not line.strip() or line.strip().startswith("#"):
            continue

        indent_match = re.match(r"^(\s*)", line)
        indent_len = len(indent_match.group(1)) if indent_match else 0

        if indent_len % 4 != 0:
            raise Exception(
                f"インデントエラー (行 {line_num}): "
                "半角スペース4つ単位で字下げしてください。"
            )

        indent_level = indent_len // 4

        for mo in re.finditer(tok_regex, line):
            kind = mo.lastgroup
            value = mo.group()

            if kind in ("COMMENT", "SKIP"):
                continue

            if kind == "MISMATCH":
                raise Exception(
                    f"構文エラー (行 {line_num}): "
                    f"不正な文字 '{value}' があります。"
                )

            tokens.append({
                "type": kind,
                "value": value,
                "line": line_num,
                "indent": indent_level
            })

        tokens.append({
            "type": "NL",
            "value": "\n",
            "line": line_num,
            "indent": indent_level
        })

    return tokens


# ====================================================
# Expression Parser
# ====================================================

class ExpressionParser:

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        if (
            self.pos < len(self.tokens)
            and self.tokens[self.pos]["type"] != "NL"
        ):
            return self.tokens[self.pos]
        return None

    def consume(self, expected_type=None):
        tok = self.peek()

        if tok and (
            expected_type is None
            or tok["type"] == expected_type
        ):
            self.pos += 1
            return tok

        return None

    def parse(self):
        return self.parse_or()

    def parse_or(self):
        node = self.parse_and()

        while (
            self.peek()
            and self.peek()["type"] == "LOGIC"
            and self.peek()["value"] == "or"
        ):
            self.consume()
            right = self.parse_and()

            node = {
                "type": "BinaryExpr",
                "op": "||",
                "left": node,
                "right": right
            }

        return node

    def parse_and(self):
        node = self.parse_equality()

        while (
            self.peek()
            and self.peek()["type"] == "LOGIC"
            and self.peek()["value"] == "and"
        ):
            self.consume()
            right = self.parse_equality()

            node = {
                "type": "BinaryExpr",
                "op": "&&",
                "left": node,
                "right": right
            }

        return node

    def parse_equality(self):
        node = self.parse_expr()

        while (
            self.peek()
            and self.peek()["type"] == "OP"
            and self.peek()["value"]
            in ["==", "!=", "<", ">", "<=", ">="]
        ):
            op = self.consume()["value"]
            right = self.parse_expr()

            node = {
                "type": "BinaryExpr",
                "op": op,
                "left": node,
                "right": right
            }

        return node

    def parse_expr(self):
        node = self.parse_term()

        while (
            self.peek()
            and self.peek()["type"] == "OP"
            and self.peek()["value"] in ["+", "-"]
        ):
            op = self.consume()["value"]
            right = self.parse_term()

            node = {
                "type": "BinaryExpr",
                "op": op,
                "left": node,
                "right": right
            }

        return node

    def parse_term(self):
        node = self.parse_factor()

        while (
            self.peek()
            and self.peek()["type"] == "OP"
            and self.peek()["value"] in ["*", "/", "%"]
        ):
            op = self.consume()["value"]
            right = self.parse_factor()

            node = {
                "type": "BinaryExpr",
                "op": op,
                "left": node,
                "right": right
            }

        return node

    def parse_factor(self):
        tok = self.peek()

        if not tok:
            return None

        if tok["type"] in ("NUMBER", "STRING"):
            return {
                "type": "Literal",
                "value": self.consume()["value"]
            }

        if tok["type"] == "LBRACK":
            self.consume("LBRACK")
            elements = []

            while (
                self.peek()
                and self.peek()["type"] != "RBRACK"
            ):
                elements.append(self.parse())

                if (
                    self.peek()
                    and self.peek()["type"] == "COMMA"
                ):
                    self.consume("COMMA")

            self.consume("RBRACK")

            return {
                "type": "ArrayLiteral",
                "elements": elements
            }

        if tok["type"] == "ID":
            name = self.consume()["value"]

            if (
                self.peek()
                and self.peek()["type"] == "LPAREN"
            ):
                self.consume("LPAREN")
                args = []

                while (
                    self.peek()
                    and self.peek()["type"] != "RPAREN"
                ):
                    args.append(self.parse())

                    if (
                        self.peek()
                        and self.peek()["type"] == "COMMA"
                    ):
                        self.consume("COMMA")

                self.consume("RPAREN")

                return {
                    "type": "CallExpr",
                    "name": name,
                    "args": args
                }

            if (
                self.peek()
                and self.peek()["type"] == "LBRACK"
            ):
                self.consume("LBRACK")
                index_node = self.parse()
                self.consume("RBRACK")

                return {
                    "type": "IndexExpr",
                    "name": name,
                    "index": index_node
                }

            return {
                "type": "Variable",
                "value": name
            }

        if tok["type"] == "LPAREN":
            self.consume("LPAREN")
            node = self.parse()
            self.consume("RPAREN")
            return node

        if (
            tok["type"] == "LOGIC"
            and tok["value"] == "not"
        ):
            self.consume()

            return {
                "type": "UnaryExpr",
                "op": "!",
                "right": self.parse_factor()
            }

        return None


# ====================================================
# Parser
# ====================================================

def parse(tokens):
    root = {
        "type": "Program",
        "body": []
    }

    if not tokens:
        return root

    stack = [(-1, root)]
    i = 0

    while i < len(tokens):
        token = tokens[i]

        if token["type"] == "NL":
            i += 1
            continue

        while (
            stack
            and token["indent"] <= stack[-1][0]
        ):
            stack.pop()

        parent = stack[-1][1]

        node = {
            "line": token["line"]
        }

        line_tokens = []

        while (
            i < len(tokens)
            and tokens[i]["type"] != "NL"
        ):
            line_tokens.append(tokens[i])
            i += 1

        if i < len(tokens):
            i += 1

        if not line_tokens:
            continue

        first = line_tokens[0]

        if (
            first["type"] == "KEYWORDS"
            and first["value"] == "func"
        ):
            if (
                len(line_tokens) < 2
                or line_tokens[1]["type"] != "ID"
            ):
                raise Exception(
                    f"構文エラー (行 {first['line']}): "
                    "func の後ろに関数名が必要です。"
                )

            func_name = line_tokens[1]["value"]
            params = []

            for tok in line_tokens[2:]:
                if tok["type"] == "ID":
                    params.append(tok["value"])

            node.update({
                "type": "FunctionDef",
                "name": func_name,
                "params": params,
                "body": []
            })

            parent["body"].append(node)
            stack.append((first["indent"], node))
            continue

        if (
            first["type"] == "KEYWORDS"
            and first["value"] in ["if", "elif", "while"]
        ):
            if first["value"] == "if":
                kind = "IfStatement"
            elif first["value"] == "elif":
                kind = "ElifStatement"
            else:
                kind = "WhileStatement"

            expr_parser = ExpressionParser(line_tokens[1:])
            cond_ast = expr_parser.parse()

            node.update({
                "type": kind,
                "condition": cond_ast,
                "body": []
            })

            parent["body"].append(node)
            stack.append((first["indent"], node))
            continue

        if (
            first["type"] == "KEYWORDS"
            and first["value"] == "else"
        ):
            node.update({
                "type": "ElseStatement",
                "body": []
            })

            parent["body"].append(node)
            stack.append((first["indent"], node))
            continue

        if (
            first["type"] == "KEYWORDS"
            and first["value"] == "for"
        ):
            if (
                len(line_tokens) < 4
                or line_tokens[1]["type"] != "ID"
                or line_tokens[2]["value"] != "in"
            ):
                raise Exception(
                    f"構文エラー (行 {first['line']}): "
                    "for 変数 in 範囲 の形式で書いてください。"
                )

            loop_var = line_tokens[1]["value"]

            expr_parser = ExpressionParser(
                line_tokens[3:]
            )

            range_ast = expr_parser.parse()

            node.update({
                "type": "ForStatement",
                "variable": loop_var,
                "range": range_ast,
                "body": []
            })

            parent["body"].append(node)
            stack.append((first["indent"], node))
            continue

        if (
            first["type"] == "KEYWORDS"
            and first["value"] in [
                "break",
                "continue",
                "return"
            ]
        ):
            val = first["value"]

            if val == "return":
                if len(line_tokens) > 1:
                    expr_parser = ExpressionParser(
                        line_tokens[1:]
                    )

                    node.update({
                        "type": "ReturnStatement",
                        "value": expr_parser.parse()
                    })
                else:
                    node.update({
                        "type": "ReturnStatement",
                        "value": None
                    })
            else:
                node.update({
                    "type": val.upper() + "Statement"
                })

            parent["body"].append(node)
            continue

        if (
            first["type"] == "ID"
            and first["value"] == "ps"
        ):
            expr_parser = ExpressionParser(
                line_tokens[1:]
            )

            node.update({
                "type": "PrintStatement",
                "value": expr_parser.parse()
            })

            parent["body"].append(node)
            continue

        if first["type"] == "ID":

            if (
                len(line_tokens) > 1
                and line_tokens[1]["type"] == "LBRACK"
            ):
                idx_end = 1

                while (
                    idx_end < len(line_tokens)
                    and line_tokens[idx_end]["type"]
                    != "RBRACK"
                ):
                    idx_end += 1

                if (
                    idx_end + 1 < len(line_tokens)
                    and line_tokens[idx_end + 1]["value"] == "="
                ):
                    expr_parser = ExpressionParser(
                        line_tokens[2:idx_end]
                    )

                    idx_ast = expr_parser.parse()

                    expr_parser2 = ExpressionParser(
                        line_tokens[idx_end + 2:]
                    )

                    val_ast = expr_parser2.parse()

                    node.update({
                        "type": "ArrayAssignStatement",
                        "name": first["value"],
                        "index": idx_ast,
                        "value": val_ast
                    })

                    parent["body"].append(node)
                    continue

            if (
                len(line_tokens) > 1
                and line_tokens[1]["type"] == "OP"
                and line_tokens[1]["value"] == "="
            ):
                expr_parser = ExpressionParser(
                    line_tokens[2:]
                )

                node.update({
                    "type": "AssignmentExpression",
                    "variable": first["value"],
                    "value": expr_parser.parse()
                })

                parent["body"].append(node)
            else:
                expr_parser = ExpressionParser(
                    line_tokens
                )

                node.update({
                    "type": "ExpressionStatement",
                    "value": expr_parser.parse()
                })

                parent["body"].append(node)

    return root


# ====================================================
# C++ Generator
# ====================================================

def generate_cpp(ast):
    cpp_lines = [
        "#include <iostream>",
        "#include <string>",
        "#include <vector>",
        "#include <algorithm>",
        "#include <type_traits>",
        "using namespace std;",
        "",
        "template<typename T> int len(const vector<T>& v) { return static_cast<int>(v.size()); }",
        "int len(const string& s) { return static_cast<int>(s.length()); }",
        "string input() { string s; getline(cin, s); return s; }",
        "vector<int> range(int stop) { vector<int> r; for (int i = 0; i < stop; ++i) r.push_back(i); return r; }",
        "vector<int> range(int start, int stop) { vector<int> r; for (int i = start; i < stop; ++i) r.push_back(i); return r; }",
        "vector<int> range(int start, int stop, int step) { vector<int> r; if (step == 0) return r; if (step > 0) for (int i = start; i < stop; i += step) r.push_back(i); else for (int i = start; i > stop; i += step) r.push_back(i); return r; }",
        ""
    ]

    functions_buf = []

    def to_cpp_expr(node):
        if not node:
            return ""

        typ = node["type"]

        if typ == "Literal":
            return node["value"]
        if typ == "Variable":
            return node["value"]
        if typ == "UnaryExpr":
            return f"({node['op']}{to_cpp_expr(node['right'])})"
        if typ == "BinaryExpr":
            left_str = to_cpp_expr(node["left"])
            right_str = to_cpp_expr(node["right"])
            if node["op"] == "+" and (left_str.startswith('"') or right_str.startswith('"')):
                if left_str.startswith('"'):
                    left_str = f"string({left_str})"
                if right_str.startswith('"'):
                    right_str = f"string({right_str})"
            return f"({left_str} {node['op']} {right_str})"
        if typ == "ArrayLiteral":
            return "{" + ", ".join(to_cpp_expr(e) for e in node.get("elements", [])) + "}"
        if typ == "IndexExpr":
            return f"{node['name']}[{to_cpp_expr(node['index'])}]"
        if typ == "CallExpr":
            return f"{node['name']}(" + ", ".join(to_cpp_expr(a) for a in node.get("args", [])) + ")"
        return ""

    def has_return(nodes):
        for node in nodes:
            if node.get("type") == "ReturnStatement":
                return True
            if isinstance(node.get("body"), list) and has_return(node["body"]):
                return True
        return False

    def array_type(node):
        elements = node.get("elements", [])
        if not elements:
            return "vector<int>"
        first = elements[0]
        if first.get("type") == "Literal" and first.get("value", "").startswith('"'):
            return "vector<string>"
        if first.get("type") == "Literal" and "." in first.get("value", ""):
            return "vector<double>"
        return "vector<int>"

    def walk(nodes, indent_str="    ", declared_vars=None):
        if declared_vars is None:
            declared_vars = set()
        lines = []

        for node in nodes:
            typ = node["type"]

            if typ == "AssignmentExpression":
                var = node["variable"]
                val_node = node["value"]
                cpp_val = to_cpp_expr(val_node)
                if var in declared_vars:
                    lines.append(f"{indent_str}{var} = {cpp_val};")
                else:
                    if val_node["type"] == "ArrayLiteral":
                        type_str = array_type(val_node)
                    elif val_node["type"] == "Literal" and val_node["value"].startswith('"'):
                        type_str = "string"
                    elif val_node["type"] == "CallExpr" and val_node["name"] == "input":
                        type_str = "string"
                    else:
                        type_str = "auto"
                    lines.append(f"{indent_str}{type_str} {var} = {cpp_val};")
                    declared_vars.add(var)

            elif typ == "ArrayAssignStatement":
                lines.append(f"{indent_str}{node['name']}[{to_cpp_expr(node['index'])}] = {to_cpp_expr(node['value'])};")

            elif typ == "PrintStatement":
                lines.append(f"{indent_str}cout << {to_cpp_expr(node['value'])} << endl;")

            elif typ in ("IfStatement", "ElifStatement"):
                prefix = "if" if typ == "IfStatement" else "else if"
                lines.append(f"{indent_str}{prefix} ({to_cpp_expr(node['condition'])}) {{")
                lines.extend(walk(node["body"], indent_str + "    ", declared_vars))
                lines.append(f"{indent_str}}}")

            elif typ == "ElseStatement":
                lines.append(f"{indent_str}else {{")
                lines.extend(walk(node["body"], indent_str + "    ", declared_vars))
                lines.append(f"{indent_str}}}")

            elif typ == "WhileStatement":
                lines.append(f"{indent_str}while ({to_cpp_expr(node['condition'])}) {{")
                lines.extend(walk(node["body"], indent_str + "    ", declared_vars))
                lines.append(f"{indent_str}}}")

            elif typ == "ForStatement":
                rng_str = to_cpp_expr(node["range"])
                loop_var = node["variable"]
                lines.append(f"{indent_str}for (auto {loop_var} : {rng_str}) {{")
                inner_vars = set(declared_vars)
                inner_vars.add(loop_var)
                lines.extend(walk(node["body"], indent_str + "    ", inner_vars))
                lines.append(f"{indent_str}}}")

            elif typ == "BREAKStatement":
                lines.append(f"{indent_str}break;")
            elif typ == "CONTINUEStatement":
                lines.append(f"{indent_str}continue;")
            elif typ == "ReturnStatement":
                if node.get("value") is None:
                    lines.append(f"{indent_str}return;")
                else:
                    lines.append(f"{indent_str}return {to_cpp_expr(node['value'])};")

            elif typ == "FunctionDef":
                params = node.get("params", [])
                if params:
                    t_params = ", ".join(f"typename T_{x}" for x in params)
                    param_str = ", ".join(f"T_{x} {x}" for x in params)
                    header = [f"template <{t_params}>"]
                else:
                    param_str = ""
                    header = []
                ret_type = "auto" if has_return(node.get("body", [])) else "void"
                func_lines = header + [f"{ret_type} {node['name']}({param_str}) {{"]
                local_declared = set(params)
                func_lines.extend(walk(node.get("body", []), "    ", local_declared))
                func_lines.append("}")
                functions_buf.append("\n".join(func_lines))

            elif typ == "ExpressionStatement":
                lines.append(f"{indent_str}{to_cpp_expr(node['value'])};")

        return lines

    main_body = walk(ast["body"], declared_vars=set())
    final_code = cpp_lines + [""] + functions_buf + [""] + ["int main() {"] + main_body + ["    return 0;", "}"]
    return "\n".join(final_code)


# ====================================================
# ps Discord Runtime
# ====================================================

_ps_discord_bot = None
_ps_discord_lock = threading.Lock()
_ps_discord_handlers = {}


class PsReturnSignal(Exception):
    def __init__(self, value=None):
        self.value = value


class PsBreakSignal(Exception):
    pass


class PsContinueSignal(Exception):
    pass


class PsFunction:
    def __init__(self, node, interpreter):
        self.node = node
        self.interpreter = interpreter

    def call(self, args):
        env = dict(self.interpreter.globals)

        for i, name in enumerate(
            self.node.get("params", [])
        ):
            env[name] = (
                args[i]
                if i < len(args)
                else None
            )

        try:
            self.interpreter.execute_block(
                self.node.get("body", []),
                env
            )
        except PsReturnSignal as e:
            return e.value

        return None


class PsDiscordInterpreter:

    def __init__(self, tree):
        self.tree = tree
        self.globals = {}
        self.functions = {}
        self.output = []
        self._if_chain_end = 0

        for node in tree.get("body", []):
            if node.get("type") == "FunctionDef":
                self.functions[node["name"]] = PsFunction(
                    node,
                    self
                )

    def eval_expr(self, node, env):
        if node is None:
            return None

        typ = node.get("type")

        if typ == "Literal":
            raw = node.get("value", "")

            if (
                raw.startswith('"')
                and raw.endswith('"')
            ):
                value = raw[1:-1]
                result = []
                i = 0
                mapping = {
                    "n": "\n",
                    "t": "\t",
                    "r": "\r",
                    '"': '"',
                    "\\": "\\"
                }

                while i < len(value):
                    if value[i] != "\\":
                        result.append(value[i])
                        i += 1
                        continue

                    i += 1

                    if i >= len(value):
                        result.append("\\")
                        break

                    esc = value[i]

                    if esc in mapping:
                        result.append(mapping[esc])
                    else:
                        result.append("\\" + esc)

                    i += 1

                return "".join(result)

            if "." in raw:
                try:
                    return float(raw)
                except ValueError:
                    return raw

            try:
                return int(raw)
            except ValueError:
                return raw

        if typ == "Variable":
            name = node["value"]

            if name in env:
                return env[name]

            if name in self.globals:
                return self.globals[name]

            if name == "true":
                return True

            if name == "false":
                return False

            raise Exception(
                f"未定義の変数: {name}"
            )

        if typ == "ArrayLiteral":
            return [
                self.eval_expr(x, env)
                for x in node.get("elements", [])
            ]

        if typ == "IndexExpr":
            base = env.get(
                node["name"],
                self.globals.get(node["name"])
            )

            index = self.eval_expr(
                node["index"],
                env
            )

            return base[index]

        if typ == "UnaryExpr":
            value = self.eval_expr(
                node["right"],
                env
            )

            if node["op"] == "!":
                return not bool(value)

            return value

        if typ == "BinaryExpr":
            op = node["op"]

            left = self.eval_expr(
                node["left"],
                env
            )

            if op == "&&":
                if not bool(left):
                    return False

                return bool(
                    self.eval_expr(
                        node["right"],
                        env
                    )
                )

            if op == "||":
                if bool(left):
                    return True

                return bool(
                    self.eval_expr(
                        node["right"],
                        env
                    )
                )

            right = self.eval_expr(
                node["right"],
                env
            )

            if op == "+":
                return left + right

            if op == "-":
                return left - right

            if op == "*":
                return left * right

            if op == "/":
                return left / right

            if op == "%":
                return left % right

            if op == "==":
                return left == right

            if op == "!=":
                return left != right

            if op == "<":
                return left < right

            if op == ">":
                return left > right

            if op == "<=":
                return left <= right

            if op == ">=":
                return left >= right

            raise Exception(
                f"未対応の演算子: {op}"
            )

        if typ == "CallExpr":
            name = node["name"]

            args = [
                self.eval_expr(arg, env)
                for arg in node.get("args", [])
            ]

            return self.call_builtin(
                name,
                args,
                env
            )

        raise Exception(
            f"未対応の式: {typ}"
        )

    def call_builtin(self, name, args, env):
        global _ps_discord_bot

        if name == "len":
            if len(args) != 1:
                raise Exception(
                    "len() は引数を1個指定してください。"
                )

            return len(args[0])

        if name == "range":
            if len(args) == 1:
                return range(int(args[0]))
            if len(args) == 2:
                return range(int(args[0]), int(args[1]))
            if len(args) == 3:
                return range(int(args[0]), int(args[1]), int(args[2]))
            raise Exception(
                "range() の引数は1〜3個です。"
            )

        if name == "input":
            if len(args) > 1:
                raise Exception(
                    "input() の引数は0〜1個です。"
                )

            if args:
                print(str(args[0]), end="")

            return input()

        # ================================================
        # Discord: start
        # ================================================

        if name == "discord_start":
            if not PS_DISCORD_AVAILABLE:
                raise Exception(
                    "discord_runtime.py が利用できません。"
                )

            if len(args) != 1 or not args[0]:
                raise Exception(
                    "discord_start() にはBot Tokenが必要です。"
                )

            with _ps_discord_lock:
                if _ps_discord_bot is not None:
                    return True

                bot = DiscordBot(
                    str(args[0])
                )

                for event_name, fn in list(
                    _ps_discord_handlers.items()
                ):
                    self._attach_discord_handler(
                        bot,
                        event_name,
                        fn
                    )

                _ps_discord_bot = bot

                def runner():
                    global _ps_discord_bot

                    try:
                        bot.start(
                            background=False
                        )

                    except Exception as e:
                        print(
                            f"[ps Discord] {e}"
                        )

                    finally:
                        with _ps_discord_lock:
                            if _ps_discord_bot is bot:
                                _ps_discord_bot = None

                threading.Thread(
                    target=runner,
                    daemon=True
                ).start()

            return True

        # ================================================
        # Discord: stop
        # ================================================

        if name == "discord_stop":
            with _ps_discord_lock:
                bot = _ps_discord_bot
                _ps_discord_bot = None

            if bot is not None:
                try:
                    bot.stop()
                except Exception:
                    pass

            return True

        # ================================================
        # Discord: status
        # ================================================

        if name == "discord_status":
            with _ps_discord_lock:
                bot = _ps_discord_bot

            if bot is None:
                return {
                    "running": False
                }

            return {
                "running": True
            }

        # ================================================
        # Discord: send
        # ================================================

        if name == "discord_send":
            if len(args) != 2:
                raise Exception(
                    "discord_send() は "
                    "チャンネルIDとメッセージの2個の引数が必要です。"
                )

            with _ps_discord_lock:
                bot = _ps_discord_bot

            if bot is None:
                raise Exception(
                    "Discord Botが起動していません。"
                )

            channel_id = str(args[0])
            content = str(args[1])

            bot.send_message(
                channel_id,
                content
            )

            return True

        # ================================================
        # Discord: event handler
        # ================================================

        if name == "discord_on":
            if len(args) != 2:
                raise Exception(
                    "discord_on() は "
                    "イベント名と関数名の2個の引数が必要です。"
                )

            event_name = str(args[0])
            function_name = str(args[1])

            if function_name not in self.functions:
                raise Exception(
                    f"関数が見つかりません: {function_name}"
                )

            fn = self.functions[
                function_name
            ]

            _ps_discord_handlers[
                event_name
            ] = fn

            with _ps_discord_lock:
                bot = _ps_discord_bot

            if bot is not None:
                self._attach_discord_handler(
                    bot,
                    event_name,
                    fn
                )

            return True

        # ================================================
        # Discord: reply
        # ================================================

        if name == "discord_reply":
            if len(args) != 1:
                raise Exception(
                    "discord_reply() は "
                    "メッセージ内容を1個指定してください。"
                )

            with _ps_discord_lock:
                bot = _ps_discord_bot

            if bot is None:
                raise Exception(
                    "Discord Botが起動していません。"
                )

            event = getattr(
                bot,
                "current_event",
                None
            )

            if event is None:
                raise Exception(
                    "現在処理中のDiscordイベントがありません。"
                )

            message = getattr(
                event,
                "message",
                None
            )

            if message is None:
                raise Exception(
                    "現在のイベントにメッセージがありません。"
                )

            channel_id = getattr(
                message,
                "channel_id",
                None
            )

            if channel_id is None:
                raise Exception(
                    "チャンネルIDを取得できません。"
                )

            bot.send_message(
                str(channel_id),
                str(args[0])
            )

            return True

        # ================================================
        # Discord: message content
        # ================================================

        if name == "discord_content":
            with _ps_discord_lock:
                bot = _ps_discord_bot

            if bot is None:
                return ""

            event = getattr(
                bot,
                "current_event",
                None
            )

            if event is None:
                return ""

            message = getattr(
                event,
                "message",
                None
            )

            if message is None:
                return ""

            return str(
                getattr(
                    message,
                    "content",
                    ""
                )
            )

        # ================================================
        # Discord: channel ID
        # ================================================

        if name == "discord_channel_id":
            with _ps_discord_lock:
                bot = _ps_discord_bot

            if bot is None:
                return ""

            event = getattr(
                bot,
                "current_event",
                None
            )

            if event is None:
                return ""

            message = getattr(
                event,
                "message",
                None
            )

            if message is None:
                return ""

            return str(
                getattr(
                    message,
                    "channel_id",
                    ""
                )
            )

        # ================================================
        # Discord: message ID
        # ================================================

        if name == "discord_message_id":
            with _ps_discord_lock:
                bot = _ps_discord_bot

            if bot is None:
                return ""

            event = getattr(
                bot,
                "current_event",
                None
            )

            if event is None:
                return ""

            message = getattr(
                event,
                "message",
                None
            )

            if message is None:
                return ""

            return str(
                getattr(
                    message,
                    "id",
                    ""
                )
            )

        # ================================================
        # Discord: author ID
        # ================================================

        if name == "discord_author_id":
            with _ps_discord_lock:
                bot = _ps_discord_bot

            if bot is None:
                return ""

            event = getattr(
                bot,
                "current_event",
                None
            )

            if event is None:
                return ""

            message = getattr(
                event,
                "message",
                None
            )

            if message is None:
                return ""

            author = getattr(
                message,
                "author",
                None
            )

            if author is None:
                return ""

            return str(
                getattr(
                    author,
                    "id",
                    ""
                )
            )

        # ================================================
        # Discord: guild ID
        # ================================================

        if name == "discord_guild_id":
            with _ps_discord_lock:
                bot = _ps_discord_bot

            if bot is None:
                return ""

            event = getattr(
                bot,
                "current_event",
                None
            )

            if event is None:
                return ""

            message = getattr(
                event,
                "message",
                None
            )

            if message is None:
                return ""

            guild_id = getattr(
                message,
                "guild_id",
                None
            )

            if guild_id is None:
                return ""

            return str(guild_id)

        # ================================================
        # Discord: event
        # ================================================

        if name == "discord_event":
            if len(args) != 1:
                raise Exception(
                    "discord_event() は "
                    "イベント名を1個指定してください。"
                )

            event_name = str(args[0])

            with _ps_discord_lock:
                bot = _ps_discord_bot

            if bot is None:
                return False

            return (
                getattr(
                    bot,
                    "current_event_name",
                    ""
                )
                == event_name
            )

        # ================================================
        # User function
        # ================================================

        if name in self.functions:
            return self.functions[name].call(args)

        raise Exception(
            f"未定義の関数です: {name}"
        )

    def _attach_discord_handler(
        self,
        bot,
        event_name,
        fn
    ):
        def callback(event):
            try:
                bot.current_event = event
                bot.current_event_name = event_name

                args = []

                if event_name == "message":
                    args = [
                        self._discord_message_dict(
                            event
                        )
                    ]

                fn.call(args)

            except Exception as e:
                print(
                    f"[ps Discord handler:{event_name}] "
                    f"{e}"
                )

        bot.on(
            event_name,
            callback
        )

    def _discord_message_dict(self, event):
        message = getattr(
            event,
            "message",
            None
        )

        if message is None:
            return {}

        author = getattr(
            message,
            "author",
            None
        )

        return {
            "content": getattr(
                message,
                "content",
                ""
            ),
            "channel_id": str(
                getattr(
                    message,
                    "channel_id",
                    ""
                )
            ),
            "message_id": str(
                getattr(
                    message,
                    "id",
                    ""
                )
            ),
            "author_id": (
                str(
                    getattr(
                        author,
                        "id",
                        ""
                    )
                )
                if author is not None
                else ""
            ),
            "guild_id": (
                str(
                    getattr(
                        message,
                        "guild_id",
                        ""
                    )
                )
                if getattr(
                    message,
                    "guild_id",
                    None
                ) is not None
                else ""
            )
        }

    # ====================================================
    # Execution
    # ====================================================

    def execute_node(self, node, env):
        typ = node["type"]

        if typ == "PrintStatement":
            value = self.eval_expr(
                node["value"],
                env
            )

            text = str(value)

            self.output.append(text)

            print(text)

            return

        if typ == "AssignmentExpression":
            value = self.eval_expr(
                node["value"],
                env
            )

            env[
                node["variable"]
            ] = value

            return

        if typ == "ArrayAssignStatement":
            name = node["name"]

            if name not in env:
                raise Exception(
                    f"未定義の配列: {name}"
                )

            index = self.eval_expr(
                node["index"],
                env
            )

            value = self.eval_expr(
                node["value"],
                env
            )

            env[name][index] = value

            return

        if typ == "ExpressionStatement":
            self.eval_expr(
                node["value"],
                env
            )

            return

        if typ == "IfStatement":
            if self.eval_expr(
                node["condition"],
                env
            ):
                self.execute_block(
                    node["body"],
                    env
                )

            return

        if typ == "ElifStatement":
            if self.eval_expr(
                node["condition"],
                env
            ):
                self.execute_block(
                    node["body"],
                    env
                )

            return

        if typ == "ElseStatement":
            self.execute_block(
                node["body"],
                env
            )

            return

        if typ == "WhileStatement":
            while self.eval_expr(
                node["condition"],
                env
            ):
                try:
                    self.execute_block(
                        node["body"],
                        env
                    )

                except PsBreakSignal:
                    break

                except PsContinueSignal:
                    continue

            return

        if typ == "ForStatement":
            iterable = self.eval_expr(
                node["range"],
                env
            )

            for value in iterable:
                env[
                    node["variable"]
                ] = value

                try:
                    self.execute_block(
                        node["body"],
                        env
                    )

                except PsBreakSignal:
                    break

                except PsContinueSignal:
                    continue

            return

        if typ == "BREAKStatement":
            raise PsBreakSignal()

        if typ == "CONTINUEStatement":
            raise PsContinueSignal()

        if typ == "ReturnStatement":
            value = None

            if node.get("value") is not None:
                value = self.eval_expr(
                    node["value"],
                    env
                )

            raise PsReturnSignal(value)

        if typ == "FunctionDef":
            return

        raise Exception(
            f"未対応のASTノード: {typ}"
        )

    def execute_block(self, body, env):
        i = 0

        while i < len(body):
            node = body[i]
            typ = node.get("type")

            # if / elif / else は同じチェーンとして処理する。
            if typ == "IfStatement":
                matched = False

                if self.eval_expr(node["condition"], env):
                    self.execute_block(node.get("body", []), env)
                    matched = True

                i += 1

                while i < len(body) and body[i].get("type") in (
                    "ElifStatement", "ElseStatement"
                ):
                    branch = body[i]
                    branch_type = branch.get("type")

                    if (
                        not matched
                        and branch_type == "ElifStatement"
                        and self.eval_expr(branch["condition"], env)
                    ):
                        self.execute_block(branch.get("body", []), env)
                        matched = True

                    elif (
                        not matched
                        and branch_type == "ElseStatement"
                    ):
                        self.execute_block(branch.get("body", []), env)
                        matched = True

                    i += 1

                continue

            # elif / else が単独で来た場合も安全に実行する。
            if typ == "ElifStatement":
                if self.eval_expr(node["condition"], env):
                    self.execute_block(node.get("body", []), env)
                i += 1
                continue

            if typ == "ElseStatement":
                self.execute_block(node.get("body", []), env)
                i += 1
                continue

            self.execute_node(node, env)
            i += 1

    def run(self):
        self.execute_block(
            self.tree.get("body", []),
            self.globals
        )

        return "\n".join(
            self.output
        )


# ====================================================
# Discord feature detection
# ====================================================

DISCORD_FUNCTIONS = {
    "discord_start",
    "discord_stop",
    "discord_status",
    "discord_send",
    "discord_on",
    "discord_reply",
    "discord_content",
    "discord_channel_id",
    "discord_message_id",
    "discord_author_id",
    "discord_guild_id",
    "discord_event"
}


def contains_discord_features(tree):
    def walk(node):
        if isinstance(node, dict):
            if (
                node.get("type") == "CallExpr"
                and node.get("name")
                in DISCORD_FUNCTIONS
            ):
                return True

            return any(
                walk(value)
                for value in node.values()
            )

        if isinstance(node, list):
            return any(
                walk(value)
                for value in node
            )

        return False

    return walk(tree)


# ====================================================
# /run
# ====================================================

@app.route("/run", methods=["POST"])
def run_code():
    data = request.get_json(
        silent=True
    ) or {}

    source = data.get(
        "code",
        ""
    )

    if not isinstance(source, str):
        return jsonify({
            "success": False,
            "error": "code は文字列で指定してください。"
        }), 400

    if not source.strip():
        return jsonify({
            "success": False,
            "error": "コードが空です。"
        }), 400

    try:
        tokens = tokenize(source)

        tree = parse(tokens)

        if contains_discord_features(tree):
            if not PS_DISCORD_AVAILABLE:
                return jsonify({
                    "success": False,
                    "error": (
                        "discord_runtime.py "
                        "が利用できません。"
                    )
                }), 500

            interpreter = PsDiscordInterpreter(
                tree
            )

            output = interpreter.run()

            return jsonify({
                "success": True,
                "output": output,
                "mode": "ps"
            })

        cpp_code = generate_cpp(tree)

        # リクエストごとに専用ディレクトリを使う。
        # 同時実行時に main.cpp / main が競合しないようにする。
        with tempfile.TemporaryDirectory(prefix="ps_") as workdir:
            source_path = os.path.join(workdir, "main.cpp")
            executable_path = os.path.join(workdir, "main")

            with open(
                source_path,
                "w",
                encoding="utf-8"
            ) as f:
                f.write(cpp_code)

            compile_result = subprocess.run(
                [
                    "g++",
                    "-std=c++17",
                    source_path,
                    "-o",
                    executable_path
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30
            )

            if compile_result.returncode != 0:
                return jsonify({
                    "success": False,
                    "error": compile_result.stderr,
                    "cpp": cpp_code
                })

            run_result = subprocess.run(
                [executable_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30
            )

            return jsonify({
                "success": run_result.returncode == 0,
                "output": run_result.stdout,
                "error": run_result.stderr,
                "cpp": cpp_code,
                "mode": "cpp"
            })

    except subprocess.TimeoutExpired:
        return jsonify({
            "success": False,
            "error": "実行がタイムアウトしました。"
        }), 408

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400


# ====================================================
# REST Discord API
# ====================================================

@app.route("/discord/status", methods=["GET"])
def discord_status():
    bot = get_discord_bot()

    if bot is None:
        return jsonify({
            "running": False
        })

    return jsonify({
        "running": True
    })


@app.route("/discord/start", methods=["POST"])
def discord_start():
    if not PS_DISCORD_AVAILABLE:
        return jsonify({
            "success": False,
            "error": "discord_runtime.py が利用できません。"
        }), 500

    data = request.get_json(
        silent=True
    ) or {}

    token = data.get(
        "token",
        ""
    )

    if not token:
        return jsonify({
            "success": False,
            "error": "Bot Tokenが必要です。"
        }), 400

    existing = get_discord_bot()

    if existing is not None:
        return jsonify({
            "success": True,
            "message": "Discord Botはすでに起動しています。",
            "reused": True
        })

    try:
        bot = DiscordBot(
            str(token)
        )

        set_discord_bot(bot)

        def runner():
            try:
                bot.start(
                    background=False
                )

            except Exception as e:
                print(
                    f"[REST Discord] {e}"
                )

            finally:
                current = get_discord_bot()

                if current is bot:
                    set_discord_bot(None)

        threading.Thread(
            target=runner,
            daemon=True
        ).start()

        return jsonify({
            "success": True,
            "message": "Discord Botを起動しました。",
            "reused": False
        })

    except Exception as e:
        set_discord_bot(None)

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/discord/stop", methods=["POST"])
def discord_stop():
    bot = get_discord_bot()

    if bot is None:
        return jsonify({
            "success": True,
            "message": "Discord Botは起動していません。"
        })

    try:
        bot.stop()

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    finally:
        set_discord_bot(None)

    return jsonify({
        "success": True,
        "message": "Discord Botを停止しました。"
    })


@app.route("/discord/send", methods=["POST"])
def discord_send():
    bot = get_discord_bot()

    if bot is None:
        return jsonify({
            "success": False,
            "error": "Discord Botが起動していません。"
        }), 400

    data = request.get_json(
        silent=True
    ) or {}

    channel_id = data.get(
        "channel_id"
    )

    content = data.get(
        "content"
    )

    if channel_id is None:
        return jsonify({
            "success": False,
            "error": "channel_id が必要です。"
        }), 400

    if content is None:
        return jsonify({
            "success": False,
            "error": "content が必要です。"
        }), 400

    try:
        bot.send_message(
            str(channel_id),
            str(content)
        )

        return jsonify({
            "success": True
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ====================================================
# Startup
# ====================================================

if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
