import os
import subprocess
import re
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

# ====================================================
# 【新言語 ps】 究極のフル機能コンパイラエンジン (v2.0)
# ====================================================

# 1. 字句解析（Lexer）：コメント(#)の除外、すべてのキーワード・演算子のトークン化
def tokenize(source_code):
    tokens = []
    for line_num, line in enumerate(source_code.split('\n'), 1):
        # コメント（#）の削除
        if '#' in line:
            line = line.split('#', 1)[0]
            
        indent_match = re.match(r'^(\s*)', line)
        indent_level = len(indent_match.group(1)) if indent_match else 0
        cleaned = line.strip()
        if not cleaned: continue

        # 論理演算子のテキスト置換 (and -> &&, or -> ||, not -> !)
        cleaned = re.sub(r'\band\b', '&&', cleaned)
        cleaned = re.sub(r'\bor\b', '||', cleaned)
        cleaned = re.sub(r'\bnot\b', '!', cleaned)

        # 各種構文のトークン識別
        if cleaned.startswith('if '):
            tokens.append({'type': 'IF', 'cond': cleaned[3:].strip(), 'indent': indent_level, 'line': line_num})
        elif cleaned.startswith('elif '):
            tokens.append({'type': 'ELIF', 'cond': cleaned[5:].strip(), 'indent': indent_level, 'line': line_num})
        elif cleaned == 'else':
            tokens.append({'type': 'ELSE', 'indent': indent_level, 'line': line_num})
        elif cleaned.startswith('while '):
            tokens.append({'type': 'WHILE', 'cond': cleaned[6:].strip(), 'indent': indent_level, 'line': line_num})
        elif cleaned.startswith('for ') and ' in ' in cleaned:
            match = re.match(r'^for\s+(\w+)\s+in\s+(.+)$', cleaned)
            if match:
                tokens.append({'type': 'FOR', 'var': match.group(1), 'range': match.group(2), 'indent': indent_level, 'line': line_num})
        elif cleaned == 'break':
            tokens.append({'type': 'BREAK', 'indent': indent_level, 'line': line_num})
        elif cleaned == 'continue':
            tokens.append({'type': 'CONTINUE', 'indent': indent_level, 'line': line_num})
        elif cleaned.startswith('func '):
            match = re.match(r'^func\s+(\w+)\((.*)\)$', cleaned)
            if match:
                tokens.append({'type': 'FUNC', 'name': match.group(1), 'params': match.group(2), 'indent': indent_level, 'line': line_num})
        elif cleaned.startswith('return '):
            tokens.append({'type': 'RETURN', 'val': cleaned[7:].strip(), 'indent': indent_level, 'line': line_num})
        elif cleaned.startswith('ps '):
            tokens.append({'type': 'PRINT', 'val': cleaned[3:].strip(), 'indent': indent_level, 'line': line_num})
        elif '=' in cleaned:
            left, right = cleaned.split('=', 1)
            tokens.append({'type': 'ASSIGN', 'var': left.strip(), 'val': right.strip(), 'indent': indent_level, 'line': line_num})
        else:
            tokens.append({'type': 'EXPR', 'val': cleaned, 'indent': indent_level, 'line': line_num})
            
    return tokens

# 2. 構文解析（Parser）＆ AST（抽象構文木）構築
def parse(tokens):
    root = {'type': 'Program', 'body': []}
    stack = [(-1, root)]

    for i, token in enumerate(tokens):
        if i > 0 and token['indent'] > tokens[i-1]['indent'] and token['indent'] - tokens[i-1]['indent'] != 4:
            raise Exception(f"インデントエラー (行 {token['line']}): 空白4つで字下げしてください。")

        while stack and token['indent'] <= stack[-1][0]:
            stack.pop()

        parent_node = stack[-1][1]
        node = {'type': token['type'], 'line': token['line']}

        if token['type'] == 'ASSIGN':
            node.update({'variable': token['var'], 'value': token['val']})
        elif token['type'] == 'PRINT':
            node.update({'value': token['val']})
        elif token['type'] in ['IF', 'ELIF']:
            node.update({'condition': token['cond'], 'body': []})
        elif token['type'] == 'ELSE':
            node.update({'body': []})
        elif token['type'] == 'WHILE':
            node.update({'condition': token['cond'], 'body': []})
        elif token['type'] == 'FOR':
            node.update({'variable': token['var'], 'range': token['range'], 'body': []})
        elif token['type'] == 'FUNC':
            node.update({'name': token['name'], 'params': token['params'], 'body': []})
        elif token['type'] == 'RETURN':
            node.update({'value': token['val']})
        elif token['type'] == 'EXPR':
            node.update({'value': token['val']})

        if 'body' in parent_node:
            parent_node['body'].append(node)
        else:
            parent_node['body'] = [node]

        if token['type'] in ['IF', 'ELIF', 'ELSE', 'WHILE', 'FOR', 'FUNC']:
            stack.append((token['indent'], node))

    return root

