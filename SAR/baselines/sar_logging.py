# -*- coding: utf-8 -*-
"""
sar_logging.py — SAR 实验日志记录器

本文件定义了 SARLogger 类，用于记录 SAR 基线系统的实验结果。
主要功能：
  1. 记录轨迹数据（trajectory.csv）：步数、动作、成功状态、覆盖率和运输率等
  2. 记录智能体记忆数据（memory.csv）：步数、动作、原因、子任务、记忆等
  3. 保存环境渲染图片（render/ 目录）
  4. 汇总统计指标（summarize）：成功率、运输率、覆盖率、平均步数、平衡度
  5. 重新计算指标（recompute_metrics）：从已保存的轨迹重新计算统计指标

日志目录结构：
  {baseline_path}/actions/{seed}/{scene}/trajectory.csv
  {baseline_path}/actions/{seed}/{scene}/memory.csv
  {baseline_path}/render/*.png

注意：与 AI2Thor 的日志记录器非常相似。
"""

from pathlib import Path
import collections
import json, os, sys
import glob
import pandas as pd
import numpy as np
import imageio
import re
import ast
import copy
from pprint import pprint

# set parent directory to address relative imports
# 设置父目录以处理相对导入
directory = Path(os.getcwd()).absolute()
sys.path.append(
    str(directory)
)  # note: no ".parent" addition is needed for python (.py) files

from misc import extract_number

