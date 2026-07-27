# -*- coding: utf-8 -*-
"""
llamar_utils_multiagent.py — LLaMAR SAR 基线系统的 LLM 工具模块

本文件包含：
1. 三个核心 LLM Prompt（PLANNER_PROMPT、VERIFIER_PROMPT、ACTION_PROMPT）
   - PLANNER: "excellent planner" — 生成子任务计划
   - ACTOR: "excellent planner and robot controller" — 为每个智能体选择动作
   - VERIFIER: "excellent planner" — 判断已完成子任务
2. LLM API 调用工具（get_gpt_response、prepare_payload 等）
3. 动作后处理工具（action_checker、process_action_llm_output、action_mapping）

LLM 使用 gpt-4-turbo，通过 requests.post 直接调用 OpenAI API。
重试机制：指数退避 while True 循环，三层正则回退解析（json / python / tilde 代码块）。
"""

import base64
import requests
import json, os
import pandas as pd
from pathlib import Path
import sys

# set parent directory to address relative imports
# 设置父目录以处理相对导入
directory = Path(os.getcwd()).absolute()
sys.path.append(
    str(directory)
)  # note: no ".parent" addition is needed for python (.py) files
print(os.getcwd())

from env import SAREnv
from base_env import SARBaseEnv as baseenv
from object_actions import get_closest_feasible_action
from misc import *

# --- 加载 OpenAI API 密钥 ---
with open(os.path.expanduser("~") + "/openai_key.json") as json_file:
    key = json.load(json_file)
    api_key = key["my_openai_api_key"]
headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

# read config file
# 读取多智能体配置文件（由 llamar.py 动态生成）
with open("multiagent_config.json", "r") as f:
    d = json.load(f)
    NUM_AGENTS = d["num_agents"]

# AGENT_NAMES global variable from env_new contains all the agent names (6 of them)
# subsample to num agents (so len(.) gives accurate amt)
# AGENT_NAMES 全局变量来自 SAREnv，包含全部 6 个智能体名称
# 根据 NUM_AGENTS 截取前 N 个
AGENT_NAMES_ALL = SAREnv.AGENT_NAMES
AGENT_NAMES = AGENT_NAMES_ALL[:NUM_AGENTS]

# useful variables
# 从 SAREnv 获取环境相关常量
FIRE_TYPES = SAREnv.FIRE_TYPES                         # 火灾类型列表
EXTINGUISH_TYPES = SAREnv.EXTINGUISH_TYPES              # 灭火资源类型列表
AMT_FIRE_TYPES = len(FIRE_TYPES)                        # 火灾类型数量
INVENTORY_CAPACITY = SAREnv.INVENTORY_CAPACITY           # 智能体背包容量
INVENTORY_TYPES = SAREnv.INVENTORY_TYPES                 # 背包可存放的资源类型
MIN_REQUIRED_AGENTS = SAREnv.MIN_REQUIRED_AGENTS          # 搬运人员所需最少智能体数
ALL_INTENSITIES = SAREnv.ALL_INTENSITIES                  # 火焰强度等级列表
CRITICAL_INTENSITY = SAREnv.CRITICAL_INTENSITY            # 临界强度（达到后开始蔓延）
L_TO_M = SAREnv.L_TO_M                                   # LOW→MEDIUM 所需步数
M_TO_H = SAREnv.M_TO_H                                   # MEDIUM→HIGH 所需步数
CARDINAL_DIRECTIONS = SAREnv.CARDINAL_DIRECTIONS           # 基本方向（上下左右等）
GRID_WIDTH = SAREnv.GRID_WIDTH                            # 网格宽度
GRID_HEIGHT = SAREnv.GRID_HEIGHT                          # 网格高度

# NOTE: removed information about when to use deposit (just avoid altogether)
# 注意：移除了关于何时使用存储点的说明（总体上避免使用）
_removed = """
Therefore, since time is of the essence to extinguish fires, it might be benefitial to have some robots collecting resources for others and store it in deposits.
This is in order to avoid having the ones fighting the fires have to waste steps collecting the resources 1-unit at a time from the reservoirs.
However, if an agent goes to the deposit, drops their supplies, and then immediately collects them, this is a waste of time. The use of deposits only makes sense if some agents are 'collectors' and other are 'firefighters'. So make sure you use deposits wisely.
"""

