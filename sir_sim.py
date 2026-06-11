# -*- coding: utf-8 -*-
"""
离散传染病模型模拟器（SI / SIS / SIR）
Python 核心 + tkinter 可视化

特点：
- 每个人有独立的起床 / 出门 / 回家 / 上床时间（围绕均值扰动）
- 每个人有独立的移动速度
- 白天目标导向的外出移动，夜间回到床位休息
- 离散接触判定 + 零点状态更新
- Disease / SceneElement / Individual 分层设计，便于扩展到 SEIR / 医院 / 隔离区 等
"""

import random
import math
import time
import tkinter as tk
from tkinter import ttk

random.seed()

# =========================================================
#  Disease  （疾病参数 + 预设）
# =========================================================
class Disease:
    def __init__(self, name="自定义", beta=0.15, incubation=0,
                 recovery=5, vertical=0.0, mortality=0.0,
                 latent_infectious=False):
        self.name = name
        self.beta = beta                      # 每次接触传染概率
        self.incubation = incubation          # 平均潜伏期（天），0 = 当天转 I
        self.recovery = recovery              # 平均康复期（天），SI 忽略
        self.vertical_transmission = vertical # 子代传播（预留）
        self.mortality = mortality            # 死亡率
        self.latent_infectious = latent_infectious  # E 是否具传染性

    @staticmethod
    def presets(key):
        table = {
            "流感":    Disease("流感",    0.25, 1, 4, 0, 0.002, False),
            "新冠类":  Disease("新冠类",  0.18, 2, 7, 0, 0.01,  False),
            "普通感冒":Disease("普通感冒",0.10, 0, 3, 0, 0.0,   False),
            "超级传播":Disease("超级传播",0.40, 1, 6, 0, 0.005, True),
            "自定义":  Disease("自定义",  0.15, 0, 5, 0, 0.0,   False),
        }
        return table.get(key, Disease())


# =========================================================
#  场景元素（寝室 / 广场 / 预留医院、隔离区）
# =========================================================
class SceneElement:
    def __init__(self, x, y, w, h, name):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.name = name
    def contains(self, px, py, pad=0):
        return (self.x - pad <= px <= self.x + self.w + pad and
                self.y - pad <= py <= self.y + self.h + pad)


class Dormitory(SceneElement):
    def __init__(self, x, y, w, h, idx):
        super().__init__(x, y, w, h, f"寝室#{idx}")
        self.idx = idx
        self.occupants = []  # Individual 列表
    def random_inside(self, margin=10):
        return (random.uniform(self.x + margin, self.x + self.w - margin),
                random.uniform(self.y + margin, self.y + self.h - margin))


class Plaza(SceneElement):
    def __init__(self, x, y, w, h):
        super().__init__(x, y, w, h, "广场")
    def random_inside(self, margin=10):
        return (random.uniform(self.x + margin, self.x + self.w - margin),
                random.uniform(self.y + margin, self.y + self.h - margin))


# =========================================================
#  Individual —— 每个个体的完整状态
# =========================================================
# 行为阶段（供内部使用）
ACT_ASLEEP = 0    # 在床上（完全不动）
ACT_WAKING = 1    # 起床、在寝室内活动
ACT_OUTGOING = 2  # 出门路上
ACT_OUTSIDE = 3   # 在外面随机游走
ACT_RETURNING = 4 # 回家路上
ACT_EVENING = 5   # 已归寝、在寝室活动
ACT_BED = 6       # 上床


