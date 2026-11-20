"""Measure actual Tk geometry with a 576-logical-pixel VPS viewport."""
import ctypes
from ctypes import wintypes
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from control_host import ControlHost, LocalClient
from test_control_host import FakeStrategy


class SmallDesktopTests(unittest.TestCase):
    def test_main_and_all_settings_fit(self):
        loader = SourceFileLoader("small_desktop", str(Path(__file__).with_name("recovery_desktop.pyw")))
        spec = spec_from_loader(loader.name, loader)
        gui = module_from_spec(spec)
        loader.exec_module(gui)
        root = gui.tk.Tk()
        root.attributes("-alpha", 0)
        root.attributes("-toolwindow", True)
        self.addCleanup(root.destroy)
        observed = gui.work_area(root)
        area = (0, 0, min(576, observed[2]), min(900, observed[3]))
        base_toplevel = gui.tk.Toplevel
        class InvisibleDialog(base_toplevel):
            def __init__(self, master):
                super().__init__(master)
                self.attributes("-alpha", 0)
                self.attributes("-toolwindow", True)
        def bounds(widget):
            x,y=widget.winfo_rootx(),widget.winfo_rooty()
            return x,y,x+widget.winfo_width(),y+widget.winfo_height()
        def visible(child, owner):
            self.assertTrue(child.winfo_ismapped(), child)
            left, top, right, bottom = bounds(child)
            x,y,x2,y2=bounds(owner)
            self.assertGreaterEqual(left, x)
            self.assertGreaterEqual(top, y)
            self.assertLessEqual(right, min(x2, area[2]))
            self.assertLessEqual(bottom, min(y2, area[3]))
        with tempfile.TemporaryDirectory() as tmp, ControlHost(tmp) as host, patch.object(gui, "work_area", return_value=area), patch.object(gui.tk,"Toplevel",InvisibleDialog):
            strategy=FakeStrategy()
            host.poll(strategy)
            ui=gui.Desktop(root,LocalClient(tmp),lambda:None)
            root.update()
            for button in (*ui.buttons.values(),ui.settings_btn):
                visible(button,root)
            self.assertLess(bounds(ui.settings_btn)[0]-bounds(ui.buttons["pause"])[2],20)
            ui.edit()
            root.update()
            for field in (*ui.fields,ui.save_btn):
                visible(field,ui.editor)
            self.assertEqual(len(ui.fields),26)
            strategy.data.update(mode="active", running=True, can_start=False, step=7,
                                 position_ticket=1122334455, opposite_ticket=2233445566)
            for reason in ("move_rejected_price", "awaiting_move_evidence", "interrupted_move_retirement"):
                strategy.data["reason"] = reason
                host.poll(strategy)
                ui.state = ui.client.status()
                ui.render()
                root.update_idletasks()
                self.assertIn(gui.REASONS[reason], ui.details.get())
                for button in (*ui.buttons.values(), ui.settings_btn):
                    visible(button, root)
            # Real desktop scope is a test TEMP directory. No requests were sent.
            self.assertEqual(list(host.commands.glob("*.json")),[])
            for window in (root,ui.editor):
                print("Tk bounds",bounds(window))
                hwnd=ctypes.windll.user32.GetAncestor(window.winfo_id(),2)
                rect=wintypes.RECT()
                result=ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd,9,ctypes.byref(rect),ctypes.sizeof(rect))
                if result==0:
                    print("DWM frame",rect.left,rect.top,rect.right,rect.bottom)


if __name__ == "__main__":
    unittest.main()
