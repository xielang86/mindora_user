# 1. 导入核心依赖
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
# 用轻量LLM（嵌入式可选：远端API/本地量化LLM，这里用模拟LLM演示核心逻辑）
# from langchain_core.llms import FakeLLM
from langchain_community.llms.fake import FakeListLLM

# 模拟Qt UI Skill（和方式1一致）
class QtMainWindow:
    def switch_page(self, page_name: str) -> bool:
        print(f"[Qt Skill执行] 切换界面到：{page_name}")
        return True
    def show_alert(self, msg: str) -> None:
        print(f"[Qt Skill执行] 弹出提示：{msg}")
qt_window = QtMainWindow()

# ---------------------- 步骤1：封装Skill为Tool（方式1的装饰器） ----------------------
@tool
def switch_ui_page(page_name: str) -> str:
    """切换Qt嵌入式界面到指定页面，page_name可选：监控页、设置页、数据页"""
    success = qt_window.switch_page(page_name)
    return f"页面切换{'成功' if success else '失败'}"

@tool
def show_qt_alert(msg: str) -> str:
    """在Qt嵌入式界面显示弹窗提示，msg为弹窗的提示内容"""
    qt_window.show_alert(msg)
    return f"弹窗已显示，内容：{msg}"

tools = [switch_ui_page, show_qt_alert]

# ---------------------- 步骤2：定义Prompt，指导LLM调用Tool ----------------------
# Prompt告诉LLM：有哪些Tool、能做什么、需要返回指定格式的调用指令
# prompt = ChatPromptTemplate.from_messages([
#     ("system", """
#     你是RK3562嵌入式设备的Qt UI智能助手，只能调用提供的Tool完成用户指令，返回JSON格式：
#     {"tool_name": "工具名称", "args": {"参数名": "参数值"}}
#     可用Tool：
#     1. switch_ui_page：切换Qt页面，入参page_name
#     2. show_qt_alert：显示Qt弹窗，入参msg
#     """),
#     ("user", "{input}")
# ])
# 更规范的Prompt（无需转义，LLM同样能返回正确JSON）
prompt = ChatPromptTemplate.from_messages([
    ("system", """
    你是RK3562嵌入式设备的Qt UI智能助手，仅能调用提供的工具完成用户指令，严格返回纯JSON格式结果，不要添加任何额外文字、解释、标点。
    JSON格式要求：包含tool_name（工具名称）和args（参数字典）两个键，args的键为工具入参名，值为对应参数。
    可用工具：
    1. switch_ui_page：切换Qt页面，唯一入参page_name（可选：监控页、设置页、数据页）
    2. show_qt_alert：显示Qt弹窗，唯一入参msg（弹窗提示内容）
    """),
    ("user", "{input}")
])

# ---------------------- 步骤3：初始化LLM（嵌入式替换为实际LLM：如讯飞API/本地Llama3） ----------------------
# FakeLLM是模拟LLM，实际项目中替换为：
# from langchain_community.llms import Ollama  # 本地量化LLM
# llm = Ollama(model="llama3:8b-instruct-q4_0")  # 4-bit量化，适配RK3562
llm = FakeListLLM(
    responses=[
        '{"tool_name": "switch_ui_page", "args": {"page_name": "监控页"}}',
        '{"tool_name": "show_qt_alert", "args": {"msg": "监控页切换成功"}}'
    ]
)

# ---------------------- 步骤4：LangChain编排：Prompt → LLM → 解析 → 调用Tool ----------------------
# 解析LLM返回的JSON指令
parser = JsonOutputParser()
# 构建链：用户输入 → Prompt → LLM → 解析JSON
chain = prompt | llm | parser

# ---------------------- 核心智能交互：用户自然语言 → LLM决策 → LangChain调用Skill ----------------------
# 第一次调用：切换监控页
user_input1 = "把Qt界面切换到监控页"
llm_result1 = chain.invoke({"input": user_input1})
# LangChain根据LLM结果调用对应Skill
tool1 = next(t for t in tools if t.name == llm_result1["tool_name"])
result1 = tool1.invoke(llm_result1["args"])
print(f"用户指令：{user_input1} → 执行结果：{result1}")

# 第二次调用：弹窗提示
user_input2 = "弹出提示说监控页切换成功"
llm_result2 = chain.invoke({"input": user_input2})
tool2 = next(t for t in tools if t.name == llm_result2["tool_name"])
result2 = tool2.invoke(llm_result2["args"])
print(f"用户指令：{user_input2} → 执行结果：{result2}")