class Individual:
    _id = 0

    def __init__(self, dorm, world_w, world_h, disease):
        Individual._id += 1
        self.id = Individual._id
        self.dorm = dorm
        self.world_w = world_w
        self.world_h = world_h
        # "室外区域" 的左边界（比寝室右边界再右 20px，用于确保目标在室外）
        self.outdoor_x0 = dorm.x + dorm.w + 20

        # 床位：在寝室内随机固定一点
        self.bed_x, self.bed_y = dorm.random_inside(margin=8)
        # 初始位置 = 床位
        self.x, self.y = self.bed_x, self.bed_y

        # 当前移动目标（外出目的地 / 回家目标）
        self.target = None

        # 健康状态
        self.state = "S"          # S / E / I / R / Dead
        self.state_days = 0
        self.incubation_left = 0
        self.infection_left = 0
        self.is_dead = False

        # 个体属性（差异化）
        self.age = random.randint(18, 65)
        self.sex = random.choice(("M", "F"))
        self.roles = ["普通人"]

        # —— 作息时刻表（围绕均值扰动）——
        # phase 0.00 ~ 1.00；整体后移，允许晚归
        self.wake_up  = 0.08 + random.gauss(0, 0.015)
        self.leave    = 0.20 + random.gauss(0, 0.025)
        self.return_t = 0.75 + random.gauss(0, 0.050)   # 回家更晚
        self.to_bed   = 0.97 + random.gauss(0, 0.015)   # 上床非常晚
        # 约束顺序
        self.wake_up  = min(max(self.wake_up,  0.01), 0.20)
        self.leave    = min(max(self.leave,    self.wake_up + 0.03), 0.35)
        self.return_t = min(max(self.return_t, self.leave + 0.20), 0.85)
        self.to_bed   = min(max(self.to_bed,   self.return_t + 0.05), 0.99)

        # 每个阶段的持续时长（phase 单位）
        self.t_outgoing  = 0.12 + random.gauss(0, 0.015)
        self.t_returning = 0.12 + random.gauss(0, 0.015)
        self.t_outgoing  = max(0.05, self.t_outgoing)
        self.t_returning = max(0.05, self.t_returning)

        # 移动速度（像素/秒） —— 提速
        self.speed = random.uniform(140, 280)

        # 外出时的游走参数
        self.wander_ttl = random.uniform(1.0, 2.5)

    # ---------- 行为阶段判断 ----------
    def current_activity(self, phase):
        if self.is_dead:
            return ACT_ASLEEP
        if phase < self.wake_up:
            return ACT_ASLEEP
        if phase < self.leave:
            return ACT_WAKING
        if phase < self.leave + self.t_outgoing:
            return ACT_OUTGOING
        if phase < self.return_t:
            return ACT_OUTSIDE
        if phase < self.return_t + self.t_returning:
            return ACT_RETURNING
        if phase < self.to_bed:
            return ACT_EVENING
        return ACT_BED

    # ---------- 是否具传染性 ----------
    def is_infectious(self, disease):
        if self.is_dead or self.state == "R" or self.state == "S":
            return False
        if self.state == "I":
            return True
        if self.state == "E" and disease.latent_infectious:
            return True
        return False

    # ---------- 移动一帧 ----------
    # day_len: 每天多少"真实秒"；用于按剩余时间计算归寝时的加速速度
    def step_move(self, dt, phase, plaza, day_len=20.0):
        act = self.current_activity(phase)

        # —— 当阶段变化时，清除旧目标 ——
        if act != getattr(self, "_prev_act", -1):
            self.target = None
            self._prev_act = act

        if act == ACT_ASLEEP:
            # 已经在床上，小幅抖动即可（不再瞬移）
            target = (self.bed_x, self.bed_y)
            dist = math.hypot(self.x - target[0], self.y - target[1])
            if dist > 2.0:
                self._move_toward_pt(target, dt, self.speed * 0.3)
            self._clamp_to_dorm()
            return

        if act == ACT_WAKING:
            # 起床但还没出门：在寝室内小幅活动，然后等出门时间
            if self.target is None or self._dist_to_target() < 4:
                self.target = self.dorm.random_inside(margin=12)
            self._move_toward(dt, self.speed * 0.7)
            self._clamp_to_dorm()
            return

        if act == ACT_OUTGOING:
            # 出门路上：朝广场/室外走（没到达就持续走）
            if self.target is None or self._dist_to_target() < 10:
                self.target = plaza.random_inside(margin=50)
            self._move_toward(dt, self.speed)
            self._clamp_world()
            return

        if act == ACT_OUTSIDE:
            # 在外面（包含广场 & 走廊区域）游荡
            if not self._is_outside(plaza):
                # 还没进入户外 —— 朝门口走
                if self.target is None or self._dist_to_target() < 10:
                    self.target = plaza.random_inside(margin=50)
                self._move_toward(dt, self.speed)
            else:
                # 已经在外面 → 随机游荡
                self.wander_ttl -= dt
                if self.wander_ttl <= 0 or self.target is None:
                    if random.random() < 0.7:
                        self.target = plaza.random_inside(margin=50)
                    else:
                        tx = random.uniform(self.outdoor_x0, self.world_w - 10)
                        ty = random.uniform(30, self.world_h - 30)
                        self.target = (tx, ty)
                    self.wander_ttl = random.uniform(1.5, 3.5)
                self._move_toward(dt, self.speed)
            self._clamp_world()
            return

        if act == ACT_RETURNING:
            # 回家：朝床位持续移动（不瞬移）
            target = (self.bed_x, self.bed_y)
            self._move_toward_pt(target, dt, self.speed)
            self._clamp_world()
            return

        if act == ACT_EVENING:
            # 晚归：朝床位物理移动，根据"距离 / 剩余时间"动态加速，
            # 保证在上床时刻前到达床位，不瞬移。
            target = (self.bed_x, self.bed_y)
            dist = math.hypot(self.x - target[0], self.y - target[1])
            remaining_phase = max(0.002, self.to_bed - phase)
            remaining_sec = remaining_phase * day_len
            # 所需速度 = 距离 / 剩余时间；1.3 倍安全系数
            needed_speed = (dist / remaining_sec) * 1.3
            speed = max(self.speed, needed_speed)
            # 限速避免极端瞬移感
            speed = min(speed, self.speed * 12.0)
            self._move_toward_pt(target, dt, speed)
            self._clamp_to_dorm()
            return

        if act == ACT_BED:
            # 上床：仅做小幅靠拢（仍不瞬移），之后基本静止
            target = (self.bed_x, self.bed_y)
            dist = math.hypot(self.x - target[0], self.y - target[1])
            if dist > 1.5:
                self._move_toward_pt(target, dt, self.speed * 0.4)
            self._clamp_to_dorm()
            return

    # ---------- 朝指定坐标移动（不使用 self.target）----------
    def _move_toward_pt(self, target, dt, v):
        dx = target[0] - self.x
        dy = target[1] - self.y
        d = math.hypot(dx, dy)
        if d < 1e-3:
            return
        step = v * dt
        if step >= d:
            self.x, self.y = target[0], target[1]
        else:
            self.x += dx / d * step
            self.y += dy / d * step

    # ---------- 是否已经在室外区域（plaza 或走廊） ----------
    def _is_outside(self, plaza):
        # x 坐标 > dorm.x + dorm.w + 10 或 在 plaza 内部
        if plaza.contains(self.x, self.y, 0):
            return True
        return self.x > self.outdoor_x0

    def _dist_to_target(self):
        if self.target is None:
            return 1e9
        return math.hypot(self.x - self.target[0], self.y - self.target[1])

    def _move_toward(self, dt, v):
        if self.target is None:
            return
        dx = self.target[0] - self.x
        dy = self.target[1] - self.y
        d = math.hypot(dx, dy)
        if d < 1e-3:
            return
        step = v * dt
        if step >= d:
            self.x, self.y = self.target[0], self.target[1]
            self.target = None  # 到了，下次重新选
        else:
            self.x += dx / d * step
            self.y += dy / d * step

    def _clamp_to_dorm(self):
        d = self.dorm
        self.x = min(max(self.x, d.x + 4), d.x + d.w - 4)
        self.y = min(max(self.y, d.y + 4), d.y + d.h - 4)

    def _clamp_world(self):
        self.x = min(max(self.x, 6), self.world_w - 6)
        self.y = min(max(self.y, 6), self.world_h - 6)