# 3. コード生成（Code Generator）
def generate_cpp(ast):
    cpp_lines = [
        "#include <iostream>",
        "#include <string>",
        "#include <vector>",
        "#include <algorithm>",
        "using namespace std;"
    ]
    
    functions_buf = []
    declared_vars = set()

    def walk(nodes, indent_str="    "):
        lines = []
        for node in nodes:
            if node['type'] == 'ASSIGN':
                var = node['variable']
                val = node['value']
                
                if val.startswith('[') and val.endswith(']'):
                    elements = val[1:-1].strip()
                    if elements and elements.split(',')[0].strip().startswith('"'):
                        type_str = "vector<string>"
                    else:
                        type_str = "vector<int>"
                    
                    if var in declared_vars:
                        lines.append(f"{indent_str}{var} = {type_str}{{{elements}}};")
                    else:
                        lines.append(f"{indent_str}{type_str} {var} = {{{elements}}};")
                        declared_vars.add(var)
                else:
                    if var in declared_vars:
                        lines.append(f"{indent_str}{var} = {val};")
                    else:
                        if val.startswith('"') and val.endswith('"'):
                            lines.append(f"{indent_str}string {var} = {val};")
                        elif val in ['true', 'false']:
                            lines.append(f"{indent_str}bool {var} = {val};")
                        else:
                            lines.append(f"{indent_str}auto {var} = {val};")
                        declared_vars.add(var)
                        
            elif node['type'] == 'PRINT':
                lines.append(f"{indent_str}cout << {node['value']} << endl;")
            elif node['type'] == 'IF':
                lines.append(f"{indent_str}if ({node['condition']}) {{")
                lines.extend(walk(node['body'], indent_str + "    "))
                lines.append(f"{indent_str}}}")
            elif node['type'] == 'ELIF':
                lines.append(f"{indent_str}else if ({node['condition']}) {{")
                lines.extend(walk(node['body'], indent_str + "    "))
                lines.append(f"{indent_str}}}")
            elif node['type'] == 'ELSE':
                lines.append(f"{indent_str}else {{")
                lines.extend(walk(node['body'], indent_str + "    "))
                lines.append(f"{indent_str}}}")
            elif node['type'] == 'WHILE':
                lines.append(f"{indent_str}while ({node['condition']}) {{")
                lines.extend(walk(node['body'], indent_str + "    "))
                lines.append(f"{indent_str}}}")
            elif node['type'] == 'FOR':
                rng = node['range']
                if 'range(' in rng:
                    nums = rng.replace('range(', '').replace(')', '').split(',')
                    start = nums[0].strip()
                    end = nums[1].strip()
                    lines.append(f"{indent_str}for (int {node['variable']} = {start}; {node['variable']} < {end}; ++{node['variable']}) {{")
                else:
                    lines.append(f"{indent_str}for (auto& {node['variable']} : {rng}) {{")
                lines.extend(walk(node['body'], indent_str + "    "))
                lines.append(f"{indent_str}}}")
            elif node['type'] == 'BREAK':
                lines.append(f"{indent_str}break;")
            elif node['type'] == 'CONTINUE':
                lines.append(f"{indent_str}continue;")
            elif node['type'] == 'RETURN':
                lines.append(f"{indent_str}return {node['value']};")
            elif node['type'] == 'FUNC':
                func_lines = [f"auto {node['name']}({node['params']}) {{"]
                func_lines.extend(walk(node['body'], "    "))
                func_lines.append("}")
                functions_buf.append("\n".join(func_lines))
            elif node['type'] == 'EXPR':
                lines.append(f"{indent_str}{node['value']};")
                
        return lines

    main_body = walk(ast['body'])
    final_code = cpp_lines + [""] + functions_buf + ["", "int main() {"] + main_body + ["    return 0;", "}"]
    return "\n".join(final_code)

# ====================================================
# APIルート
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
            raise Exception(f"型または演算エラー (C++コンパイル失敗):\n{compile_res.stderr}")
            
        result = subprocess.run(["./prog"], capture_output=True, text=True, check=True)

        return jsonify({"success": True, "output": result.stdout, "cpp": cpp_code})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