# v1 - updated environment string to also give the negatives of using deposits (intended, more neutral)
# v2 - add rules about how the env processes person drops
# v3 - removed info about why to use/not to use, made it clear to steer away of deposits to store resources
# v4 - add info that fire has different regions and they're all important
# v5 - some regions might be on fire and some not (not all equally important)

# --- 环境描述字符串（注入到三个 Prompt 中） ---
# 描述 SAR 环境的规则：火灾类型、强度变化、蔓延机制、人员救援、资源系统等
ENV_STR = f"""The environment consists of fires and lost persons, along with reservoirs, deposits, and robots (you). All in a grid with width {GRID_WIDTH} and height {GRID_HEIGHT}.

Initially, the robots can see all the fires, but does not know the location of any of the lost people - robots must explore.
The fires can be of {AMT_FIRE_TYPES} different types: {join_conjunction(FIRE_TYPES, 'or')}, each requiring a different resource to extinguish - {join_conjunction(EXTINGUISH_TYPES, 'and')} respectively. Make sure you use the proper resource to do so.
A fire consists of a group of 'flammable' objects with intensities of {join_conjunction(ALL_INTENSITIES, 'or')}. It is divided into different regions geographically, so all regions that aren't extinguished (intensity {ALL_INTENSITIES[0]}), must be properly addressed before fire can be extinguished. The first few regions (1,2,etc) are the sources of the fire, and must be addressed first.
At each step, if a flammable object has an intensity of {ALL_INTENSITIES[1]} or higher, it'll increase in intensity if not extinguished. They spread quickly, so it's important for almost all robots to work collectively to stop the fire.
In {L_TO_M} steps, the flammable object will go from {ALL_INTENSITIES[1]} to {ALL_INTENSITIES[2]}. In {M_TO_H} steps, the flammable object will go from {ALL_INTENSITIES[2]} to {ALL_INTENSITIES[3]}.
Once a flammable object reaches an intensity of {CRITICAL_INTENSITY} and not before, it spreads to its immediate neighboors (neighboors with intensity {ALL_INTENSITIES[0]} start with intensity of {ALL_INTENSITIES[1]}).  In order to extinguish a fire, a robot can use the appropriate extinguish resource at that location.
Then, all the flammable objects in or immediately around this location will lower in intensity by one notch (e.g. from {ALL_INTENSITIES[2]} to {ALL_INTENSITIES[1]}, or {ALL_INTENSITIES[3]} to {ALL_INTENSITIES[2]}).

The reservoirs can be of type {join_conjunction(EXTINGUISH_TYPES, 'or')}, resources can only be collected at a rate of 1-unit per step.
Thus, to get more resources, you have to collect the resources multiple times.

The deposits can hold any amount and type of resources: {join_conjunction(INVENTORY_TYPES, 'and')}.
The robots can store their entire inventory into the deposit in order to save it for other robots to use.
When a robot gets a certain resource type (if any is available) from a deposit, the space left in their inventory is filled with that resource type.
Deposits create an unnecessary middle-step when using them to store resources, so do not waste time in this way.

If any robot enters the area of visibility of a lost person, that person is found and all robots can now see it.
Once a person is found, at least {MIN_REQUIRED_AGENTS} robots are required to carry it (could be more depending on person). Otherwise, the person cannot be moved.
A carried person should be dropped into a deposit (any suffices).
To drop a carried person, all agents should have navigated to deposit, and they must ALL perform the DropOff action.

The robots have an inventory capacity of {INVENTORY_CAPACITY} with slots for {join_conjunction(INVENTORY_TYPES, 'and')}."""

