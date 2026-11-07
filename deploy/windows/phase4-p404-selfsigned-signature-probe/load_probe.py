import ctypes
import hashlib
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
result = {
    "path": str(target),
    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    "loaded": False,
}
try:
    library = ctypes.WinDLL(str(target))
    result["loaded"] = True
    result["handle"] = library._handle
except BaseException as exc:
    result["error_type"] = type(exc).__name__
    result["error"] = str(exc)
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
