# -*- coding: utf-8 -*-
"""
llamar.py — LLaMAR 实验主入口

本文件是 SAR（Search & Rescue）基线系统的启动脚本。
功能流程：
  1. 解析命令行参数（场景编号、智能体数量、随机种子等）
  2. 创建 SAR 环境并重置
  3. 调用 Planner（规划器）生成初始子任务计划
  4. 进入主循环（Plan → Act → Verify）：
     - update_plan()           # 更新计划
     - Actor (LLM)             # LLM 为每个智能体选择动作
     - action_mapping()        # 将自然语言动作映射为环境可执行动作
     - env.step()              # 执行动作，更新环境
     - Verifier (LLM)          # LLM 判断哪些子任务已完成
     - Planner (LLM)           # LLM 更新子任务计划
  5. 记录日志并输出统计指标

注意：每步包含 3 次 LLM 调用（Actor → Verifier → Planner），初始还有一次 Planner。
"""

import re
import base64
import requests
import json, os
import pandas as pd
import tqdm
from pathlib import Path
import sys

# set parent directory to address relative imports
# 设置父目录以处理相对导入
directory = Path(os.getcwd()).absolute()
sys.path.append(
    str(directory)
)  # note: no ".parent" addition is needed for python (.py) files
print(os.getcwd())

from misc import Arg

# import environment
# 导入环境
from env import SAREnv

# load info about amt of agent before utils
# 在导入工具之前加载智能体数量信息
import argparse

# --- 命令行参数解析 ---
parser = argparse.ArgumentParser()
parser.add_argument("--scene", type=int, default=1)          # 场景编号（1-5）
parser.add_argument("--verbose", action="store_true")        # 详细输出模式
parser.add_argument("--action_verbose", action="store_true") # 动作详细输出模式
parser.add_argument("--tiny_verbose", action="store_true")   # 精简输出模式
parser.add_argument("--name", type=str, default="default_location")  # 实验名称（用于日志目录）
parser.add_argument("--agents", type=int, default=2)         # 智能体数量
parser.add_argument("--seed", type=int, default=42)          # 随机种子
args = parser.parse_args()

# change config file
# write config file
# 根据命令行参数动态生成多智能体配置文件
with open("multiagent_config.json", "w+") as f:
    d = {"num_agents": args.agents}
    json.dump(d, f)

# import utils for this baseline - with updated config file
# 导入本基线系统的工具模块和日志记录器（此时配置文件已更新）
from llamar_utils_multiagent import *
from sar_logging import SARLogger

import warnings
import os


# to avoid warning
# 避免 tokenizers 的并行化警告
os.environ["TOKENIZERS_PARALLELISM"] = "true"

# no warnings
# 忽略所有警告
warnings.filterwarnings("ignore")


# --- 加载 OpenAI API 密钥 ---
with open(os.path.expanduser("~") + "/openai_key.json") as json_file:
    key = json.load(json_file)
    api_key = key["my_openai_api_key"]
headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

# environment initialization
# --- 创建并重置 SAR 环境 ---
env = SAREnv(num_agents=args.agents, scene=args.scene, seed=args.seed, save_frames=True)
env.reset()
timeout = env.task_timeout

# config
# --- LLM 配置：温度和模型名称 ---
config = Arg(temperature=0.7, model="gpt-4")

# logger
# --- 初始化日志记录器 ---
logger = SARLogger(env=env, baseline_name=args.name)
print("baseline path (w/ results):", logger.baseline_path)

# some inits
# --- 初始化上一轮动作和成功状态 ---
previous_action = [] * args.agents
previous_success = [True, True]  # initialize with True

# there is weird try-except loop wrapper (done in order to prevent json errors)
# --- 主流程开始 ---
print("*" * 50)
print("Starting the llamar baseline")
print("*" * 50)

# PLANNER - Initial only!
# --- 初始规划：仅执行一次 Planner，生成初始子任务列表 ---
success = False
while not success:
    try:
        response = get_gpt_response(env, config, action_or_planner="planner")
        outdict = get_action(response)

        if args.verbose:
            print("*" * 10, "planner outdict!", "*" * 10)
            print(outdict)
            print()
        success = True
    except Exception as e:
        print("failure reason (in try-except loop):", e)
        pass