# --- 观察格式描述字符串 ---
OBS_STR = f"""You will get a description of the task robots are supposed to do. You will get an textual description of the environment from the perspective of {join_conjunction(AGENT_NAMES, 'and')} as the observation input. You will also get a list of objects each robot is able to see in the environment. Here the objects will have a distinct name which will also include which type of object it is.
So, along with the observation inputs you will get the following information:
"""


# planner
# REMOVED: "do not perform additional steps..."
# REMOVED: "since there are _ agents, ... don't bump into each other"
# v1 - Avoid having too fine-grained tasks (especially if there might the multiple of them)
# v2 - divide subtasks into extinguishing regions and extinguishing overall fire

# --- Planner 的观察变量描述 ---
PLANNER_OBS_STR = ",\n".join(
    [
        f"{name}'s observation: local observation (from up, down, left, right, center), global observation, and a list of objects {name} is observing"
        for name in AGENT_NAMES
    ]
)

# --- Planner Prompt（规划器） ---
# 功能：根据任务描述、观察、未完成/已完成子任务和记忆，生成子任务计划
# 输出格式：{"reason": "...", "plan": ["subtask1", "subtask2", ...]}
PLANNER_PROMPT = f"""You are an excellent planner who is tasked with helping {len(AGENT_NAMES)} embodied robots named {join_conjunction(AGENT_NAMES, 'and')} to carry out a task. Both robots have a partially observable view of the environment. Hence they have to explore around in the environment to do the task.

{ENV_STR}

{OBS_STR}
### INPUT FORMAT ###
{{Task: description of the task the robots are supposed to do,
{PLANNER_OBS_STR},
Robots' open subtasks: list of subtasks the robots are supposed to carry out to finish the task. If no plan has been already created, this will be None.
Robots' completed subtasks: list of subtasks the robots have already completed. If no subtasks have been completed, this will be None.
Robots' combined memory: description of robots' combined memory}}

Reason over the robots' task, image inputs, observations, open subtasks, completed subtasks and memory, and then output the following:
* Reason: The reason for why new subtasks need to be added.
* Subtasks: A list of open subtasks the robots are supposed to take to complete the task. Remember, as you get new information about the environment, you can modify this list. You can keep the same plan if you think it is still valid. Do not include the subtasks that have already been completed.
The "Plan" should be in a list format where the actions are listed sequentially.
For example:
     ["extinguish ChicagoFire_Region_1 using water", "extinguish ChicagoFire_Region_2 using water", "extinguish all of ChicagoFire using water", "collect sufficient water form reservoir"]
     ["locate the lost person", "carry the lost person", "navigate to deposit with lost person", "drop lost person in deposit"]

Your output should be in the form of a python dictionary as shown below.
Example output: {{
"reason": "since the subtask list is empty, the robots need to extinguish the fire, and find & drop the lost person. Thus, for the fire we have to get water from the reservoir and extinguish the chicago fire, using the deposit if needed to store resources. For the person, we have to locate them by exploring, carry them with enough agents, go to a deposit, and drop the person in it.",
"plan": ["extinguish ChicagoFire_Region_1 using water", "extinguish ChicagoFire_Region_2 using water", "extinguish all of ChicagoFire using water", "locate the lost person", "carry the lost person", "navigate to deposit with lost person", "drop lost person in deposit"]
}}

Ensure that the subtasks are not generic statements like "explore the environment" or "do the task". They should be specific to the task at hand.
Do not assign subtasks to any particular robot. Try not to modify the subtasks that already exist in the open subtasks list. Rather add new subtasks to the list.

* NOTE: DO NOT OUTPUT ANYTHING EXTRA OTHER THAN WHAT HAS BEEN SPECIFIED
Let's work this out in a step by step way to be sure we have the right answer.
"""


# verifier
# moves subtasks from open to completed
# v1 - don't have ambiguous / hyper-specific tasks ('collect enough water' or 'use reservoir'). Add only those that are clear

# --- Verifier 的观察变量描述 ---
VERIFIER_OBS_STR = ",\n".join(
    [
        f"{name}'s observation: list of objects the {name} is observing,\n{name}'s state: description of {name}'s state,\n{name}'s previous action: the action {name} took in the previous step,"
        for name in AGENT_NAMES
    ]
)

