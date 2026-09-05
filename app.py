import os
import subprocess
import re
import threading
from flask import Flask, request, jsonify, render_template

# ====================================================
# Discord Runtime
# ====================================================
# discord_runtime.py が存在する場合のみ読み込みます。
# 既存の ps コンパイラ単体でも動作できるようにしています。
try:
    from discord_runtime import DiscordBot, DiscordError
    DISCORD_AVAILABLE = True
except ImportError:
    DiscordBot = None
    DiscordError = Exception
    DISCORD_AVAILABLE = False


app = Flask(__name__)


# ====================================================
# Discord Bot 管理
# ====================================================

discord_bot = None
discord_bot_lock = threading.Lock()


def get_discord_bot():
    """現在起動しているDiscord Botを取得"""
    global discord_bot
    with discord_bot_lock:
        return discord_bot


def set_discord_bot(bot):
    """Discord Botを設定"""
    global discord_bot
    with discord_bot_lock:
        discord_bot = bot


@app.route("/")
def index():
    return render_template("index.html")


# ====================================================
# 【新言語 ps】 最終完全体コンパイラエンジン (v5.0)
# ====================================================


# ====================================================
# 1. 字句解析（Lexer）
# ====================================================

def tokenize(source_code):
    token_specification = [
        ('COMMENT',   r'#.*'),
        ('STRING',    r'"[^"\\]*(?:\\.[^"\\]*)*"'),
        ('NUMBER',    r'\d+(?:\.\d+)?'),
        ('KEYWORDS',  r'\b(if|elif|else|while|for|in|break|continue|func|return)\b'),
        ('LOGIC',     r'\b(and|or|not)\b'),
        ('ID',        r'[a-zA-Z_]\w*'),
        ('OP',        r'==|!=|<=|>=|[-+*/%=<>]'),
        ('LPAREN',    r'\('),
        ('RPAREN',    r'\)'),
        ('LBRACK',    r'\['),
        ('RBRACK',    r'\]'),
        ('COMMA',     r','),
        ('NL',        r'\n'),
        ('SKIP',      r'[ \t]+'),
        ('MISMATCH',  r'.'),
    ]

    tok_regex = '|'.join(
        f'(?P<{name}>{regex})'
        for name, regex in token_specification
    )

    tokens = []

    for line_num, line in enumerate(source_code.split('\n'), 1):
        if not line.strip() or line.strip().startswith('#'):
            continue

        indent_match = re.match(r'^(\s*)', line)
        indent_len = len(indent_match.group(1)) if indent_match else 0

        if indent_len % 4 != 0:
            raise Exception(
                f"インデントエラー (行 {line_num}): "
                f"半角スペース4つ単位で字下げしてください。"
            )

        indent_level = indent_len // 4

        for mo in re.finditer(tok_regex, line):
            kind = mo.lastgroup
            value = mo.group()

            if kind == 'COMMENT' or kind == 'SKIP':
                continue

            elif kind == 'MISMATCH':
                raise Exception(
                    f"構文エラー (行 {line_num}): "
                    f"不正な文字 '{value}' があります。"
                )

            tokens.append({
                'type': kind,
                'value': value,
                'line': line_num,
                'indent': indent_level
            })

        tokens.append({
            'type': 'NL',
            'value': '\n',
            'line': line_num,
            'indent': indent_level
        })

    return tokens


# ====================================================
# 2-A. 式解析エンジン
# ====================================================

