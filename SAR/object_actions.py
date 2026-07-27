import re
import copy
import torch
import functools
import pandas as pd
from sentence_transformers import SentenceTransformer
from pathlib import Path
from itertools import product
from core import Controller
from misc import extract_number, hashable_with_cache

script_dir = Path(__file__).parent.absolute()

# --- 全局变量：保存场景初始化参数（对象名称列表等）---
# 在首次调用 all_actions_embeddings 时通过 update_params() 设置
INIT_PARAMS=None

def update_params(object_dict : dict):
    """
    更新全局 INIT_PARAMS，供 get() 函数查询场景中各类对象的名称。

    参数:
        object_dict: 包含各类对象（reservoirs, deposits, fires, persons, agents）列表的字典
    """
    global INIT_PARAMS
    INIT_PARAMS=object_dict

def get(pois = None):
    """
    从 INIT_PARAMS 中获取指定类型（poi, points of interest）的所有对象名称列表。

    参数:
        pois: 字符串或字符串列表，指定要查询的类型（如 'reservoirs', 'fires'）。
              如果为 None，则返回所有类型的名称。

    返回:
        list[str]: 所有匹配对象的 name 属性列表
    """
    li_params=INIT_PARAMS
    assert li_params is not None, f"Cannot get name of objects if INIT_PARAMS is none."

    if isinstance(pois, str): pois=[pois]

    # 如果未指定类型，则获取所有类型
    if pois is None:
        pois=list(li_params.keys())

    l=[]
    for poi in pois:
        ll=li_params.get(poi, [])
        ll=list(map(lambda a : a.name, ll))
        l.extend(ll)
    return l # list of names for all poi(s) listed in the pois

def cardinal_directions():
    """
    获取可移动的方向列表（包含 Center/Stay 在原地）。

    返回:
        list[str]: 从 Controller.MOVABLE_CARDINAL_DIRECTIONS 获取的方向名称
    """
    return Controller.MOVABLE_CARDINAL_DIRECTIONS # includes the Center

def supply_types():
    """
    获取场景中的补给类型（如 Sand, Water），首字母大写格式化。

    返回:
        list[str]: 格式化后的补给类型名称列表
    """
    return list(map(lambda v : v.capitalize(), Controller.SUPPLY_TYPES))

# --- 规范动作格式说明 ---
# 以下是 LLM 输出的自然语言动作需要映射到的规范动作格式：
"""
Formatting for actions, required action_args:

'NavigateTo' : to_target_id (location),
'Move' : to_target_id (direction), # Up, Down, Left, Right
'Carry' : from_target_id (person),
'DropOff' : from_target_id (person), to_target_id (deposit),
'StoreSupply' : to_target_id (deposit),
'UseSupply' : from_target_id (fire), supply_type (on fire)
'GetSupply' : from_target_id (deposit or reservoir), supply_type (deposit)
'ClearInventory',
'NoOp',
"""

# ----------------------------

