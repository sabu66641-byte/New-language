import os
import subprocess
import re
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

# ====================================================
# 【新言語 ps】 最終完全体コンパイラエンジン (v5.0)
# ====================================================

# 1. 字句解析（Lexer）：すべての記号、数式、キーワードを完璧に分解
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
    tok_regex = '|'.join(f'(?P<{name}>{regex})' for name, regex in token_specification)
    tokens = []
    
    for line_num, line in enumerate(source_code.split('\n'), 1):
        if not line.strip() or line.strip().startswith('#'):
            continue
            
        indent_match = re.match(r'^(\s*)', line)
        indent_len = len(indent_match.group(1)) if indent_match else 0
        if indent_len % 4 != 0:
            raise Exception(f"インデントエラー (行 {line_num}): 半角スペース4つ単位で字下げしてください。")
        indent_level = indent_len // 4
        
        for mo in re.finditer(tok_regex, line):
            kind = mo.lastgroup
            value = mo.group()
            if kind == 'COMMENT' or kind == 'SKIP':
                continue
            elif kind == 'MISMATCH':
                raise Exception(f"構文エラー (行 {line_num}): 不正な文字 '{value}' があります。")
            tokens.append({'type': kind, 'value': value, 'line': line_num, 'indent': indent_level})
        tokens.append({'type': 'NL', 'value': '\n', 'line': line_num, 'indent': indent_level})
    return tokens

# 2-A. 式解析エンジン（ExpressionParser）：数式、カッコ、論理演算、関数呼び出しをAST化
class ExpressionParser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        if self.pos < len(self.tokens) and self.tokens[self.pos]['type'] != 'NL':
            return self.tokens[self.pos]
        return None

    def consume(self, expected_type=None):
        tok = self.peek()
        if tok and (expected_type is None or tok['type'] == expected_type):
            self.pos += 1
            return tok
        return None

    def parse(self):
        return self.parse_or()

    def parse_or(self):
        node = self.parse_and()
        while self.peek() and self.peek()['type'] == 'LOGIC' and self.peek()['value'] == 'or':
            self.consume()
            right = self.parse_and()
            node = {'type': 'BinaryExpr', 'op': '||', 'left': node, 'right': right}
        return node

    def parse_and(self):
        node = self.parse_equality()
        while self.peek() and self.peek()['type'] == 'LOGIC' and self.peek()['value'] == 'and':
            self.consume()
            right = self.parse_equality()
            node = {'type': 'BinaryExpr', 'op': '&&', 'left': node, 'right': right}
        return node

    def parse_equality(self):
        node = self.parse_expr()
        while self.peek() and self.peek()['type'] == 'OP' and self.peek()['value'] in ['==', '!=', '<', '>', '<=', '>=']:
            op = self.consume()['value']
            right = self.parse_expr()
            node = {'type': 'BinaryExpr', 'op': op, 'left': node, 'right': right}
        return node

    def parse_expr(self):
        node = self.parse_term()
        while self.peek() and self.peek()['type'] == 'OP' and self.peek()['value'] in ['+', '-']:
            op = self.consume()['value']
            right = self.parse_term()
            node = {'type': 'BinaryExpr', 'op': op, 'left': node, 'right': right}
        return node

    def parse_term(self):
        node = self.parse_factor()
        while self.peek() and self.peek()['type'] == 'OP' and self.peek()['value'] in ['*', '/', '%']:
            op = self.consume()['value']
            right = self.parse_factor()
            node = {'type': 'BinaryExpr', 'op': op, 'left': node, 'right': right}
        return node

    def parse_factor(self):
        tok = self.peek()
        if not tok: return None
        
        if tok['type'] == 'NUMBER' or tok['type'] == 'STRING':
            return {'type': 'Literal', 'value': self.consume()['value']}
            
        elif tok['type'] == 'LBRACK': # 配列リテラル [1, 2, 3]
            self.consume('LBRACK')
            elements = []
            while self.peek() and self.peek()['type'] != 'RBRACK':
                elements.append(self.parse())
                if self.peek() and self.peek()['type'] == 'COMMA':
                    self.consume('COMMA')
            self.consume('RBRACK')
            return {'type': 'ArrayLiteral', 'elements': elements}
            
        elif tok['type'] == 'ID':
            name = self.consume()['value']
            if self.peek() and self.peek()['type'] == 'LPAREN': # 関数呼び出し len(x) など
                self.consume('LPAREN')
                args = []
                while self.peek() and self.peek()['type'] != 'RPAREN':
                    args.append(self.parse())
                    if self.peek() and self.peek()['type'] == 'COMMA':
                        self.consume('COMMA')
                self.consume('RPAREN')
                return {'type': 'CallExpr', 'name': name, 'args': args}
            
            if self.peek() and self.peek()['type'] == 'LBRACK': # 配列の要素取得 arr[0]
                self.consume('LBRACK')
                index_node = self.parse()
                self.consume('RBRACK')
                return {'type': 'IndexExpr', 'name': name, 'index': index_node}
                
            return {'type': 'Variable', 'value': name}
            
        elif tok['type'] == 'LPAREN':
            self.consume('LPAREN')
            node = self.parse()
            self.consume('RPAREN')
            return node
            
        elif tok['type'] == 'LOGIC' and tok['value'] == 'not':
            self.consume()
            return {'type': 'UnaryExpr', 'op': '!', 'right': self.parse_factor()}
            
        return None

