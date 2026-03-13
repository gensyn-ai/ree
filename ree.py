#!/usr/bin/env python3
# ===- ree.py --------------------------------------------------------------===#

# Copyright 2026 Gensyn, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the “Software”), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# ===-----------------------------------------------------------------------===#

"""Primary interface for interacting with the Gensyn Reproducible Execution Environment (REE).

This UI launches `ree.sh` and consumes a lightweight phase protocol from stdout
(`__REE_TUI_PHASE__:<phase>`) and renders:
- Monotonic phase progress
- Structured run output summary
- Full raw log tail

Note: Using this tool will download and invoke the Gensyn REE, which is subject to the REE license
agreement.
"""

from __future__ import annotations

import curses
import os
import queue
import re
import shlex
import signal
import subprocess
import textwrap
import threading
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class StrEnum(str, Enum):
    pass


class Phase(StrEnum):
    IDLE = "idle"
    PULL_START = "pull:start"
    PULL_DONE = "pull:done"
    PREPARE_ARGS = "prepare:args"
    PREPARE_ACL_START = "prepare:acl:start"
    PREPARE_DONE = "prepare:done"
    RUN_START = "run:start"
    RUN_DONE = "run:done"
    RECEIPT_SCAN = "receipt:scan"
    RECEIPT_FOUND = "receipt:found"
    COMPLETE = "complete"
    RUN_FAILED = "run:failed"


class VerificationStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class PhaseInfo:
    phase: Phase
    progress: float
    label: str | None = None


PHASES = (
    PhaseInfo(Phase.PULL_START, 0.05, "Pull image"),
    PhaseInfo(Phase.PULL_DONE, 0.25),
    PhaseInfo(Phase.PREPARE_ARGS, 0.40),
    PhaseInfo(Phase.PREPARE_ACL_START, 0.45),
    PhaseInfo(Phase.PREPARE_DONE, 0.55, "Prepare mounts and ACL"),
    PhaseInfo(Phase.RUN_START, 0.65, "Run gensyn-sdk"),
    PhaseInfo(Phase.RUN_DONE, 0.90, "Command finished"),
    PhaseInfo(Phase.RECEIPT_SCAN, 0.95, "Locate receipt"),
    PhaseInfo(Phase.RECEIPT_FOUND, 0.98),
    PhaseInfo(Phase.COMPLETE, 1.00, "Done"),
    PhaseInfo(Phase.RUN_FAILED, 0.90),
)

PHASE_INFO_BY_PHASE = {info.phase: info for info in PHASES}
TIMELINE_PHASES = tuple(info for info in PHASES if info.label is not None)
LICENSE_PATH = Path("./REE-EULA")

SUBCOMMAND_OPTIONS = [
    "run",
    "receipt",
    "validate",
    "verify",
]


@dataclass
class Field:
    key: str
    label: str
    value: str


class Mode(Enum):
    EDIT = "edit"
    RUNNING = "running"
    DONE = "done"


class CaptureState(Enum):
    IDLE = "idle"
    CAPTURING = "capturing"
    DONE = "done"


class ColorPair(Enum):
    DEFAULT = 1
    SUCCESS = 2
    FAILURE = 3
    DIM = 4
    PROGRESS = 5


