# 1. 导入核心依赖
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from langchain_core.callbacks import CallbackManagerForToolRun
# 模拟Qt UI和硬件检测Skill（复杂逻辑）
class QtHardwareSkill:
    def check_serial(self, port: str = "/dev/ttyS0") -> dict:
        """检测串口状态（复杂Skill：返回多字段结果）"""
        print(f"[硬件Skill执行] 检测串口：{port}")
        return {"port": port, "status": "正常", "baudrate": 115200}
    def update_serial_status(self, status: dict) -> str:
        """更新Qt界面的串口状态显示"""
        print(f"[Qt Skill执行] 更新串口状态：{status}")
        return f"串口状态更新完成：{status['port']} - {status['status']}"

# 初始化复杂Skill实例
qt_hw_skill = QtHardwareSkill()

# ---------------------- 步骤1：定义Skill的入参模型（自定义参数校验） ----------------------
# 用Pydantic做参数校验：指定入参类型、默认值、描述，不符合则直接报错
class CheckSerialInput(BaseModel):
    port: str = Field(default="/dev/ttyS0", description="串口设备路径，如/dev/ttyS0、/dev/ttyUSB0")

# ---------------------- 步骤2：继承BaseTool封装Skill ----------------------
class CheckSerialTool(BaseTool):
    # 1. 定义Tool元信息（必须写）
    name: str = "check_serial_port"  # Tool的唯一名称（LangChain识别用）
    description: str = "检测嵌入式设备的串口状态，返回串口路径、状态、波特率"  # Skill功能描述
    args_schema: type[BaseModel] = CheckSerialInput  # 绑定入参模型（开启参数校验）
    return_direct: bool = True  # 直接返回Skill结果，不经过额外处理

    # 2. 核心方法：自定义Skill的执行逻辑（必须实现）
    def _run(
        self,
        port: str,  # 入参与args_schema一致
        run_manager: CallbackManagerForToolRun | None = None  # 回调（嵌入式可忽略）
    ) -> dict:
        # 底层调用实际的复杂Skill逻辑
        return qt_hw_skill.check_serial(port)

# 同理，封装「更新Qt串口状态」的Tool
class UpdateSerialStatusTool(BaseTool):
    name: str = "update_serial_status_ui"
    description: str = "根据串口检测结果，更新Qt界面的串口状态显示"
    return_direct: bool = True

    # 自定义入参（简单场景可省略args_schema，直接在_run中定义）
    def _run(self, status: dict, run_manager: CallbackManagerForToolRun | None = None) -> str:
        return qt_hw_skill.update_serial_status(status)

# ---------------------- LangChain调用复杂Skill：类封装Tool的交互 ----------------------
# 初始化Tool
check_serial_tool = CheckSerialTool()
update_status_tool = UpdateSerialStatusTool()

# 第一步：调用串口检测Skill
serial_status = check_serial_tool.invoke({"port": "/dev/ttyUSB0"})
print(f"LangChain调用检测Skill结果：{serial_status}")

# 第二步：将检测结果传入，调用Qt界面更新Skill（多Skill编排）
update_result = update_status_tool.invoke({"status": serial_status})
print(f"LangChain调用更新Skill结果：{update_result}")
