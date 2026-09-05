import os
import subprocess
import re
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

# ====================================================
# 【新言語 ps】 ガチのコンパイラエンジン
# ====================================================

# 1. 字句解析（Lexer）：コードを意味のある最小単位（トークン）にバラバラにする
def tokenize(source_code):
    tokens = []
    # 行ごとに分割して解析
    for line in source_code.split('\n'):
        line = line.strip()
        if not line: continue
        
        # 変数代入のパターン (例: x = 10)
        if '=' in line and not line.startswith('ps'):
            left, right = line.split('=', 1)
            tokens.append({'type': 'ASSIGN', 'var': left.strip(), 'val': right.strip()})
        # 出力命令のパターン (例: ps "Hello" や ps x + 5)
        elif line.startswith('ps '):
            content = line[3:].strip()
            tokens.append({'type': 'PRINT', 'content': content})
        else:
            tokens.append({'type': 'UNKNOWN', 'content': line})
    return tokens

# 2. 構文解析（Parser）＆ AST（抽象構文木）生成
# トークンの並びを解析して、プログラムの構造（ツリー型データ）を構築する
def parse(tokens):
    ast = {
        'type': 'Program',
        'body': []
    }
    for token in tokens:
        if token['type'] == 'ASSIGN':
            ast['body'].append({
                'type': 'AssignmentExpression',
                'variable': token['var'],
                'value': token['val']
            })
        elif token['type'] == 'PRINT':
            ast['body'].append({
                'type': 'PrintStatement',
                'value': token['content']
            })
    return ast

# 3. コード生成（Code Generator）：ASTを元に、PCに優しいC++コードを組み立てる
def generate_cpp(ast):
    cpp_lines = []
    cpp_lines.append("#include <iostream>")
    cpp_lines.append("#include <string>")
    cpp_lines.append("int main() {")
    
    # ASTのノードを1つずつC++の命令に翻訳していく
    for node in ast['body']:
        if node['type'] == 'AssignmentExpression':
            val = node['value']
            # 値が文字か数字かを自動判定（型推論）
            if val.startswith('"') and val.endswith('"'):
                cpp_lines.append(f"    std::string {node['variable']} = {val};")
            else:
                cpp_lines.append(f"    auto {node['variable']} = {val};")
                
        elif node['type'] == 'PrintStatement':
            cpp_lines.append(f"    std::cout << {node['value']} << std::endl;")
            
    cpp_lines.append("    return 0;")
    cpp_lines.append("}")
    return "\n".join(cpp_lines)

# ====================================================
# 実行用ルート
# ====================================================
@app.route("/run", methods=["POST"])
def run_code():
    data = request.get_json()
    user_code = data.get("code", "")

    try:
        # 【本格言語の全ステップを実行】
        tokens = tokenize(user_code)       # 1. 字句解析
        ast = parse(tokens)                # 2. 構文解析 & AST生成
        cpp_code = generate_cpp(ast)       # 3. C++コード生成

        # C++コードを保存して、Render上で爆速コンパイル＆実行
        with open("main.cpp", "w") as f:
            f.write(cpp_code)
            
        subprocess.run(["g++", "main.cpp", "-o", "prog"], check=True)
        result = subprocess.run(["./prog"], capture_output=True, text=True, check=True)

        return jsonify({"success": True, "output": result.stdout, "cpp": cpp_code})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
