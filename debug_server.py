from flask import Flask, render_template_string, request, jsonify
import requests
import time
import logging

app = Flask(__name__)

# 配置信息
# USER_SERVER_URL = "http://127.0.0.1:9001/user_profile"
USER_SERVER_URL = "https://api.mindora316.com/user_server/user_profile"
TEST_UID = "test_debug_user_001"

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Mindora 调试控制台</title>
    <style>
        body { font-family: sans-serif; margin: 40px; background: #f4f7f6; color: #333; }
        .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin-bottom: 20px; }
        label { display: block; margin-top: 15px; font-weight: bold; }
        select, input { width: 100%; padding: 10px; margin-top: 5px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        button { margin-top: 20px; padding: 12px 24px; cursor: pointer; border: none; border-radius: 4px; font-weight: bold; transition: 0.3s; }
        .btn-update { background: #28a745; color: white; }
        .btn-update:hover { background: #218838; }
        .btn-fetch { background: #007bff; color: white; }
        .btn-fetch:hover { background: #0069d9; }
        #result { white-space: pre-wrap; background: #272822; color: #f8f8f2; padding: 15px; margin-top: 20px; border-radius: 4px; font-family: monospace; max-height: 400px; overflow: auto; }
        .scenario-card { border: 1px solid #007bff; border-radius: 6px; padding: 15px; margin-top: 15px; background: #f0f7ff; }
        .stage-item { background: white; margin: 5px 0; padding: 8px; border-radius: 4px; font-size: 0.9em; border-left: 3px solid #007bff; }
    </style>
</head>
<body>
    <h1>🌙 Mindora 推荐引擎调试器</h1>
    
    <div class="card">
        <h3>1. 更新测试画像 (Update Profile)</h3>
        <form id="updateForm">
            <label>睡眠类型 (User Type)</label>
            <select name="user_type">
                <option value="ONSET">ONSET (入睡困难)</option>
                <option value="MAINTE">MAINTE (睡眠维持困难)</option>
                <option value="TER">TER (早醒)</option>
                <option value="DSWPD">DSWPD (睡眠周期推迟)</option>
                <option value="ASWPD">ASWPD (睡眠周期提前)</option>
                <option value="ISWRD">ISWRD (不规则睡眠节律)</option>
                <option value="PTSD">PTSD (创伤后应激障碍)</option>
            </select>
            
            <label>压力指数 (Stress Index: 0.0 ~ 1.0)</label>
            <input type="number" name="stress_index" step="0.05" value="0.5" min="0" max="1">
            
            <button type="button" class="btn-update" onclick="updateProfile()">发送更新请求</button>
        </form>
    </div>

    <div class="card">
        <h3>2. 推荐候选展示 (Sleep Scenarios)</h3>
        <button class="btn-fetch" onclick="fetchScenarios()">获取最新推荐序列</button>
        <div id="scenarioDisplay"></div>
        <div id="result">控制台输出等待中...</div>
    </div>

    <script>
        async function updateProfile() {
            const formData = new FormData(document.getElementById('updateForm'));
            const resultDiv = document.getElementById('result');
            resultDiv.innerText = "Processing...";

            const stressVal = parseFloat(formData.get('stress_index'));
            const userTypeRaw = formData.get('user_type'); // 假设表单值是 "ONSET", "DUR" 等

            // 构造符合要求的 user_type 字符串，例如 "sleep_type_ONSET"
            const sleepTypeStr = `sleep_type_${userTypeRaw}`;

            const payload = {
                "request_type": "update_profile",
                "version": "1.0",
                "timestamp": Math.floor(Date.now() / 1000),
                "data": {
                    "uid": "{{ uid }}",
                    "user_profile": {
                        // 修正 1: 如果后端报错无法将 'ONSET' 转为 float，
                        // 说明该位置必须传数字，或者 Key 应该对应字符串。
                        // 我们将 Key 改为描述性的，Value 保持为数字（或根据后端需求调整）
                        "long_term_profile": [
                            [sleepTypeStr, 1.0], // 如果后端允许字符串
                            ["stress_index", stressVal]
                        ]
                    }
                }
            };

            try {
                const resp = await fetch('/debug/api/proxy', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });

                const resJson = await resp.json();
                resultDiv.innerText = "Update Response:" + JSON.stringify(resJson, null, 2);
            } catch (e) {
                resultDiv.innerText = "Error: " + e;
            }
        }
        async function fetchScenarios() {
            const payload = {
                "request_type": "query_profile",
                "version": "1.0",
                "timestamp": Math.floor(Date.now() / 1000),
                "data": { "uid": "{{ uid }}" }
            };
            
            const resp = await fetch('/debug/api/proxy', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            const resJson = await resp.json();
            
            // 解析返回的 sleep_scenarios
            const scenarios = resJson.data?.user_profile?.sleep_scenarios || [];
            let html = "";
            
            if (scenarios.length === 0) {
                html = "<p>暂无推荐方案，请先执行更新请求。</p>";
            } else {
                scenarios.forEach(s => {
                    html += `<div class="scenario-card">
                        <strong>方案: ${s.scenario_name} (ID: ${s.scenario_id})</strong>
                        <div>` + s.stages.map(st => `
                            <div class="stage-item">
                                <b>${st.stage_name}</b>: 🎵 ${st.audio_file} | 💡 ${st.light_scene} | 🌬️ ${st.aroma_mode}
                            </div>
                        `).join('') + `</div>
                    </div>`;
                });
            }
            
            document.getElementById('scenarioDisplay').innerHTML = html;
            document.getElementById('result').innerText = "Query Response:\\n" + JSON.stringify(resJson, null, 2);
        }
    </script>
</body>
</html>
"""

@app.route('/debug')
def index():
    return render_template_string(HTML_TEMPLATE, uid=TEST_UID)

@app.route('/debug/api/proxy', methods=['POST'])
def proxy():
    # 直接获取前端发来的原始字节流，不解析，直接转发
    raw_data = request.get_data() 
    print(f"raw data {raw_data}")
    try:
        resp = requests.post(
            USER_SERVER_URL, 
            data=raw_data, # 使用 data= 发送原始字节
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        return resp.content, resp.status_code, resp.headers.items()
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500

if __name__ == '__main__':
    # 打印 Flask 内部所有注册成功的 URL
    print("\n--- Flask 路由清单开始 ---")
    for rule in app.url_map.iter_rules():
        print(f"URL: {rule.rule} | Methods: {rule.methods}")
    print("--- Flask 路由清单结束 ---\n")
    
    app.run(host='0.0.0.0', port=5001, debug=True)
    print(f"Debug Web Server 启动，访问: http://127.0.0.1:5001")