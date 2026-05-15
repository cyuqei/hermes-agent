#!/usr/bin/env python3
"""Local resize test harness using pyte (terminal emulator) + PTY.

Spawns hermes inside a real PTY, drives it through resize sequences,
captures all output through a pyte HistoryScreen (which models scrollback),
and counts duplicate status-bar markers in scrollback after each resize.

Usage:
    HERMES_RESIZE_STRATEGY=native python tools_test/resize_harness.py
    HERMES_RESIZE_STRATEGY=clear  python tools_test/resize_harness.py
"""
import os, pty, fcntl, termios, struct, time, select, signal, sys, re
import pyte

REPO = '/Users/brooklyn/www/hermes-agent'
HERMES = os.path.join(REPO, '.venv', 'bin', 'hermes')

STATUS_RE = re.compile(r'claude-opus')
INPUT_RE  = re.compile(r'^\s*❯')


def set_winsz(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))


def drain(fd, screen, stream, timeout=1.0):
    """Read available bytes from PTY and feed them to pyte."""
    end = time.time() + timeout
    total = 0
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            continue
        try:
            chunk = os.read(fd, 16384)
        except OSError:
            break
        if not chunk:
            break
        try:
            stream.feed(chunk.decode('utf-8', 'replace'))
        except Exception:
            pass
        total += len(chunk)
    return total


def visible_lines(screen):
    return [screen.buffer[y] for y in range(screen.lines)]


def count_status_bars_in_scrollback_and_viewport(screen):
    """Count status-bar lines visible in (history + viewport).

    pyte.HistoryScreen has `screen.history.top` (bottom of scrollback) and
    `screen.history.bottom` (top of newer scrollback that wraps). We just
    iterate every Char-row currently held.
    """
    count = 0
    rows = []
    # Scrollback (older) rows
    for line in screen.history.top:
        text = ''.join(c.data for c in (line.values() if hasattr(line, 'values') else line))
        rows.append(text)
    for line in screen.history.bottom:
        text = ''.join(c.data for c in (line.values() if hasattr(line, 'values') else line))
        rows.append(text)
    # Visible viewport
    for y in range(screen.lines):
        text = ''.join(screen.buffer[y][x].data for x in range(screen.columns))
        rows.append(text)
    for r in rows:
        if STATUS_RE.search(r):
            count += 1
    return count, rows


def run(strategy="native"):
    env = os.environ.copy()
    env['TERM'] = 'xterm-256color'
    env['HERMES_DEBUG_RESIZE'] = '1'
    env['HERMES_RESIZE_STRATEGY'] = strategy

    # Start with generous size
    INITIAL_ROWS, INITIAL_COLS = 50, 160
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(HERMES, [HERMES], env)

    set_winsz(fd, INITIAL_ROWS, INITIAL_COLS)
    # pyte HistoryScreen tracks scrollback; ratio=0.5 keeps a few full screens
    screen = pyte.HistoryScreen(INITIAL_COLS, INITIAL_ROWS, history=1000, ratio=0.5)
    stream = pyte.Stream(screen)

    # Wait for banner + prompt to settle
    drain(fd, screen, stream, timeout=3.5)

    # Type something so input area is non-empty
    os.write(fd, b'rawr')
    drain(fd, screen, stream, timeout=0.6)

    # Resize: a sequence of column shrinks (the broken case)
    shrinks = [140, 120, 100, 80, 60, 50, 40]
    samples = []
    for cols in shrinks:
        set_winsz(fd, INITIAL_ROWS, cols)
        # pyte must be told the new size too — it has its own internal grid
        screen.resize(INITIAL_ROWS, cols)
        time.sleep(0.25)
        drain(fd, screen, stream, timeout=0.5)
        c, _ = count_status_bars_in_scrollback_and_viewport(screen)
        samples.append((cols, c))

    # quit
    os.write(fd, b'\n/quit\n')
    drain(fd, screen, stream, timeout=0.5)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass

    print(f"strategy={strategy}")
    for cols, c in samples:
        print(f"  after shrink to {cols} cols: {c} status-bar rows in scrollback+viewport")
    final_count, rows = count_status_bars_in_scrollback_and_viewport(screen)
    print(f"  final total: {final_count}")
    return samples, final_count


if __name__ == '__main__':
    strategy = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('HERMES_RESIZE_STRATEGY', 'native')
    run(strategy)
