import random
import warnings
import inspect
import random
import time
import functools
import re


# --- 从字符串中提取第一个数字 ---
def extract_number(s):
    """
    使用正则表达式从字符串中提取第一个数字。

    参数:
        s: 输入字符串（如 'Agent_2'）

    返回:
        int 或 None: 提取到的整数，如果没有数字则返回 None
    """
    l=re.findall(r'\d+',s)
    if len(l)==0:   return None
    return int(l[0])


# --- 连接列表为自然语言短语（含连接词） ---
def join_conjunction(l, conj):
    """
    将字符串列表用连接词组合成自然语言短语。
    例如: ['a', 'b', 'c'] + 'and' -> 'a, b, and c'

    参数:
        l:    字符串列表
        conj: 连接词（如 'and', 'or'）

    返回:
        str: 组合后的字符串
    """
    if len(l)<2: return str(l[0])
    if len(l)==2: return f"{l[0]} {conj} {l[1]}"
    return ", ".join(l[:-1]) + f", {conj} {l[-1]}"


# --- 将枚举值转换为小写字符串（去除类名前缀） ---
def read_enum(s):
    """
    将枚举值转换为小写字符串，去掉类名前缀。
    例如: FireType.A -> 'a'

    参数:
        s: 枚举值

    返回:
        str: 小写的枚举成员名
    """
    return str(s).split('.')[1].lower()


# --- 设置随机种子（用于结果复现） ---
def set_seed(num):
    """
    为 Python 内置 random 模块设置随机种子，确保实验可复现。

    参数:
        num: 随机种子值（整数）
    """
    random.seed(num)


""" ------ 装饰器 / wrapper 集合 ------ """

# 来源 - https://stackoverflow.com/questions/2536307/decorators-in-the-python-standard-lib-deprecated-specifically
def deprecated(comment=""):
    """
    标记函数为已弃用的装饰器。使用该函数时会发出 DeprecationWarning 警告。

    参数:
        comment: 附加的弃用说明文字

    返回:
        装饰器函数
    """
    def _deprecated(func):
        """This is a decorator which can be used to mark functions
        as deprecated. It will result in a warning being emitted
        when the function is used."""
        @functools.wraps(func)
        def new_func(*args, **kwargs):
            warnings.simplefilter('always', DeprecationWarning)  # turn off filter
            warnings.warn(f"Call to deprecated function {func.__name__}. {comment}",
                          category=DeprecationWarning,
                          stacklevel=2)
            warnings.simplefilter('default', DeprecationWarning)  # reset filter
            return func(*args, **kwargs)
        return new_func
    return _deprecated


# --- 字典哈希工具：将字典转换为不可变的 frozenset，用于缓存键 ---
def hash_dict(d):
    """
    将字典转换为可哈希的 frozenset，用于缓存键。
    列表值会被转为 tuple 以确保可哈希性。

    参数:
        d: 输入字典

    返回:
        frozenset: 可哈希的键值对集合
    """
    l=[]
    for k,v in d.items():
        if isinstance(v, list):
            v=tuple(v)
        l.append((k,v))
    return frozenset(l)


# --- frozenset 还原为字典 ---
unhash_dict=lambda fs : dict(list(fs))


# --- 装饰器：基于参数字典哈希的缓存机制 ---
def hashable_with_cache(fn):
    """
    装饰器：将函数参数中的字典进行哈希，使其可作为 functools.cache 的键。
    在 object_actions.py 中用于缓存 all_actions_embeddings 的嵌入向量。

    原理：
        - 对外暴露 _fn(unhashed_dct, **kwargs) 接口，接收普通字典
        - 内部通过 hash_dict() 将字典转为 frozenset，再调用缓存化的 __fn

    参数:
        fn: 被装饰的函数，第一参数为字典

    返回:
        _fn: 带有缓存能力的包装函数
    """
    @functools.cache
    def __fn(hashed_dct, **kwargs):
        return fn(unhash_dict(hashed_dct), **kwargs)
    def _fn(unhashed_dct, **kwargs):
        return __fn(hash_dict(unhashed_dct), **kwargs)
    return _fn


# --- 装饰器：基于时钟步长的 LRU 缓存（用于仿真循环中避免重复计算） ---
def clocked_cache(fn):
    """
    装饰器：基于 self.clock 的 LRU 缓存。在同一时钟 tick 内，相同参数的函数
    调用只执行一次，后续直接返回缓存结果。

    适用于仿真循环中每步需要多次查询但结果不变的场景。

    参数:
        fn: 被装饰的方法，第一个参数为 self

    返回:
        _fn: 带有时钟感知缓存能力的包装函数
    """
    @functools.lru_cache(maxsize=1)
    def __fn(self, clock, *args, **kwargs):
        return fn(self, *args, **kwargs)
    def _fn(self, *args, **kwargs):
        return __fn(self, self.clock, *args, **kwargs)
    return _fn


# --- 装饰器：函数执行时间计时器（调试用） ---
def timeit(f):
    """
    装饰器：打印被装饰函数的执行耗时（秒）。

    参数:
        f: 被装饰的函数

    返回:
        _time: 包装后的函数，执行时会自动打印耗时
    """
    def _time(*args, **kwargs):
        # time decorator
        a=time.time()
        output=f(*args, **kwargs)
        b=time.time()
        print(f"Function {f.__name__} took {b-a} seconds to run")
        return output
    return _time


""" --- 通用数学 / 字典工具函数 --- """


# --- 元组逐元素相加（自动补齐较短元组） ---
def tuple_add(t1, t2):
    """
    两个元组按元素相加，长度不足的元组自动补零。

    参数:
        t1, t2: 输入元组

    返回:
        tuple: 逐元素相加后的元组
    """
    t1l,t2l=len(t1),len(t2)
    lmax=max(t1l,t2l)
    # adds padding to max
    t1+=tuple(0 for _ in range(lmax-t1l))
    t2+=tuple(0 for _ in range(lmax-t2l))
    return tuple([t1v+t2v for t1v,t2v in zip(t1,t2)])


# --- 数值钳制 ---
def clamp(v, mi, ma):
    """
    将数值 v 限制在 [mi, ma] 区间内。

    参数:
        v:  输入值
        mi: 最小值
        ma: 最大值

    返回:
        钳制后的值（min(max(v, mi), ma)）
    """
    return max(min(v, ma), mi)


# --- 字典键转大写（就地修改） ---
def upper_keys(d):
    """
    将字典的所有键转换为大写字母，原地修改原字典。

    参数:
        d: 输入字典（会被修改）
    """
    for k in list(d.keys()):
        d[k.upper()]=d.pop(k)


# --- 简单属性容器类（类似命名元组，但允许属性赋值） ---
class Arg:
    """
    简单的属性容器，将关键字参数初始化为实例属性。
    类似 types.SimpleNamespace，用于传递灵活的参数字典。

    用法:
        config = Arg(lr=0.001, batch_size=32)
        print(config.lr)  # -> 0.001
    """
    def __init__(self, **kwargs):
        for k,v in kwargs.items():
            setattr(self,str(k),v)