# v1 - disclaimer at end to not add completed subtask before it has been (try to avoid catastrophic forgetting)

# --- Verifier Prompt（验证器） ---
# 功能：根据智能体的观察、状态、上一步动作和记忆，判断哪些子任务已完成
# 将子任务从 "open" 列表移动到 "completed" 列表
# 输出格式：{"reason": "...", "completed subtasks": ["subtask1", ...]}
VERIFIER_PROMPT = f"""You are an excellent planner who is tasked with helping {len(AGENT_NAMES)} embodied robots named {join_conjunction(AGENT_NAMES, 'and')} to carry out a task. Both robots have a partially observable view of the environment. Hence they have to explore around in the environment to do the task.

{ENV_STR}

{OBS_STR}
### INPUT FORMAT ###
{{Task: description of the task the robots are supposed to do,
{VERIFIER_OBS_STR}
Robots' open subtasks: list of open subtasks the robots in the previous step. If no plan has been already created, this will be None,
Robots' completed subtasks: list of subtasks the robots have already completed. If no subtasks have been completed, this will be None,
Robots' combined memory: description of robots' combined memory
}}

You will receive the following information:
* Reason: The reason for why you think a particular subtask should be moved from the open subtasks list to the completed subtasks list.
* Completed Subtasks: The list of subtasks that have been completed by the robots. Note that you can add subtasks to this list only if they have been successfully completed and were in the open subtask list. If no subtasks have been completed at the current step, return an empty list.

The "Completed Subtasks" should be in a list format where the completed subtasks are listed. For example: ["collect water from deposit", "collect sufficient sand from reservoir"]
Your output should be in the form of a python dictionary as shown below.

Example output with two agents (do it for {len(AGENT_NAMES)} agents):
{{"reason": "{AGENT_NAMES[0]} used water on the ChicagoFire in the previous step and was successful, and {AGENT_NAMES[1]} explored and was successful. Since the ChicagoFire is still not extinguished completely, {AGENT_NAMES[0]} has still not completed the subtask of extinguishing ChicagoFire using water. Since the lost person is now visible {AGENT_NAMES[1]} has completed the subtask of finding the lost person.",
"completed subtasks": ["locate lost person"]
}}

When you output the completed subtasks, make sure to not forget to include the previous ones in addition to the new ones.
Also, make sure to never add a subtask to completed subtasks before it has successfully been completed.
Let's work this out in a step by step way to be sure we have the right answer.

* NOTE: DO NOT OUTPUT ANYTHING EXTRA OTHER THAN WHAT HAS BEEN SPECIFIED
"""

# v1 - change failure reasons to include causal order and relevant information
# --- 失败原因提示模板（注入到 Action Prompt 中） ---
FAILURE_REASON = """
If any robot's previous action failed, use the previous history, your current knowledge of the room (i.e. what things are where), and your understanding of causality to think and rationalize about why the previous action failed. Output the reason for failure and how to fix this in the next timestep. If the previous action was successful, output "None".
Common failure reasons to lookout for include:
Trying to collect a resource before navigating to reservoir first,
trying to use a resource before navigating to fire location first,
not being close enough to interact with object.
"""

# NEW example
# changed to non-related, yet specific example
# 用于构造 Action Prompt 中的示例输出

action_wrapper = lambda name, action: f'"{name}\'s action" : "{action}"'

# previous actions (that failed) -> ["DropOff(LostPersonJeremy, TheDeposit)", "Idle", "UseSupply(ChicagoFire, Sand)"]
action_agents_1 = ["NavigateTo(TheDeposit)", "Idle", "UseSupply(ChicagoFire, Water)"]
ACTION_1 = ",\n".join(
    action_wrapper(AGENT_NAMES_ALL[i], action_agents_1[i]) for i in range(3)
)