class ReeTUI:
    def __init__(self) -> None:
        self.fields = [
            Field("subcommand", "Subcommand", "run"),
            Field("model_name", "Model Name", "Qwen/Qwen3-8B"),
            Field("prompt_text", "Prompt Text", "hello"),
            Field("prompt_file", "Prompt File", ""),
            Field("max_new_tokens", "Max New Tokens", "50"),
            Field("receipt_path", "Receipt Path", ""),
            Field("extra_args", "Extra Args", ""),
        ]
        self.selected = 0
        self.mode = Mode.EDIT
        self.logs: list[str] = []
        self.max_logs = 4000
        self.phase = Phase.IDLE
        self.progress = 0.0
        self.status = "Ready"
        self.proc_lock = threading.Lock()
        self.proc: subprocess.Popen[str] | None = None
        self.proc_thread: threading.Thread | None = None
        self.events: "queue.Queue[tuple[str, str | int | None]]" = queue.Queue()
        self.return_code: int | None = None
        self.last_command: list[str] = []
        self.last_run_at: float | None = None
        self.last_log_at: float | None = None
        self.quit_when_done = False
        self.should_exit = False
        self.cancel_requested = False
        self.pending_cancel = False
        self.pending_force_cancel = False
        self.last_receipt_path = ""
        self.capture_state = CaptureState.IDLE
        self.current_output_lines: list[str] = []
        self.model_outputs: list[str] = []
        self.verify_status: VerificationStatus | None = None
        self.after_pipeline_complete = False
        self.post_complete_output_lines: list[str] = []
        self.reached_phases: set[Phase] = set()
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        self.phase = Phase.IDLE
        self.progress = 0.0
        self.return_code = None
        self.last_run_at = None
        self.last_log_at = None
        self.quit_when_done = False
        self.cancel_requested = False
        self.pending_cancel = False
        self.pending_force_cancel = False
        self.last_receipt_path = ""
        self.capture_state = CaptureState.IDLE
        self.current_output_lines = []
        self.model_outputs = []
        self.verify_status = None
        self.after_pipeline_complete = False
        self.post_complete_output_lines = []
        self.reached_phases = set()
        with self.proc_lock:
            self.proc = None
            self.proc_thread = None

    def field(self, key: str) -> str:
        for f in self.fields:
            if f.key == key:
                return f.value.strip()
        return ""

    def _reset_extra_args(self) -> None:
        for f in self.fields:
            if f.key == "extra_args":
                f.value = ""
                return

    def set_phase(self, phase: Phase) -> None:
        self.phase = phase
        self.reached_phases.add(phase)
        phase_info = PHASE_INFO_BY_PHASE.get(phase)
        if phase_info is not None:
            self.progress = max(self.progress, phase_info.progress)

    def color_attr(self, pair: ColorPair) -> int:
        try:
            return curses.color_pair(pair.value)
        except curses.error:
            return curses.A_NORMAL

    def init_colors(self) -> None:
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            try:
                curses.use_default_colors()
                default_bg = -1
            except curses.error:
                default_bg = curses.COLOR_BLACK
            progress_color = curses.COLOR_MAGENTA
            if curses.can_change_color() and curses.COLORS > 16:
                progress_color = 16
                curses.init_color(progress_color, 980, 843, 820)
            curses.init_pair(ColorPair.DEFAULT.value, curses.COLOR_WHITE, default_bg)
            curses.init_pair(ColorPair.SUCCESS.value, curses.COLOR_GREEN, default_bg)
            curses.init_pair(ColorPair.FAILURE.value, curses.COLOR_RED, default_bg)
            curses.init_pair(ColorPair.DIM.value, curses.COLOR_WHITE, default_bg)
            curses.init_pair(ColorPair.PROGRESS.value, progress_color, default_bg)
        except curses.error:
            pass

    def failed_phase_key(self) -> Phase | None:
        if self.phase != Phase.RUN_FAILED:
            return None
        for phase_info in TIMELINE_PHASES:
            if phase_info.phase not in self.reached_phases:
                return phase_info.phase
        return TIMELINE_PHASES[-1].phase

    def phase_attr(self, phase_key: Phase) -> int:
        failed_phase = self.failed_phase_key()
        if failed_phase == phase_key:
            return self.color_attr(ColorPair.FAILURE)
        if phase_key in self.reached_phases:
            return self.color_attr(ColorPair.SUCCESS)
        return self.color_attr(ColorPair.DEFAULT)

    def status_attr(self) -> int:
        upper = self.status.upper()
        if "FAILED" in upper or "INVALID" in upper:
            return self.color_attr(ColorPair.FAILURE)
        if "SUCCESS" in upper or "SUCCEEDED" in upper or "PASSED" in upper:
            return self.color_attr(ColorPair.SUCCESS)
        return curses.A_BOLD

    def output_line_attr(self, line: str) -> int:
        upper = line.upper()
        if "FAILED" in upper:
            return self.color_attr(ColorPair.FAILURE)
        if "PASSED" in upper or "SUCCEEDED" in upper or "SUCCESS" in upper:
            return self.color_attr(ColorPair.SUCCESS)
        return curses.A_NORMAL

    def log_attr(self, line: str) -> int:
        if "ERROR" in line.upper():
            return self.color_attr(ColorPair.FAILURE)
        return self.color_attr(ColorPair.DIM) | curses.A_DIM

    def progress_attr(self) -> int:
        return self.color_attr(ColorPair.PROGRESS)

    def add_log(self, line: str) -> None:
        self.logs.append(line.rstrip("\n"))
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs :]

    def build_args(self) -> list[str]:
        args: list[str] = []
        subcommand = self.field("subcommand") or "run"
        if subcommand != "run":
            args.append(subcommand)

        if subcommand in ("verify", "validate"):
            receipt_path = self.field("receipt_path")
            if receipt_path:
                args.extend(["--receipt-path", receipt_path])
        else:
            model_name = self.field("model_name")
            if model_name:
                args.extend(["--model-name", model_name])

            prompt_file = self.field("prompt_file")
            prompt_text = self.field("prompt_text")
            if prompt_file:
                args.extend(["--prompt-file", prompt_file])
            elif prompt_text:
                args.extend(["--prompt-text", prompt_text])

            max_new_tokens = self.field("max_new_tokens")
            if max_new_tokens:
                args.extend(["--max-new-tokens", max_new_tokens])

        extra = self.field("extra_args")
        if extra:
            args.extend(shlex.split(extra))

        return args

    def visible_field_indices(self) -> list[int]:
        subcommand = self.field("subcommand") or "run"
        visible_keys = ["subcommand"]
        if subcommand in ("verify", "validate"):
            visible_keys.extend(["receipt_path", "extra_args"])
        else:
            visible_keys.extend(
                [
                    "model_name",
                    "prompt_text",
                    "prompt_file",
                    "max_new_tokens",
                    "extra_args",
                ]
            )
        key_set = set(visible_keys)
        return [i for i, field in enumerate(self.fields) if field.key in key_set]

    def current_subcommand_index(self) -> int:
        value = self.field("subcommand")
        try:
            return SUBCOMMAND_OPTIONS.index(value)
        except ValueError:
            return 0

    def start_run(self) -> None:
        if self.mode == Mode.RUNNING:
            return

        try:
            cmd = ["bash", "./ree.sh", *self.build_args()]
        except ValueError as exc:
            self.mode = Mode.DONE
            self.status = "Invalid extra args"
            self.add_log(f"Invalid extra args: {exc}")
            return

        env = os.environ.copy()
        env["REE_TUI_PHASES"] = "1"
        self.logs = []
        self._reset_run_state()
        self.mode = Mode.RUNNING
        self.status = "Running"
        self.last_command = cmd
        self.last_run_at = time.time()
        self.last_log_at = self.last_run_at
        self.add_log(f"$ {' '.join(shlex.quote(c) for c in cmd)}")

        def worker() -> None:
            rc = 1
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                    # Needed so cancel can signal the whole child process tree.
                    start_new_session=True,
                )
                with self.proc_lock:
                    self.proc = proc
                    force_cancel = self.pending_force_cancel
                    graceful_cancel = self.pending_cancel

                if force_cancel or graceful_cancel:
                    sig = signal.SIGKILL if force_cancel else signal.SIGINT
                    os.killpg(os.getpgid(proc.pid), sig)

                if proc.stdout is None:
                    raise RuntimeError("subprocess stdout was not captured")

                for raw in proc.stdout:
                    self.events.put(("line", raw.rstrip("\n")))

                proc.wait()
                rc = proc.returncode if proc.returncode is not None else 1
            except (OSError, ValueError) as exc:
                self.events.put(("error", str(exc)))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.events.put(("error", f"Unexpected worker error: {exc}"))
                self.events.put(("error", traceback.format_exc(limit=8)))
            finally:
                self.events.put(("done", rc))

        thread = threading.Thread(target=worker, daemon=True)
        with self.proc_lock:
            self.proc_thread = thread
        thread.start()

    def stop_run(self, force: bool = False, exit_after: bool = False) -> None:
        if self.mode != Mode.RUNNING:
            return

        self.quit_when_done = exit_after
        self.cancel_requested = True
        self.pending_cancel = self.pending_cancel or not force
        self.pending_force_cancel = self.pending_force_cancel or force

        with self.proc_lock:
            proc = self.proc

        if proc is None:
            self.status = "Cancelling... (waiting for process start)"
            return

        try:
            pgid = os.getpgid(proc.pid)
            if force:
                os.killpg(pgid, signal.SIGKILL)
                self.status = "Force stopping..."
            else:
                os.killpg(pgid, signal.SIGINT)
                self.status = "Cancelling..."
        except ProcessLookupError:
            self.status = "Stopping..."
        except OSError as exc:
            self.add_log(f"Stop signal failed: {exc}")
            self.status = "Stop failed"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.add_log(f"Unexpected stop error: {exc}")
            self.add_log(traceback.format_exc(limit=8))
            self.status = "Stop failed"

    def reset_view_state(self) -> None:
        if self.mode == Mode.RUNNING:
            return
        self.logs = []
        self._reset_run_state()
        self.status = "Ready"
        self.mode = Mode.EDIT

    def flush_output_buffer(self) -> None:
        output = "\n".join(self.current_output_lines).strip()
        if output:
            self.model_outputs.append(output)
            self.model_outputs = self.model_outputs[-3:]
        self.current_output_lines = []

    def _is_sdk_log_line(self, stripped: str) -> bool:
        return bool(re.match(r"^[A-Z]\s+\S+:\s", stripped))

    def _update_capture_state(
        self, stripped: str, upper: str, is_sdk_log: bool
    ) -> None:
        if "PRINTING MODEL OUTPUTS" in upper:
            self.capture_state = CaptureState.CAPTURING
            self.current_output_lines = []
            return

        if self.capture_state == CaptureState.CAPTURING:
            if stripped == "===========":
                self.flush_output_buffer()
                # Stop capturing after the first separator within this output block.
                self.capture_state = CaptureState.DONE
                return
            if stripped.startswith("Receipt:") or stripped.startswith(
                "Running command:"
            ):
                self.flush_output_buffer()
                self.capture_state = CaptureState.DONE
            elif stripped and not is_sdk_log:
                self.current_output_lines.append(stripped)

    def _update_post_pipeline_state(
        self, stripped: str, upper: str, is_sdk_log: bool
    ) -> None:
        if "RUN-ALL PIPELINE COMPLETE" in upper:
            self.after_pipeline_complete = True
            return

        if self.after_pipeline_complete:
            if not stripped:
                return
            if stripped.startswith("Receipt:"):
                self.after_pipeline_complete = False
                return
            if stripped.startswith("Running command:"):
                return
            if is_sdk_log:
                return
            self.post_complete_output_lines.append(stripped)
            self.post_complete_output_lines = self.post_complete_output_lines[-8:]

    def _update_verification_status(self, upper: str) -> None:
        if "VERIFICATION PASSED" in upper:
            self.verify_status = VerificationStatus.PASSED
        elif "VERIFICATION FAILED" in upper:
            self.verify_status = VerificationStatus.FAILED
        elif "VERIFY FAILED:" in upper:
            self.verify_status = VerificationStatus.FAILED

    def parse_result_line(self, line: str) -> None:
        stripped = line.strip()
        upper = stripped.upper()
        is_sdk_log = self._is_sdk_log_line(stripped)
        self._update_capture_state(stripped, upper, is_sdk_log)
        self._update_post_pipeline_state(stripped, upper, is_sdk_log)
        self._update_verification_status(upper)

    def output_summary_lines(self) -> list[str]:
        lines: list[str] = []
        if self.verify_status is not None:
            lines.append(f"Verification: {self.verify_status.value}")
        if self.last_receipt_path:
            lines.append(f"Receipt: {self.last_receipt_path}")
        if self.post_complete_output_lines:
            lines.append("Model Output:")
            lines.extend(self.post_complete_output_lines[-4:])
            return lines
        if self.model_outputs:
            lines.append("Model Output:")
            output_lines = self.model_outputs[-1].splitlines()
            max_lines = 4
            lines.extend(output_lines[:max_lines])
            if len(output_lines) > max_lines:
                lines.append("...")
        if not lines:
            lines.append("No parsed output yet. Check logs below for full details.")
        return lines

    def read_license_lines(self) -> list[str]:
        try:
            return LICENSE_PATH.read_text(encoding="utf-8").splitlines() or [""]
        except OSError as exc:
            return [f"Failed to read {LICENSE_PATH}: {exc}"]

    def show_license(self, stdscr: curses.window) -> None:
        try:
            stdscr.nodelay(False)
            stdscr.timeout(-1)
        except curses.error:
            pass

        raw_lines = self.read_license_lines()
        offset = 0

        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            if h < 4 or w < 20:
                try:
                    stdscr.addstr(0, 0, "Terminal too small for license viewer")
                    stdscr.refresh()
                    stdscr.getch()
                except curses.error:
                    pass
                break

            body_width = max(1, w - 2)
            wrapped_lines: list[str] = []
            for line in raw_lines:
                wrapped_lines.extend(textwrap.wrap(line, width=body_width) or [""])

            body_height = max(1, h - 3)
            max_offset = max(0, len(wrapped_lines) - body_height)
            offset = min(offset, max_offset)

            try:
                stdscr.addstr(
                    0,
                    1,
                    f"REE License ({LICENSE_PATH.name})"[: w - 2],
                    curses.A_BOLD,
                )
                for row in range(body_height):
                    line_index = offset + row
                    if line_index >= len(wrapped_lines):
                        break
                    stdscr.addstr(row + 1, 1, wrapped_lines[line_index][: w - 2])
                stdscr.addstr(
                    h - 1,
                    1,
                    "q close | Up/Down scroll | PgUp/PgDn page"[: w - 2],
                    curses.A_DIM,
                )
                stdscr.refresh()
            except curses.error:
                pass

            key = stdscr.getch()
            if key in (ord("q"), ord("l"), 27, 10, 13, curses.KEY_ENTER):
                break
            if key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                offset = min(max_offset, offset + 1)
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - body_height)
            elif key == curses.KEY_NPAGE:
                offset = min(max_offset, offset + body_height)
            elif key == curses.KEY_HOME:
                offset = 0
            elif key == curses.KEY_END:
                offset = max_offset

        try:
            stdscr.nodelay(True)
            stdscr.timeout(100)
        except curses.error:
            pass

    def handle_event(self, kind: str, payload: str | int | None) -> None:
        if kind == "line":
            assert isinstance(payload, str)
            self.last_log_at = time.time()
            if payload.startswith("__REE_TUI_PHASE__:"):
                phase_value = payload.removeprefix("__REE_TUI_PHASE__:")
                try:
                    self.set_phase(Phase(phase_value))
                except ValueError:
                    self.add_log(f"Unknown phase: {phase_value}")
                return
            self.parse_result_line(payload)
            self.add_log(payload)
            if "Receipt:" in payload:
                self.set_phase(Phase.RECEIPT_FOUND)
                self.last_receipt_path = payload.split("Receipt:", 1)[1].strip()
            return

        if kind == "error":
            assert isinstance(payload, str)
            self.add_log(f"Failed to start process: {payload}")
            self.set_phase(Phase.RUN_FAILED)
            return

        if kind == "done":
            assert isinstance(payload, int)
            if self.capture_state == CaptureState.CAPTURING:
                self.flush_output_buffer()
                self.capture_state = CaptureState.DONE
            self.return_code = payload
            if self.cancel_requested:
                self.mode = Mode.DONE
                self.status = "Cancelled"
                self.set_phase(Phase.RUN_FAILED)
            else:
                self.mode = Mode.DONE
                if payload == 0:
                    self.status = "Success"
                    self.set_phase(Phase.COMPLETE)
                else:
                    self.status = f"Failed (exit {payload})"
                    self.set_phase(Phase.RUN_FAILED)
            if self.quit_when_done:
                self.should_exit = True

    def select_subcommand(self, stdscr: curses.window) -> None:
        h, w = stdscr.getmaxyx()
        win_h = min(len(SUBCOMMAND_OPTIONS) + 4, h - 2)
        win_w = min(w - 4, 100)
        if win_h < 4 or win_w < 12:
            self.status = "Terminal too small for selector"
            return
        win_y = (h - win_h) // 2
        win_x = (w - win_w) // 2
        try:
            win = curses.newwin(win_h, win_w, win_y, win_x)
        except curses.error:
            self.status = "Terminal too small for selector"
            return
        win.keypad(True)
        win.box()
        win.addstr(1, 2, "Select subcommand")
        selected = self.current_subcommand_index()

        while True:
            for i, option in enumerate(SUBCOMMAND_OPTIONS[: max(0, win_h - 3)]):
                attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
                win.addstr(2 + i, 2, option.ljust(win_w - 4)[: win_w - 4], attr)
            win.refresh()
            key = win.getch()
            if key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(SUBCOMMAND_OPTIONS) - 1, selected + 1)
            elif key in (10, 13, curses.KEY_ENTER):
                prev = self.fields[self.selected].value
                self.fields[self.selected].value = SUBCOMMAND_OPTIONS[selected]
                if self.fields[self.selected].value != prev:
                    self._reset_extra_args()
                return
            elif key in (27, ord("q")):
                return

    def edit_selected_field(self, stdscr: curses.window) -> None:
        visible = self.visible_field_indices()
        if not visible:
            return
        if self.selected not in visible:
            self.selected = visible[0]
        field = self.fields[self.selected]
        if field.key == "subcommand":
            self.select_subcommand(stdscr)
            return

        h, w = stdscr.getmaxyx()
        win_h = 5
        win_w = min(w - 4, 120)
        if win_h < 4 or win_w < 12:
            self.status = "Terminal too small for editor"
            return
        win_y = (h - win_h) // 2
        win_x = (w - win_w) // 2
        try:
            win = curses.newwin(win_h, win_w, win_y, win_x)
        except curses.error:
            self.status = "Terminal too small for editor"
            return
        win.keypad(True)
        win.box()
        win.addstr(1, 2, f"{field.label} (Enter save, Esc cancel)"[: win_w - 4])

        value = field.value
        cursor = len(value)
        saved_escdelay = None
        if hasattr(curses, "get_escdelay") and hasattr(curses, "set_escdelay"):
            try:
                saved_escdelay = curses.get_escdelay()
                curses.set_escdelay(25)
            except curses.error:
                saved_escdelay = None

        try:
            while True:
                view_w = max(1, win_w - 4)
                if cursor <= view_w:
                    offset = 0
                else:
                    offset = cursor - view_w + 1
                displayed_value = value[offset : offset + view_w]
                win.addstr(2, 2, " " * view_w)
                win.addstr(2, 2, displayed_value)
                cursor_x = 2 + max(0, min(view_w - 1, cursor - offset))
                win.move(2, cursor_x)
                win.refresh()
                try:
                    curses.curs_set(1)
                except curses.error:
                    pass
                try:
                    key = win.getch()
                finally:
                    try:
                        curses.curs_set(0)
                    except curses.error:
                        pass

                if key in (10, 13, curses.KEY_ENTER):
                    field.value = value
                    return
                if key == 27:
                    # On mac terminals, Fn+Delete may begin with ESC and then
                    # produce "[3~". Use a non-blocking probe to disambiguate.
                    win.nodelay(True)
                    try:
                        k1 = win.getch()
                        if k1 == -1:
                            return
                        if k1 == ord("["):
                            k2 = win.getch()
                            k3 = win.getch()
                            if k2 == ord("3") and k3 == ord("~"):
                                if cursor < len(value):
                                    value = value[:cursor] + value[cursor + 1 :]
                                continue
                    finally:
                        win.nodelay(False)
                    return
                if key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                    continue
                if key == curses.KEY_RIGHT:
                    cursor = min(len(value), cursor + 1)
                    continue
                if key == curses.KEY_HOME:
                    cursor = 0
                    continue
                if key == curses.KEY_END:
                    cursor = len(value)
                    continue
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    if cursor > 0:
                        value = value[: cursor - 1] + value[cursor:]
                        cursor -= 1
                    continue
                if key == curses.KEY_DC:
                    if cursor < len(value):
                        value = value[:cursor] + value[cursor + 1 :]
                    continue
                if 32 <= key <= 126:
                    value = value[:cursor] + chr(key) + value[cursor:]
                    cursor += 1
        finally:
            if saved_escdelay is not None and hasattr(curses, "set_escdelay"):
                try:
                    curses.set_escdelay(saved_escdelay)
                except curses.error:
                    pass

    def draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        title = "Gensyn REE"
        run_info = f"Status: {self.status}"
        stdscr.addstr(0, 1, title[: w - 2], curses.A_BOLD)
        stdscr.addstr(
            0,
            max(1, w - len(run_info) - 2),
            run_info[: w - 2],
            self.status_attr() | curses.A_BOLD,
        )

        bar_y = 2
        bar_x = 1
        bar_w = max(10, w - 2)
        inner_w = bar_w - 2
        fill = int(inner_w * self.progress)
        pct = f" {int(self.progress * 100):3d}%"
        stdscr.addstr(bar_y, bar_x, "[")
        if fill > 0:
            stdscr.addstr(bar_y, bar_x + 1, "#" * fill, self.progress_attr())
        if fill < inner_w:
            stdscr.addstr(bar_y, bar_x + 1 + fill, "-" * (inner_w - fill))
        stdscr.addstr(bar_y, bar_x + bar_w - 1, "]")
        stdscr.addstr(bar_y, bar_x + bar_w, pct[: max(0, w - (bar_x + bar_w) - 1)])
        phase_line = f"Current phase: {self.phase.value}"
        if self.last_run_at and self.mode == Mode.RUNNING:
            elapsed = int(time.time() - self.last_run_at)
            heartbeat = "|/-\\"[int(time.time() * 5) % 4]
            quiet_for = (
                0 if not self.last_log_at else int(time.time() - self.last_log_at)
            )
            phase_line += f" | {heartbeat} alive | Elapsed: {elapsed}s | Last log: {quiet_for}s ago"
        stdscr.addstr(bar_y + 1, 1, phase_line[: w - 2], curses.A_DIM)

        phase_y = 5
        current_y = phase_y
        for phase_info in TIMELINE_PHASES:
            if current_y >= h:
                break
            phase_key = phase_info.phase
            done = phase_key in self.reached_phases
            marker = "[x]" if done else "[ ]"
            line = f"{marker} {phase_info.label}"
            if phase_key == Phase.RECEIPT_SCAN and self.last_receipt_path:
                line = f"{line}: {self.last_receipt_path}"
            wrapped = textwrap.wrap(line, width=max(10, w - 2)) or [""]
            for wrapped_line in wrapped:
                if current_y >= h:
                    break
                stdscr.addstr(
                    current_y,
                    1,
                    wrapped_line[: w - 2],
                    self.phase_attr(phase_key),
                )
                current_y += 1

        if current_y + 1 < h:
            current_y += 1
            stdscr.addstr(current_y, 1, "REE Output", curses.A_UNDERLINE)
            current_y += 1
            for line in self.output_summary_lines():
                wrapped = textwrap.wrap(line, width=max(10, w - 2)) or [""]
                for wrapped_line in wrapped:
                    if current_y >= h:
                        break
                    stdscr.addstr(
                        current_y,
                        1,
                        wrapped_line[: w - 2],
                        self.output_line_attr(line),
                    )
                    current_y += 1

        form_start = current_y + 2
        visible_indices = self.visible_field_indices()
        selected_for_draw = self.selected
        if visible_indices and selected_for_draw not in visible_indices:
            selected_for_draw = visible_indices[0]
        form_max = min(len(visible_indices), max(0, h - form_start - 8))
        stdscr.addstr(
            form_start - 1,
            1,
            "Fields (Enter edit, arrows move, r run)",
            curses.A_UNDERLINE,
        )
        for row in range(form_max):
            idx = visible_indices[row]
            field = self.fields[idx]
            y = form_start + row
            val = field.value if field.value else "-"
            if field.key == "subcommand":
                val = f"{val} v"
            line = f"{field.label:14} {val}"
            attr = (
                curses.A_REVERSE
                if idx == selected_for_draw and self.mode != Mode.RUNNING
                else curses.A_NORMAL
            )
            stdscr.addstr(y, 1, line[: w - 2], attr)

        logs_y = form_start + form_max + 1
        if logs_y < h - 2:
            stdscr.addstr(logs_y, 1, "Logs", curses.A_UNDERLINE)
            log_height = max(1, h - logs_y - 2)
            tail = self.logs[-log_height:]
            for i, line in enumerate(tail):
                stdscr.addstr(logs_y + 1 + i, 1, line[: w - 2], self.log_attr(line))

        help_text = "q quit | r run | c cancel | e reset run state | l license"
        stdscr.addstr(h - 1, 1, help_text[: w - 2], curses.A_DIM)
        stdscr.refresh()

    def handle_key(self, stdscr: curses.window, key: int) -> bool:
        if key == -1:
            return True
        if key == ord("l"):
            self.show_license(stdscr)
            return True
        if key in (ord("q"), 27):
            if self.mode == Mode.RUNNING:
                self.stop_run(force=True, exit_after=True)
                return True
            return False

        if self.mode == Mode.RUNNING:
            if key == ord("c"):
                self.stop_run(force=False, exit_after=False)
            return True

        visible = self.visible_field_indices()
        if visible and self.selected not in visible:
            self.selected = visible[0]

        if key in (curses.KEY_UP, ord("k")):
            if visible:
                pos = visible.index(self.selected)
                self.selected = visible[max(0, pos - 1)]
        elif key in (curses.KEY_DOWN, ord("j")):
            if visible:
                pos = visible.index(self.selected)
                self.selected = visible[min(len(visible) - 1, pos + 1)]
        elif key == curses.KEY_LEFT and self.fields[self.selected].key == "subcommand":
            idx = self.current_subcommand_index()
            new_idx = max(0, idx - 1)
            self.fields[self.selected].value = SUBCOMMAND_OPTIONS[new_idx]
            if new_idx != idx:
                self._reset_extra_args()
            visible = self.visible_field_indices()
            if self.selected not in visible and visible:
                self.selected = visible[0]
        elif key == curses.KEY_RIGHT and self.fields[self.selected].key == "subcommand":
            idx = self.current_subcommand_index()
            new_idx = min(len(SUBCOMMAND_OPTIONS) - 1, idx + 1)
            self.fields[self.selected].value = SUBCOMMAND_OPTIONS[new_idx]
            if new_idx != idx:
                self._reset_extra_args()
            visible = self.visible_field_indices()
            if self.selected not in visible and visible:
                self.selected = visible[0]
        elif key in (10, 13, curses.KEY_ENTER):
            self.edit_selected_field(stdscr)
        elif key == ord("r"):
            self.start_run()
        elif key == ord("e"):
            self.reset_view_state()
        return True

    def run(self, stdscr: curses.window) -> None:
        self.init_colors()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.nodelay(True)
        stdscr.timeout(100)
        while True:
            while True:
                try:
                    kind, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                self.handle_event(kind, payload)

            try:
                self.draw(stdscr)
            except curses.error:
                # Resize or tiny terminal during draw; retry next frame.
                pass
            key = stdscr.getch()
            if not self.handle_key(stdscr, key):
                break
            if self.should_exit:
                break


def main() -> int:
    if not Path("./ree.sh").exists():
        print("ree.sh not found in current directory")
        return 2
    tui = ReeTUI()
    curses.wrapper(tui.run)
    if tui.return_code is None:
        return 0
    if tui.return_code < 0:
        return 128 + abs(tui.return_code)
    return tui.return_code


if __name__ == "__main__":
    raise SystemExit(main())
