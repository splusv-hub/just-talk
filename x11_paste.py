"""
X11 底层粘贴模块 - 使用 XTest 扩展

方案 B: PRIMARY + Shift+Insert
- 整个流程（设置 selection、发送组合键、响应请求）在同一后台线程内完成
- 避免阻塞主应用程序的事件循环（或者造成长达几秒的卡顿）
- 修复了跨 Display 连接导致无法响应该 SelectionRequest 的卡死问题
"""

import logging
import threading
import time
from typing import List, Optional

try:
    from Xlib import display, X, XK, Xatom
    from Xlib.ext import xtest
    from Xlib.protocol import event
    XLIB_AVAILABLE = True
except ImportError:
    XLIB_AVAILABLE = False


LOG = logging.getLogger("just-talk")


class X11Paste:
    """X11 底层粘贴实现 - 方案 B: PRIMARY + Shift+Insert"""

    def __init__(self):
        if not XLIB_AVAILABLE:
            raise RuntimeError("python-xlib not available")

        self._handler_thread: Optional[threading.Thread] = None
        self._stop_handler = False
        self._run_counter = 0

    def paste(self, text: str) -> bool:
        """
        粘贴文本到当前焦点窗口
        
        启动一个后台线程完成全套粘贴流程，立即返回不阻塞主线程。

        Args:
            text: 要粘贴的文本

        Returns:
            如果成功启动流程返回 True，否则 False
        """
        if not XLIB_AVAILABLE:
            return False

        try:
            self.cleanup()

            self._stop_handler = False
            self._run_counter += 1
            run_id = self._run_counter
            self._handler_thread = threading.Thread(
                target=self._paste_process,
                args=(text, run_id, False),
                daemon=True
            )
            self._handler_thread.start()
            LOG.info("X11_PASTE start run=%s len=%d", run_id, len(text))
            
            # 等待一小段时间，确保后台线程至少发出了按键
            # 这样可以在函数返回后调用方执行其他操作时，按键已经生效
            time.sleep(0.05)
            
            return True
        except Exception:
            return False

    def reproduce_race(self, texts: List[str], stagger_ms: int = 8) -> bool:
        """
        专门用于复现旧线程未退、新线程又复活共享 stop 标记的竞态。

        这里故意在极短时间内反复 cleanup + 重启后台线程，不等待旧线程退出。
        """
        if not XLIB_AVAILABLE:
            return False
        if not texts:
            return False
        try:
            for index, text in enumerate(texts):
                self.cleanup()
                self._stop_handler = False
                self._run_counter += 1
                run_id = self._run_counter
                thread = threading.Thread(
                    target=self._paste_process,
                    args=(text, run_id, True),
                    daemon=True,
                    name=f"x11-paste-race-{run_id}",
                )
                self._handler_thread = thread
                thread.start()
                LOG.info(
                    "X11_PASTE reproduce start run=%s step=%s/%s len=%d stagger_ms=%d",
                    run_id,
                    index + 1,
                    len(texts),
                    len(text),
                    stagger_ms,
                )
                time.sleep(max(0, stagger_ms) / 1000.0)
            return True
        except Exception:
            LOG.exception("X11_PASTE reproduce_race failed")
            return False

    def _paste_process(self, text: str, run_id: int, forced_race: bool):
        """后台线程中执行的完整粘贴流程"""
        disp: Optional[display.Display] = None
        owner_window = None
        selection_text = text.encode("utf-8")
        try:
            selection_latin1 = text.encode("latin-1")
        except UnicodeEncodeError:
            selection_latin1 = None

        try:
            LOG.info(
                "X11_PASTE worker_enter run=%s forced_race=%s thread=%s",
                run_id,
                forced_race,
                threading.get_ident(),
            )
            disp = display.Display()
            root = disp.screen().root

            atom_primary = disp.intern_atom("PRIMARY")
            atom_utf8 = disp.intern_atom("UTF8_STRING")
            atom_targets = disp.intern_atom("TARGETS")
            atom_text = disp.intern_atom("TEXT")
            atom_text_plain = disp.intern_atom("text/plain")
            atom_text_plain_utf8 = disp.intern_atom("text/plain;charset=utf-8")

            shift_keycode = disp.keysym_to_keycode(XK.XK_Shift_L)
            insert_keycode = disp.keysym_to_keycode(XK.XK_Insert)

            # 1. 创建 owner 窗口并接管 PRIMARY
            owner_window = root.create_window(
                0, 0, 1, 1, 0, X.CopyFromParent, X.InputOnly
            )
            owner_window.set_selection_owner(atom_primary, X.CurrentTime)
            disp.sync()

            actual_owner = disp.get_selection_owner(atom_primary)
            if actual_owner != owner_window:
                LOG.info("X11_PASTE owner_failed run=%s", run_id)
                return  # 获取 ownership 失败
            LOG.info("X11_PASTE owner_ok run=%s", run_id)

            # 2. 模拟 Shift+Insert 组合键
            xtest.fake_input(disp, X.KeyPress, shift_keycode)
            disp.sync()
            time.sleep(0.01)
            xtest.fake_input(disp, X.KeyPress, insert_keycode)
            disp.sync()
            time.sleep(0.01)
            xtest.fake_input(disp, X.KeyRelease, insert_keycode)
            disp.sync()
            time.sleep(0.01)
            xtest.fake_input(disp, X.KeyRelease, shift_keycode)
            disp.sync()

            # 3. 处理目标窗口发来的 SelectionRequest 事件
            start = time.time()
            timeout = 2.0
            handled = 0

            while not self._stop_handler and time.time() - start < timeout:
                if disp.pending_events():
                    ev = disp.next_event()
                    if ev.type == X.SelectionRequest:
                        LOG.info("X11_PASTE selection_request run=%s handled=%d", run_id, handled + 1)
                        self._respond_selection(
                            ev, disp, selection_text,
                            selection_latin1,
                            atom_utf8, atom_targets,
                            atom_text, atom_text_plain, atom_text_plain_utf8,
                        )
                        handled += 1
                        if handled >= 5:  # 处理足够多的请求后可提早退出
                            break
                    elif ev.type == X.SelectionClear:
                        # 其它窗口接管了 PRIMARY
                        LOG.info("X11_PASTE selection_clear run=%s", run_id)
                        break
                else:
                    time.sleep(0.01)

        except Exception:
            LOG.exception("X11_PASTE worker_failed run=%s", run_id)
        finally:
            LOG.info(
                "X11_PASTE worker_exit run=%s forced_race=%s stop=%s",
                run_id,
                forced_race,
                self._stop_handler,
            )
            if owner_window:
                try:
                    owner_window.destroy()
                except Exception:
                    pass
            if disp:
                try:
                    disp.flush()
                    disp.close()
                except Exception:
                    pass

    def _respond_selection(
        self,
        ev,
        disp: display.Display,
        selection_text: bytes,
        selection_latin1: Optional[bytes],
        atom_utf8,
        atom_targets,
        atom_text,
        atom_text_plain,
        atom_text_plain_utf8,
    ):
        """发送 SelectionNotify 响应请求"""
        target = ev.target
        prop = ev.property if ev.property else ev.target

        try:
            if target == atom_targets:
                targets = [atom_utf8, atom_text_plain_utf8, atom_text_plain, atom_text]
                if selection_latin1 is not None:
                    targets.append(Xatom.STRING)
                ev.requestor.change_property(
                    prop, Xatom.ATOM, 32,
                    targets,
                )
            elif target in (atom_utf8, atom_text_plain_utf8, atom_text_plain, atom_text):
                ev.requestor.change_property(
                    prop, atom_utf8, 8, selection_text
                )
            elif target == Xatom.STRING and selection_latin1 is not None:
                ev.requestor.change_property(
                    prop, Xatom.STRING, 8, selection_latin1
                )
            else:
                prop = X.NONE

            notify = event.SelectionNotify(
                time=ev.time,
                requestor=ev.requestor,
                selection=ev.selection,
                target=target,
                property=prop,
            )
            ev.requestor.send_event(notify)
            disp.flush()
        except Exception:
            try:
                reject = event.SelectionNotify(
                    time=ev.time,
                    requestor=ev.requestor,
                    selection=ev.selection,
                    target=target,
                    property=X.NONE,
                )
                ev.requestor.send_event(reject)
                disp.flush()
            except Exception:
                pass

    def cleanup(self):
        """清理当前的后台流程"""
        self._stop_handler = True
        LOG.info("X11_PASTE cleanup alive=%s", bool(self._handler_thread and self._handler_thread.is_alive()))
        if self._handler_thread and self._handler_thread.is_alive():
            # 这里不用 join 阻塞，让其在后台自行自然退出
            pass
        self._handler_thread = None


# 单例实例
_x11_paste: Optional[X11Paste] = None


def x11_paste(text: str) -> bool:
    """
    使用 X11 底层 API 粘贴文本（PRIMARY + Shift+Insert）
    """
    global _x11_paste

    if not XLIB_AVAILABLE:
        return False

    try:
        if _x11_paste is None:
            _x11_paste = X11Paste()
        return _x11_paste.paste(text)
    except Exception:
        if _x11_paste:
            _x11_paste.cleanup()
            _x11_paste = None
        return False


def x11_paste_reproduce_race(texts: List[str], stagger_ms: int = 8) -> bool:
    """专门用于复现 X11 粘贴竞态。"""
    global _x11_paste

    if not XLIB_AVAILABLE:
        return False

    try:
        if _x11_paste is None:
            _x11_paste = X11Paste()
        return _x11_paste.reproduce_race(texts, stagger_ms=stagger_ms)
    except Exception:
        if _x11_paste:
            _x11_paste.cleanup()
            _x11_paste = None
        return False


def is_available() -> bool:
    """检查 X11 粘贴是否可用"""
    return XLIB_AVAILABLE
