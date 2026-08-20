from enum import Enum
from collections import defaultdict
import math
import copy
import functools, itertools
import warnings
import inspect
import random
import time
import uuid
import numpy as np
from misc import *

# ===================================================================
# Coordinate（坐标）类
# ===================================================================
class Coordinate:
    """
    坐标系统，默认 3x3x1 离散网格，XY 模式，原点在左上角。
    - WIDTH=3（x轴）、HEIGHT=3（y轴）、ALTITUDE=1（z轴）
    - 所有坐标值始终为离散整数
    - 支持设置坐标轴模式（如 'xz'）、边界检查、距离计算、邻域判断
    """

    WIDTH=3 # x轴宽度
    HEIGHT=3  # y轴高度
    ALTITUDE=1 # z轴高度

    ALL_AXES='xyz'
    AXES='xy'
    AXES_LIMITS={'x' : 'WIDTH', 'y' : 'HEIGHT', 'z' : 'ALTITUDE'}

    """ -------------------- 静态方法 -------------------- """
    @staticmethod
    def set_axes_mode(s : str):
        """ 设置坐标轴模式，例如 .set_axes_mode('xz')，顺序重要 """
        s=s.lower().strip()
        axes=''.join(list(s))
        Coordinate.AXES=axes

    @staticmethod
    def get_axes_limits():
        """
        获取各轴的最大值（含），例如默认返回 [2,2]。
        因为离散坐标从0开始，所以值=尺寸-1
        """
        return [getattr(Coordinate, Coordinate.AXES_LIMITS[ax])-1 for ax in Coordinate.AXES]

    """ 边界检查方法 """
    @staticmethod
    def within_bounds(*args):
        """ 检查坐标是否在网格边界内 """
        limits=Coordinate.get_axes_limits()
        # assert len(args)<=len(limits), f"Not enough axes provided, need all of {list(Coordinate.AXES)}."
        min_l=min(len(limits), len(args))
        for axv,lim in zip(args[:min_l], limits[:min_l]):
            if not (0<=axv<=lim): return False
        return True

    @staticmethod
    def assert_within_bounds(*args):
        """ 断言坐标在边界内，越界则抛出异常 """
        limits=Coordinate.get_axes_limits()
        assert Coordinate.within_bounds(*args), f"Coordinate not within bounds {list(Coordinate.AXES)} -> {limits}"

    """ 距离计算方法 """
    @staticmethod
    def delta(c1, c2):
        """ 计算坐标差 c1 - c2，返回元组 """
        t1=c1.get()
        t2=c2.get()
        return tuple([(v1-v2) for v1,v2 in zip(t1,t2)])

    @staticmethod
    def euclidean(c1, c2):
        """ 计算欧几里得距离 """
        dp=Coordinate.delta(c1,c2)
        return math.sqrt(sum([v**2 for v in dp]))

    @staticmethod
    def manhattan(c1, c2):
        """ 计算曼哈顿距离 """
        dp=Coordinate.delta(c1,c2)
        return sum([abs(v) for v in dp])

    """ 快速初始化 """
    def __init__(self, *args, radius=0):
        """
        初始化坐标点。
        如果提供了 args，则按 Coordinate.AXES 的顺序赋给各轴，
        不足的轴自动补0。
        - radius: 可见/交互半径
        """
        args=args+tuple( 0 for _ in range(len(Coordinate.AXES)-len(args)) ) # 补零

        self.r=radius
        self.coords={ax : axv for ax,axv in zip(Coordinate.AXES, args)}
        Coordinate.assert_within_bounds(*self.coords.values())

    @property
    def radius(self):
        """ 返回整数形式的半径 """
        return int(self.r)

    def __eq__(self, other):
        """ 判断两个坐标是否相等（比较所有属性） """
        return (self.r==other.r) and (self.coords == other.coords)

    """
    关于 getter 和 setter 的重要说明:
    getter  - 返回离散化后的位置（默认取整）
    setter  - 设置位置（含离散化处理）

    --- set(*get()) = 恒等操作 ---
    """

    def get(self, fn=int):
        """ 获取离散化后的坐标值（默认 fn=int 取整） """
        return tuple(fn(v) for v in self.coords.values())

    def set(self, *args, fn=lambda x:x, suppress_bounds=False):
        """ 设置坐标值，默认检查边界 """
        if not suppress_bounds:
            Coordinate.assert_within_bounds(*args)
        self.coords={ax : fn(axv) for ax,axv in zip(Coordinate.AXES, args)}

    def change_position(self, *dargs):
        """ 相对当前位置移动坐标（增量方式） """
        # @here
        args=self.get()
        dargs=dargs+tuple( 0 for _ in range(len(args)-len(dargs)) ) # 补零

        self.set(*tuple_add(args, dargs), suppress_bounds=True)

    """
    直接 getter 和 setter，绕过离散化处理，
    供引擎在非连续环境中直接访问位置
    """
    def direct_get(self):
        """ 直接获取坐标（不做取整处理） """
        return self.get(fn=lambda x:x)

    @deprecated(comment=".set(.) is already direct")
    def direct_set(self, *args):
        """ 直接设置坐标（已废弃，.set() 已经支持直接设置） """
        return self.set(*args, fn=lambda x:x)

    """
    单独的 set/get radius 函数，因为位置由引擎管理，半径是抽象概念
    """
    def set_radius(self, r : float): self.r=r
    def get_radius(self): return self.r

    # 改变底层网格尺寸
    @staticmethod
    def set_params(**kwargs):
        """ 设置网格参数（宽度、高度、高度等） """
        for axn, axv in kwargs.items():
            setattr(Coordinate, axn.upper(), axv)

    @staticmethod
    def get_params():
        """ 获取当前网格参数 """
        return {axn : getattr(Coordinate, axn) for axn in Coordinate.AXES_LIMITS.values()}


    """ 坐标间关系判断方法 """
    def within_radius(self, c):
        """
        判断坐标 c 是否在当前对象的半径范围内。
        排除自身引用的情况（id 相同返回 False）
        """
        # 如果是同一个对象，返回 False
        if id(self)==id(c):
            return False
        d=Coordinate.euclidean(self, c)
        """ 距离使用连续方式测量（避免离散化带来的歧义） """
        return (d <= self.radius)

    @staticmethod
    def neighboors(c1, c2, diagonal=False):
        """
        判断两个坐标是否为邻居。
        - diagonal=False 时仅检查4个主方向
        - diagonal=True 时还包括对角线方向
        - 如果 c1==c2 则返回 False
        """
        ptpl=c1.get()

        # 如果是同一个对象，返回 False
        if id(c1)==id(c2):
            return False

        for h in [-1,0,1]:
            for w in [-1,0,1]:
                if not diagonal and abs(h)+abs(w)>1:
                    continue
                if tuple_add(ptpl, (w,h))==c2.get():
                    return True
        return False

    @staticmethod
    def overlap(c1, c2):
        """ 判断两个坐标是否重叠（位置相同），而非半径重叠 """
        return c1.get() == c2.get()


# ===================================================================
# GPS — 全局定位系统
# ===================================================================
"""
重要说明！
全局定位系统（Global Positioning System）。
每当对象的坐标被设置或改变时，该类的静态变量会同步更新位置追踪。

所有具有指定位置和 id 属性的对象都会自动被追踪。
用于追踪世界状态的变化。
"""
class GPS:
    tracker=defaultdict(list)    # 位置 -> [对象id列表]
    id_mapping=defaultdict(None) # 对象id -> 对象引用

    @staticmethod
    def update_track(o, previous_position, future_position):
        """
        更新对象的位置追踪记录。
        - 从旧位置列表中移除
        - 添加到新位置列表
        - 更新全局 id_mapping
        """
        oid=o.id

        # 更新位置追踪
        prevc,futurec=previous_position.get(),future_position.get()
        if oid in GPS.tracker[prevc]:
            # 确保同一个对象不会同时出现在两个不同位置
            assert (prevc==futurec) or (oid not in GPS.tracker[futurec]), f"Object w/ id {oid} in two positions at once {previous_position.get()} and {future_position.get()}"
            # 从旧位置移除
            GPS.tracker[prevc].remove(oid)

        # 添加到新位置
        GPS.tracker[futurec].append(oid)

        # 添加到 id_mapping（如果尚未添加），确保始终有全局映射
        GPS.id_mapping[o.id]=o

    @staticmethod
    def at(position):
        """ 获取指定位置的所有对象 id 列表 """
        return GPS.tracker[position.get()]

    @staticmethod
    def near(central_position, radius):
        """
        获取中心位置 radius 范围内的所有位置及其对象 id 的字典。
        - 包括中心位置本身
        - 以欧几里得距离测量
        - 返回按距离排序的字典 {位置元组: [对象id列表]}
        """
        assert radius>=0, f"No negative radii accepted"
        within_positions=[]

        # 从中心位置搜索 radius 范围内的矩形区域
        half_l=math.ceil(radius/2)
        ctpl=central_position.get()
        for dx in range(-half_l, half_l+1):
            for dy in range(-half_l, half_l+1):
                ntpl=tuple_add(ctpl, (dx,dy))
                if Coordinate.within_bounds(*ntpl):
                    ncoord=Coordinate(*ntpl)
                    within_radius=( Coordinate.euclidean(ncoord, central_position) <= radius )
                    if within_radius: within_positions.append(ncoord)

        # 按距离中心点的远近排序
        within_positions.sort(key=lambda ncoord : Coordinate.euclidean(ncoord, central_position))

        d=dict([(p.get(),GPS.at(p)) for p in within_positions])
        return d

    @staticmethod
    def around(central_position, layer, inclusive, diagonal, get_out_of_bounds=False):
        """
        获取中心位置第 layer 层的所有位置及其对象 id 的字典。
        - layer: 第几层（0=中心，1=第一层，以此类推）
        - inclusive: 是否包含内层位置
        - diagonal: 是否包含对角线方向
        - get_out_of_bounds: 是否同时返回越界位置
        """
        assert layer>=0, f"No negative layers accepted"
        assert isinstance(layer, int), f"Layer must be int"

        def _in_layer(x,y,l):
            """ 判断 (x,y) 是否在第 l 层 """
            if l==0:
                # TODO: 如果不 inclusive 则改为 False？
                return (x==0) and (y==0) and inclusive

            diagonal_bool=(abs(x)!=abs(y)) if not diagonal else True
            inside_layer=(x<=l and y<=l) and diagonal_bool
            if inclusive:
                return inside_layer
            return inside_layer and not (x<=l-1 and x<=l-1)

        within_positions=[]
        out_of_bounds=[]

        # 从中心位置搜索 layer 层的矩形区域
        ctpl=central_position.get()
        for dx in range(-layer,layer+1):
            for dy in range(-layer,layer+1):
                ntpl=tuple_add(ctpl, (dx,dy))
                if Coordinate.within_bounds(*ntpl):
                    if _in_layer(dx,dy,layer):
                        ncoord=Coordinate(*ntpl)
                        within_positions.append(ncoord)
                else: out_of_bounds.append(ntpl)

        d=dict([(p.get(),GPS.at(p)) for p in within_positions])
        if not get_out_of_bounds:
            return d
        return d, out_of_bounds


# ===================================================================
# 装饰器定义
# ===================================================================
# named 装饰器：为类添加名称属性
def named(cls):
    """ 装饰器：为类添加 name 属性和 set_name/get_name 方法 """
    class NameWrapper(cls):
        def __init__(self, *args, **kwargs):
            try:
                self.name=kwargs.pop('name')
            except KeyError:
                self.name=None
            super().__init__(*args, **kwargs)
        def set_name(self, name : str):
            self.name=name
        def get_name(self):
            return self.name
    return NameWrapper

