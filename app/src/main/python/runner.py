import os
import threading
from http.server import ThreadingHTTPServer

_srv = None


def start(db_path):
    global _srv
    if _srv is not None:
        return _srv.server_address[1]
    os.environ["DB_PATH"] = db_path
    import gg
    _srv = ThreadingHTTPServer(("127.0.0.1", 0), gg.Handler)
    _srv.daemon_threads = True
    threading.Thread(target=_srv.serve_forever, daemon=True).start()
    return _srv.server_address[1]