class ExpressionParser:

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        if (
            self.pos < len(self.tokens)
            and self.tokens[self.pos]['type'] != 'NL'
        ):
            return self.tokens[self.pos]

        return None

    def consume(self, expected_type=None):
        tok = self.peek()

        if tok and (
            expected_type is None
            or tok['type'] == expected_type
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
            and self.peek()['type'] == 'LOGIC'
            and self.peek()['value'] == 'or'
        ):
            self.consume()

            right = self.parse_and()

            node = {
                'type': 'BinaryExpr',
                'op': '||',
                'left': node,
                'right': right
            }

        return node

    def parse_and(self):
        node = self.parse_equality()

        while (
            self.peek()
            and self.peek()['type'] == 'LOGIC'
            and self.peek()['value'] == 'and'
        ):
            self.consume()

            right = self.parse_equality()

            node = {
                'type': 'BinaryExpr',
                'op': '&&',
                'left': node,
                'right': right
            }

        return node

    def parse_equality(self):
        node = self.parse_expr()

        while (
            self.peek()
            and self.peek()['type'] == 'OP'
            and self.peek()['value']
            in ['==', '!=', '<', '>', '<=', '>=']
        ):
            op = self.consume()['value']
            right = self.parse_expr()

            node = {
                'type': 'BinaryExpr',
                'op': op,
                'left': node,
                'right': right
            }

        return node

    def parse_expr(self):
        node = self.parse_term()

        while (
            self.peek()
            and self.peek()['type'] == 'OP'
            and self.peek()['value'] in ['+', '-']
        ):
            op = self.consume()['value']
            right = self.parse_term()

            node = {
                'type': 'BinaryExpr',
                'op': op,
                'left': node,
                'right': right
            }

        return node

    def parse_term(self):
        node = self.parse_factor()

        while (
            self.peek()
            and self.peek()['type'] == 'OP'
            and self.peek()['value'] in ['*', '/', '%']
        ):
            op = self.consume()['value']
            right = self.parse_factor()

            node = {
                'type': 'BinaryExpr',
                'op': op,
                'left': node,
                'right': right
            }

        return node

    def parse_factor(self):
        tok = self.peek()

        if not tok:
            return None

        # 数値・文字列
        if tok['type'] == 'NUMBER' or tok['type'] == 'STRING':
            return {
                'type': 'Literal',
                'value': self.consume()['value']
            }

        # 配列
        elif tok['type'] == 'LBRACK':
            self.consume('LBRACK')

            elements = []

            while (
                self.peek()
                and self.peek()['type'] != 'RBRACK'
            ):
                elements.append(self.parse())

                if (
                    self.peek()
                    and self.peek()['type'] == 'COMMA'
                ):
                    self.consume('COMMA')

            self.consume('RBRACK')

            return {
                'type': 'ArrayLiteral',
                'elements': elements
            }

        # 識別子・関数呼び出し・配列アクセス
        elif tok['type'] == 'ID':
            name = self.consume()['value']

            # 関数呼び出し
            if (
                self.peek()
                and self.peek()['type'] == 'LPAREN'
            ):
                self.consume('LPAREN')

                args = []

                while (
                    self.peek()
                    and self.peek()['type'] != 'RPAREN'
                ):
                    args.append(self.parse())

                    if (
                        self.peek()
                        and self.peek()['type'] == 'COMMA'
                    ):
                        self.consume('COMMA')

                self.consume('RPAREN')

                return {
                    'type': 'CallExpr',
                    'name': name,
                    'args': args
                }

            # 配列アクセス
            if (
                self.peek()
                and self.peek()['type'] == 'LBRACK'
            ):
                self.consume('LBRACK')

                index_node = self.parse()

                self.consume('RBRACK')

                return {
                    'type': 'IndexExpr',
                    'name': name,
                    'index': index_node
                }

            return {
                'type': 'Variable',
                'value': name
            }

        # 括弧
        elif tok['type'] == 'LPAREN':
            self.consume('LPAREN')

            node = self.parse()

            self.consume('RPAREN')

            return node

        # not
        elif (
            tok['type'] == 'LOGIC'
            and tok['value'] == 'not'
        ):
            self.consume()

            return {
                'type': 'UnaryExpr',
                'op': '!',
                'right': self.parse_factor()
            }

        return None


# ====================================================
# 2-B. メイン構文解析（Parser）
# ====================================================

