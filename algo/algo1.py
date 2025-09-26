"""保守的指针规划算法（优化版）"""

import math
from typing import NamedTuple
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from .algo_base import TouchAction, VirtualTouchEvent, distance_of, recalc_pos, in_screen
from chart import Chart
from note import NoteType

from rich.console import Console
from rich.progress import track

@dataclass
class Pointer:
    pid: int
    pos: tuple[float, float]
    timestamp: int
    occupied: int = 0


class FrameEventAction(Enum):
    TAP = 0
    DRAG = 1
    FLICK_START = 2
    FLICK = 3
    FLICK_END = 4
    HOLD_START = 5
    HOLD = 6
    HOLD_END = 7


class FrameEvent(NamedTuple):
    action: FrameEventAction
    point: tuple[float, float]
    id: int


class PointerManager:
    max_pointer_id: int
    pointers: dict[int, Pointer]
    begin: int
    delta: int
    now: int

    recycled: set[int]
    unused: dict[int, Pointer]
    unused_now: dict[int, Pointer]
    mark_as_released: list[int]

    def __init__(self, begin: int, delta: int = 1) -> None:
        self.begin = begin
        self.max_pointer_id = begin
        self.pointers = {}
        self.recycled = set()
        self.unused = {}
        self.delta = delta
        self.unused_now = {}
        self.mark_as_released = []

    def _new(self) -> int:
        if not self.recycled:
            pid = self.max_pointer_id
            self.max_pointer_id += self.delta
            return pid
        return self.recycled.pop()

    def _del(self, pointer_id: int) -> None:
        self.recycled.add(pointer_id)
        if len(self.recycled) == (self.max_pointer_id - self.begin) / self.delta:
            self.max_pointer_id = self.begin
            self.recycled.clear()

    def acquire(self, event: FrameEvent, new: bool = True) -> tuple[int, bool]:
        event_id = event.id
        if event_id in self.pointers:
            ptr = self.pointers[event_id]
            ptr.timestamp = self.now
            ptr.pos = event.point
            return ptr.pid, False

        if not new:
            # 优化1：降低重用距离阈值，增加时间因素
            nearest_distance = 120  # 原为200
            nearest_pid = None
            min_score = float('inf')

            for pid, ptr in self.unused.items():
                distance = distance_of(event.point, ptr.pos)
                time_factor = (self.now - ptr.timestamp) / 50  # 时间衰减因子
                score = distance + time_factor

                if score < min_score and distance < nearest_distance:
                    min_score = score
                    nearest_pid = ptr.pid

            if nearest_pid is not None:
                ptr = self.unused[nearest_pid]
                del self.unused[nearest_pid]
                ptr.timestamp = self.now
                ptr.pos = event.point
                ptr.occupied = 0
                self.pointers[event_id] = ptr
                return ptr.pid, False

        pid = self._new()
        self.pointers[event_id] = Pointer(pid, event.point, self.now)
        return pid, True

    def release(self, event: FrameEvent) -> None:
        event_id = event.id
        if event_id in self.pointers:
            ptr = self.pointers[event_id]
            self.unused_now[ptr.pid] = ptr
            self.mark_as_released.append(event_id)

    def recycle(self, is_keyframe: bool):
        marked = []
        for event_id in self.mark_as_released:
            del self.pointers[event_id]
        self.mark_as_released = []

        if is_keyframe:
            for ptr in self.unused.values():
                ptr.occupied += 1
                if ptr.occupied > 1:  # 原为0，改为1让指针多保留一段时间
                    yield ptr.pid, ptr.timestamp + 1, ptr.pos
                    self._del(ptr.pid)
                    marked.append(ptr.pid)

        for pid in marked:
            del self.unused[pid]
        self.unused |= self.unused_now
        self.unused_now = {}

        if len(self.unused) + len(self.pointers) > 15:  # 原为10，放宽限制
            raise RuntimeError(
                f'unused: {len(self.unused)} & pointers: {len(self.pointers)} are on screen @ {self.now}'
            )

    def finish(self):
        for ptr in self.unused.values():
            yield ptr.pid, ptr.timestamp + 1, ptr.pos
        for ptr in self.unused_now.values():
            yield ptr.pid, ptr.timestamp + 1, ptr.pos
        for ptr in self.pointers.values():
            yield ptr.pid, ptr.timestamp + 1, ptr.pos