# 2-B. メイン構文解析（Parser）：式解析器を完全接続し、全体の構造木（AST）を作る
def parse(tokens):
    root = {'type': 'Program', 'body': []}
    if not tokens: return root
    
    stack = [(-1, root)]
    i = 0
    
    while i < len(tokens):
        token = tokens[i]
        if token['type'] == 'NL':
            i += 1
            continue
            
        # stack[-1][0] にしてインデントの数値と正しく比較する
        while stack and token['indent'] <= stack[-1][0]:
            stack.pop()
            
        parent = stack[-1][1] # タプルのノード側(1番目)を正しく取得
        node = {'line': token['line']}
        
        line_tokens = []
        while i < len(tokens) and tokens[i]['type'] != 'NL':
            line_tokens.append(tokens[i])
            i += 1
        if i < len(tokens): i += 1
        
        if not line_tokens: continue
        # 【★AI指摘のバグ修正！】リストの最初のトークン（0番目）を正しく取得
        first = line_tokens[0]
        
        # --- func (関数定義) ---
        if first['type'] == 'KEYWORDS' and first['value'] == 'func':
            if len(line_tokens) < 2 or line_tokens[1]['type'] != 'ID':
                raise Exception(f"構文エラー (行 {first['line']}): func の後ろに関数名が必要です。")
            func_name = line_tokens[1]['value']
            
            params = []
            p_idx = 2
            while p_idx < len(line_tokens):
                if line_tokens[p_idx]['type'] == 'ID':
                    params.append(line_tokens[p_idx]['value'])
                p_idx += 1
                
            node.update({'type': 'FunctionDef', 'name': func_name, 'params': params, 'body': []})
            parent['body'].append(node)
            stack.append((first['indent'], node))
            
        # --- if / elif / while ---
        elif first['type'] == 'KEYWORDS' and first['value'] in ['if', 'elif', 'while']:
            kind = 'IfStatement' if first['value'] == 'if' else ('ElifStatement' if first['value'] == 'elif' else 'WhileStatement')
            expr_parser = ExpressionParser(line_tokens[1:])
            cond_ast = expr_parser.parse()
            node.update({'type': kind, 'condition': cond_ast, 'body': []})
            parent['body'].append(node)
            stack.append((first['indent'], node))
            
        # --- else ---
        elif first['type'] == 'KEYWORDS' and first['value'] == 'else':
            node.update({'type': 'ElseStatement', 'body': []})
            parent['body'].append(node)
            stack.append((first['indent'], node))
            
        # --- for ---
        elif first['type'] == 'KEYWORDS' and first['value'] == 'for':
            if len(line_tokens) < 4 or line_tokens[1]['type'] != 'ID' or line_tokens[2]['value'] != 'in':
                raise Exception(f"構文エラー (行 {first['line']}): for 変数 in 範囲 の形式で書いてください。")
            loop_var = line_tokens[1]['value']
            expr_parser = ExpressionParser(line_tokens[3:])
            range_ast = expr_parser.parse()
            node.update({'type': 'ForStatement', 'variable': loop_var, 'range': range_ast, 'body': []})
            parent['body'].append(node)
            stack.append((first['indent'], node))
            
        # --- break / continue / return ---
        elif first['type'] == 'KEYWORDS' and first['value'] in ['break', 'continue', 'return']:
            val = first['value']
            if val == 'return':
                if len(line_tokens) > 1:
                    expr_parser = ExpressionParser(line_tokens[1:])
                    node.update({'type': 'ReturnStatement', 'value': expr_parser.parse()})
                else:
                    node.update({'type': 'ReturnStatement', 'value': None})
            else:
                node.update({'type': val.upper() + 'Statement'})
            parent['body'].append(node)
            
        # --- ps (出力) ---
        elif first['type'] == 'ID' and first['value'] == 'ps':
            expr_parser = ExpressionParser(line_tokens[1:])
            node.update({'type': 'PrintStatement', 'value': expr_parser.parse()})
            parent['body'].append(node)
            
        # --- 変数代入 / 配列要素変更 ---
        elif first['type'] == 'ID':
            if len(line_tokens) > 1 and line_tokens[1]['type'] == 'LBRACK':
                idx_end = 1
                while idx_end < len(line_tokens) and line_tokens[idx_end]['type'] != 'RBRACK':
                    idx_end += 1
                if idx_end + 1 < len(line_tokens) and line_tokens[idx_end + 1]['value'] == '=':
                    expr_parser = ExpressionParser(line_tokens[2:idx_end])
                    idx_ast = expr_parser.parse()
                    expr_parser2 = ExpressionParser(line_tokens[idx_end+2:])
                    val_ast = expr_parser2.parse()
                    node.update({'type': 'ArrayAssignStatement', 'name': first['value'], 'index': idx_ast, 'value': val_ast})
                    parent['body'].append(node)
                    continue
            
            if len(line_tokens) > 1 and line_tokens[1]['type'] == 'OP' and line_tokens[1]['value'] == '=':
                expr_parser = ExpressionParser(line_tokens[2:])
                node.update({'type': 'AssignmentExpression', 'variable': first['value'], 'value': expr_parser.parse()})
                parent['body'].append(node)
            else:
                expr_parser = ExpressionParser(line_tokens)
                node.update({'type': 'ExpressionStatement', 'value': expr_parser.parse()})
                parent['body'].append(node)
                
    return root

