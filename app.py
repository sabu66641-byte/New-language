import os
import subprocess
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# トップページにアクセスしたときに、あの青いボタンの画面（index.html）を表示する命令
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/run", methods=["POST"])
def run_code():
    data = request.get_json()
    user_code = data.get("code", "")

    # ps "文字" を C++のコードに手動で置き換える
    if user_code.startswith('ps '):
        text = user_code.replace('ps ', '').strip()
        cpp_code = f'#include <iostream>\nint main() {{ std::cout << {text} << std::endl; return 0; }}'
    else:
        cpp_code = '#include <iostream>\nint main() { return 0; }'

    try:
        with open("main.cpp", "w") as f:
            f.write(cpp_code)
            
        subprocess.run(["g++", "main.cpp", "-o", "prog"], check=True)
        result = subprocess.run(["./prog"], capture_output=True, text=True, check=True)

        return jsonify({"success": True, "output": result.stdout, "cpp": cpp_code})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
