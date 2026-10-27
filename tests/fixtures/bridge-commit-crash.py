"""Fault fixture: commit one real operation, then exit before returning its receipt."""

import json
import os
import sys

from memorydreaming.bridge import BridgeServer
from memorydreaming.runtime import MemoryRuntime

runtime = MemoryRuntime(db_path=sys.argv[1])
server = BridgeServer(runtime, user_id="u1", character_id="c1", relationship_id="r1")
response = server.handle(json.loads(sys.stdin.readline()))
if response["ok"]:
    # No reply is emitted. Parent cannot distinguish commit from rollback.
    os._exit(0)
os._exit(3)