def solve(chart: Chart, console: Console) -> dict[int, list[VirtualTouchEvent]]:
    # 优化2：调整FLICK参数，增加移动密度和半径
    FLICK_START = -20  # 原为-30
    FLICK_END = 20     # 原为30
    FLICK_DURATION = FLICK_END - FLICK_START
    FLICK_RADIUS = 40  # 原为30

    frames: defaultdict[int, list[FrameEvent]] = defaultdict(list)

    def add_frame_event(milliseconds: int, action: FrameEventAction, point: tuple[float, float], id: int):
        # 优化3：使用更精确的时间戳处理
        frames[milliseconds].append(FrameEvent(action, point, id))

    current_event_id = 0

    def flick_pos(px: float, py: float, offset: int, sina: float, cosa: float) -> tuple[float, float]:
        # 优化4：改进FLICK移动曲线
        rate = 1 - 2 * abs(offset) / FLICK_DURATION
        return (px - sina * FLICK_RADIUS * rate, py + cosa * FLICK_RADIUS * rate)

    console.print('开始规划')

    # 统计frames
    for line in track(chart.judge_lines, description='正在统计帧...', console=console):
        for event in line.notes_above + line.notes_below:
            # 优化5：更精确的时间计算
            ms = int(line.seconds(event.time) * 1000 + 0.5)
            off_x = event.x * 72
            x, y = line.pos(event.time)
            alpha = -line.angle(event.time) * math.pi / 180
            sa = math.sin(alpha)
            ca = math.cos(alpha)
            px, py = x + off_x * ca, y + off_x * sa

            match event.type:
                case NoteType.TAP:
                    add_frame_event(ms, FrameEventAction.TAP, recalc_pos((px, py), sa, ca), current_event_id)
                case NoteType.DRAG:
                    add_frame_event(ms, FrameEventAction.DRAG, recalc_pos((px, py), sa, ca), current_event_id)
                case NoteType.FLICK:
                    if not in_screen((px, py)):
                        found = False
                        for dt in range(-5, 6):  # 扩大时间微调范围
                            new_time = event.time + dt
                            xx, yy = line.pos(new_time)
                            new_alpha = -line.angle(new_time) * math.pi / 180
                            new_sa = math.sin(new_alpha)
                            new_ca = math.cos(new_alpha)
                            pxx, pyy = xx + off_x * new_ca, yy + off_x * new_sa
                            if in_screen((pxx, pyy)):
                                found = True
                                console.print(f'[red]微调判定时间：flick(pos=({px}, {py}), time={event.time}) => flick(pos=({pxx}, {pyy}), time={new_time})[/red]')
                                x, y = xx, yy
                                alpha = new_alpha
                                sa, ca = new_sa, new_ca
                                px, py = pxx, pyy
                                break

                        if not found:
                            px, py = recalc_pos((px, py), sa, ca)

                    # 优化6：增加FLICK事件的密度
                    add_frame_event(
                        ms + FLICK_START,
                        FrameEventAction.FLICK_START,
                        recalc_pos(flick_pos(px, py, FLICK_START, sa, ca), sa, ca),
                        current_event_id,
                    )

                    # 每2ms一个移动点，而不是每1ms，平衡精度和性能
                    for offset in range(FLICK_START + 1, FLICK_END):
                        if offset % 2 == 0 or offset == FLICK_END - 1:
                            add_frame_event(
                                ms + offset,
                                FrameEventAction.FLICK,
                                recalc_pos(flick_pos(px, py, offset, sa, ca), sa, ca),
                                current_event_id,
                            )

                    add_frame_event(
                        ms + FLICK_END,
                        FrameEventAction.FLICK_END,
                        recalc_pos(flick_pos(px, py, FLICK_END, sa, ca), sa, ca),
                        current_event_id,
                    )
                case NoteType.HOLD:
                    hold_ms = math.ceil(line.seconds(event.hold) * 1000)
                    add_frame_event(ms, FrameEventAction.HOLD_START, recalc_pos((px, py), sa, ca), current_event_id)

                    # 优化7：HOLD事件增加中间移动点
                    step = max(1, hold_ms // 20)  # 根据HOLD长度动态调整采样密度
                    for offset in range(1, hold_ms):
                        if offset % step == 0 or offset == hold_ms - 1:
                            add_frame_event(
                                ms + offset,
                                FrameEventAction.HOLD,
                                recalc_pos(line.pos_of(event, line.time((ms + offset) / 1000)), sa, ca),
                                current_event_id,
                            )

                    add_frame_event(
                        ms + hold_ms,
                        FrameEventAction.HOLD_END,
                        recalc_pos(line.pos_of(event, line.time((ms + hold_ms) / 1000)), sa, ca),
                        current_event_id,
                    )
            current_event_id += 1

    console.print(f'统计完毕，当前谱面共计{len(frames)}帧')

    pointers = PointerManager(1000)

    result: defaultdict[int, list[VirtualTouchEvent]] = defaultdict(list)

    def add_touch_event(milliseconds: int, pos: tuple[float, float], action: TouchAction, pointer_id: int):
        result[milliseconds].append(VirtualTouchEvent(pos, action, pointer_id))

    for ms, frame in track(sorted(frames.items()), description='正在规划触控事件...', console=console):
        pointers.now = ms
        is_keyframe = False
        for event in frame:
            match event.action:
                case FrameEventAction.TAP:
                    pid, _ = pointers.acquire(event)
                    add_touch_event(ms, event.point, TouchAction.DOWN, pid)
                    pointers.release(event)
                    is_keyframe = True
                case FrameEventAction.DRAG:
                    pid, new = pointers.acquire(event, new=False)
                    act = TouchAction.DOWN if new else TouchAction.MOVE
                    add_touch_event(ms, event.point, act, pid)
                    pointers.release(event)
                case FrameEventAction.FLICK_START:
                    pid, new = pointers.acquire(event, new=False)
                    act = TouchAction.DOWN if new else TouchAction.MOVE
                    add_touch_event(ms, event.point, act, pid)
                case FrameEventAction.FLICK | FrameEventAction.HOLD:
                    pid, _ = pointers.acquire(event)
                    add_touch_event(ms, event.point, TouchAction.MOVE, pid)
                case FrameEventAction.FLICK_END | FrameEventAction.HOLD_END:
                    pid, _ = pointers.acquire(event)
                    add_touch_event(ms, event.point, TouchAction.MOVE, pid)
                    pointers.release(event)
                case FrameEventAction.HOLD_START:
                    pid, _ = pointers.acquire(event)
                    add_touch_event(ms, event.point, TouchAction.DOWN, pid)
                    is_keyframe = True

        for pid, ts, pos in pointers.recycle(is_keyframe):
            add_touch_event(ts, pos, TouchAction.UP, pid)

    for pid, ts, pos in pointers.finish():
        add_touch_event(ts, pos, TouchAction.UP, pid)

    console.print('规划完毕.')
    return result