class SARLogger:
    """
    SAR 实验日志记录器。

    与 AI2Thor 的日志记录器非常相似。
    负责记录：
      - 轨迹指标到 CSV 文件
      - 环境渲染图片

    属性:
        env: SAR 环境对象
        df_cols: 轨迹 CSV 的列名
        df: 轨迹数据的 pandas DataFrame
        inner_df_cols: 记忆 CSV 的列名
        inner_df: 智能体记忆数据的 pandas DataFrame
        baseline_path: 日志保存路径（SAR/baselines/results/{baseline_name}/）
        summarize_mode: 是否处于汇总模式（不记录新步骤，只汇总已有数据）
    """

    def __init__(self, baseline_name, env=None):
        """
        初始化 SARLogger。

        参数:
            baseline_name (str): 日志文件夹名称（通常以基线系统名称命名，如 'ReAct'）
            env: SAR 环境对象。如果为 None，则进入 summarize_mode（汇总模式）
            summarize_mode: 如果为 True，则不使用 log_step，仅汇总已有日志数据

        初始化操作：
            - 创建空的 DataFrame 用于轨迹和记忆数据
            - 创建日志目录（baseline_path）
            - 设置环境渲染图片保存路径
            - 保存第一帧渲染图

        完成一次完整的循环后，通过 log_step 函数更新日志：
            log_step(step_num, action, action_successes, coverage, transport_rate)
        """

        self.summarize_mode=env is None

        self.env=env
        # --- 轨迹 CSV 列：步数、原始动作、映射后动作、成功状态、覆盖率、运输率、是否完成 ---
        self.df_cols=['Step', 'Pre-Action', 'Action', 'Success', 'Coverage', 'Transport Rate', 'Finished']
        self.df = pd.DataFrame(columns=self.df_cols)
        # --- 记忆 CSV 列：步数、动作、推理、子任务、记忆 ---
        self.inner_df_cols=['Step', 'Action', 'Reason', 'Subtask', 'Memory']
        self.inner_df = pd.DataFrame(columns=self.inner_df_cols)

        # make the save paths to be in baseline folder
        # 设置保存路径为 SAR/baselines/results/{baseline_name}/
        baseline_dir = Path(__file__).parent.absolute()
        self.baseline_path = Path(str(baseline_dir) + "/results/" + baseline_name)

        # self.baseline_path = Path("results") / baseline_name
        if not os.path.exists(self.baseline_path) and not self.summarize_mode:
            os.makedirs(self.baseline_path)

        if not self.summarize_mode:
            self.env.render_image_path = self.baseline_path / env.render_image_path
            if not os.path.exists(self.env.render_image_path):
                os.makedirs(self.env.render_image_path)

            # make sure we have the first frame in the right path (after we changed it)
            # 保存环境的第一帧渲染图
            self.env.save_frame()

    def log_step(self, step, preaction, action, success, coverage, transport_rate, finished):
        """
        记录一步的轨迹数据到 trajectory.csv。

        参数:
            step (int): 当前循环步数 (i)
            preaction (list): 语义映射前的原始动作
            action (list): [agent_1_action, agent_2_action, ...]
            success (list): [agent_1_action_success, agent_2_action_success, ...]
            coverage (float): env.checker.get_coverage() — 环境覆盖率
            transport_rate (float): env.checker.get_transport_rate() — 运输率
            finished (bool): env.checker.check_success() — 任务是否完成

        每次调用会将一行数据添加到 DataFrame 并立即写入 CSV 文件。
        """
        row = pd.DataFrame([[step, preaction, action, success, coverage, transport_rate, finished]], columns=self.df_cols)
        self.df = pd.concat([self.df, row])
        traj_path=self.baseline_path / self.env.action_dir_path
        if not os.path.exists(traj_path):
            os.makedirs(traj_path)
        self.df.to_csv(traj_path / str("trajectory.csv"), index=False)

    def recompute_metrics(self):
        """
        从已保存的 trajectory.csv 重新计算统计指标。

        当需要纠正或重新评估已保存的轨迹数据时使用。
        步骤：
          1. 读取 trajectory.csv
          2. 逐动作在环境中重新执行
          3. 重新计算覆盖率、运输率和完成状态
          4. 更新 DataFrame 并保存

        注意：目前未实现对多智能体指标的重新计算（TODO）。
        """
        traj_path_csv=self.baseline_path / self.env.action_dir_path / str("trajectory.csv")
        assert os.path.exists(traj_path_csv), f"File not found at {traj_path_csv} ... Cannot recompute statistics for nonexistent file"
        df = pd.read_csv(traj_path_csv)

        success_list = []
        coverage_list, transport_rate_list, finished_list = [], [], []
        for action_str in self.actions():
            # read in action from df
            # 从 DataFrame 读取动作字符串
            action = ast.literal_eval(action_str)
            # execute action in environment
            # 在环境中重新执行动作
            d, action_successes = self.env.step(action)
            # recompute metrics
            # 重新计算指标
            coverage = self.env.checker.get_coverage()
            transport_rate = self.env.checker.get_transport_rate()
            finished = self.env.checker.check_success()
            # update lists
            success_list.append(action_successes)
            coverage_list.append(coverage)
            transport_rate_list.append(transport_rate)
            finished_list.append(finished)

        # update df with recomputed metrics
        # 用重新计算的指标更新 DataFrame
        df['Success'] = success_list
        df['Coverage'], df['Transport Rate'], df['Finished'] = coverage_list, transport_rate_list, finished_list
        # TODO: add multi-agent metrics recomputing to this fn
        # TODO: 添加多智能体指标的重新计算
        self.df = df
        self.df.to_csv(traj_path_csv, index=False)

    def log_agent_mem(self, step, action, reason=None, subtask=None, memory=None):
        """
        记录智能体的记忆数据到 memory.csv。

        参数:
            step (int): 当前循环步数 (i)
            action (list): [agent_1_action, agent_2_action, ...]
            reason (str, optional): 智能体选择动作的推理过程
            subtask (str, optional): 智能体当前应执行的子任务
            memory (str, optional): 智能体对当前子任务的记忆

        注意：大多数基线系统不需要 reason、subtask 或 memory 字段。
        """

        # TODO: add all the results from the query to mem
        # TODO: 将所有查询结果添加到记忆

        row = pd.DataFrame([[step,action,reason,subtask,memory]], columns=self.inner_df_cols)
        self.inner_df = pd.concat([self.inner_df, row])
        path=self.baseline_path / self.env.action_dir_path
        if not os.path.exists(path):
            os.makedirs(path)
        self.inner_df.to_csv(path / str("memory.csv"), index=False)

    def actions(self):
        """
        从当前轨迹 CSV 中读取所有动作。

        返回:
            list: 每个元素是 ast.literal_eval 解析后的动作列表

        用于 recompute_metrics 的辅助方法。
        """
        # get the agent actions from the current bath
        # 从当前批次获取智能体动作
        traj_path=self.baseline_path / self.env.action_dir_path
        if not os.path.exists(traj_path):
            os.makedirs(traj_path)
        pth = traj_path / str("trajectory.csv")
        df=pd.read_csv(pth)
        actns=df['Action'].to_numpy()
        actns=[ast.literal_eval(actn) for actn in actns]
        return actns

    def read_agent_mem(self):
        """
        从 memory.csv 读取智能体记忆数据。

        返回:
            pandas.DataFrame: 记忆数据的 DataFrame
        """
        path=self.baseline_path / self.env.action_dir_path
        if not os.path.exists(path):
            os.makedirs(path)
        df=pd.read_csv(path / str("memory.csv"))
        return df

    def summarize(self):
        """
        汇总指定 baseline_name 文件夹中所有实验的统计指标。

        返回:
            dict: 嵌套字典结构
                - 外层键为智能体数量 (n_agents)
                - 内层键为场景名称 (scene_name)
                - 值包含各指标的列表（按种子/楼层平面聚合）

            当前包含的指标：
                - success_rate (成功率)
                - transport_rate (运输率)
                - coverage (覆盖率)
                - average_steps (平均步数)
                - balance (多智能体平衡度)

        注意：
            - 可以传入任意 env，会按指定智能体数量（默认2）给出汇总
            - 文件夹结构为：actions/{n_agents}/{seed}/{scene}/trajectory.csv
            - n_agents 被视作不同的基线系统
        """
        # NOTE: (NEW) Gives list of dicts for all the n_agents encountered!
        # 注意：（新）返回所有遇到的 n_agents 的字典列表

        # --- 定义各指标的计算函数（lambda） ---
        sr=lambda df : int((df['Finished'].to_numpy())[-1])      # 成功率：最后一步的 Finished 值
        tr=lambda df : (df['Transport Rate'].to_numpy())[-1]     # 运输率：最后一步的值
        c=lambda df : (df['Coverage'].to_numpy())[-1]            # 覆盖率：最后一步的值
        st=lambda df : len(df['Step'].to_numpy())                # 平均步数：总步数
        bl=lambda df: self._get_balance_metric(df['Action'].to_numpy(), df['Success'].to_numpy())  # 平衡度

        self.metrics={'success_rate' : sr, 'transport_rate' : tr, 'coverage' : c, 'average_steps' : st, 'balance' : bl}

        # NOTE: slightly modified for the folder structure (actions/n_agents/seed_j/Scene_i/trajectory.csv)
        # seeds <-> floorplans, scenes <-> tasks, n_agents <-> different baselines altogether (use num_agents)
        # seeds <-> 楼层平面, scenes <-> 任务, n_agents <-> 不同基线系统

        n_agents=glob.glob(str( self.baseline_path / str("actions") / str("*") ))
        main_dicts={}

        n_agents.sort(key=lambda x : extract_number(Path(x).name))
        for n_agent in n_agents:
            main_dict={}

            make_scene_dict=lambda : dict([(k, []) for k in self.metrics.keys()])
            scene_dicts={} # maps scene_name -> it's scene_dict
            # 映射：场景名称 -> 指标字典

            seeds=glob.glob(str(Path(n_agent) / str("*")))
            seeds.sort(key=lambda x : extract_number(Path(x).name))
            for seed in seeds:
                scenes=glob.glob(str(Path(seed) / str("*")))
                for scene in scenes:
                    pth=Path(scene) / str("trajectory.csv") # actions/n_agents/seed_j/Scene_i/trajectory.csv
                    df=pd.read_csv(pth)

                    scn_name=Path(scene).name
                    if scn_name not in scene_dicts.keys():
                        scene_dicts[scn_name]=make_scene_dict()
                        main_dict[scn_name]=scene_dicts[scn_name] # need to do once due to dict ptr
                        # 由于字典指针引用，只需设置一次

                    scn_dict=scene_dicts[scn_name]
                    for mn, mf in self.metrics.items():
                        scn_dict[mn].append(mf(df)) # add metrics to dict
                        # 将指标添加到字典

            main_dicts[extract_number(Path(n_agent).name)]=main_dict

        return main_dicts

    # multiagent metric - balance
    def _get_balance_metric(self, actions, success):
        """
        计算多智能体平衡度指标。

        平衡度衡量一个 episode 中各个智能体之间任务完成量的均衡程度。

        计算方法：
            设 x_i 为第 i 个智能体成功完成的任务数量。
            则平衡度 = min(x_0,...,x_n) / max(x_0,...,x_n)

        解释：
            - 该比值表示任务完成最少的智能体相对于最多的智能体的比例
            - 对于 >2 个智能体，该指标能很好地反映智能体间的活动范围差异（优于使用平均值的指标），
              可以轻松发现不成比例的贡献
            - 0 表示完全没有平衡（某个智能体没有完成任何任务）
            - 1 表示所有智能体之间完美平衡

        参数:
            actions (numpy.ndarray): 每步的动作字符串数组
            success (numpy.ndarray): 每步的成功状态数组

        返回:
            float: 平衡度值（0 到 1 之间）
        """
        def agentwise_actions(_filter=True):
            """
            按智能体分组统计成功动作，可选过滤掉 "Done" 和 "Idle" 动作。

            参数:
                _filter (bool): 是否过滤掉 "Done" 和 "Idle" 动作

            返回:
                defaultdict: {agent_id: [action1, action2, ...]}
            """
            agent_actions=collections.defaultdict(list)
            for action_str, success_str in zip(actions, success):
                # action
                if '[' not in action_str and ']' not in action_str:
                    action_lst= [action_str]
                else:
                    action_lst = ast.literal_eval(action_str)

                # success
                if '[' not in action_str and ']' not in action_str:
                    success_lst=[success_str]
                else:
                    success_lst = ast.literal_eval(success_str)
                for agent_id in range(len(action_lst)):
                    agent_act=action_lst[agent_id]
                    success_act=success_lst[agent_id]
                    if _filter and agent_act in ["Done", "Idle"]:
                        continue
                    # only add successful actions
                    # 仅统计成功执行的动作
                    if success_act:
                        agent_actions[agent_id].append(agent_act)
            return agent_actions

        def metric(a_actions):
            """
            计算平衡度值。

            参数:
                a_actions (defaultdict): {agent_id: [action1, action2, ...]}

            返回:
                float: min(各智能体动作数) / max(各智能体动作数)
            """
            # get lengths
            # 获取每个智能体的成功动作数量
            for k,v in a_actions.items():
                a_actions[k]=len(v)
            return min(a_actions.values()) / max(a_actions.values())

        return metric(agentwise_actions(_filter=True))

    def get_task_average(self, summary_dict):
        """
        计算每个任务内各指标在楼层平面之间的平均值（保持任务划分不变）。

        参数:
            summary_dict (dict): summarize() 返回的汇总字典

        返回:
            dict: 与 summary_dict 结构相同，但每个指标列表被替换为其平均值
        """
        s_dict=copy.deepcopy(summary_dict)
        for tsk,dct in s_dict.items():
            for metric,arr in dct.items():
                dct[metric]=np.array(arr).mean()
        return s_dict

    def get_overall_average(self, summary_dict):
        """
        计算所有任务和楼层平面的总体指标平均值。

        注意：该方法计算的是两层嵌套平均——
            1. 先在每个任务内对楼层平面取平均
            2. 再对所有任务取平均
        这与一次性对所有数据取平均的结果可能不同，因为不同任务的楼层平面数量可能不同。

        参数:
            summary_dict (dict): summarize() 返回的汇总字典

        返回:
            dict: 包含各指标总体平均值的字典
        """
        nd=dict([(k, []) for k in self.metrics.keys()])
        for tsk,dct in summary_dict.items():
            for metric,arr in dct.items():
                nd[metric].append(np.array(arr).mean())
        nd=dict([(k, np.array(v).mean()) for k,v in nd.items()])
        return nd