def parse(tokens):
    root = {
        'type': 'Program',
        'body': []
    }

    if not tokens:
        return root

    stack = [(-1, root)]
    i = 0

    while i < len(tokens):
        token = tokens[i]

        if token['type'] == 'NL':
            i += 1
            continue

        while (
            stack
            and token['indent'] <= stack[-1][0]
        ):
            stack.pop()

        parent = stack[-1][1]

        node = {
            'line': token['line']
        }

        line_tokens = []

        while (
            i < len(tokens)
            and tokens[i]['type'] != 'NL'
        ):
            line_tokens.append(tokens[i])
            i += 1

        if i < len(tokens):
            i += 1

        if not line_tokens:
            continue

        first = line_tokens[0]

        # ------------------------------
        # func
        # ------------------------------

        if (
            first['type'] == 'KEYWORDS'
            and first['value'] == 'func'
        ):
            if (
                len(line_tokens) < 2
                or line_tokens[1]['type'] != 'ID'
            ):
                raise Exception(
                    f"構文エラー (行 {first['line']}): "
                    f"func の後ろに関数名が必要です。"
                )

            func_name = line_tokens[1]['value']

            params = []

            p_idx = 2

            while p_idx < len(line_tokens):
                if line_tokens[p_idx]['type'] == 'ID':
                    params.append(
                        line_tokens[p_idx]['value']
                    )

                p_idx += 1

            node.update({
                'type': 'FunctionDef',
                'name': func_name,
                'params': params,
                'body': []
            })

            parent['body'].append(node)

            stack.append(
                (first['indent'], node)
            )

        # ------------------------------
        # if / elif / while
        # ------------------------------

        elif (
            first['type'] == 'KEYWORDS'
            and first['value']
            in ['if', 'elif', 'while']
        ):
            kind = (
                'IfStatement'
                if first['value'] == 'if'
                else (
                    'ElifStatement'
                    if first['value'] == 'elif'
                    else 'WhileStatement'
                )
            )

            expr_parser = ExpressionParser(
                line_tokens[1:]
            )

            cond_ast = expr_parser.parse()

            node.update({
                'type': kind,
                'condition': cond_ast,
                'body': []
            })

            parent['body'].append(node)

            stack.append(
                (first['indent'], node)
            )

        # ------------------------------
        # else
        # ------------------------------

        elif (
            first['type'] == 'KEYWORDS'
            and first['value'] == 'else'
        ):
            node.update({
                'type': 'ElseStatement',
                'body': []
            })

            parent['body'].append(node)

            stack.append(
                (first['indent'], node)
            )

        # ------------------------------
        # for
        # ------------------------------

        elif (
            first['type'] == 'KEYWORDS'
            and first['value'] == 'for'
        ):
            if (
                len(line_tokens) < 4
                or line_tokens[1]['type'] != 'ID'
                or line_tokens[2]['value'] != 'in'
            ):
                raise Exception(
                    f"構文エラー (行 {first['line']}): "
                    f"for 変数 in 範囲 の形式で書いてください。"
                )

            loop_var = line_tokens[1]['value']

            expr_parser = ExpressionParser(
                line_tokens[3:]
            )

            range_ast = expr_parser.parse()

            node.update({
                'type': 'ForStatement',
                'variable': loop_var,
                'range': range_ast,
                'body': []
            })

            parent['body'].append(node)

            stack.append(
                (first['indent'], node)
            )

        # ------------------------------
        # break / continue / return
        # ------------------------------

        elif (
            first['type'] == 'KEYWORDS'
            and first['value']
            in ['break', 'continue', 'return']
        ):
            val = first['value']

            if val == 'return':
                if len(line_tokens) > 1:
                    expr_parser = ExpressionParser(
                        line_tokens[1:]
                    )

                    node.update({
                        'type': 'ReturnStatement',
                        'value': expr_parser.parse()
                    })
                else:
                    node.update({
                        'type': 'ReturnStatement',
                        'value': None
                    })

            else:
                node.update({
                    'type': val.upper() + 'Statement'
                })

            parent['body'].append(node)

        # ------------------------------
        # ps
        # ------------------------------

        elif (
            first['type'] == 'ID'
            and first['value'] == 'ps'
        ):
            expr_parser = ExpressionParser(
                line_tokens[1:]
            )

            node.update({
                'type': 'PrintStatement',
                'value': expr_parser.parse()
            })

            parent['body'].append(node)

        # ------------------------------
        # 変数代入 / 配列要素変更
        # ------------------------------

        elif first['type'] == 'ID':

            if (
                len(line_tokens) > 1
                and line_tokens[1]['type'] == 'LBRACK'
            ):
                idx_end = 1

                while (
                    idx_end < len(line_tokens)
                    and line_tokens[idx_end]['type']
                    != 'RBRACK'
                ):
                    idx_end += 1

                if (
                    idx_end + 1 < len(line_tokens)
                    and line_tokens[idx_end + 1]['value'] == '='
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
                        'type': 'ArrayAssignStatement',
                        'name': first['value'],
                        'index': idx_ast,
                        'value': val_ast
                    })

                    parent['body'].append(node)

                    continue

            if (
                len(line_tokens) > 1
                and line_tokens[1]['type'] == 'OP'
                and line_tokens[1]['value'] == '='
            ):
                expr_parser = ExpressionParser(
                    line_tokens[2:]
                )

                node.update({
                    'type': 'AssignmentExpression',
                    'variable': first['value'],
                    'value': expr_parser.parse()
                })

                parent['body'].append(node)

            else:
                expr_parser = ExpressionParser(
                    line_tokens
                )

                node.update({
                    'type': 'ExpressionStatement',
                    'value': expr_parser.parse()
                })

                parent['body'].append(node)

    return root