# 3. コード生成（Code Generator）：ASTを再帰的に巡回し、完璧なC++コードに変換
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
        if not node: return ""
        if node['type'] == 'Literal':
            return node['value']
        elif node['type'] == 'Variable':
            return node['value']
        elif node['type'] == 'UnaryExpr':
            return f"({node['op']}{to_cpp_expr(node['right'])})"
        elif node['type'] == 'BinaryExpr':
            left_str = to_cpp_expr(node['left'])
            right_str = to_cpp_expr(node['right'])
            if node['op'] == '+' and (left_str.startswith('"') or right_str.startswith('"')):
                if left_str.startswith('"'): left_str = f"string({left_str})"
                if right_str.startswith('"'): right_str = f"string({right_str})"
            return f"({left_str} {node['op']} {right_str})"
        elif node['type'] == 'ArrayLiteral':
            elements = ", ".join([to_cpp_expr(e) for e in node['elements']])
            return f"{{{elements}}}"
        elif node['type'] == 'IndexExpr':
            return f"{node['name']}[{to_cpp_expr(node['index'])}]"
        elif node['type'] == 'CallExpr':
            args = ", ".join([to_cpp_expr(a) for a in node['args']])
            return f"{node['name']}({args})"
        return ""

    def walk(nodes, indent_str="    "):
        lines = []
        for node in nodes:
            if node['type'] == 'AssignmentExpression':
                var = node['variable']
                val_node = node['value']
                cpp_val = to_cpp_expr(val_node)
                
                if var in declared_vars:
                    lines.append(f"{indent_str}{var} = {cpp_val};")
                else:
                    if val_node['type'] == 'ArrayLiteral':
                        if val_node['elements'] and val_node['elements'][0]['type'] == 'Literal' and val_node['elements'][0]['value'].startswith('"'):
                            type_str = "vector<string>"
                        else:
                            type_str = "vector<int>"
                        lines.append(f"{indent_str}{type_str} {var} = {cpp_val};")
                    elif val_node['type'] == 'Literal' and val_node['value'].startswith('"'):
                        lines.append(f"{indent_str}string {var} = {cpp_val};")
                    elif val_node['type'] == 'CallExpr' and val_node['name'] == 'input':
                        lines.append(f"{indent_str}string {var} = {cpp_val};")
                    else:
                        lines.append(f"{indent_str}auto {var} = {cpp_val};")
                    declared_vars.add(var)
                    
            elif node['type'] == 'ArrayAssignStatement':
                lines.append(f"{indent_str}{node['name']}[{to_cpp_expr(node['index'])}] = {to_cpp_expr(node['value'])};")
            elif node['type'] == 'PrintStatement':
                lines.append(f"{indent_str}cout << {to_cpp_expr(node['value'])} << endl;")
            elif node['type'] == 'IfStatement':
                lines.append(f"{indent_str}if ({to_cpp_expr(node['condition'])}) {{")
                lines.extend(walk(node['body'], indent_str + "    "))
                lines.append(f"{indent_str}}}")
            elif node['type'] == 'ElifStatement':
                lines.append(f"{indent_str}else if ({to_cpp_expr(node['condition'])}) {{")
                lines.extend(walk(node['body'], indent_str + "    "))
                lines.append(f"{indent_str}}}")
            elif node['type'] == 'ElseStatement':
                lines.append(f"{indent_str}else {{")
                lines.extend(walk(node['body'], indent_str + "    "))
                lines.append(f"{indent_str}}}")
            elif node['type'] == 'WhileStatement':
                lines.append(f"{indent_str}while ({to_cpp_expr(node['condition'])}) {{")
                lines.extend(walk(node['body'], indent_str + "    "))
                lines.append(f"{indent_str}}}")
            elif node['type'] == 'ForStatement':
                rng_str = to_cpp_expr(node['range'])
                lines.append(f"{indent_str}for (auto& {node['variable']} : {rng_str}) {{")
                lines.extend(walk(node['body'], indent_str + "    "))
                lines.append(f"{indent_str}}}")
            elif node['type'] == 'BREAKStatement':
                lines.append(f"{indent_str}break;")
            elif node['type'] == 'CONTINUEStatement':
                lines.append(f"{indent_str}continue;")
            elif node['type'] == 'ReturnStatement':
                ret_val = to_cpp_expr(node['value']) if node['value'] else ""
                lines.append(f"{indent_str}return {ret_val};")
            elif node['type'] == 'FunctionDef':
                # C++17の型制限を突破するため、本格的なC++テンプレートを自動生成
                t_params = ", ".join([f"typename T_{p}" for p in node['params']])
                param_str = ", ".join([f"T_{p} {p}" for p in node['params']])
                func_lines = [
                    f"template <{t_params}>",
                    f"auto {node['name']}({param_str}) {{"
                ]
                func_lines.extend(walk(node['body'], "    "))
                func_lines.append("}")
                functions_buf.append("\n".join(func_lines))
            elif node['type'] == 'ExpressionStatement':
                lines.append(f"{indent_str}{to_cpp_expr(node['value'])};")
        return lines

    main_body = walk(ast['body'])
    final_code = cpp_lines + [""] + functions_buf + ["", "int main() {"] + main_body + ["    return 0;", "}"]
    return "\n".join(final_code)

# ====================================================
# API実行ルート
# ====================================================
@app.route("/run", methods=["POST"])
def run_code():
    data = request.get_json()
    user_code = data.get("code", "")

    try:
        tokens = tokenize(user_code)       # 1. 字句解析
        ast = parse(tokens)                # 2. 構文解析 & AST生成
        cpp_code = generate_cpp(ast)       # 3. C++コード生成

        with open("main.cpp", "w") as f:
            f.write(cpp_code)
            
        compile_res = subprocess.run(["g++", "-std=c++17", "main.cpp", "-o", "prog"], capture_output=True, text=True)
        if compile_res.returncode != 0:
            err_msg = compile_res.stderr
            if "was not declared" in err_msg:
                raise Exception("ps型・未定義エラー: 使用された変数が定義されていないか、不適切な演算が行われました。")
            else:
                raise Exception(f"C++内部コンパイルエラー:\n{err_msg}")
            
        result = subprocess.run(["./prog"], capture_output=True, text=True, check=True)
        return jsonify({"success": True, "output": result.stdout, "cpp": cpp_code})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