# ----- example 1 (failure) ------
# "failure reason" - 1
# 示例：失败原因
FAILURE_REASON_EX_1 = "".join(
    [
        f"{AGENT_NAMES_ALL[0]} and {AGENT_NAMES_ALL[1]} failed to drop off LostPersonJeremy in TheDeposit because {AGENT_NAMES_ALL[1]} had not navigated to the deposit yet, and thus wasn't close enough to interact with it; they both have to be close enough to deposit.",
        f"{AGENT_NAMES_ALL[2]} failed to use the sand supply on the ChicagoFire because the fire is non-chemical, so it requires water.",
    ]
)

# "memory" - 1
# 示例：记忆
MEMORY_EX_1 = " ".join(
    [
        f"{AGENT_NAMES_ALL[0]} finished trying to DropOff LostPersonJeremy at the TheDeposit when {AGENT_NAMES_ALL[0]} was at co-ordinates (4,4).",
        f"{AGENT_NAMES_ALL[1]} finished being idle when {AGENT_NAMES_ALL[1]} was at co-ordinates (14, 6).",
        f"{AGENT_NAMES_ALL[2]} finished using sand supply on the ChicagoFire when {AGENT_NAMES_ALL[2]} was at co-ordinates (7, 24).",
    ]
)

# "reason" - 1
# 示例：推理
REASON_EX_1 = " ".join(
    [
        f"{AGENT_NAMES_ALL[0]} can wait for {AGENT_NAMES_ALL[1]} to finish navigating to TheDeposit.",
        f"{AGENT_NAMES_ALL[1]} can navigate to TheDeposit in order to be close enough to DropOff LostPersonJeremy.",
        f"{AGENT_NAMES_ALL[2]} can go to use their water supply instead on the ChicagoFire.",
    ]
)

# "subtask" - 1
# 示例：子任务
SUBTASK_EX_1 = " ".join(
    [
        f"{AGENT_NAMES_ALL[0]} is currently waiting for {AGENT_NAMES_ALL[1]} to finish navigating,",
        f"{AGENT_NAMES_ALL[1]} is currently navigating to TheDeposit,",
        f"{AGENT_NAMES_ALL[2]} is currently navigating to the EasternFire,",
    ]
)


# -- construct failure example from this ---
# 组装完整的失败示例字典
FAILURE_EXAMPLE = f"""
Example:
{{
"failure reason": "{FAILURE_REASON_EX_1}",
"memory": "{MEMORY_EX_1}",
"reason": "{REASON_EX_1}",
"subtask": "{SUBTASK_EX_1}",
{ACTION_1}
}}
"""

# --- Actor 的观察变量描述 ---
ACTION_OBS_STR = ", ".join(
    [
        f"{name}'s observation: list of objects the {name} is observing,\n{name}'s state: description of {name}'s state,\n{name}'s previous action: description of what {name} did in the previous time step and whether it was successful,\n{name}'s previous failures: if {name}'s few previous actions failed, description of what failed,"
        for name in AGENT_NAMES
    ]
)

# action planner
# v1 - added that it must navigate to deposit strictly before dropping the agents
# v2 - added info about constraints in dropping a person
# v3 - added info about constraints on how many supplies dropped (must do multiple to clear inventory)
# v4 - change order, emphasize navigate before interact rule (does it think it includes use supply?)
# v5 - be specific for region to navigate to
# v6 - tell it that you can't direct use supply, it's only where you are

# details for actor
# --- Actor 的详细约束说明（注入到 Action Prompt 中） ---
DETAILS_STR = f"""
Important details described below:
    * Even if the robot can see an object, it might not be able to interact with them if they are too far away. Hence you will need to make the robot navigates to the objects they want to interact with.
    * When navigating to fire, please specify which specific region of the fire you wish to target.
    * Additionally, when you use the supply, it will be dropped wherever you are, NOT in the region you said. So, make sure to navigate to wherever you wish to use a supply.
    * When a fire has an average intensity of {ALL_INTENSITIES[0]}, it means all the flammable objects have been extinguished completely.
    * When the person is not initially visible, you must Explore.
    * When a person is carried, no other action other than navigation and "DropOff" can be made by any of the robots carrying it.
    * When a robot carries a person, all their other resources are dropped and the person takes the entire inventory space.
    * When a group robot wants to drop a person, they must all navigate to the deposit strictly before dropping them. Then, they should ALL perform the DropOff action.
    * When a robot is successful in carrying a person, that just means that specific robot is carrying it, but the person might still not be moveable if an insufficient amount of robots is carrying it.
    * A fire is divided into different regions, and the fire itself (which is just the center). However, there might be flammable objects from the fire that aren't immediately neighbooring this location, so the robot might have to move in different directions to reach them.
    * The resources in the inventory can only be used on fires one unit at a time, so do multiple UseSupply until you clear our your inventory.
    * When a robot is doing a "Move(<direction>)" action, if there is an obstacle, they will not be able to move in that direction.
"""