@hashable_with_cache
def all_actions_embeddings(object_dict : dict):
    """
    预计算所有规范动作的 Sentence-BERT 嵌入向量，结果会被 @hashable_with_cache 缓存。

    流程:
        1. 更新全局 INIT_PARAMS
        2. 从 object_dict 解析所有可导航的对象名称（排除 Fire 主体，保留 Fire_Region）
        3. 按动作类别（导航、搬运、补给等）构造规范动作字符串
        4. 用 SentenceTransformer 编码所有动作字符串为嵌入向量

    参数:
        object_dict: 包含 'available_object_names' 以及各类对象列表的字典

    返回:
        tuple: (all_actions, embeddings)
            - all_actions: list[str]，所有规范动作字符串
            - embeddings: torch.FloatTensor，对应的嵌入向量矩阵
    """

    # 更新全局 INIT_PARAMS 变量（供 get() 使用）
    assert object_dict is not None, f"Error, object dict for embedding all actions is none"
    update_params(object_dict)

    # --- 获取可导航的对象名称 ---
    available_object_names=object_dict.pop('available_object_names')
    # 过滤掉 Fire 本身（不含 _Region 后缀的），防止导航到整个火场
    def is_not_fire(s):
        if ('Fire' in s) and ('_Region' not in s):
            return False
        return True
    available_object_names=list(filter(is_not_fire, available_object_names))

    # --- 导航 / 移动动作 ---
    # 任何对象都可作为导航目标
    navigate=[f"NavigateTo({o})" for o in available_object_names]
    move=[f"Move({d})" for d in cardinal_directions()]
    explore=["Explore()"]

    # --- 搬运 / 放下动作 ---
    carry=[f"Carry({o})" for o in get('persons')]
    # 每个人可以放到任意 deposit
    dropoff=[f"DropOff({d}, {p})" for p,d in product(get('persons'), get('deposits'))]

    # --- 补给相关动作 ---
    store=[f"StoreSupply({o})" for o in get('deposits')]
    # 所有火情 x 所有补给类型的组合
    use=[f"UseSupply({f}, {s})" for f,s in product(get('fires'), supply_types())]
    # (deposit x 补给类型) + 从 reservoir 取水
    getsupply=[f"GetSupply({d}, {s})" for d,s in product(get('deposits'), supply_types())]
    getsupply+=[f"GetSupply({r})" for r in get('reservoirs')]
    # 清空库存
    clear=[f"ClearInventory()"]

    # --- 终止 / 空闲动作 ---
    done=[f"Done"]
    idle=[f"Idle"]

    # -----------------------------

    # --- 合并所有类别的动作到总列表 ---
    all_actions=[]
    all_actions.extend(navigate)
    all_actions.extend(move)
    all_actions.extend(explore)
    all_actions.extend(carry)
    all_actions.extend(dropoff)
    all_actions.extend(store)
    all_actions.extend(use)
    all_actions.extend(getsupply)
    all_actions.extend(clear)
    all_actions.extend(done)
    all_actions.extend(idle)

    # ----------------------------

    # 注意：不要使用针对 AI2Thor 微调的模型，它仅适用于 AI2Thor 领域
    # model = SentenceTransformer(str(Path(__file__).parent.absolute() / "sentence_transformer/finetuned_model"))
    embeddings = torch.FloatTensor(model.encode(all_actions))
    return all_actions, embeddings


# --- 全局加载 Sentence-BERT 模型（all-MiniLM-L6-v2，轻量高效）---
model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

# ---------------------------

def get_closest_feasible_action(action: str, object_dict : dict):
    """
    将 LLM 输出的自然语言动作映射到最相似的规范动作。
    例如: 'use sand supply on great fire' -> 'UseSupply(GreatFire, Sand)'

    步骤:
        1. 获取（或从缓存读取）所有规范动作的嵌入向量
        2. 对输入动作进行编码
        3. 计算余弦相似度，选出最匹配的规范动作
        4. 处理数字提取与替换（如果输入包含数字而规范动作没有，则使用默认值）

    参数:
        action:       LLM 生成的自然语言动作描述
        object_dict:  场景对象字典，用于构建规范动作列表

    返回:
        str: 最匹配的规范动作字符串（如 'UseSupply(GreatFire, Sand)'）
    """
    # 注意: 现在 get_closest_feasible_action 需要 object_dict，
    #       但已不再需要 get_closest_object_id 函数

    # --- 获取嵌入向量（缓存命中时直接返回）---
    all_actions, embeddings = all_actions_embeddings(copy.deepcopy(object_dict))

    # --- 对 LLM 输出进行编码并与所有规范动作计算余弦相似度 ---
    action_embedding = torch.FloatTensor(model.encode([action]))
    scores = torch.cosine_similarity(embeddings, action_embedding)
    max_score, max_idx = torch.max(scores, 0)
    pred_action=all_actions[max_idx]

    # --- 数字修正：如果输入动作中包含数字但最佳匹配中不包含，用 1 替换 ---
    intended_number=extract_number(action)
    actual_number=extract_number(pred_action)

    # 默认场景：输入中有数字而匹配动作中没有，则使用默认值 1
    if (intended_number is None) and (actual_number is not None):
        default_number=str(1)
        pred_action=pred_action.replace(str(actual_number), default_number)

    return pred_action