# =========================================================
#  Simulation —— 主模拟
# =========================================================
class Simulation:
    def __init__(self, root):
        self.root = root
        root.title("离散传染病模型模拟器")

        # ---- 可调参数（默认值）----
        self.model_var = tk.StringVar(value="SIR")
        self.pop_size_var = tk.IntVar(value=150)
        self.init_i_var = tk.IntVar(value=5)
        self.beta_var = tk.DoubleVar(value=0.20)
        self.incubation_var = tk.IntVar(value=0)
        self.recovery_var = tk.IntVar(value=5)
        self.mortality_var = tk.DoubleVar(value=0.0)
        self.latent_inf_var = tk.BooleanVar(value=False)
        self.infect_r_var = tk.DoubleVar(value=14)
        self.dorm_cols_var = tk.IntVar(value=3)
        self.dorm_rows_var = tk.IntVar(value=2)
        self.day_len_var = tk.DoubleVar(value=20.0)  # 每天 20 秒，给步行留足够时间
        self.speed_var = tk.DoubleVar(value=2.0)     # 2x 默认倍速
        self.running_var = tk.BooleanVar(value=False)

        self.world_w = 1100
        self.world_h = 620
        self.chart_h = 170

        self._build_ui()
        self._reset_simulation()
        self._tick()

    # ---------------- UI ----------------
    def _build_ui(self):
        top = tk.Frame(self.root)
        top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        # 控制面板（左侧）
        ctrl = tk.LabelFrame(top, text="参数设置")
        ctrl.pack(side=tk.LEFT, fill=tk.Y, padx=4)

        r = 0
        def row(widget, r, c=0, cs=1):
            widget.grid(row=r, column=c, columnspan=cs, sticky="w", padx=4, pady=2)
        def lbl(text, r, c=0):
            row(tk.Label(ctrl, text=text), r, c)

        lbl("模型", r); row(ttk.Combobox(ctrl, textvariable=self.model_var,
                                         values=["SI","SIS","SIR"], width=8, state="readonly"),
                              r, 1); r+=1
        lbl("总人数", r); row(tk.Spinbox(ctrl, from_=10, to=600, textvariable=self.pop_size_var, width=7), r, 1); r+=1
        lbl("初始感染", r); row(tk.Spinbox(ctrl, from_=0, to=100, textvariable=self.init_i_var, width=7), r, 1); r+=1
        lbl("β (传染率)", r); row(tk.Spinbox(ctrl, from_=0.0, to=1.0, increment=0.01, textvariable=self.beta_var, width=7), r, 1); r+=1
        lbl("潜伏期(天)", r); row(tk.Spinbox(ctrl, from_=0, to=30, textvariable=self.incubation_var, width=7), r, 1); r+=1
        lbl("康复期(天)", r); row(tk.Spinbox(ctrl, from_=1, to=60, textvariable=self.recovery_var, width=7), r, 1); r+=1
        lbl("死亡率", r); row(tk.Spinbox(ctrl, from_=0.0, to=0.5, increment=0.005, textvariable=self.mortality_var, width=7), r, 1); r+=1
        lbl("E具传染性", r); row(tk.Checkbutton(ctrl, variable=self.latent_inf_var), r, 1); r+=1
        lbl("接触半径(px)", r); row(tk.Spinbox(ctrl, from_=4, to=50, textvariable=self.infect_r_var, width=7), r, 1); r+=1
        lbl("寝室列/行", r)
        sub = tk.Frame(ctrl); sub.grid(row=r, column=1, sticky="w"); r+=1
        tk.Spinbox(sub, from_=1, to=6, textvariable=self.dorm_cols_var, width=4).pack(side=tk.LEFT)
        tk.Label(sub, text="×").pack(side=tk.LEFT)
        tk.Spinbox(sub, from_=1, to=5, textvariable=self.dorm_rows_var, width=4).pack(side=tk.LEFT)

        lbl("每天(秒)", r); row(tk.Spinbox(ctrl, from_=2.0, to=120.0, increment=1.0, textvariable=self.day_len_var, width=7), r, 1); r+=1
        lbl("倍速", r); row(tk.Spinbox(ctrl, from_=0.25, to=10.0, increment=0.25, textvariable=self.speed_var, width=7), r, 1); r+=1

        btn_row = tk.Frame(ctrl); btn_row.grid(row=r, column=0, columnspan=2, pady=6); r+=1
        tk.Button(btn_row, text="▶ 开始 / ⏸ 暂停", width=14,
                  command=self._toggle_run).pack(side=tk.LEFT, padx=3)
        tk.Button(btn_row, text="⟳ 重置", width=8,
                  command=self._reset_simulation).pack(side=tk.LEFT, padx=3)

        # 疾病预设
        pf = tk.LabelFrame(ctrl, text="疾病预设")
        pf.grid(row=r, column=0, columnspan=2, pady=4, sticky="we")
        self.preset_var = tk.StringVar(value="自定义")
        preset_box = ttk.Combobox(pf, textvariable=self.preset_var,
                                  values=["自定义","流感","新冠类","普通感冒","超级传播"],
                                  width=12, state="readonly")
        preset_box.pack(padx=4, pady=3)
        preset_box.bind("<<ComboboxSelected>>", self._apply_preset)

        # 状态行
        self.status_var = tk.StringVar(value="准备中")
        tk.Label(top, textvariable=self.status_var,
                 anchor="w", justify="left",
                 font=("Consolas", 10), fg="#ddd",
                 bg="#333").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        # 画布
        cv_frame = tk.Frame(self.root)
        cv_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=4)

        self.world_canvas = tk.Canvas(cv_frame, width=self.world_w, height=self.world_h,
                                      bg="#12131a", highlightthickness=1, highlightbackground="#555")
        self.world_canvas.pack(side=tk.TOP, fill=tk.X)

        self.chart_canvas = tk.Canvas(cv_frame, width=self.world_w, height=self.chart_h,
                                      bg="#12131a", highlightthickness=1, highlightbackground="#555")
        self.chart_canvas.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))

    def _apply_preset(self, *_):
        d = Disease.presets(self.preset_var.get())
        self.beta_var.set(round(d.beta, 3))
        self.incubation_var.set(d.incubation)
        self.recovery_var.set(d.recovery)
        self.mortality_var.set(d.mortality)
        self.latent_inf_var.set(d.latent_infectious)

    # ---------------- 重置 ----------------
    def _reset_simulation(self):
        self.day = 0
        self.phase = 0.0  # 0..1 一天内阶段
        self.history = {"day": [], "S": [], "E": [], "I": [], "R": [], "Dead": []}

        # 疾病
        self.disease = Disease(
            beta=self.beta_var.get(),
            incubation=self.incubation_var.get(),
            recovery=self.recovery_var.get(),
            mortality=self.mortality_var.get(),
            latent_infectious=self.latent_inf_var.get(),
        )

        # 场景布局：左区放寝室（网格），右区放广场
        left_w = int(self.world_w * 0.55)
        plaza_x = left_w + 25
        plaza_w = self.world_w - plaza_x - 10
        self.plaza = Plaza(plaza_x, 30, plaza_w, self.world_h - 40)

        self.dormitories = []
        cols = max(1, self.dorm_cols_var.get())
        rows = max(1, self.dorm_rows_var.get())
        pad_x, pad_y = 30, 40
        gap_x, gap_y = 14, 14
        dw = (left_w - pad_x * 2 - gap_x * (cols - 1)) / cols
        dh = (self.world_h - pad_y * 2 - gap_y * (rows - 1)) / rows
        idx = 1
        for r in range(rows):
            for c in range(cols):
                x = pad_x + c * (dw + gap_x)
                y = pad_y + r * (dh + gap_y)
                self.dormitories.append(Dormitory(x, y, dw, dh, idx))
                idx += 1

        # 人群
        n = max(1, int(self.pop_size_var.get()))
        self.people = []
        for i in range(n):
            d = self.dormitories[i % len(self.dormitories)]
            p = Individual(d, self.world_w, self.world_h, self.disease)
            d.occupants.append(p)
            self.people.append(p)

        # 初始感染者
        i0 = min(len(self.people), max(0, int(self.init_i_var.get())))
        for i in range(i0):
            p = self.people[i]
            p.state = "I"
            if self.model_var.get() != "SI":
                p.infection_left = max(1, self._geo(1.0 / max(1, self.disease.recovery)))
            else:
                p.infection_left = 0  # SI 不使用

        # 接触判定节奏：每天 N 次
        self.contacts_per_day = 10
        self.next_contact_phase = 1.0 / self.contacts_per_day

        self._push_history()
        self._draw_world()
        self._draw_chart()
        self._update_status()

    def _geo(self, p):
        # 几何分布（从 1 开始），均值 1/p
        k = 0
        while random.random() >= p:
            k += 1
        return k + 1

    # ---------------- 主循环 ----------------
    def _toggle_run(self):
        self.running_var.set(not self.running_var.get())

    def _tick(self):
        if self.running_var.get():
            # 时间推进：day_len 秒 = 一"天"；乘倍速
            real_dt = 0.016  # 约 60fps
            dt_phases = (real_dt / max(0.1, self.day_len_var.get())) * self.speed_var.get()
            self._advance(dt_phases)
        # 刷新画布
        self._draw_world()
        self._draw_chart()
        self._update_status()
        self.root.after(16, self._tick)

    def _advance(self, dphase):
        self.phase += dphase

        # 每人移动（以秒为单位的 dt：phase*day_len/speed）
        dt_sec = dphase * self.day_len_var.get() / max(0.01, self.speed_var.get())
        day_len = max(0.1, self.day_len_var.get())
        for p in self.people:
            p.step_move(dt_sec, self.phase, self.plaza, day_len)

        # 离散接触判定（白天/傍晚有效阶段；时刻表后移，窗口也后移）
        cur_act_phase = (self.phase % 1.0)
        activity_phase = 0.12 < cur_act_phase < 0.92
        while self.next_contact_phase < self.phase and activity_phase:
            self._do_contacts()
            self.next_contact_phase += 1.0 / self.contacts_per_day

        # 跨零点 → 每日状态更新
        if self.phase >= 1.0:
            self.phase -= 1.0
            self.next_contact_phase = 1.0 / self.contacts_per_day
            self._daily_update()

    # ---------- 接触感染 ----------
    def _do_contacts(self):
        r2 = self.infect_r_var.get() ** 2
        n = len(self.people)
        # 只有活动中的人才会接触
        active = []
        for p in self.people:
            if p.is_dead:
                continue
            act = p.current_activity(self.phase)
            if act == ACT_ASLEEP or act == ACT_BED:
                continue
            active.append(p)
        # O(m^2) 对所有活跃个体做距离判定
        m = len(active)
        beta = self.disease.beta
        for i in range(m):
            a = active[i]
            if not a.is_infectious(self.disease):
                continue
            for j in range(i + 1, m):
                b = active[j]
                if b.state != "S":
                    continue
                dx = a.x - b.x
                dy = a.y - b.y
                if dx * dx + dy * dy <= r2:
                    if random.random() < beta:
                        b.state = "E"
                        b.state_days = 0
                        if self.disease.incubation <= 0:
                            b.incubation_left = 0
                        else:
                            b.incubation_left = max(
                                1, self._geo(1.0 / max(1, self.disease.incubation)))

    # ---------- 每日零点更新 ----------
    def _daily_update(self):
        self.day += 1
        model = self.model_var.get()
        for p in self.people:
            if p.is_dead:
                continue
            p.state_days += 1

            if p.state == "E":
                p.incubation_left -= 1
                if p.incubation_left <= 0:
                    p.state = "I"
                    p.state_days = 0
                    if model != "SI":
                        p.infection_left = max(
                            1, self._geo(1.0 / max(1, self.disease.recovery)))
                    else:
                        p.infection_left = 0  # SI: 感染永不康复，不计时

            elif p.state == "I":
                if model == "SI":
                    # SI: 永远感染，不做任何转换
                    continue
                p.infection_left -= 1
                if p.infection_left <= 0:
                    # 死亡或康复
                    if random.random() < self.disease.mortality:
                        p.state = "Dead"
                        p.is_dead = True
                    elif model == "SIS":
                        p.state = "S"
                        p.state_days = 0
                    elif model == "SIR":
                        p.state = "R"
                        p.state_days = 0

            # S / R: 不做改变（R 保持免疫；S 等待被接触）

        self._push_history()

    def _push_history(self):
        s = e = ii = r = d = 0
        for p in self.people:
            if p.is_dead:
                d += 1
            elif p.state == "S":
                s += 1
            elif p.state == "E":
                e += 1
            elif p.state == "I":
                ii += 1
            elif p.state == "R":
                r += 1
        self.history["day"].append(self.day)
        self.history["S"].append(s)
        self.history["E"].append(e)
        self.history["I"].append(ii)
        self.history["R"].append(r)
        self.history["Dead"].append(d)
        if len(self.history["day"]) > 800:
            for k in self.history:
                self.history[k] = self.history[k][-800:]

    # ---------------- 绘制 ----------------
    def _state_color(self, p):
        if p.is_dead:
            return "#666666"
        return {"S": "#4a90e2", "E": "#e2c04a",
                "I": "#e04040", "R": "#5cb85c"}.get(p.state, "#aaa")

    def _draw_world(self):
        c = self.world_canvas
        c.delete("all")

        # 社会大框
        c.create_rectangle(3, 3, self.world_w - 3, self.world_h - 3,
                           outline="#ffffff", width=2)

        # 广场
        pl = self.plaza
        c.create_rectangle(pl.x, pl.y, pl.x + pl.w, pl.y + pl.h,
                           outline="#7a9c80", dash=(4, 4), width=1)
        c.create_text(pl.x + 8, pl.y + 10, anchor="w",
                      text=f"{pl.name}", fill="#a7c8b0", font=("", 10))

        # 寝室
        for d in self.dormitories:
            c.create_rectangle(d.x, d.y, d.x + d.w, d.y + d.h,
                               outline="#c89060", width=1,
                               fill="#2a1f12", stipple="gray25")
            c.create_text(d.x + 6, d.y + 8, anchor="w",
                          text=f"#{d.idx}({len(d.occupants)})",
                          fill="#e0b080", font=("", 9))

        # 夜色遮罩（睡觉时加深）
        if self.phase < 0.10 or self.phase > 0.93:
            c.create_rectangle(0, 0, self.world_w, self.world_h,
                               fill="#1a1a3a", stipple="gray25")
        elif self.phase > 0.82:
            c.create_rectangle(0, 0, self.world_w, self.world_h,
                               fill="#1a1a3a", stipple="gray50")

        # 个体
        rad = 3
        inf_r = self.infect_r_var.get()
        for p in self.people:
            col = self._state_color(p)
            if p.state == "I" and not p.is_dead:
                # 感染者画一个淡圈
                c.create_oval(p.x - inf_r, p.y - inf_r,
                              p.x + inf_r, p.y + inf_r,
                              outline="#ff7070", width=0)
                # 用一个更淡的方式提示
                c.create_oval(p.x - inf_r, p.y - inf_r,
                              p.x + inf_r, p.y + inf_r,
                              outline="#ff7070", dash=(1, 3))
            c.create_oval(p.x - rad, p.y - rad,
                          p.x + rad, p.y + rad,
                          fill=col, outline=col)

        # 顶部 phase 时间条
        bar_y = 12
        c.create_rectangle(20, bar_y, self.world_w - 20, bar_y + 8,
                            fill="#222", outline="#555")
        w = self.world_w - 40
        c.create_rectangle(20, bar_y, 20 + w * self.phase, bar_y + 8,
                            fill="#f0b040", outline="")
        # 阶段分隔
        for (label, ph) in [("睡", 0.0), ("起床", 0.10), ("出门", 0.22),
                            ("回家", 0.75), ("上床", 0.97), ("次日", 1.0)]:
            x = 20 + w * ph
            c.create_line(x, bar_y - 2, x, bar_y + 12, fill="#888")
            c.create_text(x, bar_y + 16, text=label, fill="#bbb",
                          font=("", 8))

    def _draw_chart(self):
        c = self.chart_canvas
        c.delete("all")
        w, h = self.world_w, self.chart_h
        c.create_rectangle(0, 0, w, h, outline="#444")

        n = max(1, len(self.people))
        if len(self.history["day"]) < 2:
            # 图例
            self._draw_legend(c, 10, 10)
            return

        pad_l, pad_r, pad_t, pad_b = 32, 10, 22, 22
        pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
        max_day = max(1, self.history["day"][-1])

        # 网格 + y 轴刻度（人数）
        for i in range(0, 5):
            y = pad_t + ph * i / 4
            c.create_line(pad_l, y, w - pad_r, y, fill="#262626")
            c.create_text(pad_l - 6, y, anchor="e",
                          text=str(int(n * (1 - i / 4))),
                          fill="#888", font=("", 9))
        # x 轴刻度（天）
        tick_step = max(1, math.ceil(max_day / 10))
        for d in range(0, max_day + 1, tick_step):
            x = pad_l + pw * (d / max_day)
            c.create_text(x, h - 8, text="d" + str(d), fill="#888", font=("", 9))

        def plot(arr, color):
            pts = []
            for i, v in enumerate(arr):
                x = pad_l + pw * (self.history["day"][i] / max_day)
                y = pad_t + ph * (1 - v / n)
                pts.extend([x, y])
            if len(pts) >= 4:
                c.create_line(*pts, fill=color, width=1.6)

        plot(self.history["S"], "#4a90e2")
        plot(self.history["E"], "#e2c04a")
        plot(self.history["I"], "#e04040")
        plot(self.history["R"], "#5cb85c")
        plot(self.history["Dead"], "#999999")

        self._draw_legend(c, pad_l + 8, 4)

    def _draw_legend(self, c, x, y):
        items = [("S", "#4a90e2"), ("E", "#e2c04a"), ("I", "#e04040"),
                 ("R", "#5cb85c"), ("死", "#999")]
        for name, col in items:
            c.create_rectangle(x, y, x + 9, y + 9, fill=col, outline=col)
            c.create_text(x + 14, y + 5, anchor="w", text=name,
                          fill="#ddd", font=("", 9))
            x += 32

    # ---------------- 状态行 ----------------
    def _update_status(self):
        s = e = ii = r = d = 0
        for p in self.people:
            if p.is_dead:
                d += 1
            elif p.state == "S":
                s += 1
            elif p.state == "E":
                e += 1
            elif p.state == "I":
                ii += 1
            elif p.state == "R":
                r += 1
        phase_name = self._phase_name()
        self.status_var.set(
            f"  第 {self.day:3d} 天  |  阶段: {phase_name}  |  "
            f"S={s:<4d}  E={e:<4d}  I={ii:<4d}  R={r:<4d}  死亡={d:<4d}  "
            f"|  模型: {self.model_var.get()}  |  总人数: {len(self.people)}  "
            f"|  {'▶ 运行中' if self.running_var.get() else '⏸ 暂停'}"
        )

    def _phase_name(self):
        ph = self.phase
        if ph < 0.10:
            return "深夜/睡眠"
        if ph < 0.22:
            return "早晨起床"
        if ph < 0.78:
            return "白天外出"
        if ph < 0.93:
            return "晚归途中"
        return "夜间睡眠"


# =========================================================
#  main
# =========================================================
def main():
    root = tk.Tk()
    root.configure(bg="#222")
    # 让 ttk 颜色在暗色主题下看得清
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    Simulation(root)
    root.mainloop()


if __name__ == "__main__":
    main()
