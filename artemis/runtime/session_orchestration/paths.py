"""Windows long-path support for nested workflow/session/attempt artifacts."""

import os
from pathlib import Path


def absolute_path(value):
    path = Path(value).absolute()
    text = str(path)
    prefix = chr(92) * 2 + "?" + chr(92)
    if os.name == "nt" and not text.startswith(prefix):
        if text.startswith(chr(92) * 2):
            text = prefix + "UNC" + chr(92) + text[2:]
        else:
            text = prefix + text
        path = Path(text)
    return path
