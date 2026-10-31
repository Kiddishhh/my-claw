"""开发任务的默认预算；构建入口和真实验收脚本共享，调用方仍可显式覆盖。"""

# 主代理以协调为主；子代理还需要读技能、检查文件、执行和修复，给更多空间。
DEFAULT_MODEL_CALLS = 30
DEFAULT_SUBAGENT_MODEL_CALLS = 40
# 图步数包含中间件节点，不等于模型调用次数；保留宽松的独立防循环上限。
DEFAULT_RECURSION_LIMIT = 1000
