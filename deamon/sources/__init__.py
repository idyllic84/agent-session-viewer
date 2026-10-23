"""Built-in read-only history sources and trusted root configuration."""
import json
import os
from pathlib import Path
import sys
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .copilot_cli import CopilotCLIAdapter
from .pi import PiAdapter
from .vscode_chat import VSCodeChatAdapter


def default_adapters(overrides=None):
    home = Path.home()
    codex = Path(os.environ.get('CODEX_HOME') or home / '.codex').expanduser()
    pi = Path(os.environ.get('PI_CODING_AGENT_DIR') or home / '.pi/agent').expanduser()
    if sys.platform == 'win32':
        code = Path(os.environ.get('APPDATA') or home / 'AppData/Roaming')
    elif sys.platform == 'darwin':
        code = home / 'Library/Application Support'
    else:
        code = home / '.config'
    roots = {
        'claude': [home / '.claude/projects'],
        'codex': [codex / 'sessions', codex / 'archived_sessions'],
        'copilot_cli': [home / '.copilot/session-state'],
        'pi': [Path(os.environ.get('PI_CODING_AGENT_SESSION_DIR') or pi / 'sessions')],
        'vscode_chat': [code / 'Code/User', code / 'Code - Insiders/User'],
    }
    if overrides is None:
        overrides = json.loads(os.environ.get('VIEWER_SOURCE_ROOTS') or '{}')
    if not isinstance(overrides, dict) or set(overrides) - set(roots):
        raise ValueError('VIEWER_SOURCE_ROOTS must map known source IDs to arrays of paths')
    for source, values in overrides.items():
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ValueError(f'Roots for {source} must be an array of non-empty path strings')
        roots[source] = values
    classes = [ClaudeAdapter, PiAdapter, CodexAdapter, CopilotCLIAdapter, VSCodeChatAdapter]
    return [cls(roots[cls.source]) for cls in classes]
