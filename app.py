import os
import subprocess
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# AI（ChatGPT）の準備
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

@app.route("/run", methods=["POST"])
def run_code():
    data = request.get_json()
    user_code = data.get("code", "")

    # AIへの翻訳命令（プロンプト）
    prompt = f"""
あなたはプログラミング言語の超有能な変換器（トランスパイラ）です。
以下の【独自言語のコード】を、全く同じ動作をする、最適化された「C++のソースコード」にのみ翻訳してください。

【独自言語のルール】
- `ps "文字列"` は、C++の `std::cout << "文字列" << std::endl;` に変換する。
- 変数は型指定がない（例: `x = 10`）ので、C++の適切な型（`int` や `double` など）を推測して宣言する。

【独自言語のコード】
{user_code}

【出力ルール】
余計な挨拶、解説、```cpp などのマークアップは一切含めず、C++のコード（文字列）だけを直接出力してください。
"""

    try:
        # AIにC++への翻訳を依頼
        response = client.chat.comilla.create( # 修正
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        cpp_code = response.choices.message.content.strip()

        # AIが作ったC++コードを一時保存
        with open("main.cpp", "w") as f:
            f.write(cpp_code)

        # C++コードを爆速コンパイルして実行
        subprocess.run(["g++", "main.cpp", "-o", "prog"], check=True)
        result = subprocess.run(["./prog"], capture_output=True, text=True, check=True)

        # 結果を返す
        return jsonify({"success": True, "output": result.stdout, "cpp": cpp_code})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