# Start of LoOpP - while not finished or has passed timeout
# --- PLAN-ACT-VERIFY 主循环 ---
for step_num in tqdm.trange(timeout):

    # --- 步骤1：更新计划（将 Planner 的输出写入环境） ---
    update_plan(env, outdict["plan"], env.closed_subtasks)

    # ACTOR
    # --- 步骤2：Actor — LLM 为每个智能体选择下一步动作 ---
    success = False
    while not success:
        try:
            response = get_gpt_response(env, config, action_or_planner="action")
            outdict = get_action(response)
            preaction, reason, subtask, memory, failure_reason = (
                process_action_llm_output(outdict)
            )
            success = True
            if args.verbose or args.action_verbose:
                print("*" * 10, "Actor outdict!", "*" * 10)
                print(outdict)
                print()
        except Exception as e:
            print("failure reason (in try-except loop):", e)
            pass

    # --- 将自然语言动作映射为环境可执行动作 ---
    action = action_mapping(env, preaction)

    # --- 记录智能体记忆（动作、原因、子任务等） ---
    logger.log_agent_mem(env.step_num, action, reason, subtask, memory)
    # MISSING: conditional (we're using this now)
    env.update_subtask(subtask, 0)
    env.update_memory(memory, 0)
    # --- 执行动作，更新环境状态 ---
    d1, successes = env.step(action)
    previous_action = action
    previous_success = successes

    # VERIFIER
    # --- 步骤3：Verifier — LLM 判断哪些子任务已完成 ---
    success = False
    while not success:
        try:
            response = get_gpt_response(env, config, action_or_planner="verifier")
            outdict = get_action(response)
            if args.verbose:
                print("*" * 10, "verifier outdict!", "*" * 10)
                print(outdict)
                print()
            success = True
        except Exception as e:
            print("failure reason (in try-except loop):", e)
            pass

    # v0 - update completed subtasks list - no for now
    # env.closed_subtasks = set_addition(env.closed_subtasks, outdict["completed subtasks"])
    # --- 更新已完成子任务列表 ---
    env.closed_subtasks = outdict["completed subtasks"]
    if len(env.closed_subtasks) == 0:
        env.closed_subtasks = None
    env.input_dict["Robots' completed subtasks"] = env.closed_subtasks
    env.get_planner_llm_input()

    # PLANNER
    # --- 步骤4：Planner — LLM 根据最新状态更新子任务计划 ---
    success = False
    while not success:
        try:
            response = get_gpt_response(env, config, action_or_planner="planner")
            outdict = get_action(response)
            success = True
        except Exception as e:
            print("failure reason (in try-except loop):", e)
            pass

    # get statistics and finish step
    # --- 计算当前步的统计指标 ---
    coverage = env.checker.get_coverage()
    transport_rate = env.checker.get_transport_rate()
    finished = env.checker.check_success()

    # log current 'step'
    # --- 记录当前步的日志 ---
    logger.log_step(
        step=step_num,
        preaction=preaction,
        action=action,
        success=previous_success,
        coverage=coverage,
        transport_rate=transport_rate,
        finished=finished,
    )

    if args.verbose:
        print("_" * 50)
        print(f"Step {step_num}")
        print(f"Completed Subtasks: ")
        print("\n".join(env.checker.subtasks_completed))

    # if the model outputs "Done" for both agents, break
    # --- 如果所有智能体都输出 "Done"，则提前结束循环 ---
    if all(status == "Done" for status in action):
        break

# --- 汇总输出（精简模式） ---
if args.tiny_verbose:
    # get statistics and finish step
    coverage = env.checker.get_coverage()
    transport_rate = env.checker.get_transport_rate()
    finished = env.checker.check_success()

    print("_" * 50)
    print(f"Step {step_num}")
    print(f"Completed Subtasks: ")
    print("\n".join(env.checker.subtasks_completed))
    print("Transport rate:", transport_rate)
    print("Coverage:", coverage)
    print("Finished:", finished)


# STOP - stop the controller / remove window
# --- 停止环境，释放资源 ---
env.stop()