# ====================================================
# 3. コード生成（Code Generator）
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

        if node['type'] == 'Literal':
            return node['value']

        elif node['type'] == 'Variable':
            return node['value']

        elif node['type'] == 'UnaryExpr':
            return (
                f"({node['op']}"
                f"{to_cpp_expr(node['right'])})"
            )

        elif node['type'] == 'BinaryExpr':

            left_str = to_cpp_expr(
                node['left']
            )

            right_str = to_cpp_expr(
                node['right']
            )

            if (
                node['op'] == '+'
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

        elif node['type'] == 'ArrayLiteral':

            elements = ", ".join(
                [
                    to_cpp_expr(e)
                    for e in node['elements']
                ]
            )

            return f"{{{elements}}}"

        elif node['type'] == 'IndexExpr':

            return (
                f"{node['name']}"
                f"[{to_cpp_expr(node['index'])}]"
            )

        elif node['type'] == 'CallExpr':

            args = ", ".join(
                [
                    to_cpp_expr(a)
                    for a in node['args']
                ]
            )

            return (
                f"{node['name']}"
                f"({args})"
            )

        return ""

    def walk(nodes, indent_str="    "):

        lines = []

        for node in nodes:

            # ------------------------------
            # 変数代入
            # ------------------------------

            if node['type'] == 'AssignmentExpression':

                var = node['variable']
                val_node = node['value']

                cpp_val = to_cpp_expr(
                    val_node
                )

                if var in declared_vars:

                    lines.append(
                        f"{indent_str}"
                        f"{var} = {cpp_val};"
                    )

                else:

                    if val_node['type'] == 'ArrayLiteral':

                        is_string = False

                        if val_node['elements']:

                            first_elem = (
                                val_node['elements'][0]
                            )

                            if (
                                first_elem['type']
                                == 'Literal'
                                and first_elem['value']
                                .startswith('"')
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
                        val_node['type'] == 'Literal'
                        and val_node['value']
                        .startswith('"')
                    ):
                        lines.append(
                            f"{indent_str}"
                            f"string {var} = "
                            f"{cpp_val};"
                        )

                    elif (
                        val_node['type'] == 'CallExpr'
                        and val_node['name'] == 'input'
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

            # ------------------------------
            # 配列要素変更
            # ------------------------------

            elif node['type'] == 'ArrayAssignStatement':

                lines.append(
                    f"{indent_str}"
                    f"{node['name']}"
                    f"[{to_cpp_expr(node['index'])}]"
                    f" = "
                    f"{to_cpp_expr(node['value'])};"
                )

            # ------------------------------
            # ps
            # ------------------------------

            elif node['type'] == 'PrintStatement':

                lines.append(
                    f"{indent_str}"
                    f"cout << "
                    f"{to_cpp_expr(node['value'])}"
                    f" << endl;"
                )

            # ------------------------------
            # if
            # ------------------------------

            elif node['type'] == 'IfStatement':

                lines.append(
                    f"{indent_str}"
                    f"if ({to_cpp_expr(node['condition'])}) {{"
                )

                lines.extend(
                    walk(
                        node['body'],
                        indent_str + "    "
                    )
                )

                lines.append(
                    f"{indent_str}}}"
                )

            # ------------------------------
            # elif
            # ------------------------------

            elif node['type'] == 'ElifStatement':

                lines.append(
                    f"{indent_str}"
                    f"else if "
                    f"({to_cpp_expr(node['condition'])}) {{"
                )

                lines.extend(
                    walk(
                        node['body'],
                        indent_str + "    "
                    )
                )

                lines.append(
                    f"{indent_str}}}"
                )

            # ------------------------------
            # else
            # ------------------------------

            elif node['type'] == 'ElseStatement':

                lines.append(
                    f"{indent_str}"
                    f"else {{"
                )

                lines.extend(
                    walk(
                        node['body'],
                        indent_str + "    "
                    )
                )

                lines.append(
                    f"{indent_str}}}"
                )

            # ------------------------------
            # while
            # ------------------------------

            elif node['type'] == 'WhileStatement':

                lines.append(
                    f"{indent_str}"
                    f"while "
                    f"({to_cpp_expr(node['condition'])}) {{"
                )

                lines.extend(
                    walk(
                        node['body'],
                        indent_str + "    "
                    )
                )

                lines.append(
                    f"{indent_str}}}"
                )

            # ------------------------------
            # for
            # ------------------------------

            elif node['type'] == 'ForStatement':

                rng_str = to_cpp_expr(
                    node['range']
                )

                lines.append(
                    f"{indent_str}"
                    f"for (auto& "
                    f"{node['variable']} : "
                    f"{rng_str}) {{"
                )

                lines.extend(
                    walk(
                        node['body'],
                        indent_str + "    "
                    )
                )

                lines.append(
                    f"{indent_str}}}"
                )

            # ------------------------------
            # break
            # ------------------------------

            elif node['type'] == 'BREAKStatement':

                lines.append(
                    f"{indent_str}break;"
                )

            # ------------------------------
            # continue
            # ------------------------------

            elif node['type'] == 'CONTINUEStatement':

                lines.append(
                    f"{indent_str}continue;"
                )

            # ------------------------------
            # return
            # ------------------------------

            elif node['type'] == 'ReturnStatement':

                ret_val = (
                    to_cpp_expr(node['value'])
                    if node['value']
                    else ""
                )

                lines.append(
                    f"{indent_str}"
                    f"return {ret_val};"
                )

            # ------------------------------
            # func
            # ------------------------------

            elif node['type'] == 'FunctionDef':

                t_params = ", ".join(
                    [
                        f"typename T_{p}"
                        for p in node['params']
                    ]
                )

                param_str = ", ".join(
                    [
                        f"T_{p} {p}"
                        for p in node['params']
                    ]
                )

                func_lines = [
                    f"template <{t_params}>",
                    f"auto {node['name']}({param_str}) {{"
                ]

                func_lines.extend(
                    walk(
                        node['body'],
                        "    "
                    )
                )

                func_lines.append("}")

                functions_buf.append(
                    "\n".join(func_lines)
                )

            # ------------------------------
            # 式
            # ------------------------------

            elif node['type'] == 'ExpressionStatement':

                lines.append(
                    f"{indent_str}"
                    f"{to_cpp_expr(node['value'])};"
                )

        return lines

    main_body = walk(
        ast['body']
    )

    final_code = (
        cpp_lines
        + [""]
        + functions_buf
        + [""]
        + ["int main() {"]
        + main_body
        + ["    return 0;", "}"]
    )

    return "\n".join(
        final_code
    )


# ====================================================
# API実行ルート
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

        # 1. 字句解析
        tokens = tokenize(
            user_code
        )

        # 2. 構文解析
        ast = parse(
            tokens
        )

        # 3. C++コード生成
        cpp_code = generate_cpp(
            ast
        )

        with open(
            "main.cpp",
            "w"
        ) as f:
            f.write(
                cpp_code
            )

        # 4. C++コンパイル
        compile_res = subprocess.run(
            [
                "g++",
                "-std=c++17",
                "main.cpp",
                "-o",
                "prog"
            ],
            capture_output=True,
            text=True
        )

        if compile_res.returncode != 0:

            err_msg = compile_res.stderr

            if "was not declared" in err_msg:

                raise Exception(
                    "ps型・未定義エラー: "
                    "使用された変数が定義されていないか、"
                    "不適切な演算が行われました。"
                )

            else:

                raise Exception(
                    "C++内部コンパイルエラー:\n"
                    + err_msg
                )

        # 5. 実行
        result = subprocess.run(
            ["./prog"],
            capture_output=True,
            text=True,
            check=True
        )

        return jsonify({
            "success": True,
            "output": result.stdout,
            "cpp": cpp_code
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        })


# ====================================================
# Discord API
# ====================================================

@app.route("/discord/status", methods=["GET"])
def discord_status():

    if not DISCORD_AVAILABLE:
        return jsonify({
            "success": False,
            "running": False,
            "available": False,
            "error": "discord_runtime.py が読み込めません。"
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


@app.route("/discord/start", methods=["POST"])
def discord_start():

    if not DISCORD_AVAILABLE:
        return jsonify({
            "success": False,
            "error": "discord_runtime.py が利用できません。"
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
            "error": "Discord Bot Tokenが指定されていません。"
        }), 400

    existing_bot = get_discord_bot()

    if existing_bot is not None:
        return jsonify({
            "success": False,
            "error": "Discord Botはすでに起動しています。"
        }), 409

    try:

        bot = DiscordBot(
            token
        )

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
                    set_discord_bot(
                        None
                    )

        thread = threading.Thread(
            target=run_bot,
            daemon=True
        )

        set_discord_bot(
            bot
        )

        thread.start()

        return jsonify({
            "success": True,
            "message": "Discord Botの起動処理を開始しました。"
        })

    except Exception as e:

        set_discord_bot(
            None
        )

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/discord/stop", methods=["POST"])
def discord_stop():

    bot = get_discord_bot()

    if bot is None:
        return jsonify({
            "success": False,
            "error": "起動しているDiscord Botがありません。"
        }), 404

    try:

        bot.stop()

        set_discord_bot(
            None
        )

        return jsonify({
            "success": True,
            "message": "Discord Botを停止しました。"
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/discord/send", methods=["POST"])
def discord_send():

    bot = get_discord_bot()

    if bot is None:
        return jsonify({
            "success": False,
            "error": "Discord Botが起動していません。"
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
            "error": "channel_id が指定されていません。"
        }), 400

    if not content:
        return jsonify({
            "success": False,
            "error": "content が指定されていません。"
        }), 400

    try:

        result = bot.send_message(
            channel_id,
            content
        )

        return jsonify({
            "success": True,
            "message": "Discordへメッセージを送信しました。",
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