# with_id 装饰器：为对象分配唯一 UUID
def with_id(cls):
    """
    装饰器：为类添加唯一的 id 属性（UUID 格式）。
    id 格式为: {类名}|{UUID}
    """
    class IdWrapper(cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

        def class_name(self):
            """ 获取最底层非装饰器类名（过滤掉包含 'object' 或 'wrapper' 的基类） """
            fltr=lambda s : all([ts not in s.lower().strip() for ts in ['object', 'wrapper']])
            base_names=[b.__name__ for b in self.__class__.mro()]
            # 注意：此过滤假设所有装饰器类名都包含 "wrapper"！
            base_names=list(filter(fltr, base_names))
            bn=base_names[0] # 假定过滤后只剩一个
            return bn

        @functools.cached_property
        def id(self):
            """
            生成对象的唯一 id：{类名}|{UUID}。
            无论是否被 named 装饰器包装都能正常工作。
            """
            bn=self.class_name()
            unique_id=uuid.uuid4()
            _id=f"{bn}|{unique_id}"
            return _id

    return IdWrapper

# collidable 装饰器：设置对象的碰撞属性
def collidable(is_collidable):
    """
    装饰器工厂：设置对象是否可碰撞（collidable）。
    用于导航系统——若为 True，则智能体无法走过该对象。
    """
    def _collidable(cls):
        class CollidableWrapper(cls):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                setattr(cls, 'collidable', is_collidable)
        return CollidableWrapper
    return _collidable

# with_position 装饰器：为对象添加位置信息
def with_position(mutable):
    """
    装饰器工厂：为类添加坐标位置信息。
    - mutable: 布尔值，表示该位置是否可变（仅作为信息标记，不强制）

    技术说明：由于需要带参数，使用装饰器工厂模式。
    即使有默认参数，也必须调用 with_position() 才能使用 @ 语法。
    """
    def _with_position(cls):
        def generic_sees(self, othr):
            """ 通用可见性判断：对方是否在当前对象的半径范围内 """
            return self.position.within_radius(othr.position)

        def person_sees(self, othr, strict=False):
            """
            Person 特有的可见性判断：
            - 必须在半径范围内（与通用方法相同）
            - 且未被 deposit（已存放的人员不可见、不可交互）

            注意：Person 的特殊特性——
            如果任意智能体看见了此人员，则该人员的半径变为"无限"（整个地图大小）
            """
            is_agent=(othr.class_name()=="AbsAgent")
            can_see=self.position.within_radius(othr.position)
            # 如果被智能体看见，则标记为已发现，对所有智能体可见
            if is_agent and can_see:
                self.spotted=True

            # 如果已 deposited 则不可见
            bool_condition=can_see if strict else (can_see or self.spotted)
            return bool_condition and (not self.deposited)

        class PositionWrapper(cls):
            def __init__(self, *args, **kwargs):

                # 初始化位置，默认为 (0,0)
                try:
                    xx,yy=kwargs.pop('position')
                    self.position = Coordinate(xx,yy)
                except KeyError:
                    self.position = Coordinate(0,0)
                # 确保初始化时位置被 GPS 追踪
                self._update_track(future_coords=self.position.get())

                try:
                    r=kwargs.pop('radius')
                    self.position.set_radius(r)
                except KeyError:
                    # 半径默认为0
                    pass

                super().__init__(*args, **kwargs)

                # 根据类类型设置不同的 .sees() 方法
                # Person 类需要隐藏已存放对象的能力
                base_names=[b.__name__ for b in cls.mro()]
                if 'Person' in base_names:
                    setattr(cls, 'sees', person_sees)
                else:
                    setattr(cls, 'sees', generic_sees)

            # ------ 位置设置/获取方法 ----

            def _update_track(self, future_coords):
                """ 更新 GPS 位置追踪 """
                future_position=Coordinate(*future_coords)
                if hasattr(self, 'id'):
                    GPS.update_track(o=self, previous_position=self.position, future_position=future_position)

            def set_position(self, *args):
                """ 设置位置并更新 GPS 追踪 """
                self._update_track(args)
                self.position.set(*args)

            def set_position_direct(self, *args):
                """ 直接设置位置（绕过离散化）并更新 GPS 追踪 """
                self._update_track(args)
                self.position.direct_set(*args)

            def get_position(self):
                """ 获取离散化后的位置 """
                return self.position.get()

            def get_position_direct(self):
                """ 直接获取位置（不取整） """
                return self.position.direct_get()

            # ---------------------------

            def delta(self, othr):
                """ 计算与另一对象的坐标差（othr - self） """
                return Coordinate.delta(othr.position, self.position)

            def neighboors(self, othr, diagonal=True):
                """ 判断是否与另一对象相邻 """
                return Coordinate.neighboors(self.position, othr.position, diagonal=diagonal)

            def change_position(self,dx,dy):
                """ 相对移动位置（增量方式） """
                self.position.change_position(dx,dy)

            def set_radius(self, r : float):
                """ 设置可见/交互半径 """
                self.position.set_radius(r)

            def get_radius(self):
                """ 获取可见/交互半径 """
                return self.position.get_radius()

            @functools.cached_property
            def mutable_position(self):
                """ 返回位置的不可变性标记 """
                return mutable

        return PositionWrapper
    return _with_position


# ===================================================================
# Flammable（可燃物）类
# ===================================================================
"""
Flammable 类

- type: A/B
    - A 型需要水（water）扑灭
    - B 型需要沙（sand）扑灭

    参数（离散版本）:
    - intensity: NONE（无火）/ LOW（低）/ MEDIUM（中）/ HIGH（高）
    - 强度变更步数: LOW→MEDIUM: 4步, MEDIUM→HIGH: 3步
    - 蔓延临界强度: MEDIUM

    使用正确资源灭火:
    - 1 桶资源可降低 1 级强度，降到 NONE 即完全扑灭
    - 有溅射区域效果

    火势蔓延:
    - 初始时火势保持恒强度不会蔓延，直到被至少一个智能体发现
    - 火势只能蔓延到相邻的可燃物

    clock — 内部计时器
    onpause — 暂停时 .step() 无效（初始时暂停，直到被智能体发现）

    .light() — 从 NONE 点着为 LOW
        - 在初始化时调用
        - 由其他可燃物蔓延触发

    .step() — 执行强度更新（除非暂停中）
        - 条件满足时调用 .spread()
        - 更新 clock

    .spread() — 蔓延到其他可燃物
"""

Intensity = Enum('Intensity', ['NONE', 'LOW', 'MEDIUM', 'HIGH'])

# 装饰器组合说明：
#   collidable(False) → 不可碰撞，智能体可走过
#   with_id → 分配唯一 ID
#   with_position(mutable=False) → 位置不可变
#   named → 可命名
@collidable(is_collidable=False)
@with_id
@with_position(mutable=False)
@named
class Flammable:
    """ 单个可燃物单元，管理火灾强度、蔓延和灭火 """
    # TODO: 调整以下参数
    L_TO_M=3       # LOW→MEDIUM 所需时钟步数
    M_TO_H=3       # MEDIUM→HIGH 所需时钟步数
    CRITICAL_INTENSITY=Intensity.MEDIUM  # 可蔓延的临界强度
    TYPES=["A", "B"]  # 火灾类型

    def __init__(self, fire_type : str, intensity : Intensity = Intensity.NONE):
        """
        初始化可燃物。
        - fire_type: 'A' 或 'B'
        - intensity: 初始强度，默认为 NONE（无火）
        """
        self.fire_type=fire_type.upper().strip()
        assert self.fire_type in Flammable.TYPES, f"Extinguisher type for fire must be of one of the following types {Flammable.TYPES}"

        # 初始化火灾强度（NONE = 无火）
        self.intensity=intensity
        # 内部时钟，由 .step() 更新驱动强度变化
        self._clock = 0

    def on_fire(self):
        """ 是否正在燃烧（强度 > NONE） """
        return (self.intensity.value)>1

    def spreadable(self):
        """ 是否可蔓延（达到临界强度 MEDIUM 及以上） """
        return self.intensity.value >= Flammable.CRITICAL_INTENSITY.value

    def extinguish(self):
        """ 完全扑灭（强度直接设为 NONE） """
        self.intensity=Intensity.NONE
        return True

    def lessen(self, extinguisher_type : str):
        """
        降低一级强度（使用正确的灭火剂类型时）。
        如果灭火剂类型不匹配则返回 False。
        """
        extinguisher_type=extinguisher_type.upper().strip()
        assert extinguisher_type in Flammable.TYPES, f"Extinguisher type for fire must be of one of the following types {Flammable.TYPES}"

        # 使用正确的灭火剂类型时降低一级强度
        if self.fire_type == extinguisher_type:
            self.intensity=Intensity(
                    clamp(self.intensity.value-1, 1, 4)
                    )
            # 回退时钟到此强度级别的起点
            self.clock_back()
            return True
        return False

    def clock_back(self):
        """
        回退时钟到当前强度级别的起点。
        原因：如果智能体减弱了火势但仍在临界强度以上，
        下次 .step() 火势会恢复到之前的强度。
        通过回退时钟来抵消这个效应。
        """
        if self.intensity in [Intensity.LOW, Intensity.MEDIUM]:
            self._clock=0   # 回到起点
        elif self.intensity == Intensity.HIGH:
            self._clock=Flammable.L_TO_M    # 回到 MEDIUM 的起点

    def light(self, intensity=Intensity.LOW):
        """ 点燃可燃物（如果尚未着火），默认为 LOW 强度 """
        if not self.on_fire():
            self.intensity=intensity
            assert self.on_fire(), f"Flammable .light()-ed, but no .on_fire()"
            return True
        return False

    def step(self):
        """
        时钟驱动：更新火灾强度。
        - LOW → MEDIUM: 经过 L_TO_M 步
        - MEDIUM → HIGH: 再经过 M_TO_H 步
        - HIGH 保持高火势（永不自动减弱）
        仅当正在燃烧时才更新时钟。
        """
        if self.intensity == Intensity.LOW:
            if self._clock == Flammable.L_TO_M:
                self.intensity = Intensity.MEDIUM

        if self.intensity == Intensity.MEDIUM:
            if self._clock == Flammable.L_TO_M + Flammable.M_TO_H:
                self.intensity = Intensity.HIGH

        # 重要：只在着火时更新时钟
        if self.on_fire():
            self._clock+=1
            return True
        return False


# ===================================================================
# Fire（火灾聚合）类
# ===================================================================
@with_id
@named
@with_position(mutable=False)
@collidable(is_collidable=False)
class Fire:
    """
    火灾聚合类，管理一组 Flammable 对象的整体行为。
    包含多个可燃物单元的统一蔓延、灭火和强度计算逻辑。
    默认状态为 impotent（不活跃），需要被智能体发现后才激活。
    """

    STEP_EVERY=2  # 每2个全局时钟步执行一次 step

    def __init__(self, flammables=[], impotent=True):
        """
        初始化火灾对象。
        - flammables: 可燃物列表（已包装了相对位置信息）
        - impotent: 初始时是否休眠（不蔓延、不增长）

        注意：火灾默认处于 impotent（休眠）状态。
        这可以让智能体先发现火源，然后火势才开始快速蔓延。
        但休眠时间不能太长，否则智能体有足够时间收集所有资源。
        """
        # 断言所有可燃物的火灾类型一致
        if len(flammables)>0:
            tp=flammables[0].fire_type
            for fl in flammables:
                assert fl.fire_type==tp, f"Fire types of flammables in Fire must be equal."

        self.flammables = flammables

        # （对于可燃物）id → 对象的映射表，id 不变故静态
        self.id_mapping=dict([(o.id, o) for o in self.flammables])

        # 邻域关系优化：使用 defaultdict 代替嵌套循环，速度提升10倍
        self.dneighboor = defaultdict(list)
        for i,fl in enumerate(self.flammables):
            self._add_flammable_neighboors(fl)
            # 若初始化后设置了名称，则告知其所属火灾父对象
            fl.parent_name=self.get_name() # 告诉可燃物其所属火灾的名称

        self.impotent=True
        self._clock=0

    def steppable(self):
        """ 检查是否到了执行 step 的周期 """
        return (self._clock % Fire.STEP_EVERY == 0)

    @functools.cache
    def id_get(self, _id):
        """ 通过 id 获取可燃物对象 """
        o=self.id_mapping.get(_id, None)
        return o

    def make_impotent(self):
        """ 设置为休眠状态（不活跃） """
        self.impotent=True

    def make_potent(self):
        """ 设置为活跃状态（开始蔓延和增长） """
        self.impotent=False

    @functools.cached_property
    def fire_type(self):
        """ 获取火灾类型（基于第一个可燃物） """
        if len(self.flammables)>0:
            return self.flammables[0].fire_type
        return None

    @property
    def average_intensity(self):
        """
        计算火灾的平均强度。
        特殊规则：只要有任何可燃物在燃烧，平均强度至少为 LOW。
        """
        if len(self.flammables)>0:
            intensities=[f.intensity.value for f in self.flammables]
            avg=round(sum(intensities) / len(intensities))

            # 确保如果有任何物体在燃烧，平均值至少为 LOW
            # 假定 Intensity(1)=NONE
            if sum(intensities)-len(intensities)>0: avg=clamp(avg, 2, avg)
            return Intensity(avg)
        return None

    def average_neighboor_intensity(self, fl : Flammable):
        """
        获取指定可燃物及其邻居的最大强度（而非平均强度）。
        因为平均值可能掩盖问题来源。
        """
        intensities=[f.intensity.value for f in self.dneighboor[fl.id]]+[fl.intensity.value] # 包含自身
        maximum=max(intensities)
        return Intensity(maximum)

    def _add_flammable_neighboors(self, fl : Flammable):
        """ 建立指定可燃物的邻居关系（通过 GPS 位置查询） """
        dn=GPS.around(central_position=fl.position, layer=1, inclusive=True, diagonal=True)
        # dn 格式：位置 → [对象id列表]
        if len(dn.values())>0: neighboor_ids=functools.reduce(lambda l1,l2 : l1+l2, list(dn.values()))
        else: neighboor_ids=[]
        raw_neighboors=[self.id_mapping.get(nid,None) for nid in neighboor_ids]

        # 移除不在当前火灾实例中的对象（None）
        neighboors=list(filter(lambda o: o is not None, raw_neighboors))

        # 排除自身位置
        neighboors=list(filter(lambda o: o.get_position()!=fl.get_position(), neighboors))
        self.dneighboor[fl.id]=neighboors

    def add_flammable(self, fl : Flammable):
        """ 动态添加可燃物到火灾中，并更新邻居关系 """
        self.flammables.append(fl)
        # 告知其所属火灾名称
        fl.parent_name=self.name

        # 添加到 id 字典
        self.id_mapping[fl.id]=fl

        for fl in self.flammables:
            # 更新邻居关系
            self._add_flammable_neighboors(fl)

        return True

    def all_objects(self, expand=True, with_memory=False):
        """ 返回所有构成对象（不包括自身） """
        return self.flammables

    def spread(self):
        """
        实现火灾蔓延：对所有可蔓延的可燃物，向其所有邻居点燃。
        """
        for i,fl in enumerate(self.flammables):
            # 对每个可蔓延的可燃物
            if fl.spreadable():
                for nfl in self.dneighboor[fl.id]:
                    # 蔓延 → 点燃邻居（如果尚未着火）
                    success=nfl.light()

    def step(self):
        """
        整个火灾对象的 step 步进（聚合行为：先蔓延再步进）。

        条件：
        - 不处于 impotent 状态
        - 且到了执行周期（step every nth）
        """
        # 如果 impotent 则不更新
        # TODO: 注意此处逻辑，@here
        if not self.impotent:
            self._clock+=1 # 始终更新时钟
            if self.steppable():
                self.spread()
                # .step() 返回 True/False 只表示是否 impotent（火灾本身不会"失败"）
                for fl in self.flammables:
                    clk_before=fl._clock
                    success=fl.step()
                    clk_after=fl._clock
                    assert (clk_after-clk_before)==int(success), f"Successfully stepped but clock didn't change"
            return True
        return False

    def lessen(self, loc, extinguisher_type, diagonal=True):
        """
        在指定位置及邻近区域泼洒灭火剂。
        - loc: 目标位置（Coordinate 对象）
        - extinguisher_type: 灭火剂类型（A/B）
        - diagonal: 是否使用矩形溅射区域（否则十字形）

        返回是否有至少一个可燃物被成功减弱。

        注意：如果 loc 与某个可燃物的 .position 是同一个对象，
        Coordinate.neighboors 会返回 False（由于实现限制），
        因此我们对 loc 进行深拷贝。
        """
        loc=copy.deepcopy(loc)

        successes=[]
        for fl in self.flammables:
            if Coordinate.neighboors(fl.position, loc, diagonal=diagonal):
                success=fl.lessen(extinguisher_type)
                successes.append(success)
        any_success=any(successes)

        return any_success

    @staticmethod
    def procedural_generation(fire_type, amt_light, max_w, max_h, proportion_filled, top_left, amt_regions=None, fire_name=None, seed=42, shape='circle'):
        """
        火灾的程式化生成。
        根据给定的尺寸和填充比例，在指定区域生成火灾形状。

        参数:
        - fire_type: 火灾类型（A/B）
        - amt_light: 初始点燃的可燃物数量
        - max_w, max_h: 边界区域的宽高
        - proportion_filled: 填充比例
        - top_left: 区域左上角位置
        - fire_name: 火灾名称
        - seed: 随机种子
        - shape: 形状（当前仅支持 'circle'）

        支持形状:
        - random: 火灾随机蔓延（必须连通）
        - triangle: 火灾朝一维方向蔓延
        - circle: 火灾径向蔓延
        - lines: 火灾线性蔓延

        注意：所有 reservoir 和 deposit 应具有无限容量。
        """
        set_seed(seed)
        area=max_w * max_h
        x_bnd,y_bnd=max_w,max_h
        xc,yc=int(x_bnd/2),int(y_bnd/2)
        dx,dy=top_left
        fire_radius=None

        assert amt_light>=1, f"Cannot have no .light in a fire"

        # ------ 生成几何形状（放入 numpy 数组） ------
        if shape=='circle':
            # -------- 根据填充比例确定半径 ------
            if proportion_filled < math.pi/4:
                # 存在反函数
                r=math.sqrt(proportion_filled * area / math.pi)
            else:
                # 反函数 - 从半径近似填充比例
                # (radius,prop). (min(w,h),pi/4) to (1/2*sqrt(w^2+h^2),1)

                rise=(1-math.pi/4)
                run=(math.sqrt(max_w**2+max_h**2) - 1/2*min(max_w,max_h))
                b=math.pi/4
                xi=1/2*min(max_w,max_h)
                exp=4

                alpha=(1-b)/(run)**(1/exp)
                inv=lambda x : ((x-b)/alpha)**exp+xi

                r=inv(proportion_filled)

            # ------ 生成可燃物中心点 ------
            # 在半径为 (2/3)*r 的小圆上均匀分布
            rr=(2/3)*r
            rr=clamp(rr, 0, 1/2*min(max_w,max_h) * 3/4)
            init=random.random()*2*math.pi  # 随机起始角度

            angles=[(i*(2*math.pi / (amt_light))+init)%(2*math.pi) for i in range(amt_light)]
            # angles=[(i*(2*math.pi / (amt_regions))+init)%(2*math.pi) for i in range(amt_regions)]

            light_points=[( int(math.floor(rr)*math.cos(theta))+xc, int(math.floor(rr)*math.sin(theta))+yc) for theta in angles]
            random.shuffle(light_points) # 随机打乱

            # 确保所有点燃点不重复
            for p1 in light_points:
                for p2 in light_points:
                    if id(p1)==id(p2):
                        continue
                    assert p1!=p2, 'Bounding box or area is too small to fit all the initial .light() positions evenly'

            # ------ 创建可燃物 ------
            light_point_cnt=0
            flammables=[]
            for y in range(y_bnd):
                for x in range(x_bnd):
                    in_circle=math.sqrt((x-xc)**2 + (y-yc)**2)<r # 1 在圆内，0 在圆外

                    if in_circle:
                        ps=(x+dx,y+dy)
                        fl=Flammable(fire_type=fire_type, position=ps)
                        # 设置 1.5 层邻居半径
                        fl.set_radius(1.5*math.sqrt(2))

                        if (x,y) in light_points:
                            # 只点燃 amt_light 数量的点
                            if light_point_cnt<amt_light: fl.light()

                            # 如果启用火灾细分且提供了火灾名称，则为火焰区域命名
                            if Controller.FIRE_SUBDIVISION and (fire_name is not None):
                                fl.set_name(f"{fire_name}_Region_{light_point_cnt+1}")
                                light_point_cnt+=1

                        flammables.append(fl)

            # 设置火灾对象的半径
            fire_radius=math.ceil(r)

        else:
            raise NotImplementedError

        # 将火灾位置设为一个已点燃的区域（如果存在）
        x,y=light_points[-1]
        fire_position=(x+dx, y+dy)
        fire=Fire(flammables=flammables, position=fire_position, name=fire_name)

        # ------ 火灾创建后，为所有未命名区域生成额外区域以便访问 ------
        # 在火灾创建之后进行，因为此时已知所有邻居关系

        assert amt_regions is None, f"Giving specific amount of regions is a deprecated feature"

        def get_reached_flammables():
            """ 获取所有已被区域覆盖的可燃物（自身有名或有命名的邻居） """
            reached=set()
            for fl in flammables:
                # 如果已有名称（即是一个区域），则添加自身及其直接邻居
                if fl.get_name() is not None:
                    reached.add(fl)
                    for nfl in fire.dneighboor[fl.id]:
                        reached.add(nfl)
            assert len(reached)<=len(flammables), f"Cannot have more reached than available flammables"
            return reached

        get_unreached_flammables=lambda reached : set(flammables)-reached

        reached_flammables=get_reached_flammables()
        unreached_flammables=get_unreached_flammables(reached_flammables)

        # 不断为未覆盖的可燃物命名，直到全部覆盖
        while len(unreached_flammables)>0:
            # 随机采样
            fl=random.choice(list(unreached_flammables))

            # 通过命名将其设为区域
            fl.set_name(f"{fire_name}_Region_{light_point_cnt+1}")
            light_point_cnt+=1

            # 更新集合
            reached_flammables=get_reached_flammables()
            unreached_flammables=get_unreached_flammables(reached_flammables)

        # ----- 设置火灾半径 ----------
        fire.set_radius(fire_radius)

        return fire


# ===================================================================
# Reservoir（资源库）类
# ===================================================================
@with_id
@named
@with_position(mutable=False)
@collidable(is_collidable=True)  # 可碰撞，智能体不能走过
class Reservoir:
    """
    资源库，提供灭火资源（A型=水/沙，B型=沙/水）。
    默认资源无限量，除非指定 available 限制。
    """
    TYPES=['A', 'B']

    def __init__(self, resource_type, available=None):
        """
        初始化资源库。
        - resource_type: 资源类型 'A' 或 'B'
        - available: 可用数量，None 表示无限
        """
        # 未指定限制时默认为无限
        if available is None:
            available=math.inf

        assert resource_type.upper() in Reservoir.TYPES, f"Resource type must be of the following types only: {Reservoir.TYPES}"
        self.type=resource_type.upper()
        self.left=available

    def set_available(self, available : int):
        """ 设置可用资源数量 """
        assert available>=0, "Can't set available to negative number."
        self.left=available

    @property
    def available(self):
        """ 当前可用资源数 """
        return self.left

    @property
    def empty(self):
        """ 是否已空 """
        return self.left<=0

    def use(self, amt):
        """ 使用资源，返回实际使用的数量（不超过剩余量） """
        used=min(amt, self.left)
        self.left-=used
        return used


# ===================================================================
# Deposit（资源存放点）类
# ===================================================================
"""
Deposit 类

- storage: 存储字典 {类型: 数量}
- 具有位置属性和可见范围半径（形状为圆形）
"""
@with_id
@named
@with_position(mutable=False)
@collidable(is_collidable=True)
class Deposit:
    """
    资源/人员存放点。可用于存放灭火资源（A/B）和被救人员（PERSON）。
    默认容量无限，除非指定 capacity。
    """
    TYPES=['A', 'B', 'PERSON']
    PERSON=TYPES[-1]

    def __init__(self, capacity=None, storage=None):
        """
        初始化存放点。
        - capacity: 容量上限，None 表示无限
        - storage: 初始存储字典，需包含所有类型键
        """
        if capacity is None:
            capacity=math.inf  # 默认容量无限
        self.capacity=capacity

        if storage is None:
            storage=dict([(k,0) for k in Deposit.TYPES])
        else:
            upper_keys(storage)
            assert set(Deposit.TYPES)==set(storage.keys()), f"Storage dict (resource) provided @ init must have all the keys: {Deposit.TYPES}"

        self.storage=storage
        assert self.space<=capacity, f"Storage not within capacity {capacity}"

    def _assert_type(self, rtype):
        """ 断言资源类型有效 """
        assert rtype in Deposit.TYPES, f"Resource type must be of one of these types: {Deposit.TYPES}"

    @property
    def space(self):
        """ 剩余可用空间 """
        return self.capacity-sum(self.storage.values())

    @property
    def full(self):
        """ 是否已满 """
        return self.space==0

    def available(self, rtype : str):
        """ 查询指定类型资源的当前存量 """
        rtype=rtype.upper()
        self._assert_type(rtype)
        return self.storage[rtype]

    def use(self, rtype : str, amt : int):
        """ 从存放点取出指定类型和数量的资源 """
        rtype=rtype.upper()
        assert (rtype!=Deposit.PERSON), '.use(.) undefined for person (Deposit class)'
        self._assert_type(rtype)

        used=min(amt, self.available(rtype))
        self.storage[rtype]-=used
        return used

    def store(self, rtype : str, amt : int):
        """ 向存放点存储指定类型和数量的资源 """
        rtype=rtype.upper()
        self._assert_type(rtype)

        stored=min(amt, self.space)
        self.storage[rtype]+=stored
        return stored

    def store_all(self, storage : dict):
        """
        批量存储一个完整存储字典。
        如果空间不足则失败。
        """
        upper_keys(storage)
        assert set(Deposit.TYPES)==set(storage.keys()), f"Storage dict (resource) provided to store_all(.) must have all the keys: {Deposit.TYPES}"
        amt_storage=sum(storage.values())
        enough_space=(self.space>=amt_storage)
        if not enough_space:
            return False

        for k in self.storage.keys():
            self.store(k, storage[k])
        return True


# ===================================================================
# Person（被困人员）类
# ===================================================================
"""
Person 类

功能：
- 被困人员，需要 ≥2 个智能体协作搬运
- 支持 pick（拿起）/ drop（放下）操作
- 放下只在所有耦合的智能体都在存放点时才成功
- 一旦被 deposit，人员变为不可见

状态机：GRABBED（已被抓起） / GROUNDED（在地上）
"""
PersonStatus = Enum('PersonStatus', ['GRABBED', 'GROUNDED'])
""" 具有可见半径定义 """

@with_id
@named
@with_position(mutable=True)  # 位置可变（被智能体拖动时会跟随）
@collidable(is_collidable=True)  # 可碰撞
class Person:
    """
    被困人员类。
    - 需要至少 MIN_REQUIRED_AGENTS（默认2）个智能体协作才能搬起
    - 通过 coupled/uncoupled 字典管理智能体耦合状态
    - 具有 spotted 机制：一旦被任一智能体发现，对所有智能体可见
    """
    MIN_REQUIRED_AGENTS=2  # 拿起所需的最小智能体数量

    def __init__(self, extra_load=0):
        """
        初始化被困人员。
        - extra_load: 除最小需求外的额外负载（增加搬运难度）
        """
        assert extra_load>=0, f"Extra load cannot be negative"
        self.extraload=extra_load

        # 耦合字典：{智能体id: 智能体对象}，当前正在搬运此人的智能体
        self.coupled={}
        # 解耦字典：{智能体id: 智能体对象}，想要放下此人的智能体
        # 当解耦数量 >= 负载要求时，清空两者，成功放下
        self.uncoupled={}

        # 是否已成功放入 deposit（完成营救）
        self.deposited=False
        # 是否已被发现（被发现后对所有智能体可见）
        self.spotted=False

        self._clock=0

    @functools.cached_property
    def load(self):
        """ 搬运所需的总智能体数量（最小需求+额外负载） """
        return self.extraload+Person.MIN_REQUIRED_AGENTS

    @property
    def exceeded_load(self):
        """ 当前耦合的智能体数量是否已达到或超过负载要求 """
        return len(self.coupled.keys())>=self.load

    @property
    def status(self):
        """ 当前状态：GRABBED（已抓起）或 GROUNDED（在地上） """
        if self.exceeded_load: return PersonStatus.GRABBED
        else: return PersonStatus.GROUNDED

    def depositable(self, deposit : Deposit):
        """
        检查是否可存入指定 deposit：
        - 所有耦合的智能体必须在 deposit 的半径范围内
        - deposit 必须有空间

        注意：即使某些智能体是多余的（超过负载要求），
        所有耦合的智能体都必须靠近，这是为了保持现实性和惩罚冗余。
        """
        s=[]
        for agent_id, agent in self.coupled.items():
            close=deposit.sees(agent)
            s.append(close)

        enough_space=not deposit.full
        return all(s) and enough_space

    @property
    def grabbed(self):
        """ 是否已被抓起 """
        return (self.status==PersonStatus.GRABBED)

    def pick(self, agent):
        """
        尝试将智能体耦合到此人身上（开始搬运）。
        成功条件：
        - 智能体在可见范围内
        - 尚未被足够多的智能体抓起
        - 智能体当前没有搬运其他人
        """
        info={
            'visible' : None,
            'previously_picked' : None,
            }

        # 被搬起的前提：在人员半径范围内
        visible=self.sees(agent, strict=True)

        info['visible']=visible

        if visible and (not self.grabbed):
            # 将 self 参数给智能体用于记忆
            success=agent._add_person(self)
            info['previously_picked']=success
            if success:
                self.coupled[agent.id]=agent
                return True,info

        return False,info

    def drop(self, agent, deposit : Deposit):
        """
        尝试将人员放入 deposit。
        成功条件（全局）：
        1. 智能体之前已耦合（即已 pick 过）
        2. 所有耦合的智能体都执行了 drop
        3. 处于可存放状态（depositable）
        """
        info={
            'grabbed' : None,
            'depositable' : None,
            'all_dropped' : None,
            'interactable' : None,
            }
        info['interactable']=deposit.sees(agent)

        # 只有之前已耦合的智能体才能执行 drop
        local_success=agent.id in self.coupled.keys()

        if local_success:
            global_success,_info=self._drop(agent, deposit)
            for k,v in _info.items():   info[k]=v
            return global_success,info
        return False,info

    def _drop(self, agent, deposit : Deposit):
        """
        内部 drop 实现。
        要求所有耦合的智能体都执行 uncoupled 才能成功放下。
        放下后清空 coupled 和 uncoupled。

        注意：同一智能体重复执行 .drop() 不会重复计数。
        """
        # 第一步：如果距离足够（depositable），加入 uncoupled 列表
        depositable=self.depositable(deposit)
        if depositable:
            self.uncoupled[agent.id]=agent

        amt_uncoupled=len(self.uncoupled.keys())
        amt_coupled=len(self.coupled.keys())
        all_dropped=(amt_coupled == amt_uncoupled)

        grabbed=self.grabbed
        if grabbed: assert amt_coupled>=self.load, f"Something has gone wrong, object is grabbed, yet amount coupled isn't higher or equal to the load"

        # 放下条件：当前被抓起且可存放且所有智能体都执行了 drop
        droppable=grabbed and depositable and all_dropped
        info={
            'grabbed' : grabbed,
            'depositable' : depositable,
            'all_dropped' : all_dropped,
            }

        if droppable:
            # 清空耦合和解耦列表
            # @bug: 忘记移除耦合
            for _id, agent in self.coupled.items():
                agent._remove_person()

            self.coupled={}
            self.uncoupled={}

            # 存入 deposit（假设 deposit 是黑洞，人员将永远留在那里）
            used=deposit.store(Deposit.PERSON, 1)
            assert used>0, f"Something has gone wrong, depositable was true yet there was not space for deposit (deposit object was likely changed in-between)"

            # 将人员位置设置为 deposit 的位置
            self.set_position(*deposit.get_position())

            # 人员应变为不可见
            self.deposited=True
            return True, info
        return False, info

    def step(self):
        """
        人员步进更新：如果被抓起，跟随第一个耦合智能体的位置移动。
        具体由 Controller 决定是否拆分智能体，Person 本身不处理。
        """
        if self.grabbed:
            # 跟随第一个耦合智能体的位置移动
            first_key=list(self.coupled.keys())[0]
            agnt=self.coupled[first_key]
            self.set_position(*agnt.get_position())

        self._clock+=1
        return True


# ===================================================================
# AbsAgent（抽象智能体）类
# ===================================================================
"""
Abstract Agent 类

库存容量：最多 3 个物品 [slot, slot, slot]
    - 搬运人员占用全部库存（清除其他物品）

动作：
    - GetSupply / StoreSupply — 智能体和资源库/存放点在同一位置时成功
    - UseResource(resource) — 如果库存有正确的资源类型则使用
    - Carry / DropOff — 与人员交互

注意：需考虑库存信息在 LLM 中的表示方式
"""
@with_id
@named
@with_position(mutable=True)  # 位置可变（智能体可以移动）
@collidable(is_collidable=True)  # 可碰撞（其他对象不能走到智能体位置）
class AbsAgent:
    """
    抽象智能体类，仅处理数据和简单的交互函数。
    不包含行为决策（由 LLM/规划器处理）。

    重要实现细节：如果库存中有人，所有其他槽位将被清空并占满。
    """
    INVENTORY_CAPACITY=3  # 库存容量
    TYPES=['A', 'B']       # 资源类型
    PERSON='PERSON'        # 人员类型键

    def __init__(self, inventory=None):
        """
        初始化智能体库存。
        - inventory: 初始库存字典，需包含所有类型键
        """
        if inventory is None:
            inventory=dict([(k,0) for k in AbsAgent.TYPES])
        else:
            upper_keys(inventory)
            assert set(inventory.keys())==set(AbsAgent.TYPES), f"Wrong keys for inventory. Has: {inventory.keys()}, should have: {AbsAgent.TYPES}"

        # 初始化时不允许携带人员
        inventory[AbsAgent.PERSON]=0

        self.inventory=inventory
        assert self.available>=0, f"Too many objects in inventory {self.inventory}"

    @staticmethod
    def set_capacity(capacity):
        """ 设置全局库存容量 """
        AbsAgent.INVENTORY_CAPACITY=capacity

    @property
    def has_person(self):
        """ 是否正在搬运人员 """
        return self.inventory[AbsAgent.PERSON]>0

    def used_space(self, tp : str = None):
        """
        查询已用库存空间。
        - tp=None 返回总占用
        - tp 指定则返回该类型的占用
        """
        if tp is None:
            return sum(self.inventory.values())
        tp=tp.upper()
        assert tp in AbsAgent.TYPES, f"Wrong type for inventory. Tried: {tp}, should have: {AbsAgent.TYPES}"
        return self.inventory[tp]

    @property
    def occupied(self):
        """ 当前已占用的库存总量 """
        return self.used_space()

    @property
    def available(self):
        """ 当前可用库存空间 """
        return AbsAgent.INVENTORY_CAPACITY-self.occupied

    @property
    def full(self):
        """ 库存是否已满 """
        return self.available==0

    def clear_inventory(self, including_person=False):
        """
        清空库存（可选择是否包括人员）。
        返回被清空的总物品数。
        """
        cleared=0
        for k,v in self.inventory.items():
            if not including_person and k==AbsAgent.PERSON:
                continue
            cleared+=self.inventory[k]
            self.inventory[k]=0
        return cleared

    def add_inventory(self, tp : str, amt : int = 1):
        """
        向库存添加资源（非人员类型）。
        空间不足则返回 False。
        """
        tp=tp.upper()
        # 人员只能通过 Person 实例本身添加，避免混淆
        assert tp in AbsAgent.TYPES, f"Type to add to inventory must be of the following: {AbsAgent.TYPES}"

        if self.available>=amt:
            self.inventory[tp]+=amt
            return True
        return False

    def use_inventory(self, tp : str, amt : int = 1):
        """
        使用库存中的资源。
        如果该类型库存不足则返回 False。
        """
        tp=tp.upper()
        # 人员只能通过 Person 实例管理
        assert tp in AbsAgent.TYPES, f"Type to add to inventory must be of the following: {AbsAgent.TYPES}"

        if self.used_space(tp)>=amt:
            self.inventory[tp]-=amt
            return True
        return False

    def deposit_all_inventory(self, deposit):
        """
        将全部库存（不包括人员）存入 deposit。
        同时执行 .use_inventory() 和 deposit.store()。
        考虑交互距离（interactable）检查。
        """
        info={
            'interactable' : None,
            }
        close=deposit.sees(self)
        info['interactable']=close

        if close:
            # 复制库存字典，除去 PERSON 键
            inventory_fn=copy.deepcopy(self.inventory)
            inventory_fn[AbsAgent.PERSON]=0

            deposit_success=deposit.store_all(inventory_fn)
            if not deposit_success: # 空间不足
                return False,info
            # 清空库存
            self.clear_inventory(including_person=False)
            return True,info

        return False,info

    """ 以下函数由 Person 类在搬起/放下时调用，外部不应直接使用 """
    def _add_person(self, person):
        """
        仅在 Person 类中调用：添加人员到库存。
        如果已有人在库存则返回 False。
        添加人员会清空所有其他物品并占满全部库存。
        """
        if not self.has_person:
            # 添加人员前清空所有其他物品
            self.clear_inventory(including_person=False)
            # 人员占用全部库存容量
            self.inventory[AbsAgent.PERSON]=AbsAgent.INVENTORY_CAPACITY
            return True
        return False

    def _remove_person(self):
        """
        仅在 Person 类中调用：从库存移除人员。
        如果当前没在搬运人员则返回 False。
        """
        if self.has_person:
            self.inventory[AbsAgent.PERSON]=0
            return True
        return False


# ===================================================================
# Field（场景/世界场）类
# ===================================================================
"""
Field 类

管理的所有 POI（兴趣点）和智能体：
    - reservoirs（资源库）
    - deposits（存放点）
    - agents（智能体）
    - persons（人员）
    - fires（火灾）
    - geography（地理障碍）

接口：连接引擎和环境的共享表示。
功能：可见性追踪、部分观察生成、step 步进更新。
"""
class Field:
    """
    将所有 POI 和智能体集中管理。
    - 追踪可见性：哪些对象初始可见 + 当前可见
    - 根据可见性生成部分观察
    - 对所有对象执行 .step() 更新

    条件假设：
    - 许多函数被缓存，因为假定 Field 内容在初始化后保持不变（不动态添加对象）
    - 所有对象都有名称
    - 对象名称不会改变
    """

    RECURSIVE_CLASSES=['Fire']  # 需要递归展开的类

    CLASS_TYPES=['Flammable', 'Fire', 'Person', 'Reservoir', 'Deposit', 'AbsAgent']

    # deposit 内部类型 → LLM 可读类型的映射（全大写）
    READABLE_TYPE_MAPPER_RESOURCE={Deposit.TYPES[0] : "SAND", Deposit.TYPES[1] : "WATER", Deposit.TYPES[2] : "PERSON"}
    UNREADABLE_TYPE_MAPPER_RESOURCE=dict([(v,k) for k,v in READABLE_TYPE_MAPPER_RESOURCE.items()])
    PERSON=Deposit.PERSON

    # 火灾类型的可读映射
    READABLE_TYPE_MAPPER_FIRE={Flammable.TYPES[0] : "CHEMICAL", Flammable.TYPES[1] : "NON-CHEMICAL"}
    UNREADABLE_TYPE_MAPPER_FIRE=dict([(v,k) for k,v in READABLE_TYPE_MAPPER_FIRE.items()])

    def __init__(self, agents=[], persons=[], reservoirs=[], deposits=[], fires=[]):
        """
        初始化场景场。
        地图按渲染优先级排序（后渲染覆盖前渲染：reservoir 在 fires 之上）。
        """
        self.map={
                'fires' : fires,
                'reservoirs' : reservoirs,
                'deposits' : deposits,
                'agents' : agents,
                'persons' : persons,
                }

        # ----- 映射表构建 -----
        # id → 对象映射，id 不变故静态
        self.id_mapping=[]
        for poi,l in self.map.items():
            for idx,o in enumerate(l):
                self.id_mapping.append( ( o.id, o ) )

                # 递归枚举 — 确保内部对象也有映射！
                if o.class_name() in Field.RECURSIVE_CLASSES:
                    objs=o.all_objects(expand=True, with_memory=False)
                    for io in objs:
                        self.id_mapping.append( ( io.id,  io )  )

        self.id_mapping=dict(self.id_mapping)

        # 名称 → id 映射，名称不变故静态
        self.name_mapping=self.all_names(expand=True, dct=True)

        empty_map_d=lambda : dict([(k,[]) for k in self.map.keys()])
        # 每个智能体的可见性字典（不使用 defaultdict，以便捕捉错误）
        self.visibility = dict([(i,empty_map_d()) for i in range(len(agents))])

        # ----- 初始可见性设置 ------
        # 可见性字典结构：
        # { 智能体id : {'agents' : [索引列表], 'persons' : [], ...} }
        # fires 初始处于 impotent 状态，在这里设为 potent（使其可见）
        # 注意：如果想初始不可见（只在被发现后才激活），可移除以下行。
        # 目前假设"人员搜救"是唯一需要搜索的任务（火灾管理直接可见）
        # 另外，所有 deposits 初始可见
        # 注意：reservoir 的初始可见性由生成器决定，默认隐藏

        # 注意：Fires 初始可见
        self._initially_visible_pois=['fires', 'deposits', 'reservoirs']
        for poi in self._initially_visible_pois:
            self._make_allof_poi_visible(poi)

        self.update_visibility()

    def _make_allof_poi_visible(self, poi : str):
        """ 使指定类型的所有 POI 对所有智能体可见 """
        for agent_id, visd in self.visibility.items():
            visd[poi]+=[i for i in range(len(self.map[poi]))]

    @functools.cache
    def get(self, poi : str, idx : int = None):
        """
        获取 POI 对象。
        - poi: POI 类型名称
        - idx: 索引（None 时返回全部）
        """
        poi=poi.lower().strip()
        assert poi in self.map.keys(), f"POI: {poi}, must be one of the following: {self.map.keys()}"
        poi_l=self.map[poi]
        if idx is None:
            return poi_l
        return poi_l[idx]

    @functools.cache
    def id_get(self, _id):
        """ 通过 id 获取对象 """
        o=self.id_mapping.get(_id, None)
        return o

    @functools.cache
    def get_id(self, nm):
        """ 通过名称获取 id """
        oid=self.name_mapping.get(nm, None)
        return oid

    @functools.cache
    def name_get(self, nm):
        """
        通过名称获取对象。
        对于 Fire 类型，会调用其内部映射方法以支持 Fire 内部命名位置（如 southof+fire_name），
        返回对应的 Flammable 对象。
        """
        oid=self.get_id(nm)
        return self.id_get(oid)

    @functools.cache
    def all_names(self, expand=True, dct=False):
        """
        获取所有对象名称列表（可递归展开，如 Fire → fire + flammable）。
        注意：包含智能体对象。
        """
        objs=self.all_objects(expand=expand, with_memory=expand) # 包含 Fire 和 Flammable
        objs=list(filter(lambda o : o.get_name() is not None, objs)) # 过滤掉无名对象

        objs_name_dict=dict([(o.get_name(), o.id) for o in objs])
        if dct:
            return objs_name_dict
        return list(objs_name_dict.keys())

    @functools.cache
    def all_objects(self, expand=False, with_memory=False):
        """
        获取所有对象列表。
        - expand: 是否递归展开 Fire 等聚合类
        - with_memory: 是否同时保留聚合类本身和其子对象
        """
        objs=[]
        for poi,l in self.map.items():
            if expand and (len(l)>0) and (l[0].class_name() in Field.RECURSIVE_CLASSES):
                for o in l:
                    ll=o.all_objects(expand=expand, with_memory=with_memory)
                    objs+=ll
                # 展开时只添加子对象（Flammable），不添加 Fire 本身
                # 但如果 with_memory=True，则同时添加 Fire 和 Flammable
                if not with_memory: continue
            objs+=l
        return objs


    def update_visibility(self):
        """
        更新所有智能体的可见性。
        - 不包含智能体自身（但包含其他智能体）
        - 不包含障碍物（由引擎处理）

        通常对象一旦可见就永远可见，但 overwrite_visibility 中的类型除外（如 Person）。
        Person 具有 'spotted' 特性导致 .sees() 行为不同。
        """
        # 需要覆盖可见性的对象类型（之前可见不意味着现在仍然可见）
        overwrite_visibility=['persons', 'agents']

        for poi, objs in self.map.items():

            # 如果需要，重置可见性
            if poi in overwrite_visibility:
                for _ in range(len(self.map['agents'])): self.visibility[_][poi]=[]

            for idx,obj in enumerate(objs):
                for i,a in enumerate(self.map['agents']):
                    visd=self.visibility[i]
                    # 如果对象能"看见"智能体（即 POI 在对象的可见半径内），则添加

                    # 注意：不包括自身——因为 .sees() 对自己返回 False
                    obj_visible=obj.sees(a)

                    if obj_visible and (idx not in visd[poi]):
                        visd[poi].append(idx)

        # 使所有可见的火灾变为 potent（活跃）
        visible_fr_set=[]
        for i,a in enumerate(self.map['agents']):
            visible_fr_set+=self.visibility[i]['fires']
        visible_fr_set=set(visible_fr_set)

        for i,fr in enumerate(self.map['fires']):
            if i in visible_fr_set:
                fr.make_potent()

    def partial_observation(self, agent_idx : int, expand : bool = False):
        """
        获取智能体的抽象部分视觉观察。
        expand 为 True 时递归展开（添加可命名的子对象），
        返回可见对象集合（非 map 索引）。
        """
        # 每次获取部分观察时更新可见性（以防外部变化）
        self.update_visibility()

        obs=[]
        for poi,idxs in self.visibility[agent_idx].items():
            for idx in idxs:
                o=self.map[poi][idx]
                obs.append(o)

                if expand and o.class_name() in Field.RECURSIVE_CLASSES:
                    # 递归获取所有对象
                    objs=o.all_objects(expand=expand, with_memory=False)
                    # 注意：只添加有名称的展开对象（即在名称字典中存在）
                    has_name=lambda o : o.name in self.name_mapping.keys()
                    objs=list(filter(has_name, objs))
                    obs+=objs

        return obs

    def local_partial_observation(self, agent_idx : int, diagonal : bool = True):
        """
        获取具体局部观察（上、下、左、右、对角线方向）。
        返回以智能体位置为原点的增量字典。

        是递归的（尽可能精细），with_memory=False 以避免抽象。
        返回格式：{ (dx,dy): [对象列表] }

        注意：仅当需要文本输入描述视觉信息时才有用，否则是冗余的。
        """
        agent=self.get('agents', agent_idx)

        dct={}

        dn=GPS.around(central_position=agent.position, layer=1, inclusive=True, diagonal=True)
        # 过滤掉：不在 Field 中的对象、当前智能体自身、聚合类对象（如 Fire）
        for k,l in dn.items():
            # 注意：不包括 z 轴（局部观察不需要）
            deltak=Coordinate.delta(Coordinate(*k),agent.position)[:2]
            dct[deltak]=[]
            for v in l:
                if ((v not in self.id_mapping.keys()) or (v==agent.id)): continue
                o=self.id_mapping[v]
                # 不添加聚合类对象（Fire）
                if (o.class_name() in Field.RECURSIVE_CLASSES): continue

                dct[deltak].append(o)

        return dct


    def step(self):
        """
        对所有需要 step 更新的 POI 执行步进（火灾、人员等）。
        智能体本身没有 .step()（由引擎驱动）。
        """
        for poi,l in self.map.items():
            if len(l)>0 and hasattr(l[0], 'step') and callable(l[0].step):
                for obj in l: obj.step()

        # 不要在 .step() 中更新可见性 — 在 partial_observation 中延迟执行

    @staticmethod
    def procedural_generation(params : dict, seed : int = 42):
        """
        地图的程式化生成。

        参数:
        - grid_size: (width, height, altitude)
        - reservoirs: list of (tp, position, name)
        - deposits: list of (position, name)
        - fires: list of (tp, amt_light, enclosing_grid, position, name)
        - persons: list of (extra_load, position, name)
        - agents: list of (position, name)

        说明：
        - 所有 reservoir 和 deposit 默认无限容量
        - 生成地图地理障碍（collidable 对象）
        - 为主类生成名称（Fire 的内部可燃物名称在其自己的 proc. gen. 中处理）
        - 校准 POI 的可见半径
        """
        mapw,maph,mapal=params.pop('grid_size')
        area=mapw*maph
        Coordinate.set_params(width=mapw,height=maph,altitude=mapal)
        # @here
        # TODO: 仅在初始化时包含 z 轴（其他时候保持 xy）
        Coordinate.set_axes_mode('xyz')

        corners=[(x,y) for x,y in itertools.product([0,mapw-1],[0,maph-1])]

        field_params={}

        for poi,ll in params.items():
            field_params[poi]=[]
            for args in ll:
                if poi=='reservoirs':
                    # args -> tp,position,name
                    # 注意：无限容量
                    o=Reservoir(args.tp, math.inf, position=args.position)
                    o.set_radius(3*math.sqrt(2)) # 2层邻居范围（含对角线）
                    o.set_name(args.name)
                elif poi=='deposits':
                    # args -> position,name
                    # 注意：无限容量
                    o=Deposit(capacity=math.inf, position=args.position)
                    o.set_radius(3*math.sqrt(2)) # 2层邻居范围（含对角线）
                    o.set_name(args.name)
                elif poi=='fires':
                    # args -> tp,amt_light,enclosing_grid,name,position (topleft)
                    max_w,max_h=args.enclosing_grid
                    o=Fire.procedural_generation(fire_type=args.tp, amt_light=args.amt_light, amt_regions=args.amt_regions, max_w=max_w, max_h=max_h, proportion_filled=.85, top_left=args.position, fire_name=args.name, seed=seed, shape='circle')
                    # *半径已在生成器中设置
                    # *名称已在生成器中设置
                elif poi=='persons':
                    # args -> extra_load,position,name
                    o=Person(extra_load=args.extra_load, position=args.position)
                    o.set_name(args.name)

                    # 注意：可见范围占据地图面积的一定比例（如果位于中间）
                    # 使用从"到角落最大距离"的线性插值
                    if hasattr(args, 'find_probability'):
                        FIND_PROBABILITY=args.find_probability
                        assert 0<FIND_PROBABILITY<=1, f"Find probability for person {o.get_name()} not within (0,1]"
                    else: FIND_PROBABILITY=.20
                    max_corner_distance=max([Coordinate.euclidean(o.position, Coordinate(*corner)) for corner in corners])

                    o.set_radius(FIND_PROBABILITY*max_corner_distance)
                elif poi=='agents':
                    # args -> position,name
                    o=AbsAgent(position=args.position)
                    o.set_radius(3*math.sqrt(2)) # 2层邻居范围（含对角线）
                    o.set_name(args.name)

                field_params[poi].append(o)
        field=Field(**field_params)

        return field


# ===================================================================
# Controller（控制器）类
# ===================================================================
class Controller:
    """
    控制器类：接收 LLM 动作指令，调用 Field/Backend 执行，
    生成观察结果。

    是 LLM 与仿真环境之间的核心接口。
    - 定义动作空间（移动、搬运、资源管理等）
    - 处理动作的分发和执行
    - 生成全局和局部观察
    - 管理环境时钟和步进同步
    """

    # 动作空间定义
    MOVEMENT_ACTIONS=['NavigateTo', 'Move']
    CARRY_DROP_ACTIONS=['Carry', 'DropOff']
    SUPPLY_ACTIONS=['StoreSupply', 'UseSupply', 'GetSupply', 'ClearInventory']
    ALL_ACTIONS=MOVEMENT_ACTIONS+CARRY_DROP_ACTIONS+SUPPLY_ACTIONS

    # --- 来自 Field 的引用 ---
    CLASS_TYPES=Field.CLASS_TYPES
    # 供应类型（排除 PERSON）
    SUPPLY_TYPES=list(filter(lambda k : k!=Field.PERSON, Field.UNREADABLE_TYPE_MAPPER_RESOURCE.keys()))

    # 注意：设为 True 则将火灾细分为区域
    FIRE_SUBDIVISION=True
    # 注意：设为 True 则在描述火灾时告诉 LLM 火灾类型
    TELL_FIRE_TYPE=True
    # 注意：设为 True 则过滤掉没有燃烧对象的区域
    FILTER_REGIONS=True

    # 定义：基本方向和角落
    CARDINAL_VERTICAL={
            'Up' : (0,-1), # y 轴翻转（屏幕坐标系）
            'Down' : (0,1),
            }
    CARDINAL_HORIZONTAL={
            'Left' : (-1,0),
            'Right' : (1,0),
            }

    # 注意：用于简化的移动（如需更多方向可修改此列表）
    #        包含中心停驻
    MOVABLE_CARDINAL_DIRECTIONS=list(CARDINAL_VERTICAL.keys())+list(CARDINAL_HORIZONTAL.keys())+['Center']

    # 方向名称 → (dx,dy) 的映射
    TO_DELTA_MAP=[(k,v) for k,v in CARDINAL_VERTICAL.items()]+[(k,v) for k,v in CARDINAL_HORIZONTAL.items()]
    for kvert,vvert in CARDINAL_VERTICAL.items():
        for khorz, vhorz in CARDINAL_HORIZONTAL.items():
            TO_DELTA_MAP.append((kvert+khorz, tuple_add(vvert,vhorz)))
    TO_DELTA_MAP.append(('Center', (0,0)))
    TO_DELTA_MAP=dict(TO_DELTA_MAP)

    # (dx,dy) → 方向名称 的反向映射
    FROM_DELTA_MAP=dict([(v,k) for k,v in TO_DELTA_MAP.items()])

    # 上次初始化的参数（全局变量，用于创建可行动作时访问对象名称）
    # 假设：如果创建多个 Controller，它们的环境初始化相同
    LAST_INIT_PARAMS=None

    @staticmethod
    @deprecated() # 不再支持 — 改用 Scenes/scene_{i} 文件
    def pg_params(agent_names=['Alice', 'Bob', 'Charlie', 'David', 'Emma', 'Finn'], scene=None):
        """
        已废弃 — 生成场景参数的旧方法。
        请改用 Scenes/ 目录下的场景定义文件。
        """
        num_agents=len(agent_names)
        if scene is None: scene=1
        if scene==1:
            assert 1<=num_agents<=6, f"For the 'area_900_allones_default' initialization min: 1 and max: 6 are supported not {num_agents}"
            params={
                    'grid_size' : (30,30,1),
                    'reservoirs' : [
                        Arg(tp='a',name='ReservoirUtah',position=(15,5)),
                        Arg(tp='b', name='ReservoirYork',position=(18,8))
                        ],
                    'deposits' : [
                        Arg(name='DepositFacility', position=(23,12)),
                        ],
                    'fires' : [
                        Arg(tp='a', amt_light=2, amt_regions=None, enclosing_grid=(6,6), name='CaldorFire', position=(2,2)),
                        Arg(tp='b', amt_light=2, amt_regions=None, enclosing_grid=(6,6), name='GreatFire', position=(20,16))
                        ],
                    'persons' : [
                        Arg(extra_load=0,name='LostPersonTimmy',position=(5,25)),
                        ],
                    'agents' : [
                        Arg(position=(9,7)),
                        Arg(position=(17,17)),
                        Arg(position=(16,18)),
                        Arg(position=(10,17)),
                        Arg(position=(11,18)),
                        Arg(position=(25,25))
                        ][:num_agents],
                    }
        else:
            raise NotImplementedError

        # 为智能体命名
        for i,args in enumerate(params.get('agents',[])):
            args.name=agent_names[i]

        # 注意（有用）：命名约定。名称必须包含类类型（智能体除外）
        for poi,l in params.items():
            if poi in ['grid_size','agents']: continue
            o_type=poi[:-len('s')].capitalize()
            for i,arg in enumerate(l):
                assert o_type in arg.name, f"Object of type {o_type} in initialization does not contain '{o_type}' in name {arg.name}"

        # 重要 — 更新全局变量 LAST_INIT_PARAMS，用于可行动作映射
        Controller.LAST_INIT_PARAMS=params

        return params

    def __init__(self, procedural_generation_parameters, seed=42):
        """
        初始化控制器。
        - procedural_generation_parameters: 场景生成参数
        - seed: 随机种子
        """
        self.num_agents=len(procedural_generation_parameters['agents'])

        self.field=Field.procedural_generation(params=procedural_generation_parameters, seed=seed)
        assert (len(self.field.get('agents'))==self.num_agents), f"Number of agents provided in parameters {len(self.field.get('agents'))} does not match argument {self.num_agents}"

        # 创建后端引擎（grid 模式），不包含 Fire 等聚合类抽象对象
        self.backend=Backend(objects=self.field.all_objects(expand=True, with_memory=False), engine='grid')
        # 初始化每个智能体的时钟
        self.clock=dict([(idx, 0) for idx in range(self.num_agents)])


    @property
    def all_names(self):
        """
        获取所有对象的名称列表。
        如果启用了 FILTER_REGIONS，过滤掉没有活跃火灾的区域。
        """
        names=self.field.all_names(expand=True, dct=False)
        # @here
        # 由于此函数未缓存，可以在过滤器中处理
        def region_has_fire(nm):
            """ 过滤掉没有活跃火灾的区域（平均邻居强度为 None） """
            if '_Region' not in nm:
                return True
            fl=self.name_get(nm)
            fr=self.name_get(fl.parent_name)
            intensity=fr.average_neighboor_intensity(fl)
            return read_enum(intensity).capitalize()!='None'
        names=list(filter(region_has_fire, names))
        return names

    def _monolithic_step(self, agent_idx):
        """
        集中式步进：在所有智能体完成动作后统一更新环境。
        如果当前不是所有智能体都已执行过动作，则不做更新。

        重要：通过比较各智能体时钟是否都 > 0 来判断是否该更新。
        Field.step() 仅对火灾和人员等对象执行更新。
        """
        self.clock[agent_idx]+=1

        # 检查是否是第一次执行（clock 为 0 时不执行）
        vl=self.clock[0]
        if not (vl>0): return  # clock 为 0 时不执行

        # 如果有任何智能体时钟 < 1，则不执行（等待所有智能体完成）
        for agent_idx,clk in self.clock.items():
            if clk<1: return

        # 重置所有时钟并执行 Field 的 step 更新
        self.clock=dict([(idx, 0) for idx in range(self.num_agents)])
        self.field.step()

    def step(self, action : str, **action_args):
        """
        .raw_step() 的封装，自动生成观察结果并执行集中式步进。

        执行顺序：raw_step → 获取观察 → 集中式步进
        原因：如果是最后一个智能体，我们不希望其观察来自下一步。
        如果是第一个智能体，其观察在上一步结束后已更新。
        """
        event=self.raw_step(action, **action_args)

        agent_idx=action_args['agent_idx']
        self.get_observation(agent_idx=agent_idx, dct=event) # 在原位更新 event 字典
        # 执行集中式步进 — 在获取观察之后
        inert_step=action_args.get('inert_step', False)
        if not inert_step: self._monolithic_step(agent_idx)

        return event

    # 注意：这是没有全局/局部观察和集中式步进的原始 .step()
    def raw_step(self, action : str, **action_args):
        """
        执行原始动作指令（不包含观察生成和时钟步进）。
        注意：确保不会因"错误"动作导致运行时错误。

        动作参数格式:
        'NavigateTo' : to_target_id (目标位置id),
        'Move' : to_target_id (方向), # Up, Down, Left, Right
        'Carry' : from_target_id (人员),
        'DropOff' : from_target_id (人员), to_target_id (存放点)
        'StoreSupply' : to_target_id (存放点),
        'UseSupply' : to_target_id (火灾), supply_type (灭火剂类型)
        'GetSupply' : from_target_id (存放点/资源库), supply_type (资源类型)
        'ClearInventory',
        'NoOp',

        错误类型:
        - restricted_action（受限制动作）
        - not_visible（不可见）
        - not_interactable（不可交互）
        """
        agent_idx=action_args['agent_idx']
        agent=self.field.get('agents', agent_idx)

        event={
            'success' : None,
            'global_obs' : None,
            'local_obs' : None,
            'visual_obs' : None,
            'error_type' : '',
            'info' : '',
            }

        # 不同动作可能需要的参数集合
        from_target_id=action_args.get('from_target_id', None)
        to_target_id=action_args.get('to_target_id', None)
        supply_type=action_args.get('supply_type', None)

        # 注意检查顺序：
        # 1. 先检查 NoOp（总是成功）
        # 2. 再检查受限动作（让 LLM 理解失败的根本原因不是"不可见")
        # 3. 最后检查对象是否在可见范围内

        # -----------------------------------------------------------------------------
        if action=='NoOp':
            # 空操作 — 直接跳过
            event['success']=True
            return event

        # -----------------------------------------------------------------------------
        # 如果智能体正在搬运人员，只能执行移动和搬运相关动作
        # 注意：LLM 需要在环境规则中明确这一点
        if agent.has_person and ((action not in Controller.MOVEMENT_ACTIONS) and (action not in Controller.CARRY_DROP_ACTIONS)):
            event['success']=False
            event['info']='person in inventory; no non-movement actions allowed'
            event['error_type']='restricted_action'
            return event
        # -----------------------------------------------------------------------------

        # （重要）：在错误消息（error_type）中区分"不可交互"、"不可见"和"其他"
        # 导航动作不需要交互性检查（否则需要微移才能交互）

        # -----------------------------------------------------------------------------
        # 获取智能体全局可见的所有对象 id（供动作参数验证）
        globally_visible_ids=self.get_globally_visible_ids(agent_idx)
        # from 和 to 目标都必须在智能体的视野内！
        # id 也可能是方向名称的特殊情况（如 Move 动作）
        cant_see=lambda _id : (_id is not None) and (_id not in globally_visible_ids) and (_id not in Controller.TO_DELTA_MAP.keys())
        if cant_see(from_target_id) or cant_see(to_target_id):
            event['success']=False
            event['error_type']='not_visible'
            return event
        # -----------------------------------------------------------------------------

        # 移动原语
        movement_actions=Controller.MOVEMENT_ACTIONS

        # -----------------------------------------------------------------------------
        if action in movement_actions:
            if action=='NavigateTo': # 导航到目标
                # 注意：可以导航到另一个智能体（如果半径足够大），因为不需要站在它上面
                target_obj=self.id_get(to_target_id)
                if target_obj is None:
                    event['success']=False
                    event['error_type']='invalid_target'
                    event['info']=f"invalid_target: {to_target_id}"
                    return event

                eps_radius=target_obj.get_radius()
                success,info=self.backend.navigate(agent, target_obj.position, eps=eps_radius)
                event['success']=success
                # 导航成功与否取决于是否在视野内和后端因素，交互性不重要

            elif action=='Move':
                direction=to_target_id
                if direction not in Controller.MOVABLE_CARDINAL_DIRECTIONS:
                    event['success']=False
                    event['error_type']='invalid_direction'
                    event['info']=f"invalid_direction: {direction}"
                    return event

                success=self.backend.move(agent, direction)
                event['success']=success
                # 交互性在此不适用

        # -----------------------------------------------------------------------------
        carry_drop_actions=Controller.CARRY_DROP_ACTIONS
        if action in carry_drop_actions: # 人员搬运
            person=self.id_get(from_target_id)

            if action=='Carry':
                # 使用 Person.carry(agent) 方法
                if person is None:
                    event['success']=False
                    event['error_type']='invalid_target'
                    event['info']=f"invalid_target: {from_target_id}"
                elif person.class_name()!='Person':
                    event['success']=False
                    event['info']='cannot carry non-person'
                else:
                    success,info=person.pick(agent)
                    event['success']=success
                    # 可见半径 ≠ 交互距离（对于第一个发现者以外的智能体，由于 'spotted' 机制）
                    if not info['visible']:
                        event['error_type']='not_interactable'

            elif action=='DropOff':
                # 使用 Person.drop(agent) 方法
                deposit=self.id_get(to_target_id)

                if person is None:
                    event['success']=False
                    event['error_type']='invalid_target'
                    event['info']=f"invalid_target: {from_target_id}"
                elif deposit is None:
                    event['success']=False
                    event['error_type']='invalid_target'
                    event['info']=f"invalid_target: {to_target_id}"
                elif person.class_name()!='Person':
                    event['success']=False
                    event['info']='cannot drop-off non-person object'
                elif deposit.class_name()!='Deposit':
                    event['success']=False
                    event['info']='cannot drop-off person into non-deposit object'
                else:
                    success,info=person.drop(agent, deposit)
                    event['success']=success
                    if not info['interactable']: # deposit 距离不够
                        event['error_type']='not_interactable'

        # -----------------------------------------------------------------------------
        supply_actions=Controller.SUPPLY_ACTIONS
        if action in supply_actions: # 资源/存放点/人员相关
            # 如果存在 supply_type，转换为内部可读格式
            if supply_type is not None:
                try:
                    supply_type=Field.UNREADABLE_TYPE_MAPPER_RESOURCE[supply_type.upper()]
                except (KeyError, AttributeError):
                    event['success']=False
                    event['error_type']='invalid_supply_type'
                    event['info']=f"invalid_supply_type: {supply_type}"
                    return event

            if action=='StoreSupply':
                # StoreSupply — 从智能体库存转移到 deposit
                deposit=self.id_get(to_target_id)

                if deposit is None:
                    event['success']=False
                    event['error_type']='invalid_target'
                    event['info']=f"invalid_target: {to_target_id}"
                elif deposit.class_name()!='Deposit':
                    event['success']=False
                    event['info']='cannot drop-off supplies to non-deposit'
                else:
                    success,info=agent.deposit_all_inventory(deposit)
                    if not info['interactable']:
                        event['error_type']='not_interactable'
                    event['success']=success

            elif action=='UseSupply':
                # UseSupply — 使用库存中的灭火资源
                fire=self.id_get(to_target_id)

                if fire is None:
                    event['success']=False
                    event['error_type']='invalid_target'
                    event['info']=f"invalid_target: {to_target_id}"
                else:
                    if fire.class_name()=='Flammable':
                        # 如果目标直接是 Flammable，获取其父 Fire
                        fire=self.name_get(fire.parent_name)

                    if fire is None or fire.class_name()!='Fire':
                        event['success']=False
                        event['info']='cannot use supplies for a non-fire'
                    else:
                        # 每次只使用 1 单位资源（给资源收集者留出时间）
                        # 鼓励多智能体协作

                        interactable=fire.sees(agent)

                        if interactable:
                            use_success=agent.use_inventory(tp=supply_type, amt=1)
                            if use_success:
                                lessen_success=fire.lessen(loc=agent.position, extinguisher_type=supply_type, diagonal=True)

                            # 注意：成功只根据资源是否消耗和是否至少减弱了一个 Flammable 来判断
                            # 不要求火势完全熄灭
                            event['success']=use_success and lessen_success
                        else:
                            event['error_type']='not_interactable'
                            event['success']=False

            elif action=='GetSupply':
                # 注意：差异性速率限制 — reservoir 每次 1 单位，deposit 可取全部库存
                # 这种速率限制使得 deposit 有实际用途（作为缓冲）
                dropoff=self.id_get(from_target_id)

                if dropoff is None:
                    event['success']=False
                    event['error_type']='invalid_target'
                    event['info']=f"invalid_target: {from_target_id}"
                    return event

                interactable=dropoff.sees(agent)

                if dropoff.class_name()=='Deposit':
                    agent_space=agent.available # 取决于物品种类和库存大小

                    # 如果在半径范围内
                    if interactable:
                        resource_extracted=dropoff.use(supply_type, agent_space)
                        add_success=agent.add_inventory(tp=supply_type, amt=resource_extracted)
                        use_success=(resource_extracted>0)
                        event['success']=use_success and add_success
                    else:
                        event['success']=False
                        event['info']='Not within radius of deposit'
                        event['error_type']='not_interactable'

                elif dropoff.class_name()=='Reservoir':
                    if dropoff.sees(agent):
                        # reservoir 每次只提供 1 单位（应为无限容量）
                        supply_type=dropoff.type
                        used=dropoff.use(1)
                        get_success=(used>0)
                        assert get_success, 'Reservoir not of infinite capacity, update this fn to work with finite capacity ones.'
                        if get_success:
                            add_success=agent.add_inventory(tp=supply_type, amt=1)
                        event['success']=get_success and add_success
                    else:
                        event['success']=False
                        event['info']='Not within radius of reservoir'
                        event['error_type']='not_interactable'

                else:
                    event['success']=False
                    event['info']='cannot get supplies from non-deposit/non-reservoir source'

            elif action=='ClearInventory':
                # 清空智能体库存（但不包括人员）
                clear_amt=agent.clear_inventory(including_person=False)
                person_success=(not agent.has_person)
                # 如果有人员在库存则失败
                event['success']=person_success

        # -----------------------------------------------------------------------------

        # 返回事件字典（包含成功/失败信息）
        return event

    def id_get(self, _id):
        """ 通过 id 获取对象 """
        return self.field.id_get(_id)

    def name_get(self, nm):
        """ 通过名称获取对象 """
        return self.field.name_get(nm)

    def get_observation(self, agent_idx, dct=None):
        """
        获取智能体的完整观察（全局 + 局部）。
        在 event 字典的原位更新，不创建新字典。
        """
        if dct is None: dct={}
        dct['global_obs']=self.partial_observation(agent_idx)
        dct['local_obs']=self.local_partial_observation(agent_idx)
        return dct

    def get_globally_visible_ids(self, agent_idx):
        """ 获取智能体全局可见的所有对象 id 列表 """
        dct=self.get_observation(agent_idx)
        gobs=dct['global_obs']
        gobs_ids=list(map(lambda d : d['id'], gobs))
        return gobs_ids

    def _wrap_object_readable(self, obj):
        """
        将对象包装为可读字典格式，过滤相关信息。
        这样做是为了避免顶层系统直接与 POI/Agent 类交互（抽象层）。

        返回字典包含所有对象共有的字段和类型特有的字段。
        """
        dct={}

        # 所有对象共有的属性
        ptpl=obj.get_position()
        dct['position']={axn : axv for axn,axv in zip(['x','y','z'][:len(ptpl)], ptpl)}
        dct['name']=obj.get_name()
        dct['id']=obj.id
        dct['type']=obj.class_name()
        dct['collidable']=obj.collidable
        dct['string_description']=f"{dct['name']}"

        tp=obj.class_name()
        fire_type_desc=lambda tp : f" of {tp} type" if Controller.TELL_FIRE_TYPE else ""

        if tp == 'Flammable':
            # 注意：能到达此函数的 Flammable 必须有名称
            assert hasattr(obj, 'name'), f"Flammable object in observation yet has no name"

            dct['parent_fire']=obj.parent_name
            dct['intensity']=read_enum(obj.intensity).capitalize()
            dct['fire_type']=Field.READABLE_TYPE_MAPPER_FIRE[obj.fire_type].capitalize()
            fire=self.name_get(dct['parent_fire'])
            average_neighboor_intensity=fire.average_neighboor_intensity(obj)
            dct['average_neighboor_intensity']=read_enum(average_neighboor_intensity).capitalize()

            # 用平均邻居强度代替单点强度，提供更具代表性的"区域"信息
            dct['string_description']+=f" with an intensity of {dct['average_neighboor_intensity']}"
            dct['string_description']+=fire_type_desc(dct['fire_type'])

        elif tp == 'Fire':
            dct['average_intensity']=read_enum(obj.average_intensity).capitalize()
            dct['fire_type']=Field.READABLE_TYPE_MAPPER_FIRE[obj.fire_type].capitalize()

            dct['string_description']+=f" with average intensity of {dct['average_intensity']}"
            dct['string_description']+=fire_type_desc(dct['fire_type'])

        elif tp == 'Person':
            dct['load']=obj.load # 搬运所需智能体数量
            dct['status']=read_enum(obj.status).capitalize()
            dct['spotted']=obj.spotted
            dct['deposited']=obj.deposited

            if dct['deposited']:
                dct['string_description']+=f" that has been safely deposited, congrats!"
            else:
                dct['string_description']+=f" requiring {dct['load']} agents to carry"

        elif tp == 'Reservoir':
            dct['resource_type']=Field.READABLE_TYPE_MAPPER_RESOURCE[obj.type].capitalize()
            dct['inventory']=obj.available

            dct['string_description']+=f" containing {dct['resource_type']}"

        elif tp == 'Deposit':
            inventory=dict([  (Field.READABLE_TYPE_MAPPER_RESOURCE[k].capitalize(),v) for k,v in obj.storage.items()  ])
            dct['inventory']=inventory

            dct['string_description']+=f" containing {dct['inventory']}"

        elif tp == 'AbsAgent':
            inventory=dict([  (Field.READABLE_TYPE_MAPPER_RESOURCE[k].capitalize(),v) for k,v in obj.inventory.items()  ])
            dct['inventory']=inventory

            dct['string_description']+=f" containing {dct['inventory']}"

        else:
            dct['string_description']=f""
            # 火灾区域在 Flammable 中已处理

        return dct

    def get_id(self, name):
        """ 通过名称获取 id """
        return self.field.get_id(name)

    def get(self, poi : str, idx : int = None):
        """ 获取指定类型的 POI 对象 """
        return self.field.get(poi=poi, idx=idx)

    def get_inventory(self, agent_idx : int, tp : str = None):
        """
        获取指定智能体的库存字典（可读格式）。
        如果指定了 tp，只返回该类型的库存量。
        """
        _inventory=self.get('agents', agent_idx).inventory
        inventory=dict([  (Field.READABLE_TYPE_MAPPER_RESOURCE[k].capitalize(),v) for k,v in _inventory.items()  ])
        if tp is None:
            return inventory
        return inventory.get(tp.capitalize(), None)

    def partial_observation(self, agent_idx : int):
        """
        获取智能体的全局部分观察。
        将原始对象包装为字典格式。
        默认不展开（不细分 Fire 区域），除非 FIRE_SUBDIVISION 为 True。
        """
        pobs=self.field.partial_observation(agent_idx=agent_idx, expand=Controller.FIRE_SUBDIVISION)
        dcts=[self._wrap_object_readable(o) for o in pobs]

        # 注意：过滤掉没有活跃火灾的区域（减少观察量）
        if Controller.FILTER_REGIONS:
            dcts=list(filter(lambda d : d.get('average_neighboor_intensity', '')!='None', dcts))

        return dcts

    def local_partial_observation(self, agent_idx : int):
        """
        获取智能体的局部部分观察（方向字典）。
        将原始对象包装为字典格式。
        字典键为方向名称（left, up, down, right 等）。
        """
        l_pobs=self.field.local_partial_observation(agent_idx=agent_idx, diagonal=True)
        # 注意：不过滤碰撞物（因为要包含 Flammable）
        d={}

        for k,v in l_pobs.items():
            direction=Controller.FROM_DELTA_MAP[k]
            if direction not in Controller.MOVABLE_CARDINAL_DIRECTIONS: continue # 跳过不可移动方向

            vv=list(map(self._wrap_object_readable, v))

            d[direction]=vv
        return d


# ===================================================================
# Backend（后端）类
# ===================================================================
""" Backend! 处理控制器与底层引擎之间的所有交互 """

class Backend:
    """
    提供引擎（如 ROS、GridWorld 等）与 Controller 之间的接口。
    负责导航和移动操作的分发。
    """

    def __init__(self, objects, engine='grid'):
        """
        初始化后端。
        - objects: 所有具有位置的对象列表
        - engine: 引擎类型（当前仅支持 'grid'）
        """
        # 所有对象必须有 .position 属性
        assert all([hasattr(o, 'position') for o in objects]), f"At least one object given to backend does not have .position, all objects must have"
        # 所有对象必须有 .collidable 属性
        assert all([hasattr(o, 'collidable') for o in objects]), f"At least one object given to backend does not have .collidable, all objects must have"

        self.objects=objects
        if engine=='grid':
            self.engine=GridEngine()
        else:
            raise NotImplementedError

        self.initialize(self.objects)

    def navigate(self, obj, target_position : Coordinate, eps=None):
        """
        导航到目标位置，距离目标 < eps。
        - 对于不可变对象（如 Fire, Deposit, Reservoir），不允许移动
        - 对于可变对象，允许移动

        默认 eps=0（离散网格），连续空间为 1e-6 * max(width, height)。
        """
        if eps is None:
            w,h=Coordinate.get_params()
            eps=0

        info={
                'is_mutable' : None,
            }

        # 检查位置是否可变
        info['is_mutable']=obj.mutable_position
        if not obj.mutable_position:
            return False, info

        # 使用引擎进行路径导航
        end_position=self.engine.navigation(from_position=obj.position, target_position=target_position, eps=eps)
        success=self.engine.move_object(obj, end_position)

        return success,info

    def move(self, obj, direction : str):
        """
        在网格中按指定方向移动对象。
        方向：up, down, left, right
        不能走到碰撞对象或越界。

        对于不可变对象，不允许移动。
        """
        if not obj.mutable_position:
            return False

        delta=Controller.TO_DELTA_MAP[direction.capitalize()]
        target_position=copy.deepcopy(obj.position)
        target_position.change_position(*delta)

        end_position=self.engine.navigation(from_position=obj.position, target_position=target_position, eps=0)
        success=self.engine.move_object(obj, end_position)
        return success

    def update(self):
        """ 更新后端状态（待实现） """
        raise NotImplementedError

    def initialize(self, objects):
        """ 使用环境中的对象初始化引擎 """
        self.engine.initialize(objects=objects)


# ===================================================================
# GridEngine（网格引擎）类
# ===================================================================
class GridEngine:
    """
    网格世界引擎。
    处理碰撞检测和简单导航（无 A* 搜索）。
    在网格中直接移动对象（传送方式）。
    """

    def __init__(self):
        pass

    def initialize(self, objects):
        """
        初始化引擎。
        - objects: 所有对象列表，必须有 .position 和 .collidable
        """
        assert all([hasattr(o, 'position') for o in objects]), f"At least one object given to backend does not have .position, all objects must have"
        assert all([hasattr(o, 'collidable') for o in objects]), f"At least one object given to backend does not have .collidable, all objects must have"

        self.objects=objects
        self.id_mapping=dict([(o.id,o) for o in self.objects])

    @functools.cache
    def id_get(self, _id):
        """ 通过 id 获取对象 """
        o=self.id_mapping.get(_id, None)
        return o

    def navigation(self, from_position, target_position, eps):
        """
        原始导航：从一个位置到另一个位置，到目标距离 ≤ eps。
        对象必须具有 .collidable 属性。

        通常应有 A* 搜索，但此函数仅处理可行性和碰撞检查，
        输出可行位置。

        如果找不到可行位置，返回 None；否则返回位置元组。
        对于网格世界，我们假设即使局部有碰撞物，也可以跳过（传送）。
        """
        # 如果起点或终点越界，无法移动
        if  ((not Coordinate.within_bounds(*from_position.get()))
                or (not Coordinate.within_bounds(*target_position.get())) ):
            return None

        # 如果起终点相同，直接成功
        if from_position.get() == target_position.get():
            return target_position.get()

        # 获取目标位置附近 eps 半径内的位置 → 对象 id 字典
        dct=GPS.near(central_position=target_position, radius=eps)
        for pos_xy, idl in dct.items():
            # 过滤掉引擎中不存在的对象
            pos_oids=list(filter(lambda oid : oid in self.id_mapping.keys(), idl))
            # 找出所有碰撞物
            collidable_objs=[]
            for oid in pos_oids:
                o=self.id_get(oid)
                assert o is not None, f"Filtered pos_oids by objs in GridEngine yet get None when using dict"
                if o.collidable: collidable_objs.append(o)

            # 如果该位置没有碰撞物，则可以移动到此
            if len(collidable_objs)==0:
                # 返回坐标元组
                return pos_xy

        # 如果在半径内没有空位置，则失败
        return None

    def move_object(self, obj, end_position):
        """
        移动对象到目标位置。
        对于网格世界，直接传送（tele-transport）。
        """
        # 注意：end_position 不是 Coordinate 对象
        if end_position is not None:
            obj.set_position(*end_position)
            return True
        return False


# ------------
"""

# 引擎设计说明
# 引擎必须有 agent_id → agent 映射（以便更新 agent_id 的动作）
# 由 LLM-facing 的环境类完成（移动动作）
# 必须提供智能体的部分观察（非文本、非图像）信息（后面附加地图信息）
# engine.step(actions) 更新内部物理状态

# 引擎类（类似物理碰撞、地理引擎）
# 支持网格世界？只需要处理墙和障碍物碰撞（二者等价）
# 可能不需要太大开销，但需要有漂亮的渲染函数
#    -可能使用一些核心功能

# 引擎查询:
#    地理信息:
#        -existsPath(a,b) (导航)
#        -localObs(a, r) — 获取点 a 附近的部分观察（坐标 → 对象/None 列表）
#        -existsObstacle(a) — 位置 a 是否存在障碍物

# 引擎动作:
#    返回 success
#    更新内部表示
#    对象处理:
#        -moveObject(o, p) — 将对象 o 移动到位置 p（传送）
#            -用于获取智能体的局部观察
#            -两个对象可以占据同一位置
#            -与 getPosition(o) 配合实现 moveAhead/back/left/right
"""
