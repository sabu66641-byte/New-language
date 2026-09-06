import os
import re
import subprocess
import threading

from flask import Flask, request, jsonify, render_template

try:
    from discord_runtime import DiscordBot, DiscordError
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
        "using namespace std;",
        "",
        "template<typename T> int len(const vector<T>& v) { return v.size(); }",
        "int len(const string& s) { return s.length(); }",
        "string input() { string s; getline(cin, s); return s; }",
        ""
    ]

    functions_buf = []
    declared_vars = set()

    def to_cpp_expr(node):
        if not node:
            return ""

        if node["type"] == "Literal":
            return node["value"]

        if node["type"] == "Variable":
            return node["value"]

        if node["type"] == "UnaryExpr":
            return f"({node['op']}{to_cpp_expr(node['right'])})"

        if node["type"] == "BinaryExpr":
            left_str = to_cpp_expr(node["left"])
            right_str = to_cpp_expr(node["right"])

            if (
                node["op"] == "+"
                and (
                    left_str.startswith('"')
                    or right_str.startswith('"')
                )
            ):
                if left_str.startswith('"'):
                    left_str = f"string({left_str})"

                if right_str.startswith('"'):
                    right_str = f"string({right_str})"

            return (
                f"({left_str} "
                f"{node['op']} "
                f"{right_str})"
            )

        if node["type"] == "ArrayLiteral":
            elements = ", ".join(
                to_cpp_expr(e)
                for e in node["elements"]
            )

            return f"{{{elements}}}"

        if node["type"] == "IndexExpr":
            return (
                f"{node['name']}"
                f"[{to_cpp_expr(node['index'])}]"
            )

        if node["type"] == "CallExpr":
            args = ", ".join(
                to_cpp_expr(a)
                for a in node["args"]
            )

            return (
                f"{node['name']}"
                f"({args})"
            )

        return ""

    def walk(nodes, indent_str="    "):
        lines = []

        for node in nodes:

            if node["type"] == "AssignmentExpression":
                var = node["variable"]
                val_node = node["value"]
                cpp_val = to_cpp_expr(val_node)

                if var in declared_vars:
                    lines.append(
                        f"{indent_str}{var} = {cpp_val};"
                    )
                else:
                    if val_node["type"] == "ArrayLiteral":
                        is_string = False

                        if val_node["elements"]:
                            first_elem = val_node["elements"][0]

                            if (
                                first_elem["type"] == "Literal"
                                and first_elem["value"].startswith('"')
                            ):
                                is_string = True

                        type_str = (
                            "vector<string>"
                            if is_string
                            else "vector<int>"
                        )

                        lines.append(
                            f"{indent_str}"
                            f"{type_str} {var} = "
                            f"{cpp_val};"
                        )

                    elif (
                        val_node["type"] == "Literal"
                        and val_node["value"].startswith('"')
                    ):
                        lines.append(
                            f"{indent_str}"
                            f"string {var} = "
                            f"{cpp_val};"
                        )

                    elif (
                        val_node["type"] == "CallExpr"
                        and val_node["name"] == "input"
                    ):
                        lines.append(
                            f"{indent_str}"
                            f"string {var} = "
                            f"{cpp_val};"
                        )

                    else:
                        lines.append(
                            f"{indent_str}"
                            f"auto {var} = "
                            f"{cpp_val};"
                        )

                    declared_vars.add(var)

            elif node["type"] == "ArrayAssignStatement":
                lines.append(
                    f"{indent_str}"
                    f"{node['name']}"
                    f"[{to_cpp_expr(node['index'])}]"
                    f" = "
                    f"{to_cpp_expr(node['value'])};"
                )

            elif node["type"] == "PrintStatement":
                lines.append(
                    f"{indent_str}"
                    f"cout << "
                    f"{to_cpp_expr(node['value'])}"
                    f" << endl;"
                )

            elif node["type"] == "IfStatement":
                lines.append(
                    f"{indent_str}"
                    f"if ({to_cpp_expr(node['condition'])}) {{"
                )

                lines.extend(
                    walk(
                        node["body"],
                        indent_str + "    "
                    )
                )

                lines.append(f"{indent_str}}}")

            elif node["type"] == "ElifStatement":
                lines.append(
                    f"{indent_str}"
                    f"else if "
                    f"({to_cpp_expr(node['condition'])}) {{"
                )

                lines.extend(
                    walk(
                        node["body"],
                        indent_str + "    "
                    )
                )

                lines.append(f"{indent_str}}}")

            elif node["type"] == "ElseStatement":
                lines.append(
                    f"{indent_str}"
                    f"else {{"
                )

                lines.extend(
                    walk(
                        node["body"],
                        indent_str + "    "
                    )
                )

                lines.append(f"{indent_str}}}")

            elif node["type"] == "WhileStatement":
                lines.append(
                    f"{indent_str}"
                    f"while "
                    f"({to_cpp_expr(node['condition'])}) {{"
                )

                lines.extend(
                    walk(
                        node["body"],
                        indent_str + "    "
                    )
                )

                lines.append(f"{indent_str}}}")

            elif node["type"] == "ForStatement":
                rng_str = to_cpp_expr(node["range"])

                lines.append(
                    f"{indent_str}"
                    f"for (auto& "
                    f"{node['variable']} : "
                    f"{rng_str}) {{"
                )

                lines.extend(
                    walk(
                        node["body"],
                        indent_str + "    "
                    )
                )

                lines.append(f"{indent_str}}}")

            elif node["type"] == "BREAKStatement":
                lines.append(f"{indent_str}break;")

            elif node["type"] == "CONTINUEStatement":
                lines.append(f"{indent_str}continue;")

            elif node["type"] == "ReturnStatement":
                ret_val = (
                    to_cpp_expr(node["value"])
                    if node["value"]
                    else ""
                )

                lines.append(
                    f"{indent_str}"
                    f"return {ret_val};"
                )

            elif node["type"] == "FunctionDef":
                t_params = ", ".join(
                    f"typename T_{p}"
                    for p in node["params"]
                )

                param_str = ", ".join(
                    f"T_{p} {p}"
                    for p in node["params"]
                )

                func_lines = [
                    f"template <{t_params}>",
                    f"auto {node['name']}({param_str}) {{"
                ]

                func_lines.extend(
                    walk(
                        node["body"],
                        "    "
                    )
                )

                func_lines.append("}")

                functions_buf.append(
                    "\n".join(func_lines)
                )

            elif node["type"] == "ExpressionStatement":
                lines.append(
                    f"{indent_str}"
                    f"{to_cpp_expr(node['value'])};"
                )

        return lines

    main_body = walk(ast["body"])

    final_code = (
        cpp_lines
        + [""]
        + functions_buf
        + [""]
        + ["int main() {"]
        + main_body
        + ["    return 0;", "}"]
    )

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
                try:
                    return bytes(
                        raw[1:-1],
                        "utf-8"
                    ).decode("unicode_escape")
                except Exception:
                    return raw[1:-1]

            if "." in raw:
                try:
                    return float(raw)
                except Exception:
                    return raw

            try:
                return int(raw)
            except Exception:
                return raw

        if typ == "Variable":
            name = node.get("value")

            if name in env:
                return env[name]

            if name in self.globals:
                return self.globals[name]

            return None

        if typ == "ArrayLiteral":
            return [
                self.eval_expr(element, env)
                for element in node.get("elements", [])
            ]

        if typ == "IndexExpr":
            name = node.get("name")
            index = self.eval_expr(
                node.get("index"),
                env
            )

            if name in env:
                value = env[name]
            elif name in self.globals:
                value = self.globals[name]
            else:
                return None

            return value[int(index)]

        if typ == "UnaryExpr":
            right = self.eval_expr(
                node.get("right"),
                env
            )

            if node.get("op") == "!":
                return not bool(right)

            return None

        if typ == "BinaryExpr":
            op = node.get("op")

            if op == "&&":
                left = self.eval_expr(
                    node.get("left"),
                    env
                )

                if not bool(left):
                    return False

                return bool(
                    self.eval_expr(
                        node.get("right"),
                        env
                    )
                )

            if op == "||":
                left = self.eval_expr(
                    node.get("left"),
                    env
                )

                if bool(left):
                    return True

                return bool(
                    self.eval_expr(
                        node.get("right"),
                        env
                    )
                )

            left = self.eval_expr(
                node.get("left"),
                env
            )

            right = self.eval_expr(
                node.get("right"),
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

            return None

        if typ == "CallExpr":
            name = node.get("name")

            args = [
                self.eval_expr(arg, env)
                for arg in node.get("args", [])
            ]

            return self.call_builtin(
                name,
                args,
                env
            )

        return None

        raise Exception(
            f"未対応の式: {typ}"
        )

    def call_builtin(self, name, args, env):
        global _ps_discord_bot

        if name == "len":
            if len(args) != 1:
                raise Exception(
                    "len() は引数を1つ必要とします。"
                )
            return len(args[0])

        if name == "input":
            return input()

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
                # すでにBotが起動している場合は再起動せず、
                # そのまま既存のBotを使う。
                if _ps_discord_bot is not None:
                    return True

                bot = DiscordBot(
                    str(args[0])
                )

                def runner():
                    global _ps_discord_bot

                    try:
                        bot.start()
                    except Exception as e:
                        print(
                            f"[ps Discord] {e}"
                        )
                    finally:
                        with _ps_discord_lock:
                            if _ps_discord_bot is bot:
                                _ps_discord_bot = None

                _ps_discord_bot = bot

                threading.Thread(
                    target=runner,
                    daemon=True
                ).start()

            return True

        if name == "discord_stop":
            with _ps_discord_lock:
                bot = _ps_discord_bot
                _ps_discord_bot = None

            if bot is not None:
                bot.stop()

            return True

        if name == "discord_status":
            bot = _ps_discord_bot

            if bot is None:
                return {
                    "running": False,
                    "available": PS_DISCORD_AVAILABLE
                }

            return {
                "running": True,
                "available": True,
                "user": getattr(
                    bot,
                    "user",
                    None
                )
            }

        if name == "discord_send":
            if _ps_discord_bot is None:
                raise Exception(
                    "Discord Botが起動していません。"
                )

            if len(args) != 2:
                raise Exception(
                    "discord_send(channel_id, content) "
                    "が必要です。"
                )

            return _ps_discord_bot.send_message(
                str(args[0]),
                str(args[1])
            )

        if name == "discord_on":
            if len(args) != 2:
                raise Exception(
                    "discord_on(event, function) "
                    "が必要です。"
                )

            event_name = str(args[0])
            handler_name = str(args[1])

            if handler_name not in self.functions:
                raise Exception(
                    f"関数 '{handler_name}' が存在しません。"
                )

            fn = self.functions[handler_name]

            _ps_discord_handlers[
                event_name
            ] = fn

            if (
                _ps_discord_bot is not None
                and hasattr(
                    _ps_discord_bot,
                    "on"
                )
            ):
                def callback(
                    data,
                    _fn=fn
                ):
                    try:
                        if isinstance(data, dict):
                            _ps_discord_bot.current_event = data

                        _fn.call([])

                    except Exception as e:
                        print(
                            f"[ps Discord handler] {e}"
                        )

                _ps_discord_bot.on(
                    event_name,
                    callback
                )

            return True

        if name == "discord_reply":
            if _ps_discord_bot is None:
                raise Exception(
                    "Discord Botが起動していません。"
                )

            if len(args) != 1:
                raise Exception(
                    "discord_reply(content) "
                    "が必要です。"
                )

            event = getattr(
                _ps_discord_bot,
                "current_event",
                None
            )

            if isinstance(event, dict):
                channel_id = event.get(
                    "channel_id"
                )

                if channel_id:
                    return _ps_discord_bot.send_message(
                        str(channel_id),
                        str(args[0])
                    )

            raise Exception(
                "現在のDiscordイベントに"
                "チャンネル情報がありません。"
            )

        if name == "discord_content":
            event = getattr(
                _ps_discord_bot,
                "current_event",
                None
            )

            if isinstance(event, dict):
                return event.get(
                    "content",
                    ""
                )

            return ""

        if name == "discord_channel_id":
            event = getattr(
                _ps_discord_bot,
                "current_event",
                None
            )

            if isinstance(event, dict):
                return event.get(
                    "channel_id",
                    ""
                )

            return ""

        if name == "discord_message_id":
            event = getattr(
                _ps_discord_bot,
                "current_event",
                None
            )

            if isinstance(event, dict):
                return event.get(
                    "message_id",
                    ""
                )

            return ""

        if name == "discord_author_id":
            event = getattr(
                _ps_discord_bot,
                "current_event",
                None
            )

            if isinstance(event, dict):
                return event.get(
                    "author_id",
                    ""
                )

            return ""

        if name == "discord_guild_id":
            event = getattr(
                _ps_discord_bot,
                "current_event",
                None
            )

            if isinstance(event, dict):
                return event.get(
                    "guild_id",
                    ""
                )

            return ""

        if name == "discord_event":
            event = getattr(
                _ps_discord_bot,
                "current_event",
                None
            )

            if isinstance(event, dict):
                return dict(event)

            return {}

        if name in self.functions:
            return self.functions[name].call(args)

        raise Exception(
            f"未定義の関数: {name}"
        )

    def execute_node(self, node, env):
        typ = node.get("type")

        if typ == "AssignmentExpression":
            value = self.eval_expr(
                node["value"],
                env
            )

            env[node["variable"]] = value
            self.globals[node["variable"]] = value
            return

        if typ == "ArrayAssignStatement":
            arr = env.get(
                node["name"],
                self.globals.get(node["name"])
            )

            index = self.eval_expr(
                node["index"],
                env
            )

            value = self.eval_expr(
                node["value"],
                env
            )

            arr[index] = value
            return

        if typ == "PrintStatement":
            value = self.eval_expr(
                node["value"],
                env
            )

            text = str(value)
            self.output.append(text)
            print(text)
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
                    node.get("body", []),
                    env
                )
            return

        if typ == "ElifStatement":
            if self.eval_expr(
                node["condition"],
                env
            ):
                self.execute_block(
                    node.get("body", []),
                    env
                )
            return

        if typ == "ElseStatement":
            self.execute_block(
                node.get("body", []),
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
                        node.get("body", []),
                        env
                    )
                except PsBreakSignal:
                    break
                except PsContinueSignal:
                    continue

            return

        if typ == "ForStatement":
            values = self.eval_expr(
                node["range"],
                env
            )

            if isinstance(values, int):
                values = range(values)

            for value in values:
                env[node["variable"]] = value

                try:
                    self.execute_block(
                        node.get("body", []),
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
            value = (
                self.eval_expr(
                    node["value"],
                    env
                )
                if node.get("value") is not None
                else None
            )

            raise PsReturnSignal(value)

        if typ == "FunctionDef":
            return

        raise Exception(
            f"未対応の文: {typ}"
        )

    def execute_block(self, nodes, env):
        i = 0

        while i < len(nodes):
            node = nodes[i]

            if node.get("type") == "IfStatement":
                self.execute_if_chain(
                    nodes,
                    i,
                    env
                )

                i = self._if_chain_end
                continue

            self.execute_node(
                node,
                env
            )

            i += 1

    def execute_if_chain(
        self,
        nodes,
        start,
        env
    ):
        first = nodes[start]
        branch_taken = False

        if self.eval_expr(
            first["condition"],
            env
        ):
            self.execute_block(
                first.get("body", []),
                env
            )

            branch_taken = True

        i = start + 1

        while i < len(nodes):
            node = nodes[i]

            if node.get("type") == "ElifStatement":
                if (
                    not branch_taken
                    and self.eval_expr(
                        node["condition"],
                        env
                    )
                ):
                    self.execute_block(
                        node.get("body", []),
                        env
                    )

                    branch_taken = True

                i += 1
                continue

            if node.get("type") == "ElseStatement":
                if not branch_taken:
                    self.execute_block(
                        node.get("body", []),
                        env
                    )

                i += 1

            break

        self._if_chain_end = i

    def run(self):
        self.execute_block(
            self.tree.get("body", []),
            self.globals
        )

        return "\n".join(self.output)


def contains_discord_features(tree):
    discord_names = {
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
        "discord_event",
    }

    def walk(node):
        if isinstance(node, dict):
            if (
                node.get("type") == "CallExpr"
                and node.get("name")
                in discord_names
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

    data = request.get_json()

    if not data:
        return jsonify({
            "success": False,
            "error": "JSONデータがありません。"
        })

    user_code = data.get(
        "code",
        ""
    )

    try:
        tokens = tokenize(user_code)
        ast = parse(tokens)

        if contains_discord_features(ast):
            interpreter = PsDiscordInterpreter(ast)
            output = interpreter.run()

            return jsonify({
                "success": True,
                "output": output,
                "mode": "discord"
            })

        cpp_code = generate_cpp(ast)

        with open(
            "main.cpp",
            "w",
            encoding="utf-8"
        ) as f:
            f.write(cpp_code)

        compile_res = subprocess.run(
            [
                "g++",
                "-std=c++17",
                "main.cpp",
                "-o",
                "prog"
            ],
            capture_output=True,
            text=True,
            timeout=30
        )

        if compile_res.returncode != 0:
            err_msg = compile_res.stderr

            if "was not declared" in err_msg:
                raise Exception(
                    "ps型・未定義エラー: "
                    "使用された変数が定義されていないか、"
                    "不適切な演算が行われました。"
                )

            raise Exception(
                "C++内部コンパイルエラー:\n"
                + err_msg
            )

        result = subprocess.run(
            ["./prog"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30
        )

        return jsonify({
            "success": True,
            "output": result.stdout,
            "cpp": cpp_code,
            "mode": "cpp"
        })

    except subprocess.TimeoutExpired:
        return jsonify({
            "success": False,
            "error": "実行時間が30秒を超えました。"
        }), 408

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        })


# ====================================================
# Discord API
# ====================================================

@app.route(
    "/discord/status",
    methods=["GET"]
)
def discord_status():

    if not PS_DISCORD_AVAILABLE:
        return jsonify({
            "success": False,
            "running": False,
            "available": False,
            "error": (
                "discord_runtime.py が"
                "読み込めません。"
            )
        })

    bot = get_discord_bot()

    if bot is None:
        return jsonify({
            "success": True,
            "running": False,
            "available": True
        })

    user = getattr(
        bot,
        "user",
        None
    )

    return jsonify({
        "success": True,
        "running": True,
        "available": True,
        "user": user
    })


@app.route(
    "/discord/start",
    methods=["POST"]
)
def discord_start():

    if not PS_DISCORD_AVAILABLE:
        return jsonify({
            "success": False,
            "error": (
                "discord_runtime.py が"
                "利用できません。"
            )
        }), 500

    data = request.get_json()

    if not data:
        return jsonify({
            "success": False,
            "error": "JSONデータがありません。"
        }), 400

    token = data.get(
        "token",
        ""
    )

    if not token:
        return jsonify({
            "success": False,
            "error": (
                "Discord Bot Tokenが"
                "指定されていません。"
            )
        }), 400

    existing_bot = get_discord_bot()

    if existing_bot is not None:
        return jsonify({
            "success": False,
            "error": (
                "Discord Botは"
                "すでに起動しています。"
            )
        }), 409

    try:
        bot = DiscordBot(token)

        def run_bot():
            try:
                bot.start()
            except Exception as e:
                print(
                    f"[Discord] Bot error: {e}"
                )
            finally:
                current = get_discord_bot()

                if current is bot:
                    set_discord_bot(None)

        thread = threading.Thread(
            target=run_bot,
            daemon=True
        )

        set_discord_bot(bot)
        thread.start()

        return jsonify({
            "success": True,
            "message": (
                "Discord Botの起動処理を"
                "開始しました。"
            )
        })

    except Exception as e:
        set_discord_bot(None)

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route(
    "/discord/stop",
    methods=["POST"]
)
def discord_stop():

    bot = get_discord_bot()

    if bot is None:
        return jsonify({
            "success": False,
            "error": (
                "起動しているDiscord Botが"
                "ありません。"
            )
        }), 404

    try:
        bot.stop()
        set_discord_bot(None)

        return jsonify({
            "success": True,
            "message": (
                "Discord Botを"
                "停止しました。"
            )
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route(
    "/discord/send",
    methods=["POST"]
)
def discord_send():

    bot = get_discord_bot()

    if bot is None:
        return jsonify({
            "success": False,
            "error": (
                "Discord Botが"
                "起動していません。"
            )
        }), 400

    data = request.get_json()

    if not data:
        return jsonify({
            "success": False,
            "error": "JSONデータがありません。"
        }), 400

    channel_id = str(
        data.get(
            "channel_id",
            ""
        )
    )

    content = data.get(
        "content",
        ""
    )

    if not channel_id:
        return jsonify({
            "success": False,
            "error": (
                "channel_id が"
                "指定されていません。"
            )
        }), 400

    if not content:
        return jsonify({
            "success": False,
            "error": (
                "content が"
                "指定されていません。"
            )
        }), 400

    try:
        result = bot.send_message(
            channel_id,
            content
        )

        return jsonify({
            "success": True,
            "message": (
                "Discordへメッセージを"
                "送信しました。"
            ),
            "result": result
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ====================================================
# 起動
# ====================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        )
    )