# @here
# v1 - added more specific output than 'action' to avoid dict error
# v2 - added "Done" action
# v3 - added that when using multiple agents, it must address *different* things (like different fires) + avoid agents being idle

# --- Actor Prompt（动作执行器） ---
# 功能：为每个智能体选择当前步要执行的具体动作
# 输出格式：{"failure reason": "...", "memory": "...", "reason": "...", "subtask": "...", "<agent>'s action": "..."}
ACTION_PROMPT = f"""
You are an excellent planner and robot controller who is tasked with helping {len(AGENT_NAMES)} embodied robots named {join_conjunction(AGENT_NAMES, 'and')} carry out a task. All {len(AGENT_NAMES)} robots have a partially observable view of the environment. Hence they have to explore around in the environment to do the task.

{ENV_STR}

They can perform the following actions:
["navigate to object <object_name>", "move <direction>", "explore", "carry <person_name>", "dropoff <person_name> at <deposit_name>", "store supply <deposit_name>", "use supply <resource_name> on <fire_name>", "get supply <resource_name> from <deposit_name>", "get supply <reservoir_name>", "clear inventory", "Done"]

Here <direction> is one of {str(CARDINAL_DIRECTIONS)}.
Here <resource_name> is one of {str(EXTINGUISH_TYPES)}.
The other names (<object_name>, <person_name>, <deposit_name>, <reservoir_name>) are based on the observations.
When finished with all the subtasks, output "Done" for all agents.

You need to suggest the action that each robot should take at the current time step.

{OBS_STR}
### INPUT FORMAT ###
{{Task: description of the task the robots are supposed to do,
{ACTION_OBS_STR}
Robots' open subtasks: list of subtasks  supposed to carry out to finish the task. If no plan has been already created, this will be None.
Robots' completed subtasks: list of subtasks the robots have already completed. If no subtasks have been completed, this will be None.
Robots' subtask: description of the subtasks the robots were trying to complete in the previous step,
Robots' combined memory: description of robot's combined memory}}

First of all you are supposed to reason over the image inputs, the robots' observations, previous actions, previous failures, previous memory, subtasks and the available actions the robots can perform, and think step by step and then output the following things:
* Failure reason: {FAILURE_REASON}
* Memory: Whatever important information about the scene you think you should remember for the future as a memory. Remember that this memory will be used in future steps to carry out the task. So, you should not include information that is not relevant to the task. You can also include information that is already present in its memory if you think it might be useful in the future.
* Reason: The reasoning for what each robot is supposed to do next
* Subtask: The subtask each robot should currently try to solve, choose this from the list of open subtasks.
* Actions for join_conjunction(AGENT_NAMES, 'and'): The actions the robots are supposed to take just in the next step such that they make progress towards completing the task. Make sure that this suggested actions make these robots more efficient in completing the task as compared only one agent solving the task, avoid having agents being idle. Notably, make sure that different agents address different problems (e.g. different fires)
Your output should just be in the form of a python dictionary as shown below.

Example of output for 3 robots (do these for {len(AGENT_NAMES)} robots):
{FAILURE_EXAMPLE}
Note that the output should just be a dictionary similar to the example output.

{DETAILS_STR}
"""

