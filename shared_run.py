"""Paper-only entry point for a worker shared with another application."""
import os
import resource
import shutil
import sys
from pathlib import Path

MOUNT = Path('/var/data')
DATA = MOUNT / 'fomo-lab'
MEMORY_LIMIT = 192 * 1024 * 1024
MIN_FREE = 128 * 1024 * 1024


def mounted(path, mountinfo=None):
    """Check the actual mount table, including Linux bind mounts."""
    if mountinfo is None:
        mountinfo = Path('/proc/self/mountinfo').read_text()
    return any(len(fields := line.split()) > 4 and fields[4] == str(path)
               for line in mountinfo.splitlines())


def preflight(root=MOUNT, mountinfo=None):
    if not mounted(root, mountinfo):
        raise ValueError('permanent disk is missing at /var/data; paper bot disabled')
    if shutil.disk_usage(root).free < MIN_FREE:
        raise ValueError('less than 128 MiB free on permanent disk; paper bot disabled')
    data = root / 'fomo-lab'
    data.mkdir(exist_ok=True)
    # Separate directory, DB and lock; never touch the scanner Redis keys.
    probe = data / '.write-probe'
    with probe.open('w') as f:
        f.write('paper-only\n')
        f.flush()
        os.fsync(f.fileno())
    probe.unlink()
    return data


def run():
    try:
        data = preflight()
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
        os.nice(10)
    except (OSError, ValueError) as exc:
        print(f'[fomo-paper] startup refused: {exc}', flush=True)
        return 78
    import bot
    print('[fomo-paper] permanent disk verified; paper only; 192 MiB address-space limit', flush=True)
    try:
        import fleet
        return fleet.run(data)
    except (ValueError, bot.DataError) as exc:
        # A config/database mismatch needs intervention, not a restart loop.
        print(f'[fomo-paper] stopped: {exc}', flush=True)
        return 78


if __name__ == '__main__':
    sys.exit(run())
