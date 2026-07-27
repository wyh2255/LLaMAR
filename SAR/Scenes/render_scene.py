"""
场景可视化脚本
===============
提供命令行接口，用于渲染指定编号场景的二维网格地图可视化。
可用于快速查看场景布局、子任务列表和覆盖范围信息。

使用方法:
    python SAR/Scenes/render_scene.py --scene=1
"""

import argparse
from pathlib import Path
import os, sys
from pprint import pprint

# set parent directory to address relative imports
# 将项目根目录加入 sys.path，以支持从 SAR/ 目录导入模块
directory = Path(os.getcwd()).absolute()
sys.path.append(
    str(directory)
)  # note: no ".parent" addition is needed for python (.py) files

from env import SAREnv

# --- 命令行参数解析 ---
parser = argparse.ArgumentParser()
parser.add_argument('--scene', type=str, help='场景编号（如 1, 2, 3）')
args = parser.parse_args()

# --- 创建场景环境并重置 ---
# show this environment
env=SAREnv(num_agents=6, scene=args.scene, seed=0)
env.reset()

# --- 打印子任务列表和覆盖范围 ---
print("subtasks:")
pprint(env.checker.subtasks)
print("coverage:")
pprint(env.checker.coverage)

# --- 渲染场景（弹出二维网格可视化窗口） ---
env.render()