"""
print("*"*30+"\nPLANNER PROMPT:\n"+"*"*30)
print(PLANNER_PROMPT)
print("*"*30+"\nVERIFIER PROMPT:\n"+"*"*30)
print(VERIFIER_PROMPT)
print("*"*30+"\nACTION PROMPT:\n"+"*"*30)
print(ACTION_PROMPT)
"""


def process_action_llm_output(outdict):
    """
    从 LLM 输出的字典中提取每个智能体的动作及其他元信息。

    参数:
        outdict (dict): LLM 返回的原始字典，包含每个智能体的动作、原因、子任务、记忆、失败原因。

    返回:
        tuple: (action, reason, subtask, memory, failure_reason)
            - action (list): 每个智能体的自然语言动作列表
            - reason (str): 推理过程
            - subtask (str): 当前子任务描述
            - memory (str): 记忆信息
            - failure_reason (str): 上次动作失败的原因
    """
    action = []
    for i in range(len(AGENT_NAMES)):
        action.append(outdict[f"{AGENT_NAMES[i]}'s action"])
    reason = outdict["reason"]
    subtask = outdict["subtask"]
    memory = outdict["memory"]
    failure_reason = outdict["failure reason"]
    return action, reason, subtask, memory, failure_reason


def action_mapping(env, action):
    """
    动作映射函数：将自然语言动作转换为环境可执行动作。
    目前直接调用 action_checker 进行语义匹配。

    参数:
        env: SAR 环境对象
        action (list): 自然语言动作列表

    返回:
        list: 环境可执行动作列表
    """
    action = action_checker(env, action)
    return action


def encode_image(image_path: str):
    """
    将图片文件编码为 Base64 字符串（用于 LLM 视觉输入）。

    参数:
        image_path (str): 图片文件路径

    返回:
        str: Base64 编码的图片数据
    """
    # if not os.path.exists()
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def prepare_prompt(env, module_name: str, addendum: str):
    """
    准备 LLM 的系统提示词（system_prompt）和用户提示词（user_prompt）。

    参数:
        env: SAR 环境对象
        module_name (str): 模块名称，可选 "planner"、"verifier"、"action"
        addendum (str): 附加到 user_prompt 末尾的额外文本

    返回:
        tuple: (system_prompt, user_prompt)
            - system_prompt (str): 系统提示词（对应模块的固定 Prompt）
            - user_prompt (str): 用户提示词（来自环境的当前状态信息）

    注意: module_name 从 "planner", "verifier", "action" 中选择
    """
    # Choose the appropriate prompt based on what module is being called
    # user_prompt = baseenv.convert_dict_to_string(env.input_dict)
    if module_name == "action":
        system_prompt = ACTION_PROMPT
        user_prompt = baseenv.convert_dict_to_string(env.get_action_llm_input())
    elif module_name == "planner":
        system_prompt = PLANNER_PROMPT
        user_prompt = baseenv.convert_dict_to_string(env.get_planner_llm_input())
    elif module_name == "verifier":
        system_prompt = VERIFIER_PROMPT
        user_prompt = baseenv.convert_dict_to_string(env.get_verifier_llm_input())
    user_prompt += addendum
    return system_prompt, user_prompt


# NOTE: No images here
def prepare_payload(env, config, module_name: str, addendum: str = ""):
    """
    构造发送给 OpenAI API 的请求负载（payload）。

    参数:
        env: SAR 环境对象
        config: 配置对象（包含 temperature 等参数）
        module_name (str): 模块名称（"planner"/"verifier"/"action"）
        addendum (str): 附加文本，默认为空

    返回:
        dict: 符合 OpenAI Chat Completions API 格式的请求负载
              包含 model、messages（system + user）、max_tokens、temperature

    payload 结构:
        * system prompt（常量，对应模块的固定指令）
        * user prompt（随环境状态变化）
        然后发送到 OpenAI API 获取回复（动作/计划/验证结果）
    """
    system_prompt, user_prompt = prepare_prompt(env, module_name, addendum)
    payload = {
        "model": "gpt-4-turbo",
        "messages": [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": system_prompt},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                ],
            },
        ],
        "max_tokens": 1000,
        "temperature": config.temperature,
    }
    return payload


