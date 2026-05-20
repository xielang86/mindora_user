from flask import Flask, render_template_string, request, jsonify
import requests
import time,os,urllib3
from pathlib import Path

app = Flask(__name__)
proxy_vars = ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]
for var in proxy_vars:
    if var in os.environ:
        del os.environ[var]

# 配置信息
# USER_SERVER_URL = "https://api.mindora316.com/user_server/user_profile"
USER_SERVER_URL = "http://127.0.0.1:9001/user_profile"
TEST_UID = "test_debug_user_001"
LOG_DIR = Path(__file__).resolve().parent / "user_server_logs"
LOG_TAIL_BYTES = 32 * 1024

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Mindora 调试控制台</title>
    <style>
        :root {
            --bg: #f8fafc;
            --card: #ffffff;
            --line: #e2e8f0;
            --text: #1e293b;
            --muted: #64748b;
            --accent: #10b981;
            --accent-soft: #ecfdf5;
            --blue: #3b82f6;
            --blue-soft: #eff6ff;
        }
        * { box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; 
            margin: 0; padding: 40px; background: var(--bg); color: var(--text); 
        }
        h1 { font-size: 1.8rem; font-weight: 800; color: #0f172a; margin-bottom: 32px; }
        .card { 
            background: var(--card); padding: 28px; border-radius: 20px; 
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -1px rgba(0,0,0,0.06); 
            margin-bottom: 24px; border: 1px solid var(--line); 
        }
        .editor-grid { display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 32px; align-items: stretch; }
        .field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 20px; }
        .field-card { background: #ffffff; border: 1px solid var(--line); border-radius: 12px; padding: 16px; transition: all 0.2s; }
        .field-card:hover { border-color: var(--accent); background: #fafafa; }
        label { display: block; margin-bottom: 10px; font-weight: 600; font-size: 0.9rem; color: var(--muted); }
        select, input { 
            width: 100%; padding: 12px; border: 1px solid #cbd5e1; border-radius: 8px; 
            background: white; color: var(--text); font-size: 0.95rem;
        }
        select:focus { outline: none; border-color: var(--accent); }
        
        /* 聚合描述面板样式 */
        .desc-panel { 
            background: white; border: 1px solid var(--line); border-radius: 16px; 
            padding: 24px; display: flex; flex-direction: column; position: sticky; top: 20px;
            max-height: 85vh; overflow-y: auto;
        }
        .desc-tag { 
            align-self: flex-start; padding: 4px 12px; border-radius: 6px; 
            background: var(--accent-soft); color: var(--accent); font-size: 0.75rem; 
            font-weight: 700; margin-bottom: 16px;
        }
        .desc-header { font-size: 1.25rem; font-weight: 700; color: #0f172a; margin-bottom: 20px; }
        
        .desc-section { margin-bottom: 20px; border-bottom: 1px solid #f1f5f9; padding-bottom: 15px; }
        .desc-section:last-child { border-bottom: none; }
        .section-label { font-size: 0.85rem; color: var(--accent); font-weight: 700; margin-bottom: 4px; text-transform: uppercase; }
        .section-title { font-size: 1rem; font-weight: 700; color: #334155; margin-bottom: 8px; }
        .section-text { line-height: 1.6; color: var(--muted); font-size: 0.9rem; white-space: pre-wrap; }

        button { padding: 12px 24px; cursor: pointer; border: none; border-radius: 8px; font-weight: 600; transition: 0.2s; font-size: 1rem; }
        .btn-update { background: var(--accent); color: white; width: 100%; margin-top: 24px; }
        .btn-update:hover { filter: brightness(0.9); }
        .btn-fetch { background: var(--blue); color: white; }
        
        #result { 
            white-space: pre-wrap; background: #1e293b; color: #e2e8f0; 
            padding: 20px; margin-top: 20px; border-radius: 12px; font-family: monospace; 
            font-size: 0.85rem; max-height: 400px; overflow: auto; 
        }
        .scenario-card { border: 1px solid var(--line); border-radius: 12px; padding: 20px; margin-top: 16px; background: white; }
        .stage-item { background: #f1f5f9; margin: 10px 0; padding: 12px; border-radius: 8px; border-left: 4px solid var(--blue); }
        .log-toolbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
        .btn-secondary { background: #0f172a; color: white; }
        .status-chip {
            display: inline-flex; align-items: center; min-height: 40px; padding: 0 14px;
            border-radius: 999px; background: #f1f5f9; color: var(--muted); font-size: 0.9rem;
        }
        .log-meta { color: var(--muted); font-size: 0.9rem; margin-bottom: 12px; }
        .log-viewer {
            background: #020617; color: #dbeafe; border-radius: 14px; border: 1px solid #0f172a;
            padding: 18px; min-height: 260px; max-height: 520px; overflow: auto;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 0.82rem; line-height: 1.45; white-space: pre-wrap;
        }
    </style>
</head>
<body>
    <h1>🌙 Mindora 推荐引擎调试器</h1>

    <div class="card">
        <form id="updateForm">
            <div class="editor-grid">
                <div>
                    <h3 style="margin-top:0">1. 配置测试画像</h3>
                    <div class="field-grid" id="fieldGrid"></div>
                    <div style="margin-top: 20px;">
                        <label>压力指数 (Stress Index: 0.0 ~ 1.0)</label>
                        <input type="number" name="stress_index" step="0.05" value="0.5" min="0" max="1" onchange="updateAllDescs()">
                    </div>
                    <button type="button" class="btn-update" onclick="updateProfile()">更新画像并提交</button>
                </div>
                
                <div class="desc-panel">
                    <span class="desc-tag">当前画像合集 (Full Profile Description)</span>
                    <div class="desc-header">画像详细说明</div>
                    <div id="fullDescContainer">
                        </div>
                </div>
            </div>
        </form>
    </div>

    <div class="card">
        <h3>2. 推荐候选展示</h3>
        <button class="btn-fetch" onclick="fetchScenarios()">获取最新推荐序列</button>
        <div id="scenarioDisplay"></div>
        <div id="result">控制台输出等待中...</div>
    </div>

    <div class="card">
        <h3>3. User Server 日志查看</h3>
        <div class="log-toolbar">
            <button type="button" class="btn-secondary" id="toggleLogBtn" onclick="toggleLogViewer()">开启日志显示</button>
            <button type="button" class="btn-fetch" onclick="refreshLogViewer()" id="refreshLogBtn" disabled>立即刷新</button>
            <div class="status-chip" id="logStatus">日志显示未开启</div>
        </div>
        <div class="log-meta" id="logMeta">将从 `user_server_logs/` 中读取最新日志文件，并在开启后每 10 秒自动刷新。</div>
        <pre class="log-viewer" id="logViewer">点击“开启日志显示”后加载最新日志...</pre>
    </div>

    <script>
      const TEST_UID = "{{ uid }}";
      let logViewerEnabled = false;
      let logRefreshTimer = null;
      const profileFieldDefs = [
            {
                name: 'user_type',
                label: '睡眠症状',
                profileKey: 'sleep_symptom',
                useSleepTypeKey: true,
                options: [
                    { value: 'ONSET', title: '入睡困难型 (Onset)', desc: '【睡眠结构】浅睡↑↑、深睡↓↓、REM↓\\n【特征】入睡潜伏期 >30分钟，入睡时体温下降延迟，伴随心率升高。' },
                    { value: 'MAINTE', title: '睡眠维持型 (Maintenance)', desc: '【睡眠结构】浅睡↑、深睡↓↓、REM↓\\n【特征】夜间觉醒次数 >3次，觉醒时长 >30分钟，节律紊乱。' },
                    { value: 'TER', title: '早醒型 (Terminal)', desc: '【睡眠结构】浅睡↑、深睡↓、REM↓\\n【特征】凌晨早醒后无法再入睡，体温节律升高提前。' },
                    { value: 'DSWPD', title: '睡眠时相延迟型 (DSWPD)', desc: '【特征】入睡晚，起床晚。深睡与心率节律整体后移。' },
                    { value: 'ASWPD', title: '睡眠时相提前型 (ASWPD)', desc: '【特征】入睡早，起床早。深睡与心率节律整体前移。' },
                    { value: 'ISWRD', title: '不规律睡眠 (ISWRD)', desc: '【特征】入睡/起床时间无固定规律。体温节律紊乱，无固定峰谷值。' },
                    { value: 'PTSD', title: 'PTSD / 焦虑抑郁共病', desc: '【睡眠结构】REM显著减少。频繁觉醒伴随惊醒、噩梦，心率持续偏高。' }
                ]
            },
            {
                name: 'age_group',
                label: '年龄段',
                profileKey: 'age_group',
                options: [
                    { value: '12_18', title: '12-18 岁', desc: '处于发育期，学业压力常导致作息标准差增大。' },
                    { value: '19_30', title: '19-30 岁', desc: '社交与工作活跃期，睡前电子设备使用频率高。' },
                    { value: '31_45', title: '31-45 岁', desc: '职场与家庭压力叠加，更关注睡眠深度。' },
                    { value: '46_60', title: '46-60 岁', desc: '身体机能变化，维持睡眠的稳定性开始下降。' },
                    { value: '60_PLUS', title: '60 岁 +', desc: '深度睡眠比例自然下降，早醒特征明显。' }
                ]
            },
            {
                name: 'stress_level',
                label: '压力水平',
                profileKey: 'stress_level',
                options: [
                    { value: 'none', title: '无压力', desc: '身心放松，推荐逻辑倾向于常规维护。' },
                    { value: 'light', title: '轻度压力', desc: '存在轻微思虑，侧重于心理暗示类助眠。' },
                    { value: 'medium', title: '中度压力', desc: '有明显的紧张感，推荐策略侧重于生理肌肉放松。' },
                    { value: 'high', title: '重度高压', desc: '处于高度警觉态，推荐强力遮噪及深度呼吸引导。' }
                ]
            },
            {
                name: 'sensitivity',
                label: '敏感度',
                profileKey: 'sensitivity',
                options: [
                    { value: 'light_sensitive', title: '怕光敏感', desc: '光线控制需极其精ines，避免蓝绿光，偏向极暗红光。' },
                    { value: 'sound_sensitive', title: '怕声敏感', desc: '音量衰减需更平滑，环境音需保持低频且持续。' },
                    { value: 'light_sound_sensitive', title: '声光双敏', desc: '全维度低刺激策略，环境变化幅度需控制在极小范围。' },
                    { value: 'not_sensitive', title: '不敏感', desc: '标准环境兼容度，可接受更丰富的音画变化。' }
                ]
            },
            {
                name: 'bedroom_env',
                label: '卧室环境',
                profileKey: 'bedroom_env',
                options: [
                    { value: 'noisy', title: '嘈杂', desc: '外部噪音多。推荐启用更强的背景遮噪音（Pink Noise）。' },
                    { value: 'quiet', title: '安静', desc: '底噪极低。可以尝试更细腻、空间感更强的自然声场。' }
                ]
            }
        ];

        function renderFieldGrid() {
            const root = document.getElementById('fieldGrid');
            root.innerHTML = profileFieldDefs.map(field => `
                <div class="field-card">
                    <label>${field.label}</label>
                    <select name="${field.name}" id="${field.name}" onchange="updateAllDescs()">
                        ${field.options.map(opt => `<option value="${opt.value}">${opt.title}</option>`).join('')}
                    </select>
                </div>
            `).join('');
        }

        function updateAllDescs() {
            const container = document.getElementById('fullDescContainer');
            let html = '';
            profileFieldDefs.forEach(field => {
                const selectElement = document.getElementById(field.name);
                const selectedValue = selectElement.value;
                const option = field.options.find(opt => opt.value === selectedValue);
                if (option) {
                    html += `
                        <div class="desc-section">
                            <div class="section-label">${field.label}</div>
                            <div class="section-title">${option.title}</div>
                            <div class="section-text">${option.desc.replace(/\\\\n/g, '\\n')}</div>
                        </div>
                    `;
                }
            });
            container.innerHTML = html;
        }

        async function updateProfile() {
            const formData = new FormData(document.getElementById('updateForm'));
            const resultDiv = document.getElementById('result');
            resultDiv.innerText = '发送请求中...';
            const stressVal = parseFloat(formData.get('stress_index'));
            const payload = {
                request_type: 'update_profile',
                version: '1.0',
                timestamp: Math.floor(Date.now() / 1000),
                data: {
                    uid: TEST_UID,
                    user_profile: {
                        long_term_profile: buildLongTermProfile(formData, stressVal)
                    }
                }
            };
            try {
                const resp = await fetch('/api/proxy', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const resJson = await resp.json();
                resultDiv.innerText = '提交成功:\\n' + JSON.stringify(resJson, null, 2);
            } catch (e) {
                resultDiv.innerText = '提交失败: ' + e;
            }
        }

        function buildLongTermProfile(formData, stressVal) {
            const values = [];
            profileFieldDefs.forEach(field => {
                const rawValue = formData.get(field.name);
                if (rawValue) {
                    const key = field.useSleepTypeKey ? `sleep_type_${rawValue}` : `${field.profileKey}_${rawValue}`;
                    values.push([key, 1.0]);
                }
            });
            values.push(['stress_index', stressVal]);
            return values;
        }

        async function fetchScenarios() {
            const payload = { request_type: 'query_profile', data: { uid: TEST_UID }, timestamp:  Math.floor(Date.now() / 1000) };
            try {
                const resp = await fetch('/api/proxy', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const resJson = await resp.json();
                
                // 核心修改：在控制台展示完整的画像 JSON
                document.getElementById('result').innerText = '查询成功，完整响应数据：\\n' + JSON.stringify(resJson, null, 2);

                const scenarios = resJson.data?.user_profile?.sleep_scenarios || [];
                let html = '';
                scenarios.forEach(s => {
                    html += `<div class="scenario-card">
                        <strong style="color:var(--blue)">${s.scenario_name}</strong>
                        <div>` + s.stages.map(st => `
                            <div class="stage-item">
                                <b>${st.stage_name}</b>: ${st.audio_file}
                            </div>
                        `).join('') + `</div>
                    </div>`;
                });
                document.getElementById('scenarioDisplay').innerHTML = html || '<p>暂无场景数据</p>';
            } catch (e) {
                document.getElementById('result').innerText = '查询失败: ' + e;
            }
        }

        function setLogStatus(text) {
            document.getElementById('logStatus').innerText = text;
        }

        function scheduleLogRefresh() {
            if (logRefreshTimer) {
                clearInterval(logRefreshTimer);
            }
            if (logViewerEnabled) {
                logRefreshTimer = setInterval(refreshLogViewer, 10000);
            }
        }

        async function refreshLogViewer() {
            if (!logViewerEnabled) {
                return;
            }
            const logViewer = document.getElementById('logViewer');
            const logMeta = document.getElementById('logMeta');
            setLogStatus('日志刷新中...');
            try {
                const resp = await fetch('/api/user-server-log');
                const resJson = await resp.json();
                if (!resp.ok || resJson.code !== 0) {
                    throw new Error(resJson.msg || '日志读取失败');
                }
                logMeta.innerText = `当前日志: ${resJson.filename} | 修改时间: ${resJson.modified_at} | 显示尾部 ${resJson.line_count} 行`;
                logViewer.textContent = resJson.content || '(最新日志为空)';
                logViewer.scrollTop = logViewer.scrollHeight;
                setLogStatus(`日志显示已开启，每 10 秒刷新一次。最近刷新: ${resJson.refreshed_at}`);
            } catch (e) {
                logMeta.innerText = '读取日志失败，请检查 `user_server_logs/` 目录或服务权限。';
                logViewer.textContent = String(e);
                setLogStatus('日志刷新失败');
            }
        }

        function toggleLogViewer() {
            logViewerEnabled = !logViewerEnabled;
            const toggleBtn = document.getElementById('toggleLogBtn');
            const refreshBtn = document.getElementById('refreshLogBtn');
            if (logViewerEnabled) {
                toggleBtn.innerText = '关闭日志显示';
                refreshBtn.disabled = false;
                setLogStatus('日志显示已开启，准备加载...');
                scheduleLogRefresh();
                refreshLogViewer();
            } else {
                toggleBtn.innerText = '开启日志显示';
                refreshBtn.disabled = true;
                if (logRefreshTimer) {
                    clearInterval(logRefreshTimer);
                    logRefreshTimer = null;
                }
                setLogStatus('日志显示未开启');
            }
        }

        renderFieldGrid();
        updateAllDescs();
    </script>
</body>
</html>
"""

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def _read_latest_user_server_log():
    if not LOG_DIR.exists() or not LOG_DIR.is_dir():
        raise FileNotFoundError(f"log dir not found: {LOG_DIR}")

    log_files = [path for path in LOG_DIR.iterdir() if path.is_file()]
    if not log_files:
        raise FileNotFoundError(f"no log files found in {LOG_DIR}")

    latest_log = max(log_files, key=lambda path: path.stat().st_mtime)
    with latest_log.open("rb") as handle:
        file_size = handle.seek(0, os.SEEK_END)
        read_size = min(file_size, LOG_TAIL_BYTES)
        handle.seek(-read_size, os.SEEK_END if file_size else os.SEEK_SET)
        raw_content = handle.read(read_size)

    text = raw_content.decode("utf-8", errors="replace")
    if read_size < file_size:
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]

    return {
        "filename": latest_log.name,
        "modified_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_log.stat().st_mtime)),
        "content": text.strip() or "",
        "line_count": len(text.splitlines()),
        "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, uid=TEST_UID)

@app.route('/api/proxy', methods=['POST'])
def proxy():
    raw_data = request.get_data()
    print(f"--- Forwarding {len(raw_data)} bytes to {USER_SERVER_URL} ---")
    try:
        with requests.Session() as s:
            s.trust_env = False 
            resp = s.post(
                USER_SERVER_URL,
                data=raw_data,
                headers={'Content-Type': 'application/json'},
                timeout=15,
                verify=False
            )
        print(f"--- Backend Response [{resp.status_code}]: {resp.text[:100]} ---")
        return resp.content, resp.status_code, resp.headers.items()
    except Exception as e:
        print(f"--- Final Proxy Error: {str(e)} ---")
        return jsonify({"code": -1, "msg": f"Final Proxy Error: {str(e)}"}), 500

@app.route('/api/user-server-log', methods=['GET'])
def user_server_log():
    try:
        payload = _read_latest_user_server_log()
        return jsonify({"code": 0, "msg": "ok", **payload})
    except FileNotFoundError as e:
        return jsonify({"code": -1, "msg": str(e)}), 404
    except Exception as e:
        return jsonify({"code": -1, "msg": f"log read error: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