def get_action(response):
    """
    从 OpenAI API 回复中解析出 Python 字典。

    参数:
        response: requests.post 返回的响应对象

    返回:
        dict: 解析后的 Python 字典

    解析策略（三层正则回退）：
        1. 尝试匹配 ```json ... ``` 代码块
        2. 尝试匹配 ```python ... ``` 代码块
        3. 尝试匹配 ``` ... ``` 通用代码块
        4. 直接使用原始输出
    """
    response_dict = response.json()
    # convert the string to a dict
    # json_acceptable_string = response_dict["choices"][0]["message"]["content"].replace("'", "\"").replace("\n", "").replace("json", "").replace("`", "")
    output = response_dict["choices"][0]["message"]["content"]
    json_match = re.search(r"```json(.*?)```", output, re.DOTALL)
    python_match = re.search(r"```python(.*?)```", output, re.DOTALL)
    tilde_match = re.search(r"```(.*?)```", output, re.DOTALL)
    if json_match:
        # print("Got JSON TYPE OUTPUT")
        json_data = json_match.group(1)
    elif python_match:
        # print("Got JSON TYPE OUTPUT")
        json_data = python_match.group(1)
    elif tilde_match:
        # print("Got JSON TYPE OUTPUT")
        json_data = tilde_match.group(1)
    else:
        # print("Got NORMAL TYPE OUTPUT")
        json_data = output
    # print(json_data)
    out_dict = json.loads(json_data)
    return out_dict


def get_gpt_response(env, config, action_or_planner: str, addendum: str = ""):
    """
    调用 OpenAI GPT API（gpt-4-turbo）获取模型回复。

    参数:
        env: SAR 环境对象
        config: 配置对象
        action_or_planner (str): 模块名称（"planner"/"verifier"/"action"）
        addendum (str): 附加文本，默认为空

    返回:
        requests.Response: OpenAI API 的原始响应对象

    注意：
        - 使用 requests.post 直接调用，而非 OpenAI Python SDK
        - 调用方需要在外部用 while 循环进行重试（指数退避）
    """
    payload = prepare_payload(env, config, action_or_planner, addendum)
    response = requests.post(
        "https://api.openai.com/v1/chat/completions", headers=headers, json=payload
    )
    return response


def action_checker(env, actions):
    """
    将自然语言动作转换为环境中最接近的可执行动作。

    参数:
        env: SAR 环境对象
        actions (list): 自然语言动作列表（如 "pick up the apple"）

    返回:
        list: 环境可执行动作列表（如 "PickupObject(Apple_1)"）

    工作原理：
        模型输出的动作是自然语言描述。
        该函数通过语义嵌入（embedding）找到与环境可行动作最接近的匹配。
        例如: "pick up the apple" -> "PickupObject(Apple_1)"
    """
    checked_actions = []
    for act in actions:
        act = get_closest_feasible_action(act, env.object_dict)
        checked_actions.append(act)
    return checked_actions


# v0 - change it so that it appends instead
# for verifier
def set_addition(l1, l2):
    """
    集合合并函数：合并两个列表并去重。

    参数:
        l1 (list or None): 列表1
        l2 (list or None): 列表2

    返回:
        list: 去重后的合并列表
    """
    if l1 is None:
        l1 = []
    if l2 is None:
        l2 = []
    return list(set(l1 + l2))


def update_plan(env, open_subtasks, completed_subtasks):
    """
    将 Planner 生成的子任务计划写入环境状态。

    参数:
        env: SAR 环境对象
        open_subtasks (list): 未完成的子任务列表
        completed_subtasks (list or None): 已完成的子任务列表

    功能：
        - 更新 env.open_subtasks 和 env.closed_subtasks
        - 同步更新 env.input_dict 中的对应字段
    """
    env.open_subtasks = open_subtasks
    env.closed_subtasks = completed_subtasks
    env.input_dict["Robots' open subtasks"] = env.open_subtasks
    env.input_dict["Robots' completed subtasks"] = env.closed_subtasks
